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

"""ISO-TP socket tests against host SocketCAN ISO-TP peers."""

import socket
import struct
from contextlib import ExitStack, contextmanager
from typing import Iterator

import pytest
from _can_common import host_can_socket

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_CAN_ISOTP"),
    pytest.mark.cmd_check("isotptl_main"),
]


def _payload(seed: int, length: int) -> bytes:
    """Build the test payload pattern.

    :param seed: phase and sequence seed
    :param length: message length
    :return: expected payload
    """
    return bytes((seed + i * 31 + length * 7) & 255 for i in range(length))


@contextmanager
def _peer(fd: bool, rx: int, tx: int) -> Iterator[socket.socket]:
    """Open a host kernel ISO-TP peer.

    :param fd: use CAN FD
    :param rx: peer receive CAN identifier
    :param tx: peer transmit CAN identifier
    :return: configured ISO-TP socket
    """
    with socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, 6) as peer:
        peer.settimeout(5)
        peer.setsockopt(106, 2, struct.pack("=BBB", 8, 1, 0))
        if fd:
            peer.setsockopt(106, 5, struct.pack("=BBB", 72, 64, 1))
        peer.bind(("can0", rx, tx))
        yield peer


def _start(mode: str, fd: bool, wait: bool = False, loops: int = 3) -> None:
    """Start one foreground target phase.

    :param mode: target phase
    :param fd: use CAN FD
    :param wait: request WAIT_TX_DONE
    :param loops: exchange count
    """
    assert (
        pytest.products[0]
        .core(0)
        .sendCommand(
            f"isotptl {mode} can0 {loops} {int(fd)} {int(wait)}",
            (
                "isotptl: bound"
                if mode in {"exchange", "dontwait", "async", "fault", "large"}
                else "isotptl: start"
            ),
            timeout=10,
        )
        == 0
    )


def _verdict(mode: str) -> None:
    """Require the target's final verdict.

    :param mode: expected phase
    """
    result = (
        pytest.products[0]
        .core(0)
        .readUntilPattern(r"isotptl: (PASS|FAIL)[^\r\n]*[\r\n]", timeout=30)
        .output
    )
    assert f"isotptl: PASS mode={mode}" in result, result


def _raw_frame(can_id: int, data: bytes = b"", fd: bool = False) -> bytes:
    """Pack a SocketCAN data frame.

    :param can_id: CAN identifier
    :param data: frame payload
    :param fd: use CAN FD
    :return: packed frame
    """
    return struct.pack(
        "=IB3x64s" if fd else "=IB3x8s", can_id, len(data), data
    )


@pytest.mark.parametrize("fd", [False, True], ids=["classic", "fd"])
@pytest.mark.parametrize("wait", [False, True], ids=["async", "wait-tx"])
def test_isotp_exchange(fd: bool, wait: bool) -> None:
    """Exchange segmented PDUs with the host in both send-completion modes.

    :param fd: use CAN FD
    :param wait: wait for target transmission completion
    """
    length = 384 if fd else 255
    with _peer(fd, 0x457 if fd else 0x456, 0x655 if fd else 0x654) as peer:
        _start("exchange", fd, wait)
        for seq in range(3):
            data = _payload((0x120 if fd else 0x100) + seq, length)
            assert peer.send(data) == length
            assert peer.recv(8192) == _payload(
                (0x220 if fd else 0x200) + seq, length
            )
        _verdict("exchange")


@pytest.mark.parametrize("fd", [False, True], ids=["classic", "fd"])
def test_isotp_wait_dontwait(fd: bool) -> None:
    """Reject a WAIT_TX_DONE send with MSG_DONTWAIT.

    :param fd: use CAN FD
    """
    length = 384 if fd else 255
    with _peer(fd, 0x457 if fd else 0x456, 0x655 if fd else 0x654) as peer:
        _start("dontwait", fd)
        assert peer.send(_payload(0x320 if fd else 0x300, length)) == length
        _verdict("dontwait")


@pytest.mark.parametrize("fd", [False, True], ids=["classic", "fd"])
def test_isotp_back_to_back(fd: bool) -> None:
    """Two blocking sends serialize while an asynchronous TX is active.

    :param fd: use CAN FD
    """
    length = 384 if fd else 255
    with _peer(fd, 0x457 if fd else 0x456, 0x655 if fd else 0x654) as peer:
        with host_can_socket(can_id=0x738, timeout=10) as ready:
            _start("backtoback", fd)
            assert ready.recv(16) == _raw_frame(0x738, b"\xa5")
            ready.send(_raw_frame(0x739))
            for seq in range(2):
                assert peer.recv(8192) == _payload(
                    (0x570 if fd else 0x560) + seq, length
                )
            _verdict("backtoback")


def test_isotp_concurrent_sessions() -> None:
    """Three independent address pairs keep their session state separate."""
    with ExitStack() as stack:
        peers = [
            stack.enter_context(_peer(False, 0x470 + i, 0x460 + i))
            for i in range(3)
        ]
        ready = stack.enter_context(host_can_socket(can_id=0x478, timeout=10))
        _start("concurrent", False)
        assert ready.recv(16) == _raw_frame(0x478, b"\xa5")
        ready.send(_raw_frame(0x479))
        for seq in range(3):
            for index, peer in enumerate(peers):
                assert (
                    peer.send(_payload(0x600 + index * 32 + seq, 255)) == 255
                )
            for index, peer in enumerate(peers):
                assert peer.recv(8192) == _payload(
                    0x680 + index * 32 + seq, 255
                )
        _verdict("concurrent")


@pytest.mark.parametrize("fd", [False, True], ids=["classic", "fd"])
@pytest.mark.parametrize("wait", [False, True], ids=["async", "wait-tx"])
def test_isotp_completion_order(fd: bool, wait: bool) -> None:
    """Delay FC and verify the application marker's wire ordering.

    :param fd: use CAN FD
    :param wait: wait for completion before the marker
    """
    tx_id, rx_id = (0x457, 0x655) if fd else (0x456, 0x654)
    marker = 0x735 if fd else 0x734
    with host_can_socket(fd=fd, timeout=3) as peer:
        _start("async", fd, wait)
        peer.send(_raw_frame(0x736, bytes([int(fd), int(wait)])))
        first = peer.recv(72)
        assert struct.unpack_from("=I", first)[0] == tx_id
        length = ((first[8] & 15) << 8) | first[9]
        payload = first[10 : 8 + first[4]]
        if wait:
            peer.settimeout(0.1)
            with pytest.raises(socket.timeout):
                peer.recv(72)
            peer.settimeout(3)
        else:
            assert struct.unpack_from("=I", peer.recv(72))[0] == marker
        peer.send(_raw_frame(rx_id, b"\x30\x00\x01", fd))
        seq = 1
        while len(payload) < length:
            frame = peer.recv(72)
            assert struct.unpack_from("=I", frame)[0] == tx_id
            assert frame[8] == 0x20 | (seq & 15)
            payload += frame[9 : 8 + frame[4]]
            seq += 1
        assert payload[:length] == _payload(0x540 if fd else 0x520, length)
        if wait:
            assert struct.unpack_from("=I", peer.recv(72))[0] == marker
        _verdict("async")


@pytest.mark.parametrize(
    "phase", [1, 2, 3, 4], ids=["empty", "partial", "sequence", "no-fc"]
)
def test_isotp_error_recovery(phase: int) -> None:
    """Report transport failures and reuse the same socket successfully.

    :param phase: target error scenario
    """
    # The node waits several seconds for TX flow control (N_Bs).
    with host_can_socket(can_id=0x681, timeout=20) as peer:
        _start("fault", False, loops=phase)
        if phase in {2, 3}:
            peer.send(_raw_frame(0x680, b"\x10\x10" + bytes(6)))
            assert peer.recv(16)[8] == 0x30
            if phase == 3:
                peer.send(_raw_frame(0x680, b"\x22" + bytes(7)))
        if phase == 4:
            assert peer.recv(16)[8] == 0x10
            assert peer.recv(16)[8:16] == b"\x07" + b"\x5a" * 7
        else:
            assert peer.recv(16)[8:10] == b"\x01\xa5"
            peer.send(_raw_frame(0x680, b"\x01\x5a"))
        _verdict("fault")


@pytest.mark.parametrize("fd", [False, True], ids=["classic", "fd"])
def test_isotp_large_pdu(fd: bool) -> None:
    """Transfer 4095 bytes, or an 8192-byte escaped FF with long PDUs enabled.

    :param fd: use CAN FD
    """
    conf = pytest.products[0].core(0).conf
    length = 8192 if conf.kv_check("CONFIG_LIBC_CAN_ISOTP_LONG_PDU") else 4095
    with _peer(fd, 0x457 if fd else 0x456, 0x655 if fd else 0x654) as peer:
        peer.settimeout(60)
        _start("large", fd, True, loops=1)
        assert peer.send(_payload(0x120 if fd else 0x100, length)) == length
        assert peer.recv(16384) == _payload(0x220 if fd else 0x200, length)
        _verdict("large")


def test_isotp_socket_api() -> None:
    """Reject invalid options and binds, and round-trip the bound address."""
    assert (
        pytest.products[0]
        .core(0)
        .sendCommand(
            "isotptl api can0 1 0 0", "isotptl: PASS mode=api", timeout=10
        )
        == 0
    )


def test_isotp_codec_bounds() -> None:
    """Reject truncated frames and lengths that exceed output capacity."""
    assert (
        pytest.products[0]
        .core(0)
        .sendCommand(
            "isotptl codec can0 1 0 0", "isotptl: PASS mode=codec", timeout=10
        )
        == 0
    )
