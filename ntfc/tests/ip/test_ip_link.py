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

"""Link and interface changes on a running node.

All tests change node1; the ``restore_node1`` fixture brings its
interface, its bridge port and its configured address back afterwards.
"""

import json
import subprocess
from typing import Any, Iterator, List

import pytest
from _net_common import HOST_IP, NODE_IPS, host_tcp_echo_check, nettl_server

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_TCP"),
    pytest.mark.cmd_check("nettl_main"),
    pytest.mark.cmd_check("ping_main"),
]

BRIDGE = "tl-br0"
NODE = 1
NEW_IP = "10.42.0.21"

_DOWN_PORTS: List[str] = []

SIM_TAP_EIO_BUG = (
    "arch/sim/src/sim/posix/sim_tapdev.c: sim_tapdev_send() calls exit(1) "
    "when write() fails; a TAP whose host link is down fails every write "
    "with EIO, so the sim process exits instead of dropping the frame"
)


def _core() -> Any:
    """Return the NTFC core handler of node1.

    :return: ``ProductCore`` for product 1, core 0
    """
    return pytest.products[NODE].core(0)


def _run(*args: str) -> str:
    """Run a host command and return its output.

    :param args: command and arguments
    :return: standard output
    """
    return subprocess.run(
        args, capture_output=True, text=True, check=True
    ).stdout


def _host_ping(addr: str, count: int = 5) -> bool:
    """Ping ``addr`` from the host.

    :param addr: destination IPv4 address
    :param count: echo requests to send
    :return: True when at least one echo request was answered
    """
    ret = subprocess.run(
        ["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", "1", addr],
        capture_output=True,
        check=False,
    )
    return ret.returncode == 0


def _node_ping(addr: str) -> bool:
    """Ping ``addr`` from node1, the first echo only resolves ARP.

    :param addr: destination IPv4 address
    :return: True when 3 of 3 echo requests were answered
    """
    core = _core()
    core.sendCommand(f"ping -c 1 {addr}", "packet loss", timeout=15)
    return (
        core.sendCommand(f"ping -c 3 {addr}", " 0% packet loss", timeout=30)
        == 0
    )


def _tcp_echo(addr: str, port: int) -> bool:
    """Run a node1 TCP echo server and check it from the host.

    :param addr: node1 address to connect to
    :param port: server port
    :return: True when 16 KiB were echoed intact
    """
    nettl_server(NODE, False, port, addr)
    return host_tcp_echo_check(addr, port, 16384)


def _bridge_port(addr: str) -> str:
    """Return the host bridge port behind a node address.

    :param addr: node IPv4 address
    :return: bridge port interface name
    """
    assert _host_ping(addr), f"{addr} unreachable"
    neigh = json.loads(_run("ip", "-j", "neigh", "show", addr, "dev", BRIDGE))
    mac = neigh[0]["lladdr"]
    for fdb in json.loads(_run("bridge", "-j", "fdb", "show", "br", BRIDGE)):
        if fdb.get("mac") == mac and fdb.get("ifname") != BRIDGE:
            return fdb["ifname"]
    raise AssertionError(f"no bridge port for {mac}")


@pytest.fixture(autouse=True)
def restore_node1() -> Iterator[None]:
    """Bring node1's link and static address back after the test."""
    yield
    while _DOWN_PORTS:
        # The port is gone if a sim node exited with its TAP.
        subprocess.run(
            ["ip", "link", "set", _DOWN_PORTS.pop(), "up"], check=False
        )
    _core().sendCommand("ifup eth0", timeout=10)
    _core().sendCommand(
        f"ifconfig eth0 {NODE_IPS[NODE]} dr {HOST_IP} netmask 255.255.255.0",
        timeout=10,
    )
    assert _node_ping(HOST_IP)


def test_link_ifdown_ifup() -> None:
    """node1 is unreachable while down and fully usable after ``ifup``."""
    addr = NODE_IPS[NODE]
    assert _host_ping(addr)
    assert _core().sendCommand("ifdown eth0", "...OK", timeout=10) == 0
    assert not _host_ping(addr, count=3)
    assert _core().sendCommand("ifup eth0", "...OK", timeout=10) == 0
    assert _node_ping(HOST_IP)
    assert _host_ping(addr)
    assert _tcp_echo(addr, 5500)


def test_link_host_port_flap() -> None:
    """node1 works again after its host bridge port went down and up.

    Skipped on sim: the node process exits, and NTFC reports a dead
    device as a failure even for an expected failure.
    """
    if _core().conf.kv_check("CONFIG_ARCH_SIM"):
        pytest.skip(SIM_TAP_EIO_BUG)
    addr = NODE_IPS[NODE]
    port = _bridge_port(addr)
    _run("ip", "link", "set", port, "down")
    _DOWN_PORTS.append(port)
    assert not _host_ping(addr, count=3)
    _run("ip", "link", "set", _DOWN_PORTS.pop(), "up")
    assert _node_ping(HOST_IP)
    assert _host_ping(addr)
    assert _tcp_echo(addr, 5501)


def test_link_readdress() -> None:
    """node1 answers only on its new address after ``ifconfig``."""
    ret = _core().sendCommand(
        f"ifconfig eth0 {NEW_IP} dr {HOST_IP} netmask 255.255.255.0",
        timeout=10,
    )
    assert ret == 0
    assert _node_ping(HOST_IP)
    assert _host_ping(NEW_IP)
    _run("ip", "neigh", "flush", "dev", BRIDGE, "to", NODE_IPS[NODE])
    assert not _host_ping(NODE_IPS[NODE], count=3)
    assert _tcp_echo(NEW_IP, 5502)
