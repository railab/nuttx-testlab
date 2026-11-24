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

"""TCP connection handling between the host and NuttX nodes."""

import concurrent.futures
import re
import socket
import struct
import subprocess
import time
from typing import Any, List

import pytest
from _host_services import TcpEchoServer
from _net_common import (
    ACCEPT_ADDR_BUG,
    HOST_IP,
    NODE_IPS,
    VERDICT_RE,
    host_tcp_echo_check,
    nettl_server,
    pattern,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_TCP"),
    pytest.mark.cmd_check("nettl_main"),
]

SERVER_VERDICT_RE = r"nettl: (PASS|FAIL) rx=\d+ err=\d+[\r\n]"
CHUNK = 1024
POLLERR = 0x08
ECONNRESET = 104


def _core() -> Any:
    """Return the NTFC core handler of node 0.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.products[0].core(0)


def _server_verdict(timeout: float = 20) -> str:
    """Wait for the verdict of a background nettl server on node 0.

    :param timeout: seconds to wait
    :return: verdict line or empty string
    """
    result = _core().readUntilPattern(SERVER_VERDICT_RE, timeout=timeout)
    found = re.search(SERVER_VERDICT_RE, result.output)
    return found.group(0).rstrip("\r\n") if found else ""


def _prealloc() -> int:
    """Return the node's preallocated TCP connection count.

    :return: ``CONFIG_NET_TCP_PREALLOC_CONNS`` or 8 when not set
    """
    value = _core().conf.kv_check("CONFIG_NET_TCP_PREALLOC_CONNS")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 8


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """Receive exactly ``size`` bytes.

    :param sock: connected socket
    :param size: number of bytes
    :return: received data (shorter on EOF)
    """
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def test_tcp_concurrent_host_to_node() -> None:
    """8 host connections are echoed at once by one node server.

    The host opens the 8 connections at the same time; the server
    listens with a backlog of 8.  POSIX listen(): "The backlog argument
    provides a hint to the implementation which the implementation shall
    use to limit the number of outstanding connections in the socket's
    listen queue."  The data then flows on all connections interleaved.
    """
    conns, size, port = 8, 32 * CHUNK, 5250
    ret = _core().sendCommand(
        f"nettl -s -C {conns} -p {port} -t 30 &", "listening", timeout=10
    )
    assert ret == 0
    socks: List[socket.socket] = []
    try:
        ok = True
        with concurrent.futures.ThreadPoolExecutor(conns) as pool:
            jobs = [
                pool.submit(socket.create_connection, (NODE_IPS[0], port), 10)
                for _ in range(conns)
            ]
        socks = [job.result() for job in jobs if not job.exception()]
        assert len(socks) == conns, [job.exception() for job in jobs]
        for off in range(0, size, CHUNK):
            for sock in socks:
                sock.sendall(pattern(off, CHUNK))
            for sock in socks:
                ok = ok and _recv_exact(sock, CHUNK) == pattern(off, CHUNK)
        assert ok, "echo mismatch"
    finally:
        for sock in socks:
            sock.close()
    assert _server_verdict() == f"nettl: PASS rx={conns * size} err=0"


def test_tcp_half_close() -> None:
    """Data sent before the host's FIN is echoed, then the node closes."""
    size, port = 64 * CHUNK, 5251
    nettl_server(0, False, port, NODE_IPS[0])
    with socket.create_connection((NODE_IPS[0], port), timeout=10) as sock:
        for off in range(0, size, CHUNK):
            sock.sendall(pattern(off, CHUNK))
        sock.shutdown(socket.SHUT_WR)
        echo = b""
        while True:
            data = sock.recv(4096)
            if not data:
                break
            echo += data
    assert echo == pattern(0, size)
    assert _server_verdict() == f"nettl: PASS rx={size} err=0"


def test_tcp_churn_node_client() -> None:
    """Node 0 opens more connections than it preallocates, one by one.

    The host closes first, so the node does not keep the connections in
    TIME-WAIT: RFC 9293, 3.6: "When a connection is closed actively, it
    MUST linger in the TIME-WAIT state for a time 2xMSL", which would
    hold every preallocated connection.
    """
    count, port = 3 * _prealloc(), 5252
    with TcpEchoServer(HOST_IP, port, close_after=CHUNK) as srv:
        for i in range(count):
            ret = (
                _core()
                .sendCommandReadUntilPattern(
                    f"nettl -c {HOST_IP} -p {port} -n {CHUNK}",
                    pattern=VERDICT_RE,
                    timeout=30,
                )
                .output
            )
            found = re.search(VERDICT_RE, ret)
            verdict = found.group(0).strip() if found else ret
            assert verdict.startswith("nettl: PASS"), f"#{i}: {verdict}"
    assert srv.connections == count


def _churn_node_server(port: int) -> None:
    """Run more node server connections than the node preallocates.

    :param port: node server port
    """
    for i in range(3 * _prealloc()):
        nettl_server(0, False, port, NODE_IPS[0])
        assert host_tcp_echo_check(NODE_IPS[0], port, CHUNK), f"#{i}"
        assert _server_verdict().startswith("nettl: PASS"), f"#{i}"


def test_tcp_churn_node_server() -> None:
    """Node 0 accepts more connections than it preallocates, one by one.

    The host closes first.
    """
    if _core().conf.kv_check("CONFIG_BUILD_KERNEL"):
        pytest.skip(ACCEPT_ADDR_BUG)
    _churn_node_server(5253)


def test_tcp_backlog_overflow() -> None:
    """Connections beyond the listen backlog do not break the node.

    The server (backlog 1) accepts only after 5 s; the host meanwhile
    tries more connections than the backlog holds, sends nothing and
    closes them all.  The first connection is within the backlog, so
    accept() returns it (POSIX accept(): "shall extract the first
    connection on the queue of pending connections") and the server
    reads the end of the stream: ``nettl: PASS rx=0 err=0``.  Afterwards
    the node must still accept and echo new connections.
    """
    port = 5254
    ret = _core().sendCommand(
        f"nettl -s -p {port} -D 5 -t 30 &", "listening", timeout=10
    )
    assert ret == 0
    socks: List[socket.socket] = []
    connected: List[bool] = []
    try:
        for _ in range(8):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            try:
                sock.connect((NODE_IPS[0], port))
                connected.append(True)
            except OSError:
                connected.append(False)
            socks.append(sock)
    finally:
        for sock in socks:
            sock.close()
    assert connected[0], "connection within the backlog failed"
    assert _server_verdict(timeout=30) == "nettl: PASS rx=0 err=0"
    _churn_node_server(port)


def test_tcp_rst_mid_transfer() -> None:
    """A host RST in the middle of a transfer ends the node server.

    RFC 9293, 3.10.7.4, RST in the ESTABLISHED state: "any outstanding
    RECEIVEs and SEND should receive "reset" responses".  POSIX recv()
    and send(): "[ECONNRESET] A connection was forcibly closed by a
    peer."  The stream never ends cleanly, so the server reports one
    error: ``nettl: FAIL rx=<n> err=1``, and a failed recv() reports
    ECONNRESET (104).  A new server must work afterwards.
    """
    port = 5255
    nettl_server(0, False, port, NODE_IPS[0])
    sock = socket.create_connection((NODE_IPS[0], port), timeout=10)
    for off in range(0, 16 * CHUNK, CHUNK):
        sock.sendall(pattern(off, CHUNK))
    sock.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
    )
    sock.close()
    out = _core().readUntilPattern(SERVER_VERDICT_RE, timeout=20).output
    assert re.search(r"nettl: FAIL rx=\d+ err=1[\r\n]", out), out
    failed = re.search(r"nettl: recv failed (\d+)", out)
    assert failed is None or failed.group(1) == str(ECONNRESET), out
    nettl_server(0, False, port, NODE_IPS[0])
    assert host_tcp_echo_check(NODE_IPS[0], port, 16 * CHUNK)


def test_tcp_send_after_peer_close() -> None:
    """A send on a connection the host closed fails with EPIPE.

    The host accepts and closes at once; the node waits for the FIN and
    sends until the host's reset ends the connection.  POSIX send():
    "[EPIPE] The socket is shut down for writing, or the socket is
    connection-mode and is no longer connected. In the latter case, and
    if the socket is of type SOCK_STREAM or SOCK_SEQPACKET and the
    MSG_NOSIGNAL flag is not set, the SIGPIPE signal is generated to the
    calling thread."  The first failing send may report the reset
    instead: "[ECONNRESET] A connection was forcibly closed by a peer."
    """
    port = 5259
    with TcpEchoServer(HOST_IP, port, close_after=0) as srv:
        out = (
            _core()
            .sendCommandReadUntilPattern(
                f"nettl -c {HOST_IP} -p {port} -X -t 10",
                pattern=VERDICT_RE,
                timeout=30,
            )
            .output
        )
    assert srv.connections == 1
    found = re.search(VERDICT_RE, out)
    assert found and found.group(1) == "PASS", out


@pytest.mark.dep_config("CONFIG_NET_TCP_KEEPALIVE")
def test_tcp_keepalive_live_peer() -> None:
    """Keep-alive probes answered by the host keep an idle connection up.

    The node client idles 6 s with keep-alive (1 s idle, 1 s interval, 3
    probes), so the connection only survives if the host answers the
    probes; then it echoes 1 KiB.
    """
    port = 5256
    with TcpEchoServer(HOST_IP, port):
        out = (
            _core()
            .sendCommandReadUntilPattern(
                f"nettl -c {HOST_IP} -p {port} -n {CHUNK} -k 1 -t 6",
                pattern=VERDICT_RE,
                timeout=30,
            )
            .output
        )
    assert "nettl: idle ok" in out, out
    found = re.search(VERDICT_RE, out)
    assert found and found.group(1) == "PASS", out


def _keepalive_dead_peer(port: int, opts: str) -> str:
    """Run a keep-alive node client while the host stops answering it.

    The host blackholes the route to node 0 once the client is connected
    and idle (1 s idle, 1 s interval, 3 probes, 20 s timeout). The
    connection must be found dead within 10 s.

    :param port: host echo server port
    :param opts: extra ``nettl`` client options
    :return: node output up to the client verdict
    """
    route = ["ip", "route", "add", "blackhole", f"{NODE_IPS[0]}/32"]
    with TcpEchoServer(HOST_IP, port) as srv:
        out = (
            _core()
            .sendCommandReadUntilPattern(
                f"nettl -c {HOST_IP} -p {port} -n {CHUNK} -k 1 -t 20{opts} &",
                pattern=r"nettl \[\d+:",
                timeout=10,
            )
            .output
        )
        assert re.search(r"nettl \[\d+:", out), out
        assert srv.accepted.wait(10), "node did not connect"
        subprocess.run(route, check=True)
        start = time.monotonic()
        try:
            out = _core().readUntilPattern(VERDICT_RE, timeout=25).output
        finally:
            route[2] = "del"
            subprocess.run(route, check=False)
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"detected after {elapsed:.1f} s"
    return out


@pytest.mark.dep_config("CONFIG_NET_TCP_KEEPALIVE")
def test_tcp_keepalive_dead_peer() -> None:
    """Unanswered keep-alive probes end the connection with ETIMEDOUT.

    The node client idles in recv() with keep-alive while the host stops
    answering it (a blackhole route to the node). recv() must fail with
    ETIMEDOUT (110) within 10 s, well before its 20 s receive timeout.
    """
    out = _keepalive_dead_peer(5257, "")
    found = re.search(r"nettl: recv failed (\d+)", out)
    assert found, out
    assert found.group(1) == "110", out


@pytest.mark.dep_config("CONFIG_NET_TCP_KEEPALIVE")
def test_tcp_keepalive_dead_peer_poll() -> None:
    """Unanswered keep-alive probes wake poll() with a pending ETIMEDOUT.

    Same as ``test_tcp_keepalive_dead_peer`` with the client waiting in
    poll(POLLIN).  POSIX poll(): "All error
    conditions that arise solely from the state of the object underlying
    the open file description and would be diagnosed by a return of -1
    from a read() or write() call on the file descriptor shall be
    reported as a POLLERR event."
    getsockopt(SO_ERROR) must return ETIMEDOUT (110), the error recv()
    reports.
    """
    out = _keepalive_dead_peer(5258, " -P")
    found = re.search(
        r"nettl: poll revents (0x[0-9a-f]+) so_error (-?\d+)", out
    )
    assert found, out
    revents = int(found.group(1), 16)
    assert revents & POLLERR, out
    assert found.group(2) == "110", out
