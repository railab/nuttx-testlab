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

"""TCP between two tasks of one node.

Runs in a session of its own: a stalled transfer can hang the node.
"""

import re
from typing import Any

import pytest
from _net_common import NODE_IPS, VERDICT_RE

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_TCP"),
    pytest.mark.cmd_check("nettl_main"),
]

SERVER_VERDICT_RE = r"nettl: (PASS|FAIL) rx=\d+ err=\d+[\r\n]"
OWN_ADDR_STALL_BUG = (
    "net/tcp/tcp_callback.c: tcp_datahandler() drops the data of a peer on "
    "this host while fewer than IOB_THROTTLE IOBs are free; the IOBs are in "
    "the write buffers of that peer, so the transfer stops"
)


def _core() -> Any:
    """Return the NTFC core handler of node 0.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.products[0].core(0)


@pytest.mark.xfail(strict=True, reason=OWN_ADDR_STALL_BUG)
def test_tcp_own_address_stream() -> None:
    """Stream 128 KiB both ways between two tasks over the node's address.

    RFC 9293, 3.1: TCP "is able to transfer a continuous stream of octets
    in each direction between its users".  The client sends to the node's
    own interface address while it reads the echo of a server task on the
    same node.
    """
    size, port = 128 * 1024, 5260
    ret = _core().sendCommand(
        f"nettl -s -p {port} -t 30 &", "listening", timeout=10
    )
    assert ret == 0
    out = _core().sendCommandReadUntilPattern(
        f"nettl -c {NODE_IPS[0]} -p {port} -n {size} -d -t 30",
        pattern=VERDICT_RE,
        timeout=40,
    )
    found = re.search(VERDICT_RE, out.output)
    verdict = found.group(0).strip() if found else out.output
    assert verdict == f"nettl: PASS tx={size} rx={size} err=0"
    found = re.search(SERVER_VERDICT_RE, out.output)
    if found:
        server = found.group(0).rstrip("\r\n")
    else:
        result = _core().readUntilPattern(SERVER_VERDICT_RE, timeout=20)
        found = re.search(SERVER_VERDICT_RE, result.output)
        server = found.group(0).rstrip("\r\n") if found else ""
    assert server == f"nettl: PASS rx={size} err=0"
