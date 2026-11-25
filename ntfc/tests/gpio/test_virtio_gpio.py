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

"""virtio GPIO device tests.

QEMU's generic ``vhost-user-device`` (VIRTIO device ID 41) is served by
``_vhost_user.py``: eight lines with interrupt support, whose input
levels the test drives and whose direction, output value and interrupt
type it reads back.
``gpiotl -r`` registers the lines as ``/dev/gpio0..7`` with
``gpio_lower_half()``, as board code does, and then drives them with the
GPIO character driver ioctls.

Expectations come from the VIRTIO 1.3 GPIO device section and the NuttX
GPIO driver documentation (``Documentation/os/drivers/character/gpio.rst``).
"""

import re
import time
from typing import Iterator, List

import pytest
from _vhost_user import VhostCtl

TIMEOUT = 10
NLINES = 8
VERDICT_RE = r"gpiotl: (PASS|FAIL)[^\r\n]*"

DIRECTION_OUT = 1
DIRECTION_IN = 2
IRQ_TYPE_NONE = 0
IRQ_TYPE_EDGE_RISING = 1
IRQ_TYPE_EDGE_FALLING = 2
IRQ_TYPE_EDGE_BOTH = 3
IRQ_TYPE_LEVEL_HIGH = 4
MSG_SET_DIRECTION = 3
MSG_GET_VALUE = 4
MSG_SET_VALUE = 5
MSG_SET_IRQ_TYPE = 6


@pytest.fixture(scope="module", autouse=True)
def lines() -> None:
    """Register the virtio GPIO lines as ``/dev/gpioN`` once."""
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        "ls /dev", pattern=r"nsh> ", timeout=TIMEOUT
    )
    if not re.search(r"\bgpio0\b", str(ret.output)):
        assert _gpiotl(f"-r {NLINES}") == f"gpiotl: PASS registered {NLINES}"


@pytest.fixture
def ctl() -> Iterator[VhostCtl]:
    """Connect to the device back-end with an empty request log.

    :return: control client
    """
    client = VhostCtl()
    client("clear")
    yield client
    client.close()


def _gpiotl(args: str) -> str:
    """Run ``gpiotl`` and return its verdict line.

    :param args: gpiotl arguments
    :return: verdict line
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        f"gpiotl {args}", pattern=VERDICT_RE, timeout=TIMEOUT
    )
    out = str(ret.output)
    found = re.search(VERDICT_RE, out)
    assert found, out
    value = re.search(r"gpiotl: value=(\d)", out)
    return found.group(0) + (f" value={value.group(1)}" if value else "")


def _wait_unmasked(ctl: VhostCtl, gpio: int) -> None:
    """Wait until the driver has queued the eventq buffer of a line.

    VIRTIO 1.3: the interrupt of a line is unmasked while its buffer
    pair is queued; "the device should latch the interrupt events sensed
    on a line where the interrupt is masked", so the edges of a masked
    line are one event.

    :param ctl: control client
    :param gpio: line number
    """
    deadline = time.monotonic() + TIMEOUT
    while not ctl("lines")[gpio]["unmasked"]:
        assert time.monotonic() < deadline, f"line {gpio} stays masked"
        time.sleep(0.01)


def _irq(
    ctl: VhostCtl, gpio: int, kind: str, levels: List[int], n: int
) -> str:
    """Count interrupt signals while the test drives an input line.

    :param ctl: control client
    :param gpio: line number
    :param kind: gpiotl interrupt type
    :param levels: levels to drive from low, each once the driver has
     unmasked the interrupt again
    :param n: signals gpiotl waits for
    :return: gpiotl verdict line
    """
    ctl("drive", gpio=gpio, level=0)
    core = pytest.product.core(0)
    ret = core.sendCommand(
        f"gpiotl -d /dev/gpio{gpio} -i {kind} -n {n} -T 5 &",
        "gpiotl: ready",
        timeout=TIMEOUT,
    )
    assert ret == 0
    for level in levels:
        _wait_unmasked(ctl, gpio)
        ctl("drive", gpio=gpio, level=level)
    out = str(core.readUntilPattern(VERDICT_RE, timeout=TIMEOUT).output)
    found = re.search(VERDICT_RE, out)
    assert found, out
    return found.group(0)


def _irq_requests(ctl: VhostCtl, gpio: int) -> List[int]:
    """Return the trigger types the driver set for a line, in order.

    :param ctl: control client
    :param gpio: line number
    :return: ``value`` of each VIRTIO_GPIO_MSG_SET_IRQ_TYPE message
    """
    return [
        r["value"]
        for r in ctl("log")
        if r["q"] == "req"
        and r["type"] == MSG_SET_IRQ_TYPE
        and r["gpio"] == gpio
    ]


def test_virtio_gpio_output(ctl: VhostCtl) -> None:
    """Make line 0 an output and write 1, then 0.

    VIRTIO 1.3: VIRTIO_GPIO_MSG_SET_DIRECTION with "1 = output", and
    VIRTIO_GPIO_MSG_SET_VALUE "to set the value of a line". gpio.rst:
    "The GPIOC_WRITE command sets the value of an output GPIO."
    """
    assert _gpiotl("-d /dev/gpio0 -t out -w 1") == "gpiotl: PASS"
    line = ctl("lines")[0]
    assert (line["direction"], line["value"]) == (DIRECTION_OUT, 1)
    assert _gpiotl("-d /dev/gpio0 -w 0") == "gpiotl: PASS"
    assert ctl("lines")[0]["value"] == 0


def test_virtio_gpio_input(ctl: VhostCtl) -> None:
    """Read line 1 as an input while the test drives it high and low.

    VIRTIO 1.3: VIRTIO_GPIO_MSG_GET_VALUE "to request the device to
    return current value sensed on a line", "0 = low, 1 = high".
    gpio.rst: "The GPIOC_READ command reads the value of a GPIO."
    """
    ctl("drive", gpio=1, level=1)
    assert _gpiotl("-d /dev/gpio1 -t in -R") == "gpiotl: PASS value=1"
    assert ctl("lines")[1]["direction"] == DIRECTION_IN
    ctl("drive", gpio=1, level=0)
    assert _gpiotl("-d /dev/gpio1 -R") == "gpiotl: PASS value=0"
    gets = [r for r in ctl("log") if r.get("type") == MSG_GET_VALUE]
    assert [r["gpio"] for r in gets] == [1, 1]


def test_virtio_gpio_irq_rising(ctl: VhostCtl) -> None:
    """Count rising edges on line 2.

    VIRTIO 1.3: the driver enables the interrupt with
    VIRTIO_GPIO_MSG_SET_IRQ_TYPE and unmasks it by queuing a buffer pair
    on the eventq; the device returns it with VIRTIO_GPIO_IRQ_STATUS_VALID
    on an interrupt. gpio.rst: GPIOC_REGISTER "registers a pin to receive
    a signal whenever there is an interrupt". Two rising edges, two
    signals.
    """
    verdict = _irq(ctl, 2, "rising", [1, 0, 1], 2)
    assert verdict == "gpiotl: PASS irq n=2"
    assert _irq_requests(ctl, 2)[0] == IRQ_TYPE_EDGE_RISING


def test_virtio_gpio_irq_falling(ctl: VhostCtl) -> None:
    """Count falling edges on line 3.

    VIRTIO 1.3: VIRTIO_GPIO_IRQ_TYPE_EDGE_FALLING; two falling edges give
    two signals.
    """
    verdict = _irq(ctl, 3, "falling", [1, 0, 1, 0], 2)
    assert verdict == "gpiotl: PASS irq n=2"
    assert _irq_requests(ctl, 3)[0] == IRQ_TYPE_EDGE_FALLING


def test_virtio_gpio_irq_both(ctl: VhostCtl) -> None:
    """Count both edges on line 4.

    VIRTIO 1.3: VIRTIO_GPIO_IRQ_TYPE_EDGE_BOTH; four edges give four
    signals, each delivery re-armed by the driver ("The driver can queue
    the buffers again to unmask the interrupt").
    """
    verdict = _irq(ctl, 4, "both", [1, 0, 1, 0], 4)
    assert verdict == "gpiotl: PASS irq n=4"
    assert _irq_requests(ctl, 4)[0] == IRQ_TYPE_EDGE_BOTH


def test_virtio_gpio_irq_level_high(ctl: VhostCtl) -> None:
    """Hold line 5 high with a level-high interrupt enabled.

    VIRTIO 1.3: "For level trigger type, the device should notify the
    driver once the interrupt signal on a line is sensed and the
    interrupt is unmasked for the line."
    """
    verdict = _irq(ctl, 5, "high", [1], 3)
    ctl("drive", gpio=5, level=0)
    assert verdict == "gpiotl: PASS irq n=3"
    assert _irq_requests(ctl, 5)[0] == IRQ_TYPE_LEVEL_HIGH


def test_virtio_gpio_irq_disabled_after_use(ctl: VhostCtl) -> None:
    """Check the trigger type once the interrupt is unregistered.

    VIRTIO 1.3: "The driver MUST set the IRQ trigger type to
    VIRTIO_GPIO_IRQ_TYPE_NONE once it is done using the GPIO line
    configured for interrupts." gpiotl unregisters and sets the pin back
    to an input after its verdict.
    """
    verdict = _irq(ctl, 6, "rising", [1], 1)
    assert verdict == "gpiotl: PASS irq n=1"
    assert _irq_requests(ctl, 6)[-1] == IRQ_TYPE_NONE
    line = ctl("lines")[6]
    assert line["irq_type"] == IRQ_TYPE_NONE
    assert not line["unmasked"]
