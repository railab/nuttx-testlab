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

"""CAN character driver: frame types, ioctls, FIFO behavior (``canopt``).

Runs on nodes that reach the bus through ``/dev/can0`` (see
``_can_common.can_endpoint()``); skipped on SocketCAN nodes.
"""

import threading
from typing import Iterator

import pytest
from _can_common import CAN_CHARDEV
from _canopt_common import (  # noqa: F401 - canopt_env is a fixture
    CAN_RTR_FLAG,
    CLASSIC_FRAMES,
    FD_FRAMES,
    MARKER_ID,
    Frame,
    canopt_env,
    canopt_run,
    canopt_start,
    canopt_verdict,
    chardev_node,
    host_expect,
    host_send,
    host_socket,
    interleaved_frames,
    kv_value,
    pack,
    unpack,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_CAN"),
    pytest.mark.cmd_check("canopt_main"),
    pytest.mark.usefixtures("canopt_env"),
]

RTR_ID = 0x3A0


@pytest.fixture(autouse=True)
def chardev_only() -> None:
    """Skip unless node 0 uses the CAN character driver."""
    if not chardev_node(0):
        pytest.skip("node 0 reaches the bus over SocketCAN")


def test_can_char_rx_frame_types() -> None:
    """``read()`` returns both tables intact.

    ID, EFF, RTR, DLC (CAN FD DLC 9..15 = 12..64 bytes), EDL/BRS/ESI and
    payload of every message match what the host sent.
    """
    canopt_start(0, f"crx {CAN_CHARDEV}")
    host_send(interleaved_frames() + [Frame(MARKER_ID, 1)])
    ret = canopt_verdict(0)
    assert ret.startswith("canopt: PASS crx"), ret


def test_can_char_tx_frame_types() -> None:
    """``write()`` of both tables reaches the host intact."""
    sock = host_socket()
    try:
        ret = canopt_run(0, f"ctx {CAN_CHARDEV}")
        errors = host_expect(sock, CLASSIC_FRAMES + FD_FRAMES)
    finally:
        sock.close()

    assert ret.startswith("canopt: PASS ctx"), ret
    assert errors == []


def test_can_char_ioctl() -> None:
    """Upper-half ioctls round trip; lower-half ones work or say ENOTTY.

    CANIOC_GET/SET_MSGALIGN, FIONWRITE, CANIOC_GET_BITTIMING and
    CANIOC_ADD/DEL_STDFILTER.
    """
    ret = canopt_run(0, f"cioctl {CAN_CHARDEV}")
    assert ret.startswith("canopt: PASS cioctl"), ret


def test_can_char_msgalign() -> None:
    """CANIOC_SET_MSGALIGN controls how ``read()`` packs messages.

    With 6 messages queued: alignment 1 returns 3 packed messages into a
    3-message buffer, 0 returns exactly one, 16 pads each to 16 bytes.
    """
    canopt_start(0, f"calign {CAN_CHARDEV}")
    host_send([Frame(0x5C0 + i, 8) for i in range(6)])
    ret = canopt_verdict(0)
    assert ret.startswith("canopt: PASS calign"), ret


def test_can_char_nonblock_poll() -> None:
    """O_NONBLOCK read/write and poll() on the character driver.

    EAGAIN on an empty FIFO, poll() idle timeout and POLLOUT, a
    nonblocking write that reaches the host, a POLLIN wake-up for a host
    frame, then EAGAIN again.
    """
    sock = host_socket()
    try:
        canopt_start(0, f"cnonblock {CAN_CHARDEV}")
        errors = host_expect(sock, [Frame(0x5B0, 8)])
    finally:
        sock.close()

    host_send([Frame(0x5B1, 8)])
    ret = canopt_verdict(0)
    assert errors == []
    assert ret == "canopt: PASS cnonblock", ret


@pytest.mark.dep_config("CONFIG_CAN_ERRORS")
def test_can_char_rx_overflow() -> None:
    """RX FIFO overflow is reported as one CAN_ERROR5_RXOVERFLOW message.

    The host sends ``CONFIG_CAN_RXFIFOSIZE + 16`` frames while nobody
    reads; the reader then gets the error message and the
    ``CONFIG_CAN_RXFIFOSIZE - 1`` frames the FIFO holds.
    """
    fifo = int(kv_value(0, "CONFIG_CAN_RXFIFOSIZE"))
    canopt_start(0, f"coverflow {CAN_CHARDEV}")
    host_send([Frame(0x5D0, 8)] * (fifo + 16), gap_s=0.001)
    ret = canopt_verdict(0)
    assert ret.startswith("canopt: PASS coverflow"), ret


@pytest.mark.xfail(
    strict=True,
    reason="FIONREAD stores a uint8_t into the caller's int",
)
def test_can_char_fionread() -> None:
    """FIONREAD reports the number of queued messages as an int."""
    count = 5
    canopt_start(0, f"cfionread {CAN_CHARDEV} -n {count}")
    host_send([Frame(0x5E0, 8)] * count)
    ret = canopt_verdict(0)
    assert ret.startswith("canopt: PASS cfionread"), ret


@pytest.mark.xfail(
    strict=True,
    reason="CANIOC_IFLUSH empties the FIFO but leaves rx_sem posted",
)
def test_can_char_iflush() -> None:
    """CANIOC_IFLUSH drops queued messages; the next read says EAGAIN."""
    canopt_start(0, f"ciflush {CAN_CHARDEV}")
    host_send([Frame(0x5F0, 8)] * 3)
    ret = canopt_verdict(0)
    assert ret == "canopt: PASS ciflush", ret


def _rtr_responder(stop: threading.Event) -> None:
    """Answer one remote request for ``RTR_ID`` with an 8-byte frame.

    :param stop: set to end the responder early
    """
    sock = host_socket(timeout=0.2)
    try:
        while not stop.is_set():
            try:
                can_id, _length, _flags, _data = unpack(sock.recv(72))
            except OSError:
                continue
            if can_id == CAN_RTR_FLAG | RTR_ID:
                sock.send(pack(Frame(RTR_ID, 8)))
                return
    finally:
        sock.close()


@pytest.fixture
def rtr_responder() -> Iterator[None]:
    """Run ``_rtr_responder`` in the background for one test.

    :return: fixture generator
    """
    stop = threading.Event()
    thread = threading.Thread(target=_rtr_responder, args=(stop,))
    thread.start()
    yield
    stop.set()
    thread.join()


@pytest.mark.xfail(
    strict=True,
    reason="sim CAN lower half returns ENOTSUP for remote requests",
)
@pytest.mark.usefixtures("rtr_responder")
def test_can_char_rtr_request() -> None:
    """CANIOC_RTR sends a remote request and returns the host's reply."""
    ret = canopt_run(0, f"crtr {CAN_CHARDEV}")
    assert ret == "canopt: PASS crtr", ret
