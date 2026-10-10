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

"""SocketCAN socket options, filters, poll and statistics (``canopt``).

Node 0 runs one ``canopt`` check per test on ``can0``; the host sends or
receives the frames the check needs on the shared vcan ``can0``.
"""

import re
from typing import List, Tuple

import pytest
from _canopt_common import (  # noqa: F401 - canopt_env is a fixture
    CAN_EFF_FLAG,
    CAN_EFF_MASK,
    CAN_RTR_FLAG,
    CLASSIC_FRAMES,
    MARKER_ID,
    VERDICT_RE,
    Frame,
    Kicker,
    canopt_env,
    canopt_forget,
    canopt_run,
    canopt_start,
    canopt_verdict,
    filter_match,
    host_expect,
    host_send,
    host_socket,
    kv_value,
    verdict_of,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_NET_CAN"),
    pytest.mark.cmd_check("canopt_main"),
    pytest.mark.usefixtures("canopt_env"),
]

EXACT = CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG

# (canopt filter options, filter list) per CAN_RAW_FILTER case

FILTERS = {
    "default": ("", [(0, 0)]),
    "empty": ("-Z", []),
    "sff-any-format": ("-F 123/7ff", [(0x123, 0x7FF)]),
    "sff-exact": ("-F 123/c00007ff", [(0x123, 0xC00007FF)]),
    "multi": (
        "-F 100/7f8 -F 456/7ff -F 80000000/80000000",
        [(0x100, 0x7F8), (0x456, 0x7FF), (CAN_EFF_FLAG, CAN_EFF_FLAG)],
    ),
    "inverted": ("-F 20000100/700", [(0x20000100, 0x700)]),
    "eff-exact": ("-F 92345678/9fffffff", [(0x92345678, 0x9FFFFFFF)]),
    "rtr": ("-F 40000000/40000000", [(CAN_RTR_FLAG, CAN_RTR_FLAG)]),
    "max": ("-M", []),
}


def _max_filters() -> List[Tuple[int, int]]:
    """Return the filter list ``canopt rx -M`` installs.

    :return: ``CONFIG_NET_CAN_RAW_FILTER_MAX`` exact filters, only the
     last one matching a table frame (0x105)
    """
    count = int(kv_value(0, "CONFIG_NET_CAN_RAW_FILTER_MAX"))
    return [(0x600 + i, EXACT) for i in range(count - 1)] + [(0x105, EXACT)]


def test_can_sockopt_roundtrip() -> None:
    """CAN_RAW_* and SO_TIMESTAMP set/get round trips and validation."""
    ret = canopt_run(0, "sockopt can0")
    assert ret.startswith("canopt: PASS sockopt"), ret


@pytest.mark.xfail(
    strict=True,
    reason="CAN_RAW_FD_FRAMES and SO_TIMESTAMPNS share one s_options bit",
)
def test_can_sockopt_no_alias() -> None:
    """SO_TIMESTAMPNS and CAN_RAW_FD_FRAMES are independent options."""
    ret = canopt_run(0, "optbits can0")
    assert ret.startswith("canopt: PASS optbits"), ret


@pytest.mark.dep_config("CONFIG_NET_RECV_BUFSIZE")
@pytest.mark.xfail(
    strict=True,
    reason="can_setsockopt() handles SO_RCVBUF only at level SOL_CAN_RAW",
)
def test_can_so_rcvbuf() -> None:
    """SO_RCVBUF at level SOL_SOCKET round trips on a CAN socket."""
    ret = canopt_run(0, "rcvbuf can0")
    assert ret.startswith("canopt: PASS rcvbuf"), ret


@pytest.mark.xfail(
    strict=True, reason="can_recvmsg() waits forever, ignores SO_RCVTIMEO"
)
def test_can_so_rcvtimeo() -> None:
    """A blocking read on an idle socket times out after SO_RCVTIMEO.

    The host starts sending wake-up frames after 3 s so a reader that
    ignores the timeout still returns.
    """
    canopt_start(0, "rcvtimeo can0")
    with Kicker(delay_s=3.0):
        ret = canopt_verdict(0, timeout=15)
    assert ret.startswith("canopt: PASS rcvtimeo"), ret


@pytest.mark.parametrize("case", list(FILTERS))
def test_can_raw_filter(case: str) -> None:
    """CAN_RAW_FILTER passes exactly the frames SocketCAN rules select.

    The host sends the classic frame table (SFF, EFF, RTR, DLC 0..8);
    the frames the node receives must be those the filter list matches,
    in order and intact.
    """
    opts, filters = FILTERS[case]
    if case == "max":
        filters = _max_filters()

    expected = [
        f.can_id for f in CLASSIC_FRAMES if filter_match(f.can_id, filters)
    ]

    canopt_start(0, f"rx can0 {opts}".rstrip())
    host_send(CLASSIC_FRAMES + [Frame(MARKER_ID, 1)])
    ret = canopt_verdict(0)
    assert ret.startswith("canopt: PASS rx"), ret

    ids = re.search(r"ids=(\S+)", ret).group(1)
    got = [] if ids == "-" else [int(x, 16) for x in ids.split(",")]
    assert got == expected, ret


@pytest.mark.parametrize(
    "loopback,recv_own",
    [
        (0, 0),
        pytest.param(
            1,
            0,
            marks=pytest.mark.xfail(
                strict=True,
                reason="NuttX SocketCAN has no local loopback",
            ),
        ),
        pytest.param(
            1,
            1,
            marks=pytest.mark.xfail(
                strict=True,
                reason="NuttX SocketCAN has no local loopback",
            ),
        ),
    ],
    ids=["off", "on", "recv-own"],
)
def test_can_loopback(loopback: int, recv_own: int) -> None:
    """CAN_RAW_LOOPBACK / CAN_RAW_RECV_OWN_MSGS local delivery.

    Socket A sends one frame; another socket B on the same node receives
    it only with loopback on, A itself only with recv-own-msgs too. The
    frame always reaches the bus.
    """
    sock = host_socket()
    try:
        ret = canopt_run(0, f"loopback can0 -l {loopback} -o {recv_own}")
        errors = host_expect(sock, [Frame(0x555, 8)])
    finally:
        sock.close()

    assert errors == []
    assert ret.startswith("canopt: PASS loopback"), ret


def test_can_nonblock_poll() -> None:
    """Nonblocking I/O, poll() and select() on a CAN socket.

    EAGAIN on an empty socket (O_NONBLOCK and MSG_DONTWAIT), poll() idle
    timeout and POLLOUT, a nonblocking write that reaches the host, then
    select() and poll() wake-ups for two host frames.
    """
    sock = host_socket()
    try:
        canopt_start(0, "poll can0")
        errors = host_expect(sock, [Frame(0x5A0, 8)])
    finally:
        sock.close()

    host_send([Frame(0x5A1, 8), Frame(0x5A2, 8)], gap_s=0.05)
    ret = canopt_verdict(0)
    assert errors == []
    assert ret == "canopt: PASS poll", ret


def test_can_poll_hup() -> None:
    """poll() on a CAN socket reports POLLHUP when ``can0`` goes down."""
    core = pytest.products[0].core(0)
    canopt_start(0, "hup can0")
    try:
        out = core.sendCommandReadUntilPattern(
            "ifdown can0", pattern=VERDICT_RE, timeout=15
        ).output
        canopt_forget(0)
    finally:
        assert core.sendCommand("ifup can0", "OK", timeout=10) == 0

    ret = verdict_of(out)
    assert ret.startswith("canopt: PASS hup"), ret


def _can_stats() -> Tuple[int, int]:
    """Read the CAN column of ``/proc/net/stat`` on node 0.

    :return: ``(received, sent)`` frame counters
    """
    out = (
        pytest.products[0]
        .core(0)
        .sendCommandReadUntilPattern(
            "cat /proc/net/stat", pattern=r"Sent[^\r\n]*[\r\n]", timeout=10
        )
        .output
    )
    recv = re.search(r"Received[^\r\n]*\s([0-9a-f]+)\s*[\r\n]", out)
    sent = re.search(r"Sent[^\r\n]*\s([0-9a-f]+)\s*[\r\n]", out)
    assert recv and sent, out
    return int(recv.group(1), 16), int(sent.group(1), 16)


@pytest.mark.dep_config("CONFIG_NET_STATISTICS")
def test_can_stats() -> None:
    """``/proc/net/stat`` CAN counters count received and sent frames."""
    count = 5
    recv0, sent0 = _can_stats()
    host_send([Frame(0x5C0, 8)] * count, gap_s=0.005)
    ret = canopt_run(0, "tx can0")
    assert ret.startswith("canopt: PASS tx"), ret
    recv1, sent1 = _can_stats()

    tx = sum(int(n) for n in re.findall(r"=(\d+)", ret))
    assert (recv1 - recv0, sent1 - sent0) == (count, tx)
