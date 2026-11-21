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


"""SCHED_FIFO thread list (POSIX XSH 2.8.4).

Rules quoted from https://pubs.opengroup.org/onlinepubs/9699919799/
functions/V2_chap02.html#tag_15_08_04. ``hwtl sched fifo`` creates two
busy threads A then B, one priority below hwtl, sleeps, then reads their
iteration counts before it stops them (one CPU).
"""

import pytest
from _hw_rig import hwtl

pytestmark = pytest.mark.cmd_check("hwtl_main")


def test_hw_sched_fifo() -> None:
    """SCHED_FIFO: B does not run while A runs (500 ms).

    "When a running thread becomes a preempted thread, it becomes the
    head of the thread list for its priority." "At no other time is the
    position of a thread with this scheduling policy within the thread
    lists affected."
    """
    conf = pytest.product.core(0).conf
    for opt in ("CONFIG_DISABLE_PTHREAD", "CONFIG_SMP"):
        if conf.kv_check(opt):
            pytest.skip(f"{opt}: needs pthreads and one CPU")

    verdict = hwtl("sched fifo")
    assert verdict.startswith("hwtl: PASS"), verdict
