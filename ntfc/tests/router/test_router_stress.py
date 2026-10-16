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

"""Load and link changes that may leave the node unusable.

The last module of the router session: full-duplex TCP load through
the NAT with a node health check, then each node interface taken down
and up again.
"""

import re
import time
from typing import Any, List

import pytest
from _router_common import (
    B_ROUTE,
    HOSTS,
    NODES,
    configure,
    core,
    host_ping,
    iperf_client,
    iperf_mbps,
    iperf_server,
    iptables,
    ns_run,
    nsh,
    ping_ok,
)

#: Length of the duplex load in seconds.
DUPLEX_TIME = 40

#: A network worker blocked on a mutex: ``ps`` STATE ``Waiting``, EVENT
#: ``Mutex:<holder pid>``.
STUCK_RE = re.compile(r"^.*Mutex:\d+.*(?:hpwork|lpwork|netdev).*$", re.M)

IPFWD_LOCK_BUG = (
    "net/ipforward/ipfwd_forward.c: ipfwd_forward() runs in the ingress "
    "worker and links its callback into the egress device's devif "
    "callback lists, then reads the forward state again, without the "
    "egress device's lock; with a concurrent egress worker callbacks are "
    "lost (forwarding stops, IOBs held) or freed state is used"
)


@pytest.fixture
def concurrent_workers_xfail(request: Any) -> None:
    """Expect the duplex test to fail now and then with two HP workers.

    :param request: pytest request of the test
    """
    nthreads = core().conf.kv_check("CONFIG_SCHED_HPNTHREADS")
    if int(nthreads or 1) > 1:
        request.applymarker(
            pytest.mark.xfail(strict=False, reason=IPFWD_LOCK_BUG)
        )


def _stuck_workers() -> List[str]:
    """Return ``ps`` lines of network workers waiting on a mutex.

    Sampled twice, a second apart: a worker blocked in both samples
    does not move.

    :return: lines present in both samples
    """
    first = set(STUCK_RE.findall(nsh("ps")))
    time.sleep(1)
    output = nsh("ps")
    print(output)
    return sorted(first & set(STUCK_RE.findall(output)))


@pytest.mark.dep_config("CONFIG_NET_NAT44")
@pytest.mark.usefixtures("concurrent_workers_xfail")
def test_router_duplex(router_ifs: List[str]) -> None:
    """The node keeps forwarding after duplex TCP load through NAT.

    Host A runs two iperf clients against host B at once, one sending
    and one receiving (``-R``), for :data:`DUPLEX_TIME` seconds, with
    MASQUERADE on the B interface. Then: no network worker is blocked
    on a mutex, both node addresses answer, A reaches B again and
    ``ifconfig`` returns.
    """
    rule = f"-t nat -A POSTROUTING -o {router_ifs[1]} -j MASQUERADE"
    iptables(rule)
    ns_run(1, ["ip", "route", "del", *B_ROUTE])
    healthy = False
    try:
        port = 5750
        with iperf_server(1, port):
            time.sleep(0.5)
            up = iperf_client(0, HOSTS[1], port, DUPLEX_TIME)
            down = iperf_client(0, HOSTS[1], port, DUPLEX_TIME, reverse=True)
            print(
                f"duplex: A->B {iperf_mbps(up, DUPLEX_TIME + 30):.1f}, "
                f"B->A {iperf_mbps(down, DUPLEX_TIME + 30):.1f} Mbit/s"
            )

        stuck = _stuck_workers()
        assert not stuck, f"network workers blocked: {stuck}"
        assert ping_ok(0, NODES[0])
        assert ping_ok(1, NODES[1])
        assert ping_ok(0, HOSTS[1])
        assert all(name in nsh("ifconfig", timeout=10) for name in router_ifs)
        healthy = True
    finally:
        ns_run(1, ["ip", "route", "replace", *B_ROUTE])
        if healthy:
            iptables(rule.replace(" -A ", " -D "))


@pytest.mark.parametrize("side", [0, 1], ids=["a", "b"])
def test_router_ifdown_ifup(router_ifs: List[str], side: int) -> None:
    """Forwarding stops while a node interface is down and comes back."""
    assert ping_ok(0, HOSTS[1])
    nsh(f"ifdown {router_ifs[side]}")
    assert host_ping(side, NODES[side]).returncode != 0
    assert host_ping(0, HOSTS[1]).returncode != 0
    configure()
    assert ping_ok(side, NODES[side])
    assert ping_ok(0, HOSTS[1])
    assert ping_ok(1, HOSTS[0])
