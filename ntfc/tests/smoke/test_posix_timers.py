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

"""POSIX timers and signal senders, checked by ``posixtl`` on the node.

Each test runs one ``posixtl <case>``; the case and the POSIX text it
checks are in ``apps/posixtl/posixtl_main.c``.  Sources:
https://pubs.opengroup.org/onlinepubs/9799919799/functions/timer_getoverrun.html,
.../timer_create.html, .../V2_chap02.html (2.4 Signal Concepts) and
https://pubs.opengroup.org/onlinepubs/9699919799/functions/getitimer.html.
"""

import re

import pytest

pytestmark = pytest.mark.cmd_check("posixtl_main")

OVERRUN_RESET_BUG = (
    "sched/timer/timer_settime.c:208: timer_signotify() sets pt_overrun "
    "to 0 when it queues the next signal and timer_getoverrun() "
    "(timer_getoverrun.c:90) returns that live counter, not the count of "
    "the last delivery or acceptance"
)
THREAD_TIMER_BUG = (
    "sched/timer/timer_create.c:224: timer_create() makes the calling "
    "thread the owner (pt_owner = tcb->pid) and nxsched_release_tcb() "
    "(sched/sched/sched_releasetcb.c:114) deletes the timers of each "
    "exiting thread (timer_deleteall()), so timers are not per-process"
)
SENDER_PID_BUG = (
    "sched/signal/sig_queue.c:78: nxsig_queue_send() and "
    "sched/signal/sig_kill.c:239: nxsig_kill() set si_pid to the thread "
    "ID of the sender (rtcb->pid), not its process ID"
)
RELOAD_DISARM_BUG = (
    "sched/timer/timer_settime.c:492: timer_settime() sets the reload "
    "value (pt_delay) only when it arms the timer, so disarming keeps the "
    "old it_interval"
)
SETITIMER_EINVAL_BUG = (
    "sched/timer/timer_setitimer.c:152: setitimer() does not check that "
    "tv_usec is in 0..999999 and timer_settime() (timer_settime.c:484) "
    "does not check tv_nsec"
)


def _posixtl(case: str, timeout: int = 30) -> None:
    """Run ``posixtl <case>`` and assert its PASS verdict.

    :param case: posixtl case name
    :param timeout: seconds to wait for the verdict line
    """
    pattern = rf"posixtl: (PASS|FAIL) {case}\b[^\r\n]*"
    out = (
        pytest.product.core(0)
        .sendCommandReadUntilPattern(
            f"posixtl {case}", pattern=pattern, timeout=timeout
        )
        .output
    )
    found = re.search(pattern, out)
    assert found, out
    assert found.group(1) == "PASS", found.group(0)


@pytest.mark.xfail(strict=True, reason=OVERRUN_RESET_BUG)
def test_timer_overrun_latched() -> None:
    """The overrun count is the one of the last acceptance.

    "The value returned by timer_getoverrun() shall apply to the most
    recent expiration signal delivery or acceptance for the timer."  The
    count must not change while the next signal is pending.
    """
    _posixtl("overrun_latched")


@pytest.mark.xfail(strict=True, reason=RELOAD_DISARM_BUG)
def test_timer_reload_on_disarm() -> None:
    """Disarming sets the reload value too.

    "The reload value of the timer shall be set to the value specified
    by the it_interval member of value."  timer_gettime(): it_interval
    "shall contain the reload value last set by timer_settime()".
    """
    _posixtl("reload")


@pytest.mark.xfail(strict=True, reason=THREAD_TIMER_BUG)
def test_timer_thread_exit() -> None:
    """A timer created by a thread that exits still expires.

    "The timer_create() function shall create a per-process timer"; its
    signal "shall be generated for the process".
    """
    _posixtl("thread_exit")


@pytest.mark.xfail(strict=True, reason=SENDER_PID_BUG)
def test_signal_sender_pid() -> None:
    """si_pid is the process ID of a sending thread.

    "If si_code is SI_USER or SI_QUEUE, ... si_pid and si_uid shall be
    set to the process ID and the real user ID of the sender".  The
    sender is a second thread; checked for sigqueue() and kill().
    """
    _posixtl("sender_pid")


@pytest.mark.xfail(strict=True, reason=SETITIMER_EINVAL_BUG)
def test_itimer_einval() -> None:
    """setitimer() rejects a value not in canonical form.

    "[EINVAL] The value argument is not in canonical form."
    """
    _posixtl("itimer_einval")
