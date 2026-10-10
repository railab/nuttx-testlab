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

"""SocketCAN frame types: SFF/EFF/RTR, DLC 0..8, CAN FD (``canopt``).

Uses the classic and CAN FD frame tables of ``_canopt_common``: every
CAN FD length (0..8, 12..64 = DLC 0..15) and BRS/ESI combination.
"""

import pytest
from _canopt_common import (  # noqa: F401 - canopt_env is a fixture
    CLASSIC_FRAMES,
    CTUCANFD_EFF_BUG,
    CTUCANFD_FD_RX_BUG,
    CTUCANFD_TX_BUG,
    FD_FRAMES,
    MARKER_ID,
    Frame,
    Kicker,
    canopt_env,
    canopt_run,
    canopt_start,
    canopt_verdict,
    host_expect,
    host_send,
    host_socket,
    interleaved_frames,
    xfail_on_ctucanfd,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_CAN"),
    pytest.mark.dep_config("CONFIG_NET_CAN_CANFD"),
    pytest.mark.cmd_check("canopt_main"),
    pytest.mark.usefixtures("canopt_env"),
]


def test_can_tx_frame_types(request: pytest.FixtureRequest) -> None:
    """Node sends both tables; the host receives every frame intact.

    Classic frames go out on a classic socket, which must refuse a
    CANFD_MTU write with EINVAL; CAN FD frames on a CAN_RAW_FD_FRAMES
    socket keep their length and BRS/ESI flags.

    :param request: pytest request
    """
    xfail_on_ctucanfd(request, CTUCANFD_TX_BUG)
    sock = host_socket()
    try:
        ret = canopt_run(0, "tx can0")
        errors = host_expect(sock, CLASSIC_FRAMES + FD_FRAMES)
    finally:
        sock.close()

    assert ret.startswith("canopt: PASS tx"), ret
    assert errors == []


def test_can_fd_rx(request: pytest.FixtureRequest) -> None:
    """A CAN_RAW_FD_FRAMES socket receives both tables from the host.

    CAN FD frames read as CANFD_MTU with length and BRS/ESI intact,
    classic frames as CAN_MTU.

    :param request: pytest request
    """
    xfail_on_ctucanfd(request, CTUCANFD_FD_RX_BUG)
    canopt_start(0, "fdrx can0")
    host_send(interleaved_frames() + [Frame(MARKER_ID, 1)])
    ret = canopt_verdict(0)
    assert ret.startswith("canopt: PASS fdrx"), ret


@pytest.mark.parametrize(
    "queued",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.xfail(
                strict=True,
                reason="can_readahead() hands queued CAN FD frames to "
                "classic sockets",
            ),
        ),
    ],
    ids=["blocking", "queued"],
)
def test_can_fd_to_classic_socket(
    request: pytest.FixtureRequest, queued: bool
) -> None:
    """A classic socket on a mixed bus sees only classic frames.

    The host interleaves both tables. ``blocking``: the reader waits in
    read() for each frame. ``queued``: all frames queue on the socket
    before the first read. Every read must return one classic frame of
    CAN_MTU bytes. The host sends wake-up frames after 3 s so a reader
    that blocks despite queued frames still returns.

    :param request: pytest request
    :param queued: let all frames queue before the first read
    """
    if not queued:
        xfail_on_ctucanfd(request, CTUCANFD_EFF_BUG)

    if queued:
        canopt_start(0, "legacy can0 -q")
        host_send(interleaved_frames() + [Frame(MARKER_ID, 1)])
    else:
        canopt_start(0, "legacy can0")
        host_send(interleaved_frames() + [Frame(MARKER_ID, 1)], gap_s=0.02)

    with Kicker(delay_s=3.0):
        ret = canopt_verdict(0, timeout=20)
    assert ret.startswith("canopt: PASS legacy"), ret
