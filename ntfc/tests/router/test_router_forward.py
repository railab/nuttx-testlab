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

"""IPv4 and IPv6 forwarding between host A and host B through the node."""

import subprocess
import time
from typing import Iterator, List

import pytest
from _host_services import TcpEchoServer, UdpEchoServer
from _net_common import host_tcp_echo_check, host_udp_echo_check
from _router_common import (
    HOSTS,
    HOSTS6,
    NODES,
    NODES6,
    TAPS,
    configure,
    host_ping,
    iperf_client,
    iperf_mbps,
    iperf_server,
    iptables,
    netns,
    ping_ok,
    ports,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_IPFORWARD"),
    pytest.mark.usefixtures("router_ifs"),
]

#: Minimum forwarded TCP throughput in Mbit/s.
IPERF_FLOOR = 5
#: iperf run length in seconds.
IPERF_TIME = 10
#: Bytes echoed by the TCP integrity check.
TCP_BYTES = 256 * 1024
#: Datagrams (1024-byte payload) echoed by the UDP integrity check.
UDP_COUNT = 200

DIRECTIONS = [pytest.param(0, id="a_to_b"), pytest.param(1, id="b_to_a")]
FAMILIES = [pytest.param(False, id="ipv4"), pytest.param(True, id="ipv6")]


def _host(side: int, ipv6: bool) -> str:
    """Return a host address.

    :param side: 0 for host A, 1 for host B
    :param ipv6: IPv6 instead of IPv4
    :return: address of that host
    """
    return HOSTS6[side] if ipv6 else HOSTS[side]


def _tcp_echo(src: int, ipv6: bool, port: int) -> bool:
    """Echo TCP data from host ``src`` through the node to the other host.

    :param src: side of the client
    :param ipv6: use IPv6
    :param port: server port
    :return: True when every echoed byte matches
    """
    dst = _host(1 - src, ipv6)
    with netns(1 - src):
        srv = TcpEchoServer(dst, port)
    with srv, netns(src):
        return host_tcp_echo_check(dst, port, TCP_BYTES, timeout=10)


def _udp_echo(src: int, ipv6: bool, port: int) -> bool:
    """Echo UDP datagrams from host ``src`` through the node.

    :param src: side of the client
    :param ipv6: use IPv6
    :param port: server port
    :return: True when every datagram came back intact
    """
    dst = _host(1 - src, ipv6)
    with netns(1 - src):
        srv = UdpEchoServer(dst, port)
    with srv, netns(src):
        return host_udp_echo_check(
            dst, port, UDP_COUNT, length=1024, timeout=1, retries=3
        )


@pytest.fixture
def b_port_down() -> Iterator[List[str]]:
    """Bring the B side back after a test took it down.

    :return: list the test fills with the taken down host TAPs
    """
    down: List[str] = []
    yield down
    for tap in down:
        subprocess.run(["ip", "link", "set", tap, "up"], check=False)
    configure()


@pytest.mark.parametrize("side", [0, 1], ids=["a", "b"])
def test_router_port_up(side: int) -> None:
    """The NIC of a side is probed, has carrier and answers its host."""
    name, status = ports()[side]
    assert name, "NIC not found"
    assert status == "RUNNING"
    assert ping_ok(side, NODES[side])
    assert ping_ok(side, NODES6[side])


@pytest.mark.parametrize("ipv6", FAMILIES)
def test_router_ping(ipv6: bool) -> None:
    """Hosts A and B ping each other through the node."""
    assert ping_ok(0, _host(1, ipv6))
    assert ping_ok(1, _host(0, ipv6))


@pytest.mark.parametrize("ipv6", FAMILIES)
def test_router_ttl(ipv6: bool) -> None:
    """The node decrements TTL and reports TTL exceeded."""
    ping = host_ping(0, _host(1, ipv6))
    assert ping.returncode == 0, ping.stdout
    assert "ttl=63" in ping.stdout, ping.stdout
    ping = host_ping(0, _host(1, ipv6), count=1, ttl=1)
    node = NODES6[0] if ipv6 else NODES[0]
    assert f"From {node}" in ping.stdout, ping.stdout
    assert "exceeded" in ping.stdout, ping.stdout


@pytest.mark.dep_config("CONFIG_NET_IPFILTER")
def test_router_reject() -> None:
    """A FORWARD REJECT rule answers with ICMP destination unreachable."""
    rule = f"-A FORWARD -d {HOSTS[1]} -j REJECT"
    iptables(rule)
    try:
        ping = host_ping(0, HOSTS[1], count=1)
    finally:
        iptables(rule.replace("-A ", "-D "))
    assert f"From {NODES[0]}" in ping.stdout, ping.stdout
    assert "Unreachable" in ping.stdout, ping.stdout
    assert ping_ok(0, HOSTS[1])


@pytest.mark.parametrize("ipv6", FAMILIES)
@pytest.mark.parametrize("src", DIRECTIONS)
def test_router_tcp(src: int, ipv6: bool) -> None:
    """256 KiB echoed over TCP between the hosts arrive intact."""
    assert _tcp_echo(src, ipv6, 5700 + src)


@pytest.mark.parametrize("ipv6", FAMILIES)
@pytest.mark.parametrize("src", DIRECTIONS)
def test_router_udp(src: int, ipv6: bool) -> None:
    """200 UDP datagrams echoed between the hosts arrive intact."""
    assert _udp_echo(src, ipv6, 5710 + src)


@pytest.mark.parametrize("src", DIRECTIONS)
def test_router_iperf(src: int) -> None:
    """Forwarded TCP throughput reaches the floor."""
    port = 5720 + src
    with iperf_server(1 - src, port):
        time.sleep(0.5)
        client = iperf_client(src, HOSTS[1 - src], port, IPERF_TIME)
        mbps = iperf_mbps(client, IPERF_TIME + 30)
    print(f"router iperf side {src}: {mbps:.1f} Mbit/s")
    assert mbps >= IPERF_FLOOR


def test_router_port_flap(b_port_down: List[str]) -> None:
    """Forwarding recovers after the B side link drops under load."""
    port = 5730
    with iperf_server(1, port):
        time.sleep(0.5)
        client = iperf_client(0, HOSTS[1], port, 3 * IPERF_TIME)
        time.sleep(IPERF_TIME)
        subprocess.run(["ip", "link", "set", TAPS[1], "down"], check=True)
        b_port_down.append(TAPS[1])
        time.sleep(3)
        subprocess.run(["ip", "link", "set", TAPS[1], "up"], check=True)
        iperf_mbps(client, 3 * IPERF_TIME + 30)
    assert ping_ok(0, HOSTS[1])
    assert ping_ok(1, HOSTS[0])
    assert _tcp_echo(0, False, 5731)
