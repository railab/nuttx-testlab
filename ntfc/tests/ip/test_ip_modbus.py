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

"""Modbus TCP: NuttX nxmodbus slave and master against pymodbus.

Register maps and ``nxmbclient`` output: see ``_modbus_common``.
"""

import re
import socket
import struct
from typing import Any, Iterator, List

import pytest
from _modbus_common import (
    ILLEGAL_DATA_ADDRESS,
    ILLEGAL_DATA_VALUE,
    REGS,
    WRITE_COILS_BUG,
    HostSlave,
    client_lines,
    items,
)
from _net_common import HOST_IP, NODE_IPS, wait_tcp_room
from pymodbus.client import ModbusTcpClient
from pymodbus.server import ModbusTcpServer

pytestmark = [
    pytest.mark.dep_config("CONFIG_NXMODBUS_TCP"),
    pytest.mark.cmd_check("nxmbserver_main"),
    pytest.mark.cmd_check("nxmbclient_main"),
]

NODE_PORT = 502
HOST_PORT = 1502
TCP_ROOM = 4


def _core(node: int) -> Any:
    """Return the NTFC core handler of a node.

    :param node: product index
    :return: ``ProductCore`` for the product's core 0
    """
    return pytest.products[node].core(0)


def _host_slave() -> HostSlave:
    """Return a pymodbus TCP slave on the host bridge, port 1502.

    :return: slave, not yet started
    """
    return HostSlave(
        lambda dev: ModbusTcpServer(dev, address=(HOST_IP, HOST_PORT))
    )


def _start_slave(node: int) -> int:
    """Start ``nxmbserver`` in the background on a node.

    :param node: product index
    :return: PID of the server task
    """
    out = (
        _core(node)
        .sendCommandReadUntilPattern(
            f"nxmbserver -t tcp -P {NODE_PORT} &",
            pattern=r"Server running",
            timeout=10,
        )
        .output
    )
    found = re.search(r"nxmbserver \[(\d+):", out)
    assert found and "Server running" in out, out
    return int(found.group(1))


def _stop_slave(node: int, pid: int) -> None:
    """Stop a node's ``nxmbserver`` with SIGINT.

    :param node: product index
    :param pid: PID of the server task
    """
    ret = _core(node).sendCommand(
        f"kill -2 {pid}", "Shutting down", timeout=10
    )
    assert ret == 0, "nxmbserver did not stop"


@pytest.fixture(scope="module")
def node_slave() -> Iterator[None]:
    """Run ``nxmbserver`` on node0 for the slave tests."""
    pid = _start_slave(0)
    yield
    _stop_slave(0, pid)


@pytest.fixture
def master(node_slave: None) -> Iterator[ModbusTcpClient]:
    """Connect a pymodbus master to the node0 slave.

    :param node_slave: node0 slave fixture
    :return: connected client
    """
    client = ModbusTcpClient(NODE_IPS[0], port=NODE_PORT, timeout=5)
    assert client.connect(), "cannot connect to nxmbserver"
    yield client
    client.close()


def _raw_request(pdu: bytes) -> bytes:
    """Send one raw Modbus TCP request to the node0 slave.

    :param pdu: function code and data
    :return: response PDU
    """
    mbap = struct.pack(">HHHB", 0x4242, 0, len(pdu) + 1, 1)
    with socket.create_connection((NODE_IPS[0], NODE_PORT), timeout=5) as s:
        s.sendall(mbap + pdu)
        head = s.recv(7)
        assert len(head) == 7, "short MBAP header"
        tid, _, length, _ = struct.unpack(">HHHB", head)
        assert tid == 0x4242, "transaction id not echoed"
        body = b""
        while len(body) < length - 1:
            data = s.recv(length - 1 - len(body))
            if not data:
                break
            body += data
    return body


def test_modbus_slave_read(master: ModbusTcpClient) -> None:
    """A host master reads all four tables of the NuttX slave."""
    rsp = master.read_holding_registers(0, count=10)
    assert not rsp.isError(), rsp
    assert rsp.registers == [i * 100 for i in range(10)]
    rsp = master.read_input_registers(90, count=10)
    assert not rsp.isError(), rsp
    assert rsp.registers == [i * 10 for i in range(90, 100)]
    rsp = master.read_coils(0, count=20)
    assert not rsp.isError(), rsp
    assert rsp.bits[:20] == [False] * 20
    rsp = master.read_discrete_inputs(80, count=20)
    assert not rsp.isError(), rsp
    assert rsp.bits[:20] == [False] * 20


def test_modbus_slave_write(master: ModbusTcpClient) -> None:
    """Single and multiple writes by a host master read back the same."""
    regs = [0x1234, 0xFFFF, 0, 0x8001]
    coils = [True, False, True, True, False, False, True, False, True]
    assert not master.write_register(7, 4242).isError()
    assert not master.write_registers(20, regs).isError()
    assert not master.write_coil(5, True).isError()
    assert not master.write_coils(40, coils).isError()
    assert master.read_holding_registers(7, count=1).registers == [4242]
    assert master.read_holding_registers(20, count=4).registers == regs
    assert master.read_coils(5, count=1).bits[0] is True
    assert master.read_coils(40, count=9).bits[:9] == coils


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
def test_modbus_slave_exception(
    node_slave: None, pdu: bytes, code: int
) -> None:
    """Invalid requests get the Modbus exception response.

    :param node_slave: node0 slave fixture
    :param pdu: request PDU
    :param code: expected exception code
    """
    assert _raw_request(pdu) == bytes([pdu[0] | 0x80, code])


@pytest.fixture(autouse=True)
def tcp_room() -> None:
    """Wait until both nodes have free TCP connections.

    Every ``nxmbclient`` call is a short TCP connection.
    """
    wait_tcp_room(TCP_ROOM)


def _client(node: int, server: str, port: int, cmd: str) -> List[str]:
    """Run ``nxmbclient`` on a node.

    :param node: product index
    :param server: slave address
    :param port: slave TCP port
    :param cmd: nxmbclient command and arguments
    :return: output lines after the command line
    """
    out = (
        _core(node)
        .sendCommandReadUntilPattern(
            f"nxmbclient -t tcp -h {server} -P {port} -T 2000 {cmd}",
            pattern=r"nsh> ",
            timeout=15,
        )
        .output
    )
    return client_lines(out)


def test_modbus_master_read() -> None:
    """The NuttX master reads all four tables of a host slave."""
    with _host_slave():
        out = _client(0, HOST_IP, HOST_PORT, "read-holding 10 5")
        assert items(out) == {i: 1000 + i for i in range(10, 15)}, out
        out = _client(0, HOST_IP, HOST_PORT, "read-input 95 5")
        assert items(out) == {i: 2000 + i for i in range(95, 100)}, out
        out = _client(0, HOST_IP, HOST_PORT, "read-coils 0 12")
        assert items(out) == {i: int(i % 3 == 0) for i in range(12)}, out
        out = _client(0, HOST_IP, HOST_PORT, "read-discrete 3 9")
        assert items(out) == {i: int(i % 2 == 0) for i in range(3, 12)}, out


def test_modbus_master_write() -> None:
    """Writes by the NuttX master arrive at a host slave as sent."""
    cmds = [
        ("write-holding 7 4242", (6, 7, [4242])),
        ("write-holdings 20 1 65535 0 32769", (16, 20, [1, 65535, 0, 32769])),
        ("write-coil 5 1", (5, 5, [True])),
        ("write-coil 6 0", (5, 6, [False])),
    ]
    with _host_slave() as slave:
        for cmd, _ in cmds:
            out = _client(0, HOST_IP, HOST_PORT, cmd)
            assert "OK" in out, f"{cmd}: {out}"
        out = _client(0, HOST_IP, HOST_PORT, "read-holding 20 4")
        assert items(out) == {20: 1, 21: 65535, 22: 0, 23: 32769}, out
    assert slave.writes == [expect for _, expect in cmds]


@pytest.mark.xfail(strict=True, reason=WRITE_COILS_BUG)
def test_modbus_master_write_coils() -> None:
    """``write-coils`` by the NuttX master sets each listed coil."""
    with _host_slave() as slave:
        out = _client(0, HOST_IP, HOST_PORT, "write-coils 40 1 0 1 1 0 1")
        assert "OK" in out, out
        out = _client(0, HOST_IP, HOST_PORT, "read-coils 40 6")
        assert items(out) == {40: 1, 41: 0, 42: 1, 43: 1, 44: 0, 45: 1}, out
    assert slave.writes == [(15, 40, [True, False, True, True, False, True])]


def test_modbus_master_exception() -> None:
    """An exception response and a missing slave fail the command.

    Without a slave the connect in ``nxmb_enable()`` already fails
    (``ECONNREFUSED``).
    """
    failed = "Error: read-holding failed:"
    with _host_slave():
        out = _client(0, HOST_IP, HOST_PORT, "read-holding 0 1")
        assert items(out) == {0: 1000}, out
        out = _client(0, HOST_IP, HOST_PORT, f"read-holding {REGS} 1")
        assert any(line.startswith(failed) for line in out), out
        assert not items(out), out
    out = _client(0, HOST_IP, HOST_PORT, "read-holding 0 1")
    assert "Error: failed to enable context: -111" in out, out


def test_modbus_master_to_node_slave() -> None:
    """The node0 master writes and reads back registers on a node1 slave."""
    pid = _start_slave(1)
    try:
        out = _client(0, NODE_IPS[1], NODE_PORT, "write-holdings 50 7 8 9")
        assert "OK" in out, out
        for coil in (60, 61, 63):
            out = _client(0, NODE_IPS[1], NODE_PORT, f"write-coil {coil} 1")
            assert "OK" in out, out
        out = _client(0, NODE_IPS[1], NODE_PORT, "read-holding 49 5")
        assert items(out) == {49: 4900, 50: 7, 51: 8, 52: 9, 53: 5300}, out
        out = _client(0, NODE_IPS[1], NODE_PORT, "read-coils 60 4")
        assert items(out) == {60: 1, 61: 1, 62: 0, 63: 1}, out
        out = _client(0, NODE_IPS[1], NODE_PORT, "read-input 0 3")
        assert items(out) == {0: 0, 1: 10, 2: 20}, out
    finally:
        _stop_slave(1, pid)
