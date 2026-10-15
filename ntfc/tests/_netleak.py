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

"""Check that a network test gives back every I/O buffer."""

import re
import time
from typing import Any, Dict, List, Optional

import pytest
from _net_common import HOST_IP

IOB_RE = re.compile(r"^\s*(\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s*$", re.MULTILINE)
STATS_RE = re.compile(r"\t(RX|TX):[^\n]*\n\t\s+([0-9a-fA-F ]+)")
SETTLE_S = 30.0

#: A leak this small (per node) is reported as :class:`IobCountLost`
COUNT_LOST_MAX = 2


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def iob_free(node: int) -> int:
    """Return the number of free IOBs of a node.

    :param node: product index
    :return: ``nfree`` column of ``/proc/iobinfo``
    """
    ret = _core(node).sendCommandReadUntilPattern(
        "cat /proc/iobinfo", pattern=r"nsh> ", timeout=10
    )
    found = IOB_RE.search(str(ret.output))
    assert found, f"node {node}: no /proc/iobinfo: {ret.output}"
    return int(found.group(2))


def dev_stats(node: int, dev: str) -> Dict[str, List[int]]:
    """Return the driver counters of a network device.

    :param node: product index
    :param dev: interface name, e.g. ``eth0``
    :return: ``{"RX": [...], "TX": [...]}``, empty without
     ``CONFIG_NETDEV_STATISTICS``
    """
    ret = _core(node).sendCommandReadUntilPattern(
        f"cat /proc/net/{dev}", pattern=r"nsh> ", timeout=10
    )
    return {
        m.group(1): [int(v, 16) for v in m.group(2).split()]
        for m in STATS_RE.finditer(str(ret.output))
    }


def _kick(node: int, peer: str) -> None:
    """Send one ping so the driver reclaims buffers it frees lazily.

    A driver may hold its last transmitted packet until the next
    transmission (e.g. virtio-net), which moves the free count by one.

    :param node: product index
    :param peer: address to ping
    """
    _core(node).sendCommand(
        f"ping -c 1 -W 1 {peer}", "packet loss", timeout=10
    )


def _free_all(nodes: List[int]) -> Dict[int, int]:
    """Return the free IOB count of every node.

    :param nodes: product indexes
    :return: free IOBs per node
    """
    return {node: iob_free(node) for node in nodes}


class IobCountLost(AssertionError):
    """At most ``COUNT_LOST_MAX`` IOBs per node are missing."""


class LeakCheck:
    """Free IOB and driver counter snapshot taken before a test."""

    def __init__(self, dev: str, peer: str) -> None:
        """Take the snapshot on every node.

        :param dev: interface whose driver counters are printed
        :param peer: address pinged to make drivers reclaim TX buffers
        """
        self.dev = dev
        self.peer = peer
        self.nodes = list(range(len(pytest.products)))
        for node in self.nodes:
            _kick(node, peer)
        self.before = _free_all(self.nodes)
        self.stats = {node: dev_stats(node, dev) for node in self.nodes}

    def _print_stats(self) -> None:
        """Print the driver counter deltas of every node."""
        for node in self.nodes:
            now = dev_stats(node, self.dev)
            delta = {
                key: [
                    a - b
                    for a, b in zip(vals, self.stats[node].get(key, vals))
                ]
                for key, vals in now.items()
            }
            print(f"node{node} {self.dev} counters delta: {delta}")

    def check(self, nodes: Optional[List[int]] = None) -> None:
        """Fail when a node has fewer free IOBs than before the test.

        Waits up to ``SETTLE_S`` for buffers still in flight, pinging
        ``peer`` from a node that misses buffers. Raises
        :class:`IobCountLost` when no node misses more than
        ``COUNT_LOST_MAX`` IOBs, so an expected failure can name that
        case alone.

        :param nodes: nodes the test used (default all)
        """
        nodes = self.nodes if nodes is None else nodes
        deadline = time.monotonic() + SETTLE_S
        after = _free_all(nodes)
        while (
            any(after[n] < self.before[n] for n in nodes)
            and time.monotonic() < deadline
        ):
            time.sleep(2)
            for node in nodes:
                if after[node] < self.before[node]:
                    _kick(node, self.peer)
            after = _free_all(nodes)
        self._print_stats()
        leaked = {
            n: self.before[n] - after[n]
            for n in nodes
            if after[n] < self.before[n]
        }
        msg = f"leaked IOBs per node: {leaked}"
        if leaked and max(leaked.values()) <= COUNT_LOST_MAX:
            raise IobCountLost(msg)
        assert not leaked, msg


@pytest.fixture
def net_leak(request: pytest.FixtureRequest) -> LeakCheck:
    """Return a snapshot whose ``check()`` the test calls at its end.

    The check runs in the test's call phase, so an expected failure
    marker applies to it. The interface is ``request.module.LEAK_DEV``
    (default ``eth0``), the ping peer ``request.module.LEAK_PEER``
    (default the ip-pair host).

    :param request: pytest request
    :return: snapshot taken before the test
    """
    return LeakCheck(
        getattr(request.module, "LEAK_DEV", "eth0"),
        getattr(request.module, "LEAK_PEER", HOST_IP),
    )
