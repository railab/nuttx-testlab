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

"""Shared helpers for the ``canopt`` CAN feature tests.

``canopt <check> <endpoint> [options]`` (``apps/canopt``) runs one CAN
feature check on a node and prints a single ``canopt: PASS|FAIL ...``
verdict line. Checks that need bus traffic print ``canopt: ready`` once
their sockets are set up; the host then plays its part on vcan ``can0``.

The frame tables below mirror ``apps/canopt/canopt_table.c``.
"""

import re
import socket
import struct
import threading
import time
from typing import Any, Iterator, List, NamedTuple, Tuple

import pytest
from _can_common import CAN_CHARDEV, CAN_IFNAME, can_endpoint, ctucanfd_node

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x7FF
CAN_INV_FILTER = 0x20000000
CANFD_BRS = 0x01
CANFD_ESI = 0x02
CAN_MTU = 16
CANFD_MTU = 72

MARKER_ID = 0x7FE
KICK_ID = 0x7FD

VERDICT_RE = r"canopt: (PASS|FAIL)[^\r\n]*[\r\n]"
READY = "canopt: ready"

CTUCANFD_EFF_BUG = (
    "drivers/can/ctucanfd_pci.c: a 29-bit CAN ID is read from and written "
    "to only the 18-bit id_ext field of the frame ID word, the upper 11 bits "
    "(base ID field) are lost"
)
CTUCANFD_TX_BUG = (
    "drivers/can/ctucanfd_pci.c: ctucanfd_sock_transmit() stores "
    "can_id & CAN_RTR_FLAG and flags & CANFD_ESI in 1-bit bitfields (RTR and "
    "ESI always go out as 0) and drops 29-bit ID bits; drivers/can/"
    "can_common.c: can_bytes2dlc()/can_dlc2bytes() map CAN FD lengths above "
    "8 only with CONFIG_CAN_FD, so a SocketCAN-only build sends and receives "
    "them as 8 bytes"
)
CTUCANFD_FD_RX_BUG = (
    "drivers/can/ctucanfd_pci.c drops the upper 11 bits of 29-bit CAN IDs; "
    "drivers/can/can_common.c: can_dlc2bytes() maps CAN FD DLC 9..15 to 8 "
    "bytes unless CONFIG_CAN_FD (character driver) is set"
)

# (node, deadline) of background canopt checks that may still run

_PENDING: List[Tuple[int, float]] = []


class Frame(NamedTuple):
    """One frame of the shared host/node frame tables."""

    can_id: int
    length: int
    flags: int = 0
    fd: bool = False


EFF = CAN_EFF_FLAG
RTR = CAN_RTR_FLAG
BRS = CANFD_BRS
ESI = CANFD_ESI

CLASSIC_FRAMES = [
    Frame(0x100, 0),
    Frame(0x101, 1),
    Frame(0x102, 2),
    Frame(0x103, 3),
    Frame(0x104, 4),
    Frame(0x105, 5),
    Frame(0x106, 6),
    Frame(0x107, 7),
    Frame(0x108, 8),
    Frame(0x123, 8),
    Frame(0x456, 8),
    Frame(0x7FF, 8),
    Frame(0x000, 8),
    Frame(EFF | 0x123, 8),
    Frame(EFF | 0x12345678, 8),
    Frame(EFF | 0x1FFFFFFF, 8),
    Frame(RTR | 0x321, 4),
    Frame(RTR | EFF | 0x1234567, 2),
]

FD_FRAMES = [
    Frame(0x200, 0, 0, True),
    Frame(0x201, 1, BRS, True),
    Frame(0x202, 7, ESI, True),
    Frame(EFF | 0x1800003, 8, BRS | ESI, True),
    Frame(0x204, 12, 0, True),
    Frame(0x205, 16, BRS, True),
    Frame(0x206, 20, ESI, True),
    Frame(EFF | 0x1800007, 24, BRS | ESI, True),
    Frame(0x208, 32, 0, True),
    Frame(0x209, 48, BRS, True),
    Frame(0x20A, 64, ESI, True),
]


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def frame_data(can_id: int, length: int) -> bytes:
    """Return the payload of a table frame.

    :param can_id: CAN ID with flags
    :param length: payload length in bytes
    :return: bytes ``((can_id & 0xff) + 7 * k + length) & 0xff``, empty
     for RTR frames
    """
    if can_id & CAN_RTR_FLAG:
        return b""
    return bytes(
        ((can_id & 0xFF) + 7 * k + length) & 0xFF for k in range(length)
    )


def interleaved_frames() -> List[Frame]:
    """Return both tables interleaved, CAN FD frame first.

    :return: ``FD[0], C[0], FD[1], C[1], ...`` then the remaining classic
     frames
    """
    out: List[Frame] = []
    for i in range(max(len(FD_FRAMES), len(CLASSIC_FRAMES))):
        if i < len(FD_FRAMES):
            out.append(FD_FRAMES[i])
        if i < len(CLASSIC_FRAMES):
            out.append(CLASSIC_FRAMES[i])
    return out


def filter_match(can_id: int, filters: List[Tuple[int, int]]) -> bool:
    """Apply SocketCAN ``CAN_RAW_FILTER`` semantics to one CAN ID.

    :param can_id: CAN ID with flags
    :param filters: ``(can_id, can_mask)`` filter list
    :return: True if any filter matches (``CAN_INV_FILTER`` inverts one)
    """
    for fid, mask in filters:
        hit = (can_id & mask) == (fid & ~CAN_INV_FILTER & mask)
        if bool(fid & CAN_INV_FILTER) != hit:
            return True
    return False


def ctucanfd_eff_mangled(can_id: int) -> int:
    """Return the CAN ID a CTU CAN FD node sees for a sent ID.

    :param can_id: CAN ID with flags
    :return: the ID with only the low 18 bits of a 29-bit ID kept
    """
    if can_id & CAN_EFF_FLAG:
        return can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG | 0x3FFFF)
    return can_id


def xfail_on_ctucanfd(
    request: pytest.FixtureRequest, reason: str, strict: bool = True
) -> None:
    """Mark the running test xfail if node 0 uses the CTU CAN FD driver.

    :param request: pytest request of the running test
    :param reason: xfail reason
    :param strict: strict xfail
    """
    if ctucanfd_node(0):
        request.applymarker(pytest.mark.xfail(strict=strict, reason=reason))


def chardev_node(node: int) -> bool:
    """Check whether a node reaches the bus through the character driver.

    :param node: product index
    :return: True if ``can_endpoint(node)`` is ``/dev/can0``
    """
    return can_endpoint(node) == CAN_CHARDEV


def kv_value(node: int, name: str) -> Any:
    """Return a Kconfig value from a node's build.

    :param node: product index
    :param name: Kconfig symbol, e.g. ``CONFIG_CAN_RXFIFOSIZE``
    :return: the parsed value (bool, int or str), False if not set
    """
    return _core(node).conf.kv_check(name)


# --- host side --------------------------------------------------------------


def host_socket(fd: bool = True, timeout: float = 5.0) -> socket.socket:
    """Open and bind an unfiltered host CAN_RAW socket on ``can0``.

    :param fd: enable CAN FD frames
    :param timeout: receive timeout in seconds
    :return: bound socket
    """
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    if fd:
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1)
    sock.settimeout(timeout)
    sock.bind((CAN_IFNAME,))
    return sock


def pack(frame: Frame) -> bytes:
    """Pack a table frame for ``socket.send``.

    :param frame: table frame
    :return: ``can_frame`` or ``canfd_frame`` bytes
    """
    data = frame_data(frame.can_id, frame.length)
    if frame.fd:
        return struct.pack(
            "=IBB2x64s", frame.can_id, frame.length, frame.flags, data
        )
    return struct.pack("=IB3x8s", frame.can_id, frame.length, data)


def unpack(raw: bytes) -> Tuple[int, int, int, bytes]:
    """Unpack a frame read from a host socket.

    :param raw: raw ``can_frame`` / ``canfd_frame`` bytes
    :return: ``(can_id, length, flags, data)``; flags only for CAN FD
    """
    can_id, length, flags = struct.unpack_from("=IBB", raw, 0)
    if len(raw) == CAN_MTU:
        flags = 0
    return can_id, length, flags, raw[8 : 8 + length]


def host_send(frames: List[Frame], gap_s: float = 0.002) -> None:
    """Send frames from the host.

    :param frames: frames to send, in order
    :param gap_s: delay between frames in seconds
    """
    sock = host_socket()
    try:
        for frame in frames:
            sock.send(pack(frame))
            if gap_s:
                time.sleep(gap_s)
    finally:
        sock.close()


def host_send_marker() -> None:
    """Send the end-of-sequence marker frame (``MARKER_ID``)."""
    host_send([Frame(MARKER_ID, 1)], gap_s=0)


def host_expect(sock: socket.socket, frames: List[Frame]) -> List[str]:
    """Receive frames on a host socket and compare them to the tables.

    :param sock: bound host socket (CAN FD enabled)
    :param frames: frames expected, in order
    :return: list of mismatch descriptions, empty if all arrived intact
    """
    errors: List[str] = []
    for want in frames:
        try:
            raw = sock.recv(CANFD_MTU)
        except OSError:
            errors.append(f"missing {want.can_id:x}")
            break
        can_id, length, flags, data = unpack(raw)
        size = CANFD_MTU if want.fd else CAN_MTU
        got = (can_id, length, flags & (BRS | ESI), len(raw))
        exp = (want.can_id, want.length, want.flags, size)
        if got != exp:
            errors.append(f"got {got} want {exp}")
        elif not can_id & CAN_RTR_FLAG and data != frame_data(
            want.can_id, want.length
        ):
            errors.append(f"data {can_id:x}")
    return errors


class Kicker:
    """Background host thread sending ``KICK_ID`` frames.

    Wakes up a node reader that blocks although frames are queued.
    """

    def __init__(self, delay_s: float, period_s: float = 0.05) -> None:
        """Prepare the kicker.

        :param delay_s: delay before the first kick
        :param period_s: kick period
        """
        self._delay_s = delay_s
        self._period_s = period_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        """Send kicks until stopped."""
        if self._stop.wait(self._delay_s):
            return
        sock = host_socket()
        try:
            while not self._stop.wait(self._period_s):
                sock.send(pack(Frame(KICK_ID, 0)))
        finally:
            sock.close()

    def __enter__(self) -> "Kicker":
        """Start kicking.

        :return: self
        """
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop kicking.

        :param exc: exception info (ignored)
        """
        self._stop.set()
        self._thread.join()


# --- node side --------------------------------------------------------------


def canopt_run(node: int, args: str, timeout: int = 30) -> str:
    """Run ``canopt`` in the foreground and return its verdict line.

    :param node: product index
    :param args: canopt arguments (check, endpoint, options)
    :param timeout: command timeout in seconds
    :return: verdict line or empty string
    """
    ret = _core(node).sendCommandReadUntilPattern(
        f"canopt {args}", pattern=VERDICT_RE, timeout=timeout
    )
    return verdict_of(ret.output)


def canopt_start(node: int, args: str, timeout_s: int = 20) -> None:
    """Start ``canopt`` in the background and wait for ``canopt: ready``.

    :param node: product index
    :param args: canopt arguments (check, endpoint, options)
    :param timeout_s: how long the check may run before it is drained
    """
    ret = _core(node).sendCommand(f"canopt {args} &", READY, timeout=10)
    assert ret == 0, f"canopt {args}: not ready"
    _PENDING.append((node, time.monotonic() + timeout_s + 5))


def canopt_verdict(node: int, timeout: int = 30) -> str:
    """Read a background check's verdict line.

    :param node: product index
    :param timeout: how long to wait for the verdict
    :return: verdict line or empty string
    """
    ret = _core(node).readUntilPattern(VERDICT_RE, timeout=timeout)
    canopt_forget(node)
    return verdict_of(ret.output)


def canopt_forget(node: int) -> None:
    """Stop tracking a background check whose verdict was read elsewhere.

    :param node: product index
    """
    for i, (pending, _deadline) in enumerate(_PENDING):
        if pending == node:
            _PENDING.pop(i)
            break


def verdict_of(output: str) -> str:
    """Extract the canopt verdict line from command output.

    :param output: device output
    :return: verdict line or empty string
    """
    found = re.search(VERDICT_RE, output)
    return found.group(0).rstrip("\r\n") if found else ""


def canopt_cleanup() -> None:
    """Make background checks left by a failed test finish.

    Sends end markers and kick frames so blocked readers return, then
    drains each check's verdict up to its deadline.
    """
    if not _PENDING:
        return
    try:
        host_send([Frame(MARKER_ID, 1), Frame(KICK_ID, 0)] * 3, gap_s=0.01)
    except OSError:
        pass
    while _PENDING:
        node, deadline = _PENDING.pop()
        remaining = max(1.0, deadline - time.monotonic())
        try:
            _core(node).readUntilPattern(VERDICT_RE, timeout=remaining)
        except Exception:  # noqa: BLE001 - best-effort drain
            pass


def can_ifup_all() -> None:
    """Bring up ``can0`` on every node that uses SocketCAN."""
    for node in range(len(pytest.products)):
        if can_endpoint(node) == CAN_IFNAME:
            ret = _core(node).sendCommand(
                f"ifup {CAN_IFNAME}", "OK", timeout=10
            )
            assert ret == 0


@pytest.fixture
def canopt_env() -> Iterator[None]:
    """Bring up the bus and clean up background checks after a test.

    :return: fixture generator
    """
    can_ifup_all()
    yield
    canopt_cleanup()
