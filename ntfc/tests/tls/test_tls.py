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

"""Mbed TLS on NuttX (tlstl) against OpenSSL peers on the host.

The host generates a test PKI per session (:class:`_tls_common.Pki`)
and copies the CA, and for the node server its certificate and key, to
the node over TFTP.  Certificate validity is checked against the node
clock, set from the host over NTP (RFC 5280 6.1.3 (a)(2)).
"""

import concurrent.futures
import time
from typing import Any, Iterator

import pytest
from _net_common import NODE_IPS
from _tls_common import (
    BADCERT_CN_MISMATCH,
    BADCERT_EXPIRED,
    BADCERT_FUTURE,
    BADCERT_NOT_TRUSTED,
    CERT_VERIFY_FAILED,
    ECONNRESET,
    VERSION_NAMES,
    HeapCheck,
    Pki,
    PortInUse,
    SessionHung,
    TlsServer,
    client,
    core,
    host_client,
    netem,
    node_time,
    pin_shell,
    push,
    server_start,
    server_verdict,
    sync_clock,
    wait_conn_room,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_TESTLAB_TLSTL"),
    pytest.mark.cmd_check("tlstl_main"),
]

BRIDGE = "tl-br0"
NODE_FILES = ["ca.pem", "node.pem", "node.key"]

#: Free TCP connections each test starts with: more than the sessions
#: of any test but the concurrency and churn tests, which take over
#: TIME_WAIT connections on purpose
CONN_ROOM = 6

#: Bytes of the large interop transfers
BIG = 256 * 1024

#: Loss and reordering in both directions for the netem tests
NETEM = "delay 20ms 10ms loss 5% reorder 25% 50%"

#: Per-read timeout of a session under netem: 4 retransmissions of one
#: segment take 1.5 + 3 + 6 + 12 s with NuttX's 1.5 s initial RTO
NETEM_TIMEOUT = 30

FUTURE_ALERT_BUG = (
    "mbedtls 3.6.2 library/ssl_tls.c mbedtls_ssl_verify_certificate(): "
    "BADCERT_FUTURE has no alert mapping and falls through to "
    "certificate_unknown; RFC 8446 6.2 / RFC 5246 7.2.2 define "
    "certificate_expired as 'expired or is not currently valid'"
)


RST_BUG = (
    "net/tcp/tcp_monitor.c tcp_close_connection(): TCP_ABORT (peer RST) "
    "sets _SF_CLOSED like a graceful close, so recv() after poll() "
    "returns 0 (end of file) instead of failing with ECONNRESET"
)

CONNECT_RACE_BUG = (
    "net/tcp/tcp_connect.c psock_tcp_connect(): tcp_connect() puts the "
    "connection on the active list before the TCP_CONNECTED callback is "
    "registered under conn_dev_lock(); a device poll from another "
    "thread in between sends the SYN, the handshake completes unseen "
    "and connect() waits forever (since 5032377c346); in some runs only"
)

REUSEADDR_BUG = (
    "net/tcp/tcp_conn.c tcp_listener(): bind() treats a TIME_WAIT "
    "connection on the port as in use and never checks SO_REUSEADDR, so "
    "a restarted server gets EADDRINUSE for TCP_TIME_WAIT_TIMEOUT (2 min)"
)


@pytest.fixture(scope="module")
def pki() -> Iterator[Pki]:
    """Generate the test PKI around the host's current time.

    :return: test PKI
    """
    keys = Pki(time.time())
    yield keys
    keys.cleanup()


@pytest.fixture
def node(pki: Pki) -> Pki:
    """Set the node clock over NTP and copy the PKI files to the node.

    :param pki: test PKI
    :return: test PKI
    """
    sync_clock()
    pin_shell()
    push(pki, NODE_FILES)
    wait_conn_room(CONN_ROOM)
    return pki


@pytest.fixture
def heap(node: Pki) -> HeapCheck:
    """Return a heap snapshot taken once the PKI files are on the node.

    :param node: test PKI, copied to the node
    :return: snapshot, its ``check()`` ends the test
    """
    return HeapCheck()


@pytest.mark.parametrize("version", [12, 13])
def test_tls_client_interop(node: Pki, version: int) -> None:
    """Node client echoes 256 KiB through an OpenSSL server.

    The server allows one TLS version; the session ends with
    ``close_notify`` from both sides (RFC 8446 6.1).
    """
    with TlsServer(
        node.path("valid.pem"), node.path("valid.key"), version
    ) as srv:
        verdict = client(f"-n {BIG} -t 30", timeout=170)
    assert verdict.get("verdict") == "PASS", verdict
    assert verdict["ver"] == VERSION_NAMES[version]
    assert verdict["rx"] == BIG and verdict["end"] == "notify"
    assert srv.results == [
        {
            "version": VERSION_NAMES[version],
            "rx": BIG,
            "error": None,
            "notify": True,
        }
    ]


@pytest.mark.parametrize("version", [12, 13])
def test_tls_server_interop(node: Pki, version: int) -> None:
    """An OpenSSL client echoes 256 KiB through the node server.

    The client verifies the node certificate (``node.testlab``) against
    the test CA and allows one TLS version.
    """
    port = 5110 + version
    server_start(port, f"-n {BIG} -t 30")
    result = host_client(
        NODE_IPS[0], node.path("ca.pem"), BIG, version=version, port=port
    )
    verdict = server_verdict()
    assert result["error"] is None, result
    assert result["version"] == VERSION_NAMES[version]
    assert result["rx"] == BIG and not result["bad"] and result["notify"]
    assert verdict.get("verdict") == "PASS", verdict
    assert verdict["ver"] == VERSION_NAMES[version]


def test_tls_cert_clock_unset(pki: Pki) -> None:
    """A node without wall clock time rejects a currently valid cert.

    After a reboot, before NTP, the node's clock is far before the
    certificate's notBefore, so the certificate is not yet valid.
    """
    assert pytest.products[0].reboot(), "reboot failed"
    now = node_time()
    assert now is not None and now < pki.now - 2 * 86400, now
    push(pki, ["ca.pem"])
    with TlsServer(pki.path("valid.pem"), pki.path("valid.key")) as srv:
        verdict = client("-n 1024")
    assert verdict.get("verdict") == "FAIL", verdict
    assert verdict["ret"] == CERT_VERIFY_FAILED, verdict
    assert verdict["verify"] == BADCERT_FUTURE, verdict
    assert len(srv.results) == 1 and srv.results[0]["rx"] == 0


CERT_CASES = {
    "expired": BADCERT_EXPIRED,
    "future": BADCERT_FUTURE,
    "wronghost": BADCERT_CN_MISMATCH,
    "untrusted": BADCERT_NOT_TRUSTED,
}


@pytest.mark.parametrize("version", [12, 13])
@pytest.mark.parametrize("cert", list(CERT_CASES))
def test_tls_cert_reject(
    node: Pki, heap: HeapCheck, cert: str, version: int
) -> None:
    """The node client rejects a bad server certificate.

    The handshake fails with ``MBEDTLS_ERR_X509_CERT_VERIFY_FAILED`` and
    the verification flags name exactly the defect; no application data
    is sent.
    """
    with TlsServer(
        node.path(f"{cert}.pem"), node.path(f"{cert}.key"), version
    ) as srv:
        verdict = client("-n 1024")
    assert verdict.get("verdict") == "FAIL", verdict
    assert verdict["ret"] == CERT_VERIFY_FAILED, verdict
    assert verdict["verify"] == CERT_CASES[cert], verdict
    assert verdict["tx"] == 0
    assert len(srv.results) == 1 and srv.results[0]["rx"] == 0
    heap.check()


ALERTS = {
    "expired": "CERTIFICATE_EXPIRED",
    "future": "CERTIFICATE_EXPIRED",
    "untrusted": "UNKNOWN_CA",
}


@pytest.mark.parametrize("cert", list(ALERTS))
def test_tls_cert_alert(
    request: pytest.FixtureRequest, node: Pki, cert: str
) -> None:
    """The node client sends the alert that names the certificate defect.

    RFC 8446 6.2: ``certificate_expired`` for a certificate that "has
    expired or is not currently valid", ``unknown_ca`` for a chain that
    does not end at a trust anchor.
    """
    if cert == "future":
        request.applymarker(
            pytest.mark.xfail(
                strict=True, raises=WrongAlert, reason=FUTURE_ALERT_BUG
            )
        )
    with TlsServer(node.path(f"{cert}.pem"), node.path(f"{cert}.key")) as srv:
        verdict = client("-n 1024")
    assert verdict.get("verdict") == "FAIL", verdict
    assert len(srv.results) == 1, srv.results
    if ALERTS[cert] not in str(srv.results[0]["error"]):
        raise WrongAlert(str(srv.results))


class ResetNotReported(AssertionError):
    """A peer's RST did not end the session with ``ECONNRESET``."""


class WrongAlert(AssertionError):
    """The node sent a different alert than RFC 8446 6.2 names."""


@pytest.mark.xfail(strict=True, raises=ResetNotReported, reason=RST_BUG)
@pytest.mark.parametrize("role", ["client", "server"])
def test_tls_peer_reset(
    net_leak: Any, node: Pki, heap: HeapCheck, role: str
) -> None:
    """A TCP RST from the peer in the middle of a transfer is an error.

    The node reports the reset (``ECONNRESET``, POSIX ``recv()``: "A
    connection was forcibly closed by a peer") instead of hanging or a
    clean end, frees its memory and gives back every IOB.
    """
    if role == "client":
        with TlsServer(
            node.path("valid.pem"), node.path("valid.key"), mode="reset",
            count=BIG // 2,
        ):  # fmt: skip
            verdict = client(f"-R -n {BIG}")
    else:
        server_start(5121, f"-n {BIG}")
        host_client(
            NODE_IPS[0], node.path("ca.pem"), BIG, end="reset", port=5121
        )
        verdict = server_verdict()
    assert verdict.get("verdict") == "FAIL", verdict
    heap.check()
    net_leak.check()
    if verdict["end"] != "reset" or verdict["errno"] != ECONNRESET:
        raise ResetNotReported(verdict["line"])


@pytest.mark.parametrize("role", ["client", "server"])
def test_tls_truncation(node: Pki, heap: HeapCheck, role: str) -> None:
    """A TCP FIN without ``close_notify`` is reported as truncation.

    RFC 8446 6.1: each party MUST send ``close_notify`` before closing
    its write side; an end of stream without it must not be taken for
    the end of the data (RFC 5246 7.2.1: truncation attack).
    """
    if role == "client":
        with TlsServer(
            node.path("valid.pem"), node.path("valid.key"), mode="fin",
            count=BIG // 2,
        ):  # fmt: skip
            verdict = client(f"-R -n {BIG // 2}")
        assert verdict.get("rx") == BIG // 2, verdict
    else:
        server_start(5122, f"-n {BIG // 2}")
        result = host_client(
            NODE_IPS[0], node.path("ca.pem"), BIG, end="fin", port=5122
        )
        verdict = server_verdict()
        assert result["rx"] == BIG // 2, result
    assert verdict.get("verdict") == "FAIL", verdict
    assert verdict["end"] == "eof", verdict
    heap.check()


@pytest.mark.parametrize("version", [12, 13])
@pytest.mark.parametrize("role", ["client", "server"])
def test_tls_handshake_lossy(node: Pki, role: str, version: int) -> None:
    """Handshakes complete over a link with loss and reordering.

    netem drops 5% and reorders 25% of the frames in both directions;
    TCP retransmission must recover every handshake and transfer: 5
    sessions of 16 KiB, each read within ``NETEM_TIMEOUT``.
    """
    sessions = 5
    count = 16 * 1024
    deadline = sessions * NETEM_TIMEOUT
    with netem(BRIDGE, NETEM):
        start = time.monotonic()
        if role == "client":
            with TlsServer(
                node.path("valid.pem"), node.path("valid.key"), version
            ) as srv:
                verdict = client(
                    f"-n {count} -r {sessions} -t {NETEM_TIMEOUT}",
                    timeout=170,
                )
            assert all(r["error"] is None for r in srv.results), srv.results
        else:
            port = 5130 + version
            server_start(port, f"-n {count} -r {sessions} -t {NETEM_TIMEOUT}")
            for _ in range(sessions):
                result = host_client(
                    NODE_IPS[0],
                    node.path("ca.pem"),
                    count,
                    version=version,
                    port=port,
                )
                assert result["error"] is None, result
                assert result["rx"] == count and not result["bad"], result
                assert result["notify"], result
            verdict = server_verdict(timeout=60)
        elapsed = time.monotonic() - start
    assert verdict.get("verdict") == "PASS", verdict
    assert verdict["ok"] == sessions
    assert verdict["ver"] == VERSION_NAMES[version]
    assert elapsed < deadline, f"{elapsed:.1f} s"


@pytest.mark.parametrize("role", ["client", "server"])
def test_tls_concurrent_sessions(
    request: pytest.FixtureRequest,
    net_leak: Any,
    node: Pki,
    heap: HeapCheck,
    role: str,
) -> None:
    """4 concurrent sessions, 16 in total, each echoing 32 KiB.

    Every session ends cleanly, and the node frees its memory and gives
    back every IOB afterwards.
    """
    sessions = 16
    count = 32 * 1024
    if role == "client":
        request.applymarker(
            pytest.mark.xfail(
                strict=False,
                raises=SessionHung,
                reason=CONNECT_RACE_BUG,
            )
        )
        with TlsServer(node.path("valid.pem"), node.path("valid.key")) as srv:
            verdict = client(
                f"-n {count} -r {sessions} -N 4 -t 30", timeout=150
            )
        assert len(srv.results) == sessions
        assert all(r["notify"] for r in srv.results), srv.results
    else:
        server_start(5141, f"-n {count} -r {sessions} -N 4 -t 30")
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(
                pool.map(
                    lambda _: host_client(
                        NODE_IPS[0], node.path("ca.pem"), count, port=5141
                    ),
                    range(sessions),
                )
            )
        verdict = server_verdict(timeout=120)
        assert all(
            r["error"] is None
            and r["rx"] == count
            and not r["bad"]
            and r["notify"]
            for r in results
        ), results
    assert verdict.get("verdict") == "PASS", verdict
    assert verdict["ok"] == sessions
    heap.check()
    net_leak.check()


@pytest.mark.xfail(strict=True, raises=PortInUse, reason=REUSEADDR_BUG)
def test_tls_server_restart(node: Pki) -> None:
    """A TLS server restarted at once binds its port again.

    The node closes its finished session first, leaving it in TIME_WAIT;
    a new server with ``SO_REUSEADDR`` must still bind the port.
    """
    port = 5151
    for _ in range(2):
        server_start(port, "-n 1024")
        result = host_client(NODE_IPS[0], node.path("ca.pem"), 1024, port=port)
        verdict = server_verdict()
        assert result["error"] is None and result["notify"], result
        assert verdict.get("verdict") == "PASS", verdict


def test_tls_session_churn(net_leak: Any, node: Pki, heap: HeapCheck) -> None:
    """40 back-to-back short sessions leak no memory and no IOBs."""
    sessions = 40
    with TlsServer(node.path("valid.pem"), node.path("valid.key")) as srv:
        verdict = client(f"-n 1024 -r {sessions}", timeout=170)
    assert verdict.get("verdict") == "PASS", verdict
    assert verdict["ok"] == sessions and len(srv.results) == sessions
    heap.check()
    net_leak.check()
    assert core().sendCommand("echo alive", "alive", timeout=10) == 0
