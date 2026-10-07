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

"""Shared cantl helpers for multi-node CAN tests.

All nodes (sim processes, or QEMU's ``can-host-socketcan`` bridge) and the
host share a single vcan interface, ``can0``: every frame sent by any of
them is delivered to every other socket on the bus.  A node reaches that
bus either over SocketCAN (``CONFIG_NET_CAN``, ifname ``can0``) or through
the CAN character driver (``CONFIG_CAN``, ``/dev/can0``); see
``can_endpoint()``.
"""

import re
import socket
import struct
import time
from typing import Any, List, Optional, Tuple

import pytest

CAN_IFNAME = "can0"
CAN_CHARDEV = "/dev/can0"
CAN_ID_DEFAULT = 0x123
CAN_ID_ALT = 0x456

CAN_MAX_DLEN = 8
CANFD_MAX_DLEN = 64
CAN_MTU = 16
CANFD_MTU = 72
SEQ_HDR_LEN = 4

SOL_CAN_RAW = socket.SOL_CAN_RAW
CAN_RAW_FILTER = socket.CAN_RAW_FILTER
CAN_RAW_FD_FRAMES = socket.CAN_RAW_FD_FRAMES
CAN_SFF_MASK = 0x7FF
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
EXACT_ID_MASK = CAN_SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG

TX_VERDICT_RE = r"cantl: (PASS|FAIL) tx=[^\r\n]*[\r\n]"
RX_VERDICT_RE = r"cantl: (PASS|FAIL) rx=[^\r\n]*[\r\n]"

# (node, deadline) of cantl receivers started by the current test that may
# still be running when it ends.

_PENDING: List[Tuple[int, float]] = []


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def frame_pattern(seq: int, length: int) -> bytes:
    """Return the cantl data pattern for one frame.

    :param seq: frame sequence number
    :param length: number of pattern bytes (frame length minus the
     4-byte sequence header)
    :return: pattern bytes, byte ``k`` is ``(seq * 31 + 7 + k) & 0xff``
    """
    return bytes((seq * 31 + 7 + k) & 0xFF for k in range(length))


def can_endpoint(node: int) -> str:
    """Return the cantl endpoint a node's build reaches the CAN bus on.

    :param node: product index
    :return: ``CAN_IFNAME`` if the node has ``CONFIG_NET_CAN`` (SocketCAN),
     else ``CAN_CHARDEV`` if it has ``CONFIG_CAN`` (character driver)
    """
    conf = _core(node).conf
    if conf.kv_check("CONFIG_NET_CAN"):
        return CAN_IFNAME

    assert conf.kv_check("CONFIG_CAN"), "node has neither NET_CAN nor CAN"
    return CAN_CHARDEV


def canfd_supported(node: int) -> bool:
    """Check whether a node's build has a CAN FD capable driver.

    :param node: product index
    :return: True if the node's active backend (SocketCAN or the CAN
     character driver) was built with CAN FD support
    """
    conf = _core(node).conf
    if conf.kv_check("CONFIG_NET_CAN"):
        return bool(conf.kv_check("CONFIG_NET_CAN_HAVE_CANFD"))

    return bool(conf.kv_check("CONFIG_CAN_FD"))


def cantl_sender(
    node: int,
    count: int,
    can_id: int = CAN_ID_DEFAULT,
    fd: bool = False,
    gap_ms: int = 0,
    timeout: int = 60,
    endpoint: Optional[str] = None,
) -> str:
    """Run ``cantl -s`` on a node and return its verdict line.

    :param node: product index
    :param count: number of frames to send
    :param can_id: CAN ID to send with
    :param fd: use CAN FD frames (64-byte payload) instead of classic
    :param gap_ms: delay between frames in milliseconds
    :param timeout: command timeout in seconds
    :param endpoint: cantl device argument; defaults to ``can_endpoint(node)``
    :return: verdict line or empty string
    """
    flags = "-f " if fd else ""
    dev = endpoint if endpoint is not None else can_endpoint(node)
    ret = _core(node).sendCommandReadUntilPattern(
        f"cantl -s {dev} -n {count} -i 0x{can_id:x} " f"{flags}-g {gap_ms}",
        pattern=TX_VERDICT_RE,
        timeout=timeout,
    )
    found = re.search(TX_VERDICT_RE, ret.output)
    return found.group(0).rstrip("\r\n") if found else ""


def cantl_receiver_start(
    node: int,
    count: int,
    can_id: int = CAN_ID_DEFAULT,
    fd: bool = False,
    timeout: int = 20,
    endpoint: Optional[str] = None,
) -> None:
    """Start a background ``cantl -r`` receiver on a node.

    :param node: product index
    :param count: number of frames the receiver expects
    :param can_id: exact CAN ID the receiver filters for
    :param fd: accept CAN FD frames (64-byte payload) instead of classic
    :param timeout: receiver's own idle timeout in seconds
    :param endpoint: cantl device argument; defaults to ``can_endpoint(node)``
    """
    flags = "-f " if fd else ""
    dev = endpoint if endpoint is not None else can_endpoint(node)
    ret = _core(node).sendCommand(
        f"cantl -r {dev} -n {count} -i 0x{can_id:x} " f"{flags}-t {timeout} &",
        "listening",
        timeout=10,
    )
    assert ret == 0
    _PENDING.append((node, time.monotonic() + timeout + 10))


def cantl_receiver_verdict(node: int, timeout: int = 30) -> str:
    """Read a background receiver's verdict line and stop tracking it.

    :param node: product index
    :param timeout: how long to wait for the verdict line
    :return: verdict line or empty string
    """
    ret = _core(node).readUntilPattern(RX_VERDICT_RE, timeout=timeout)
    for i, (pending_node, _deadline) in enumerate(_PENDING):
        if pending_node == node:
            _PENDING.pop(i)
            break

    found = re.search(RX_VERDICT_RE, ret.output)
    return found.group(0).rstrip("\r\n") if found else ""


def cantl_cleanup() -> None:
    """Let receivers left running by a failed test exit on their own.

    There is no in-band way to wake a ``cantl -r`` early without matching
    its exact-ID filter, so this just drains output up to each receiver's
    own ``-t`` deadline; it never kills the task (NuttX can leak a socket
    when a task blocked in accept()/read() is killed).
    """
    while _PENDING:
        node, deadline = _PENDING.pop()
        remaining = max(1.0, deadline - time.monotonic())
        try:
            _core(node).readUntilPattern(RX_VERDICT_RE, timeout=remaining)
        except Exception:  # noqa: BLE001 - best-effort drain
            pass


def _pack_frame(can_id: int, seq: int, length: int, fd: bool) -> bytes:
    """Pack one SocketCAN frame matching the cantl wire format.

    :param can_id: CAN ID
    :param seq: frame sequence number
    :param length: total payload length (8 or 64)
    :param fd: pack as ``canfd_frame`` instead of classic ``can_frame``
    :return: raw frame bytes ready for ``socket.send``
    """
    payload = struct.pack(">I", seq) + frame_pattern(seq, length - SEQ_HDR_LEN)
    data = payload.ljust(CANFD_MAX_DLEN if fd else CAN_MAX_DLEN, b"\x00")
    return struct.pack("=IB3x64s" if fd else "=IB3x8s", can_id, length, data)


def _unpack_frame(raw: bytes) -> Tuple[int, int, bytes]:
    """Unpack one SocketCAN frame.

    :param raw: raw frame bytes read from a CAN_RAW socket
    :return: ``(can_id, length, data)``
    """
    can_id, length = struct.unpack_from("=IB", raw, 0)
    return can_id, length, raw[8 : 8 + length]


def host_can_socket(
    fd: bool = False, can_id: Optional[int] = None, timeout: float = 5.0
) -> socket.socket:
    """Open and bind a host-side CAN_RAW socket on ``can0``.

    :param fd: enable receiving/sending CAN FD frames
    :param can_id: if given, install an exact-match ``CAN_RAW_FILTER``
    :param timeout: socket receive timeout in seconds
    :return: bound socket
    """
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    if fd:
        sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)
    if can_id is not None:
        sock.setsockopt(
            SOL_CAN_RAW,
            CAN_RAW_FILTER,
            struct.pack("=II", can_id, EXACT_ID_MASK),
        )
    sock.settimeout(timeout)
    sock.bind((CAN_IFNAME,))
    return sock


def host_can_send(
    count: int,
    can_id: int = CAN_ID_DEFAULT,
    fd: bool = False,
    gap_s: float = 0.0,
) -> bool:
    """Send ``count`` cantl-pattern frames from the host.

    :param count: number of frames to send
    :param can_id: CAN ID to send with
    :param fd: send CAN FD frames (64-byte payload) instead of classic
    :param gap_s: delay between frames in seconds
    :return: True if every frame was sent
    """
    length = CANFD_MAX_DLEN if fd else CAN_MAX_DLEN
    sock = host_can_socket(fd=fd)
    try:
        for seq in range(count):
            sock.send(_pack_frame(can_id, seq, length, fd))
            if gap_s:
                time.sleep(gap_s)
    finally:
        sock.close()
    return True


def host_can_verify(
    sock: socket.socket, count: int, fd: bool = False
) -> Tuple[int, int, int]:
    """Receive and verify ``count`` cantl-pattern frames on a socket.

    :param sock: a bound, already-filtered (or unfiltered) socket
    :param count: number of frames expected
    :param fd: expect CAN FD frames (64-byte payload) instead of classic
    :return: ``(rx, lost, err)``
    """
    mtu = CANFD_MTU if fd else CAN_MTU
    rx = 0
    lost = 0
    err = 0
    expected = 0
    while expected < count:
        try:
            raw = sock.recv(mtu + 16)
        except OSError:
            break
        if len(raw) != mtu:
            err += 1
            continue
        _can_id, length, data = _unpack_frame(raw)
        seq = struct.unpack(">I", data[:SEQ_HDR_LEN])[0]
        if seq < expected:
            err += 1
            continue
        lost += seq - expected
        expected = seq
        if data[SEQ_HDR_LEN:length] != frame_pattern(
            seq, length - SEQ_HDR_LEN
        ):
            err += 1
        expected += 1
        rx += 1
    if expected < count:
        lost += count - expected
    return rx, lost, err
