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


"""Oneshot timer devices (/dev/oneshotN, /dev/lponeshotN)."""

import pytest
from _hw_rig import Entry, entries, entry_id, run_entry

pytestmark = pytest.mark.cmd_check("hwtl_main")


@pytest.mark.dep_config("CONFIG_ONESHOT")
@pytest.mark.parametrize("entry", entries("oneshot"), ids=entry_id)
def test_hw_oneshot_delay(entry: Entry) -> None:
    """The entry's delay fires, early by at most 1 tick, late by 2 ticks.

    :param entry: board entry
    """
    run_entry("oneshot", entry)
