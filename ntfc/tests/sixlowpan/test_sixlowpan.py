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
"""6LoWPAN on one sim node with a loopback radio.

The node's only network device is ``wpan0``, an IEEE 802.15.4 loopback
(``CONFIG_IEEE802154_LOOPBACK``, 127-byte frames) or a packet radio
loopback (``CONFIG_PKTRADIO_LOOPBACK``, 128-byte frames): every frame
the adaptation layer sends is received by the same node, so a UDP, TCP
or ICMPv6 exchange with the node's own link-local address passes
through IPHC compression (RFC 6282), fragmentation and reassembly
(RFC 4944 5.3) in both directions. ``nettl`` echoes the data and prints
a verdict; ``/proc/net/wpan0`` counts the frames.

NuttX compresses a packet with HC06 only when its payload reaches
``CONFIG_NET_6LOWPAN_COMPRESSION_THRESHOLD`` bytes (default 63); below
that it sends the uncompressed IPv6 header (RFC 4944 5.1, dispatch
01000001). The size parameters cover both, one-frame packets and
fragmented ones up to the 1280-byte IPv6 minimum MTU (RFC 8200 5).
"""

import math
import re
from typing import Any, Iterator, List

import pytest
from _net_common import VERDICT_RE, _core, nettl_client, nettl_server
from _netleak import dev_stats

pytestmark = [pytest.mark.dep_config("CONFIG_NET_6LOWPAN")]

DEV = "wpan0"

# IEEE 802.15.4-2006 6.4.1 aMaxPHYPacketSize: 127 octets per frame; the
# packet radio loopback uses CONFIG_IOB_BUFSIZE = 128.
FRAME_MAX = 127

# RFC 8200 5: IPv6 requires every link to carry 1280-byte packets;
# 1280 - 40 (IPv6) - 8 (UDP) bytes of UDP payload. nettl adds a 4-byte
# sequence header to every datagram.
UDP_MAX = 1232 - 4
NETTL_UDP_HDR = 4

# RFC 6282 4.3.3: UDP ports 0xF0B0..0xF0BF are compressed to 4 bits
# each; any other port goes inline.
PORT_COMPRESSED = 61616
PORT_INLINE = 5101

UDP_SIZES = [16, 64, 120, 500, UDP_MAX]
PING_SIZES = [16, 300, 1200]
UDP_COUNT = 4

LOOPBACK_SADDR_BUG = (
    "wireless/ieee802154/mac802154_loopback.c lo_addr2ip(): the short "
    "address is copied into d_mac.radio.nv_addr and the link-local IID in "
    "over-the-air (little-endian) byte order, while mac802154_netdev.c "
    "and sixlowpan_extract_srcaddr() swap it; the IID decompressed from "
    "the MAC address (HC06 SAM/DAM=11) is therefore fe80::ff:fe00:cdab "
    "for a node at fe80::ff:fe00:abcd and ipv6_input() drops every "
    "IPHC-compressed packet as not destined for us"
)

TCP_TAIL_BUG = (
    "net/sixlowpan/sixlowpan_tcpsend.c, net/tcp/tcp_input.c: a TCP echo of "
    "4096 bytes over the loopback radio returns the 814-byte second "
    "segment of the second 2048-byte write with wrong data (nettl "
    "err=814) while the server received all 4096 bytes intact; writes up "
    "to 3000 bytes and UDP datagrams of the same sizes are intact; both "
    "814-byte segments are read-ahead buffered by tcp_datahandler() and "
    "sent with the right sndseq, so the wrong bytes come from the "
    "receive side; not isolated further"
)

BURST_HANG_BUG = (
    "wireless/ieee802154/mac802154_loopback.c, net/sixlowpan: with the "
    "lo_addr2ip() byte-order bug (LOOPBACK_SADDR_BUG) every "
    "IPHC-compressed packet is dropped by ipv6_input(); a client that "
    "sends 50 fragmented 1228-byte datagrams in a row then stops the "
    "node: no verdict, no NSH prompt, the console stays dead (4 such "
    "datagrams only lose their echoes); with the byte order fixed the "
    "same burst passes"
)

TCP_SEND_BLOCK_BUG = (
    "net/sixlowpan/sixlowpan_tcpsend.c psock_6lowpan_tcp_send(): with "
    "every data segment dropped by the receiver (LOOPBACK_SADDR_BUG) the "
    "client's send() never returns: the 3-way handshake (uncompressed, "
    "below the compression threshold) succeeds, the first data segment is "
    "retransmitted without end and the task stays blocked for minutes "
    "although SO_RCVTIMEO is 10 s and TCP_MAXRTX should abort the "
    "connection; the node answers no other command meanwhile"
)

OVER_MTU_HANG_BUG = (
    "net/sixlowpan: a UDP datagram of 1236 bytes (IPv6 length 1284, "
    "within the 1294-byte CONFIG_NET_6LOWPAN_PKTSIZE of wpan0) is "
    "fragmented into 13 frames and reassembled, but the node stops "
    "answering the console after the reassembled echo is dispatched "
    "(sixlowpan_frame_process() reports the full length and nothing "
    "follows); 1232 bytes work"
)

_ADDR_RE = re.compile(r"inet6 addr: (fe80:[0-9a-f:]+)/\d+")
_HWADDR_RE = re.compile(r"HWaddr ([0-9a-f:]+) at")
_PING_RE = re.compile(r"(\d+) packets transmitted, (\d+) received")


def _conf() -> Any:
    """Return the Kconfig view of the node.

    :return: NTFC core configuration
    """
    return pytest.product.core(0).conf


def _threshold() -> int:
    """Return the payload size from which NuttX compresses with HC06.

    :return: CONFIG_NET_6LOWPAN_COMPRESSION_THRESHOLD
    """
    return int(_conf().kv_check("CONFIG_NET_6LOWPAN_COMPRESSION_THRESHOLD"))


def _ieee802154_loopback() -> bool:
    """Return whether the node uses the IEEE 802.15.4 loopback.

    :return: CONFIG_IEEE802154_LOOPBACK is set
    """
    return bool(_conf().kv_check("CONFIG_IEEE802154_LOOPBACK"))


def _loopback_bug(request: pytest.FixtureRequest, compressed: bool) -> None:
    """Expect the IEEE 802.15.4 loopback to drop a compressed packet.

    :param request: pytest request
    :param compressed: the exchange carries IPHC-compressed packets
    """
    if compressed and _ieee802154_loopback():
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=LOOPBACK_SADDR_BUG)
        )


def _tcp_send_block() -> None:
    """Skip a TCP test whose client blocks the IEEE 802.15.4 loopback."""
    if _ieee802154_loopback():
        pytest.skip(TCP_SEND_BLOCK_BUG)


def _burst_hang() -> None:
    """Skip a burst that hangs the IEEE 802.15.4 loopback node."""
    if _ieee802154_loopback():
        pytest.skip(BURST_HANG_BUG)


@pytest.fixture(scope="module")
def ifconfig() -> str:
    """Return the ``ifconfig wpan0`` output."""
    ret = _core(0).sendCommandReadUntilPattern(
        f"ifconfig {DEV}", pattern=r"inet6 addr: [^\n]*\n", timeout=10
    )
    return str(ret.output)


@pytest.fixture(scope="module")
def addr(ifconfig: str) -> str:
    """Return the node's link-local address on wpan0."""
    found = _ADDR_RE.search(ifconfig)
    assert found, ifconfig
    return found.group(1)


def _frames() -> int:
    """Return the frames the loopback delivered so far.

    :return: ``RX Received`` counter of ``/proc/net/wpan0``
    """
    return dev_stats(0, DEV)["RX"][0]


@pytest.fixture
def frames() -> Iterator[List[int]]:
    """Give the test a list whose ``check(min)`` compares the frame delta."""
    before = _frames()
    yield [before]


def _frames_since(before: int, minimum: int) -> int:
    """Check that at least ``minimum`` frames were exchanged.

    :param before: counter before the test
    :param minimum: lower bound
    :return: the delta
    """
    delta = _frames() - before
    print(f"{DEV} frames: {delta} (at least {minimum} expected)")
    assert delta >= minimum, (delta, minimum)
    return delta


def _min_frames(size: int, packets: int) -> int:
    """Lower bound of frames for ``packets`` packets of ``size`` bytes.

    A frame carries at most FRAME_MAX octets including every header, so
    a packet needs at least ceil(size / FRAME_MAX) frames (RFC 4944 5.3:
    a datagram that does not fit in a frame is split into fragments,
    each carried in one frame).

    :param size: payload bytes of one packet
    :param packets: packets sent over the link
    :return: lower bound of frames
    """
    return packets * math.ceil(size / FRAME_MAX)


def test_sixlowpan_link_local_address(ifconfig: str, addr: str) -> None:
    """wpan0 has the link-local address derived from its link address.

    RFC 6282 3.2.2: a 16-bit short address XXXX gives the interface
    identifier ``0000:00ff:fe00:XXXX``; RFC 4944 6: the link-local
    address is fe80:: followed by the interface identifier. The packet
    radio loopback uses a 1-byte address, placed in the same position.
    """
    found = _HWADDR_RE.search(ifconfig)
    assert found, ifconfig
    hwaddr = int(found.group(1).replace(":", ""), 16)
    groups = addr.split(":")
    assert groups[0] == "fe80", addr
    tail = groups[-3:]
    assert tail[0] == "ff" and tail[1] == "fe00", addr
    assert int(tail[2], 16) == hwaddr, (addr, found.group(1))


@pytest.mark.parametrize("size", UDP_SIZES)
@pytest.mark.parametrize(
    "port",
    [
        pytest.param(PORT_COMPRESSED, id="port-compressed"),
        pytest.param(PORT_INLINE, id="port-inline"),
    ],
)
def test_sixlowpan_udp_echo(
    request: pytest.FixtureRequest,
    addr: str,
    frames: List[int],
    port: int,
    size: int,
) -> None:
    """UDP datagrams of ``size`` bytes are echoed intact over wpan0.

    POSIX sendto()/recvfrom(): each datagram arrives whole or not at
    all; RFC 4944 5.3: a datagram larger than a frame is fragmented and
    reassembled; RFC 8200 5: packets up to 1280 bytes must get through.
    PASS: every datagram comes back (``lost=0``), and the loopback
    carried at least ceil(size / 127) frames per datagram and direction.
    """
    _loopback_bug(request, size + NETTL_UDP_HDR >= _threshold())
    nettl_server(0, True, port, ipv6=True)
    verdict = nettl_client(0, addr, True, port, UDP_COUNT, size)
    assert verdict == f"nettl: PASS tx={UDP_COUNT} rx={UDP_COUNT} lost=0 err=0"
    _frames_since(frames[0], _min_frames(size, 2 * UDP_COUNT))


def test_sixlowpan_udp_fragment_burst(
    request: pytest.FixtureRequest, addr: str, frames: List[int]
) -> None:
    """50 maximum-size datagrams back to back are all reassembled.

    RFC 4944 5.3: fragments of one datagram share datagram_tag and
    datagram_size; the receiver reassembles each datagram from its own
    fragments. PASS: ``lost=0`` for 50 datagrams of 1228 bytes.
    """
    _burst_hang()
    _loopback_bug(request, True)
    nettl_server(0, True, PORT_COMPRESSED, ipv6=True)
    verdict = nettl_client(0, addr, True, PORT_COMPRESSED, 50, UDP_MAX)
    assert verdict == "nettl: PASS tx=50 rx=50 lost=0 err=0"
    _frames_since(frames[0], _min_frames(UDP_MAX, 100))


def test_sixlowpan_udp_interleaved(
    request: pytest.FixtureRequest, addr: str
) -> None:
    """Two fragmented UDP flows at once are both reassembled.

    RFC 4944 5.3: the datagram_tag together with the link addresses
    identifies the datagram a fragment belongs to, so fragments of
    different datagrams may interleave. PASS: a background client and a
    foreground client, each sending 20 datagrams of 1228 bytes to its
    own server, both report ``lost=0``.
    """
    _burst_hang()
    _loopback_bug(request, True)
    nettl_server(0, True, PORT_COMPRESSED, ipv6=True)
    nettl_server(0, True, PORT_INLINE, ipv6=True)
    core = _core(0)
    client = f"nettl -c {addr} -6 -u -l {UDP_MAX} -n 20"
    out = str(
        core.sendCommandReadUntilPattern(
            f"{client} -p {PORT_COMPRESSED} &", pattern=r"nsh> ", timeout=10
        ).output
    )
    out += str(
        core.sendCommandReadUntilPattern(
            f"{client} -p {PORT_INLINE}", pattern=VERDICT_RE, timeout=120
        ).output
    )
    client_re = r"nettl: (?:PASS|FAIL) tx=[^\r\n]*"
    if len(re.findall(client_re, out)) < 2:
        out += str(core.readUntilPattern(VERDICT_RE, timeout=60).output)
    verdicts = re.findall(client_re, out)
    assert len(verdicts) == 2, out
    assert all(
        v == "nettl: PASS tx=20 rx=20 lost=0 err=0" for v in verdicts
    ), verdicts


def test_sixlowpan_udp_over_mtu(
    request: pytest.FixtureRequest, addr: str
) -> None:
    """A UDP datagram just above the 1280-byte minimum MTU is echoed.

    RFC 8200 5: a node may send packets larger than 1280 bytes on a
    link whose MTU allows it; ``wpan0`` reports mtu 1294. PASS: one
    1236-byte datagram (IPv6 length 1284) comes back, or sendto() fails
    with EMSGSIZE (POSIX). Skipped: the node hangs, see the reason.
    """
    pytest.skip(OVER_MTU_HANG_BUG)
    _loopback_bug(request, True)
    nettl_server(0, True, PORT_INLINE, ipv6=True)
    verdict = nettl_client(0, addr, True, PORT_INLINE, 1, UDP_MAX + 4)
    assert verdict == "nettl: PASS tx=1 rx=1 lost=0 err=0"


@pytest.mark.parametrize(
    "nbytes",
    [
        pytest.param(3000, id="3000"),
        pytest.param(
            4096,
            id="4096",
            marks=pytest.mark.xfail(strict=True, reason=TCP_TAIL_BUG),
        ),
        pytest.param(
            65536,
            id="65536",
            marks=pytest.mark.xfail(strict=True, reason=TCP_TAIL_BUG),
        ),
    ],
)
def test_sixlowpan_tcp_echo(
    request: pytest.FixtureRequest, addr: str, frames: List[int], nbytes: int
) -> None:
    """A TCP stream of ``nbytes`` bytes is echoed intact over wpan0.

    RFC 9293 3.4 / POSIX: the stream arrives complete and in order; the
    segments are larger than a frame, so each is fragmented (RFC 4944
    5.3). nettl writes 2048-byte chunks and reads each back before the
    next. PASS: ``nettl: PASS tx=N rx=N err=0`` and at least
    ceil(N / 127) frames per direction.
    """
    _tcp_send_block()
    _loopback_bug(request, True)
    nettl_server(0, False, 5100, ipv6=True)
    verdict = nettl_client(0, addr, False, 5100, nbytes)
    assert verdict == f"nettl: PASS tx={nbytes} rx={nbytes} err=0"
    _frames_since(frames[0], 2 * math.ceil(nbytes / FRAME_MAX))


@pytest.mark.cmd_check("ping6_main")
@pytest.mark.parametrize("size", PING_SIZES)
def test_sixlowpan_ping6(
    request: pytest.FixtureRequest, addr: str, frames: List[int], size: int
) -> None:
    """ICMPv6 echo requests of ``size`` data bytes are answered over wpan0.

    RFC 4443 4.2: every Echo Request to a node's unicast address gets an
    Echo Reply with the same data. PASS: ``ping6 -c 2 -s size`` reports
    2 received, and at least ceil(size / 127) frames per packet.
    """
    _loopback_bug(request, size + 8 >= _threshold())
    ret = _core(0).sendCommandReadUntilPattern(
        f"ping6 -c 2 -s {size} {addr}", pattern=_PING_RE.pattern, timeout=30
    )
    found = _PING_RE.search(str(ret.output))
    assert found, ret.output
    assert found.group(1) == "2" and found.group(2) == "2", ret.output
    _frames_since(frames[0], _min_frames(size, 4))
