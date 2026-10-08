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

"""IP fragmentation and reassembly on the ip-pair bridge."""

import socket
import struct
import time
from typing import List

import pytest
from _net_common import (
    HOST_IP,
    HOST_IP6,
    NODE_IP6S,
    NODE_IPS,
    host_udp_echo_check,
    nettl_client,
    nettl_server,
    pattern,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_IPFRAG", "CONFIG_NET_UDP"),
]

# Larger than the 1500-byte MTU: every datagram is fragmented

BIG = 6000
FRAG_DATA = 1480  # IPv4 fragment payload, a multiple of 8
HOST_PORT = 47001
IP_HDRINCL_ID = 0x4242


def _ping_cmd(ipv6: bool) -> str:
    """Return the ping command for an address family.

    :param ipv6: use ping6
    :return: command name
    """
    return "ping6" if ipv6 else "ping"


@pytest.mark.parametrize("ipv6", [False, True], ids=["v4", "v6"])
def test_ping_fragmented_host(ipv6: bool) -> None:
    """Node 0 pings the host with 4000-byte echoes, 0% loss."""
    if ipv6 and not pytest.products[0].core(0).conf.kv_check(
        "CONFIG_NET_IPv6"
    ):
        pytest.skip("no IPv6")
    cmd = _ping_cmd(ipv6)
    addr = HOST_IP6 if ipv6 else HOST_IP
    core = pytest.products[0].core(0)
    core.sendCommand(f"{cmd} -c 1 {addr}", "packet loss", timeout=15)
    ret = core.sendCommand(
        f"{cmd} -s 4000 -c 3 {addr}", " 0% packet loss", timeout=30
    )
    assert ret == 0


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("ipv6", [False, True], ids=["v4", "v6"])
def test_udp_fragmented_node_to_node(ipv6: bool) -> None:
    """Node 0 exchanges 6000-byte UDP datagrams with node 1."""
    if ipv6 and not pytest.products[0].core(0).conf.kv_check(
        "CONFIG_NET_IPv6"
    ):
        pytest.skip("no IPv6")
    addr = NODE_IP6S[1] if ipv6 else NODE_IPS[1]
    nettl_server(1, True, 5220, addr, ipv6=ipv6)
    verdict = nettl_client(0, addr, True, 5220, 50, length=BIG)
    assert verdict.startswith("nettl: PASS"), verdict


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("ipv6", [False, True], ids=["v4", "v6"])
def test_host_udp_fragmented_to_node(ipv6: bool) -> None:
    """Host sends 6000-byte UDP datagrams to node 0, echoes intact."""
    if ipv6 and not pytest.products[0].core(0).conf.kv_check(
        "CONFIG_NET_IPv6"
    ):
        pytest.skip("no IPv6")
    addr = NODE_IP6S[0] if ipv6 else NODE_IPS[0]
    nettl_server(0, True, 5221, addr, ipv6=ipv6)
    assert host_udp_echo_check(addr, 5221, 50, length=BIG, retries=2)


def _csum(data: bytes) -> int:
    """Return the Internet checksum of ``data``.

    :param data: bytes to sum
    :return: 16-bit one's complement checksum
    """
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _udp_datagram(dport: int, seq: int, length: int) -> bytes:
    """Build a nettl UDP datagram (header + payload) from the host.

    :param dport: node UDP port
    :param seq: nettl sequence number
    :param length: nettl payload length
    :return: UDP header and data with a valid checksum
    """
    data = struct.pack(">I", seq) + pattern(seq * length, length)
    ulen = 8 + len(data)
    pseudo = (
        socket.inet_aton(HOST_IP)
        + socket.inet_aton(NODE_IPS[0])
        + struct.pack("!BBH", 0, socket.IPPROTO_UDP, ulen)
    )
    hdr = struct.pack("!HHHH", HOST_PORT, dport, ulen, 0)
    csum = _csum(pseudo + hdr + data) or 0xFFFF
    return struct.pack("!HHHH", HOST_PORT, dport, ulen, csum) + data


def _fragments(ident: int, udp: bytes) -> List[bytes]:
    """Split a UDP datagram into IPv4 fragments.

    :param ident: IP identification
    :param udp: UDP header and data
    :return: IPv4 packets in offset order
    """
    frags = []
    for off in range(0, len(udp), FRAG_DATA):
        chunk = udp[off : off + FRAG_DATA]
        more = 0x2000 if off + FRAG_DATA < len(udp) else 0
        hdr = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(chunk),
            ident,
            more | (off // 8),
            64,
            socket.IPPROTO_UDP,
            0,
            socket.inet_aton(HOST_IP),
            socket.inet_aton(NODE_IPS[0]),
        )
        hdr = hdr[:10] + struct.pack("!H", _csum(hdr)) + hdr[12:]
        frags.append(hdr + chunk)
    return frags


def _echoed(rx: socket.socket, seq: int, length: int) -> bool:
    """Wait for the node echo of datagram ``seq``.

    :param rx: host UDP socket bound to ``HOST_PORT``
    :param seq: nettl sequence number
    :param length: nettl payload length
    :return: True when the intact echo arrived
    """
    expect = struct.pack(">I", seq) + pattern(seq * length, length)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            data = rx.recv(65535)
        except socket.timeout:
            continue
        if data == expect:
            return True
    return False


@pytest.mark.cmd_check("nettl_main")
def test_frag_reorder_and_stale() -> None:
    """Node 0 reassembles reversed fragments and survives stale ones.

    The host injects raw IPv4 fragments: a datagram with its fragments in
    reverse order, then 32 datagrams of which only the first fragment is
    sent, then complete datagrams again, before and after the reassembly
    timeout.
    """
    nettl_server(0, True, 5222, NODE_IPS[0])
    raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.bind((HOST_IP, HOST_PORT))
        rx.settimeout(0.5)

        def send(ident: int, seq: int, order: int = 1, count: int = 0) -> None:
            frags = _fragments(ident, _udp_datagram(5222, seq, BIG))
            for frag in (frags[::order])[: count or len(frags)]:
                raw.sendto(frag, (NODE_IPS[0], 0))

        send(IP_HDRINCL_ID, 0, order=-1)
        assert _echoed(rx, 0, BIG), "reversed fragments not reassembled"

        for i in range(32):
            send(IP_HDRINCL_ID + 1 + i, 1000 + i, count=1)
        send(IP_HDRINCL_ID + 100, 1)
        assert _echoed(rx, 1, BIG), "no echo after incomplete datagrams"

        time.sleep(3)  # longer than CONFIG_NET_IPFRAG_REASS_MAXAGE (2 s)
        send(IP_HDRINCL_ID + 101, 2)
        assert _echoed(rx, 2, BIG), "no echo after reassembly timeout"
    finally:
        raw.close()
        rx.close()
