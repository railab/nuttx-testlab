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

"""Shared Modbus helpers: pymodbus host slave and nxmbclient parsing.

``nxmbserver`` (examples/nxmbserver) has a fixed register map: coils and
discrete inputs 0, input register ``i`` holds ``i * 10``, holding
register ``i`` starts at ``i * 100``; 100 of each.

``nxmbclient`` (system/nxmbclient) prints one ``<addr> <value>`` line
(tab separated) per read item and ``OK`` after a write.
"""

import asyncio
import re
import threading
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from pymodbus.simulator import DataType, SimData, SimDevice

REGS = 100

ILLEGAL_DATA_ADDRESS = 2
ILLEGAL_DATA_VALUE = 3

WRITE_COILS_BUG = (
    "system/nxmbclient: write-coils passes one byte per coil to "
    "nxmb_write_coils(), which expects packed bits, so only the first "
    "(count + 7) / 8 values are sent, as bits"
)


class HostSlave:
    """pymodbus slave on the host with a known register map.

    Coil ``i`` is ``i % 3 == 0``, discrete input ``i`` is ``i % 2 == 0``,
    holding register ``i`` is ``1000 + i``, input register ``i`` is
    ``2000 + i`` (``i`` < 100). Every write request is recorded as
    ``(function code, address, values)``.
    """

    def __init__(self, make_server: Callable[[SimDevice], Any]) -> None:
        """Prepare the slave.

        :param make_server: returns a pymodbus server (e.g.
         ``ModbusTcpServer``) for the simulated device; called in the
         slave's event loop
        """
        self.writes: List[Tuple[int, int, List[Any]]] = []
        self._make_server = make_server
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._server: Any = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def _action(
        self,
        func: int,
        start: int,
        addr: int,
        count: int,
        regs: List[int],
        values: Optional[List[Any]],
    ) -> None:
        """Record write requests.

        :param func: request function code
        :param start: address of ``regs[0]``
        :param addr: request address
        :param count: request count
        :param regs: current registers
        :param values: values to write, None for reads
        """
        if values is not None:
            self.writes.append((func, addr, list(values)))

    def _device(self) -> SimDevice:
        """Build the simulated device.

        :return: device with the register map in the class docstring
        """
        coils = [i % 3 == 0 for i in range(REGS)]
        discrete = [i % 2 == 0 for i in range(REGS)]
        return SimDevice(
            id=1,
            simdata=(
                [SimData(0, values=coils, datatype=DataType.BITS)],
                [SimData(0, values=discrete, datatype=DataType.BITS)],
                [
                    SimData(
                        0,
                        values=[1000 + i for i in range(REGS)],
                        datatype=DataType.REGISTERS,
                    )
                ],
                [
                    SimData(
                        0,
                        values=[2000 + i for i in range(REGS)],
                        datatype=DataType.REGISTERS,
                    )
                ],
            ),
            action=self._action,
        )

    def _run(self) -> None:
        """Serve in this thread's event loop."""
        asyncio.set_event_loop(self._loop)

        async def serve() -> None:
            self._server = self._make_server(self._device())
            self._ready.set()
            await self._server.serve_forever()

        self._loop.run_until_complete(serve())

    def __enter__(self) -> "HostSlave":
        """Start serving.

        :return: this slave
        """
        self._thread.start()
        assert self._ready.wait(10), "pymodbus slave did not start"
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Stop serving.

        :param exc_type: exception type, if any
        :param exc: exception, if any
        :param tb: traceback, if any
        """
        assert self._server is not None
        asyncio.run_coroutine_threadsafe(
            self._server.shutdown(), self._loop
        ).result(10)
        self._thread.join(10)


def client_lines(out: str) -> List[str]:
    """Split the output of an ``nxmbclient`` command into lines.

    :param out: console output, starting with the echoed command line
    :return: non-empty output lines after the command line, without the
     prompt
    """
    lines = [line.strip() for line in out.replace("\r", "").split("\n")]
    lines = [line for line in lines[1:] if line and line != "nsh>"]
    assert not any(line.startswith("nsh:") for line in lines), lines
    return lines


def items(lines: List[str]) -> Dict[int, int]:
    """Parse the ``<addr> <value>`` lines of an ``nxmbclient`` read.

    :param lines: nxmbclient output lines
    :return: value by address
    """
    found = {}
    for line in lines:
        match = re.fullmatch(r"(\d+)\s+(\d+)", line)
        if match:
            found[int(match.group(1))] = int(match.group(2))
    return found
