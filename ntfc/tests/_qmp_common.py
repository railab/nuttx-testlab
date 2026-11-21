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
"""QMP client for the QEMU products.

The product's QEMU arguments carry ``-qmp unix:<path>,server=on,wait=off``;
the client speaks the QEMU Machine Protocol on that socket.
"""

import json
import re
import socket
from typing import Any, Dict

import pytest

TIMEOUT = 10


class Qmp:
    """Minimal QMP client on the product's ``-qmp unix:`` socket."""

    def __init__(self) -> None:
        """Connect and leave capabilities negotiation mode."""
        args = str(pytest.product.core(0).conf.exec_args)
        found = re.search(r"-qmp\s+unix:([^,\s]+)", args)
        assert found, f"no -qmp unix socket in {args!r}"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(TIMEOUT)
        self.sock.connect(found.group(1))
        self.rfile = self.sock.makefile("r")
        json.loads(self.rfile.readline())  # greeting
        self.execute("qmp_capabilities")

    def execute(self, cmd: str, **arguments: Any) -> Dict[str, Any]:
        """Run one QMP command and return its reply.

        :param cmd: command name
        :param arguments: command arguments
        :return: the ``return`` object of the reply
        """
        msg: Dict[str, Any] = {"execute": cmd}
        if arguments:
            msg["arguments"] = arguments
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = json.loads(self.rfile.readline())
            if "event" in reply:
                continue
            assert "error" not in reply, reply
            return reply["return"]

    def close(self) -> None:
        """Close the QMP connection."""
        self.rfile.close()
        self.sock.close()
