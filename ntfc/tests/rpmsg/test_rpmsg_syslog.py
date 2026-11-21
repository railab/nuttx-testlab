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

"""SYSLOG over RPMSG: node 1's log shows up on node 0's console.

Node 1 has ``SYSLOG_RPMSG`` (Kconfig: "Use the RPMSG as a SYSLOG output
device, send message to remote proc.") and ``SYSLOG_CHARDEV`` ("a
simple character driver at /dev/log whose write() method will transfer
data to the SYSLOG device"); node 0 has ``SYSLOG_RPMSG_SERVER`` ("Use
RPMSG to receive message from remote proc.") and logs to its console.
NuttX Documentation/os/drivers/special/syslog.rst: "``CONFIG_SYSLOG_RPMSG``:
Enable the rpmsg channel (routes syslog messages over IPC via rpmsg)."
"""

import itertools
import re

from _rpmsg_common import core, run

TIMEOUT = 30

_RUN = itertools.count()


def _received(tag: str, last: str) -> str:
    """Read node 0's console until a line of node 1's log arrived.

    :param tag: unique tag of the test's lines
    :param last: suffix of the last line expected
    :return: console output read
    """
    ret = core(0).readUntilPattern(f"{tag}-{last}", timeout=TIMEOUT)
    return str(ret.output)


def test_syslog_rpmsg_lines() -> None:
    """Five lines logged on node 1 reach node 0 once each, in order."""
    tag = f"tlsl{next(_RUN)}"
    for i in range(5):
        run(1, f"echo {tag}-{i} > /dev/log")
    out = _received(tag, "4")
    got = re.findall(rf"{tag}-(\d)", out)
    assert got == [str(i) for i in range(5)], out


def test_syslog_rpmsg_burst() -> None:
    """40 lines (2000 bytes) written at once all reach node 0 intact.

    The burst is larger than one RPMSG buffer (1536 bytes on this
    transport) and smaller than node 1's ``SYSLOG_RPMSG_BUFSIZE``.
    """
    tag = f"tlsb{next(_RUN)}"
    path = "/tmp/burst.txt"
    run(1, f"rm {path}")
    for i in range(40):
        line = f"{tag}-{i:02d}-" + "x" * 40
        run(1, f"echo {line} >> {path}")
    run(1, f"cat {path} > /dev/log")
    out = _received(tag, "39-" + "x" * 40)
    got = re.findall(rf"{tag}-(\d\d)-(x*)", out)
    assert [int(i) for i, _ in got] == list(range(40)), out
    assert all(len(pad) == 40 for _, pad in got), out
