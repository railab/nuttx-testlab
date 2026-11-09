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

"""RTC devices (/dev/rtcN): time, alarms and periodic wakeup."""

import pytest
from _hw_rig import Entry, entries, entry_id, run_entry

pytestmark = pytest.mark.cmd_check("hwtl_main")


@pytest.mark.dep_config("CONFIG_RTC_DRIVER")
@pytest.mark.parametrize("entry", entries("rtctime"), ids=entry_id)
def test_hw_rtc_time(entry: Entry) -> None:
    """The time set is read back and advances across a year rollover.

    :param entry: board entry
    """
    run_entry("rtctime", entry)


@pytest.mark.dep_config("CONFIG_RTC_ALARM")
@pytest.mark.parametrize("entry", entries("rtcalarm"), ids=entry_id)
def test_hw_rtc_alarm(entry: Entry) -> None:
    """The alarm is active and fires 0 to 1 s after its delay.

    :param entry: board entry
    """
    run_entry("rtcalarm", entry)


@pytest.mark.dep_config("CONFIG_RTC_ALARM")
@pytest.mark.parametrize("entry", entries("rtccancel"), ids=entry_id)
def test_hw_rtc_cancel(entry: Entry) -> None:
    """A cancelled alarm is inactive and does not fire.

    :param entry: board entry
    """
    run_entry("rtccancel", entry)


@pytest.mark.dep_config("CONFIG_RTC_PERIODIC")
@pytest.mark.parametrize("entry", entries("rtcperiodic"), ids=entry_id)
def test_hw_rtc_periodic(entry: Entry) -> None:
    """The wakeups counted in an entry's time are within one of the expected.

    :param entry: board entry
    """
    run_entry("rtcperiodic", entry)
