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

"""Shared helpers for the router scenario (``testenv/router.sh``).

One NuttX node with two NICs forwards between host A (network namespace
``tl-ra``) and host B (``tl-rb``). Side 0 is A, side 1 is B; tuples
below are indexed by side.
"""

import os
import re
import subprocess
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Tuple

import pytest

NS = ("tl-ra", "tl-rb")
TAPS = ("tl-rtap0", "tl-rtap1")
HOSTS = ("10.51.0.1", "10.52.0.1")
NODES = ("10.51.0.10", "10.52.0.10")
HOSTS6 = ("fc00::1", "fc00::1:1")
NODES6 = ("fc00::10", "fc00::1:10")
MACS = ("52:54:00:33:00:10", "52:54:00:33:00:20")

#: Route of host B back to subnet A (removed while NAT is tested)
B_ROUTE = ("10.51.0.0/24", "via", NODES[1])

IFCONFIG_RE = re.compile(
    r"^(\w+)\s+Link encap:Ethernet HWaddr ([0-9a-fA-F:]{17}) at (\w+)",
    re.MULTILINE,
)


def core() -> Any:
    """Return the NTFC core handler of the router node.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.products[0].core(0)


def nsh(cmd: str, timeout: float = 30) -> str:
    """Run an NSH command and return its output.

    :param cmd: command line
    :param timeout: seconds to wait for the prompt
    :return: console output up to the next prompt
    """
    ret = core().sendCommandReadUntilPattern(
        cmd, pattern=r"nsh> ", timeout=timeout
    )
    return str(ret.output)


def ports() -> List[Tuple[str, str]]:
    """Return the node's Ethernet interfaces.

    :return: (name, status) per side, found by MAC address; status is
     ``RUNNING``, ``UP`` or ``DOWN``, name is empty for a missing NIC
    """
    found = {
        mac.lower(): (name, status)
        for name, mac, status in IFCONFIG_RE.findall(nsh("ifconfig"))
    }
    return [found.get(mac, ("", "")) for mac in MACS]


def configure() -> List[str]:
    """Address both node interfaces and bring them up.

    :return: interface name per side
    """
    names = [name for name, _status in ports()]
    assert all(names), f"router NIC missing: {names}"
    for side, name in enumerate(names):
        nsh(f"ifup {name}")
        nsh(
            f"ifconfig {name} {NODES[side]} dr {HOSTS[side]} "
            "netmask 255.255.255.0"
        )
        nsh(f"ifconfig {name} inet6 {NODES6[side]} prefixlen 112")
    return names


@contextmanager
def netns(side: int) -> Iterator[None]:
    """Run the block (this thread) in a host network namespace.

    Sockets created inside the block stay in that namespace.

    :param side: 0 for host A, 1 for host B
    """
    target = os.open(f"/run/netns/{NS[side]}", os.O_RDONLY)
    home = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
    try:
        os.setns(target, os.CLONE_NEWNET)
        try:
            yield
        finally:
            os.setns(home, os.CLONE_NEWNET)
    finally:
        os.close(target)
        os.close(home)


def ns_run(
    side: int, args: List[str], timeout: float = 60
) -> "subprocess.CompletedProcess[str]":
    """Run a command in a host namespace.

    :param side: 0 for host A, 1 for host B
    :param args: command and arguments
    :param timeout: seconds before the command is killed
    :return: finished process with text output
    """
    return subprocess.run(  # noqa: S603
        ["ip", "netns", "exec", NS[side], *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ns_popen(side: int, args: List[str]) -> "subprocess.Popen[str]":
    """Start a command in a host namespace.

    :param side: 0 for host A, 1 for host B
    :param args: command and arguments
    :return: running process, stdout and stderr piped as text
    """
    return subprocess.Popen(  # noqa: S603
        ["ip", "netns", "exec", NS[side], *args],  # noqa: S607
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop(proc: "subprocess.Popen[str]") -> str:
    """Stop a background process.

    :param proc: process from :func:`ns_popen`
    :return: its output
    """
    if proc.poll() is None:
        proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def host_ping(
    side: int, addr: str, count: int = 3, ttl: Optional[int] = None
) -> "subprocess.CompletedProcess[str]":
    """Ping from a host namespace.

    :param side: 0 for host A, 1 for host B
    :param addr: destination IPv4 or IPv6 address
    :param count: echo requests to send
    :param ttl: IP TTL / hop limit of the requests
    :return: finished ``ping`` process
    """
    args = ["ping", "-n", "-c", str(count), "-i", "0.2", "-W", "2"]
    if ttl is not None:
        args += ["-t", str(ttl)]
    return ns_run(side, [*args, addr])


def ping_ok(side: int, addr: str) -> bool:
    """Ping until replies come back, the first ones only resolve ARP.

    :param side: 0 for host A, 1 for host B
    :param addr: destination address
    :return: True when 3 of 3 echo requests of the last try were answered
    """
    for _attempt in range(3):
        if host_ping(side, addr).returncode == 0:
            return True
    return False


@contextmanager
def iperf_server(side: int, port: int) -> Iterator[None]:
    """Run an iperf2 TCP server on a host for the block.

    :param side: 0 for host A, 1 for host B
    :param port: listen port
    """
    proc = ns_popen(side, ["iperf", "-s", "-p", str(port)])
    try:
        yield
    finally:
        stop(proc)


def iperf_client(
    side: int,
    addr: str,
    port: int,
    seconds: int,
    reverse: bool = False,
) -> "subprocess.Popen[str]":
    """Start an iperf2 TCP client on a host.

    :param side: 0 for host A, 1 for host B
    :param addr: server address
    :param port: server port
    :param seconds: test duration
    :param reverse: the server sends (``-R``)
    :return: running client; pass it to :func:`iperf_mbps`
    """
    args = ["iperf", "-c", addr, "-p", str(port), "-t", str(seconds)]
    args += ["-y", "C"] + (["-R"] if reverse else [])
    return ns_popen(side, args)


def iperf_mbps(proc: "subprocess.Popen[str]", timeout: float) -> float:
    """Wait for an iperf2 client and return its throughput.

    :param proc: client from :func:`iperf_client`
    :param timeout: seconds to wait
    :return: Mbit/s of the whole-run report, 0 when there is none
    """
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        out = stop(proc)
    rows = [ln.split(",") for ln in (out or "").splitlines()]
    rates = [int(row[8]) for row in rows if len(row) >= 9]
    print(f"iperf: {out}")
    return max(rates, default=0) / 1e6


def iptables(args: str, ipv6: bool = False) -> str:
    """Run ``iptables`` (or ``ip6tables``) on the node.

    :param args: arguments
    :param ipv6: use ``ip6tables``
    :return: command output
    """
    return nsh(f"{'ip6tables' if ipv6 else 'iptables'} {args}")


@contextmanager
def masquerade(out_if: str) -> Iterator[None]:
    """NAT everything leaving ``out_if`` (host B side) for the block.

    Host B loses its route back to subnet A, so only translated traffic
    gets answers.

    :param out_if: node interface on the B side
    """
    rule = f"-t nat -A POSTROUTING -o {out_if} -j MASQUERADE"
    iptables(rule)
    ns_run(1, ["ip", "route", "del", *B_ROUTE])
    try:
        yield
    finally:
        ns_run(1, ["ip", "route", "replace", *B_ROUTE])
        iptables(rule.replace(" -A ", " -D "))
