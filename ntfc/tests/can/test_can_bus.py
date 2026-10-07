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

"""Two nodes and the host on one SocketCAN bus (vcan ``can0``)."""

import pytest
from _can_common import (
    CAN_ID_ALT,
    CAN_ID_DEFAULT,
    CAN_IFNAME,
    canfd_supported,
    cantl_receiver_start,
    cantl_receiver_verdict,
    cantl_sender,
    host_can_send,
    host_can_socket,
    host_can_verify,
)

pytestmark = [pytest.mark.dep_config("CONFIG_NET_CAN")]

# Gap between host-sent frames. 1 ms is ~8x a classic 8-byte frame at
# 1 Mbit/s, enough for a node to drain its RX FIFO between frames.

HOST_GAP_S = 0.001


# arch/sim/src/sim/sim_cansock.c:sim_can_work() reads one frame from the
# host socket per run and requeues itself after USEC2TICK(1000), one 10 ms
# sim tick. Frames from an earlier burst are still queued when this test's
# receiver starts, so it consumes stale frames and misses the new ones.

SIM_CAN_RX_BUG = (
    "sim_cansock.c drains one host frame per 10 ms tick: stale frames "
    "from earlier bursts reach later sockets (nuttx 20f3b659372c)"
)


@pytest.fixture
def sim_can_rx_xfail(request: pytest.FixtureRequest) -> None:
    """Expect stale-frame delivery on the sim SocketCAN driver.

    :param request: pytest request of the test using this fixture
    """
    if pytest.products[1].core(0).conf.kv_check("CONFIG_ARCH_SIM"):
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=SIM_CAN_RX_BUG)
        )


@pytest.fixture(scope="module", autouse=True)
def can_bus_up() -> None:
    """Bring up ``can0`` on every node once per test module."""
    for node in range(len(pytest.products)):
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
    tx = cantl_sender(0, count, fd=fd)
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
        tx = cantl_sender(0, count)
        assert tx.startswith("cantl: PASS"), tx
        rx, lost, err = host_can_verify(sock, count)
    finally:
        sock.close()

    assert (rx, lost, err) == (count, 0, 0)


@pytest.mark.cmd_check("cantl_main")
@pytest.mark.usefixtures("sim_can_rx_xfail")
def test_can_filter() -> None:
    """A receiver's exact-ID filter rejects frames on another CAN ID.

    Node 0 and the host both send: node 0 sends the frames the receiver
    expects (``CAN_ID_DEFAULT``), the host interleaves unrelated noise
    frames on ``CAN_ID_ALT`` before, during and after. The receiver's
    ``CAN_RAW_FILTER`` must pass only the matching ID.
    """
    count = 20
    cantl_receiver_start(1, count, can_id=CAN_ID_DEFAULT)

    host_can_send(5, can_id=CAN_ID_ALT)
    tx = cantl_sender(0, count, can_id=CAN_ID_DEFAULT)
    host_can_send(5, can_id=CAN_ID_ALT)

    assert tx.startswith("cantl: PASS"), tx
    rx = cantl_receiver_verdict(1)
    assert rx == f"cantl: PASS rx={count} lost=0 err=0", rx
