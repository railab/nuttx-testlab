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

"""SocketCAN RX timestamps: SO_TIMESTAMP / SO_TIMESTAMPNS (``canopt``).

The host sends frames ``GAP_MS`` apart; ``canopt ts`` lets the first ones
queue on the socket before it reads, so both the read-ahead and the
blocked-reader paths are stamped.
"""

import pytest
from _canopt_common import (  # noqa: F401 - canopt_env is a fixture
    Frame,
    canopt_env,
    canopt_start,
    canopt_verdict,
    host_send,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_CAN"),
    pytest.mark.dep_config("CONFIG_NET_TIMESTAMP"),
    pytest.mark.cmd_check("canopt_main"),
    pytest.mark.usefixtures("canopt_env"),
]

GAP_MS = 20
COUNT = 10


def _run_ts(opts: str) -> str:
    """Run ``canopt ts`` against ``COUNT`` host frames.

    :param opts: extra canopt options (``-m``, ``-f``)
    :return: verdict line
    """
    canopt_start(0, f"ts can0 {opts} -n {COUNT} -g {GAP_MS}")
    host_send([Frame(0x123, 8)] * COUNT, gap_s=GAP_MS / 1000)
    return canopt_verdict(0)


@pytest.mark.parametrize("mode", ["us", "ns"])
def test_can_rx_timestamp(mode: str) -> None:
    """Every frame carries one SO_TIMESTAMP(NS) control message.

    Stamps are CLOCK_REALTIME, not before the check started, not after
    the read, monotonic, and spread over at least half the host's send
    span (stamped at arrival, not at read).
    """
    ret = _run_ts(f"-m {mode}")
    assert ret.startswith("canopt: PASS ts"), ret


@pytest.mark.parametrize(
    "fd",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.xfail(
                strict=True,
                reason="CAN_RAW_FD_FRAMES sets the SO_TIMESTAMPNS bit",
            ),
        ),
    ],
    ids=["classic", "fd"],
)
def test_can_rx_no_timestamp(fd: bool) -> None:
    """Without a timestamp option recvmsg() returns no control message."""
    ret = _run_ts("-m none -f" if fd else "-m none")
    assert ret.startswith("canopt: PASS ts"), ret
