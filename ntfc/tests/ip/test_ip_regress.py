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
see each test's docstring for the behaviour it guards.
"""

import re
import socket
import struct
import time
from typing import Any

import pytest
from _net_common import (
    ACCEPT_ADDR_BUG,
    HOST_IP,
    NODE_IPS,
    host_tcp_echo_check,
    host_udp_echo_check,
    nettl_cleanup,
    nettl_server,
    pattern,
    track_server,
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

#: errno values of NuttX (include/errno.h).
ECONNABORTED = 103

ACCEPT_ABORTED_BUG = (
    "net/tcp/tcp_input.c:1141: tcp_input() deletes a connection that the "
    "peer reset from the backlog of its listener (tcp_backlogdelete()) "
    "and frees it, so accept() never fails with ECONNABORTED and keeps "
    "waiting (net/tcp/tcp_accept.c:278: psock_tcp_accept())"
)

#: Confirmed reproducing on every target with CONFIG_SIG_DEFAULT.
KILL_CALL_BUG = (
    "sched/signal/sig_default.c:268: nxsig_abnormal_termination() ends a "
    "task blocked in an OS call with _exit() from the signal handler, "
    "which runs no cleanup handler, so the rest of the call never runs: "
    "the file reference of the call (e.g. fs/socket/accept.c:232: "
    "accept4()) is never dropped, fdlist_free() (fs/inode/fs_files.c:508) "
    "does not close the file and the socket keeps its network state"
)

#: Confirmed reproducing on every target with CONFIG_SIG_DEFAULT.
KILL_POLL_BUG = (
    "nuttx: a task terminated by a signal default action (_exit()) while "
    "blocked in poll() never runs poll_teardown(), so the file references "
    "taken in poll_setup() (fs/vfs/fs_poll.c) are leaked and the socket is "
    "never closed"
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


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.xfail(strict=True, reason=ACCEPT_ABORTED_BUG)
def test_tcp_rst_after_handshake() -> None:
    """A peer RST right after the handshake makes accept() fail.

    A connection reset by the peer right after the handshake must not
    be accepted as connected, with a blocking send() that then hangs
    forever. ``-D`` delays accept() so the host's RST (a connect +
    ``SO_LINGER(1, 0)`` + close()) lands before accept() runs; ``-w``
    makes the server send (not echo).

    POSIX accept(): "The accept() function shall fail if: ...
    [ECONNABORTED] A connection has been aborted."  PASS: ``nettl:
    accept failed 103`` and ``nettl: FAIL rx=0 err=1`` within 15 s.
    """
    port = 5400
    core = _core(0)
    if core.conf.kv_check("CONFIG_BUILD_KERNEL"):
        # The blocked accept() faults the node: the next tests would fail

        pytest.skip(ACCEPT_ADDR_BUG)
    ret = core.sendCommand(
        f"nettl -s -w -D 2 -p {port} &", "listening", timeout=10
    )
    assert ret == 0
    track_server(NODE_IPS[0], port, False)

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
    if not found:
        # accept() still waits: end the server before the test fails

        nettl_cleanup()
        core.readUntilPattern(SERVER_VERDICT_RE, timeout=10)
    assert found, result.output
    assert f"nettl: accept failed {ECONNABORTED}" in result.output
    assert found.group(0).rstrip("\r\n") == "nettl: FAIL rx=0 err=1"


@pytest.mark.cmd_check("nettl_main")
def test_udp_reuseaddr_broadcast() -> None:
    """A 2nd+ SO_REUSEADDR UDP listener must get a correct d_len.

    With 2+ SO_REUSEADDR UDP listeners on the same port, broadcast
    fan-out must not give the 2nd+ socket a ``d_len`` too large by the
    IP+UDP header size, which corrupts its read-ahead. PASS: both
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

    300 datagrams at a 0.1 s interval take
    around 30 s, more than twice the default
    ``CONFIG_NET_ARP_MAXAGE`` (120 deciseconds), so the node's ARP
    entry for the host expires and must be re-resolved mid-test.
    RFC 1122, 2.3.2.2: "The link layer SHOULD save (rather than
    discard) at least one (the latest) packet of each set of packets
    destined to the same unresolved IP address, and transmit the saved
    packet when the address has been resolved."  PASS: every datagram,
    sent once, is echoed intact.
    """
    port = 5402
    nettl_server(0, True, port, NODE_IPS[0])
    assert host_udp_echo_check(NODE_IPS[0], port, 300, interval=0.1)


@pytest.mark.cmd_check("nettl_main")
def test_tcp_long_transfer() -> None:
    """A sustained TCP transfer must not stall.

    4 MiB host-to-node TCP echo, with a 10 s per-operation timeout
    acting as a stall watchdog (the socket timeout set by
    ``host_tcp_echo_check`` applies to every subsequent recv() on the
    connection). PASS: all bytes echoed intact within that watchdog.
    """
    port = 5403
    nbytes = 4 * 1024 * 1024
    nettl_server(0, False, port, NODE_IPS[0])
    assert host_tcp_echo_check(NODE_IPS[0], port, nbytes, timeout=10.0)


def _bound(core: Any, port: int, proto: str = "udp") -> bool:
    """Return True when a node socket uses a local port.

    :param core: NTFC core handler of the node
    :param port: local port
    :param proto: ``udp`` or ``tcp``
    :return: True when ``/proc/net/<proto>`` has a row with that local port
    """
    ret = core.sendCommandReadUntilPattern(
        f"cat /proc/net/{proto}", pattern=r"nsh> ", timeout=10
    )
    return bool(
        re.search(rf"^ *\d+: .*:{port} ", str(ret.output), re.MULTILINE)
    )


def _wait_unbound(core: Any, port: int, proto: str = "udp") -> bool:
    """Wait up to 5 s for a node port to be released.

    :param core: NTFC core handler of the node
    :param port: local port
    :param proto: ``udp`` or ``tcp``
    :return: True when the port left ``/proc/net/<proto>``
    """
    deadline = time.monotonic() + 5
    while _bound(core, port, proto) and time.monotonic() < deadline:
        time.sleep(0.5)
    return not _bound(core, port, proto)


def _wait_refused(port: int) -> bool:
    """Wait up to 5 s for node 0 to refuse TCP connections to a port.

    :param port: node TCP port
    :return: True when a host connect is refused, False when it connects
     or times out
    """
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            socket.create_connection((NODE_IPS[0], port), timeout=2).close()
            return False
        except ConnectionRefusedError:
            return True
        except OSError:
            time.sleep(0.5)
    return False


def _start_killable(core: Any, cmd: str, pattern: str) -> str:
    """Start a background nettl server and return its PID.

    :param core: NTFC core handler of the node
    :param cmd: nettl command line, without the trailing ``&``
    :param pattern: console pattern printed once the server listens
    :return: PID of the server task
    """
    result = core.sendCommandReadUntilPattern(
        f"{cmd} &", pattern=pattern, timeout=10
    )
    found = re.search(PID_RE, result.output)
    assert found, result.output
    return found.group(1)


def _wait_blocked(core: Any, pid: str) -> None:
    """Wait up to 5 s until a node task waits in a blocking call.

    :param core: NTFC core handler of the node
    :param pid: PID of the task
    """
    deadline = time.monotonic() + 5
    while True:
        ret = core.sendCommandReadUntilPattern(
            "ps", pattern=r"nsh> ", timeout=10
        )
        out = str(ret.output)
        if re.search(rf"^\s*{pid}\s.*\sWaiting\s", out, re.MULTILINE):
            return
        assert time.monotonic() < deadline, f"task {pid} not blocked: {out}"
        time.sleep(0.2)


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.dep_config("CONFIG_SIG_DEFAULT")
@pytest.mark.xfail(strict=True, reason=KILL_POLL_BUG)
@pytest.mark.parametrize("signo", [9, 15])
def test_udp_kill_poll_close(signo: int) -> None:
    """A task killed by a signal while blocked in poll() closes its socket.

    A task terminated by the default action of a signal while blocked
    in poll() must drop the file references poll() holds, so that its
    sockets are closed. ``nettl -s -u -L 1`` blocks in poll()
    on one UDP socket; it is killed with ``kill -<signo>`` (SIGKILL,
    and SIGTERM, which nettl does not catch). PASS: the port leaves
    ``/proc/net/udp`` within 5 s, and a restarted server on the same
    port receives all 3 host datagrams: ``nettl: PASS rx=3 err=0``.
    """
    port = 5410 + signo
    count = 3
    length = 64
    core = _core(0)
    pid = _start_killable(
        core,
        f"nettl -s -u -L 1 -n {count} -l {length} -p {port} -t 60",
        r"listening udp",
    )
    time.sleep(1.0)  # let the server reach its blocking poll()
    assert _bound(core, port)

    ret = core.sendCommand(f"kill -{signo} {pid}", timeout=10)
    assert ret == 0
    assert _wait_unbound(core, port)

    ret = core.sendCommand(
        f"nettl -s -u -L 1 -n {count} -l {length} -p {port} -t 15 &",
        "listening",
        timeout=10,
    )
    assert ret == 0
    _send_udp_burst(NODE_IPS[0], port, count, length)
    result = core.readUntilPattern(SERVER_VERDICT_RE, timeout=20)
    found = re.search(SERVER_VERDICT_RE, result.output)
    assert found, result.output
    assert found.group(0).rstrip("\r\n") == (
        f"nettl: PASS rx={count} err=0"
    ), result.output


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.dep_config("CONFIG_SIG_DEFAULT")
@pytest.mark.xfail(strict=True, reason=KILL_CALL_BUG)
@pytest.mark.parametrize("call", ["recvfrom", "epoll_wait", "accept", "recv"])
def test_kill_blocked_call_close(call: str) -> None:
    """A task killed while blocked in an OS call releases its socket.

    A task terminated by the default action of a signal while blocked
    in an OS call must release the call's file reference and network
    state, so that its socket is closed. A ``nettl`` server is killed
    (``kill -9``) while blocked in:

    - ``recvfrom``: ``nettl -s -u``, UDP. PASS: the port leaves
      ``/proc/net/udp`` within 5 s, the node survives 3 host datagrams
      sent to the port, and a restarted server echoes 3 datagrams.
    - ``epoll_wait``: ``nettl -s -u -L 1 -E``, UDP. PASS: as
      ``recvfrom``.
    - ``accept``: ``nettl -s``, TCP. PASS: a host connect to the port is
      refused within 5 s, and a restarted server echoes 4 KiB.
    - ``recv``: ``nettl -s`` with a host connection open. PASS: the host
      sees the connection closed (EOF or reset) within 5 s, and a server
      started on another port echoes 4 KiB.
    """
    ports = {
        "recvfrom": 5430,
        "accept": 5431,
        "recv": 5432,
        "epoll_wait": 5433,
    }
    port = ports[call]
    udp = call in ("recvfrom", "epoll_wait")
    opts = {"recvfrom": "-u ", "epoll_wait": "-u -L 1 -E -n 3 -l 64 "}
    core = _core(0)
    pid = _start_killable(
        core,
        f"nettl -s {opts.get(call, '')}-p {port} -t 60",
        rf"listening {'udp' if udp else 'tcp'}",
    )
    host = None
    if call == "recv":
        host = socket.create_connection((NODE_IPS[0], port), timeout=5)
    try:
        time.sleep(1.0)  # let the server reach its blocking call
        ret = core.sendCommand(f"kill -9 {pid}", timeout=10)
        assert ret == 0
        if udp:
            assert _wait_unbound(core, port)
        elif host is not None:
            try:
                assert host.recv(16) == b""
            except ConnectionResetError:
                pass
        else:
            assert _wait_refused(port)
    finally:
        if host is not None:
            host.close()

    if udp:
        _send_udp_burst(NODE_IPS[0], port, 3, 64)
        nettl_server(0, True, port, NODE_IPS[0])
        assert host_udp_echo_check(NODE_IPS[0], port, 3)
    else:
        # The closed connection of "recv" keeps its port in TIME_WAIT
        port += 10 if call == "recv" else 0
        nettl_server(0, False, port, NODE_IPS[0])
        assert host_tcp_echo_check(NODE_IPS[0], port, 4096)


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.dep_config("CONFIG_SIG_DEFAULT")
@pytest.mark.usefixtures("reboot_after_failure")
@pytest.mark.xfail(strict=True, reason=KILL_CALL_BUG)
def test_tcp_kill_listener_leak() -> None:
    """A kill -9 of a task blocked in accept() must not leak its conn.

    Killing a task blocked in accept() on a listening TCP socket must
    free its TCP connection. Repeats
    ``CONFIG_NET_TCP_PREALLOC_CONNS + 1`` times: start a listener,
    kill -9 it once ``ps`` shows it waiting in accept(). PASS: a final
    probe connect still allocates a socket and is refused by the host
    (``connect failed 111``); FAIL if the leak exhausted the
    preallocated pool (``socket failed 12`` / ``connect failed 12``).
    Without ``CONFIG_SIG_DEFAULT`` ``kill -9`` does not end a task.
    """
    core = _core(0)
    prealloc = core.conf.kv_check("CONFIG_NET_TCP_PREALLOC_CONNS")
    assert prealloc, "CONFIG_NET_TCP_PREALLOC_CONNS not set"

    base_port = 5500
    for i in range(int(prealloc) + 1):
        pid = _start_killable(
            core, f"nettl -s -p {base_port + i} -t 30", r"listening tcp"
        )
        _wait_blocked(core, pid)
        ret = core.sendCommand(f"kill -9 {pid}", timeout=10)
        assert ret == 0

    probe = core.sendCommandReadUntilPattern(
        f"nettl -c {HOST_IP} -p 9 -n 1",
        pattern=CONNECT_FAILED_RE,
        timeout=15,
    )
    found = re.search(CONNECT_FAILED_RE, probe.output)
    assert found, probe.output
    assert found.group(1) == "111", probe.output
