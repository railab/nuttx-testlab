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
"""UDP bind() conflict rules between wildcard and specific addresses.

- POSIX bind(): "[EADDRINUSE] The specified address is already in use."
  https://pubs.opengroup.org/onlinepubs/9799919799/functions/bind.html
- POSIX 2.10.16: "The SO_REUSEADDR option indicates that the rules used
  in validating addresses supplied in a bind() should allow reuse of
  local addresses."  "The default value for SO_REUSEADDR is off; that
  is, reuse of local addresses is not permitted."

A wildcard address takes every local address of its port, so a
specific address on the same port is in use, in both bind orders; two
different specific addresses are not.
"""

import re
from typing import Any

import pytest
from _net_common import NODE_IP6S, NODE_IPS, VERDICT_RE

pytestmark = [
    pytest.mark.dep_config(
        "CONFIG_NET_IPv6", "CONFIG_NET_UDP", "CONFIG_NET_SOCKOPTS"
    ),
    pytest.mark.cmd_check("nettl_main"),
]

NODE = 0
EADDRINUSE_CASES = ["wild-spec", "spec-wild"]
REUSE_CASES = ["reuse-wild-spec", "reuse-spec-wild"]


def _core() -> Any:
    """Return the NTFC core handler of node 0.

    :return: ``ProductCore`` for core 0
    """
    return pytest.products[NODE].core(0)


def _bind_case(
    case: str, port: int, addr: str = "", ipv6: bool = False
) -> str:
    """Run ``nettl -b <case>`` on node 0 and return its verdict line.

    :param case: nettl bind case name
    :param port: UDP port the case uses
    :param addr: address passed with ``-c``, if any
    :param ipv6: run the case on AF_INET6 sockets
    :return: verdict line or empty string
    """
    cmd = f"nettl -b {case} -p {port}"
    cmd += f" -c {addr}" if addr else ""
    cmd += " -6" if ipv6 else ""
    ret = _core().sendCommandReadUntilPattern(
        cmd, pattern=VERDICT_RE, timeout=30
    )
    found = re.search(VERDICT_RE, ret.output)
    return found.group(0).rstrip("\r\n") if found else ""


def _node_addr(ipv6: bool) -> str:
    """Return the bridge address of node 0.

    :param ipv6: IPv6 instead of IPv4
    :return: address string
    """
    return NODE_IP6S[NODE] if ipv6 else NODE_IPS[NODE]


@pytest.mark.parametrize("ipv6", [False, True], ids=["v4", "v6"])
@pytest.mark.parametrize("case", EADDRINUSE_CASES)
def test_udp_bind_inuse(case: str, ipv6: bool) -> None:
    """A wildcard and a specific address on one port conflict.

    POSIX bind(): "[EADDRINUSE] The specified address is already in
    use."  Without SO_REUSEADDR ("reuse of local addresses is not
    permitted"), the wildcard, then the node address (``wild-spec``),
    and the node address, then the wildcard (``spec-wild``), on one
    port: the second bind() fails with EADDRINUSE.
    """
    verdict = _bind_case(case, 5701, _node_addr(ipv6), ipv6)
    assert verdict.startswith("nettl: PASS"), verdict


@pytest.mark.dep_config("CONFIG_NET_LOOPBACK")
@pytest.mark.parametrize("ipv6", [False, True], ids=["v4", "v6"])
def test_udp_bind_two_specific(ipv6: bool) -> None:
    """Two different specific addresses share a port.

    POSIX bind(): "[EADDRINUSE] The specified address is already in
    use."  The node address and the loopback address (``127.0.0.1`` or
    ``::1``) are different local addresses, so neither is in use by
    the other: both bind() calls on one port succeed.
    """
    verdict = _bind_case("spec-spec", 5702, _node_addr(ipv6), ipv6)
    assert verdict.startswith("nettl: PASS"), verdict


@pytest.mark.parametrize("ipv6", [False, True], ids=["v4", "v6"])
@pytest.mark.parametrize("case", REUSE_CASES)
def test_udp_bind_reuseaddr(case: str, ipv6: bool) -> None:
    """SO_REUSEADDR on both sockets lets wildcard and specific share a port.

    POSIX 2.10.16: "The SO_REUSEADDR option indicates that the rules
    used in validating addresses supplied in a bind() should allow reuse
    of local addresses."  The wildcard and the node address on one port,
    in both orders, with SO_REUSEADDR set on both sockets: both bind()
    calls succeed.
    """
    verdict = _bind_case(case, 5703, _node_addr(ipv6), ipv6)
    assert verdict.startswith("nettl: PASS"), verdict
