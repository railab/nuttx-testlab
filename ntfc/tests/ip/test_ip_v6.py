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

"""IPv6 between two NuttX nodes and the host on the ip-pair bridge."""

import pytest
from _net_common import (
    HOST_IP6,
    NODE_IP6S,
    host_tcp_echo_check,
    host_udp_echo_check,
    nettl_client,
    nettl_server,
)

pytestmark = [
    pytest.mark.dep_config(
        "CONFIG_NET_IPv6", "CONFIG_NET_TCP", "CONFIG_NET_UDP"
    ),
]


def _ping6(node: int, addr: str) -> int:
    """Ping6 ``addr`` from a node and require 0% loss.

    The first ping only resolves the neighbor: the packet that triggers
    neighbor solicitation may get no reply.

    :param node: product index
    :param addr: destination IPv6 address
    :return: ``0`` if 3 of 3 echo requests were answered
    """
    core = pytest.products[node].core(0)
    core.sendCommand(f"ping6 -c 1 {addr}", "packet loss", timeout=15)
    return core.sendCommand(
        f"ping6 -c 3 {addr}", " 0% packet loss", timeout=30
    )


@pytest.mark.cmd_check("ping6_main")
@pytest.mark.parametrize("node", [0, 1])
def test_ping6_host(node: int) -> None:
    """Each node reaches the host bridge address over IPv6."""
    assert _ping6(node, HOST_IP6) == 0


@pytest.mark.cmd_check("ping6_main")
def test_ping6_node_to_node() -> None:
    """Node 0 reaches node 1 over IPv6."""
    assert _ping6(0, NODE_IP6S[1]) == 0


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("udp", [False, True], ids=["tcp", "udp"])
def test_nettl6_node_to_node(udp: bool) -> None:
    """Node 0 client exchanges verified data with node 1 over IPv6."""
    port = 5211 if udp else 5210
    nettl_server(1, udp, port, NODE_IP6S[1], ipv6=True)
    verdict = nettl_client(0, NODE_IP6S[1], udp, port, 200 if udp else 262144)
    assert verdict.startswith("nettl: PASS"), verdict


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("node", [0, 1])
def test_host_tcp6_to_node(node: int) -> None:
    """Host TCP client to nettl on each node over IPv6, data intact."""
    nettl_server(node, False, 5212, NODE_IP6S[node], ipv6=True)
    assert host_tcp_echo_check(NODE_IP6S[node], 5212, 131072)


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("node", [0, 1])
def test_host_udp6_to_node(node: int) -> None:
    """Host UDP client to nettl on each node over IPv6, data intact."""
    nettl_server(node, True, 5213, NODE_IP6S[node], ipv6=True)
    assert host_udp_echo_check(NODE_IP6S[node], 5213, 100, retries=2)
