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

"""Modbus RTU over a serial line: NuttX against pymodbus on the host.

The node's serial device (:data:`NODE_DEV`) and the host's
``/dev/ttyTL1`` are the two ends of one serial line
(``testenv/modbus-rtu.sh``). nxmodbus runs as ``nxmbserver`` slave
(unit 1) and ``nxmbclient`` master at 19200 baud; register maps: see
``_modbus_common``.
"""

import re
import struct
import time
from typing import Any, Iterator, List

import pytest
import serial
from _modbus_common import (
    ILLEGAL_DATA_ADDRESS,
    ILLEGAL_DATA_VALUE,
    REGS,
    WRITE_COILS_BUG,
    HostSlave,
    client_lines,
    items,
)
from pymodbus.client import ModbusSerialClient
from pymodbus.server import ModbusSerialServer

HOST_DEV = "/dev/ttyTL1"

BAUD = 19200
UNIT = 1

MIN_FRAME_BUG = (
    "industry/nxmodbus: the RTU receiver drops frames shorter than 5 "
    "bytes (NXMB_RTU_MIN_FRAME_SIZE), so a 4-byte Report Server ID "
    "(FC17) request gets no reply"
)
RESTART_BUG = (
    "examples/nxmbserver: g_running is set only at load time, so after "
    "one SIGINT every later run in the same boot exits at once"
)


def _core() -> Any:
    """Return the NTFC core handler of the node.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.products[0].core(0)


# The node's end of the serial line, by CONFIG_ARCH: the sim UART, the
# 16550 COM2 (qemu-intel64) or the virtio-serial console (rv-virt, and
# ttyS2 on qemu-armv8a, where the PL011 console is ttyS1).

NODE_DEV = {
    "sim": "/dev/ttyTL0",
    "x86_64": "/dev/ttyS1",
    "risc-v": "/dev/ttyS1",
    "arm64": "/dev/ttyS2",
}


def _node_dev() -> str:
    """Return the node's end of the serial line.

    :return: device path on the node
    """
    return NODE_DEV[str(_core().conf.kv_check("CONFIG_ARCH"))]


def _flush_host() -> None:
    """Drop stale bytes queued for the host end of the line."""
    with serial.Serial(HOST_DEV) as port:
        time.sleep(0.1)
        port.reset_input_buffer()


@pytest.fixture(scope="module", autouse=True)
def node_dev() -> None:
    """Check that the node end of the line exists."""
    dev = _node_dev()
    ret = _core().sendCommand(f"ls {dev}", dev.split("/")[-1])
    assert ret == 0, f"no {dev} on the node"


def _crc16(data: bytes) -> int:
    """Return the Modbus RTU CRC of a frame.

    :param data: address, function code and data
    :return: CRC-16/MODBUS
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _frame(unit: int, pdu: bytes) -> bytes:
    """Build an RTU frame.

    :param unit: slave address
    :param pdu: function code and data
    :return: frame with CRC (low byte first)
    """
    body = bytes([unit]) + pdu
    return body + struct.pack("<H", _crc16(body))


def _raw(frame: bytes, baud: int = BAUD, wait: float = 1.0) -> bytes:
    """Send one raw RTU frame from the host and read the reply.

    :param frame: complete frame, CRC included
    :param baud: line speed
    :param wait: how long to wait for the first reply byte, in seconds
    :return: reply frame, empty if there is none
    """
    with serial.Serial(
        HOST_DEV,
        baud,
        timeout=wait,
        inter_byte_timeout=0.1,
    ) as port:
        time.sleep(0.1)
        port.reset_input_buffer()
        port.write(frame)
        return bytes(port.read(256))


def _raw_pdu(unit: int, pdu: bytes, baud: int = BAUD) -> bytes:
    """Send one request PDU to a slave and return the reply PDU.

    :param unit: slave address
    :param pdu: request function code and data
    :param baud: line speed
    :return: reply PDU (CRC and address checked and stripped)
    """
    reply = _raw(_frame(unit, pdu), baud)
    assert len(reply) >= 4, f"short reply {reply.hex()}"
    body, crc = reply[:-2], struct.unpack("<H", reply[-2:])[0]
    assert crc == _crc16(body), f"bad CRC in {reply.hex()}"
    assert body[0] == unit, f"reply from unit {body[0]}"
    return body[1:]


def _master(baud: int = BAUD) -> ModbusSerialClient:
    """Open a pymodbus RTU master on the host end of the line.

    :param baud: line speed
    :return: connected client
    """
    _flush_host()
    client = ModbusSerialClient(HOST_DEV, baudrate=baud, timeout=2, retries=1)
    assert client.connect(), f"cannot open {HOST_DEV}"
    return client


def _client(cmd: str, timeout: int = 15) -> List[str]:
    """Run ``nxmbclient`` against the host end of the line.

    :param cmd: nxmbclient command and arguments
    :param timeout: command timeout in seconds
    :return: output lines after the command line
    """
    out = (
        _core()
        .sendCommandReadUntilPattern(
            f"nxmbclient -t rtu -d {_node_dev()} -b {BAUD} {cmd}",
            pattern=r"nsh> ",
            timeout=timeout,
        )
        .output
    )
    return client_lines(out)


def _host_slave() -> HostSlave:
    """Return a pymodbus RTU slave (unit 1) on the host end of the line.

    :return: slave, not yet started
    """
    _flush_host()
    return HostSlave(
        lambda dev: ModbusSerialServer(dev, port=HOST_DEV, baudrate=BAUD)
    )


@pytest.mark.cmd_check("nxmbclient_main")
def test_modbus_rtu_master_read() -> None:
    """The NuttX master reads all four tables of a host slave."""
    with _host_slave():
        out = _client("read-holding 10 5")
        assert items(out) == {i: 1000 + i for i in range(10, 15)}, out
        out = _client("read-input 95 5")
        assert items(out) == {i: 2000 + i for i in range(95, 100)}, out
        out = _client("read-coils 0 12")
        assert items(out) == {i: int(i % 3 == 0) for i in range(12)}, out
        out = _client("read-discrete 3 9")
        assert items(out) == {i: int(i % 2 == 0) for i in range(3, 12)}, out


@pytest.mark.cmd_check("nxmbclient_main")
def test_modbus_rtu_master_write() -> None:
    """Writes by the NuttX master arrive at a host slave as sent."""
    cmds = [
        ("write-holding 7 4242", (6, 7, [4242])),
        ("write-holdings 20 1 65535 0 32769", (16, 20, [1, 65535, 0, 32769])),
        ("write-coil 5 1", (5, 5, [True])),
        ("write-coil 6 0", (5, 6, [False])),
    ]
    with _host_slave() as slave:
        for cmd, _ in cmds:
            out = _client(cmd)
            assert "OK" in out, f"{cmd}: {out}"
        out = _client("read-holding 20 4")
        assert items(out) == {20: 1, 21: 65535, 22: 0, 23: 32769}, out
    assert slave.writes == [expect for _, expect in cmds]


@pytest.mark.cmd_check("nxmbclient_main")
@pytest.mark.xfail(strict=True, reason=WRITE_COILS_BUG)
def test_modbus_rtu_master_write_coils() -> None:
    """``write-coils`` by the NuttX master sets each listed coil."""
    with _host_slave() as slave:
        out = _client("write-coils 40 1 0 1 1 0 1")
        assert "OK" in out, out
        out = _client("read-coils 40 6")
        assert items(out) == {40: 1, 41: 0, 42: 1, 43: 1, 44: 0, 45: 1}, out
    assert slave.writes == [(15, 40, [True, False, True, True, False, True])]


@pytest.mark.cmd_check("nxmbclient_main")
def test_modbus_rtu_master_exception() -> None:
    """An exception reply and a silent line fail the command."""
    failed = "Error: read-holding failed:"
    with _host_slave():
        out = _client("read-holding 0 1")
        assert items(out) == {0: 1000}, out
        out = _client(f"read-holding {REGS} 1")
        assert any(line.startswith(failed) for line in out), out
        assert not items(out), out
    out = _client("read-holding 0 1")
    assert f"{failed} -110" in out, out


class NodeSlave:
    """``nxmbserver`` on the node, started once per boot (``RESTART_BUG``)."""

    def __init__(self) -> None:
        """Prepare, not started."""
        self.pid = 0

    def start(self) -> None:
        """Start ``nxmbserver`` in the background."""
        out = (
            _core()
            .sendCommandReadUntilPattern(
                f"nxmbserver -t rtu -d {_node_dev()} -b {BAUD} -p none &",
                pattern=r"Server running",
                timeout=10,
            )
            .output
        )
        found = re.search(r"nxmbserver \[(\d+):", out)
        assert found and "Server running" in out, out
        self.pid = int(found.group(1))

    def stop(self) -> None:
        """Stop ``nxmbserver`` with SIGINT, if started."""
        if self.pid:
            pid, self.pid = self.pid, 0
            ret = _core().sendCommand(
                f"kill -2 {pid}", "Shutting down", timeout=10
            )
            assert ret == 0, "nxmbserver did not stop"


@pytest.fixture(scope="module")
def node_slave() -> Iterator[NodeSlave]:
    """Run ``nxmbserver`` on the node for the slave tests.

    The slave tests come last in this module: the server holds the
    node's serial line until the end of the module.
    """
    slave = NodeSlave()
    slave.start()
    yield slave
    slave.stop()


@pytest.fixture
def master(node_slave: NodeSlave) -> Iterator[ModbusSerialClient]:
    """Open a pymodbus master to the ``nxmbserver`` slave.

    :param node_slave: node slave fixture
    :return: connected client
    """
    client = _master()
    yield client
    client.close()


@pytest.mark.cmd_check("nxmbserver_main")
def test_modbus_rtu_slave_read(master: ModbusSerialClient) -> None:
    """A host master reads all four tables of the NuttX slave."""
    rsp = master.read_holding_registers(0, count=10, device_id=UNIT)
    assert not rsp.isError(), rsp
    assert rsp.registers == [i * 100 for i in range(10)]
    rsp = master.read_input_registers(90, count=10, device_id=UNIT)
    assert not rsp.isError(), rsp
    assert rsp.registers == [i * 10 for i in range(90, 100)]
    rsp = master.read_coils(0, count=20, device_id=UNIT)
    assert not rsp.isError(), rsp
    assert rsp.bits[:20] == [False] * 20
    rsp = master.read_discrete_inputs(80, count=20, device_id=UNIT)
    assert not rsp.isError(), rsp
    assert rsp.bits[:20] == [False] * 20


@pytest.mark.cmd_check("nxmbserver_main")
def test_modbus_rtu_slave_write(master: ModbusSerialClient) -> None:
    """Writes by a host master read back the same, FC23 included."""
    regs = [0x1234, 0xFFFF, 0, 0x8001]
    coils = [True, False, True, True, False, False, True, False, True]
    assert not master.write_register(7, 4242, device_id=UNIT).isError()
    assert not master.write_registers(20, regs, device_id=UNIT).isError()
    assert not master.write_coil(5, True, device_id=UNIT).isError()
    assert not master.write_coils(40, coils, device_id=UNIT).isError()
    rsp = master.readwrite_registers(
        read_address=20,
        read_count=4,
        write_address=60,
        values=[6, 7],
        device_id=UNIT,
    )
    assert not rsp.isError(), rsp
    assert rsp.registers == regs
    rsp = master.read_holding_registers(7, count=1, device_id=UNIT)
    assert rsp.registers == [4242]
    rsp = master.read_holding_registers(59, count=4, device_id=UNIT)
    assert rsp.registers == [5900, 6, 7, 6200]
    assert master.read_coils(5, count=1, device_id=UNIT).bits[0] is True
    rsp = master.read_coils(40, count=9, device_id=UNIT)
    assert rsp.bits[:9] == coils


@pytest.mark.cmd_check("nxmbserver_main")
def test_modbus_rtu_slave_diag(node_slave: NodeSlave) -> None:
    """Diagnostics Return Query Data (FC08/0) echoes the request.

    :param node_slave: node slave fixture
    """
    echo = struct.pack(">BHH", 8, 0, 0xA55A)
    assert _raw_pdu(UNIT, echo) == echo


@pytest.mark.cmd_check("nxmbserver_main")
@pytest.mark.xfail(strict=True, reason=MIN_FRAME_BUG)
def test_modbus_rtu_slave_id(node_slave: NodeSlave) -> None:
    """Report Server ID (FC17): byte count, unit ID, running.

    :param node_slave: node slave fixture
    """
    assert _raw_pdu(UNIT, b"\x11") == bytes([0x11, 2, UNIT, 0xFF])


@pytest.mark.cmd_check("nxmbserver_main")
@pytest.mark.parametrize(
    "pdu, code",
    [
        pytest.param(
            struct.pack(">BHH", 3, REGS, 1),
            ILLEGAL_DATA_ADDRESS,
            id="read-past-end",
        ),
        pytest.param(
            struct.pack(">BHH", 3, REGS - 5, 10),
            ILLEGAL_DATA_ADDRESS,
            id="read-across-end",
        ),
        pytest.param(
            struct.pack(">BHH", 3, 0, 0), ILLEGAL_DATA_VALUE, id="read-zero"
        ),
        pytest.param(
            struct.pack(">BHH", 3, 0, 126),
            ILLEGAL_DATA_VALUE,
            id="read-126",
        ),
        pytest.param(
            struct.pack(">BHH", 5, 0, 0x1234),
            ILLEGAL_DATA_VALUE,
            id="coil-bad-value",
        ),
    ],
)
def test_modbus_rtu_slave_exception(
    node_slave: NodeSlave, pdu: bytes, code: int
) -> None:
    """Invalid requests get the Modbus exception response.

    :param node_slave: node slave fixture
    :param pdu: request PDU
    :param code: expected exception code
    """
    assert _raw_pdu(UNIT, pdu) == bytes([pdu[0] | 0x80, code])


@pytest.mark.cmd_check("nxmbserver_main")
def test_modbus_rtu_slave_framing(node_slave: NodeSlave) -> None:
    """Bad CRC and other units get no reply; broadcasts apply silently.

    :param node_slave: node slave fixture
    """
    read = struct.pack(">BHH", 3, 30, 1)
    frame = _frame(UNIT, read)
    bad = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    assert _raw(bad, wait=0.5) == b"", "reply to a bad CRC"
    assert _raw(_frame(UNIT + 1, read), wait=0.5) == b"", "reply to unit 2"
    write = struct.pack(">BHH", 6, 30, 0x5A5A)
    assert _raw(_frame(0, write), wait=0.5) == b"", "reply to a broadcast"
    assert _raw_pdu(UNIT, read) == bytes([3, 2, 0x5A, 0x5A])


@pytest.mark.cmd_check("nxmbserver_main")
@pytest.mark.xfail(strict=True, reason=RESTART_BUG)
def test_modbus_rtu_slave_restart(node_slave: NodeSlave) -> None:
    """``nxmbserver`` started a second time serves requests.

    :param node_slave: node slave fixture
    """
    node_slave.stop()
    node_slave.start()
    try:
        time.sleep(0.5)
        read = struct.pack(">BHH", 4, 1, 1)
        assert _raw_pdu(UNIT, read) == bytes([4, 2, 0, 10])
    finally:
        _core().sendCommand(f"kill -2 {node_slave.pid}", timeout=10)
        node_slave.pid = 0
