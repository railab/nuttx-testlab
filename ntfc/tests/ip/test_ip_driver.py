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
import subprocess
import time
from typing import Any, Iterator, List, Optional, Tuple

import pytest
from _net_common import (
    NODE_IP6S,
    NODE_IPS,
    host_tcp_echo_check,
    host_udp_echo_check,
    host_udp_flood,
    nettl_client,
    nettl_server,
    pat,
    track_server,
)
from _netleak import IobCountLost, LeakCheck, iob_free

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_TCP", "CONFIG_NET_UDP"),
    pytest.mark.cmd_check("nettl_main"),
]

PEERS = ["host", "node1"]
UDP_HDRS = {"v4": 14 + 20 + 8 + 4, "v6": 14 + 40 + 8 + 4}
FRAME_POINTS = ["min", "min1", "f70", "max1", "max"]
FLOOD_LEN = 1400
BACKPRESSURE_BYTES = 4 << 20
SINK_WAIT = 15
SINK_RCVBUF = 16 << 20
SINK_VERDICT_RE = r"nettl: (PASS|FAIL) rx=[^\r\n]*[\r\n]"

E1000_RX_DROP_BUG = (
    "drivers/net/e1000.c: when no RX packet is free, the drop loop of "
    "e1000_receive() advances rx_now past a descriptor it never gives "
    "back to the NIC on its last pass, so the driver and the NIC lose "
    "step and RX stops"
)
VIRTIO_NET_RXFILL_BUG = (
    "drivers/virtio/virtio-net.c: the RX virtqueue is refilled only in "
    "virtio_net_recv(); once netpkt_alloc() fails the queue runs empty, "
    "no RX interrupt arrives any more and RX never restarts"
)
UDP_READAHEAD_THROTTLE_BUG = (
    "net/udp/udp_callback.c: udp_datahandler() chains RX IOBs into the "
    "read-ahead queue without honoring CONFIG_IOB_THROTTLE, so a flood "
    "empties the pool and the UDP echo server blocks forever in its own "
    "sendto()"
)
IOB_WAIT_TIMEOUT_BUG = (
    "mm/iob/iob_alloc.c: a timed-out iob_allocwait() does not undo its "
    "g_throttle_wait++ / g_iob_count-- reservation, so every timed-out "
    "wait while the pool is empty loses one IOB for good"
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
    bugs: List[Tuple[str, str, Optional[str]]],
) -> None:
    """Mark the test as an expected failure on matching nodes.

    :param request: pytest request
    :param bugs: ``(config, reason, raises)``: node 0 has ``config``
     set; with ``raises="leak"`` only :class:`IobCountLost` is expected
    """
    for config, reason, raises in bugs:
        if _core(0).conf.kv_check(config):
            request.applymarker(
                pytest.mark.xfail(
                    strict=True,
                    reason=reason,
                    raises=IobCountLost if raises == "leak" else None,
                )
            )
            return


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
    _expect_failure(
        request, [("CONFIG_ARCH_SIM", UDP_READAHEAD_THROTTLE_BUG, None)]
    )
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


@pytest.mark.parametrize("peer", PEERS)
def test_drv_rx_exhaustion_recovery(
    request: pytest.FixtureRequest, net_leak: LeakCheck, peer: str
) -> None:
    """The node receives again after RX ran out of I/O buffers.

    A UDP sink whose receive buffer is larger than the IOB pool holds
    every datagram for ``SINK_WAIT`` s while the peer floods it, so the
    driver finds no free RX packet and drops frames.

    :param request: pytest request
    :param net_leak: IOB leak check
    :param peer: ``host`` or ``node1``
    """
    bugs = [
        ("CONFIG_ARCH_SIM", IOB_WAIT_TIMEOUT_BUG, "leak"),
        ("CONFIG_DRIVERS_VIRTIO_NET", VIRTIO_NET_RXFILL_BUG, None),
    ]
    if peer == "host":
        bugs.append(("CONFIG_NET_E1000", E1000_RX_DROP_BUG, None))
    _expect_failure(request, bugs)
    port = 6500
    ret = _core(0).sendCommand(
        f"nettl -s -u -S -D {SINK_WAIT} -R {SINK_RCVBUF} -p {port} -t 5 &",
        "listening",
        timeout=10,
    )
    assert ret == 0
    track_server(NODE_IPS[0], port, True)
    start = time.monotonic()
    if peer == "host":
        assert host_udp_flood(NODE_IPS[0], port, 5.0, FLOOD_LEN) > 0
    else:
        ret = nettl_client(
            1,
            NODE_IPS[0],
            udp=True,
            port=port,
            count=5000,
            length=FLOOD_LEN,
            extra="-r 0",
        )
        assert ret.startswith("nettl: PASS"), ret
    free = iob_free(0)
    assert time.monotonic() - start < SINK_WAIT, "flood outlasted the sink"
    total = int(_core(0).conf.kv_check("CONFIG_IOB_NBUFFERS"))
    assert free < total // 16, f"IOB pool not exhausted: {free} free"
    out = _core(0).readUntilPattern(SINK_VERDICT_RE, timeout=SINK_WAIT + 30)
    found = re.search(SINK_VERDICT_RE, str(out.output))
    assert found and found.group(1) == "PASS", out.output
    assert _host_ping(NODE_IPS[0]), "node 0 does not answer ping"
    nettl_server(0, udp=False, port=6501, addr=NODE_IPS[0])
    assert host_tcp_echo_check(NODE_IPS[0], 6501, 1 << 20, timeout=30)
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
