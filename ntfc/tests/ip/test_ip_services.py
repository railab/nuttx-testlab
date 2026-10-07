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

"""NuttX network services and clients against Linux host peers."""

import ftplib
import hashlib
import io
import socket
import time
from typing import Any

import pytest
from _host_services import DnsServer, HttpServer, SntpServer, TftpServer
from _net_common import HOST_IP, NODE_IPS, pattern

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_TCP", "CONFIG_NET_UDP"),
    pytest.mark.cmd_check("cmd_md5"),
]

NODE = 0
WORKDIR = "/tl"
HTTP_PORT = 8080

# 2030-01-01 00:00:00 UTC

NTP_TIME = 1893456000


def _core() -> Any:
    """Return the NTFC core handler of the node under test.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.products[NODE].core(0)


def _md5_on_node(path: str, expected: bytes) -> bool:
    """Check a node file against expected content with nsh ``md5``.

    :param path: file path on the node
    :param expected: expected content
    :return: True when the node digest matches
    """
    digest = hashlib.md5(expected).hexdigest()  # noqa: S324
    ret = _core().sendCommand(f"md5 -f {path}", digest, timeout=30)
    return ret == 0


@pytest.fixture(autouse=True)
def workdir() -> None:
    """Mount a tmpfs scratch directory on the node.

    Mounted per test: a node rebooted after a failure loses it.
    """
    if _core().sendCommand("mount", WORKDIR, timeout=10) != 0:
        _core().sendCommand(f"mount -t tmpfs {WORKDIR}", timeout=10)
        assert _core().sendCommand("mount", WORKDIR, timeout=10) == 0


@pytest.mark.cmd_check("cmd_wget")
@pytest.mark.dep_config("CONFIG_NETUTILS_WEBCLIENT")
def test_wget_from_host() -> None:
    """Node downloads a file from a host HTTP server intact."""
    blob = pattern(0, 65536 + 123)
    with HttpServer(HOST_IP, {"/blob.bin": blob}, HTTP_PORT):
        ret = _core().sendCommand(
            f"wget -o {WORKDIR}/http.bin "
            f"http://{HOST_IP}:{HTTP_PORT}/blob.bin",
            timeout=60,
        )
        assert ret == 0
    assert _md5_on_node(f"{WORKDIR}/http.bin", blob)


@pytest.mark.cmd_check("cmd_get")
@pytest.mark.dep_config("CONFIG_NETUTILS_TFTPC")
def test_tftp_get_put() -> None:
    """Node fetches a file from a host TFTP server and uploads it back."""
    blob = pattern(7, 20000)
    files = {"blob.bin": blob}
    with TftpServer(HOST_IP, files):
        ret = _core().sendCommand(
            f"get -b -f {WORKDIR}/tftp.bin -h {HOST_IP} blob.bin",
            timeout=60,
        )
        assert ret == 0
        assert _md5_on_node(f"{WORKDIR}/tftp.bin", blob)
        ret = _core().sendCommand(
            f"put -b -f up.bin -h {HOST_IP} {WORKDIR}/tftp.bin",
            timeout=60,
        )
        assert ret == 0
    assert files.get("up.bin") == blob


@pytest.mark.cmd_check("ntpcstart_main")
def test_ntpc_from_host() -> None:
    """Node sets its clock from a host SNTP server.

    netinit starts the daemon at boot, before the server exists; restart
    it so it does not wait out its retry back-off.
    """
    _core().sendCommand("ntpcstop", timeout=10)
    with SntpServer(HOST_IP, NTP_TIME) as server:
        assert _core().sendCommand("ntpcstart", timeout=10) == 0
        try:
            deadline = time.monotonic() + 60
            synced = False
            while not synced and time.monotonic() < deadline:
                synced = _core().sendCommand("date", "2030", timeout=2) == 0
        finally:
            _core().sendCommand("ntpcstop", timeout=10)
        assert server.requests > 0
        assert synced


@pytest.mark.cmd_check("ntpcstart_main")
def test_ntpc_stop_no_server() -> None:
    """The ntpcstop command returns promptly while the server is silent."""
    _core().sendCommand("ntpcstop", timeout=30)
    assert _core().sendCommand("ntpcstart", timeout=10) == 0
    time.sleep(3)
    assert _core().sendCommand("ntpcstop", timeout=3) == 0


@pytest.mark.cmd_check("cmd_nslookup")
def test_nslookup_from_host() -> None:
    """Node resolves a name through a host DNS server."""
    with DnsServer(HOST_IP, {"peer.testlab": "10.42.0.99"}):
        ret = _core().sendCommand(
            "nslookup peer.testlab", "Addr: 10.42.0.99", timeout=30
        )
    assert ret == 0


def _telnet_read_until(sock: socket.socket, token: bytes) -> bytes:
    """Read from a telnet session until ``token``, dropping IAC options.

    :param sock: connected telnet socket
    :param token: bytes to wait for
    :return: data read so far, without IAC sequences
    """
    data = b""
    while token not in data:
        chunk = sock.recv(512)
        if not chunk:
            break
        while b"\xff" in chunk:
            pos = chunk.index(b"\xff")
            data += chunk[:pos]
            chunk = chunk[pos + 3 :]
        data += chunk
    return data


@pytest.mark.cmd_check("telnetd_main")
def test_host_telnet_to_node() -> None:
    """Host runs an nsh command over the node telnet daemon.

    The output line must end with CR LF, the telnet end of line (RFC 854).
    """
    _core().sendCommand("telnetd &", timeout=10)
    with socket.create_connection((NODE_IPS[NODE], 23), timeout=10) as s:
        _telnet_read_until(s, b"nsh>")
        s.sendall(b"echo tl-telnet   ok\r\n")
        out = _telnet_read_until(s, b"nsh>")
        s.sendall(b"exit\r\n")
    assert b"tl-telnet ok\r\n" in out


@pytest.mark.cmd_check("ftpd_start_main")
def test_host_ftp_to_node() -> None:
    """Host uploads a file to the node FTP server and reads it back."""
    blob = pattern(3, 40000)
    ret = _core().sendCommand("ftpd_start -4", "started", timeout=10)
    assert ret == 0
    try:
        with ftplib.FTP() as ftp:
            ftp.connect(NODE_IPS[NODE], 21, timeout=30)
            ftp.login("root", "abc123")
            ftp.storbinary(f"STOR {WORKDIR}/ftp.bin", io.BytesIO(blob))
            got = io.BytesIO()
            ftp.retrbinary(f"RETR {WORKDIR}/ftp.bin", got.write)
    finally:
        _core().sendCommand("ftpd_stop", timeout=10)
    assert got.getvalue() == blob
    assert _md5_on_node(f"{WORKDIR}/ftp.bin", blob)
