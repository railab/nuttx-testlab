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

"""Shared helpers for the CANopen tests (Lely examples vs python-canopen).

The nodes run the nuttx-apps Lely CANopen examples: ``coslave``
(examples/lely_slave, node-ID 2) and ``comaster`` (examples/lely_master,
node-ID 1), each with a fixed object dictionary built into the example.
The host joins the same vcan ``can0`` through python-canopen and records
every frame it sees.
"""

import re
import struct
import threading
import time
from typing import Any, Callable, List, NamedTuple, Optional

import can
import canopen
import pytest

CAN_IFNAME = "can0"

SLAVE_ID = 2
MASTER_ID = 1

NMT_START = 0x01
NMT_STOP = 0x02
NMT_PREOP = 0x80
NMT_RESET_NODE = 0x81
NMT_RESET_COMM = 0x82

STATE_BOOTUP = 0x00
STATE_STOPPED = 0x04
STATE_OPERATIONAL = 0x05
STATE_PREOP = 0x7F

COB_NMT = 0x000
COB_SYNC = 0x080
COB_TIME = 0x100
COB_TPDO1 = 0x180
COB_HB = 0x700

# Lely example object dictionary values (examples/lely_*/sdev_*.c)

HB_PERIOD_S = 0.050
SYNC_PERIOD_S = 0.100


class Frame(NamedTuple):
    """One CAN frame seen by the host."""

    t: float
    can_id: int
    data: bytes
    is_fd: bool


class FrameLog(can.Listener):
    """Thread-safe record of every frame the host receives on the bus."""

    def __init__(self) -> None:
        """Create an empty log."""
        super().__init__()
        self._lock = threading.Lock()
        self._frames: List[Frame] = []

    def on_message_received(self, msg: can.Message) -> None:
        """Record one received frame.

        :param msg: received frame
        """
        frame = Frame(
            time.monotonic(),
            msg.arbitration_id,
            bytes(msg.data),
            bool(msg.is_fd),
        )
        with self._lock:
            self._frames.append(frame)

    def mark(self) -> int:
        """Return a position to read later frames from.

        :return: number of frames recorded so far
        """
        with self._lock:
            return len(self._frames)

    def since(self, mark: int, can_id: Optional[int] = None) -> List[Frame]:
        """Return the frames recorded after a mark.

        :param mark: position returned by ``mark()``
        :param can_id: only frames with this CAN ID, if given
        :return: frames in reception order
        """
        with self._lock:
            frames = self._frames[mark:]
        if can_id is None:
            return frames
        return [f for f in frames if f.can_id == can_id]

    def wait_for(
        self,
        mark: int,
        can_id: int,
        match: Optional[Callable[[Frame], bool]] = None,
        timeout: float = 5.0,
    ) -> Optional[Frame]:
        """Wait for a frame with a CAN ID after a mark.

        :param mark: position returned by ``mark()``
        :param can_id: CAN ID to wait for
        :param match: extra condition on the frame, if given
        :param timeout: how long to wait in seconds
        :return: first matching frame, or None on timeout
        """
        deadline = time.monotonic() + timeout
        while True:
            for frame in self.since(mark, can_id):
                if match is None or match(frame):
                    return frame
            if time.monotonic() > deadline:
                return None
            time.sleep(0.01)


class HostBus:
    """python-canopen network on ``can0`` plus the host frame log."""

    def __init__(self) -> None:
        """Connect to the bus.

        The bus accepts CAN FD frames so that a node sending one by
        mistake shows up in the log instead of being dropped by the host
        socket.
        """
        self.log = FrameLog()
        self.net = canopen.Network()
        self.net.listeners.append(self.log)
        self.net.connect(interface="socketcan", channel=CAN_IFNAME, fd=True)

    def close(self) -> None:
        """Disconnect from the bus."""
        self.net.disconnect()

    def send(self, can_id: int, data: bytes) -> None:
        """Send one classic CAN frame.

        :param can_id: CAN ID
        :param data: payload
        """
        self.net.send_message(can_id, data)

    def nmt(self, cs: int, node_id: int) -> None:
        """Send an NMT command.

        :param cs: NMT command specifier
        :param node_id: addressed node-ID, 0 for all nodes
        """
        self.send(COB_NMT, bytes([cs, node_id]))

    def sync(self, count: int, period: float = SYNC_PERIOD_S) -> None:
        """Send SYNC messages.

        :param count: number of SYNC messages
        :param period: time between them in seconds
        """
        for _ in range(count):
            self.send(COB_SYNC, b"")
            time.sleep(period)

    def states(self, mark: int, node_id: int) -> List[int]:
        """Return the NMT states a node reported after a mark.

        :param mark: position returned by ``FrameLog.mark()``
        :param node_id: node-ID
        :return: heartbeat (or boot-up) state bytes in order
        """
        return [
            f.data[0] for f in self.log.since(mark, COB_HB + node_id) if f.data
        ]

    def last_state(self, node_id: int, settle: float = 0.3) -> Optional[int]:
        """Return a node's NMT state after a short settle time.

        :param node_id: node-ID
        :param settle: time to collect heartbeats in seconds
        :return: state of the last heartbeat, or None if none was seen
        """
        mark = self.log.mark()
        time.sleep(settle)
        states = self.states(mark, node_id)
        return states[-1] if states else None

    def silent(self, node_id: int, quiet: float = 0.5) -> bool:
        """Wait until a node stops sending heartbeats.

        :param node_id: node-ID
        :param quiet: required time without heartbeats in seconds
        :return: True if the node went silent within 3 s
        """
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            mark = self.log.mark()
            time.sleep(quiet)
            if not self.log.since(mark, COB_HB + node_id):
                return True
        return False


def periods(frames: List[Frame]) -> List[float]:
    """Return the time between consecutive frames.

    :param frames: frames in reception order
    :return: gaps in seconds
    """
    return [b.t - a.t for a, b in zip(frames, frames[1:])]


def core(node: int) -> Any:
    """Return the NTFC core handler of a node.

    :param node: product index
    :return: ``ProductCore`` for the product's core 0
    """
    return pytest.products[node].core(0)


def start_app(node: int, app: str, pattern: str, timeout: int = 15) -> str:
    """Start a Lely example in the background on a node.

    The caller checks the output for ``pattern``, after it has arranged
    to stop the example (``stop_app()``) even if the startup failed.

    :param node: product index
    :param app: ``coslave`` or ``comaster``
    :param pattern: regex that ends the startup output
    :param timeout: timeout in seconds
    :return: console output up to ``pattern`` (or the timeout), with NSH
     prompts removed: on SMP targets NSH can print its prompt into the
     middle of the example's first line
    """
    ret = core(node).sendCommandReadUntilPattern(
        f"{app} &", pattern=pattern, timeout=timeout
    )
    return re.sub(r"nsh> (?:\x1b\[K)?", "", str(ret.output))


def app_pid(output: str, app: str) -> Optional[int]:
    """Return the PID NSH printed for a background command.

    :param output: console output of ``<app> &``
    :param app: application name
    :return: PID, or None if not found
    """
    found = re.search(rf"{app} \[(\d+):", output)
    return int(found.group(1)) if found else None


def task_alive(node: int, pid: int) -> bool:
    """Check whether a task still exists on a node.

    :param node: product index
    :param pid: task PID
    :return: True if ``/proc/<pid>`` exists
    """
    ret = core(node).sendCommandReadUntilPattern(
        f"ls /proc/{pid}", pattern=r"status|failed", timeout=10
    )
    return "status" in ret.output


def stop_app(
    bus: HostBus, node: int, node_id: int, pid: Optional[int]
) -> None:
    """Stop a Lely example with an NMT reset node command.

    The examples exit on NMT reset node. If the task is still there,
    kill it so that later tests start clean, then fail.

    :param bus: host bus
    :param node: product index
    :param node_id: CANopen node-ID of the example
    :param pid: PID of the example task
    """
    # The character driver backend of the examples loses frames that
    # arrive together, so do not send right behind the test's last frame

    time.sleep(0.1)
    bus.nmt(NMT_RESET_NODE, node_id)
    silent = bus.silent(node_id)
    assert pid is not None, "no PID for the example task"
    alive = task_alive(node, pid)
    if alive:
        core(node).sendCommand(f"kill -9 {pid}", timeout=5)

    # Drop the output left by the example

    core(node).sendCommand("echo stopped", "stopped", timeout=10)
    assert silent and not alive, (
        f"task {pid} on node {node} did not exit on NMT reset node "
        f"(heartbeat silent: {silent})"
    )


def time_of_day(days: int, ms: int) -> bytes:
    """Pack a CANopen TIME_OF_DAY value.

    :param days: days since 1984-01-01
    :param ms: milliseconds after midnight
    :return: 6-byte TIME payload
    """
    return struct.pack("<IH", ms & 0x0FFFFFFF, days)
