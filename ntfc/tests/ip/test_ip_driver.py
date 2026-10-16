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

"""Network driver tests: frame sizes, duplex, overload, backpressure."""

import re
import socket
import struct
import subprocess
import time
from typing import Any, Iterator, Tuple

import pytest
from _net_common import (
    NODE_IP6S,
    NODE_IPS,
    UDP_END,
    family,
    host_tcp_echo_check,
    host_udp_echo_check,
    host_udp_flood,
    nettl_client,
    nettl_server,
    pat,
    track_server,
)
from _netleak import LeakCheck, iob_free

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_TCP", "CONFIG_NET_UDP"),
    pytest.mark.cmd_check("nettl_main"),
]

PEERS = ["host", "node1"]
UDP_HDRS = {"v4": 14 + 20 + 8 + 4, "v6": 14 + 40 + 8 + 4}
FRAME_POINTS = ["min", "min1", "f70", "max1", "max"]
FLOOD_LEN = 1400
FLOOD_COUNT = 20000
UDP_END_MARKERS = 3
BACKPRESSURE_BYTES = 4 << 20
SINK_WAIT = 15
SINK_RCVBUF = 16 << 20
SINK_SMALL = 8
NETTL_HDR = 4
RX_VERDICT_RE = r"nettl: (PASS|FAIL) rx=(\d+) err=(\d+)[\r\n]"
PARK_COUNT = 50
PARK_LEN = 64

UDP_READAHEAD_THROTTLE_BUG = (
    "net/udp/udp_callback.c: udp_datahandler() chains RX IOBs into the "
    "read-ahead queue without honoring CONFIG_IOB_THROTTLE, so a flood "
    "empties the pool and blocks throttled writers such as sendto()"
)


@pytest.fixture(autouse=True)
def reboot_after_failure(request: pytest.FixtureRequest) -> Iterator[None]:
    """Reboot the nodes after a failed or expected-to-fail test.

    A driver that stopped receiving would fail every later test.

    :param request: pytest request
    """
    yield
    report = getattr(request.node, "rep_call", None)
    if report is not None and (report.failed or hasattr(report, "wasxfail")):
        for product in pytest.products:
            assert product.reboot(), f"{product.name}: reboot failed"


def _expect_failure(
    request: pytest.FixtureRequest,
    applies: bool,
    reason: str,
) -> None:
    """Mark the test as an expected failure.

    :param request: pytest request
    :param applies: apply the mark
    :param reason: the defect that makes the test fail
    """
    if applies:
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def _frame_len(fam: str, point: str) -> int:
    """Return the Ethernet frame length (no FCS) for a size point.

    :param fam: ``v4`` or ``v6``
    :param point: one of ``FRAME_POINTS``
    :return: frame length in bytes
    """
    top = int(_core(0).conf.kv_check("CONFIG_NET_ETH_PKTSIZE"))
    low = max(60, UDP_HDRS[fam] + 1)
    return {
        "min": low,
        "min1": low + 1,
        "f70": 70,
        "max1": top - 1,
        "max": top,
    }[point]


@pytest.mark.parametrize("point", FRAME_POINTS)
@pytest.mark.parametrize("fam", ["v4", "v6"])
@pytest.mark.parametrize("peer", PEERS)
def test_drv_frame_sizes(
    net_leak: LeakCheck, peer: str, fam: str, point: str
) -> None:
    """UDP echo at Ethernet frame size boundaries is byte-exact.

    :param net_leak: IOB leak check
    :param peer: ``host`` or ``node1``
    :param fam: ``v4`` or ``v6``
    :param point: frame size point
    """
    length = _frame_len(fam, point) - UDP_HDRS[fam]
    addr = NODE_IPS[0] if fam == "v4" else NODE_IP6S[0]
    port = 6100 + FRAME_POINTS.index(point) + (10 if fam == "v6" else 0)
    nettl_server(0, udp=True, port=port, addr=addr, ipv6=fam == "v6")
    if peer == "host":
        assert host_udp_echo_check(addr, port, 200, length=length)
    else:
        ret = nettl_client(
            1, addr, udp=True, port=port, count=200, length=length
        )
        assert ret.startswith("nettl: PASS"), ret
    net_leak.check([0] if peer == "host" else [0, 1])


@pytest.mark.parametrize("peer", PEERS)
def test_drv_rx_overload_recovery(
    request: pytest.FixtureRequest, net_leak: LeakCheck, peer: str
) -> None:
    """The node survives a UDP flood and then carries TCP normally.

    :param request: pytest request
    :param net_leak: IOB leak check
    :param peer: ``host`` or ``node1``
    """
    unbounded = _iob_conf("CONFIG_NET_RECV_BUFSIZE") == 0
    _expect_failure(request, unbounded, UDP_READAHEAD_THROTTLE_BUG)
    nettl_server(0, udp=True, port=6300, addr=NODE_IPS[0])
    if peer == "host":
        assert host_udp_flood(NODE_IPS[0], 6300, 10.0, FLOOD_LEN) > 0
    else:
        ret = nettl_client(
            1,
            NODE_IPS[0],
            udp=True,
            port=6300,
            count=20000,
            length=FLOOD_LEN,
            extra="-r 0",
        )
        assert ret.startswith("nettl: PASS"), ret
    assert _core(0).sendCommand("echo alive", "alive", timeout=10) == 0
    nettl_server(0, udp=False, port=6301, addr=NODE_IPS[0])
    assert host_tcp_echo_check(NODE_IPS[0], 6301, 1 << 20, timeout=30)
    net_leak.check([0] if peer == "host" else [0, 1])


def _host_ping(addr: str) -> bool:
    """Ping a node from the host.

    :param addr: node IPv4 address
    :return: True when at least one echo request was answered
    """
    ret = subprocess.run(  # noqa: S603, S607
        ["ping", "-n", "-q", "-c", "5", "-i", "0.2", "-W", "1", addr],
        capture_output=True,
        check=False,
    )
    return ret.returncode == 0


def _udp_stats(node: int) -> Tuple[int, int]:
    """Return the UDP received and dropped counters of a node.

    :param node: product index
    :return: ``(received, dropped)`` from ``/proc/net/stat`` (16 bit)
    """
    out = str(
        _core(node)
        .sendCommandReadUntilPattern(
            "cat /proc/net/stat", pattern=r"nsh> ", timeout=10
        )
        .output
    )
    lines = out.replace("\r", "").split("\n")
    hdr = next(ln for ln in lines if " UDP" in ln and "IPv4" in ln).split()
    col = hdr.index("UDP") + 1
    rcv = next(ln for ln in lines if ln.startswith("Received")).split()
    drp = next(ln for ln in lines if ln.startswith("Dropped")).split()
    return int(rcv[col], 16), int(drp[col], 16)


def _host_udp_burst(addr: str, port: int, count: int, length: int) -> None:
    """Send ``count`` nettl datagrams as fast as possible.

    :param addr: node address
    :param port: node port
    :param count: number of datagrams
    :param length: payload length
    """
    with socket.socket(family(addr), socket.SOCK_DGRAM) as sock:
        for seq in range(count):
            dgram = struct.pack(">I", seq) + pat(seq * length, length)
            sock.sendto(dgram, (addr, port))


def _rx_verdict(timeout: float) -> "re.Match[str]":
    """Wait for the next nettl receiver verdict line on node 0.

    :param timeout: seconds to wait
    :return: match of ``RX_VERDICT_RE``
    """
    out = _core(0).readUntilPattern(RX_VERDICT_RE, timeout=timeout)
    found = re.search(RX_VERDICT_RE, str(out.output))
    assert found, out.output
    return found


def _sink_limit(limit: str) -> int:
    """Return the sink's ``SO_RCVBUF`` for a limit case.

    :param limit: ``pool`` (larger than the IOB pool) or ``rcvbuf``
    :return: ``SO_RCVBUF`` in bytes
    """
    if limit == "pool":
        return SINK_RCVBUF
    if _iob_conf("CONFIG_NET_RECV_BUFSIZE") == 0:
        pytest.skip("no SO_RCVBUF without CONFIG_NET_RECV_BUFSIZE")
    return SINK_SMALL * (FLOOD_LEN + NETTL_HDR)


def _flood_sink(peer: str, port: int) -> None:
    """Flood the node 0 sink from the host or from node 1.

    :param peer: ``host`` or ``node1``
    :param port: sink port
    """
    if peer == "host":
        _host_udp_burst(NODE_IPS[0], port, FLOOD_COUNT, FLOOD_LEN)
        return
    ret = nettl_client(
        1,
        NODE_IPS[0],
        udp=True,
        port=port,
        count=FLOOD_COUNT // 4,
        length=FLOOD_LEN,
        extra="-r 0",
    )
    assert ret.startswith("nettl: PASS"), ret


@pytest.mark.parametrize("limit", ["pool", "rcvbuf"])
@pytest.mark.parametrize("peer", PEERS)
def test_drv_rx_exhaustion_recovery(
    request: pytest.FixtureRequest, net_leak: LeakCheck, peer: str, limit: str
) -> None:
    """A flood into a UDP socket that does not read keeps the IOB reserve.

    The sink does not read for ``SINK_WAIT`` s; its ``SO_RCVBUF`` is
    larger than the IOB pool (``pool``) or a few datagrams (``rcvbuf``).
    Its queue stays within that limit and its read-ahead leaves at least
    ``CONFIG_IOB_THROTTLE`` IOBs free; every other datagram is dropped
    and counted. Meanwhile the node answers ping; then the sink reads
    intact data and the node carries TCP.

    :param request: pytest request
    :param net_leak: IOB leak check
    :param peer: ``host`` or ``node1``
    :param limit: ``pool`` or ``rcvbuf``
    """
    thr = _iob_conf("CONFIG_IOB_THROTTLE")
    if thr == 0:
        pytest.skip("CONFIG_IOB_THROTTLE is 0")
    rcvbuf = _sink_limit(limit)
    _expect_failure(request, limit == "pool", UDP_READAHEAD_THROTTLE_BUG)
    port = 6500
    rcv0, drop0 = _udp_stats(0)
    ret = _core(0).sendCommand(
        f"nettl -s -u -S -D {SINK_WAIT} -R {rcvbuf} -p {port} -t 5 &",
        "listening",
        timeout=10,
    )
    assert ret == 0
    track_server(NODE_IPS[0], port, True)
    start = time.monotonic()
    _flood_sink(peer, port)
    alive = _host_ping(NODE_IPS[0])
    rcv1, drop1 = _udp_stats(0)
    free = iob_free(0)
    assert time.monotonic() - start < SINK_WAIT, "flood outlasted the sink"
    assert free >= thr, f"read-ahead took the IOB reserve: {free} free"
    assert alive, "node 0 does not answer ping while the sink holds data"
    found = _rx_verdict(SINK_WAIT + 30)
    assert found.group(1) == "PASS", found.group(0)
    queued = int(found.group(2))
    rcvd = (rcv1 - rcv0) & 0xFFFF
    drop = (drop1 - drop0) & 0xFFFF
    assert (queued - 1) * (FLOOD_LEN + NETTL_HDR) <= rcvbuf, queued
    assert drop > 0, f"no UDP drops counted: {rcvd} received"
    left = rcvd - queued - drop
    assert 0 <= left <= UDP_END_MARKERS, (rcvd, found.group(0), drop)
    nettl_server(0, udp=False, port=port + 1, addr=NODE_IPS[0])
    assert host_tcp_echo_check(NODE_IPS[0], port + 1, 1 << 20, timeout=30)
    net_leak.check([0] if peer == "host" else [0, 1])


def test_drv_tx_backpressure(net_leak: LeakCheck) -> None:
    """A node sending to a slow reader delivers every byte in order.

    :param net_leak: IOB leak check
    """
    port = 6400
    ret = _core(0).sendCommand(
        f"nettl -s -w -p {port} -n {BACKPRESSURE_BYTES} -t 60 &",
        "listening",
        timeout=10,
    )
    assert ret == 0
    track_server(NODE_IPS[0], port, False)
    rx = 0
    bad = 0
    with socket.create_connection((NODE_IPS[0], port), timeout=30) as sock:
        while rx < BACKPRESSURE_BYTES:
            data = sock.recv(65536)
            if not data:
                break
            bad += data != pat(rx, len(data))
            rx += len(data)
            time.sleep(0.2 * len(data) / 65536)
    assert (rx, bad) == (BACKPRESSURE_BYTES, 0)
    net_leak.check([0])


def _iob_conf(name: str) -> int:
    """Return an integer IOB Kconfig value of node 0.

    :param name: Kconfig symbol
    :return: its value, 0 when not set
    """
    return int(_core(0).conf.kv_check(name) or 0)


def _send_paced(addr: str, port: int, count: int, length: int) -> None:
    """Send nettl datagrams 20 ms apart, then the end marker.

    :param addr: node address
    :param port: node port
    :param count: number of datagrams
    :param length: payload length
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for seq in range(count):
            dgram = struct.pack(">I", seq) + pat(seq * length, length)
            sock.sendto(dgram, (addr, port))
            time.sleep(0.02)
        time.sleep(0.5)
        for _ in range(3):
            sock.sendto(struct.pack(">I", UDP_END), (addr, port))


def _park_pool(sock: socket.socket, thr: int) -> None:
    """Wait until node 0 holds its IOB pool at the throttle level.

    :param sock: host end of a TCP connection that is not read
    :param thr: ``CONFIG_IOB_THROTTLE``
    """
    deadline = time.monotonic() + 30
    free = iob_free(0)
    while free > thr and time.monotonic() < deadline:
        time.sleep(1)
        free = iob_free(0)
    assert free <= thr, f"TCP writes left {free} IOBs free"


def _drain(sock: socket.socket, nbytes: int) -> None:
    """Read and verify the whole stream of a node TCP writer.

    :param sock: host end of the TCP connection
    :param nbytes: bytes the node sends
    """
    rx = 0
    bad = 0
    sock.settimeout(30)
    while rx < nbytes:
        data = sock.recv(65536)
        if not data:
            break
        bad += data != pat(rx, len(data))
        rx += len(data)
    assert (rx, bad) == (nbytes, 0)


@pytest.mark.parametrize("mode", ["poll", "blocking"])
def test_drv_udp_rx_at_iob_throttle(net_leak: LeakCheck, mode: str) -> None:
    """A UDP socket receives while TCP writes hold the IOB pool.

    TCP write buffering may use the pool down to ``CONFIG_IOB_THROTTLE``
    free IOBs. A UDP socket with an empty receive queue must still get
    each datagram, whether it waits in ``poll()`` or in ``recv()``.

    :param net_leak: IOB leak check
    :param mode: ``poll`` (``nettl -L 1``) or ``blocking`` (``nettl -S``)
    """
    thr = _iob_conf("CONFIG_IOB_THROTTLE")
    if thr == 0:
        pytest.skip("CONFIG_IOB_THROTTLE is 0")
    nbytes = (
        2 * _iob_conf("CONFIG_IOB_NBUFFERS") * _iob_conf("CONFIG_IOB_BUFSIZE")
    )
    wport = 6600 if mode == "poll" else 6602
    uport = wport + 1
    ret = _core(0).sendCommand(
        f"nettl -s -w -p {wport} -n {nbytes} -t 60 &",
        "listening",
        timeout=10,
    )
    assert ret == 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.settimeout(10)
        sock.connect((NODE_IPS[0], wport))
        _park_pool(sock, thr)
        opts = (
            f"-L 1 -n {PARK_COUNT} -l {PARK_LEN}" if mode == "poll" else "-S"
        )
        ret = _core(0).sendCommand(
            f"nettl -s -u {opts} -p {uport} -t 15 &",
            "listening",
            timeout=10,
        )
        assert ret == 0
        track_server(NODE_IPS[0], uport, True)
        _send_paced(NODE_IPS[0], uport, PARK_COUNT, PARK_LEN)
        out = _core(0).readUntilPattern(RX_VERDICT_RE, timeout=30)
        found = re.search(RX_VERDICT_RE, str(out.output))
        free = iob_free(0)
        _drain(sock, nbytes)
    assert found, out.output
    assert (found.group(2), found.group(3)) == (str(PARK_COUNT), "0"), (
        found.group(0),
        f"{free} IOBs free",
    )
    net_leak.check([0])
