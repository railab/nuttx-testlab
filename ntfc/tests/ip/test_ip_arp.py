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

"""Address resolution (ARP, IPv6 Neighbor Discovery) on node1.

Every assertion comes from the quoted standard:

- RFC 1122, 2.3.2.1: "A mechanism to prevent ARP flooding (repeatedly
  sending an ARP Request for the same IP address, at a high rate) MUST
  be included."  https://www.rfc-editor.org/rfc/rfc1122#section-2.3.2.1
- RFC 4861, 7.2.2: "Retransmissions MUST be rate-limited to at most one
  solicitation per neighbor every RetransTimer milliseconds."
  (RETRANS_TIMER 1,000 milliseconds, section 10).
  https://www.rfc-editor.org/rfc/rfc4861#section-7.2.2
- POSIX connect(): "[ENETUNREACH] No route to the network is present.",
  "[ETIMEDOUT] The attempt to connect timed out before a connection was
  made.", "[EHOSTUNREACH] The destination host cannot be reached
  (probably because the host is down or a remote router cannot reach
  it)." and "If the connection cannot be established immediately and
  O_NONBLOCK is set for the file descriptor for the socket, connect()
  shall fail and set errno to [EINPROGRESS]".
  https://pubs.opengroup.org/onlinepubs/9799919799/functions/connect.html
- POSIX sendto(): the same [EHOSTUNREACH] and [ENETUNREACH] text.
  https://pubs.opengroup.org/onlinepubs/9799919799/functions/sendto.html

An address on node1's subnet that no host answers has a route, so a
failed resolution is not "No route to the network".  10.42.0.250-253
and fc00::252-253 have no host on ``tl-br0``.
"""

import re
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Iterator, List, Optional, Tuple

import pytest
from _host_services import TcpEchoServer
from _net_common import HOST_IP, NODE_IPS

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_ARP_SEND"),
    pytest.mark.cmd_check("ping_main"),
]

BRIDGE = "tl-br0"
NODE = 1
ETH_P_ARP = 0x0806
ETH_P_IP = 0x0800
ETH_P_IPV6 = 0x86DD

#: RFC 4861, section 10: RETRANS_TIMER 1,000 milliseconds.
RETRANS_TIMER = 1.0

#: Capture timestamp jitter allowed on a solicitation gap.
JITTER = 0.1

#: Matches ping's sendto() failure line and captures the errno.
SENDTO_RE = r"sendto failed at seqno \d+: (\d+)"

ENETUNREACH = 101
ETIMEDOUT = 110
EHOSTUNREACH = 113
EINPROGRESS = 115

RESOLVE_ERRNO_BUG = (
    "net/icmp/icmp_sendmsg.c, net/icmpv6/icmpv6_sendmsg.c, "
    "net/tcp/tcp_conn.c tcp_connect(): a failed address resolution of an "
    'address on the local subnet is returned as ENETUNREACH ("No route '
    'to the network is present")'
)
NONBLOCK_CONNECT_BUG = (
    "net/tcp/tcp_conn.c tcp_connect(): connect() resolves the address "
    "with arp_send()/icmpv6_neighbor() before sending the SYN, also on a "
    "nonblocking socket, and fails with ENETUNREACH instead of "
    "EINPROGRESS"
)
ND_RATE_BUG = (
    "net/icmpv6/icmpv6_neighbor.c: Neighbor Solicitations are "
    "retransmitted every CONFIG_ICMPv6_NEIGHBOR_DELAYMSEC (default 20 ms), "
    "not at most once per RetransTimer (RFC 4861 RETRANS_TIMER 1 s)"
)


def _core() -> Any:
    """Return the NTFC core handler of node1.

    :return: ``ProductCore`` for product 1, core 0
    """
    return pytest.products[NODE].core(0)


def _run(*args: str) -> str:
    """Run a host command and return its output.

    :param args: command and arguments
    :return: standard output
    """
    return subprocess.run(
        args, capture_output=True, text=True, check=True
    ).stdout


def _sh(cmd: str, timeout: int = 60) -> str:
    """Run an nsh command on node1 and return its output.

    :param cmd: nsh command line
    :param timeout: seconds to wait for the next prompt
    :return: command output
    """
    ret = _core().sendCommandReadUntilPattern(
        cmd, pattern=r"nsh> ", timeout=timeout
    )
    return str(ret.output)


def _node_mac() -> str:
    """Return node1's MAC address, as learnt by the host.

    :return: MAC address, e.g. ``42:e1:c4:3f:48:dd``
    """
    subprocess.run(
        [
            "ping",
            "-n",
            "-q",
            "-c",
            "2",
            "-i",
            "0.2",
            "-W",
            "1",
            NODE_IPS[NODE],
        ],
        capture_output=True,
        check=False,
    )
    out = _run("ip", "neigh", "show", NODE_IPS[NODE], "dev", BRIDGE)
    found = re.search(r"lladdr ([0-9a-f:]{17})", out)
    assert found, out
    return found.group(1)


class Capture:
    """Record node1's ARP requests and Neighbor Solicitations on tl-br0."""

    def __init__(self) -> None:
        """Open the capture socket and start the receive thread."""
        self.requests: List[Tuple[float, str]] = []
        self._stop = threading.Event()
        self._sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)
        )
        self._sock.bind((BRIDGE, 0))
        self._sock.settimeout(0.1)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _record(self, frame: bytes) -> Optional[str]:
        """Return the target of an ARP request or NS from node1.

        :param frame: Ethernet frame
        :return: target address, or None for other frames
        """
        (etype,) = struct.unpack("!H", frame[12:14])
        if etype == ETH_P_ARP and len(frame) >= 42:
            op, spa, tpa = struct.unpack("!20xH6x4s6x4s", frame[:42])
            if op == 1 and socket.inet_ntoa(spa) == NODE_IPS[NODE]:
                return socket.inet_ntoa(tpa)
        elif etype == ETH_P_IPV6 and len(frame) >= 78:
            if frame[20] == 58 and frame[54] == 135:
                return socket.inet_ntop(socket.AF_INET6, frame[62:78])
        return None

    def _loop(self) -> None:
        """Receive frames until stopped."""
        while not self._stop.is_set():
            try:
                frame, addr = self._sock.recvfrom(256)
            except socket.timeout:
                continue
            if addr[2] != socket.PACKET_OUTGOING:
                target = self._record(frame)
                if target is not None:
                    self.requests.append((time.time(), target))

    def times(self, target: str) -> List[float]:
        """Return the times of the requests for a target address.

        :param target: requested address
        :return: capture times (``time.time()``)
        """
        return [t for t, tpa in self.requests if tpa == target]

    def close(self) -> None:
        """Stop the receive thread and close the socket."""
        self._stop.set()
        self._thread.join()
        self._sock.close()


@pytest.fixture
def capture() -> Iterator[Capture]:
    """Capture node1's ARP requests and NSs for the duration of the test.

    :return: running :class:`Capture`
    """
    cap = Capture()
    yield cap
    cap.close()


@pytest.fixture
def host_neigh() -> Iterator[str]:
    """Pin node1's MAC in the host neighbor table (no host ARP).

    :return: node1's MAC address
    """
    mac = _node_mac()
    _run(
        "ip",
        "neigh",
        "replace",
        NODE_IPS[NODE],
        "lladdr",
        mac,
        "dev",
        BRIDGE,
        "nud",
        "permanent",
    )
    yield mac
    subprocess.run(
        ["ip", "neigh", "del", NODE_IPS[NODE], "dev", BRIDGE], check=False
    )


def _gaps(times: List[float]) -> List[float]:
    """Return the gaps between consecutive times.

    :param times: sorted times
    :return: differences in seconds
    """
    return [b - a for a, b in zip(times, times[1:])]


def _ping_errno(out: str) -> Optional[int]:
    """Return the errno of a failed ping sendto(), None if it worked.

    :param out: ping output
    :return: errno or None
    """
    found = re.search(SENDTO_RE, out)
    return int(found.group(1)) if found else None


def _spoofed_echo(mac: bytes, src: str, seq: int) -> bytes:
    """Build an Ethernet frame with an ICMP echo request to node1.

    :param mac: node1's MAC address
    :param src: IPv4 source address (an unresolvable one)
    :param seq: ICMP sequence number
    :return: frame
    """

    def csum(data: bytes) -> int:
        total = sum(struct.unpack(f"!{len(data) // 2}H", data))
        total = (total >> 16) + (total & 0xFFFF)
        total += total >> 16
        return ~total & 0xFFFF

    icmp = struct.pack("!BBHHH", 8, 0, 0, 0x4152, seq & 0xFFFF) + bytes(32)
    icmp = icmp[:2] + struct.pack("!H", csum(icmp)) + icmp[4:]
    iph = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(icmp),
        seq & 0xFFFF,
        0,
        64,
        1,
        0,
        socket.inet_aton(src),
        socket.inet_aton(NODE_IPS[NODE]),
    )
    iph = iph[:10] + struct.pack("!H", csum(iph)) + iph[12:]
    eth = mac + bytes.fromhex("020000002afa") + struct.pack("!H", ETH_P_IP)
    return eth + iph + icmp


def _echo_flood(mac: str, src: str, seconds: float) -> int:
    """Send 100 spoofed echo requests per second to node1.

    :param mac: node1's MAC address
    :param src: IPv4 source address of the requests
    :param seconds: duration
    :return: requests sent
    """
    dst = bytes.fromhex(mac.replace(":", ""))
    seq = 0
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as sock:
        sock.bind((BRIDGE, 0))
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sock.send(_spoofed_echo(dst, src, seq))
            seq += 1
            time.sleep(0.01)
    return seq


def test_arp_no_flood(capture: Capture, host_neigh: str) -> None:
    """ARP requests are not sent at the rate of the outgoing packets.

    RFC 1122, 2.3.2.1: a mechanism to prevent "repeatedly sending an ARP
    Request for the same IP address, at a high rate" MUST be included.
    The host sends node1 100 echo requests per second for 6 s from
    10.42.0.250, which nobody answers; each echo reply needs ARP.
    PASS: node1 sent fewer requests for 10.42.0.250 than one per 10
    echo requests (one request per packet is flooding).
    """
    target = "10.42.0.250"
    sent = _echo_flood(host_neigh, target, 6.0)
    time.sleep(1.0)
    times = capture.times(target)
    assert times, "node1 sent no ARP request"
    assert len(times) < sent / 10, (len(times), sent)


@pytest.mark.xfail(strict=True, reason=RESOLVE_ERRNO_BUG)
def test_arp_unreachable(capture: Capture) -> None:
    """A ping to an address that never answers ARP fails with EHOSTUNREACH.

    POSIX sendto(): "[EHOSTUNREACH] The destination host cannot be
    reached"; "[ENETUNREACH] No route to the network is present." does
    not apply, 10.42.0.251 is on node1's subnet.
    PASS: node1 sent ARP requests for 10.42.0.251 and ping's sendto()
    failed with ``EHOSTUNREACH``.
    """
    target = "10.42.0.251"
    out = _sh(f"ping -c 1 -W 1000 {target}")
    assert capture.times(target), "node1 sent no ARP request"
    assert _ping_errno(out) == EHOSTUNREACH, out


@pytest.mark.xfail(strict=True, reason=RESOLVE_ERRNO_BUG)
@pytest.mark.cmd_check("nettl_main")
def test_arp_unreachable_connect() -> None:
    """A blocking connect() to an unanswered address fails, not ENETUNREACH.

    POSIX connect(): [ETIMEDOUT] "The attempt to connect timed out before
    a connection was made." or [EHOSTUNREACH] "The destination host
    cannot be reached"; [ENETUNREACH] "No route to the network is
    present." does not apply, 10.42.0.252 is on node1's subnet.
    PASS: ``nettl -c 10.42.0.252`` prints ``connect failed`` with
    ``ETIMEDOUT`` or ``EHOSTUNREACH``.
    """
    ret = _core().sendCommandReadUntilPattern(
        "nettl -c 10.42.0.252 -p 5600 -n 16",
        pattern=r"nettl: FAIL tx=",
        timeout=180,
    )
    found = re.search(r"nettl: connect failed (\d+)", str(ret.output))
    assert found, ret.output
    assert int(found.group(1)) in (ETIMEDOUT, EHOSTUNREACH), ret.output


@pytest.mark.xfail(strict=True, reason=NONBLOCK_CONNECT_BUG)
@pytest.mark.cmd_check("nettl_main")
def test_arp_unreachable_connect_nonblock() -> None:
    """A nonblocking connect() to an unresolved address returns EINPROGRESS.

    POSIX connect(): "If the connection cannot be established immediately
    and O_NONBLOCK is set for the file descriptor for the socket,
    connect() shall fail and set errno to [EINPROGRESS], but the
    connection request shall not be aborted".  The attempt then fails
    as the blocking one does.
    PASS: ``nettl -N -c 10.42.0.253`` prints ``connect returned -1
    errno 115`` and the poll result has ``SO_ERROR`` ``ETIMEDOUT`` or
    ``EHOSTUNREACH``.
    """
    ret = _core().sendCommandReadUntilPattern(
        "nettl -N -c 10.42.0.253 -p 5600 -t 120",
        pattern=r"nettl: (PASS|FAIL) tx=",
        timeout=180,
    )
    out = str(ret.output)
    assert f"connect returned -1 errno {EINPROGRESS}" in out, out
    found = re.search(r"so_error (\d+)", out)
    assert found, out
    assert int(found.group(1)) in (ETIMEDOUT, EHOSTUNREACH), out


@pytest.mark.cmd_check("nettl_main")
@pytest.mark.parametrize("cached", [True, False], ids=["cached", "unresolved"])
def test_arp_connect_nonblock(cached: bool) -> None:
    """A nonblocking connect() to the host is established asynchronously.

    POSIX connect(): "If the connection cannot be established immediately
    and O_NONBLOCK is set for the file descriptor for the socket,
    connect() shall fail and set errno to [EINPROGRESS], but the
    connection request shall not be aborted, and the connection shall be
    established asynchronously."  With the host in node1's ARP table and
    after its entry was deleted.
    PASS: ``nettl -N -c <host>`` connects (``SO_ERROR`` 0).

    :param cached: keep node1's ARP entry for the host
    """
    port = 5601 if cached else 5602
    _sh(f"ping -c 1 -W 3000 {HOST_IP}")
    if not cached:
        _sh(f"arp -d {HOST_IP}")
    with TcpEchoServer(HOST_IP, port):
        ret = _core().sendCommandReadUntilPattern(
            f"nettl -N -c {HOST_IP} -p {port} -t 60",
            pattern=r"nettl: (PASS|FAIL) tx=",
            timeout=90,
        )
    out = str(ret.output)
    assert "nettl: PASS" in out, out


@pytest.mark.xfail(strict=True, reason=RESOLVE_ERRNO_BUG)
@pytest.mark.dep_config("CONFIG_NET_ICMPv6_NEIGHBOR")
@pytest.mark.cmd_check("ping6_main")
def test_nd_unreachable() -> None:
    """A ping6 to an address that never answers NS fails with EHOSTUNREACH.

    RFC 4861, 7.2.2: "If no Neighbor Advertisement is received after
    MAX_MULTICAST_SOLICIT solicitations, address resolution has failed.
    The sender MUST return ICMP destination unreachable indications with
    code 3 (Address Unreachable)"; for sendto() that is POSIX
    "[EHOSTUNREACH] The destination host cannot be reached".
    PASS: ping6's ``sendto()`` to fc00::252 fails with ``EHOSTUNREACH``.
    """
    out = _sh("ping6 -c 1 -W 1000 fc00::252")
    assert _ping_errno(out) == EHOSTUNREACH, out


@pytest.mark.xfail(strict=True, reason=ND_RATE_BUG)
@pytest.mark.dep_config("CONFIG_NET_ICMPv6_NEIGHBOR")
@pytest.mark.cmd_check("ping6_main")
def test_nd_solicit_rate(capture: Capture) -> None:
    """Neighbor Solicitations for one address are at least 1 s apart.

    RFC 4861, 7.2.2: "Retransmissions MUST be rate-limited to at most one
    solicitation per neighbor every RetransTimer milliseconds", section
    10: "RETRANS_TIMER 1,000 milliseconds" (the host sends no Router
    Advertisement that changes it).
    PASS: node1's NSs for fc00::253 are ``RETRANS_TIMER`` or more apart
    (``JITTER`` allowed for the capture timestamps).
    """
    target = "fc00::253"
    _sh(f"ping6 -c 1 -W 1000 {target}")
    times = capture.times(target)
    assert times, "node1 sent no Neighbor Solicitation"
    gaps = _gaps(times)
    assert all(gap >= RETRANS_TIMER - JITTER for gap in gaps), gaps
