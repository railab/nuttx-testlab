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

"""Host side of the TLS tests: test PKI, OpenSSL peers, tlstl helpers.

The host peers use Python's ``ssl`` module (OpenSSL), which gives each
test control over how a connection ends: ``close_notify``, a TCP FIN
without it, or a TCP RST.
"""

import contextlib
import datetime
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, Iterator, List, Optional, Type

import pytest
from _host_services import SntpServer, TftpServer
from _net_common import HOST_IP, pat, tcp_free

TLS_PORT = 5101
WORKDIR = "/tl"
HOST_NAME = "tls.testlab"
NODE_NAME = "node.testlab"
CHUNK = 4096
DAY = 86400

#: ``ssl`` versions by tlstl ``-V`` value
VERSIONS = {
    12: ssl.TLSVersion.TLSv1_2,
    13: ssl.TLSVersion.TLSv1_3,
}

VERSION_NAMES = {12: "TLSv1.2", 13: "TLSv1.3"}

VERDICT_RE = (
    r"tlstl: (PASS|FAIL) ver=(\S+) sessions=(\d+)/(\d+) tx=(\d+) rx=(\d+) "
    r"end=(\w+) ret=-0x([0-9a-f]+) verify=0x([0-9a-f]+) errno=(\d+)[\r\n]"
)

#: mbedtls ``MBEDTLS_ERR_X509_CERT_VERIFY_FAILED``
CERT_VERIFY_FAILED = 0x2700

#: mbedtls ``MBEDTLS_X509_BADCERT_*`` verification flags
BADCERT_EXPIRED = 0x01
BADCERT_CN_MISMATCH = 0x04
BADCERT_NOT_TRUSTED = 0x08
BADCERT_FUTURE = 0x0200

ECONNRESET = 104
EADDRINUSE = 98

_CA_CONF = """\
[ca]
default_ca = tl

[tl]
dir = {dir}
database = {dir}/index.txt
new_certs_dir = {dir}
serial = {dir}/serial
default_md = sha256
policy = pol
unique_subject = no
copy_extensions = none
email_in_dn = no

[pol]
commonName = supplied

[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash

[v3_leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:${{ENV::TL_SAN}}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def _asn1_time(when: float) -> str:
    """Return an ``openssl ca`` date.

    :param when: seconds since 1970
    :return: ``YYYYMMDDHHMMSSZ`` (UTC)
    """
    stamp = datetime.datetime.fromtimestamp(when, datetime.timezone.utc)
    return stamp.strftime("%Y%m%d%H%M%SZ")


class Pki:
    """Test CA, rogue CA and leaf certificates, valid relative to ``now``.

    ``files`` maps a file name to its PEM content: ``ca.pem`` (trusted
    CA), leaf certificates ``<name>.pem`` with keys ``<name>.key`` for
    ``valid``, ``expired`` (ended 1 day ago), ``future`` (starts in 1
    day) and ``wronghost`` (SAN ``other.testlab``), all for
    ``tls.testlab`` and signed by the CA; ``untrusted`` (signed by a CA
    the node does not know) and ``node`` (``node.testlab``, the node's
    server certificate).
    """

    def __init__(self, now: float) -> None:
        """Generate every key and certificate with the openssl tool.

        :param now: reference time, seconds since 1970
        """
        self._tmp = tempfile.TemporaryDirectory(prefix="tl-pki-")
        self.dir = Path(self._tmp.name)
        self.now = now
        self._ca("ca", "testlab-ca")
        self._ca("rogue", "rogue-ca")
        self._leaf("valid", "ca", HOST_NAME, now - DAY, now + 365 * DAY)
        self._leaf("expired", "ca", HOST_NAME, now - 730 * DAY, now - DAY)
        self._leaf("future", "ca", HOST_NAME, now + DAY, now + 730 * DAY)
        self._leaf(
            "wronghost", "ca", "other.testlab", now - DAY, now + 365 * DAY
        )
        self._leaf("untrusted", "rogue", HOST_NAME, now - DAY, now + 365 * DAY)
        self._leaf("node", "ca", NODE_NAME, now - DAY, now + 365 * DAY)
        self.files = {
            p.name: p.read_bytes()
            for p in self.dir.glob("*")
            if p.suffix in (".pem", ".key")
        }

    def path(self, name: str) -> str:
        """Return the host path of a PKI file.

        :param name: file name, e.g. ``valid.pem``
        :return: absolute path
        """
        return str(self.dir / name)

    def _openssl(
        self, *args: str, env: Optional[Dict[str, str]] = None
    ) -> None:
        """Run the openssl tool in the PKI directory.

        :param args: openssl arguments
        :param env: extra environment
        """
        subprocess.run(  # noqa: S603
            ["openssl", *args],  # noqa: S607
            cwd=self.dir,
            env={"PATH": "/usr/bin:/bin", **(env or {})},
            capture_output=True,
            check=True,
        )

    def _key(self, name: str) -> None:
        """Generate a P-256 key.

        :param name: base name
        """
        self._openssl(
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            f"{name}.key",
        )

    def _sign(
        self,
        name: str,
        issuer: str,
        start: float,
        end: float,
        ext: str,
        san: str = "",
    ) -> None:
        """Sign ``<name>.csr`` with an issuer's CA database.

        :param name: base name of the CSR and the certificate
        :param issuer: base name of the issuing CA (itself: self-signed)
        :param start: notBefore, seconds since 1970
        :param end: notAfter, seconds since 1970
        :param ext: extension section of the CA configuration
        :param san: DNS subject alternative name (leaf certificates)
        """
        cadir = self.dir / f"{issuer}.db"
        args = [
            "ca",
            "-batch",
            "-notext",
            "-config",
            str(cadir / "ca.cnf"),
            "-in",
            f"{name}.csr",
            "-out",
            f"{name}.pem",
            "-startdate",
            _asn1_time(start),
            "-enddate",
            _asn1_time(end),
            "-extensions",
            ext,
            "-keyfile",
            f"{issuer}.key",
        ]
        if name == issuer:
            args.append("-selfsign")
        else:
            args += ["-cert", f"{issuer}.pem"]
        self._openssl(*args, env={"TL_SAN": san or "unused"})

    def _ca(self, name: str, cn: str) -> None:
        """Create a self-signed CA valid 10 years around ``now``.

        :param name: base name
        :param cn: subject common name
        """
        cadir = self.dir / f"{name}.db"
        cadir.mkdir()
        (cadir / "index.txt").write_text("")
        (cadir / "serial").write_text("1000\n")
        (cadir / "ca.cnf").write_text(_CA_CONF.format(dir=cadir))
        self._key(name)
        self._openssl(
            "req", "-new", "-key", f"{name}.key", "-subj", f"/CN={cn}",
            "-out", f"{name}.csr",
        )  # fmt: skip
        span = 3650 * DAY
        self._sign(name, name, self.now - span, self.now + span, "v3_ca")

    def _leaf(
        self, name: str, issuer: str, host: str, start: float, end: float
    ) -> None:
        """Create a server certificate for ``host``.

        :param name: base name
        :param issuer: base name of the issuing CA
        :param host: subject common name and DNS SAN
        :param start: notBefore, seconds since 1970
        :param end: notAfter, seconds since 1970
        """
        self._key(name)
        self._openssl(
            "req", "-new", "-key", f"{name}.key", "-subj", f"/CN={host}",
            "-out", f"{name}.csr",
        )  # fmt: skip
        self._sign(name, issuer, start, end, "v3_leaf", host)

    def cleanup(self) -> None:
        """Remove the PKI directory."""
        self._tmp.cleanup()


def _drain(sock: Any) -> None:
    """Wait until the peer closes, discarding what it sends.

    :param sock: socket (TLS or plain)
    """
    with contextlib.suppress(OSError, ssl.SSLError):
        while sock.recv(CHUNK):
            pass


def _reset(sock: socket.socket) -> None:
    """Close a socket with a TCP RST instead of a FIN.

    :param sock: socket (a TLS socket closes without ``close_notify``)
    """
    sock.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
    )
    sock.close()


def _fin(sock: ssl.SSLSocket) -> None:
    """Close the write side with a TCP FIN without ``close_notify``.

    ``SSLSocket.shutdown()`` drops the TLS layer without sending an
    alert; then wait for the peer to close.

    :param sock: TLS socket
    """
    sock.shutdown(socket.SHUT_WR)
    _drain(sock)
    sock.close()


class TlsServer:
    """Host TLS server: one thread per connection, results per connection.

    Modes, per connection:

    - ``echo``: echo until the peer's ``close_notify``, answer it.
    - ``send``: send ``count`` pattern bytes, then ``close_notify``.
    - ``reset``: send ``count`` pattern bytes, wait, then TCP RST.
    - ``fin``: send ``count`` pattern bytes, then TCP FIN without
      ``close_notify``.

    ``results`` holds one dict per connection: ``version``, ``rx``,
    ``error`` (the ``ssl`` error reason, e.g. a received alert) and
    ``notify`` (the peer sent ``close_notify``).
    """

    def __init__(
        self,
        cert: str,
        key: str,
        version: Optional[int] = None,
        mode: str = "echo",
        count: int = 0,
        port: int = TLS_PORT,
    ) -> None:
        """Bind the server.

        :param cert: certificate file
        :param key: key file
        :param version: 12 or 13 to allow only that TLS version
        :param mode: connection behaviour, see the class description
        :param count: bytes to send in the sending modes
        :param port: TCP port
        """
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        if version:
            self.ctx.minimum_version = VERSIONS[version]
            self.ctx.maximum_version = VERSIONS[version]
        self.mode = mode
        self.count = count
        self.results: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST_IP, port))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "TlsServer":
        """Start serving.

        :return: this server
        """
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Stop accepting, wait for open connections, close the socket.

        :param exc_type: exception type, if any
        :param exc: exception, if any
        :param tb: traceback, if any
        """
        self._stop.set()
        self._thread.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=30)
        self.sock.close()

    def _loop(self) -> None:
        """Accept connections, one handler thread each."""
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(
                target=self._serve, args=(conn,), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _serve(self, conn: socket.socket) -> None:
        """Handle one connection and record its result.

        :param conn: accepted TCP connection
        """
        result: Dict[str, Any] = {
            "version": None,
            "rx": 0,
            "error": None,
            "notify": False,
        }
        conn.settimeout(60)
        try:
            tls = self.ctx.wrap_socket(
                conn, server_side=True, suppress_ragged_eofs=False
            )
            result["version"] = tls.version()
            getattr(self, f"_{self.mode}")(tls, result)
        except (OSError, ssl.SSLError) as err:
            result["error"] = getattr(err, "reason", None) or repr(err)
            conn.close()
        with self.lock:
            self.results.append(result)

    def _echo(self, tls: ssl.SSLSocket, result: Dict[str, Any]) -> None:
        """Echo until ``close_notify``, then answer it.

        :param tls: TLS socket
        :param result: connection result
        """
        while True:
            data = tls.recv(16384)
            if not data:
                break
            result["rx"] += len(data)
            tls.sendall(data)
        result["notify"] = True
        tls.unwrap().close()

    def _send_pattern(self, tls: ssl.SSLSocket) -> None:
        """Send ``count`` bytes of the nettl pattern.

        :param tls: TLS socket
        """
        for off in range(0, self.count, CHUNK):
            tls.sendall(pat(off, min(CHUNK, self.count - off)))

    def _send(self, tls: ssl.SSLSocket, result: Dict[str, Any]) -> None:
        """Send the pattern, then ``close_notify``; wait for the peer's.

        :param tls: TLS socket
        :param result: connection result
        """
        self._send_pattern(tls)
        tls.unwrap().close()
        result["notify"] = True

    def _reset(self, tls: ssl.SSLSocket, result: Dict[str, Any]) -> None:
        """Send the pattern, let it arrive, then reset the connection.

        :param tls: TLS socket
        :param result: connection result
        """
        self._send_pattern(tls)
        time.sleep(1)
        _reset(tls)

    def _fin(self, tls: ssl.SSLSocket, result: Dict[str, Any]) -> None:
        """Send the pattern, then a FIN without ``close_notify``.

        :param tls: TLS socket
        :param result: connection result
        """
        self._send_pattern(tls)
        time.sleep(1)
        _fin(tls)


def host_client(
    addr: str,
    cafile: str,
    count: int,
    version: Optional[int] = None,
    end: str = "notify",
    port: int = TLS_PORT,
) -> Dict[str, Any]:
    """Run a TLS echo session from the host against a node server.

    :param addr: node address
    :param cafile: CA certificate verifying the node
    :param count: bytes to echo; ``reset``/``fin`` stop after half
    :param version: 12 or 13 to allow only that TLS version
    :param end: ``notify`` (``close_notify``), ``reset`` (TCP RST) or
     ``fin`` (TCP FIN without ``close_notify``)
    :param port: node port
    :return: ``version``, ``rx`` (verified echo bytes), ``bad`` (echo
     mismatch), ``notify`` (node answered ``close_notify``), ``error``
    """
    ctx = ssl.create_default_context(cafile=cafile)
    if version:
        ctx.minimum_version = VERSIONS[version]
        ctx.maximum_version = VERSIONS[version]
    result: Dict[str, Any] = {
        "version": None,
        "rx": 0,
        "bad": False,
        "notify": False,
        "error": None,
    }
    total = count if end == "notify" else count // 2
    try:
        sock = socket.create_connection((addr, port), timeout=60)
        tls = ctx.wrap_socket(
            sock, server_hostname=NODE_NAME, suppress_ragged_eofs=False
        )
        result["version"] = tls.version()
        for off in range(0, total, CHUNK):
            chunk = pat(off, min(CHUNK, total - off))
            tls.sendall(chunk)
            echo = b""
            while len(echo) < len(chunk):
                data = tls.recv(len(chunk) - len(echo))
                if not data:
                    raise ConnectionError("echo ended early")
                echo += data
            result["bad"] = result["bad"] or echo != chunk
            result["rx"] += len(echo)
        if end == "reset":
            _reset(tls)
        elif end == "fin":
            _fin(tls)
        else:
            # The node answers close_notify and closes first: wait for
            # its FIN, so the node holds the connection in TIME_WAIT
            sock = tls.unwrap()
            result["notify"] = True
            _drain(sock)
            sock.close()
    except (OSError, ssl.SSLError) as err:
        result["error"] = getattr(err, "reason", None) or repr(err)
    return result


@contextlib.contextmanager
def netem(dev: str, spec: str) -> Iterator[None]:
    """Apply a netem qdisc to both directions of a host bridge.

    Host to node: the bridge's own root qdisc.  Node to host: frames
    the bridge passes up to the host go through its ingress hook, which
    redirects them to an ``ifb`` device with the same netem qdisc.

    :param dev: bridge, e.g. ``tl-br0``
    :param spec: netem arguments, e.g. ``delay 10ms loss 5%``
    """
    ifb = "tl-ifb0"

    def run(*args: str, check: bool = True) -> None:
        subprocess.run(args, capture_output=True, check=check)  # noqa: S603

    run("modprobe", "ifb", "numifbs=0", check=False)
    run("ip", "link", "del", ifb, check=False)
    run("tc", "qdisc", "del", "dev", dev, "root", check=False)
    run("tc", "qdisc", "del", "dev", dev, "ingress", check=False)
    run("ip", "link", "add", ifb, "type", "ifb")
    try:
        run("ip", "link", "set", ifb, "up")
        run("tc", "qdisc", "add", "dev", dev, "root", "netem", *spec.split())
        run("tc", "qdisc", "add", "dev", dev, "handle", "ffff:", "ingress")
        run(
            "tc", "filter", "add", "dev", dev, "parent", "ffff:",
            "matchall", "action", "mirred", "egress", "redirect",
            "dev", ifb,
        )  # fmt: skip
        run("tc", "qdisc", "add", "dev", ifb, "root", "netem", *spec.split())
        yield
    finally:
        run("tc", "qdisc", "del", "dev", dev, "root", check=False)
        run("tc", "qdisc", "del", "dev", dev, "ingress", check=False)
        run("ip", "link", "del", ifb, check=False)


def core(node: int = 0) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def mount_workdir() -> None:
    """Mount a tmpfs scratch directory on node 0 if it is not mounted."""
    if core().sendCommand("mount", WORKDIR, timeout=10) != 0:
        core().sendCommand(f"mount -t tmpfs {WORKDIR}", timeout=10)
        assert core().sendCommand("mount", WORKDIR, timeout=10) == 0


def push(pki: Pki, names: List[str]) -> None:
    """Copy PKI files to the node work directory over TFTP.

    :param pki: test PKI
    :param names: file names
    """
    mount_workdir()
    with TftpServer(HOST_IP, pki.files):
        for name in names:
            ret = core().sendCommand(
                f"get -b -f {WORKDIR}/{name} -h {HOST_IP} {name}",
                timeout=30,
            )
            assert ret == 0, f"tftp get {name} failed"


_DATE_RE = re.compile(
    r"\w{3}, (\w{3}) (\d+) (\d\d):(\d\d):(\d\d) (\d{4})", re.ASCII
)


def node_time() -> Optional[float]:
    """Return node 0's wall clock time.

    :return: seconds since 1970 (UTC), None when ``date`` prints no time
    """
    ret = core().sendCommandReadUntilPattern(
        "date", pattern=r"nsh> ", timeout=10
    )
    found = _DATE_RE.search(str(ret.output))
    if not found:
        return None
    mon, day, hour, mins, sec, year = found.groups()
    stamp = datetime.datetime.strptime(
        f"{year} {mon} {day} {hour}:{mins}:{sec}", "%Y %b %d %H:%M:%S"
    ).replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def sync_clock(tolerance: float = 60) -> None:
    """Set node 0's clock to the host's time over NTP.

    :param tolerance: largest accepted difference, seconds
    """
    now = node_time()
    if now is not None and abs(now - time.time()) < tolerance:
        return
    core().sendCommand("ntpcstop", timeout=10)
    with SntpServer(HOST_IP, int(time.time())):
        assert core().sendCommand("ntpcstart", timeout=10) == 0
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                now = node_time()
                if now is not None and abs(now - time.time()) < tolerance:
                    break
                time.sleep(1)
        finally:
            core().sendCommand("ntpcstop", timeout=10)
    assert (
        now is not None and abs(now - time.time()) < tolerance
    ), f"node clock not set: {now}"


def _parse(out: str) -> Dict[str, Any]:
    """Parse a tlstl verdict line.

    :param out: console output
    :return: verdict fields, empty when there is no verdict line
    """
    found = re.search(VERDICT_RE, out)
    if not found:
        return {}
    keys = (
        "verdict", "ver", "ok", "sessions", "tx", "rx", "end", "ret",
        "verify", "errno",
    )  # fmt: skip
    vals: Dict[str, Any] = dict(zip(keys, found.groups()))
    for key in ("ok", "sessions", "tx", "rx", "errno"):
        vals[key] = int(vals[key])
    vals["ret"] = int(vals["ret"], 16)
    vals["verify"] = int(vals["verify"], 16)
    vals["line"] = found.group(0).rstrip("\r\n")
    return vals


class SessionHung(AssertionError):
    """tlstl printed no verdict in time; the node was rebooted."""


def client(args: str, timeout: int = 120) -> Dict[str, Any]:
    """Run tlstl as client on node 0 against the host.

    :param args: tlstl options after ``-c <host> -A <ca> -H <name>``
    :param timeout: seconds to wait for the verdict, below NTFC's 180 s
     busy loop limit
    :return: parsed verdict, see :func:`_parse`
    :raises SessionHung: no verdict within ``timeout``
    """
    ret = core().sendCommandReadUntilPattern(
        f"tlstl -c {HOST_IP} -A {WORKDIR}/ca.pem -H {HOST_NAME} {args}",
        pattern=VERDICT_RE,
        timeout=timeout,
    )
    verdict = _parse(str(ret.output))
    if not verdict:
        assert pytest.products[0].reboot(), "reboot failed"
        raise SessionHung(f"no verdict in {timeout} s: {ret.output}")
    return verdict


class PortInUse(AssertionError):
    """The tlstl server could not bind its port (``EADDRINUSE``)."""


def server_start(port: int, args: str = "") -> None:
    """Start a background tlstl server with the node certificate.

    :param port: listen port
    :param args: extra tlstl options
    :raises PortInUse: bind() failed with ``EADDRINUSE``
    """
    ret = core().sendCommandReadUntilPattern(
        f"tlstl -s -C {WORKDIR}/node.pem -K {WORKDIR}/node.key -p {port} "
        f"{args} &",
        pattern=r"tlstl: (listening|listen failed: \d+)",
        timeout=30,
    )
    out = str(ret.output)
    if f"listen failed: {EADDRINUSE}" in out:
        raise PortInUse(f"port {port}: {out}")
    assert "tlstl: listening" in out, f"tlstl server did not start: {out}"


def server_verdict(timeout: int = 60) -> Dict[str, Any]:
    """Wait for the verdict of the background tlstl server.

    :param timeout: seconds to wait
    :return: parsed verdict, see :func:`_parse`
    """
    ret = core().readUntilPattern(VERDICT_RE, timeout=timeout)
    return _parse(str(ret.output))


_FREE_RE = re.compile(r"^\s*\d+\s+(\d+)\s+\d+\s+\d+\s+\d+.*Umem", re.M)

#: Seconds an exited task may take to release its memory
HEAP_SETTLE_S = 10.0


def _ncpus() -> int:
    """Return the CPU count of an SMP node.

    :return: ``CONFIG_SMP_NCPUS``, 1 without ``CONFIG_SMP``
    """
    if not core().conf.kv_check("CONFIG_SMP"):
        return 1
    return int(core().conf.kv_check("CONFIG_SMP_NCPUS") or 1)


def pin_shell() -> None:
    """Keep the node shell, and so every program it starts, on CPU 0.

    Only on SMP nodes.  A task's stack is freed while the task switches
    out, onto a delay list of its CPU, which only that CPU empties, at
    its next allocation (``free`` runs ``mallinfo()``, which empties the
    list of the calling CPU only).  With all programs on the shell's CPU,
    ``free`` sees every block they freed as free.
    """
    if _ncpus() == 1:
        return
    ret = core().sendCommandReadUntilPattern(
        "ps", pattern=r"nsh> ", timeout=10
    )
    found = re.search(
        r"^\s*(\d+)\s.*\sRunning\s.*\s(nsh_main|sh)\s*$",
        str(ret.output),
        re.M,
    )
    assert found, f"no running shell: {ret.output}"
    ret = core().sendCommandReadUntilPattern(
        f"taskset -p 1 {found.group(1)}", pattern=r"nsh> ", timeout=10
    )
    assert "mask: 0x1" in str(ret.output), str(ret.output)


def heap_used() -> int:
    """Return the bytes allocated in node 0's user heap.

    :return: ``used`` column of the ``Umem`` row of ``free``
    """
    ret = core().sendCommandReadUntilPattern(
        "free", pattern=r"Umem[^\n]*\n", timeout=10
    )
    found = _FREE_RE.search(str(ret.output))
    assert found, f"no Umem row: {ret.output}"
    return int(found.group(1))


class HeapCheck:
    """``free`` snapshot taken before a test; ``check()`` compares."""

    def __init__(self) -> None:
        """Take the snapshot."""
        self.before = heap_used()

    def check(self) -> None:
        """Fail when the heap holds more than before the test.

        An exited task releases its memory after its parent is woken up,
        so wait up to ``HEAP_SETTLE_S`` for the count to come back.
        """
        deadline = time.monotonic() + HEAP_SETTLE_S
        after = heap_used()
        while after > self.before and time.monotonic() < deadline:
            time.sleep(1)
            after = heap_used()
        assert (
            after <= self.before
        ), f"heap grew by {after - self.before} bytes"


def wait_conn_room(room: int, timeout: float = 130) -> None:
    """Wait until node 0 has ``room`` free TCP connections.

    Connections a test closed first stay in TIME_WAIT for 2 minutes;
    waiting keeps the next test from taking over one of them.

    :param room: free connections needed
    :param timeout: seconds to wait
    """
    deadline = time.monotonic() + timeout
    while tcp_free(0) < room and time.monotonic() < deadline:
        time.sleep(5)
