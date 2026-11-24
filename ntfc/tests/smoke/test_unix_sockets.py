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

"""AF_UNIX (local) socket tests on one node.

``unixtl <case>`` runs one case on the target and prints one verdict
line. Each case derives its expectations from POSIX.1-2024 (XSH
socket(), socketpair(), bind(), connect(), send(), sendmsg(), recv(),
recvmsg(), poll(), XBD ``<sys/socket.h>``); the quoted text is in
``apps/unixtl/unixtl_main.c``.
"""

import re

import pytest

#: bind() of a pathname bound by a socket of another type succeeds.
BIND_TYPE_BUG = (
    "net/local/local_conn.c:122: local_findconn() compares the name only "
    "with sockets of the same type (lc_proto), so bind() of a pathname "
    "bound by a socket of another type succeeds instead of failing with "
    "EADDRINUSE"
)

#: bind() of the pathname of an existing file succeeds.
BIND_FILE_BUG = (
    "net/local/local_bind.c:92: psock_local_bind() checks only the names "
    "of other local sockets, not the file system, so bind() of the "
    "pathname of an existing file succeeds instead of failing with "
    "EADDRINUSE"
)

#: connect() to a bound, non-listening stream socket fails with ENOENT.
CONNECT_REFUSED_BUG = (
    "net/local/local_connect.c:267: psock_local_connect() matches only "
    "listening sockets and then nx_stat()s the pathname "
    "(local_connect.c:292), which bind() does not create, so "
    "connect() to a bound socket that is not listening fails with ENOENT "
    "instead of ECONNREFUSED"
)

#: connect() to a pathname bound by another socket type fails with ENOENT.
CONNECT_TYPE_BUG = (
    "net/local/local_connect.c:268: psock_local_connect() matches only "
    "stream sockets and then nx_stat()s the pathname "
    "(local_connect.c:292), so connect() to a pathname bound by a socket "
    "of another type fails with ENOENT instead of EPROTOTYPE"
)

#: recvmsg() of a truncated datagram does not set MSG_TRUNC.
TRUNC_BUG = (
    "net/local/local_recvmsg.c:640: psock_dgram_recvfrom() discards the "
    "excess of a datagram larger than the buffer, but local_recvmsg() "
    "(local_recvmsg.c:723) never sets MSG_TRUNC in msg_flags"
)

#: recvmsg() of a datagram into more than one iovec fails with ENOTSUP.
SCATTER_BUG = (
    "net/local/local_recvmsg.c:719: local_recvmsg() fails recvmsg() of a "
    "datagram socket with more than one iovec with ENOTSUP"
)

#: recvmsg() with a short control buffer does not set MSG_CTRUNC.
CTRUNC_BUG = (
    "net/local/local_recvmsg.c:739: local_recvmsg() skips local_recvctl() "
    "when msg_controllen is not larger than a cmsghdr, and local_recvctl() "
    "(local_recvmsg.c:171) ignores a failed cmsg_append(); MSG_CTRUNC is "
    "never set"
)


def _xfail(case: str, reason: str) -> object:
    """Return a parameter for a case that fails because of a NuttX bug.

    :param case: unixtl case name
    :param reason: the bug
    :return: ``pytest.param`` with a strict xfail mark
    """
    return pytest.param(
        case, marks=pytest.mark.xfail(strict=True, reason=reason)
    )


pytestmark = [
    pytest.mark.dep_config(
        "CONFIG_NET_LOCAL_STREAM", "CONFIG_NET_LOCAL_DGRAM"
    ),
    pytest.mark.cmd_check("unixtl_main"),
]


def _unixtl(case: str) -> None:
    """Run one unixtl case and require its PASS verdict.

    :param case: unixtl case name
    """
    pattern = rf"unixtl: (PASS|FAIL) {case}\b[^\r\n]*"
    out = (
        pytest.product.core(0)
        .sendCommandReadUntilPattern(
            f"unixtl {case}", pattern=pattern, timeout=30
        )
        .output
    )
    found = re.search(pattern, out)
    assert found, out
    assert found.group(1) == "PASS", found.group(0)


@pytest.mark.xfail(strict=True, reason=TRUNC_BUG)
def test_unix_dgram_trunc() -> None:
    """recvmsg() of a truncated datagram sets MSG_TRUNC.

    recvmsg(): "If a message is too long to fit in the supplied buffers,
    and MSG_PEEK is not set in the flags argument, the excess bytes shall
    be discarded, and MSG_TRUNC shall be set in the msg_flags member of
    the msghdr structure."
    """
    _unixtl("dgram_trunc")


def test_unix_eof() -> None:
    """recv() of a socketpair() returns the data, then 0, after close().

    recv(): "If no messages are available to be received and the peer
    has performed an orderly shutdown, recv() shall return 0."
    """
    _unixtl("eof_pair")


@pytest.mark.parametrize("case", ["epipe_pair", "epipe_path"])
def test_unix_epipe(case: str) -> None:
    """send() to a closed peer fails with EPIPE; SIGPIPE unless NOSIGNAL.

    send(): EPIPE "the socket is connection-mode and is no longer
    connected"; SIGPIPE "is generated to the calling thread" when
    MSG_NOSIGNAL is not set; with MSG_NOSIGNAL "The [EPIPE] error shall
    still be returned."

    :param case: unixtl case name
    """
    _unixtl(case)


@pytest.mark.parametrize(
    "case",
    [
        _xfail("connect_refused", CONNECT_REFUSED_BUG),
        "connect_noent",
        _xfail("connect_prototype", CONNECT_TYPE_BUG),
    ],
)
def test_unix_connect_errors(case: str) -> None:
    """connect() errors: ECONNREFUSED, ENOENT, EPROTOTYPE.

    ECONNREFUSED "The target address was not listening for
    connections"; ENOENT "A component of the pathname does not name an
    existing file"; EPROTOTYPE "The specified address has a different
    type than the socket bound to the specified peer address."

    :param case: unixtl case name
    """
    _unixtl(case)


@pytest.mark.parametrize(
    "case",
    [
        _xfail("bind_inuse_type", BIND_TYPE_BUG),
        _xfail("bind_file", BIND_FILE_BUG),
    ],
)
def test_unix_bind_errors(case: str) -> None:
    """bind() of a pathname held by another socket type or a file.

    EADDRINUSE "The specified address is already in use", also for a
    pathname bound by a socket of another type and for "an existing
    file".

    :param case: unixtl case name
    """
    _unixtl(case)


def test_unix_poll() -> None:
    """poll() POLLIN/POLLOUT readiness of a stream socketpair().

    POLLIN "Data other than high-priority data may be read without
    blocking"; POLLOUT "Normal data may be written without blocking":
    clear after the socket is filled to EAGAIN, set after the peer read.
    """
    _unixtl("poll_stream")


@pytest.mark.parametrize(
    "case",
    [_xfail("iov_dgram_recv", SCATTER_BUG), "iov_stream"],
)
def test_unix_iov(case: str) -> None:
    """sendmsg()/recvmsg() with eight iovecs.

    A datagram received into eight iovecs fills them in turn; a stream
    carries eight iovecs.

    sendmsg(): "The data from each storage area indicated by msg_iov is
    sent in turn"; recvmsg(): "Each storage area indicated by msg_iov is
    filled with received data in turn".

    :param case: unixtl case name
    """
    _unixtl(case)


@pytest.mark.dep_config("CONFIG_NET_LOCAL_SCM")
@pytest.mark.xfail(strict=True, reason=CTRUNC_BUG)
def test_unix_scm_ctrunc() -> None:
    """recvmsg() with a short control buffer sets MSG_CTRUNC.

    MSG_CTRUNC "Control data was truncated."  The SCM_RIGHTS message
    finds room for a header but not for the descriptor.
    """
    _unixtl("scm_ctrunc")
