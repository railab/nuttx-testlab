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


"""Board clock against the host."""

import re
import time

import pytest

pytestmark = pytest.mark.cmd_check("hwtl_main")

#: Host time between the two newlines of ``hwtl clock``, seconds.
CLOCK_WINDOW = 20

#: Largest relative error of the board clock (as test_hw_timer_period).
CLOCK_MAX_ERROR = 0.02


def test_hw_clock_accuracy() -> None:
    """Board CLOCK_MONOTONIC over 20 s of host time is within 2 %.

    ``hwtl clock`` stamps the arrival of two newlines that the host sends
    20 s apart (host ``time.monotonic()``).
    """
    core = pytest.product.core(0)
    core.sendCommandReadUntilPattern(
        "hwtl clock", pattern="hwtl: clock ready", timeout=10
    )

    start = time.monotonic()
    core.sendCtrlCmd("J")
    time.sleep(CLOCK_WINDOW)
    host = time.monotonic() - start
    core.sendCtrlCmd("J")

    out = core.readUntilPattern(
        pattern=r"hwtl: (PASS|FAIL) clock[^\r\n]*", timeout=10
    ).output
    found = re.search(r"hwtl: PASS clock us=(\d+)", out)
    assert found, out

    board = int(found.group(1)) / 1e6
    error = abs(board - host) / host
    assert error < CLOCK_MAX_ERROR, f"board {board:.4f} s host {host:.4f} s"
