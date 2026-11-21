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

"""RPMSG domain socket (``AF_RPMSG``) tests between two nodes.

Node 0 runs the ``rpsocktl -s`` server in the background, node 1 the
``rpsocktl -c`` client checks (``apps/rpsocktl``). Expectations are
quoted from POSIX.1-2024, https://pubs.opengroup.org/onlinepubs/9799919799/
(``functions/<name>.html``).
"""

import re
from typing import Tuple

import pytest
from _rpmsg_common import core, cpuname, verdict

SERVER_RE = r"rpsocktl: (PASS|FAIL) server [^\r\n]*"
CLIENT_RE = r"rpsocktl: (PASS|FAIL) (?!server)[^\r\n]*"
TIMEOUT = 60

SOCKIF = "net/rpmsg/rpmsg_sockif.c"

LISTEN_DGRAM_BUG = (
    f"{SOCKIF}:1096 rpmsg_socket_listen() returns ENOSYS for a socket "
    "that is not SOCK_STREAM; POSIX listen() fails with [EOPNOTSUPP] "
    "'The socket protocol does not support listen()'"
)
LISTEN_UNBOUND_BUG = (
    f"{SOCKIF}:1099 rpmsg_socket_listen() returns EINVAL for an unbound "
    "socket; POSIX listen() fails with [EDESTADDRREQ] 'The socket is not "
    "bound to a local address, and the protocol does not support "
    "listening on an unbound socket' ([EINVAL]: already connected)"
)
LISTEN_BACKLOG_BUG = (
    f"{SOCKIF}:1099 rpmsg_socket_listen() fails with EINVAL for a "
    "backlog of 0; POSIX listen() takes a backlog below 0 as 0, 'A "
    "backlog argument of 0 may allow the socket to accept connections', "
    "and lists no error for it"
)
RECV_NOTCONN_BUG = (
    f"{SOCKIF}:1669 rpmsg_socket_recvmsg() returns EISCONN on a socket "
    "that is not connected; POSIX recv() fails with [ENOTCONN] 'A "
    "receive is attempted on a connection-mode socket that is not "
    "connected'"
)
PEERNAME_BUG = (
    f"{SOCKIF}:1083 rpmsg_socket_getpeername() returns the unset peer "
    "address of a socket that is not connected; POSIX getpeername() "
    "fails with [ENOTCONN] 'The socket is not connected'"
)
SHUTDOWN_DATA_BUG = (
    f"{SOCKIF}:1672 rpmsg_socket_recvmsg() returns 0 as soon as the "
    "peer's SHUT_WR arrived, before reading the data still in the "
    "receive buffer; POSIX recv() returns 0 only 'If no messages are "
    "available to be received and the peer has performed an orderly "
    "shutdown'"
)
SELECT_EOF_BUG = (
    f"{SOCKIF}:1291 rpmsg_socket_poll() reports no event for a "
    "connected socket whose peer did shutdown(SHUT_WR) (conn->how has "
    "SHUT_RD, the buffer is empty), so select() waits although recv() "
    "returns 0 at once; POSIX select(): 'A descriptor shall be considered "
    "ready for reading when a call to an input function with O_NONBLOCK "
    "clear would not block'"
)
READV_BUG = (
    f"{SOCKIF}:1651 rpmsg_socket_recvmsg() fails with ENOTSUP for more "
    "than one iovec; POSIX readv() 'shall place the input data into the "
    "iovcnt buffers specified by the members of the iov array'"
)
REFUSED_BUG = (
    f"{SOCKIF}:1152 rpmsg_socket_connect_internal() turns the NS_DESTROY "
    "that rpmsg_socket_ns_bind() sends when nobody listens on the name "
    "into ECONNRESET; POSIX connect() fails with [ECONNREFUSED] 'The "
    "target address was not listening for connections or refused the "
    "connection request'"
)
TRUNC_BUG = (
    f"{SOCKIF}:1688 (and the direct copy at :507) rpmsg_socket_recvmsg() "
    "discards the rest of a datagram longer than the buffer without "
    "setting MSG_TRUNC; POSIX recvmsg(): 'the excess bytes shall be "
    "discarded, and MSG_TRUNC shall be set in the msg_flags member'"
)


def _xfail(bug: str) -> pytest.MarkDecorator:
    """Return a strict xfail mark.

    :param bug: reason naming the NuttX bug
    :return: mark
    """
    return pytest.mark.xfail(strict=True, reason=bug)


def _pair(
    name: str, server: str, client: str, udp: bool = False
) -> Tuple[str, str]:
    """Run an ``rpsocktl`` server on node 0 and a client on node 1.

    :param name: socket name, unique per test
    :param server: server options
    :param client: client options
    :param udp: datagram socket; the server connects to node 1
    :return: server and client verdict lines
    """
    opts = f"-u -c {cpuname(1)} -t 5 {server}" if udp else server
    ret = core(0).sendCommand(
        f"rpsocktl -s {opts} {name} &", "listening", timeout=10
    )
    assert ret == 0, "rpsocktl server did not start"
    copts = f"-u {client}" if udp else client
    cli = verdict(1, f"rpsocktl -c {cpuname(0)} {copts} {name}", CLIENT_RE)
    ret = core(0).readUntilPattern(SERVER_RE, timeout=TIMEOUT)
    found = re.search(SERVER_RE, str(ret.output))
    return (found.group(0).strip() if found else ""), cli


def _fields(line: str) -> dict:
    """Return the ``key=value`` fields of a verdict line.

    :param line: verdict line
    :return: field values by key
    """
    return dict(re.findall(r"(\w+)=(\S*)", line))


@pytest.mark.parametrize(
    "check",
    [
        pytest.param("dgramlisten", marks=_xfail(LISTEN_DGRAM_BUG)),
        pytest.param("unboundlisten", marks=_xfail(LISTEN_UNBOUND_BUG)),
        pytest.param("listen0", marks=_xfail(LISTEN_BACKLOG_BUG)),
        "acceptnl",
        pytest.param("recvnc", marks=_xfail(RECV_NOTCONN_BUG)),
        "sendnc",
        pytest.param("peernc", marks=_xfail(PEERNAME_BUG)),
        "shutnc",
    ],
)
def test_rpsock_errno(check: str) -> None:
    """A call on a socket without a peer fails with the POSIX errno.

    - ``dgramlisten``: listen() on a bound ``SOCK_DGRAM`` socket,
      listen(): "[EOPNOTSUPP] The socket protocol does not support
      listen()."
    - ``unboundlisten``: listen() on an unbound stream socket, listen():
      "[EDESTADDRREQ] The socket is not bound to a local address, and the
      protocol does not support listening on an unbound socket."
    - ``listen0``: listen(fd, 0) on a bound stream socket succeeds,
      listen(): "A backlog argument of 0 may allow the socket to accept
      connections, in which case the length of the listen queue may be
      set to an implementation-defined minimum value." (no error listed)
    - ``acceptnl``: accept() on a bound socket that does not listen,
      accept(): "[EINVAL] The socket is not accepting connections."
    - ``recvnc``: recv(): "[ENOTCONN] A receive is attempted on a
      connection-mode socket that is not connected."
    - ``sendnc``: send(): "[ENOTCONN] The socket is not connected."
    - ``peernc``: getpeername(): "[ENOTCONN] The socket is not connected
      or otherwise has not had the peer pre-specified."
    - ``shutnc``: shutdown(): "[ENOTCONN] The socket is not connected."

    :param check: ``rpsocktl -e`` check
    """
    line = verdict(1, f"rpsocktl -e {check}", CLIENT_RE)
    assert line.startswith(f"rpsocktl: PASS {check}"), line


def test_rpsock_eof_after_close() -> None:
    """Data sent before close() is read, then end-of-file.

    recv(): "If no messages are available to be received and the peer
    has performed an orderly shutdown, recv() shall return 0." The
    client reads only after the data and the close arrived.
    """
    srv, cli = _pair("tl-eofc", "-m close -n 1000", "-m eof -n 1000")
    assert srv.startswith("rpsocktl: PASS"), srv
    assert cli.startswith("rpsocktl: PASS eof rx=1000"), cli


@pytest.mark.xfail(strict=True, reason=SHUTDOWN_DATA_BUG)
def test_rpsock_eof_after_shutdown() -> None:
    """Data sent before shutdown(SHUT_WR) is read, then end-of-file.

    shutdown(): "SHUT_WR Disables further send operations." recv(): "If
    no messages are available to be received and the peer has performed
    an orderly shutdown, recv() shall return 0." The data the peer sent
    before is still available, so it is returned first.
    """
    srv, cli = _pair("tl-eofs", "-m send -n 1000", "-m eof -n 1000")
    assert srv.startswith("rpsocktl: PASS"), srv
    assert cli.startswith("rpsocktl: PASS eof rx=1000"), cli


@pytest.mark.xfail(strict=True, reason=SELECT_EOF_BUG)
def test_rpsock_select_eof() -> None:
    """select() reports a socket at end-of-file as readable.

    select(): "A descriptor shall be considered ready for reading when a
    call to an input function with O_NONBLOCK clear would not block,
    whether or not the function would transfer data successfully. (The
    function might return data, an end-of-file indication, or an error
    other than one indicating that it is blocked, and in each of these
    cases the descriptor shall be considered ready for reading.)"
    """
    srv, cli = _pair("tl-sele", "-m send -n 0", "-m selecteof -t 5")
    assert srv.startswith("rpsocktl: PASS"), srv
    assert cli.startswith("rpsocktl: PASS selecteof"), cli


def test_rpsock_select_listen() -> None:
    """select() reports a listening socket with a connection as readable.

    select(): "If the socket is currently listening, then it shall be
    marked as readable if an incoming connection request has been
    received, and a call to the accept() or accept4() function shall
    complete without blocking."
    """
    srv, cli = _pair("tl-sell", "-m select", "-m isconn")
    assert _fields(srv).get("sel") == "1", srv
    assert srv.startswith("rpsocktl: PASS"), srv


@pytest.mark.xfail(strict=True, reason=READV_BUG)
def test_rpsock_readv() -> None:
    """readv() scatters stream data over two buffers in order.

    readv(): "The readv() function shall place the input data into the
    iovcnt buffers specified by the members of the iov array: iov[0],
    iov[1], ..., iov[iovcnt-1]."
    """
    srv, cli = _pair("tl-rdv", "-m send -n 100", "-m readv -n 100")
    assert srv.startswith("rpsocktl: PASS"), srv
    assert cli.startswith("rpsocktl: PASS readv rx=100"), cli


def test_rpsock_peer_address() -> None:
    """getpeername() and accept() return the address of the other end.

    getpeername(): "The getpeername() function shall retrieve the peer
    address of the specified socket". accept(): "address Either a null
    pointer, or a pointer to a sockaddr structure where the address of
    the connecting socket shall be returned." The client's peer is the
    server's CPU and name; the server's peer is the client's CPU.
    """
    srv, cli = _pair("tl-peer", "", "-m peer")
    assert cli.startswith("rpsocktl: PASS peer"), cli
    got = _fields(srv)
    assert srv.startswith("rpsocktl: PASS"), srv
    assert got["peer"] == cpuname(1) and got["peer2"] == cpuname(1), srv


def test_rpsock_connect_twice() -> None:
    """connect() on a connected socket fails with EISCONN.

    connect(): "[EISCONN] The specified socket is connection-mode and is
    already connected."
    """
    srv, cli = _pair("tl-isc", "", "-m isconn")
    assert cli.startswith("rpsocktl: PASS isconn"), cli
    assert srv.startswith("rpsocktl: PASS"), srv


def test_rpsock_send_after_shutdown() -> None:
    """send() after shutdown(SHUT_WR) fails with EPIPE.

    send(): "[EPIPE] The socket is shut down for writing, or the socket
    is connection-mode and is no longer connected."
    """
    srv, cli = _pair("tl-epipe", "", "-m epipe")
    assert cli.startswith("rpsocktl: PASS epipe"), cli
    assert srv.startswith("rpsocktl: PASS"), srv


@pytest.mark.xfail(strict=True, reason=REFUSED_BUG)
def test_rpsock_connect_refused() -> None:
    """connect() to a name nobody listens on fails with ECONNREFUSED.

    connect(): "[ECONNREFUSED] The target address was not listening for
    connections or refused the connection request."
    """
    line = verdict(
        1, f"rpsocktl -c {cpuname(0)} -m refused -t 5 tl-nobody", CLIENT_RE
    )
    assert line.startswith("rpsocktl: PASS refused"), line


def test_rpsock_dgram_boundaries() -> None:
    """Each datagram is received whole, by one recv(), at its length.

    recv(): "For message-based sockets, such as SOCK_DGRAM and
    SOCK_SEQPACKET, the entire message shall be read in a single
    operation." Lengths 1 to 1000 bytes, each echoed by node 0.
    """
    srv, cli = _pair("tl-dgb", "", "-m bounds", udp=True)
    assert cli.startswith("rpsocktl: PASS bounds n=8"), cli
    assert srv.startswith("rpsocktl: PASS server mode=dgram rx=8"), srv


@pytest.mark.xfail(strict=True, reason=TRUNC_BUG)
def test_rpsock_dgram_truncate() -> None:
    """A datagram longer than the buffer is truncated and flagged.

    recvmsg(): "If a message is too long to fit in the supplied buffers,
    and MSG_PEEK is not set in the flags argument, the excess bytes shall
    be discarded, and MSG_TRUNC shall be set in the msg_flags member of
    the msghdr structure." A 200-byte datagram read into 50 bytes; the
    next datagram (30 bytes) is then received whole.
    """
    srv, cli = _pair("tl-dgt", "", "-m trunc", udp=True)
    assert cli.startswith("rpsocktl: PASS trunc"), cli
    assert srv.startswith("rpsocktl: PASS"), srv


def test_rpsock_dgram_too_long() -> None:
    """A 64 KiB datagram is sent whole or fails with EMSGSIZE.

    send(): "[EMSGSIZE] The message is too large to be sent all at once,
    as the socket requires." The socket then still carries a small
    datagram.
    """
    srv, cli = _pair("tl-dgm", "", "-m msgsize", udp=True)
    assert cli.startswith("rpsocktl: PASS msgsize"), cli
    assert srv.startswith("rpsocktl: PASS"), srv
