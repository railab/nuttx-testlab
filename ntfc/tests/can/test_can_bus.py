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

"""Two nodes and the host on one CAN bus (vcan ``can0``).

Each node reaches the bus over whichever backend its build has: SocketCAN
(``CONFIG_NET_CAN``, ifname ``can0``) or the CAN character driver
(``CONFIG_CAN``, ``/dev/can0``); see ``_can_common.can_endpoint()``. Both
backends are driven through the same ``cantl`` tool and share the host's
vcan ``can0``, so this module runs unmodified against either.
"""

import pytest
from _can_common import (
    CAN_CHARDEV,
    CAN_ID_ALT,
    CAN_ID_DEFAULT,
    CAN_IFNAME,
    CTUCANFD_TX_BUSY_BUG,
    KVASER_RX_BUG,
    can_endpoint,
    canfd_supported,
    cantl_receiver_start,
    cantl_receiver_verdict,
    cantl_sender,
    ctucanfd_node,
    host_can_send,
    host_can_socket,
    host_can_verify,
    kvaser_node,
)

# Gap between frames the host or a node sends. 1 ms is ~8x a classic
# 8-byte frame at 1 Mbit/s, enough for a node to drain its RX FIFO and
# TX buffers between frames. Back-to-back sending is covered by
# test_can_node_burst_to_host.

HOST_GAP_S = 0.001
NODE_GAP_MS = 1


@pytest.fixture(scope="module", autouse=True)
def can_bus_up() -> None:
    """Bring up ``can0`` on every node that uses SocketCAN.

    A node using the CAN character driver needs no interface to be
    brought up: ``/dev/can0`` is ready as soon as the board boots.
    """
    for node in range(len(pytest.products)):
        if can_endpoint(node) != CAN_IFNAME:
            continue

        ret = (
            pytest.products[node]
            .core(0)
            .sendCommand(f"ifup {CAN_IFNAME}", "OK", timeout=10)
        )
        assert ret == 0


@pytest.mark.cmd_check("cantl_main")
@pytest.mark.parametrize("fd", [False, True], ids=["classic", "fd"])
def test_can_node_to_node(fd: bool) -> None:
    """Node 0 sender, node 1 filtered receiver, over the shared CAN bus."""
    if fd and not (canfd_supported(0) and canfd_supported(1)):
        pytest.skip(
            "target has no CAN FD capable CAN driver on both nodes "
            "(e.g. CONFIG_CAN_KVASER is classic-only)"
        )

    count = 30
    cantl_receiver_start(1, count, fd=fd)
    tx = cantl_sender(0, count, fd=fd, gap_ms=NODE_GAP_MS)
    assert tx.startswith("cantl: PASS"), tx
    rx = cantl_receiver_verdict(1)
    assert rx == f"cantl: PASS rx={count} lost=0 err=0", rx


@pytest.mark.cmd_check("cantl_main")
def test_can_host_to_nodes() -> None:
    """Host sends; every node's receiver gets every frame intact.

    The host paces its frames like a real CAN bus would: a back-to-back
    burst from vcan overflows the small RX FIFO of emulated controllers
    (e.g. the SJA1000 on QEMU kvaser_pci holds 64 bytes, ~5 frames).
    """
    count = 20
    cantl_receiver_start(0, count)
    cantl_receiver_start(1, count)
    assert host_can_send(count, gap_s=HOST_GAP_S)
    for node in (0, 1):
        rx = cantl_receiver_verdict(node)
        assert rx == f"cantl: PASS rx={count} lost=0 err=0", rx


@pytest.mark.cmd_check("cantl_main")
def test_can_node_to_host() -> None:
    """Node 0 sends; the host verifies every frame intact.

    The host socket must be bound before the node starts sending: the
    vcan bus only delivers a frame to sockets that already exist when
    it is sent.
    """
    count = 20
    sock = host_can_socket(can_id=CAN_ID_DEFAULT, timeout=10.0)
    try:
        tx = cantl_sender(0, count, gap_ms=NODE_GAP_MS)
        assert tx.startswith("cantl: PASS"), tx
        rx, lost, err = host_can_verify(sock, count)
    finally:
        sock.close()

    assert (rx, lost, err) == (count, 0, 0)


@pytest.mark.cmd_check("cantl_main")
def test_can_filter(request: pytest.FixtureRequest) -> None:
    """A receiver's exact-ID filter rejects frames on another CAN ID.

    Node 0 and the host both send: node 0 sends the frames the receiver
    expects (``CAN_ID_DEFAULT``), the host interleaves unrelated noise
    frames on ``CAN_ID_ALT`` before, during and after. The receiver's
    exact-ID filter (hardware, or software when the driver has none)
    must pass only the matching ID.

    :param request: pytest request
    """
    if kvaser_node(1) and can_endpoint(1) == CAN_CHARDEV:
        request.applymarker(
            pytest.mark.xfail(strict=False, reason=KVASER_RX_BUG)
        )
    count = 20
    cantl_receiver_start(1, count, can_id=CAN_ID_DEFAULT)

    host_can_send(5, can_id=CAN_ID_ALT)
    tx = cantl_sender(0, count, can_id=CAN_ID_DEFAULT, gap_ms=NODE_GAP_MS)
    host_can_send(5, can_id=CAN_ID_ALT)

    assert tx.startswith("cantl: PASS"), tx
    rx = cantl_receiver_verdict(1)
    assert rx == f"cantl: PASS rx={count} lost=0 err=0", rx


@pytest.mark.cmd_check("cantl_main")
def test_can_node_burst_to_host(request: pytest.FixtureRequest) -> None:
    """Node 0 sends back-to-back; the host verifies every frame intact.

    :param request: pytest request
    """
    if ctucanfd_node(0):
        request.applymarker(
            pytest.mark.xfail(strict=False, reason=CTUCANFD_TX_BUSY_BUG)
        )

    count = 200
    sock = host_can_socket(can_id=CAN_ID_DEFAULT, timeout=10.0)
    try:
        tx = cantl_sender(0, count)
        assert tx.startswith("cantl: PASS"), tx
        rx, lost, err = host_can_verify(sock, count)
    finally:
        sock.close()

    assert (rx, lost, err) == (count, 0, 0)
