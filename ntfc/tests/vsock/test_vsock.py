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

"""virtio socket device (vsock) stream tests.

QEMU gives the node a ``vhost-vsock`` device with the guest CID set by
``guest-cid=`` in the product's QEMU arguments; the host end of each
connection is an ``AF_VSOCK`` socket in the test process (CID 2,
``VMADDR_CID_HOST``). The node side is ``nettl -V``.

Expectations come from the VIRTIO 1.2 socket device section (stream
sockets, buffer space management, disconnect) and from POSIX
``connect()``/``recv()``.
"""

import errno
import re
import socket
import threading
from typing import Iterator, List, Optional, Tuple

import pytest
from _net_common import VERDICT_RE, pattern

VMADDR_CID_HOST = 2
NETTL_TIMEOUT = 30

NETTL_SERVER_RE = r"nettl: (PASS|FAIL) rx=[^\r\n]*[\r\n]"
CONNECT_FAILED_RE = r"nettl: connect failed (\d+)"

_SERVERS: List[int] = []


def _guest_cid() -> int:
    """Return the guest CID given to QEMU for this product.

    :return: ``guest-cid=`` value of the ``vhost-vsock`` device
    """
    args = str(pytest.product.core(0).conf.exec_args)
    found = re.search(r"vhost-vsock-[a-z]+,\S*?guest-cid=(\d+)", args)
    assert found, f"no vhost-vsock guest-cid in {args!r}"
    return int(found.group(1))


def _vsock() -> socket.socket:
    """Create a host ``AF_VSOCK`` stream socket.

    :return: socket with a timeout set
    """
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.settimeout(NETTL_TIMEOUT)
    return sock


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    """Receive exactly ``length`` bytes or until EOF.

    :param sock: connected socket
    :param length: bytes wanted
    :return: data received, shorter than ``length`` only on EOF
    """
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _recv_eof(sock: socket.socket) -> bytes:
    """Receive until the peer ends the stream.

    :param sock: connected socket
    :return: all data received before ``recv()`` returned 0
    """
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk


class _HostEcho:
    """Host ``AF_VSOCK`` echo server for one connection, in a thread."""

    def __init__(self, port: int) -> None:
        """Bind and listen on ``VMADDR_CID_ANY``.

        :param port: vsock port
        """
        self.sock = _vsock()
        self.sock.bind((socket.VMADDR_CID_ANY, port))
        self.sock.listen(1)
        self.peer: Optional[Tuple[int, int]] = None
        self.echoed = 0
        self.error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Accept one connection and echo it until EOF."""
        try:
            conn, self.peer = self.sock.accept()
            with conn:
                conn.settimeout(NETTL_TIMEOUT)
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(data)
                    self.echoed += len(data)
        except OSError as exc:
            self.error = exc

    def close(self) -> None:
        """Wait for the connection to end and close the listener."""
        self._thread.join(NETTL_TIMEOUT)
        self.sock.close()


def _guest_server(port: int, extra: str = "") -> None:
    """Start a background ``nettl -V -s`` on the node.

    :param port: vsock port to listen on
    :param extra: additional nettl server options
    """
    opts = f"{extra} " if extra else ""
    _SERVERS.append(port)
    ret = pytest.product.sendCommand(
        f"nettl -V -s {opts}-p {port} -t {NETTL_TIMEOUT} &",
        "listening",
        timeout=10,
    )
    assert ret == 0


def _host_echo(port: int, count: int) -> bool:
    """Echo ``count`` pattern bytes lock-step through a node server.

    :param port: node vsock port
    :param count: bytes to send, a multiple of 1024
    :return: True when every echoed chunk matches
    """
    with _vsock() as sock:
        sock.connect((_guest_cid(), port))
        for off in range(0, count, 1024):
            chunk = pattern(off, 1024)
            sock.sendall(chunk)
            if _recv_exact(sock, len(chunk)) != chunk:
                return False
    return True


def _guest_server_verdict() -> str:
    """Wait for the verdict of the background node server.

    :return: verdict line or empty string
    """
    ret = pytest.product.core(0).readUntilPattern(
        NETTL_SERVER_RE, timeout=NETTL_TIMEOUT
    )
    _SERVERS.clear()
    found = re.search(NETTL_SERVER_RE, str(ret.output))
    return found.group(0).rstrip("\r\n") if found else ""


def _guest_client(port: int, count: int, extra: str = "") -> str:
    """Run ``nettl -V -c 2`` on the node and return its output.

    :param port: host vsock port
    :param count: bytes to echo
    :param extra: additional nettl client options
    :return: command output up to the verdict line
    """
    opts = f"{extra} " if extra else ""
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        f"nettl -V -c {VMADDR_CID_HOST} {opts}-p {port} -n {count} "
        f"-t {NETTL_TIMEOUT}",
        pattern=VERDICT_RE,
        timeout=NETTL_TIMEOUT + 10,
    )
    return str(ret.output)


def _verdict(output: str) -> str:
    """Extract the nettl client verdict line.

    :param output: command output
    :return: verdict line or empty string
    """
    found = re.search(VERDICT_RE, output)
    return found.group(0).rstrip("\r\n") if found else ""


@pytest.fixture(autouse=True)
def guest_servers() -> Iterator[None]:
    """Let a node server a failed test left running exit normally.

    A connect + close gives it end of stream instead of killing it.
    """
    yield
    while _SERVERS:
        port = _SERVERS.pop()
        try:
            with _vsock() as sock:
                sock.settimeout(2)
                sock.connect((_guest_cid(), port))
        except OSError:
            pass  # server already exited


@pytest.mark.cmd_check("nettl_main")
def test_vsock_guest_connect_echo() -> None:
    """Echo a stream the node opens to the host.

    VIRTIO 1.2, Stream Sockets: "Stream sockets provide in-order,
    guaranteed, connection-oriented delivery without message
    boundaries." Receive and Transmit (driver): "The guest_cid
    configuration field MUST be used as the source CID when sending
    outgoing packets", so the host sees the connection from the
    configured guest CID.
    """
    count = 65536
    server = _HostEcho(20001)
    try:
        output = _guest_client(20001, count)
    finally:
        server.close()
    assert _verdict(output) == f"nettl: PASS tx={count} rx={count} err=0"
    assert server.error is None
    assert server.echoed == count
    assert server.peer is not None and server.peer[0] == _guest_cid()


@pytest.mark.cmd_check("nettl_main")
def test_vsock_host_connect_echo() -> None:
    """Echo a stream the host opens to the node's listening port.

    VIRTIO 1.2, Stream Sockets: "If a listening socket exists on the
    destination a VIRTIO_VSOCK_OP_RESPONSE reply is sent and the
    connection is established." The host closing its end is seen by
    the node as end of stream (POSIX ``recv()`` returns 0 after an
    orderly shutdown), which ends the node server.
    """
    count = 32768
    _guest_server(20002)
    assert _host_echo(20002, count)
    assert _guest_server_verdict() == f"nettl: PASS rx={count} err=0"


@pytest.mark.cmd_check("nettl_main")
def test_vsock_host_bulk_flow_control() -> None:
    """Send a large burst to the node without waiting for the echo.

    VIRTIO 1.2, Buffer Space Management: credit accounting "facilitates
    flow control so data is never dropped", and "VIRTIO_VSOCK_OP_RW
    data packets MUST only be transmitted when the peer has sufficient
    free buffer space for the payload." The burst is far larger than the
    node's receive buffer, so the stream only survives intact if both
    sides honour each other's credit.
    """
    count = 512 * 1024
    data = pattern(0, count)
    echoed: List[bytes] = []
    _guest_server(20003)
    with _vsock() as sock:
        sock.connect((_guest_cid(), 20003))
        reader = threading.Thread(
            target=lambda: echoed.append(_recv_exact(sock, count)),
            daemon=True,
        )
        reader.start()
        sock.sendall(data)
        reader.join(NETTL_TIMEOUT * 2)
        assert not reader.is_alive(), "echo not complete"
    assert echoed and echoed[0] == data
    assert _guest_server_verdict() == f"nettl: PASS rx={count} err=0"


@pytest.mark.cmd_check("nettl_main")
def test_vsock_guest_close_eof() -> None:
    """Read a stream the node writes and closes until end of stream.

    VIRTIO 1.2, Stream Sockets: "Clean disconnect is achieved by one or
    more VIRTIO_VSOCK_OP_SHUTDOWN packets that indicate no more data
    will be sent and received". POSIX ``recv()``: once "the peer has
    performed an orderly shutdown, recv() shall return 0", after all
    data sent before the close.
    """
    count = 65536
    _guest_server(20004, f"-w -n {count}")
    with _vsock() as sock:
        sock.connect((_guest_cid(), 20004))
        data = _recv_eof(sock)
    assert _guest_server_verdict() == "nettl: PASS rx=0 err=0"
    assert data == pattern(0, count)


@pytest.mark.cmd_check("nettl_main")
def test_vsock_host_connect_refused() -> None:
    """Connect from the host to a node port nobody listens on.

    VIRTIO 1.2, Stream Sockets: "A VIRTIO_VSOCK_OP_RST reply is sent if
    a listening socket does not exist on the destination".  The host
    stack ends a connection request that is answered with
    VIRTIO_VSOCK_OP_RST with ECONNRESET; without a reply connect() times
    out instead.
    """
    with _vsock() as sock:
        with pytest.raises(ConnectionResetError):
            sock.connect((_guest_cid(), 20005))


@pytest.mark.cmd_check("nettl_main")
def test_vsock_guest_connect_refused() -> None:
    """Connect from the node to a host port nobody listens on.

    VIRTIO 1.2, Stream Sockets: "A VIRTIO_VSOCK_OP_RST reply is sent if
    a listening socket does not exist on the destination"; POSIX
    ``connect()`` then fails with [ECONNREFUSED].
    """
    output = _guest_client(20006, 1024)
    found = re.search(CONNECT_FAILED_RE, output)
    assert found, output
    assert int(found.group(1)) == errno.ECONNREFUSED
    assert _verdict(output) == "nettl: FAIL tx=0 rx=0 err=1"


@pytest.mark.cmd_check("nettl_main")
def test_vsock_reuseaddr_server() -> None:
    """Set ``SO_REUSEADDR`` on a node vsock server, then echo.

    POSIX ``setsockopt()``: SO_REUSEADDR allows reuse of local addresses
    "if this is supported by the protocol"; the call either succeeds or
    fails with [ENOPROTOOPT], it never blocks. The server then listens
    and echoes a host stream.
    """
    count = 4096
    _guest_server(20007, "-A")
    assert _host_echo(20007, count)
    assert _guest_server_verdict() == f"nettl: PASS rx={count} err=0"
