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

"""Shared nettl helpers for smoke and multi-node IP tests."""

import re
import socket
import struct
import time
from typing import Any, List, Optional, Tuple

import pytest

# Host-reachable nettl servers started by the current test: (addr, port, udp)

_SERVERS: List[Tuple[str, int, bool]] = []

VERDICT_RE = r"nettl: (PASS|FAIL) tx=[^\r\n]*[\r\n]"

HOST_IP = "10.42.0.1"
NODE_IPS = ("10.42.0.10", "10.42.0.11")
HOST_IP6 = "fc00::1"
NODE_IP6S = ("fc00::10", "fc00::11")
UDP_END = 0xFFFFFFFF


def pattern(offset: int, length: int) -> bytes:
    """Return the nettl data pattern.

    :param offset: absolute stream offset of the first byte
    :param length: number of bytes
    :return: pattern bytes, byte ``i`` is ``(i * 31 + 7) & 0xff``
    """
    return bytes(((offset + i) * 31 + 7) & 0xFF for i in range(length))


def family(addr: str) -> socket.AddressFamily:
    """Return the socket address family of an IP address.

    :param addr: IPv4 or IPv6 address
    :return: ``AF_INET6`` for IPv6, ``AF_INET`` otherwise
    """
    return socket.AF_INET6 if ":" in addr else socket.AF_INET


def _opts(udp: bool, ipv6: bool) -> str:
    """Return nettl protocol options.

    :param udp: use UDP instead of TCP
    :param ipv6: use IPv6
    :return: option string, ending with a space when not empty
    """
    return ("-6 " if ipv6 else "") + ("-u " if udp else "")


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def nettl_server(
    node: int,
    udp: bool,
    port: int,
    addr: Optional[str] = None,
    ipv6: bool = False,
    group: Optional[str] = None,
) -> None:
    """Start a background nettl server on a node.

    :param node: product index
    :param udp: use UDP instead of TCP
    :param port: listen port
    :param addr: node address reachable from the host; when given,
     :func:`nettl_cleanup` unblocks a server the test left running
    :param ipv6: listen on IPv6
    :param group: multicast group to join (UDP only)
    """
    proto = _opts(udp, ipv6) + (f"-g {group} " if group else "")
    ret = _core(node).sendCommand(
        f"nettl -s {proto}-p {port} -t 30 &", "listening", timeout=10
    )
    assert ret == 0
    if addr:
        _SERVERS.append((addr, port, udp))


def nettl_cleanup() -> None:
    """Make servers left running by a failed test exit normally.

    A TCP server gets a connect + close (EOF), a UDP server gets the end
    marker. Killing the task instead leaks its TCP connection in NuttX.
    """
    while _SERVERS:
        addr, port, udp = _SERVERS.pop()
        try:
            if udp:
                with socket.socket(family(addr), socket.SOCK_DGRAM) as s:
                    s.sendto(struct.pack(">I", UDP_END), (addr, port))
            else:
                socket.create_connection((addr, port), timeout=2).close()
        except OSError:
            pass  # server already exited


def nettl_client(
    node: int,
    addr: str,
    udp: bool,
    port: int,
    count: int,
    length: Optional[int] = None,
) -> str:
    """Run nettl client on a node and return its verdict line.

    :param node: product index
    :param addr: server IPv4 or IPv6 address
    :param udp: use UDP instead of TCP
    :param port: server port
    :param count: bytes (TCP) or datagrams (UDP)
    :param length: UDP payload length (nettl default if not given)
    :return: verdict line or empty string
    """
    proto = _opts(udp, family(addr) == socket.AF_INET6)
    if length is not None:
        proto += f"-l {length} "
    ret = _core(node).sendCommandReadUntilPattern(
        f"nettl -c {addr} {proto}-p {port} -n {count}",
        pattern=VERDICT_RE,
        timeout=120,
    )
    found = re.search(VERDICT_RE, ret.output)
    return found.group(0).rstrip("\r\n") if found else ""


def host_tcp_echo_check(
    addr: str, port: int, nbytes: int, timeout: float = 10.0
) -> bool:
    """Act as nettl TCP client from the host.

    :param addr: node address running ``nettl -s``
    :param port: node port
    :param nbytes: total bytes to send
    :param timeout: socket timeout in seconds
    :return: True when every echoed byte matches the pattern
    """
    with socket.create_connection((addr, port), timeout=timeout) as sock:
        sent = 0
        while sent < nbytes:
            chunk = pattern(sent, min(512, nbytes - sent))
            sock.sendall(chunk)
            echo = b""
            while len(echo) < len(chunk):
                data = sock.recv(len(chunk) - len(echo))
                if not data:
                    return False
                echo += data
            if echo != chunk:
                return False
            sent += len(chunk)
    return True


def host_udp_echo_check(
    addr: str,
    port: int,
    count: int,
    length: int = 512,
    timeout: float = 2.0,
    interval: float = 0.0,
    retries: int = 0,
    mcast_dev: Optional[str] = None,
) -> bool:
    """Act as nettl UDP client from the host.

    :param addr: node address running ``nettl -s -u``
    :param port: node port
    :param count: number of datagrams
    :param length: payload length
    :param timeout: per-datagram timeout in seconds
    :param interval: delay in seconds after each matched echo, before
     sending the next datagram (0 paces nothing, the default)
    :param retries: extra send attempts for a datagram that times out,
     on top of the first attempt (0 keeps the original behaviour: stop
     on the first lost datagram)
    :param mcast_dev: host interface for a multicast ``addr``
    :return: True when all datagrams are echoed intact
    """
    with socket.socket(family(addr), socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        dest = _mcast_dest(sock, addr, port, mcast_dev)
        ok = True
        for seq in range(count):
            dgram = struct.pack(">I", seq) + pattern(seq * length, length)
            echo: Optional[bytes] = None
            for _attempt in range(retries + 1):
                sock.sendto(dgram, dest)
                try:
                    echo = sock.recv(len(dgram) + 16)
                    break
                except socket.timeout:
                    echo = None
            if echo is None:
                ok = False
                break
            ok = ok and echo == dgram
            if interval:
                time.sleep(interval)
        for _ in range(3):
            sock.sendto(struct.pack(">I", UDP_END), dest)
    return ok


def _mcast_dest(
    sock: socket.socket, addr: str, port: int, mcast_dev: Optional[str]
) -> Tuple[Any, ...]:
    """Set up multicast sending on ``mcast_dev`` and return the destination.

    :param sock: UDP socket
    :param addr: destination address
    :param port: destination port
    :param mcast_dev: host interface for a multicast ``addr``, or None
    :return: ``sendto()`` address
    """
    if not mcast_dev:
        return (addr, port)
    if family(addr) == socket.AF_INET6:
        index = socket.if_nametoindex(mcast_dev)
        sock.setsockopt(
            socket.IPPROTO_IPV6,
            socket.IPV6_MULTICAST_IF,
            struct.pack("@I", index),
        )
        return (addr, port, 0, index)
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(HOST_IP)
    )
    return (addr, port)
