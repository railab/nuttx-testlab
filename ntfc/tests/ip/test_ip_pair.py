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


def _ping(node: int, addr: str) -> int:
    """Ping ``addr`` from a node and require 0% loss.

    The first ping only resolves ARP: NuttX drops the packet that
    triggers the ARP request, so it may get no reply.

    :param node: product index
    :param addr: destination IPv4 address
    :return: ``0`` if 3 of 3 echo requests were answered
    """
    core = pytest.products[node].core(0)
    core.sendCommand(f"ping -c 1 {addr}", "packet loss", timeout=15)
    return core.sendCommand(f"ping -c 3 {addr}", " 0% packet loss", timeout=30)


@pytest.mark.cmd_check("ping_main")
@pytest.mark.parametrize("node", [0, 1])
def test_ping_host(node: int) -> None:
    """Each node reaches the host bridge address."""
    assert _ping(node, HOST_IP) == 0


@pytest.mark.cmd_check("ping_main")
def test_ping_node_to_node() -> None:
    """Node 0 reaches node 1 directly over the bridge."""
    assert _ping(0, NODE_IPS[1]) == 0


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("udp", [False, True], ids=["tcp", "udp"])
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
