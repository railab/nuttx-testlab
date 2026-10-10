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

"""CANopen: Lely CANopen examples on the nodes against python-canopen.

NuttX slave: ``coslave`` (examples/lely_slave), node-ID 2, heartbeat
50 ms, starts in PRE-OPERATIONAL; TPDO1 (COB-ID ``0x182``, synchronous,
every SYNC) maps the counter ``0x2100``, incremented every 100 ms.

NuttX master: ``comaster`` (examples/lely_master), node-ID 1, heartbeat
50 ms, SYNC producer every 100 ms. 500 ms after start it reads object
``0x1000`` of node 2 over SDO, then starts node 2 (NMT) and prints every
change of its RPDO1 (COB-ID ``0x182``) counter.

Both examples exit on NMT reset node.
"""

import datetime
import re
import struct
import time
from typing import Iterator, List

import canopen
import pytest
from _can_common import KVASER_RX_BUG, kvaser_node
from _canopen_common import (
    COB_HB,
    COB_SYNC,
    COB_TIME,
    COB_TPDO1,
    HB_PERIOD_S,
    MASTER_ID,
    NMT_PREOP,
    NMT_RESET_COMM,
    NMT_RESET_NODE,
    NMT_START,
    NMT_STOP,
    SLAVE_ID,
    STATE_BOOTUP,
    STATE_OPERATIONAL,
    STATE_PREOP,
    STATE_STOPPED,
    SYNC_PERIOD_S,
    HostBus,
    app_pid,
    core,
    periods,
    start_app,
    stop_app,
    time_of_day,
)
from canopen.objectdictionary import UNSIGNED16, UNSIGNED32, ODVariable

pytestmark = [
    pytest.mark.dep_config("CONFIG_EXAMPLES_LELYSLAVE"),
    pytest.mark.dep_config("CONFIG_EXAMPLES_LELYMASTER"),
    pytest.mark.cmd_check("coslave_main"),
    pytest.mark.cmd_check("comaster_main"),
]

SLAVE_RUNNING = r"slave running \(node 0x02\)"
MASTER_OPERATIONAL = r"NMT: master \+ slave 0x02 OPERATIONAL"
PDO_RX_RE = r"PDO rx: counter = (\d+)"

HOST_DEVICE_TYPE = 0x00020192

ABORT_READ_ONLY = 0x06010002
ABORT_NO_OBJECT = 0x06020000
ABORT_NO_SUBINDEX = 0x06090011
ABORT_LEN_LOW = 0x06070013

CHARDEV_EDL_BUG = (
    "examples/lely_slave, examples/lely_master candev_char.c: "
    "*_candev_send() leaves cm_hdr.ch_edl/ch_brs/ch_esi of the stack "
    "struct can_msg_s uninitialized, so with CONFIG_CAN_FD a frame can go "
    "out as a CAN FD frame"
)
CHARDEV_RECV_BUG = (
    "examples/lely_slave, examples/lely_master candev_char.c: "
    "*_candev_recv() read()s into one struct can_msg_s and decodes only "
    "the first message, but the CAN character driver returns every queued "
    "message that fits the buffer, so frames that arrive together are lost"
)


@pytest.fixture(scope="module")
def bus() -> Iterator[HostBus]:
    """Join ``can0`` with python-canopen for the module."""
    host = HostBus()
    yield host
    host.close()


@pytest.fixture
def slave(bus: HostBus) -> Iterator[canopen.RemoteNode]:
    """Run ``coslave`` on node 0 and attach a host SDO client to it.

    The first frame ``coslave`` sends must be its boot-up message.

    :param bus: host bus
    :return: python-canopen remote node for node-ID 2
    """
    mark = bus.log.mark()
    out = start_app(0, "coslave", SLAVE_RUNNING)
    pid = app_pid(out, "coslave")
    node = canopen.RemoteNode(SLAVE_ID, canopen.ObjectDictionary())
    node.sdo.RESPONSE_TIMEOUT = 1.0
    bus.net.add_node(node)
    try:
        assert re.search(SLAVE_RUNNING, out), out
        assert bus.log.wait_for(mark, COB_HB + SLAVE_ID), "no boot-up"
        assert bus.states(mark, SLAVE_ID)[0] == STATE_BOOTUP
        yield node
    finally:
        del bus.net[SLAVE_ID]
        stop_app(bus, 0, SLAVE_ID, pid)


def _host_slave(bus: HostBus, device_type: bool) -> canopen.LocalNode:
    """Create a python-canopen slave (node-ID 2) and boot it.

    :param bus: host bus
    :param device_type: provide object ``0x1000``
    :return: local node, PRE-OPERATIONAL, no heartbeat (``comaster`` does
     not consume heartbeats)
    """
    od = canopen.ObjectDictionary()
    if device_type:
        var = ODVariable("Device type", 0x1000, 0)
        var.data_type = UNSIGNED32
        var.access_type = "ro"
        var.default = HOST_DEVICE_TYPE
        od.add_object(var)
    var = ODVariable("Producer heartbeat time", 0x1017, 0)
    var.data_type = UNSIGNED16
    var.access_type = "rw"
    var.default = 0
    od.add_object(var)

    node = canopen.LocalNode(SLAVE_ID, od)
    bus.net.add_node(node)
    node.nmt.send_command(NMT_RESET_NODE)
    node.nmt.send_command(NMT_PREOP)
    return node


def _pdo_values(output: str) -> List[int]:
    """Return the counter values ``comaster`` printed.

    :param output: console output
    :return: counter values in order
    """
    return [int(v) for v in re.findall(PDO_RX_RE, output)]


def _is_chardev(node: int) -> bool:
    """Check whether a node's examples use the CAN character driver.

    :param node: product index
    :return: True for the character driver backend
    """
    return bool(core(node).conf.kv_check("CONFIG_EXAMPLES_LELYSLAVE_CHARDEV"))


def test_canopen_slave_boot(bus: HostBus, slave: canopen.RemoteNode) -> None:
    """After boot-up the slave sends PRE-OPERATIONAL heartbeats every 50 ms.

    :param bus: host bus
    :param slave: node 0 slave
    """
    mark = bus.log.mark()
    time.sleep(2.0)
    beats = bus.log.since(mark, COB_HB + SLAVE_ID)
    assert {f.data for f in beats} == {bytes([STATE_PREOP])}
    gaps = periods(beats)
    mean = sum(gaps) / len(gaps)
    assert 0.8 * HB_PERIOD_S <= mean <= 1.25 * HB_PERIOD_S, gaps
    assert max(gaps) < 2 * HB_PERIOD_S, gaps


def test_canopen_slave_nmt(bus: HostBus, slave: canopen.RemoteNode) -> None:
    """NMT commands move the slave between states; others are ignored.

    :param bus: host bus
    :param slave: node 0 slave
    """
    steps = [
        (NMT_START, SLAVE_ID, STATE_OPERATIONAL),
        (NMT_STOP, SLAVE_ID, STATE_STOPPED),
        (NMT_PREOP, SLAVE_ID, STATE_PREOP),
        (NMT_START, 0, STATE_OPERATIONAL),
        (NMT_STOP, SLAVE_ID + 1, STATE_OPERATIONAL),
        (NMT_PREOP, 0, STATE_PREOP),
    ]
    for cs, node_id, state in steps:
        bus.nmt(cs, node_id)
        assert bus.last_state(SLAVE_ID) == state, (cs, node_id)

    mark = bus.log.mark()
    bus.nmt(NMT_RESET_COMM, SLAVE_ID)
    time.sleep(0.3)
    states = bus.states(mark, SLAVE_ID)
    assert STATE_BOOTUP in states, states
    after = states[states.index(STATE_BOOTUP) + 1 :]
    assert after and set(after) == {STATE_PREOP}, states


def test_canopen_slave_frame_burst(
    request: pytest.FixtureRequest, bus: HostBus, slave: canopen.RemoteNode
) -> None:
    """Back-to-back NMT start and stop leave the slave STOPPED.

    :param request: pytest request
    :param bus: host bus
    :param slave: node 0 slave
    """
    if _is_chardev(0):
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=CHARDEV_RECV_BUG)
        )

    for _ in range(3):
        bus.nmt(NMT_PREOP, SLAVE_ID)
        assert bus.last_state(SLAVE_ID) == STATE_PREOP
        bus.nmt(NMT_START, SLAVE_ID)
        bus.nmt(NMT_STOP, SLAVE_ID)
        assert bus.last_state(SLAVE_ID) == STATE_STOPPED


def test_canopen_slave_sdo_read(slave: canopen.RemoteNode) -> None:
    """Expedited SDO uploads return the example's object dictionary.

    :param slave: node 0 slave
    """
    expect = [
        (0x1000, 0, 0),
        (0x1005, 0, 0x80),
        (0x1017, 0, 50),
        (0x1018, 0, 4),
        (0x1018, 1, 0x360),
        (0x1800, 1, 0x182),
        (0x1800, 2, 1),
        (0x1A00, 0, 1),
        (0x1A00, 1, 0x21000020),
        (0x1F80, 0, 4),
    ]
    for index, sub, value in expect:
        data = slave.sdo.upload(index, sub)
        assert int.from_bytes(data, "little") == value, (hex(index), sub)


def test_canopen_slave_sdo_write(slave: canopen.RemoteNode) -> None:
    """Expedited, segmented and block SDO downloads read back.

    :param slave: node 0 slave
    """
    slave.sdo.download(0x2000, 0, struct.pack("<I", 0x12345678))
    assert slave.sdo.upload(0x2000, 0) == struct.pack("<I", 0x12345678)

    data = struct.pack("<I", 0xCAFEF00D)
    slave.sdo.download(0x2000, 0, data, force_segment=True)
    assert slave.sdo.upload(0x2000, 0) == data

    data = struct.pack("<I", 0x0BADC0DE)
    with slave.sdo.open(0x2000, 0, "wb", size=4, block_transfer=True) as f:
        f.write(data)
    with slave.sdo.open(0x2000, 0, "rb", block_transfer=True) as f:
        assert f.read() == data


@pytest.mark.parametrize(
    "index, sub, data, code",
    [
        pytest.param(0x2001, 0, b"\1\0\0\0", ABORT_READ_ONLY, id="read-only"),
        pytest.param(0x3000, 0, None, ABORT_NO_OBJECT, id="no-object"),
        pytest.param(0x1018, 9, None, ABORT_NO_SUBINDEX, id="no-subindex"),
        pytest.param(0x2000, 0, b"\1\2", ABORT_LEN_LOW, id="short-data"),
    ],
)
def test_canopen_slave_sdo_abort(
    slave: canopen.RemoteNode, index: int, sub: int, data: bytes, code: int
) -> None:
    """Invalid SDO requests get the CiA 301 abort code.

    :param slave: node 0 slave
    :param index: object index
    :param sub: sub-index
    :param data: data to download, None for an upload
    :param code: expected abort code
    """
    with pytest.raises(canopen.SdoAbortedError) as err:
        if data is None:
            slave.sdo.upload(index, sub)
        else:
            slave.sdo.download(index, sub, data)
    assert err.value.code == code, hex(err.value.code)


def test_canopen_slave_stopped(
    bus: HostBus, slave: canopen.RemoteNode
) -> None:
    """A STOPPED slave answers neither SDO nor SYNC, then recovers.

    :param bus: host bus
    :param slave: node 0 slave
    """
    bus.nmt(NMT_START, SLAVE_ID)
    assert bus.last_state(SLAVE_ID) == STATE_OPERATIONAL
    bus.nmt(NMT_STOP, SLAVE_ID)
    assert bus.last_state(SLAVE_ID) == STATE_STOPPED

    mark = bus.log.mark()
    slave.sdo.RESPONSE_TIMEOUT = 0.5
    with pytest.raises(canopen.SdoCommunicationError):
        slave.sdo.upload(0x1000, 0)
    bus.sync(3)
    assert not bus.log.since(mark, 0x580 + SLAVE_ID)
    assert not bus.log.since(mark, COB_TPDO1 + SLAVE_ID)

    bus.nmt(NMT_PREOP, SLAVE_ID)
    assert bus.last_state(SLAVE_ID) == STATE_PREOP
    assert slave.sdo.upload(0x1000, 0) == bytes(4)


def test_canopen_slave_heartbeat_time(
    bus: HostBus, slave: canopen.RemoteNode
) -> None:
    """Writing ``0x1017`` changes the heartbeat period; 0 disables it.

    :param bus: host bus
    :param slave: node 0 slave
    """
    slave.sdo.download(0x1017, 0, struct.pack("<H", 200))
    time.sleep(0.3)
    mark = bus.log.mark()
    time.sleep(2.1)
    gaps = periods(bus.log.since(mark, COB_HB + SLAVE_ID))
    mean = sum(gaps) / len(gaps)
    assert 0.18 <= mean <= 0.22, gaps

    slave.sdo.download(0x1017, 0, struct.pack("<H", 0))
    assert bus.silent(SLAVE_ID)
    assert slave.sdo.upload(0x1017, 0) == bytes(2)

    slave.sdo.download(0x1017, 0, struct.pack("<H", 50))
    assert bus.last_state(SLAVE_ID) == STATE_PREOP


def test_canopen_slave_tpdo(bus: HostBus, slave: canopen.RemoteNode) -> None:
    """TPDO1 answers each SYNC only in OPERATIONAL, with the counter.

    :param bus: host bus
    :param slave: node 0 slave
    """
    mark = bus.log.mark()
    bus.sync(3)
    assert not bus.log.since(mark, COB_TPDO1 + SLAVE_ID), "TPDO in PRE-OP"

    bus.nmt(NMT_START, SLAVE_ID)
    assert bus.last_state(SLAVE_ID) == STATE_OPERATIONAL
    mark = bus.log.mark()
    bus.sync(10)
    time.sleep(0.1)
    pdos = bus.log.since(mark, COB_TPDO1 + SLAVE_ID)
    assert len(pdos) == 10, pdos
    values = [struct.unpack("<I", f.data)[0] for f in pdos]
    assert all(len(f.data) == 4 for f in pdos), pdos
    assert values == sorted(values) and values[-1] > values[0], values


def test_canopen_slave_tpdo_config(
    bus: HostBus, slave: canopen.RemoteNode
) -> None:
    """SDO writes to ``0x1800`` move TPDO1 and change its SYNC rate.

    The PDO is disabled (COB-ID bit 31) before its COB-ID is changed.

    :param bus: host bus
    :param slave: node 0 slave
    """
    new_cob = 0x190
    slave.sdo.download(0x1800, 1, struct.pack("<I", 0x80000182))
    slave.sdo.download(0x1800, 2, bytes([2]))
    slave.sdo.download(0x1800, 1, struct.pack("<I", new_cob))
    bus.nmt(NMT_START, SLAVE_ID)
    assert bus.last_state(SLAVE_ID) == STATE_OPERATIONAL

    mark = bus.log.mark()
    bus.sync(10)
    time.sleep(0.1)
    assert not bus.log.since(mark, COB_TPDO1 + SLAVE_ID)
    assert len(bus.log.since(mark, new_cob)) == 5


def test_canopen_slave_time(bus: HostBus, slave: canopen.RemoteNode) -> None:
    """A TIME message sets the node's wall clock.

    :param bus: host bus
    :param slave: node 0 slave
    """
    bus.nmt(NMT_START, SLAVE_ID)
    assert bus.last_state(SLAVE_ID) == STATE_OPERATIONAL

    days = (datetime.date(2030, 1, 1) - datetime.date(1984, 1, 1)).days
    bus.send(COB_TIME, time_of_day(days, 12 * 3600 * 1000))
    time.sleep(0.3)
    ret = core(0).sendCommandReadUntilPattern(
        "date", pattern=r"\d\d:\d\d:\d\d \d{4}", timeout=10
    )
    assert re.search(r"Jan 01 12:00:\d\d 2030", ret.output), ret.output


def test_canopen_slave_reset_node(
    bus: HostBus, slave: canopen.RemoteNode
) -> None:
    """NMT reset node ends ``coslave``: its heartbeat stops.

    :param bus: host bus
    :param slave: node 0 slave
    """
    bus.nmt(NMT_RESET_NODE, SLAVE_ID)
    assert bus.silent(SLAVE_ID)
    ret = core(0).sendCommand("echo alive", "alive", timeout=10)
    assert ret == 0


@pytest.fixture
def host_slave(bus: HostBus) -> Iterator[canopen.LocalNode]:
    """Run a python-canopen slave (node-ID 2) with object ``0x1000``.

    :param bus: host bus
    :return: local node
    """
    node = _host_slave(bus, device_type=True)
    yield node
    del bus.net[SLAVE_ID]


def test_canopen_master_boot(
    bus: HostBus, host_slave: canopen.LocalNode
) -> None:
    """``comaster`` reads the host slave over SDO and starts it.

    It also produces its heartbeat (boot-up, then OPERATIONAL) and SYNC
    every 100 ms.

    :param bus: host bus
    :param host_slave: host slave
    """
    mark = bus.log.mark()
    out = start_app(0, "comaster", MASTER_OPERATIONAL)
    try:
        assert re.search(MASTER_OPERATIONAL, out), out
        assert f"(0x1000) = 0x{HOST_DEVICE_TYPE:08x}" in out, out
        assert host_slave.nmt.state == "OPERATIONAL"
        req = bus.log.since(mark, 0x600 + SLAVE_ID)
        assert req and req[0].data[:4] == bytes([0x40, 0x00, 0x10, 0x00])

        time.sleep(2.0)
        states = bus.states(mark, MASTER_ID)
        assert states[0] == STATE_BOOTUP, states
        assert set(states[1:]) == {STATE_OPERATIONAL}, states

        syncs = bus.log.since(mark, COB_SYNC)
        gaps = periods(syncs[1:])
        mean = sum(gaps) / len(gaps)
        assert 0.9 * SYNC_PERIOD_S <= mean <= 1.1 * SYNC_PERIOD_S, gaps
    finally:
        stop_app(bus, 0, MASTER_ID, app_pid(out, "comaster"))


def test_canopen_master_rpdo(
    bus: HostBus, host_slave: canopen.LocalNode
) -> None:
    """``comaster`` prints the counter of each PDO the host slave sends.

    :param bus: host bus
    :param host_slave: host slave
    """
    out = start_app(0, "comaster", MASTER_OPERATIONAL)
    try:
        assert re.search(MASTER_OPERATIONAL, out), out
        values = [7, 1000, 0xDEADBEEF]
        for value in values:
            bus.send(COB_TPDO1 + SLAVE_ID, struct.pack("<I", value))
            ret = core(0).readUntilPattern(
                rf"PDO rx: counter = {value}\D", timeout=5
            )
            out += ret.output
        assert _pdo_values(out)[-3:] == values, out
    finally:
        stop_app(bus, 0, MASTER_ID, app_pid(out, "comaster"))


def test_canopen_master_sdo_abort(bus: HostBus) -> None:
    """``comaster`` reports the abort code of a failed SDO read.

    :param bus: host bus
    """
    _host_slave(bus, device_type=False)
    try:
        out = start_app(0, "comaster", MASTER_OPERATIONAL)
        try:
            assert f"abort code 0x{ABORT_NO_OBJECT:08x}" in out, out
            assert re.search(MASTER_OPERATIONAL, out), out
        finally:
            stop_app(bus, 0, MASTER_ID, app_pid(out, "comaster"))
    finally:
        del bus.net[SLAVE_ID]


def test_canopen_node_to_node(
    request: pytest.FixtureRequest, bus: HostBus
) -> None:
    """``comaster`` on node 0 drives ``coslave`` on node 1.

    :param request: pytest request
    :param bus: host bus
    """
    if _is_chardev(0):
        pytest.skip(CHARDEV_RECV_BUG)

    if kvaser_node(1):
        request.applymarker(
            pytest.mark.xfail(strict=False, reason=KVASER_RX_BUG)
        )

    mark = bus.log.mark()
    slave_out = start_app(1, "coslave", SLAVE_RUNNING)
    master_out = ""
    try:
        assert bus.log.wait_for(mark, COB_HB + SLAVE_ID) is not None
        master_out = start_app(0, "comaster", MASTER_OPERATIONAL)
        assert re.search(MASTER_OPERATIONAL, master_out), master_out
        assert "(0x1000) = 0x00000000" in master_out, master_out
        ret = core(0).readUntilPattern(rf"(?:{PDO_RX_RE}\s+){{5}}", timeout=10)
        master_out += ret.output
        values = _pdo_values(master_out)
        assert len(values) >= 5, master_out
        assert values == sorted(values) and values[-1] > values[0], values
        assert bus.last_state(SLAVE_ID) == STATE_OPERATIONAL
        assert bus.log.since(mark, COB_TPDO1 + SLAVE_ID)
    finally:
        if master_out:
            stop_app(bus, 0, MASTER_ID, app_pid(master_out, "comaster"))
        stop_app(bus, 1, SLAVE_ID, app_pid(slave_out, "coslave"))


def test_canopen_classic_frames(
    bus: HostBus, slave: canopen.RemoteNode
) -> None:
    """Every frame ``coslave`` sends is a classic CAN frame.

    :param bus: host bus
    :param slave: node 0 slave
    """
    mark = bus.log.mark()
    bus.nmt(NMT_START, SLAVE_ID)
    assert bus.last_state(SLAVE_ID) == STATE_OPERATIONAL
    bus.sync(5)
    slave.sdo.upload(0x1018, 1)
    assert len(bus.log.since(mark, COB_TPDO1 + SLAVE_ID)) == 5
    fd = [f for f in bus.log.since(mark) if f.is_fd]
    if fd and _is_chardev(0):
        pytest.xfail(CHARDEV_EDL_BUG)
    assert not fd, fd
