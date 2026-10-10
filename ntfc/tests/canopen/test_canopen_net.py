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

"""CANopen network: a ``conode`` master, three ``conode`` slaves, the host.

Master node-ID 1 (node 0), slaves 2-4 (nodes 1-3), host node 5.  The
master produces SYNC every 100 ms, consumes the heartbeat of nodes 2-5
(500 ms) and boots every node that sends a boot-up: SDO write and read
back of ``0x2000``, then NMT start.  Every node sends a counter in TPDO1
on each SYNC and receives TPDO1 of all other nodes.  See
``_canopen_net.py`` for the object dictionary.
"""

import random
import re
import struct
import threading
import time
from typing import Dict, Iterator, List

import pytest
from _canopen_common import (
    COB_HB,
    COB_SYNC,
    COB_TPDO1,
    NMT_PREOP,
    NMT_START,
    NMT_STOP,
    STATE_OPERATIONAL,
    STATE_PREOP,
    STATE_STOPPED,
    HostBus,
    periods,
)
from _canopen_net import (
    ALL_IDS,
    HB_MS,
    HBC_MS,
    HOST_ID,
    MASTER_ID,
    NUTTX_IDS,
    OBJ_BOOTUPS,
    OBJ_BOOTVAL,
    OBJ_HBRES,
    OBJ_HBTMO,
    OBJ_NMTST,
    OBJ_SCRATCH,
    OBJ_TXERR,
    SLAVE_IDS,
    SYNC_MS,
    Network,
    bootval,
    counters,
    peers,
    wait_until,
)

pytestmark = [
    pytest.mark.dep_config("CONFIG_TESTLAB_CONODE"),
    pytest.mark.cmd_check("conode_main"),
]

SLAVES = ALL_IDS[1:]
LOAD_SYNC_MS = 2
LOAD_TIME_S = 5.0

KILL_BUG = (
    "nuttx sched/signal/sig_default.c: a task terminated by a signal "
    "default action (_exit()) while blocked in poll() never runs "
    "poll_teardown(), so the file reference taken in poll_setup() "
    "(fs/vfs/fs_poll.c) is leaked and the CAN socket is never closed; its "
    "connection keeps holding received frames and no other CAN socket on "
    "the node receives anything"
)
LELY_BOOTUP_BUG = (
    "lely-core src/co/nmt_hb.c co_nmt_hb_recv(): the master's heartbeat "
    "consumer ignores a boot-up without recording state 0, so a node that "
    "reboots into its previous state is never reported as a state change "
    "and the master's view stays at BOOTUP"
)


@pytest.fixture(scope="module")
def net() -> Iterator[Network]:
    """Boot the CANopen network for the module.

    :return: the running network
    """
    bus = HostBus()
    network = Network(bus)
    try:
        network.boot()
        yield network
    finally:
        try:
            network.shutdown()
        finally:
            bus.close()


def _boots(net: Network) -> Dict[int, int]:
    """Return the number of boot-ups the master has seen of each node.

    :param net: network
    :return: boot-ups by node-ID
    """
    return net.master_view(OBJ_BOOTUPS)


def _check_bootvals(net: Network) -> None:
    """Check that every node holds the value of its last boot.

    :param net: network
    """
    boots = _boots(net)
    for node_id in SLAVE_IDS:
        value = net.upload(node_id, OBJ_BOOTVAL)
        assert value == bootval(boots[node_id], node_id), (node_id, value)
    assert net.host.bootval() == bootval(boots[HOST_ID], HOST_ID)


def _check_pdo_rx(net: Network, minimum: int) -> None:
    """Check TPDO1 of every peer arrived without a gap, no TX errors.

    :param net: network
    :param minimum: minimum number of PDOs per producer
    """
    bad = {}
    for node_id in NUTTX_IDS:
        txerr = net.upload(node_id, OBJ_TXERR)
        if txerr:
            bad[node_id] = txerr
        for peer in peers(node_id):
            cnt, lost, nonincr = net.rx(node_id, peer)
            if cnt < minimum or lost or nonincr:
                bad[(node_id, peer)] = (cnt, lost, nonincr)
    assert not bad, bad


def _tpdo_values(net: Network, mark: int, node_id: int) -> List[int]:
    """Return the TPDO1 counters a NuttX node sent after a mark.

    :param net: network
    :param mark: frame log mark
    :param node_id: node-ID
    :return: counter values in order
    """
    return counters(net.bus.log.since(mark, COB_TPDO1 + node_id))


def test_canopen_net_boot(net: Network) -> None:
    """The master boots every node to OPERATIONAL.

    :param net: network
    """
    assert net.master_view(OBJ_NMTST) == dict.fromkeys(
        SLAVES, STATE_OPERATIONAL
    )
    assert all(b >= 1 for b in _boots(net).values()), _boots(net)
    assert net.wait_states(ALL_IDS, STATE_OPERATIONAL)
    assert net.master_view(OBJ_HBTMO) == dict.fromkeys(SLAVES, 0)


def test_canopen_net_master_sdo(net: Network) -> None:
    """The master's boot SDO write reached each node.

    :param net: network
    """
    _check_bootvals(net)


def test_canopen_net_heartbeat(net: Network) -> None:
    """Every NuttX node sends an OPERATIONAL heartbeat every 100 ms.

    :param net: network
    """
    mark = net.bus.log.mark()
    time.sleep(2.0)
    for node_id in NUTTX_IDS:
        beats = net.bus.log.since(mark, COB_HB + node_id)
        assert {f.data for f in beats} == {bytes([STATE_OPERATIONAL])}
        gaps = periods(beats)
        mean = sum(gaps) / len(gaps)
        assert 0.8 * HB_MS / 1000 <= mean <= 1.25 * HB_MS / 1000, gaps
        assert max(gaps) < 2 * HB_MS / 1000, (node_id, gaps)


def test_canopen_net_pdo(net: Network) -> None:
    """TPDO1 of every node reaches every other node; counters are gapless.

    The NuttX slaves receive each other's TPDOs, the master's and the
    host's (slave-to-slave PDO exchange).

    :param net: network
    """
    net.reset_rx()
    mark = net.bus.log.mark()
    time.sleep(20 * SYNC_MS / 1000)
    frames = net.bus.log.since(mark)
    syncs = len([f for f in frames if f.can_id == COB_SYNC])
    assert syncs >= 15, syncs
    _check_pdo_rx(net, syncs - 3)
    for node_id in NUTTX_IDS:
        values = counters(
            [f for f in frames if f.can_id == COB_TPDO1 + node_id]
        )
        assert abs(len(values) - syncs) <= 1, (node_id, values)
        assert values == list(range(values[0], values[0] + len(values)))


def test_canopen_net_host_sdo_concurrent(net: Network) -> None:
    """The host runs SDO transfers to all NuttX nodes at once.

    Each thread writes a random value to ``0x2001`` (expedited and
    segmented), reads it back and reads ``0x1018:1``, 30 times.

    :param net: network
    """
    errors: List[str] = []

    def worker(node_id: int) -> None:
        sdo = net.remote[node_id].sdo
        rng = random.Random(node_id)
        try:
            for i in range(30):
                data = struct.pack("<I", rng.getrandbits(32))
                sdo.download(OBJ_SCRATCH, 0, data, force_segment=bool(i & 1))
                if sdo.upload(OBJ_SCRATCH, 0) != data:
                    errors.append(f"node {node_id}: readback {i}")
                if sdo.upload(0x1018, 1) != struct.pack("<I", 0x360):
                    errors.append(f"node {node_id}: 0x1018:1 {i}")
        except Exception as exc:
            errors.append(f"node {node_id}: {exc!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in NUTTX_IDS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors


def test_canopen_net_nmt_broadcast(net: Network) -> None:
    """NMT stop, pre-operational and start to all nodes.

    The master ignores NMT commands and keeps sending SYNC and its TPDO;
    the slaves follow each command and send TPDO1 only in OPERATIONAL.

    :param net: network
    """
    try:
        for cs, state in (
            (NMT_STOP, STATE_STOPPED),
            (NMT_PREOP, STATE_PREOP),
        ):
            net.nmt(cs, 0)
            assert net.wait_states(SLAVES, state), cs
            assert net.wait_view(OBJ_NMTST, SLAVES, state, 2), cs
            mark = net.bus.log.mark()
            time.sleep(5 * SYNC_MS / 1000)
            assert net.bus.states(mark, MASTER_ID)[-1] == STATE_OPERATIONAL
            assert len(net.bus.log.since(mark, COB_SYNC)) >= 4
            assert len(_tpdo_values(net, mark, MASTER_ID)) >= 4
            for node_id in SLAVE_IDS:
                assert not _tpdo_values(net, mark, node_id), (cs, node_id)
    finally:
        net.nmt(NMT_START, 0)

    assert net.wait_states(SLAVES, STATE_OPERATIONAL)
    assert net.wait_view(OBJ_NMTST, SLAVES, STATE_OPERATIONAL, 2)
    net.reset_rx()
    mark = net.bus.log.mark()
    time.sleep(10 * SYNC_MS / 1000)
    for node_id in SLAVE_IDS:
        assert len(_tpdo_values(net, mark, node_id)) >= 8, node_id
    _check_pdo_rx(net, 7)


def test_canopen_net_hb_timeout(net: Network) -> None:
    """The master detects a lost slave and boots it again after restart.

    ``conode`` on node-ID 4 is stopped (SIGTERM, socket closed) and
    started again.

    :param net: network
    """
    node_id = 4
    tmo = net.upload(MASTER_ID, OBJ_HBTMO, node_id)
    res = net.upload(MASTER_ID, OBJ_HBRES, node_id)
    boots = net.upload(MASTER_ID, OBJ_BOOTUPS, node_id)

    net.stop(node_id)
    stopped = time.monotonic()
    assert net.wait_view(OBJ_HBTMO, (node_id,), tmo + 1, 2.0)
    detect = time.monotonic() - stopped
    assert detect <= (HBC_MS + 2 * HB_MS) / 1000 + 0.5, detect
    assert net.bus.silent(node_id)
    assert net.upload(MASTER_ID, OBJ_HBRES, node_id) == res

    net.start(node_id)
    assert net.wait_states((node_id,), STATE_OPERATIONAL)
    assert net.upload(MASTER_ID, OBJ_HBRES, node_id) == res + 1
    assert net.upload(MASTER_ID, OBJ_BOOTUPS, node_id) == boots + 1
    assert net.upload(node_id, OBJ_BOOTVAL) == bootval(boots + 1, node_id)

    net.reset_rx()
    time.sleep(10 * SYNC_MS / 1000)
    _check_pdo_rx(net, 7)


def test_canopen_net_duplicate_bootup(net: Network) -> None:
    """A second boot-up of a running node makes the master boot it again.

    The host sends a boot-up with node-ID 3 while node 3 runs: the
    master repeats the SDO write and NMT start, node 3 stays OPERATIONAL
    and its PDO counter continues.

    :param net: network
    """
    node_id = 3
    boots = net.upload(MASTER_ID, OBJ_BOOTUPS, node_id)
    net.reset_rx()
    mark = net.bus.log.mark()
    net.bus.send(COB_HB + node_id, bytes([0]))

    # Wait for the NMT start that ends the boot: node 3 has a single SDO
    # server, so the host must not use it while the master does

    start = bytes([NMT_START, node_id])
    assert net.bus.log.wait_for(
        mark, 0, lambda f: f.data == start
    ), net.bus.log.since(mark, 0)
    assert net.upload(node_id, OBJ_BOOTVAL) == bootval(boots + 1, node_id)
    assert net.upload(MASTER_ID, OBJ_BOOTUPS, node_id) == boots + 1
    time.sleep(5 * SYNC_MS / 1000)
    states = net.bus.states(mark, node_id)
    assert set(states) == {STATE_OPERATIONAL}, states
    _check_pdo_rx(net, 4)


@pytest.mark.xfail(strict=True, reason=LELY_BOOTUP_BUG)
def test_canopen_net_duplicate_bootup_state(net: Network) -> None:
    """After a duplicate boot-up the master sees node 3 OPERATIONAL again.

    :param net: network
    """
    node_id = 3
    net.bus.send(COB_HB + node_id, bytes([0]))
    time.sleep(0.5)
    assert net.wait_view(OBJ_NMTST, (node_id,), STATE_OPERATIONAL, 2)


def test_canopen_net_bus_load(net: Network) -> None:
    """SYNC every 2 ms for 5 s: no PDO lost, no heartbeat timeout.

    The master's SYNC period (``0x1006``) is changed over SDO.

    :param net: network
    """
    tmo = net.master_view(OBJ_HBTMO)
    net.reset_rx()
    net.download(MASTER_ID, 0x1006, 0, "<I", LOAD_SYNC_MS * 1000)
    time.sleep(0.2)
    mark = net.bus.log.mark()
    time.sleep(LOAD_TIME_S)
    end = net.bus.log.mark()
    net.download(MASTER_ID, 0x1006, 0, "<I", SYNC_MS * 1000)

    frames = net.bus.log.since(mark)[: end - mark]
    syncs = [f for f in frames if f.can_id == COB_SYNC]
    gaps = periods(syncs)
    mean = sum(gaps) / len(gaps)
    assert mean <= 1.25 * LOAD_SYNC_MS / 1000, mean
    time.sleep(0.3)
    _check_pdo_rx(net, int(0.95 * len(syncs)))
    for node_id in NUTTX_IDS:
        values = _tpdo_values(net, mark, node_id)
        assert values == list(range(values[0], values[0] + len(values)))
    assert net.master_view(OBJ_HBTMO) == tmo


def test_canopen_net_master_restart(net: Network) -> None:
    """A restarted master resets and boots the whole network again.

    Stopping the master prints its PDO verdict.

    :param net: network
    """
    net.reset_rx()
    time.sleep(10 * SYNC_MS / 1000)
    out = net.stop(MASTER_ID)
    found = re.search(r"conode: (PASS|FAIL) rx=(\d+) lost=(\d+)", out)
    assert found and found.group(1) == "PASS", out
    assert int(found.group(2)) >= 4 * 7, out

    mark = net.bus.log.mark()
    net.start(MASTER_ID)
    assert wait_until(
        lambda: _boots(net) == dict.fromkeys(SLAVES, 1), 5.0
    ), _boots(net)
    assert net.wait_states(ALL_IDS, STATE_OPERATIONAL)
    for node_id in SLAVE_IDS:
        assert 0 in net.bus.states(mark, node_id), node_id
    _check_bootvals(net)


@pytest.mark.skip(reason=KILL_BUG)
def test_canopen_net_kill_restart(net: Network) -> None:
    """A slave killed with SIGKILL and restarted is booted again.

    :param net: network
    """
    node_id = 4
    tmo = net.upload(MASTER_ID, OBJ_HBTMO, node_id)
    net.kill(node_id)
    assert net.wait_view(OBJ_HBTMO, (node_id,), tmo + 1, 2.0)
    time.sleep(1.0)
    net.start(node_id)
    assert net.wait_states((node_id,), STATE_OPERATIONAL)
    assert net.upload(node_id, 0x1018, 1) == 0x360
