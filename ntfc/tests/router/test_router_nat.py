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

"""NAT44 MASQUERADE on the node's B interface.

While the rule is set host B has no route back to subnet A, so only
translated traffic is answered.
"""

import threading
from typing import Iterator, List

import pytest
from _host_services import TcpEchoServer, UdpEchoServer
from _net_common import host_tcp_echo_check, host_udp_echo_check
from _router_common import HOSTS, NODES, masquerade, netns, ping_ok

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_NAT44"),
    pytest.mark.usefixtures("router_ifs"),
]

#: Concurrent connections in the tracking test.
STREAMS = 4


@pytest.fixture(autouse=True)
def nat(router_ifs: List[str]) -> Iterator[None]:
    """MASQUERADE on the B interface for each test.

    :param router_ifs: node interface name per side
    """
    with masquerade(router_ifs[1]):
        yield


def test_nat_ping() -> None:
    """Host A pings host B through the NAT."""
    assert ping_ok(0, HOSTS[1])


def test_nat_tcp() -> None:
    """Host B sees the connection from the node's B address."""
    port = 5740
    with netns(1):
        srv = TcpEchoServer(HOSTS[1], port)
    with srv, netns(0):
        assert host_tcp_echo_check(HOSTS[1], port, 64 * 1024)
    assert [addr for addr, _ in srv.peers] == [NODES[1]]


def test_nat_udp() -> None:
    """Host B sees the datagrams from the node's B address."""
    port = 5741
    with netns(1):
        srv = UdpEchoServer(HOSTS[1], port)
    with srv, netns(0):
        assert host_udp_echo_check(
            HOSTS[1], port, 50, length=512, timeout=1, retries=3
        )
    assert [addr for addr, _ in srv.peers] == [NODES[1]]


def test_nat_tracking() -> None:
    """Concurrent connections get their own mapping and their own data."""
    port = 5742
    results: List[bool] = []

    def client() -> None:
        """Echo one stream from host A."""
        with netns(0):
            results.append(host_tcp_echo_check(HOSTS[1], port, 64 * 1024))

    with netns(1):
        srv = TcpEchoServer(HOSTS[1], port)
    with srv:
        threads = [threading.Thread(target=client) for _ in range(STREAMS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
    assert results == [True] * STREAMS
    assert {addr for addr, _ in srv.peers} == {NODES[1]}
    assert len({port for _, port in srv.peers}) == STREAMS
