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

"""Shared helpers for the multi-node CANopen network tests.

Every NuttX node runs ``conode`` (apps/conode, Lely CANopen) on vcan
``can0``: node 0 is the master (node-ID 1), nodes 1-3 are slaves
(node-IDs 2-4).  The host joins as node-ID 5 (``HostNode``) and records
every frame through ``HostBus``.

``conode`` object dictionary used by the tests (sub-index = node-ID):

* ``0x2000`` value the master wrote over SDO when it booted the node:
  ``boots << 16 | master << 8 | node``
* ``0x2001`` free for SDO tests
* ``0x2100`` TPDO1 counter, +1 per SYNC in OPERATIONAL
* ``0x2201``/``0x2202``/``0x2203`` PDOs received from a node, counter
  values skipped, counter values not increasing
* ``0x2204`` last NMT state seen of a node, ``0x2205``/``0x2206``
  heartbeat timeouts occurred/resolved, ``0x2207`` boot-ups seen
* ``0x2210`` write non-zero to clear the PDO statistics, ``0x2211``
  frames the CAN socket did not accept
"""

import re
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import canopen
from _canopen_common import (
    COB_HB,
    COB_SYNC,
    COB_TPDO1,
    NMT_PREOP,
    NMT_RESET_COMM,
    NMT_RESET_NODE,
    NMT_START,
    NMT_STOP,
    STATE_BOOTUP,
    STATE_OPERATIONAL,
    STATE_PREOP,
    STATE_STOPPED,
    HostBus,
    core,
)
from canopen.objectdictionary import UNSIGNED16, UNSIGNED32, ODVariable

MASTER_ID = 1
SLAVE_IDS = (2, 3, 4)
HOST_ID = 5
NUTTX_IDS = (MASTER_ID,) + SLAVE_IDS
ALL_IDS = NUTTX_IDS + (HOST_ID,)

# Product index of each NuttX node-ID

PRODUCT = {MASTER_ID: 0, 2: 1, 3: 2, 4: 3}

HB_MS = 100
HBC_MS = 500
SYNC_MS = 100

OBJ_BOOTVAL = 0x2000
OBJ_SCRATCH = 0x2001
OBJ_RXCNT = 0x2201
OBJ_RXLOST = 0x2202
OBJ_RXBAD = 0x2203
OBJ_NMTST = 0x2204
OBJ_HBTMO = 0x2205
OBJ_HBRES = 0x2206
OBJ_BOOTUPS = 0x2207
OBJ_RXRESET = 0x2210
OBJ_TXERR = 0x2211

STATE_TO_CS = {
    STATE_OPERATIONAL: NMT_START,
    STATE_STOPPED: NMT_STOP,
    STATE_PREOP: NMT_PREOP,
}


def peers(node_id: int) -> Tuple[int, ...]:
    """Return the node-IDs whose TPDO1 a node receives.

    :param node_id: node-ID
    :return: every other node of the network
    """
    return tuple(i for i in ALL_IDS if i != node_id)


def bootval(boots: int, node_id: int) -> int:
    """Return the ``0x2000`` value the master writes when booting a node.

    :param boots: number of boot-ups of the node the master has seen
    :param node_id: node-ID
    :return: expected object value
    """
    return (boots << 16) | (MASTER_ID << 8) | node_id


def wait_until(cond: Callable[[], bool], timeout: float) -> bool:
    """Poll a condition.

    :param cond: condition to check
    :param timeout: how long to wait in seconds
    :return: True once the condition holds, False on timeout
    """
    deadline = time.monotonic() + timeout
    while True:
        if cond():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)


class HostNode:
    """python-canopen node on the host acting as an NMT slave.

    python-canopen's ``LocalNode`` serves SDO; NMT, heartbeat and the
    SYNC-driven TPDO1 counter are implemented here: a reset sends a
    boot-up and enters PRE-OPERATIONAL, and every SYNC in OPERATIONAL
    sends the counter on ``0x180 + node-ID``, then increments it.
    """

    def __init__(self, bus: HostBus, node_id: int = HOST_ID) -> None:
        """Create the node; it stays silent until ``boot()``.

        :param bus: host bus
        :param node_id: node-ID
        """
        od = canopen.ObjectDictionary()
        for index, dtype, value in (
            (0x1000, UNSIGNED32, 0),
            (0x1017, UNSIGNED16, HB_MS),
            (OBJ_BOOTVAL, UNSIGNED32, 0),
            (OBJ_SCRATCH, UNSIGNED32, 0),
        ):
            var = ODVariable(f"0x{index:04x}", index, 0)
            var.data_type = dtype
            var.access_type = "rw"
            var.default = value
            od.add_object(var)

        self.bus = bus
        self.node_id = node_id
        self.state = STATE_BOOTUP
        self.counter = 0
        self.local = canopen.LocalNode(node_id, od)
        bus.net.add_node(self.local)
        self._hb: Optional[Any] = None
        self._lock = threading.Lock()
        bus.net.subscribe(0, self._on_nmt)
        bus.net.subscribe(COB_SYNC, self._on_sync)

    def close(self) -> None:
        """Stop the heartbeat and leave the network."""
        if self._hb is not None:
            self._hb.stop()
        self.bus.net.unsubscribe(0, self._on_nmt)
        self.bus.net.unsubscribe(COB_SYNC, self._on_sync)
        del self.bus.net[self.node_id]

    def boot(self) -> None:
        """Send the boot-up message and enter PRE-OPERATIONAL."""
        with self._lock:
            self.bus.send(COB_HB + self.node_id, bytes([STATE_BOOTUP]))
            self._set_state(STATE_PREOP)

    def bootval(self) -> int:
        """Return the value of ``0x2000`` (written by the master).

        :return: object value
        """
        return int(self.local.sdo[OBJ_BOOTVAL].raw)

    def _set_state(self, state: int) -> None:
        """Change the NMT state and the heartbeat payload.

        :param state: new NMT state
        """
        self.state = state
        if self._hb is None:
            self._hb = self.bus.net.send_periodic(
                COB_HB + self.node_id, [state], HB_MS / 1000
            )
        else:
            self._hb.update([state])

    def _on_nmt(self, can_id: int, data: bytearray, ts: float) -> None:
        """Handle an NMT command received from the bus.

        :param can_id: CAN ID (``0x000``)
        :param data: command specifier and node-ID
        :param ts: reception time
        """
        self.command(data)

    def command(self, data: bytes) -> None:
        """Apply an NMT command.

        The host does not receive its own frames, so commands the tests
        send are applied here too.

        :param data: command specifier and node-ID
        """
        if len(data) < 2 or data[1] not in (0, self.node_id):
            return
        with self._lock:
            if data[0] in (NMT_RESET_NODE, NMT_RESET_COMM):
                if data[0] == NMT_RESET_NODE:
                    self.counter = 0
                self.bus.send(COB_HB + self.node_id, bytes([STATE_BOOTUP]))
                self._set_state(STATE_PREOP)
                return
            for state, cs in STATE_TO_CS.items():
                if data[0] == cs:
                    self._set_state(state)

    def _on_sync(self, can_id: int, data: bytearray, ts: float) -> None:
        """Send TPDO1 on SYNC in OPERATIONAL.

        :param can_id: CAN ID (``0x080``)
        :param data: SYNC payload
        :param ts: reception time
        """
        with self._lock:
            if self.state != STATE_OPERATIONAL:
                return
            self.bus.send(
                COB_TPDO1 + self.node_id, struct.pack("<I", self.counter)
            )
            self.counter = (self.counter + 1) & 0xFFFFFFFF


class Network:
    """The CANopen network: ``conode`` on every NuttX node plus the host."""

    def __init__(self, bus: HostBus) -> None:
        """Attach SDO clients for the NuttX nodes.

        :param bus: host bus
        """
        self.bus = bus
        self.host = HostNode(bus)
        self.pids: Dict[int, int] = {}
        self.remote: Dict[int, canopen.RemoteNode] = {}
        for node_id in NUTTX_IDS:
            node = canopen.RemoteNode(node_id, canopen.ObjectDictionary())
            node.sdo.RESPONSE_TIMEOUT = 1.0
            bus.net.add_node(node)
            self.remote[node_id] = node

    @staticmethod
    def args(node_id: int) -> str:
        """Return the ``conode`` arguments of a node.

        :param node_id: node-ID
        :return: command-line arguments
        """
        if node_id == MASTER_ID:
            slaves = " ".join(str(i) for i in ALL_IDS if i != MASTER_ID)
            return f"-i {node_id} -m -b {HB_MS} -c {HBC_MS} {slaves}"
        rx = ",".join(str(i) for i in peers(node_id))
        return f"-i {node_id} -r {rx} -b {HB_MS}"

    def start(self, node_id: int) -> None:
        """Start ``conode`` on a NuttX node in the background.

        :param node_id: node-ID
        """
        ret = core(PRODUCT[node_id]).sendCommandReadUntilPattern(
            f"conode {self.args(node_id)} &",
            pattern=rf"conode: node {node_id} \w+ running",
            timeout=15,
        )
        out = str(ret.output)
        found = re.search(r"conode \[(\d+):", out)
        assert found and "running" in out, out
        self.pids[node_id] = int(found.group(1))

    def stop(self, node_id: int) -> str:
        """Stop ``conode`` with SIGTERM; it prints its PDO verdict.

        :param node_id: node-ID
        :return: console output up to ``conode: exit``
        """
        pid = self.pids.pop(node_id)
        ret = core(PRODUCT[node_id]).sendCommandReadUntilPattern(
            f"kill {pid}", pattern=r"conode: exit", timeout=10
        )
        return str(ret.output)

    def kill(self, node_id: int) -> None:
        """Kill ``conode`` with SIGKILL.

        :param node_id: node-ID
        """
        pid = self.pids.pop(node_id)
        core(PRODUCT[node_id]).sendCommand(f"kill -9 {pid}", timeout=5)

    def boot(self) -> None:
        """Start the slaves, the host node and then the master.

        The master resets all nodes and boots each one that sends a
        boot-up message.
        """
        for node_id in SLAVE_IDS:
            self.start(node_id)
        self.host.boot()
        self.start(MASTER_ID)
        assert self.wait_view(
            OBJ_NMTST, ALL_IDS[1:], STATE_OPERATIONAL, 10
        ), self.master_view(OBJ_NMTST)

    def shutdown(self) -> None:
        """Stop every ``conode`` and the host node."""
        for node_id in list(self.pids):
            self.stop(node_id)
        self.host.close()

    def upload(self, node_id: int, index: int, sub: int = 0) -> int:
        """Read an integer object of a NuttX node over SDO.

        :param node_id: node-ID
        :param index: object index
        :param sub: sub-index
        :return: object value
        """
        data = self.remote[node_id].sdo.upload(index, sub)
        return int.from_bytes(data, "little")

    def download(
        self, node_id: int, index: int, sub: int, fmt: str, value: int
    ) -> None:
        """Write an integer object of a NuttX node over SDO.

        :param node_id: node-ID
        :param index: object index
        :param sub: sub-index
        :param fmt: ``struct`` format of the value
        :param value: value
        """
        self.remote[node_id].sdo.download(index, sub, struct.pack(fmt, value))

    def nmt(self, cs: int, node_id: int) -> None:
        """Send an NMT command from the host, also to the host node.

        :param cs: NMT command specifier
        :param node_id: addressed node-ID, 0 for all nodes
        """
        self.bus.nmt(cs, node_id)
        self.host.command(bytes([cs, node_id]))

    def master_view(self, index: int) -> Dict[int, int]:
        """Return the master's per-node object for all other nodes.

        :param index: ``OBJ_NMTST``, ``OBJ_HBTMO``, ``OBJ_HBRES`` or
         ``OBJ_BOOTUPS``
        :return: value by node-ID
        """
        return {i: self.upload(MASTER_ID, index, i) for i in ALL_IDS[1:]}

    def wait_view(
        self, index: int, ids: Tuple[int, ...], value: int, timeout: float
    ) -> bool:
        """Wait until the master's per-node object has a value.

        :param index: per-node object index
        :param ids: node-IDs to check
        :param value: expected value
        :param timeout: how long to wait in seconds
        :return: True if every node reached the value in time
        """

        def check() -> bool:
            try:
                return all(
                    self.upload(MASTER_ID, index, i) == value for i in ids
                )
            except canopen.SdoError:
                return False

        return wait_until(check, timeout)

    def reset_rx(self) -> None:
        """Clear the PDO statistics of every NuttX node."""
        for node_id in NUTTX_IDS:
            self.download(node_id, OBJ_RXRESET, 0, "<B", 1)

    def rx(self, node_id: int, peer: int) -> Tuple[int, int, int]:
        """Return the PDO statistics of a receiver for one producer.

        :param node_id: receiving node-ID
        :param peer: producing node-ID
        :return: (received, lost, not increasing)
        """
        return (
            self.upload(node_id, OBJ_RXCNT, peer),
            self.upload(node_id, OBJ_RXLOST, peer),
            self.upload(node_id, OBJ_RXBAD, peer),
        )

    def heartbeats(self, mark: int, node_id: int) -> List[int]:
        """Return the heartbeat states a node sent after a mark.

        The host node's own frames are not looped back, so its state is
        returned instead.

        :param mark: position returned by ``FrameLog.mark()``
        :param node_id: node-ID
        :return: states in order
        """
        if node_id == HOST_ID:
            return [self.host.state]
        return self.bus.states(mark, node_id)

    def wait_states(self, ids: Tuple[int, ...], state: int) -> bool:
        """Wait until the heartbeats of nodes report a state.

        :param ids: node-IDs
        :param state: NMT state
        :return: True if every node reported it within 3 s
        """

        def check() -> bool:
            mark = self.bus.log.mark()
            time.sleep(2.5 * HB_MS / 1000)
            return all(self.heartbeats(mark, i)[-1:] == [state] for i in ids)

        return wait_until(check, 3.0)


def counters(frames: List[Any]) -> List[int]:
    """Return the TPDO1 counter values of frames.

    :param frames: TPDO1 frames
    :return: counter values in order
    """
    return [struct.unpack("<I", f.data)[0] for f in frames]
