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
"""Malformed input: the node returns the POSIX errno or keeps running.

The host builds the packets byte by byte and sends them on the bridge
with an ``AF_PACKET`` socket, so they never pass the host's own stack.
After every batch the node must still answer echo requests from the
host and still reach the host itself; where the standard requires an
answer to a well-formed message, that answer is checked too.

- RFC 8200, 4: "extension headers ... must be processed strictly in the
  order they appear"; 4.3: the Hop-by-Hop Options header "must be
  examined and processed by every node along a packet's delivery path".
  A header longer than the packet cannot be processed, the packet is
  discarded.
- RFC 4443, 4.2: "Every node MUST implement an ICMPv6 Echo responder
  function that receives Echo Requests and originates corresponding Echo
  Replies."
- RFC 3810, 5.2: Multicast Listener Report format, Number of Multicast
  Address Records and, per record, Aux Data Len and Number of Sources,
  which define where each record ends.
- RFC 2131, 4.3.1: a server receiving a DHCPDISCOVER "may respond with
  a DHCPOFFER message"; RFC 2132, 2: "All other options have a one-octet
  length field followed by the data"; the end option marks the end of
  the options field.
- POSIX sendmsg(): "[EINVAL] The sum of the iov_len values overflows an
  ssize_t."
  https://pubs.opengroup.org/onlinepubs/9799919799/functions/sendmsg.html
"""

import re
import socket
import struct
import subprocess
import time
from typing import Any, List, Optional, Tuple

import pytest
from _net_common import HOST_IP, HOST_IP6, NODE_IP6S

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_IPv6", "CONFIG_NET_UDP"),
    pytest.mark.cmd_check("nettl_main"),
]

BRIDGE = "tl-br0"
HOST_MAC = "52:54:00:2a:00:01"  # TL_BRIDGE_MAC in testenv/ip-pair.sh
NODE = 0
ETH_P_IPV6 = 0x86DD
NH_HOPOPTS = 0
NH_ICMPV6 = 58
NH_DSTOPTS = 60
ICMPV6_ECHO_REQUEST = 128
ICMPV6_ECHO_REPLY = 129
ICMPV6_MLDV2_REPORT = 143
MLD_ALL_V2_ROUTERS = "ff02::16"
ECHO_ID = 0x5242
MALFORMED_COUNT = 50
EINVAL = 22
_MAC_RE = re.compile(r"HWaddr ([0-9a-fA-F:]{17})")


def _core(node: int = NODE) -> Any:
    """Return the NTFC core handler of a node.

    :param node: product index
    :return: ``ProductCore`` for core 0
    """
    return pytest.products[node].core(0)


def _node_mac(node: int = NODE) -> bytes:
    """Return the MAC address of a node's eth0.

    :param node: product index
    :return: 6 bytes
    """
    ret = _core(node).sendCommandReadUntilPattern(
        "ifconfig eth0", pattern=r"HWaddr [0-9a-fA-F:]{17}", timeout=10
    )
    found = _MAC_RE.search(str(ret.output))
    assert found, f"no HWaddr: {ret.output}"
    return bytes.fromhex(found.group(1).replace(":", ""))


def _csum(data: bytes) -> int:
    """Return the Internet checksum of ``data``.

    :param data: bytes (padded to an even length)
    :return: 16-bit checksum
    """
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _icmpv6(src: str, dst: str, body: bytes) -> bytes:
    """Fill in the ICMPv6 checksum (RFC 8200 8.1 pseudo-header).

    :param src: IPv6 source
    :param dst: IPv6 destination
    :param body: ICMPv6 message with a zero checksum field
    :return: the message with its checksum
    """
    pseudo = (
        socket.inet_pton(socket.AF_INET6, src)
        + socket.inet_pton(socket.AF_INET6, dst)
        + struct.pack("!IxxxB", len(body), NH_ICMPV6)
    )
    return body[:2] + struct.pack("!H", _csum(pseudo + body)) + body[4:]


def _frame6(
    dst_mac: bytes,
    src: str,
    dst: str,
    nxt: int,
    payload: bytes,
    payload_len: Optional[int] = None,
    hops: int = 64,
) -> bytes:
    """Build an Ethernet frame carrying an IPv6 packet.

    :param dst_mac: destination MAC
    :param src: IPv6 source
    :param dst: IPv6 destination
    :param nxt: Next Header of the IPv6 header
    :param payload: everything after the IPv6 header
    :param payload_len: Payload Length field (default the real length)
    :param hops: Hop Limit
    :return: frame
    """
    length = len(payload) if payload_len is None else payload_len
    ip6 = (
        struct.pack("!IHBB", 0x60000000, length, nxt, hops)
        + socket.inet_pton(socket.AF_INET6, src)
        + socket.inet_pton(socket.AF_INET6, dst)
    )
    eth = dst_mac + bytes.fromhex(HOST_MAC.replace(":", ""))
    return eth + struct.pack("!H", ETH_P_IPV6) + ip6 + payload


def _echo_request(seq: int) -> bytes:
    """Build an ICMPv6 Echo Request with a zero checksum.

    :param seq: sequence number
    :return: message
    """
    return struct.pack("!BBHHH", ICMPV6_ECHO_REQUEST, 0, 0, ECHO_ID, seq)


def _hbh(nxt: int, ext_len: int, data_len: int = 6) -> bytes:
    """Build a Hop-by-Hop (or Destination) Options header.

    :param nxt: Next Header after this header
    :param ext_len: Hdr Ext Len field (units of 8 octets, not counting
     the first 8)
    :param data_len: bytes of PadN option data actually present
    :return: header bytes
    """
    padn = bytes([1, data_len - 2]) + bytes(data_len - 2)
    return bytes([nxt, ext_len]) + padn


class EchoCapture:
    """Count the Echo Replies the node sends to the host."""

    def __init__(self) -> None:
        """Open the capture socket."""
        self._sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)
        )
        self._sock.bind((BRIDGE, 0))
        self._sock.settimeout(0.2)

    def close(self) -> None:
        """Close the capture socket."""
        self._sock.close()

    def send(self, frame: bytes) -> None:
        """Send a frame on the bridge.

        :param frame: Ethernet frame
        """
        self._sock.send(frame)

    def replies(self, seconds: float) -> List[int]:
        """Collect Echo Reply sequence numbers for ``seconds``.

        :param seconds: capture time
        :return: sequence numbers of replies to :data:`ECHO_ID`
        """
        seqs = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                frame, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            if addr[2] == socket.PACKET_OUTGOING or len(frame) < 62:
                continue
            (etype,) = struct.unpack("!H", frame[12:14])
            if etype != ETH_P_IPV6:
                continue
            hdr = _upper_layer(frame[14:])
            if hdr is None:
                continue
            msgtype, _code, _sum, ident, seq = struct.unpack("!BBHHH", hdr)
            if msgtype == ICMPV6_ECHO_REPLY and ident == ECHO_ID:
                seqs.append(seq)
        return seqs


def _upper_layer(packet: bytes) -> Optional[bytes]:
    """Return the first 8 bytes of the ICMPv6 header of an IPv6 packet.

    Hop-by-Hop and Destination Options headers in front of it are
    skipped (RFC 8200 4.3, 4.6: Hdr Ext Len in 8-octet units not
    counting the first 8).

    :param packet: IPv6 packet
    :return: ICMPv6 header, or None for other upper layers
    """
    nxt = packet[6]
    offset = 40
    while nxt in (NH_HOPOPTS, NH_DSTOPTS):
        if len(packet) < offset + 2:
            return None
        nxt = packet[offset]
        offset += (packet[offset + 1] + 1) * 8
    if nxt != NH_ICMPV6 or len(packet) < offset + 8:
        return None
    return packet[offset : offset + 8]


def _host_ping6(addr: str) -> bool:
    """Ping an address from the host.

    :param addr: IPv6 address on the bridge
    :return: True when 3 of 3 echo requests were answered
    """
    ret = subprocess.run(
        ["ping", "-6", "-n", "-q", "-c", "3", "-W", "2", "-I", BRIDGE, addr],
        capture_output=True,
        text=True,
        check=False,
    )
    return " 0% packet loss" in ret.stdout


def _node_ping6(node: int, addr: str) -> bool:
    """Ping ``addr`` from a node (the first ping resolves the neighbor).

    :param node: product index
    :param addr: IPv6 destination
    :return: True when 3 of 3 echo requests were answered
    """
    core = _core(node)
    core.sendCommand(f"ping6 -c 1 {addr}", "packet loss", timeout=15)
    return (
        core.sendCommand(f"ping6 -c 3 {addr}", " 0% packet loss", timeout=30)
        == 0
    )


def _assert_alive(node: int = NODE) -> None:
    """Check that the node still answers and still reaches the host.

    :param node: product index
    """
    assert _host_ping6(NODE_IP6S[node]), "node stopped answering ping6"
    assert _node_ping6(node, HOST_IP6), "node cannot reach the host"


def _ext_cases() -> List[Tuple[str, bytes, Optional[int]]]:
    """Return the malformed extension header cases.

    :return: (name, payload after the IPv6 header, Payload Length)
    """
    echo = _icmpv6(HOST_IP6, NODE_IP6S[NODE], _echo_request(1))
    return [
        # Hop-by-Hop header claims 20 x 8 + 8 octets, packet has 8
        ("hbh-oversized", _hbh(NH_ICMPV6, 20) + echo, None),
        # Payload Length ends inside the 8-octet Hop-by-Hop header
        ("hbh-truncated", _hbh(NH_ICMPV6, 0) + echo, 4),
        # Destination Options header longer than the packet
        ("dstopts-oversized", _hbh(NH_ICMPV6, 255) + echo, None),
        # Hop-by-Hop header with no option bytes at all
        ("hbh-empty", bytes([NH_ICMPV6, 0]), None),
        # Two chained headers, the second beyond the packet
        ("chain-oversized", _hbh(NH_DSTOPTS, 0) + _hbh(NH_ICMPV6, 40), None),
    ]


@pytest.mark.cmd_check("ping6_main")
def test_ipv6_exthdr_bounds() -> None:
    """Extension headers beyond the packet are dropped, the node lives.

    RFC 8200 4 and 4.3 (header processing order, Hop-by-Hop on every
    node); RFC 4443 4.2 (echo responder).  Each malformed case is sent
    50 times to node0: no Echo Reply may come back for them, a
    well-formed request behind a Hop-by-Hop PadN option must be answered
    and the node must still ping and be pinged afterwards.
    """
    mac = _node_mac()
    # Resolve the host as a neighbor first: NuttX answers the first
    # packet to an unknown neighbor with a Neighbor Solicitation instead
    # of the reply, which would hide a missing reply below.
    assert _host_ping6(NODE_IP6S[NODE]), "node does not answer ping6"
    cap = EchoCapture()
    try:
        for name, payload, plen in _ext_cases():
            nxt = NH_DSTOPTS if name.startswith("dstopts") else NH_HOPOPTS
            frame = _frame6(mac, HOST_IP6, NODE_IP6S[NODE], nxt, payload, plen)
            for _ in range(MALFORMED_COUNT):
                cap.send(frame)
            replies = cap.replies(1.0)
            assert not replies, f"{name}: node answered {len(replies)}"
        good = _hbh(NH_ICMPV6, 0) + _icmpv6(
            HOST_IP6, NODE_IP6S[NODE], _echo_request(7)
        )
        frame = _frame6(mac, HOST_IP6, NODE_IP6S[NODE], NH_HOPOPTS, good)
        cap.send(frame)
        cap.send(frame)
        assert 7 in cap.replies(2.0), "no Echo Reply behind a PadN option"
    finally:
        cap.close()
    _assert_alive()


def _mld_report(records: bytes, nrecords: int) -> bytes:
    """Build an MLDv2 Report (RFC 3810 5.2) with the Router Alert option.

    :param records: Multicast Address Record bytes
    :param nrecords: Number of Multicast Address Records field
    :return: payload after the IPv6 header (Hop-by-Hop + ICMPv6)
    """
    body = struct.pack("!BBHHH", ICMPV6_MLDV2_REPORT, 0, 0, 0, nrecords)
    body += records
    src = "fe80::5054:ff:fe2a:1"
    icmp = _icmpv6(src, MLD_ALL_V2_ROUTERS, body)
    # Router Alert (RFC 2711): type 5, length 2, value 0 (MLD), + PadN
    hbh = bytes([NH_ICMPV6, 0, 5, 2, 0, 0, 1, 0])
    return hbh + icmp


def _mld_record(
    nsources: int, auxlen: int, sources: int, rectype: int = 1
) -> bytes:
    """Build one Multicast Address Record.

    :param nsources: Number of Sources field
    :param auxlen: Aux Data Len field (32-bit words)
    :param sources: source addresses actually appended
    :param rectype: Record Type (1 = MODE_IS_INCLUDE)
    :return: record bytes
    """
    group = socket.inet_pton(socket.AF_INET6, "ff12::42")
    return (
        struct.pack("!BBH", rectype, auxlen, nsources)
        + group
        + socket.inet_pton(socket.AF_INET6, HOST_IP6) * sources
    )


@pytest.mark.dep_config("CONFIG_NET_MLD")
@pytest.mark.cmd_check("ping6_main")
def test_mld_report_bounds() -> None:
    """MLDv2 Reports whose records run past the packet leave node0 alive.

    RFC 3810 5.2: the record count, Aux Data Len and Number of Sources
    fields define the records; records that extend beyond the message
    cannot be valid.  Each case is sent 50 times to ff02::16 (all MLDv2
    routers, which every MLDv2 node listens to); node0 must still ping
    and be pinged afterwards.
    """
    cases = [
        ("count-beyond", _mld_report(_mld_record(0, 0, 0), 4)),
        ("sources-beyond", _mld_report(_mld_record(65535, 0, 0), 1)),
        ("auxdata-beyond", _mld_report(_mld_record(0, 255, 0), 1)),
        ("record-truncated", _mld_report(_mld_record(0, 0, 0)[:6], 1)),
        ("short-report", _mld_report(b"", 1)[:12]),
    ]
    mac = bytes.fromhex("333300000016")  # RFC 2464 7: ff02::16
    cap = EchoCapture()
    try:
        for _name, payload in cases:
            frame = _frame6(
                mac,
                "fe80::5054:ff:fe2a:1",
                MLD_ALL_V2_ROUTERS,
                NH_HOPOPTS,
                payload,
                hops=1,
            )
            for _ in range(MALFORMED_COUNT):
                cap.send(frame)
            time.sleep(0.2)
    finally:
        cap.close()
    _assert_alive()


DHCP_MAGIC = b"\x63\x82\x53\x63"
DHCP_CLIENT_MAC = bytes.fromhex("02000000bad1")


def _dhcp_discover(options: bytes, xid: int) -> bytes:
    """Build a BOOTREQUEST with the broadcast flag and given options.

    :param options: bytes after the magic cookie
    :param xid: transaction id
    :return: DHCP message
    """
    return (
        bytes([1, 1, 6, 0])
        + struct.pack("!IHH", xid, 0, 0x8000)
        + bytes(16)
        + DHCP_CLIENT_MAC
        + bytes(10)
        + bytes(192)
        + DHCP_MAGIC
        + options
    )


def _dhcp_option(code: int, data: bytes) -> bytes:
    """Encode one DHCP option (RFC 2132 2).

    :param code: option code
    :param data: option data
    :return: option bytes
    """
    return bytes([code, len(data)]) + data


def _dhcp_cases() -> List[Tuple[str, bytes]]:
    """Return DISCOVER option fields with lengths beyond the field.

    :return: (name, options after the magic cookie)
    """
    discover = _dhcp_option(53, b"\x01")
    return [
        # message type option claims 200 bytes, 3 follow
        ("type-overrun", bytes([53, 200, 1]) + b"\xff"),
        # client id option runs past the end of the message
        ("clientid-overrun", discover + bytes([61, 255]) + bytes(8)),
        # length byte missing: option code is the last byte
        ("no-length", discover + bytes([55])),
        # no end option and the field filled to the last byte
        ("no-end", discover + bytes([12, 255]) + b"x" * 255),
        # overload option pointing at sname/file that hold no options
        ("overload", discover + _dhcp_option(52, b"\x03") + b"\xff"),
        # pad bytes only, no message type
        ("pad-only", bytes(300)),
    ]


def _dhcp_offer(sock: socket.socket, xid: int, timeout: float) -> bool:
    """Wait for a DHCPOFFER to ``xid``.

    :param sock: UDP socket bound to port 68 on the bridge
    :param xid: transaction id
    :param timeout: seconds
    :return: True when an OFFER with a non-zero yiaddr arrived
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data, _peer = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if len(data) < 240 or data[0] != 2 or data[236:240] != DHCP_MAGIC:
            continue
        if struct.unpack("!I", data[4:8])[0] != xid:
            continue
        opts = data[240:]
        if 53 in opts and opts[opts.index(53) + 2] == 2:
            return data[16:20] != bytes(4)
    return False


@pytest.mark.dep_config("CONFIG_NETUTILS_DHCPD", "CONFIG_NET_BROADCAST")
@pytest.mark.cmd_check("dhcpd_start_main")
@pytest.mark.cmd_check("dhcpd_stop_main")
@pytest.mark.cmd_check("ping_main")
def test_dhcpd_option_bounds() -> None:
    """node0's DHCP server ignores option fields that overrun the message.

    RFC 2132 2: options carry a one-octet length followed by the data;
    RFC 2131 4.3.1: a server answers a DHCPDISCOVER with a DHCPOFFER.
    Each malformed DISCOVER is sent 20 times; none may be answered with
    an OFFER, a well-formed DISCOVER from the same client must then get
    an OFFER, and node0 must still answer ping.
    """
    core = _core()
    assert core.sendCommand("dhcpd_start eth0", timeout=10) == 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE, BRIDGE.encode()
        )
        sock.bind(("", 68))
        sock.settimeout(0.2)
        xid = 0x1000
        for name, options in _dhcp_cases():
            xid += 1
            for _ in range(20):
                sock.sendto(
                    _dhcp_discover(options, xid), ("255.255.255.255", 67)
                )
            assert not _dhcp_offer(sock, xid, 1.0), f"{name}: OFFER sent"
        xid += 1
        good = _dhcp_option(53, b"\x01") + b"\xff"
        sock.sendto(_dhcp_discover(good, xid), ("255.255.255.255", 67))
        assert _dhcp_offer(sock, xid, 5.0), "no OFFER for a valid DISCOVER"
    finally:
        sock.close()
        core.sendCommand("dhcpd_stop", timeout=10)
    core.sendCommand(f"ping -c 1 {HOST_IP}", "packet loss", timeout=15)
    assert (
        core.sendCommand(f"ping -c 3 {HOST_IP}", " 0% packet loss", timeout=30)
        == 0
    )


def test_sendmsg_iov_overflow() -> None:
    """sendmsg() with iov_len values that overflow ssize_t fails EINVAL.

    POSIX sendmsg(): "[EINVAL] The sum of the iov_len values overflows an
    ssize_t."  node0 ``nettl -c <host> -u -I``: sendmsg() of a 2-element
    iov (SSIZE_MAX + 16 bytes) on a UDP socket returns -1 with errno
    EINVAL.
    """
    ret = _core().sendCommandReadUntilPattern(
        f"nettl -c {HOST_IP} -u -I -p 5300",
        pattern=r"nettl: (PASS|FAIL) tx=[^\r\n]*[\r\n]",
        timeout=20,
    )
    found = re.search(r"nettl: (PASS|FAIL) [^\r\n]*", str(ret.output))
    assert found, ret.output
    verdict = found.group(0)
    assert verdict.startswith("nettl: PASS"), verdict
    assert f"iov={EINVAL}" in verdict, verdict
