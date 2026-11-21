############################################################################
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
############################################################################

"""virtio input device tests.

QEMU gives the node a virtio keyboard and a virtio mouse or tablet; the
test injects events with the QMP ``input-send-event`` command (one
command is one device update, ended by ``EV_SYN``) and ``inputtl`` on
the node prints the reports read from ``/dev/kbd0``, ``/dev/mouse0`` or
``/dev/input0``.

Expectations come from the VIRTIO 1.2 input device section (events are
evdev events, an update ends with ``EV_SYN``), the evdev key codes it
refers to, and the NuttX input interfaces (``keyboard.h``, ``mouse.h``,
``touchscreen.h``).
"""

import re
from typing import Any, Dict, List

import pytest
from _qmp_common import Qmp

VERDICT_RE = r"inputtl: (PASS|FAIL) n=\d+[\r\n]"
TIMEOUT = 10

KEYBOARD_PRESS = 0
KEYBOARD_RELEASE = 1
KEYBOARD_SPECPRESS = 2
KEYBOARD_SPECREL = 3
KEYCODE_UP = 7

MOUSE_BUTTON_1 = 1 << 0

TOUCH_DOWN = 1 << 0
TOUCH_MOVE = 1 << 1
TOUCH_UP = 1 << 2
TOUCH_ID_VALID = 1 << 3
TOUCH_POS_VALID = 1 << 4


def _key(qcode: str, down: bool) -> Dict[str, Any]:
    """Build a QMP key event.

    :param qcode: QEMU key code name
    :param down: press instead of release
    :return: ``InputEvent`` object
    """
    return {
        "type": "key",
        "data": {"down": down, "key": {"type": "qcode", "data": qcode}},
    }


def _btn(down: bool) -> Dict[str, Any]:
    """Build a QMP left button event.

    :param down: press instead of release
    :return: ``InputEvent`` object
    """
    return {"type": "btn", "data": {"down": down, "button": "left"}}


def _axis(kind: str, axis: str, value: int) -> Dict[str, Any]:
    """Build a QMP relative or absolute axis event.

    :param kind: ``rel`` or ``abs``
    :param axis: ``x`` or ``y``
    :param value: axis value
    :return: ``InputEvent`` object
    """
    return {"type": kind, "data": {"axis": axis, "value": value}}


def _inject(
    kind: str, dev: str, updates: List[List[Dict[str, Any]]], count: int
) -> List[str]:
    """Read ``count`` reports on the node while sending ``updates``.

    :param kind: inputtl device kind, ``m``, ``k`` or ``t``
    :param dev: device path
    :param updates: device updates to send, in order
    :param count: reports the node must read
    :return: report lines, without the ``inputtl:`` prefix
    """
    core = pytest.product.core(0)
    ret = core.sendCommand(
        f"inputtl -{kind} -d {dev} -n {count} -T {TIMEOUT} &",
        "inputtl: ready",
        timeout=TIMEOUT,
    )
    assert ret == 0
    qmp = Qmp()
    try:
        for events in updates:
            qmp.execute("input-send-event", events=events)
    finally:
        qmp.close()
    out = str(core.readUntilPattern(VERDICT_RE, timeout=TIMEOUT * 2).output)
    found = re.search(VERDICT_RE, out)
    assert found, out
    reports = re.findall(r"inputtl: ((?:key|mouse|touch) [^\r\n]*)", out)
    assert found.group(1) == "PASS", reports
    return reports


def _keys(updates: List[List[Dict[str, Any]]], count: int) -> List[str]:
    """Inject keyboard updates and return the key reports.

    :param updates: device updates to send
    :param count: key events the node must read
    :return: ``key type=<t> code=<c>`` lines
    """
    return _inject("k", "/dev/kbd0", updates, count)


def test_virtio_input_registered() -> None:
    """Check that each virtio input device got its NuttX device node.

    VIRTIO 1.2, input device configuration: VIRTIO_INPUT_CFG_EV_BITS
    "returns a bitmap of supported event codes" per event type, so the
    driver tells a keyboard (EV_KEY), a mouse (EV_REL) and a tablet
    (EV_ABS) apart.
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        "ls /dev", pattern=r"nsh> ", timeout=TIMEOUT
    )
    out = str(ret.output)
    assert re.search(r"\bkbd0\b", out), out
    args = str(pytest.product.core(0).conf.exec_args)
    if "virtio-mouse" in args:
        assert re.search(r"\bmouse0\b", out), out
    if "virtio-tablet" in args:
        assert re.search(r"\binput0\b", out), out


def test_virtio_input_key_press_release() -> None:
    """Press and release ``a``, one update each.

    evdev ``EV_KEY`` value 1 is a press, 0 a release; ``keyboard.h``: "A
    normal key carries its character in the code field".
    """
    reports = _keys([[_key("a", True)], [_key("a", False)]], 2)
    assert reports == [
        f"key type={KEYBOARD_PRESS} code={ord('a')}",
        f"key type={KEYBOARD_RELEASE} code={ord('a')}",
    ]


def test_virtio_input_special_key() -> None:
    """Press and release the up arrow.

    ``keyboard.h``: "A special key carries a value from enum
    kbd_keycode_e instead, and must be reported with one of the SPEC
    types".
    """
    reports = _keys([[_key("up", True)], [_key("up", False)]], 2)
    assert reports == [
        f"key type={KEYBOARD_SPECPRESS} code={KEYCODE_UP}",
        f"key type={KEYBOARD_SPECREL} code={KEYCODE_UP}",
    ]


def test_virtio_input_bracket_keys() -> None:
    """Press the ``[`` and ``]`` keys.

    evdev KEY_LEFTBRACE and KEY_RIGHTBRACE are the keys that type ``[``
    and ``]`` without a modifier (USB HID usages 0x2F "Keyboard [ and {"
    and 0x30 "Keyboard ] and }").
    """
    reports = _keys(
        [
            [_key("bracket_left", True)],
            [_key("bracket_left", False)],
            [_key("bracket_right", True)],
            [_key("bracket_right", False)],
        ],
        4,
    )
    assert reports == [
        f"key type={KEYBOARD_PRESS} code={ord('[')}",
        f"key type={KEYBOARD_RELEASE} code={ord('[')}",
        f"key type={KEYBOARD_PRESS} code={ord(']')}",
        f"key type={KEYBOARD_RELEASE} code={ord(']')}",
    ]


def test_virtio_input_keys_one_update() -> None:
    """Press two keys in one device update.

    VIRTIO 1.2, input device: an update "consists of several input
    events ... and a terminating EV_SYN event"; every ``EV_KEY`` of the
    update is a key change, so both presses are reported.
    """
    reports = _keys(
        [
            [_key("a", True), _key("b", True)],
            [_key("a", False), _key("b", False)],
        ],
        4,
    )
    assert sorted(reports[:2]) == [
        f"key type={KEYBOARD_PRESS} code={ord('a')}",
        f"key type={KEYBOARD_PRESS} code={ord('b')}",
    ]
    assert sorted(reports[2:]) == [
        f"key type={KEYBOARD_RELEASE} code={ord('a')}",
        f"key type={KEYBOARD_RELEASE} code={ord('b')}",
    ]


def _need(device: str) -> None:
    """Skip unless the product has a QEMU device of this kind.

    :param device: QEMU device name prefix, e.g. ``virtio-mouse``
    """
    if device not in str(pytest.product.core(0).conf.exec_args):
        pytest.skip(f"no {device} in this product")


def test_virtio_input_mouse_motion_button() -> None:
    """Move the mouse with the left button down, then release it.

    evdev: ``EV_REL`` values are motion deltas of the update, ``BTN_LEFT``
    value 1/0 is press/release; ``mouse.h``: ``MOUSE_BUTTON_1`` "True: Left
    mouse button pressed". The release update has no motion.
    """
    _need("virtio-mouse")
    reports = _inject(
        "m",
        "/dev/mouse0",
        [
            [_axis("rel", "x", 10), _axis("rel", "y", -5), _btn(True)],
            [_btn(False)],
        ],
        2,
    )
    assert reports == [
        f"mouse buttons={MOUSE_BUTTON_1} x=10 y=-5",
        "mouse buttons=0 x=0 y=0",
    ]


def test_virtio_input_tablet_touch() -> None:
    """Touch, move and release on the tablet.

    evdev: ``EV_ABS`` values are absolute positions, the button is the
    contact; ``touchscreen.h``: TOUCH_DOWN "A new touch contact is
    established", TOUCH_MOVE "Movement occurred with previously reported
    contact", TOUCH_UP "The touch contact was lost".
    """
    _need("virtio-tablet")
    reports = _inject(
        "t",
        "/dev/input0",
        [
            [_axis("abs", "x", 1000), _axis("abs", "y", 2000), _btn(True)],
            [_axis("abs", "x", 1500), _axis("abs", "y", 2500)],
            [_btn(False)],
        ],
        3,
    )
    valid = TOUCH_ID_VALID | TOUCH_POS_VALID
    assert reports[0] == (
        f"touch npoints=1 flags=0x{TOUCH_DOWN | valid:02x} x=1000 y=2000"
    )
    assert reports[1] == (
        f"touch npoints=1 flags=0x{TOUCH_MOVE | valid:02x} x=1500 y=2500"
    )
    flags = int(re.search(r"flags=0x([0-9a-f]+)", reports[2]).group(1), 16)
    assert flags & (TOUCH_DOWN | TOUCH_MOVE | TOUCH_UP) == TOUCH_UP
