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

"""iperf throughput between the host (iperf2) and NuttX nodes.

The node side is ``netutils/iperf``. Its server measures from the first
connection or datagram for ``-t`` seconds and then exits, so every
measurement is taken on the receiving node's server, or on the node
client when the host receives. A node server prints its report reliably
only when its ``-t`` window ends before the peer stops sending.
"""

import re
import subprocess
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from _net_common import HOST_IP, NODE_IPS, track_server

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_TCP", "CONFIG_NET_UDP"),
    pytest.mark.cmd_check("iperf_main"),
]

#: Minimum throughput in Mbit/s per ``CONFIG_ARCH``, about a tenth of
#: the lowest measured value.
FLOORS: Dict[str, Dict[str, float]] = {
    "sim": {"node_to_host": 80, "host_to_node": 100, "node_to_node": 2},
    "arm64": {"node_to_host": 60, "host_to_node": 80, "node_to_node": 60},
    "risc-v": {"node_to_host": 50, "host_to_node": 70, "node_to_node": 50},
    "x86_64": {"node_to_host": 70, "host_to_node": 60, "node_to_node": 60},
}

#: Measurement window of the node (seconds).
WINDOW = 5
#: Host UDP sender: offered rate, datagram payload and duration.
UDP_RATE = "10M"
UDP_LEN = 1024
UDP_TIME = 3
#: Maximum UDP loss (fraction of the bytes the host sent).
UDP_MAX_LOSS = 0.05

E1000_RX_PANIC_BUG = (
    "drivers/net/e1000.c: e1000_receive() calls PANIC() when "
    "netpkt_alloc() finds no free IOB to refill the RX ring, so a bulk "
    "TCP transfer that uses up the IOB pool crashes the node"
)

REPORT_RE = re.compile(r"(\d+\.\d+)-\s*(\d+\.\d+) sec\s+(\d+) Bytes")
EXIT_RE = "iperf exit"


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def _floor(name: str) -> float:
    """Return the throughput floor of a test on this target.

    :param name: key in the :data:`FLOORS` entry of the target
    :return: minimum throughput in Mbit/s
    """
    return FLOORS[str(_core(0).conf.kv_check("CONFIG_ARCH"))][name]


@pytest.fixture
def e1000_rx_panic_skip() -> None:
    """Skip bulk TCP on e1000 nodes: the node crashes.

    NTFC reports a crashed device as a failure even for an expected
    failure, so the test is skipped instead of marked ``xfail``.
    """
    if _core(0).conf.kv_check("CONFIG_NET_E1000"):
        pytest.skip(E1000_RX_PANIC_BUG)


def _report(output: str) -> Tuple[float, int]:
    """Return the whole-run report of an iperf run.

    :param output: iperf console output
    :return: seconds and bytes of the report line that starts at 0 s
     and covers the longest time
    """
    totals = [
        (float(end), int(nbytes))
        for start, end, nbytes in REPORT_RE.findall(output)
        if float(start) == 0.0 and float(end) > 0.0
    ]
    assert totals, f"no iperf report in output:\n{output}"
    return max(totals)


def _mbps(output: str) -> float:
    """Return the throughput of the whole-run report.

    :param output: iperf console output
    :return: throughput in Mbit/s
    """
    seconds, nbytes = _report(output)
    return nbytes * 8 / seconds / 1e6


def _check(name: str, mbps: float) -> None:
    """Assert a throughput floor and log the measurement.

    :param name: key in :data:`FLOORS`
    :param mbps: measured throughput in Mbit/s
    """
    floor = _floor(name)
    print(f"iperf {name}: {mbps:.2f} Mbit/s (floor {floor})")
    assert mbps >= floor, f"{name}: {mbps:.2f} < {floor} Mbit/s"


def _node_server(node: int, port: int, udp: bool = False) -> None:
    """Start a background iperf server on a node.

    :param node: product index
    :param port: listen port
    :param udp: UDP instead of TCP
    """
    proto = "-u " if udp else ""
    ret = _core(node).sendCommand(
        f"iperf -s {proto}-B {NODE_IPS[node]} -p {port} -t {WINDOW} -i 1 &",
        "udp-server" if udp else "tcp-server",
        timeout=10,
    )
    assert ret == 0
    track_server(NODE_IPS[node], port, udp)
    time.sleep(1)  # the banner is printed before the socket is bound


def _node_server_report(node: int) -> str:
    """Wait for a background node server to exit.

    :param node: product index
    :return: console output up to ``iperf exit``
    """
    ret = _core(node).readUntilPattern(EXIT_RE, timeout=30)
    assert ret.output and EXIT_RE in ret.output, ret.output
    return ret.output


def _node_client(node: int, addr: str, port: int, seconds: int) -> str:
    """Run an iperf TCP client on a node until it exits.

    :param node: product index
    :param addr: server address
    :param port: server port
    :param seconds: client ``-t``
    :return: console output up to ``iperf exit``
    """
    ret = _core(node).sendCommandReadUntilPattern(
        f"iperf -c {addr} -B {NODE_IPS[node]} -p {port} -t {seconds} -i 1",
        pattern=EXIT_RE,
        timeout=seconds + 30,
    )
    assert EXIT_RE in ret.output, ret.output
    return ret.output


@contextmanager
def _host_iperf(args: List[str]) -> Iterator["subprocess.Popen[bytes]"]:
    """Run host iperf2 in the background.

    :param args: iperf arguments
    :return: running process, stopped on exit
    """
    proc = subprocess.Popen(  # noqa: S603
        ["iperf", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.usefixtures("e1000_rx_panic_skip")
def test_iperf_tcp_node_to_host() -> None:
    """node0 client sends to a host iperf2 server for 5 s."""
    port = 5600
    with _host_iperf(["-s", "-B", HOST_IP, "-p", str(port)]):
        time.sleep(0.5)
        output = _node_client(0, HOST_IP, port, WINDOW)
    _check("node_to_host", _mbps(output))


@pytest.mark.usefixtures("e1000_rx_panic_skip")
def test_iperf_tcp_host_to_node() -> None:
    """A host iperf2 client sends to a node0 server for its 5 s window."""
    port = 5601
    _node_server(0, port)
    args = ["-c", NODE_IPS[0], "-p", str(port), "-t", str(3 * WINDOW)]
    with _host_iperf(args):
        output = _node_server_report(0)
    _check("host_to_node", _mbps(output))


def test_iperf_udp_host_to_node() -> None:
    """A host iperf2 client sends UDP at a fixed rate to node0.

    The node server window outlasts the host's transmission, so it
    counts every datagram that arrived.
    """
    port = 5602
    _node_server(0, port, udp=True)
    host = subprocess.run(  # noqa: S603
        ["iperf", "-c", NODE_IPS[0], "-u", "-p", str(port)]
        + ["-b", UDP_RATE, "-l", str(UDP_LEN), "-t", str(UDP_TIME)]
        + ["--no-udp-fin", "-y", "C"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = [ln for ln in host.stdout.splitlines() if ln.count(",") >= 8]
    assert lines, f"no host iperf report: {host.stdout} {host.stderr}"
    sent = int(lines[-1].split(",")[7])
    received = _report(_node_server_report(0))[1]
    loss = 1 - received / sent
    print(f"iperf udp: sent {sent} received {received} loss {loss:.3f}")
    assert sent > 0
    assert loss <= UDP_MAX_LOSS, f"sent {sent}, received {received}"


@pytest.mark.usefixtures("e1000_rx_panic_skip")
def test_iperf_tcp_node_to_node() -> None:
    """node0 client sends to a node1 server for its 5 s window."""
    port = 5603
    _node_server(1, port)
    _node_client(0, NODE_IPS[1], port, 3 * WINDOW)
    _check("node_to_node", _mbps(_node_server_report(1)))
