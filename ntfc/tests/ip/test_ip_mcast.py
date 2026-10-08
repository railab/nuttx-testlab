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

"""Multicast (IGMP/MLD) on the ip-pair bridge.

The host bridge is the IGMP/MLD querier while these tests run: it sends a
General Query every 5 s and drops a membership after 15 s without a
report (timers set by testenv/ip-pair.sh). Its group table
(``bridge mdb``) shows what the nodes reported.
"""

import json
import socket
import struct
import subprocess
import time
from typing import Iterator

import pytest
from _net_common import (
    NODE_IP6S,
    NODE_IPS,
    host_udp_echo_check,
    nettl_client,
    nettl_server,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_UDP"),
    pytest.mark.cmd_check("nettl_main"),
]

BRIDGE = "tl-br0"
GROUPS = {False: "239.42.0.1", True: "ff12::42"}
MEMBERSHIP_S = 15  # mcast_membership_interval in testenv/ip-pair.sh

MLD_REPORT_BUG = (
    "net/mld/mld_send.c sends MLDv2 reports with MODE_IS_INCLUDE and no "
    "sources, which means not listening (RFC 3810)"
)

IGMP_REJOIN_BUG = (
    "net/igmp/igmp_input.c creates a group for a Group-Specific Query "
    "of a group the host left, and reports it again (timing dependent)"
)

FAMILIES = [
    pytest.param(False, id="v4"),
    pytest.param(
        True,
        id="v6",
        marks=pytest.mark.xfail(strict=True, reason=MLD_REPORT_BUG),
    ),
]


def _querier(on: bool) -> None:
    """Turn the host bridge IGMP/MLD querier on or off.

    :param on: querier state
    """
    subprocess.run(
        ["ip", "link", "set", BRIDGE, "type", "bridge", "mcast_querier"]
        + ["1" if on else "0"],
        check=True,
    )


LIFETIME_FAMILIES = [
    pytest.param(
        False,
        id="v4",
        marks=pytest.mark.xfail(strict=False, reason=IGMP_REJOIN_BUG),
    ),
    FAMILIES[1],
]


@pytest.fixture(scope="module", autouse=True)
def querier() -> Iterator[None]:
    """Make the host bridge the querier for the tests in this module."""
    _querier(True)
    yield
    _querier(False)


def _skip_unless(ipv6: bool) -> None:
    """Skip when the node lacks IGMP (IPv4) or MLD (IPv6).

    :param ipv6: IPv6 variant
    """
    option = "CONFIG_NET_MLD" if ipv6 else "CONFIG_NET_IGMP"
    if not pytest.products[0].core(0).conf.kv_check(option):
        pytest.skip(f"no {option}")


def _member(group: str) -> bool:
    """Check the host bridge group table for ``group``.

    :param group: multicast group address
    :return: True when a node port is a member of ``group``
    """
    out = subprocess.run(
        ["bridge", "-j", "mdb", "show", "dev", BRIDGE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for entry in json.loads(out or "[]"):
        for mdb in entry.get("mdb", []):
            if mdb.get("grp") == group and mdb.get("port") != BRIDGE:
                return True
    return False


def _wait_member(group: str, present: bool, timeout: float) -> bool:
    """Wait until ``group`` is (or is no longer) in the group table.

    :param group: multicast group address
    :param present: wait for presence (True) or absence (False)
    :param timeout: seconds to wait
    :return: True when the state was reached
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _member(group) == present:
            return True
        time.sleep(0.5)
    return False


def _end_server(addr: str, port: int) -> None:
    """Make a nettl UDP server exit (and so leave its group).

    :param addr: node unicast address
    :param port: server port
    """
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as sock:
        for _ in range(3):
            sock.sendto(struct.pack(">I", 0xFFFFFFFF), (addr, port))


@pytest.mark.parametrize("ipv6", FAMILIES)
def test_mcast_host_to_node(ipv6: bool) -> None:
    """Node 0 joins a group; host datagrams to the group come back."""
    _skip_unless(ipv6)
    group = GROUPS[ipv6]
    addr = NODE_IP6S[0] if ipv6 else NODE_IPS[0]
    nettl_server(0, True, 5230, addr, ipv6=ipv6, group=group)
    assert _wait_member(group, True, 10), "no membership report"
    assert host_udp_echo_check(group, 5230, 50, retries=2, mcast_dev=BRIDGE)


@pytest.mark.parametrize("ipv6", FAMILIES)
def test_mcast_node_to_node(ipv6: bool) -> None:
    """Node 1 joins a group; node 0 sends to the group."""
    _skip_unless(ipv6)
    group = GROUPS[ipv6]
    addr = NODE_IP6S[1] if ipv6 else NODE_IPS[1]
    nettl_server(1, True, 5231, addr, ipv6=ipv6, group=group)
    assert _wait_member(group, True, 10), "no membership report"
    verdict = nettl_client(0, group, True, 5231, 50)
    assert verdict.startswith("nettl: PASS"), verdict


@pytest.mark.parametrize("ipv6", LIFETIME_FAMILIES)
def test_mcast_membership_lifetime(ipv6: bool) -> None:
    """A membership survives General Queries and ends when the node leaves.

    The node must answer the bridge queries for longer than the
    membership interval, and leave the group when the socket closes.
    """
    _skip_unless(ipv6)
    group = GROUPS[ipv6]
    addr = NODE_IP6S[0] if ipv6 else NODE_IPS[0]
    nettl_server(0, True, 5232, addr, ipv6=ipv6, group=group)
    assert _wait_member(group, True, 10), "no membership report"
    time.sleep(MEMBERSHIP_S + 5)
    assert _member(group), "membership expired: queries not answered"
    _end_server(addr, 5232)
    assert _wait_member(group, False, 10), "group not left on close"
