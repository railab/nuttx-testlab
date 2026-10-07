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

"""Minimal host-side servers used as Linux peers by NTFC tests.

Each server runs in a daemon thread on the host bridge address and is
used as a context manager. Standard ports are used, so the tests need
root (the Docker runner).
"""

import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Dict, Optional, Tuple, Type

NTP_EPOCH_OFFSET = 2208988800  # seconds from 1900-01-01 to 1970-01-01


class _UdpServer:
    """Base class: UDP socket served by a thread until closed."""

    def __init__(self, addr: str, port: int) -> None:
        """Bind the socket.

        :param addr: host address to bind
        :param port: UDP port
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((addr, port))
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.requests = 0

    def __enter__(self) -> "_UdpServer":
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
        """Stop serving and close the socket.

        :param exc_type: exception type, if any
        :param exc: exception, if any
        :param tb: traceback, if any
        """
        self._stop.set()
        self._thread.join(timeout=5)
        self.sock.close()

    def _loop(self) -> None:
        """Receive datagrams and dispatch them to :meth:`handle`."""
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.requests += 1
            self.handle(data, peer)

    def handle(self, data: bytes, peer: Tuple[str, int]) -> None:
        """Handle one datagram.

        :param data: received datagram
        :param peer: sender address
        """
        raise NotImplementedError


class SntpServer(_UdpServer):
    """SNTP server (stratum 1) reporting a fixed wall-clock time."""

    def __init__(self, addr: str, unix_time: int, port: int = 123) -> None:
        """Bind the socket.

        :param addr: host address to bind
        :param unix_time: time reported at start, seconds since 1970
        :param port: UDP port
        """
        super().__init__(addr, port)
        self.unix_time = unix_time
        self._start = time.monotonic()

    def _now(self) -> bytes:
        """Return the current NTP timestamp.

        :return: 64-bit NTP timestamp
        """
        now = self.unix_time + time.monotonic() - self._start
        secs = int(now)
        frac = int((now - secs) * (1 << 32)) & 0xFFFFFFFF
        return struct.pack(">II", secs + NTP_EPOCH_OFFSET, frac)

    def handle(self, data: bytes, peer: Tuple[str, int]) -> None:
        """Answer a client (mode 3) request.

        :param data: received datagram
        :param peer: sender address
        """
        if len(data) < 48 or data[0] & 0x7 != 3:
            return
        version = (data[0] >> 3) & 0x7
        now = self._now()
        reply = (
            bytes([(version << 3) | 4, 1, data[2], 0xEC])
            + struct.pack(">II", 0, 0)
            + b"TLAB"
            + now
            + data[40:48]
            + now
            + self._now()
        )
        self.sock.sendto(reply, peer)


class DnsServer(_UdpServer):
    """DNS server answering A queries from a fixed table."""

    def __init__(
        self, addr: str, records: Dict[str, str], port: int = 53
    ) -> None:
        """Bind the socket.

        :param addr: host address to bind
        :param records: map of lower-case host name to IPv4 address
        :param port: UDP port
        """
        super().__init__(addr, port)
        self.records = records

    @staticmethod
    def _qname(data: bytes) -> Tuple[str, int]:
        """Decode the question name.

        :param data: DNS query
        :return: name and offset just past it
        """
        labels = []
        pos = 12
        while pos < len(data) and data[pos]:
            size = data[pos]
            labels.append(data[pos + 1 : pos + 1 + size].decode())
            pos += 1 + size
        return ".".join(labels).lower(), pos + 1

    def handle(self, data: bytes, peer: Tuple[str, int]) -> None:
        """Answer a single-question query.

        :param data: received datagram
        :param peer: sender address
        """
        if len(data) < 17:
            return
        name, pos = self._qname(data)
        qtype, _qclass = struct.unpack(">HH", data[pos : pos + 4])
        question = data[12 : pos + 4]
        addr = self.records.get(name)
        if addr is None or qtype != 1:
            rcode = 3 if addr is None else 0
            header = data[:2] + struct.pack(
                ">HHHHH", 0x8180 | rcode, 1, 0, 0, 0
            )
            self.sock.sendto(header + question, peer)
            return
        header = data[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
        answer = struct.pack(">HHHIH", 0xC00C, 1, 1, 60, 4)
        answer += socket.inet_aton(addr)
        self.sock.sendto(header + question + answer, peer)


class TftpServer(_UdpServer):
    """TFTP server (octet mode, lock-step) keeping files in memory."""

    BLOCK = 512

    def __init__(
        self, addr: str, files: Dict[str, bytes], port: int = 69
    ) -> None:
        """Bind the socket.

        :param addr: host address to bind
        :param files: served files; uploads are stored here too
        :param port: UDP port of the listening socket
        """
        super().__init__(addr, port)
        self.addr = addr
        self.files = files

    def handle(self, data: bytes, peer: Tuple[str, int]) -> None:
        """Start a transfer for a RRQ or WRQ.

        :param data: received datagram
        :param peer: sender address
        """
        opcode = struct.unpack(">H", data[:2])[0]
        name = data[2:].split(b"\0")[0].decode()
        target = self._read if opcode == 1 else self._write
        if opcode in (1, 2):
            threading.Thread(
                target=target, args=(name, peer), daemon=True
            ).start()

    def _socket(self) -> socket.socket:
        """Return a transfer socket on an ephemeral port.

        :return: bound UDP socket
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.addr, 0))
        sock.settimeout(1.0)
        return sock

    @staticmethod
    def _error(sock: socket.socket, peer: Tuple[str, int], msg: str) -> None:
        """Send an ERROR packet.

        :param sock: transfer socket
        :param peer: client address
        :param msg: error message
        """
        sock.sendto(struct.pack(">HH", 5, 1) + msg.encode() + b"\0", peer)

    def _read(self, name: str, peer: Tuple[str, int]) -> None:
        """Serve a read request.

        :param name: requested file name
        :param peer: client address
        """
        with self._socket() as sock:
            data = self.files.get(name)
            if data is None:
                self._error(sock, peer, "not found")
                return
            block = 1
            while True:
                chunk = data[(block - 1) * self.BLOCK : block * self.BLOCK]
                pkt = struct.pack(">HH", 3, block & 0xFFFF) + chunk
                if not self._send_until_ack(sock, peer, pkt, block):
                    return
                if len(chunk) < self.BLOCK:
                    return
                block += 1

    def _send_until_ack(
        self,
        sock: socket.socket,
        peer: Tuple[str, int],
        pkt: bytes,
        block: int,
    ) -> bool:
        """Send a packet until the client acknowledges ``block``.

        :param sock: transfer socket
        :param peer: client address
        :param pkt: packet to (re)send
        :param block: block number to wait an ACK for
        :return: True when acknowledged
        """
        for _ in range(5):
            sock.sendto(pkt, peer)
            try:
                ack, _ = sock.recvfrom(16)
            except socket.timeout:
                continue
            if struct.unpack(">HH", ack[:4]) == (4, block & 0xFFFF):
                return True
        return False

    def _write(self, name: str, peer: Tuple[str, int]) -> None:
        """Serve a write request.

        :param name: uploaded file name
        :param peer: client address
        """
        with self._socket() as sock:
            data = b""
            block = 0
            ack = struct.pack(">HH", 4, 0)
            while True:
                pkt = None
                for _ in range(5):
                    sock.sendto(ack, peer)
                    try:
                        pkt, _ = sock.recvfrom(4 + self.BLOCK)
                        break
                    except socket.timeout:
                        continue
                if pkt is None:
                    return
                opcode, num = struct.unpack(">HH", pkt[:4])
                if opcode != 3:
                    return
                if num == (block + 1) & 0xFFFF:
                    data += pkt[4:]
                    block += 1
                ack = struct.pack(">HH", 4, num)
                if len(pkt) - 4 < self.BLOCK:
                    sock.sendto(ack, peer)
                    self.files[name] = data
                    return


class HttpServer:
    """HTTP server serving fixed in-memory files."""

    def __init__(self, addr: str, files: Dict[str, bytes], port: int) -> None:
        """Bind the server.

        :param addr: host address to bind
        :param files: map of URL path to body
        :param port: TCP port
        """
        served = files

        class Handler(BaseHTTPRequestHandler):
            """Serve ``files``, 404 for anything else."""

            def do_GET(self) -> None:  # noqa: N802
                """Answer a GET request."""
                body = served.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                """Keep the test output quiet.

                :param args: ignored
                """

        self.server = ThreadingHTTPServer((addr, port), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def __enter__(self) -> "HttpServer":
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
        """Stop serving.

        :param exc_type: exception type, if any
        :param exc: exception, if any
        :param tb: traceback, if any
        """
        self.server.shutdown()
        self.server.server_close()
