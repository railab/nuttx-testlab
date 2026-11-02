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
"""PTP (IEEE 1588-2008): NuttX ``netutils/ptpd`` against linuxptp.

The node runs the daemon through ``ptptl`` (apps/ptptl); the host runs
``ptp4l`` on the bridge with software timestamps and ``free_running``
(the host clock is never adjusted), and a sniffer decodes every PTP
message on the bridge. Expectations come from IEEE 1588-2008 (clause
numbers in the docstrings) and from the linuxptp reference behaviour
(``ptp4l(8)``); the clock offset of a node is measured from the
originTimestamp of the messages it sends against the host receive time.

On these targets the node has no architecture ``adjtime()``: the client
synchronizes by stepping the clock, so the offset after synchronization
is bounded by the step accuracy, not by a servo.
"""

import time
from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest
from _netleak import LeakCheck
from _ptp_common import (
    FLAG_TWO_STEP,
    MSG_ANNOUNCE,
    MSG_DELAY_REQ,
    MSG_DELAY_RESP,
    MSG_FOLLOW_UP,
    MSG_PDELAY_REQ,
    MSG_PDELAY_RESP,
    MSG_PDELAY_RESP_FUP,
    MSG_SYNC,
    PTP_UDP_EVENT,
    PTP_UDP_GENERAL,
    Ptp4l,
    PtpMsg,
    PtpSniffer,
    clock_identity,
    node_mac,
    offsets_ms,
    ptptl_run,
    ptptl_start,
    ptptl_wait,
)
from _tls_common import HeapCheck

pytestmark = [pytest.mark.dep_config("CONFIG_NETUTILS_PTPD")]

# Offset bound after synchronization: a step from software timestamps on
# a local link lands within a few ms; 50 ms also covers console and
# capture latency on a loaded CI host.
SYNC_BOUND_MS = 50.0

# ptp4l configuration in _ptp_common.Ptp4l: logAnnounceInterval 1.
PTP4L_ANNOUNCE_S = 2.0

# ptp4l(8): "announceReceiptTimeout: The number of missed Announce
# messages before the last Announce messages expires. The default is 3."
# IEEE 1588-2008 9.2.6.11: the announce receipt timeout is
# announceReceiptTimeout times the announce interval of the master.
RECEIPT_TIMEOUT = 3
LOSS_MARGIN_S = 2.0

# ptp4l veth MAC fixed by testenv/ip-pair.sh (TL_PTP_MAC).
HOST_MAC = "52:54:00:2a:00:02"
HOST_ID = clock_identity(HOST_MAC)

# NuttX defconfig: NETUTILS_PTPD_PRIORITY1 128, NETUTILS_PTPD_CLASS 248
# (1588 7.6.2.4 Table 5: 248 "Default. This clockClass shall be used if
# none of the other clockClass definitions apply").
NODE_PRIORITY1 = 128
NODE_CLASS = 248

# ptp4l port states of the slave branch (1588 9.2.5 Figure 23): a
# free-running ptp4l stays UNCALIBRATED because its servo never locks.
SLAVE_STATES = {"UNCALIBRATED", "SLAVE"}

PTPD_TIMEOUT_BUG = (
    "netutils/ptpd: a lost master is noticed after the fixed "
    "CONFIG_NETUTILS_PTPD_TIMEOUT_MS (60 s), not after "
    "announceReceiptTimeout announce intervals of the master "
    "(IEEE 1588-2008 9.2.6.11, 3 x 2 s here)"
)


@pytest.fixture
def sniffer() -> Iterator[PtpSniffer]:
    """Capture the PTP messages on the bridge during the test."""
    with PtpSniffer() as cap:
        yield cap


def _last(values: List[float], count: int) -> List[float]:
    """Return the last ``count`` values.

    :param values: samples in time order
    :param count: how many
    :return: the tail
    """
    assert len(values) >= count, f"only {len(values)} samples: {values}"
    return values[-count:]


def _assert_offsets(msgs: List[PtpMsg], count: int = 3) -> List[float]:
    """Check that the last originTimestamps are within the sync bound.

    :param msgs: node messages with originTimestamp
    :param count: samples checked
    :return: the checked offsets in ms
    """
    offs = _last(offsets_ms(msgs), count)
    print(f"node offsets ms: {[round(o, 3) for o in offs]}")
    assert all(abs(o) <= SYNC_BOUND_MS for o in offs), offs
    return offs


def _client_run(args: str, runtime: int) -> Tuple[Dict[str, Any], List[Any]]:
    """Run ``ptptl -c`` on node0 and return its verdict and status lines.

    :param args: extra options
    :param runtime: seconds
    :return: verdict, status lines
    """
    ptptl_start(0, f"-c -w 30 {args}", runtime=runtime)
    return ptptl_wait(0, runtime + 30)


def _wait_state(host: Ptp4l, states: Set[str], timeout: float) -> str:
    """Wait for one of the ptp4l port states.

    :param host: ptp4l
    :param states: accepted portState values
    :param timeout: seconds
    :return: the last state seen
    """
    deadline = time.monotonic() + timeout
    seen = host.port_state()
    while seen not in states and time.monotonic() < deadline:
        time.sleep(1)
        seen = host.port_state()
    return seen


def _identity(pmc_value: str) -> str:
    """Convert a pmc clock identity (``525400.fffe.2a0010``) to hex.

    :param pmc_value: pmc field value
    :return: 16 hex digits
    """
    return pmc_value.split("-")[0].replace(".", "").lower()


def test_ptp_client_sync_from_host(sniffer: PtpSniffer) -> None:
    """node0 synchronizes to a ptp4l grandmaster on the bridge.

    IEEE 1588-2008 9.2.5: a port that receives Announce messages from a
    better clock enters the SLAVE state; 7.5.2.2.2: the clockIdentity is
    the EUI-64 of the MAC; 8.2.2.3: stepsRemoved is the number of paths
    to the grandmaster, 0 in the grandmaster's own Announce.  PASS: the
    source is valid with the host's identity, and the last Delay_Req
    originTimestamps are within SYNC_BOUND_MS of the host clock.
    """
    mac = node_mac(0)
    with Ptp4l("master") as host:
        verdict, status = _client_run("", 40)
        assert host.port_state() == "MASTER", host.text()
    assert verdict["verdict"] == "PASS", verdict
    last = status[-1]
    assert last["valid"] == 1, last
    assert last["gm"] == HOST_ID, last
    assert last["steps"] == 0, last
    assert last["path"] >= 0, last
    _assert_offsets(sniffer.messages(MSG_DELAY_REQ, src=mac))


def test_ptp_client_steps_large_offset(sniffer: PtpSniffer) -> None:
    """A node clock two hours ahead is brought to the master's time.

    IEEE 1588-2008 9.2 / 11.2: the slave clock is synchronized to the
    master.  PASS: after the run the node's Delay_Req originTimestamps
    are within SYNC_BOUND_MS of the host clock.
    """
    mac = node_mac(0)
    ahead = time.strftime("%b %d %H:%M:%S %Y", time.gmtime(time.time() + 7200))
    assert pytest.products[0].core(0).sendCommand(f'date -s "{ahead}"') == 0
    with Ptp4l("master"):
        verdict, status = _client_run("", 40)
    assert verdict["verdict"] == "PASS", verdict
    assert status[-1]["valid"] == 1, status[-1]
    _assert_offsets(sniffer.messages(MSG_DELAY_REQ, src=mac))


@pytest.mark.xfail(strict=True, reason=PTPD_TIMEOUT_BUG)
def test_ptp_client_loses_master(sniffer: PtpSniffer) -> None:
    """The source becomes invalid soon after the master stops.

    IEEE 1588-2008 9.2.6.11 (ANNOUNCE_RECEIPT_TIMEOUT_EXPIRES): the
    announce receipt timeout is announceReceiptTimeout (default 3)
    announce intervals.  PASS: a status line with ``valid=0`` within
    3 x 2 s + LOSS_MARGIN_S after ptp4l stopped, and no valid line
    afterwards.
    """
    mac = node_mac(0)
    host = Ptp4l("master")
    host.start()
    started = ptptl_start(0, "-c -w 30", runtime=45)
    assert sniffer.wait(MSG_DELAY_REQ, 3, 40, src=mac), "node never synced"
    time.sleep(2)
    host.stop()
    stopped = time.monotonic() - started
    _verdict, status = ptptl_wait(0, 75)
    limit = stopped + RECEIPT_TIMEOUT * PTP4L_ANNOUNCE_S + LOSS_MARGIN_S
    invalid = [s["t"] for s in status if s["valid"] == 0 and s["t"] > stopped]
    print(f"master stopped at t={stopped:.1f}, invalid at {invalid[:3]}")
    assert invalid and invalid[0] <= limit, (stopped, limit, invalid[:3])
    assert all(s["valid"] == 0 for s in status if s["t"] >= invalid[0]), status


def _per_type(msgs: List[PtpMsg], msgtype: int) -> List[PtpMsg]:
    """Return the messages of one type.

    :param msgs: messages
    :param msgtype: type
    :return: subset
    """
    return [m for m in msgs if m.msgtype == msgtype]


def _assert_sequence(msgs: List[PtpMsg]) -> None:
    """Check that sequenceId increments by one (1588 7.3.7).

    :param msgs: messages of one type from one port
    """
    seqs = [m.seq for m in msgs]
    steps = {(b - a) % 65536 for a, b in zip(seqs, seqs[1:])}
    assert steps == {1}, seqs


def _assert_interval(msgs: List[PtpMsg]) -> None:
    """Check logMessageInterval against the measured interval.

    1588 7.7.2.1: the interval between messages is 2^logMessageInterval
    seconds, the value carried in the header (13.3.2.11).

    :param msgs: messages of one type from one port
    """
    gaps = [b.t - a.t for a, b in zip(msgs, msgs[1:])]
    mean = sum(gaps) / len(gaps)
    advertised = 2.0 ** msgs[0].log_interval
    print(
        f"type {msgs[0].msgtype}: mean {mean:.3f} s, "
        f"log {msgs[0].log_interval}"
    )
    assert abs(mean - advertised) <= 0.25 * advertised, (mean, advertised)
    assert {m.log_interval for m in msgs} == {msgs[0].log_interval}


def test_ptp_master_wire_format(sniffer: PtpSniffer) -> None:
    """node0 as master sends well-formed Announce, Sync and Follow_Up.

    IEEE 1588-2008 Annex D.3: event messages to UDP port 319, general
    messages to port 320, multicast 224.0.1.129; 13.3.2: versionPTP 2,
    domainNumber 0; 13.3.2.6: twoStepFlag set in Sync when a Follow_Up
    follows, clear in Follow_Up and Announce; 9.5.9.3: the Follow_Up
    carries the sequenceId of its Sync; 7.3.7: sequenceId increments by
    one per message type; 7.7.2.1: logMessageInterval matches the rate;
    7.5.2.2.2: sourcePortIdentity from the MAC, portNumber 1 (7.5.2.3);
    13.5.2: Announce grandmasterPriority1, clockClass and stepsRemoved 0
    of a grandmaster.
    """
    mac = node_mac(0)
    ident = clock_identity(mac)
    ptptl_start(0, "", runtime=12)
    verdict, _status = ptptl_wait(0, 40)
    assert verdict["verdict"] == "PASS", verdict
    msgs = sniffer.messages(src=mac)
    sync = _per_type(msgs, MSG_SYNC)
    fup = _per_type(msgs, MSG_FOLLOW_UP)
    ann = _per_type(msgs, MSG_ANNOUNCE)
    assert len(sync) >= 8 and len(ann) >= 8, (len(sync), len(ann))
    for m in msgs:
        assert m.version == 2 and m.domain == 0, m
        assert m.clock_id == ident and m.port_num == 1, m
    assert {m.dst_port for m in sync} == {PTP_UDP_EVENT}
    assert {m.dst_port for m in fup + ann} == {PTP_UDP_GENERAL}
    assert all(m.flags & FLAG_TWO_STEP for m in sync), sync
    assert not any(m.flags & FLAG_TWO_STEP for m in fup + ann)
    assert [m.seq for m in fup] == [m.seq for m in sync][: len(fup)]
    assert len(sync) - len(fup) <= 1, (len(sync), len(fup))
    for group in (sync, ann):
        _assert_sequence(group)
        _assert_interval(group)
    for m in ann:
        assert m.announce, m
        assert m.announce["priority1"] == NODE_PRIORITY1, m.announce
        assert m.announce["clock_class"] == NODE_CLASS, m.announce
        assert m.announce["steps_removed"] == 0, m.announce
        assert m.announce["gm_id"] == ident, m.announce


def test_ptp_master_to_host(sniffer: PtpSniffer) -> None:
    """ptp4l as slave follows node0 as grandmaster.

    IEEE 1588-2008 9.2.5: the host port becomes SLAVE to the better
    clock; 8.2.3: parentDS.grandmasterIdentity and grandmasterPriority1
    are those announced; 8.2.2: currentDS.stepsRemoved is 1 one hop from
    the grandmaster; 9.5.12 / 13.8: every Delay_Req is answered by a
    Delay_Resp with its sequenceId and requestingPortIdentity.  A
    free-running ptp4l never locks its servo, so its port stays in
    UNCALIBRATED (the slave branch of 9.2.5 Figure 23) instead of
    SLAVE.  PASS: pmc shows the node as grandmaster with the configured
    priority1 and clockClass, stepsRemoved 1, a positive meanPathDelay,
    and the sniffer sees a Delay_Resp for every host Delay_Req.
    """
    mac = node_mac(0)
    ident = clock_identity(mac)
    with Ptp4l("slave") as host:
        ptptl_start(0, "", runtime=35)
        state = _wait_state(host, SLAVE_STATES, 25)
        assert state in SLAVE_STATES, host.text()
        parent = host.pmc("PARENT_DATA_SET")
        assert _identity(parent["grandmasterIdentity"]) == ident, parent
        assert int(parent["grandmasterPriority1"]) == NODE_PRIORITY1
        assert int(parent["gm.ClockClass"]) == NODE_CLASS, parent
        time.sleep(5)
        current = host.pmc("CURRENT_DATA_SET")
        verdict, _status = ptptl_wait(0, 60)
    assert verdict["verdict"] == "PASS", verdict
    assert int(current["stepsRemoved"]) == 1, current
    assert float(current["meanPathDelay"]) > 0, current
    print(f"host: {state}, {current}")
    reqs = {m.seq for m in sniffer.messages(MSG_DELAY_REQ, src=HOST_MAC)}
    resps = sniffer.messages(MSG_DELAY_RESP, src=mac)
    assert reqs, "ptp4l sent no Delay_Req"
    answered = {m.seq for m in resps if m.req_id == HOST_ID}
    assert reqs - answered <= {max(reqs)}, (reqs, answered)


@pytest.mark.parametrize("better", ["host", "node"])
def test_ptp_bmca(sniffer: PtpSniffer, better: str) -> None:
    """Two masters: the lower priority1 wins the best master algorithm.

    IEEE 1588-2008 9.3.2.5 (Figure 27): the data set comparison starts
    with grandmasterPriority1, lower is better; 9.2.5 Table 10: the
    other port leaves the MASTER state.  ``host``: ptp4l priority1 127
    beats the node's 128, the node becomes slave to the host and stops
    sending Sync.  ``node``: ptp4l priority1 200 loses, the host port
    takes the slave branch (UNCALIBRATED, free running) with the node
    as grandmaster and the node never selects it.
    """
    mac = node_mac(0)
    ident = clock_identity(mac)
    prio = 127 if better == "host" else 200
    with Ptp4l("master", priority1=prio) as host:
        ptptl_start(0, "", runtime=25)
        state = _wait_state(
            host, {"MASTER"} if better == "host" else SLAVE_STATES, 25
        )
        parent = host.pmc("PARENT_DATA_SET")
        verdict, status = ptptl_wait(0, 50)
    assert verdict["verdict"] == "PASS", verdict
    if better == "host":
        assert state == "MASTER", host.text()
        assert status[-1]["valid"] == 1 and status[-1]["gm"] == HOST_ID
        first_ann = sniffer.messages(MSG_ANNOUNCE, src=HOST_MAC)[0].t
        late = [
            m
            for m in sniffer.messages(MSG_SYNC, src=mac)
            if m.t > first_ann + RECEIPT_TIMEOUT * PTP4L_ANNOUNCE_S
        ]
        assert not late, f"node kept sending Sync: {len(late)}"
    else:
        assert state in SLAVE_STATES, host.text()
        assert _identity(parent["grandmasterIdentity"]) == ident, parent
        assert all(s["valid"] == 0 for s in status), status


def test_ptp_delay_p2p(sniffer: PtpSniffer) -> None:
    """Peer delay mechanism between node0 and a ptp4l master.

    IEEE 1588-2008 11.4.3: the requester sends Pdelay_Req; the responder
    answers with Pdelay_Resp carrying the sequenceId and the
    requestingPortIdentity, and with Pdelay_Resp_Follow_Up in two-step
    operation.  PASS: every node Pdelay_Req but the last is answered by
    both, the source is valid and the measured path delay is not
    negative.
    """
    mac = node_mac(0)
    ident = clock_identity(mac)
    with Ptp4l("master", delay="p2p"):
        verdict, status = _client_run("-d p2p", 30)
    assert verdict["verdict"] == "PASS", verdict
    assert status[-1]["valid"] == 1 and status[-1]["path"] >= 0, status[-1]
    reqs = [m.seq for m in sniffer.messages(MSG_PDELAY_REQ, src=mac)]
    assert len(reqs) >= 3, reqs
    for msgtype in (MSG_PDELAY_RESP, MSG_PDELAY_RESP_FUP):
        got = {
            m.seq
            for m in sniffer.messages(msgtype, src=HOST_MAC)
            if m.req_id == ident
        }
        assert set(reqs[:-1]) <= got, (msgtype, reqs, got)


@pytest.mark.dep_config("CONFIG_NET_PKT")
def test_ptp_transport_raw(sniffer: PtpSniffer) -> None:
    """node0 synchronizes over the IEEE 802.3 transport.

    IEEE 1588-2008 Annex F: PTP messages carried directly in Ethernet
    frames with EtherType 88-F7.  PASS: every node message uses that
    EtherType and the client synchronizes as over UDP.
    """
    mac = node_mac(0)
    with Ptp4l("master", transport="l2"):
        verdict, status = _client_run("-r", 40)
    assert verdict["verdict"] == "PASS", verdict
    assert status[-1]["valid"] == 1 and status[-1]["gm"] == HOST_ID
    msgs = sniffer.messages(src=mac)
    assert msgs and all(m.l2 for m in msgs), msgs[:3]
    _assert_offsets(_per_type(msgs, MSG_DELAY_REQ))


def test_ptp_node_to_node(sniffer: PtpSniffer) -> None:
    """node1 synchronizes to node0 as grandmaster; the host only watches.

    IEEE 1588-2008 9.2.5, 11.2.  PASS: node1 reports node0's identity as
    grandmaster and the originTimestamps of node1 (Delay_Req) and node0
    (Sync) agree within SYNC_BOUND_MS, both measured against the host
    clock.
    """
    if len(pytest.products) < 2:
        pytest.skip("needs two nodes")
    mac0 = node_mac(0)
    mac1 = node_mac(1)
    ptptl_start(0, "", runtime=45)
    ptptl_start(1, "-c -w 30", runtime=40)
    verdict1, status1 = ptptl_wait(1, 70)
    verdict0, _status0 = ptptl_wait(0, 30)
    assert verdict1["verdict"] == "PASS", verdict1
    assert verdict0["verdict"] == "PASS", verdict0
    assert status1[-1]["gm"] == clock_identity(mac0), status1[-1]
    off1 = _last(offsets_ms(sniffer.messages(MSG_DELAY_REQ, src=mac1)), 3)
    off0 = _last(offsets_ms(sniffer.messages(MSG_SYNC, src=mac0)), 3)
    diff = sum(off1) / 3 - sum(off0) / 3
    print(f"node1 - node0 offset: {diff:.3f} ms")
    assert abs(diff) <= SYNC_BOUND_MS, (off1, off0)


def test_ptp_stop_restart(net_leak: LeakCheck) -> None:
    """Starting and stopping the daemon three times leaks nothing.

    ``include/netutils/ptpd.h`` ptpd_stop(): "On success, returns OK".
    The runs are in the foreground, so NSH has reaped the task before
    ``free`` is read.  PASS: each run ends with ``stop=0`` and
    ``exit=0``, and the heap (``free``) and the IOB and connection
    counters are back at the values before the test.
    """
    heap = HeapCheck()
    for _ in range(3):
        verdict, _status = ptptl_run(0, "", runtime=5)
        assert verdict["verdict"] == "PASS", verdict
        assert verdict["stop"] == 0 and verdict["exit"] == 0, verdict
    heap.check()
    net_leak.check([0])
