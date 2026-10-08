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

"""DHCP: NuttX dhcpc against host and NuttX servers on the ip-pair bridge.

node1 gets a leased address in each test; the ``static_node1`` fixture
puts its configured address back afterwards.
"""

from typing import Any, Iterator

import pytest
from _host_services import DhcpServer
from _net_common import HOST_IP, NODE_IPS

pytestmark = [
    pytest.mark.dep_config("CONFIG_NETUTILS_DHCPC", "CONFIG_NET_UDP"),
    pytest.mark.cmd_check("renew_main"),
]

CLIENT = 1
HOST_LEASE = "10.42.0.150"
NUTTX_LEASE = "10.42.0.100"  # CONFIG_NETUTILS_DHCPD_STARTIP


def _core(node: int) -> Any:
    """Return the NTFC core handler of a node.

    :param node: product index
    :return: ``ProductCore`` for the product's core 0
    """
    return pytest.products[node].core(0)


def _has_addr(node: int, addr: str) -> bool:
    """Check the IPv4 address of a node's eth0.

    :param node: product index
    :param addr: expected IPv4 address
    :return: True when ``ifconfig`` shows ``addr``
    """
    ret = _core(node).sendCommand(
        "ifconfig eth0", f"inet addr:{addr} ", timeout=10
    )
    return ret == 0


def _ping(node: int, addr: str) -> bool:
    """Ping ``addr`` from a node, the first echo only resolves ARP.

    :param node: product index
    :param addr: destination IPv4 address
    :return: True when 3 of 3 echo requests were answered
    """
    core = _core(node)
    core.sendCommand(f"ping -c 1 {addr}", "packet loss", timeout=15)
    return (
        core.sendCommand(f"ping -c 3 {addr}", " 0% packet loss", timeout=30)
        == 0
    )


@pytest.fixture(autouse=True)
def static_node1() -> Iterator[None]:
    """Restore node1's static address and router after the test."""
    yield
    _core(CLIENT).sendCommand(
        f"ifconfig eth0 {NODE_IPS[CLIENT]} dr {HOST_IP} "
        "netmask 255.255.255.0",
        timeout=10,
    )
    assert _has_addr(CLIENT, NODE_IPS[CLIENT])


def test_dhcpc_from_host() -> None:
    """node1 leases an address from a host DHCP server and uses it."""
    with DhcpServer("tl-br0", HOST_IP, HOST_LEASE, HOST_IP, HOST_IP) as srv:
        assert _core(CLIENT).sendCommand("renew eth0", timeout=30) == 0
        assert srv.acked, "no DHCPREQUEST acknowledged"
    assert _has_addr(CLIENT, HOST_LEASE)
    assert _ping(CLIENT, HOST_IP)


@pytest.mark.cmd_check("dhcpd_start_main")
@pytest.mark.cmd_check("dhcpd_stop_main")
def test_dhcpd_node_to_node() -> None:
    """node1 leases an address from the NuttX DHCP server on node0."""
    assert _core(0).sendCommand("dhcpd_start eth0", timeout=10) == 0
    try:
        assert _core(CLIENT).sendCommand("renew eth0", timeout=30) == 0
        assert _has_addr(CLIENT, NUTTX_LEASE)
        assert _ping(CLIENT, NODE_IPS[0])
    finally:
        _core(0).sendCommand("dhcpd_stop", timeout=10)
