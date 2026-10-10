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

import re
import socket
import struct
import subprocess
import time
from typing import Any, List

import pytest
from _host_services import TcpEchoServer
from _net_common import (
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

SOLINGER_TIME_WAIT_BUG = (
    "net/tcp/tcp_conn.c: with CONFIG_NET_SOLINGER tcp_alloc() never "
    "reuses TIME_WAIT connections, so a node client fails once "
    "CONFIG_NET_TCP_PREALLOC_CONNS sockets are in TIME_WAIT"
)

KEEPALIVE_ERRNO_BUG = (
    "net/tcp/tcp_timer.c: keep-alive expiry reports TCP_ABORT instead of "
    "TCP_TIMEDOUT and tcp_recvhandler() (net/tcp/tcp_recvfrom.c) maps "
    "every TCP_DISCONN_EVENTS flag to -ENOTCONN, so recv() fails with "
    "ENOTCONN instead of ETIMEDOUT"
)


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


@pytest.fixture
def solinger_time_wait_xfail(request: pytest.FixtureRequest) -> None:
    """Expect TIME_WAIT exhaustion on nodes built with SO_LINGER.

    :param request: pytest request of the test using this fixture
    """
    if _core().conf.kv_check("CONFIG_NET_SOLINGER"):
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=SOLINGER_TIME_WAIT_BUG)
        )


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

    Each connection is opened only after the previous one echoed its
    first chunk (so it was accepted); the remaining data then flows on
    all connections interleaved.
    """
    conns, size, port = 8, 32 * CHUNK, 5250
    ret = _core().sendCommand(
        f"nettl -s -C {conns} -p {port} -t 30 &", "listening", timeout=10
    )
    assert ret == 0
    socks: List[socket.socket] = []
    try:
        ok = True
        for _ in range(conns):
            sock = socket.create_connection((NODE_IPS[0], port), timeout=10)
            socks.append(sock)
            sock.sendall(pattern(0, CHUNK))
            ok = ok and _recv_exact(sock, CHUNK) == pattern(0, CHUNK)
        for off in range(CHUNK, size, CHUNK):
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


@pytest.mark.run(order=-2)
@pytest.mark.usefixtures("solinger_time_wait_xfail")
def test_tcp_churn_node_client() -> None:
    """Node 0 opens and closes more connections than it preallocates.

    The node closes first, so each socket ends in TIME_WAIT on the node.
    Runs second to last in the session (``order=-2``): a failure leaves
    the node without TCP resources for the TIME_WAIT period.
    """
    count, port = 3 * _prealloc(), 5252
    with TcpEchoServer(HOST_IP, port) as srv:
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
    _churn_node_server(5253)


def test_tcp_backlog_overflow() -> None:
    """Connections beyond the listen backlog do not break the node.

    The server accepts only after 5 s; the host meanwhile tries more
    connections than the backlog holds. Afterwards the node must still
    accept and echo new connections.
    """
    port = 5254
    ret = _core().sendCommand(
        f"nettl -s -p {port} -D 5 -t 30 &", "listening", timeout=10
    )
    assert ret == 0
    socks: List[socket.socket] = []
    try:
        for _ in range(8):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            try:
                sock.connect((NODE_IPS[0], port))
            except OSError:
                pass
            socks.append(sock)
    finally:
        for sock in socks:
            sock.close()
    _server_verdict(timeout=30)
    _churn_node_server(port)


def test_tcp_rst_mid_transfer() -> None:
    """A host RST in the middle of a transfer ends the node server.

    The node server must report the reset instead of hanging, and a new
    server must work afterwards.
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
    assert _server_verdict().startswith("nettl: "), "server hung"
    nettl_server(0, False, port, NODE_IPS[0])
    assert host_tcp_echo_check(NODE_IPS[0], port, 16 * CHUNK)


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


@pytest.mark.dep_config("CONFIG_NET_TCP_KEEPALIVE")
@pytest.mark.xfail(strict=True, reason=KEEPALIVE_ERRNO_BUG)
def test_tcp_keepalive_dead_peer() -> None:
    """Unanswered keep-alive probes end the connection with ETIMEDOUT.

    The host stops answering the node (a blackhole route to it) while
    the node client idles in recv() with keep-alive (1 s idle, 1 s
    interval, 3 probes). recv() must fail with ETIMEDOUT (110) within
    10 s, well before its 20 s receive timeout.
    """
    port = 5257
    route = ["ip", "route", "add", "blackhole", f"{NODE_IPS[0]}/32"]
    with TcpEchoServer(HOST_IP, port) as srv:
        out = (
            _core()
            .sendCommandReadUntilPattern(
                f"nettl -c {HOST_IP} -p {port} -n {CHUNK} -k 1 -t 20 &",
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
    found = re.search(r"nettl: recv failed (\d+)", out)
    assert found, out
    assert elapsed < 10, f"detected after {elapsed:.1f} s"
    assert found.group(1) == "110", out
