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

"""Two NuttX nodes and the host on one Ethernet segment."""

import pytest
from _net_common import (
    HOST_IP,
    NODE_IPS,
    host_tcp_echo_check,
    host_udp_echo_check,
    nettl_client,
    nettl_server,
)

pytestmark = [pytest.mark.dep_config("CONFIG_NET_TCP", "CONFIG_NET_UDP")]

# NuttX e1000 never sets RCTL.BAM, so broadcast frames (ARP requests) are
# dropped and two e1000 nodes cannot resolve each other. Host<->node works
# because Linux learns the node MAC from the node's own ARP request.

E1000_BAM_BUG = (
    "drivers/net/e1000.c does not set E1000_RCTL_BAM: broadcast ARP "
    "requests are dropped (fix: apache/nuttx#20470)"
)


@pytest.fixture
def e1000_broadcast_xfail(request: pytest.FixtureRequest) -> None:
    """Expect node-to-node failure on targets using the e1000 driver.

    :param request: pytest request of the test using this fixture
    """
    if pytest.products[1].core(0).conf.kv_check("CONFIG_NET_E1000"):
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=E1000_BAM_BUG)
        )


@pytest.mark.cmd_check("ping_main")
@pytest.mark.parametrize("node", [0, 1])
def test_ping_host(node: int) -> None:
    """Each node reaches the host bridge address."""
    ret = (
        pytest.products[node]
        .core(0)
        .sendCommand(f"ping -c 3 {HOST_IP}", " 0% packet loss", timeout=30)
    )
    assert ret == 0


@pytest.mark.cmd_check("ping_main")
@pytest.mark.usefixtures("e1000_broadcast_xfail")
def test_ping_node_to_node() -> None:
    """Node 0 reaches node 1 directly over the bridge."""
    ret = (
        pytest.products[0]
        .core(0)
        .sendCommand(f"ping -c 3 {NODE_IPS[1]}", " 0% packet loss", timeout=30)
    )
    assert ret == 0


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("udp", [False, True], ids=["tcp", "udp"])
@pytest.mark.usefixtures("e1000_broadcast_xfail")
def test_nettl_node_to_node(udp: bool) -> None:
    """Node 0 client exchanges verified data with node 1 server."""
    port = 5201 if udp else 5200
    count = 200 if udp else 262144
    nettl_server(1, udp, port, NODE_IPS[1])
    verdict = nettl_client(0, NODE_IPS[1], udp, port, count)
    assert verdict.startswith("nettl: PASS"), verdict


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("node", [0, 1])
def test_host_tcp_to_node(node: int) -> None:
    """Linux host client exchanges verified TCP data with a node."""
    nettl_server(node, False, 5300, NODE_IPS[node])
    assert host_tcp_echo_check(NODE_IPS[node], 5300, 131072)


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("node", [0, 1])
def test_host_udp_to_node(node: int) -> None:
    """Linux host client exchanges verified UDP data with a node."""
    nettl_server(node, True, 5301, NODE_IPS[node])
    assert host_udp_echo_check(NODE_IPS[node], 5301, 100)
