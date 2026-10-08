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

"""Regression tests reproducing real NuttX net stack bug classes.

Each test below exists because a real NuttX bug or revert triggered it;
see each test's docstring for the commit it guards.
"""

import re
import socket
import struct
from typing import Any

import pytest
from _net_common import (
    HOST_IP,
    NODE_IPS,
    host_tcp_echo_check,
    host_udp_echo_check,
    nettl_server,
    pattern,
)

pytestmark = [pytest.mark.dep_config("CONFIG_NET_TCP", "CONFIG_NET_UDP")]

#: Broadcast address of the ip-pair bridge (tl-br0, 10.42.0.0/24).
BROADCAST_IP = "10.42.0.255"

#: Matches a nettl server verdict line (``rx=<n> err=<n>``).
SERVER_VERDICT_RE = r"nettl: (PASS|FAIL) rx=[^\r\n]*[\r\n]"

#: Matches nsh's "<cmd> [<pid>:<priority>]" line for a backgrounded task.
PID_RE = r"nettl \[(\d+):\d+\]"

#: Matches the nettl client's connect() failure line.
CONNECT_FAILED_RE = r"nettl: connect failed (\d+)"

#: Fallback used when CONFIG_NET_TCP_PREALLOC_CONNS cannot be read.
DEFAULT_TCP_PREALLOC_CONNS = 16

#: Confirmed reproducing on sim; not observed on qemu-armv8a, rv-virt
#: or qemu-intel64 with the same CONFIG_NET_TCP_PREALLOC_CONNS.
SIM_TCP_LEAK_BUG = (
    "kill -9 of a task blocked in accept() leaks its TCP connection on "
    "sim (nuttx e26d467f, no upstream fix yet)"
)


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def _send_udp_burst(addr: str, port: int, count: int, length: int) -> None:
    """Send a burst of nettl UDP datagrams from the host.

    :param addr: destination address (unicast or broadcast)
    :param port: destination port
    :param count: number of datagrams to send
    :param length: payload length per datagram
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for seq in range(count):
            dgram = struct.pack(">I", seq) + pattern(seq * length, length)
            sock.sendto(dgram, (addr, port))


@pytest.fixture
def sim_tcp_leak_xfail(request: pytest.FixtureRequest) -> None:
    """Expect the accept() kill -9 TCP connection leak to occur on sim.

    :param request: pytest request of the test using this fixture
    """
    if _core(0).conf.kv_check("CONFIG_ARCH_SIM"):
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=SIM_TCP_LEAK_BUG)
        )


@pytest.mark.cmd_check("nettl_main")
def test_tcp_rst_after_handshake() -> None:
    """A peer RST right after the handshake must not hang the server.

    NuttX aeaa13227e2: a connection reset by the peer right after the
    handshake was accepted as connected, and a subsequent blocking
    send() hung forever. ``-D`` delays accept() so the host's RST (a
    connect + ``SO_LINGER(1, 0)`` + close()) lands before accept()
    runs; ``-w`` makes the server send (not echo), so it is the one
    that observes the dead connection. PASS: a verdict line (FAIL is
    expected, since send() fails) appears within 15 s, i.e. no hang.
    """
    port = 5400
    core = _core(0)
    ret = core.sendCommand(
        f"nettl -s -w -D 2 -p {port} &", "listening", timeout=10
    )
    assert ret == 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((NODE_IPS[0], port))
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
    finally:
        sock.close()  # SO_LINGER(1, 0): close() sends RST, not FIN

    result = core.readUntilPattern(SERVER_VERDICT_RE, timeout=15)
    found = re.search(SERVER_VERDICT_RE, result.output)
    assert found, result.output


@pytest.mark.cmd_check("nettl_main")
def test_udp_reuseaddr_broadcast() -> None:
    """A 2nd+ SO_REUSEADDR UDP listener must get a correct d_len.

    NuttX 8422531f931a: with 2+ SO_REUSEADDR UDP listeners on the same
    port, broadcast fan-out gave the 2nd+ socket a ``d_len`` too large
    by the IP+UDP header size, corrupting its read-ahead. PASS: both
    listeners receive all 20 datagrams, each exactly ``4 + 64`` bytes
    and pattern-intact: ``nettl: PASS rx=40 err=0``.
    """
    port = 5401
    count = 20
    length = 64
    core = _core(0)
    ret = core.sendCommand(
        f"nettl -s -u -L 2 -n {count} -l {length} -p {port} -t 15 &",
        "listening",
        timeout=10,
    )
    assert ret == 0

    _send_udp_burst(BROADCAST_IP, port, count, length)

    result = core.readUntilPattern(SERVER_VERDICT_RE, timeout=20)
    found = re.search(SERVER_VERDICT_RE, result.output)
    assert found, result.output
    assert found.group(0).rstrip("\r\n") == (
        f"nettl: PASS rx={2 * count} err=0"
    ), result.output


@pytest.mark.cmd_check("nettl_main")
def test_arp_expiry_traffic() -> None:
    """Paced UDP echo traffic must survive an ARP entry's expiry.

    Guards ARP flow regressions (reverts of 6b30226c0e2 and
    d6bd89ff144a in 2025). 300 datagrams at a 0.1 s interval take
    around 30 s, more than twice the default
    ``CONFIG_NET_ARP_MAXAGE`` (120 deciseconds), so the node's ARP
    entry for the host expires and must be re-resolved mid-test. Up
    to 3 retries per datagram tolerate the one legitimate drop while
    an ARP request is outstanding. PASS: zero datagrams lost after
    retries, all intact.
    """
    port = 5402
    nettl_server(0, True, port, NODE_IPS[0])
    assert host_udp_echo_check(NODE_IPS[0], port, 300, interval=0.1, retries=3)


@pytest.mark.cmd_check("nettl_main")
def test_tcp_long_transfer() -> None:
    """A sustained TCP transfer must not stall (revert of d5d6b652134).

    4 MiB host-to-node TCP echo, with a 10 s per-operation timeout
    acting as a stall watchdog (the socket timeout set by
    ``host_tcp_echo_check`` applies to every subsequent recv() on the
    connection). PASS: all bytes echoed intact within that watchdog.
    """
    port = 5403
    nbytes = 4 * 1024 * 1024
    nettl_server(0, False, port, NODE_IPS[0])
    assert host_tcp_echo_check(NODE_IPS[0], port, nbytes, timeout=10.0)


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.run(order=-2)
@pytest.mark.usefixtures("sim_tcp_leak_xfail")
def test_tcp_kill_listener_leak() -> None:
    """A kill -9 of a task blocked in accept() must not leak its conn.

    NuttX bug found by this project (nuttx e26d467f, no upstream fix
    yet): killing a task blocked in accept() on a listening TCP
    socket never frees its TCP connection. Repeats
    ``CONFIG_NET_TCP_PREALLOC_CONNS + 1`` times: start a listener,
    kill -9 it while it is still blocked in accept(). PASS: a final
    probe connect still allocates a socket and is refused by the host
    (``connect failed 111``); FAIL if the leak exhausted the
    preallocated pool (``socket failed 12`` / ``connect failed 12``).

    Runs second to last in the session (``order=-2``): it poisons the
    node.
    """
    core = _core(0)
    prealloc = core.conf.kv_check("CONFIG_NET_TCP_PREALLOC_CONNS")
    try:
        prealloc_n = int(prealloc)
    except (TypeError, ValueError):
        prealloc_n = 0
    if prealloc_n <= 0:
        prealloc_n = DEFAULT_TCP_PREALLOC_CONNS

    base_port = 5500
    for i in range(prealloc_n + 1):
        result = core.sendCommandReadUntilPattern(
            f"nettl -s -p {base_port + i} -t 30 &",
            pattern=PID_RE,
            timeout=10,
        )
        found = re.search(PID_RE, result.output)
        assert found, result.output
        ret = core.sendCommand(f"kill -9 {found.group(1)}", timeout=10)
        assert ret == 0

    probe = core.sendCommandReadUntilPattern(
        f"nettl -c {HOST_IP} -p 9 -n 1",
        pattern=CONNECT_FAILED_RE,
        timeout=15,
    )
    found = re.search(CONNECT_FAILED_RE, probe.output)
    assert found, probe.output
    assert found.group(1) == "111", probe.output
