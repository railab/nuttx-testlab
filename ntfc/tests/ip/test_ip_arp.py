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

"""ARP request retransmission, rate limit and resolution failure.

RFC 1122, 2.3.2.1: a host MUST prevent ARP flooding, at most 1 request
per second per destination is recommended.  A lost reply is answered
by the next request; after ``CONFIG_ARP_SEND_MAXTRIES`` unanswered
requests the resolution fails and sending reports ``EHOSTUNREACH``
(POSIX ``sendto()``: "The destination host cannot be reached").

All tests use node1.  The host sniffs ARP on ``tl-br0`` and, where
needed, drops its own ARP replies to node1 with a ``tc`` filter on
``tl-br0`` egress, removed after the test.  10.42.0.250-252 and
fc00::252 are unused addresses on the bridge subnet.
"""

import re
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Iterator, List, Optional, Tuple

import pytest
from _net_common import HOST_IP, NODE_IPS

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_ARP_SEND"),
    pytest.mark.cmd_check("ping_main"),
]

BRIDGE = "tl-br0"
NODE = 1
ETH_P_ARP = 0x0806
ETH_P_IP = 0x0800

#: RFC 1122 recommended maximum request rate: 1 per second per destination.
INTERVAL = 1.0

#: Shortest accepted gap between two requests (``INTERVAL`` minus capture
#: jitter).
MIN_GAP = 0.8

#: Matches ping's sendto() failure line and captures the errno.
SENDTO_RE = r"sendto failed at seqno \d+: (\d+)"

EHOSTUNREACH = 113

ARP_RETRANSMIT_BUG = (
    "nuttx 4ae08155f90: an unanswered ARP request is never retransmitted "
    "(arp_out) or retransmitted 5 times in 100 ms (arp_send), then the "
    "address is unreachable for 10 s (net/arp/arp_table.c arp_find())"
)

RESOLVE_ERRNO_BUG = (
    "nuttx: a failed address resolution is reported as ENETUNREACH, not "
    "EHOSTUNREACH (callers of arp_send() and icmpv6_neighbor())"
)


def _core() -> Any:
    """Return the NTFC core handler of node1.

    :return: ``ProductCore`` for product 1, core 0
    """
    return pytest.products[NODE].core(0)


def _kv_int(name: str, default: int) -> int:
    """Return an integer Kconfig value of node1.

    :param name: Kconfig option name
    :param default: value when the option is not set
    :return: option value
    """
    value = _core().conf.kv_check(name)
    return int(value) if value else default


def _run(*args: str) -> str:
    """Run a host command and return its output.

    :param args: command and arguments
    :return: standard output
    """
    return subprocess.run(
        args, capture_output=True, text=True, check=True
    ).stdout


def _sh(cmd: str, timeout: int = 30) -> str:
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


class ArpCapture:
    """Record the ARP requests node1 sends, seen on ``tl-br0``."""

    def __init__(self) -> None:
        """Open the capture socket and start the receive thread."""
        self.requests: List[Tuple[float, str]] = []
        self.first = threading.Event()  # first request for the host
        self._stop = threading.Event()
        self._sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP)
        )
        self._sock.bind((BRIDGE, 0))
        self._sock.settimeout(0.1)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """Receive frames until stopped."""
        while not self._stop.is_set():
            try:
                frame = self._sock.recv(128)
            except socket.timeout:
                continue
            now = time.time()
            if len(frame) < 42:
                continue
            op, spa, tpa = struct.unpack("!20xH6x4s6x4s", frame[:42])
            if op == 1 and socket.inet_ntoa(spa) == NODE_IPS[NODE]:
                self.requests.append((now, socket.inet_ntoa(tpa)))
                if socket.inet_ntoa(tpa) == HOST_IP:
                    self.first.set()

    def times(self, target: str) -> List[float]:
        """Return the times of the requests for a target address.

        :param target: requested IPv4 address
        :return: capture times (``time.time()``)
        """
        return [t for t, tpa in self.requests if tpa == target]

    def close(self) -> None:
        """Stop the receive thread and close the socket."""
        self._stop.set()
        self._thread.join()
        self._sock.close()


@pytest.fixture
def capture() -> Iterator[ArpCapture]:
    """Capture node1's ARP requests for the duration of the test.

    :return: running :class:`ArpCapture`
    """
    cap = ArpCapture()
    yield cap
    cap.close()


class ReplyDrop:
    """Drop the host's ARP replies to node1 with a ``tc`` filter."""

    def __init__(self) -> None:
        """Install the filter on ``tl-br0`` egress."""
        tpa = socket.inet_aton(NODE_IPS[NODE]).hex()
        subprocess.run(
            ["tc", "qdisc", "del", "dev", BRIDGE, "clsact"],
            capture_output=True,
            check=False,
        )
        _run("tc", "qdisc", "add", "dev", BRIDGE, "clsact")
        self.active = True
        try:
            _run(
                "tc",
                "filter",
                "add",
                "dev",
                BRIDGE,
                "egress",
                "protocol",
                "arp",
                "prio",
                "1",
                "u32",
                "match",
                "u16",
                "0x0002",
                "0xffff",
                "at",
                "6",
                "match",
                "u32",
                f"0x{tpa}",
                "0xffffffff",
                "at",
                "24",
                "action",
                "drop",
            )
        except subprocess.CalledProcessError:
            self.remove()
            raise

    def remove(self) -> None:
        """Remove the filter (idempotent)."""
        if self.active:
            self.active = False
            subprocess.run(
                ["tc", "qdisc", "del", "dev", BRIDGE, "clsact"], check=False
            )

    def remove_after_first(self, cap: ArpCapture, delay: float) -> None:
        """Remove the filter ``delay`` s after node1's first host request.

        A node that sends at most one request per second gets no reply
        to its first request only.

        :param cap: running capture
        :param delay: seconds to keep dropping after the first request
        """

        def run() -> None:
            cap.first.wait(30)
            time.sleep(delay)
            self.remove()

        threading.Thread(target=run, daemon=True).start()


@pytest.fixture
def reply_drop() -> Iterator[ReplyDrop]:
    """Drop the host's ARP replies to node1 until removed.

    :return: installed :class:`ReplyDrop`
    """
    drop = ReplyDrop()
    yield drop
    drop.remove()


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


def _forget_host() -> None:
    """Delete node1's ARP entry for the host."""
    _sh(f"arp -d {HOST_IP}")


@pytest.fixture(autouse=True)
def resolve_host() -> Iterator[None]:
    """Let node1 resolve the host again after the test."""
    yield
    _forget_host()
    _sh(f"ping -c 1 -W 3000 {HOST_IP}")


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


@pytest.mark.xfail(strict=True, reason=ARP_RETRANSMIT_BUG)
@pytest.mark.parametrize("drops", [1, 3])
def test_arp_lost_reply_send(
    capture: ArpCapture, reply_drop: ReplyDrop, host_neigh: str, drops: int
) -> None:
    """A ping whose first ARP replies are lost succeeds after the retries.

    node1 pings the host with no ARP entry for it; the host drops its
    replies for ``drops`` - 0.5 s after the first request (the replies to
    the first ``drops`` requests) and does not ARP node1 itself.  PASS:
    the ping is answered; the requests are 0.8-2 s apart and the answered
    one is request ``drops`` + 1.
    """
    _forget_host()
    reply_drop.remove_after_first(capture, (drops - 0.5) * INTERVAL)
    out = _sh(f"ping -c 1 -W 3000 {HOST_IP}")
    times = capture.times(HOST_IP)
    assert "1 packets transmitted, 1 received" in out, out
    assert len(times) == drops + 1, times
    assert all(MIN_GAP <= gap <= 2 * INTERVAL for gap in _gaps(times)), times


@pytest.mark.xfail(strict=True, reason=ARP_RETRANSMIT_BUG)
def test_arp_lost_reply_forward(
    capture: ArpCapture, reply_drop: ReplyDrop, host_neigh: str
) -> None:
    """Echo replies resume within 2 s after a lost ARP reply.

    The host pings node1 every 0.1 s for 5 s; node1 has no ARP entry
    for the host, so its echo replies go through ``arp_out()``.  The
    host drops its ARP replies for 0.5 s after node1's
    first request.  PASS: the host gets an echo reply within 2 s of that
    request.
    """
    _forget_host()
    reply_drop.remove_after_first(capture, 0.5 * INTERVAL)
    out = subprocess.run(
        [
            "ping",
            "-n",
            "-D",
            "-i",
            "0.1",
            "-w",
            "5",
            "-W",
            "1",
            NODE_IPS[NODE],
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    replies = [
        float(t)
        for t in re.findall(
            r"^\[(\d+\.\d+)\] \d+ bytes from", out, re.MULTILINE
        )
    ]
    times = capture.times(HOST_IP)
    assert times, "node1 sent no ARP request"
    assert replies and replies[0] - times[0] <= 2 * INTERVAL, (times, out)


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

    icmp = struct.pack("!BBHHH", 8, 0, 0, 0x4152, seq) + bytes(32)
    icmp = icmp[:2] + struct.pack("!H", csum(icmp)) + icmp[4:]
    srcip = socket.inet_aton(src)
    dstip = socket.inet_aton(NODE_IPS[NODE])
    iph = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(icmp),
        seq,
        0,
        64,
        1,
        0,
        srcip,
        dstip,
    )
    iph = iph[:10] + struct.pack("!H", csum(iph)) + iph[12:]
    eth = mac + bytes.fromhex("020000002afa") + struct.pack("!H", ETH_P_IP)
    return eth + iph + icmp


def _echo_flood(mac: str, src: str, seconds: float) -> None:
    """Send 100 spoofed echo requests per second to node1.

    :param mac: node1's MAC address
    :param src: IPv4 source address of the requests
    :param seconds: duration
    """
    dst = bytes.fromhex(mac.replace(":", ""))
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as sock:
        sock.bind((BRIDGE, 0))
        end = time.monotonic() + seconds
        seq = 0
        while time.monotonic() < end:
            sock.send(_spoofed_echo(dst, src, seq))
            seq += 1
            time.sleep(0.01)


@pytest.mark.xfail(strict=True, reason=ARP_RETRANSMIT_BUG)
def test_arp_request_rate(capture: ArpCapture, host_neigh: str) -> None:
    """At most one ARP request per second for an unresolvable address.

    node1 pings 10.42.0.250 (``arp_send()``); 0.1 s after its first
    request the host floods node1 for 6 s with echo requests from
    10.42.0.250 (each echo reply is a packet for an unresolved address in
    ``arp_out()``).
    PASS: node1 sent requests for 10.42.0.250, at most
    ``CONFIG_ARP_SEND_MAXTRIES``, at least 0.8 s apart.
    """
    target = "10.42.0.250"
    maxtries = _kv_int("CONFIG_ARP_SEND_MAXTRIES", 5)

    def flood() -> None:
        deadline = time.monotonic() + 10
        while not capture.times(target) and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        _echo_flood(host_neigh, target, 6.0)

    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    _sh(f"ping -c 1 -W 1000 {target}")
    thread.join()
    times = capture.times(target)
    assert times, "node1 sent no ARP request"
    assert len(times) <= maxtries, times
    assert all(gap >= MIN_GAP for gap in _gaps(times)), times


@pytest.mark.xfail(strict=True, reason=ARP_RETRANSMIT_BUG)
def test_arp_unreachable(capture: ArpCapture) -> None:
    """An unanswered address fails with EHOSTUNREACH, then is retried.

    node1 pings 10.42.0.251.  PASS: ping's ``sendto()`` fails with
    ``EHOSTUNREACH`` after ``CONFIG_ARP_SEND_MAXTRIES`` requests at
    least 0.8 s apart; a second ping fails without a request;
    after ``CONFIG_NET_ARP_MAXAGE_UNREACHABLE`` (x 10 s) a third ping
    sends requests again.
    """
    target = "10.42.0.251"
    maxtries = _kv_int("CONFIG_ARP_SEND_MAXTRIES", 5)
    holddown = 10 * _kv_int("CONFIG_NET_ARP_MAXAGE_UNREACHABLE", 1)

    out = _sh(f"ping -c 1 -W 1000 {target}")
    failed = time.time()
    times = capture.times(target)
    assert _ping_errno(out) == EHOSTUNREACH, out
    assert len(times) == maxtries, times
    assert all(gap >= MIN_GAP for gap in _gaps(times)), times

    out = _sh(f"ping -c 1 -W 1000 {target}")
    assert _ping_errno(out) == EHOSTUNREACH, out
    assert len(capture.times(target)) == maxtries, capture.times(target)

    time.sleep(max(0.0, failed + holddown + 1 - time.time()))
    _sh(f"ping -c 1 -W 1000 {target}")
    assert len(capture.times(target)) > maxtries, capture.times(target)


@pytest.mark.xfail(strict=True, reason=RESOLVE_ERRNO_BUG)
@pytest.mark.cmd_check("nettl_main")
def test_arp_unreachable_connect() -> None:
    """``connect()`` to an unanswered address fails with EHOSTUNREACH.

    PASS: ``nettl -c 10.42.0.252`` prints ``connect failed 113``.
    """
    ret = _core().sendCommandReadUntilPattern(
        "nettl -c 10.42.0.252 -p 5600 -n 16",
        pattern=r"nettl: FAIL tx=",
        timeout=60,
    )
    found = re.search(r"nettl: connect failed (\d+)", str(ret.output))
    assert found and int(found.group(1)) == EHOSTUNREACH, ret.output


@pytest.mark.xfail(strict=True, reason=RESOLVE_ERRNO_BUG)
@pytest.mark.dep_config("CONFIG_NET_ICMPv6_NEIGHBOR")
@pytest.mark.cmd_check("ping6_main")
def test_nd_unreachable() -> None:
    """A ping6 to an unanswered address fails with EHOSTUNREACH.

    RFC 4861, 7.2.2: address resolution has failed after the Neighbor
    Solicitation retries.  PASS: ping6's ``sendto()`` to fc00::252
    fails with ``EHOSTUNREACH``.
    """
    out = _sh("ping6 -c 1 -W 1000 fc00::252")
    assert _ping_errno(out) == EHOSTUNREACH, out
