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


"""Board tables and helpers for the ntfc/tests/hw modules.

``RIGS[board]`` maps a feature to its entries: (id, hwtl arguments).
Boards: Documentation/hardware.rst.
"""

import re
from typing import Any, Dict, List, Tuple

import pytest

#: One ``hwtl`` feature entry: test id, hwtl arguments.
Entry = Tuple[str, str]

#: Verdict line printed by every hwtl subcommand.
VERDICT = r"hwtl: (PASS|FAIL) [^\r\n]*"

RIGS: Dict[str, Dict[str, List[Any]]] = {
    "nucleo-g071rb": {
        "timer": [
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
        ],
        "oneshot": [
            ("lponeshot2-1ms", "/dev/lponeshot2 1000"),
            ("lponeshot2-20ms", "/dev/lponeshot2 20000"),
            ("lponeshot2-half", "/dev/lponeshot2 0"),
        ],
    },
    "nucleo-c092rc": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer3-1ms", "/dev/timer3 1000 2000"),
            ("timer3-20ms", "/dev/timer3 20000 2000"),
        ],
        "oneshot": [
            ("oneshot14-1ms", "/dev/oneshot14 1000"),
            ("oneshot14-20ms", "/dev/oneshot14 20000"),
            ("oneshot14-half", "/dev/oneshot14 0"),
            ("oneshot16-1ms", "/dev/oneshot16 1000"),
            ("oneshot16-20ms", "/dev/oneshot16 20000"),
            ("oneshot16-half", "/dev/oneshot16 0"),
        ],
    },
    "nucleo-c562re": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer7-1ms", "/dev/timer7 1000 2000"),
            ("timer7-20ms", "/dev/timer7 20000 2000"),
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
        ],
    },
    "nucleo-f091rc": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer7-1ms", "/dev/timer7 1000 2000"),
            ("timer7-20ms", "/dev/timer7 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
            ("oneshot14-1ms", "/dev/oneshot14 1000"),
            ("oneshot14-20ms", "/dev/oneshot14 20000"),
            ("oneshot14-half", "/dev/oneshot14 0"),
        ],
    },
    "nucleo-f446re": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer7-1ms", "/dev/timer7 1000 2000"),
            ("timer7-20ms", "/dev/timer7 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
            ("oneshot5-1ms", "/dev/oneshot5 1000"),
            ("oneshot5-20ms", "/dev/oneshot5 20000"),
        ],
    },
    "nucleo-f722ze": {
        "timer": [
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
        ],
    },
    "nucleo-f767zi": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer5-1ms", "/dev/timer5 1000 2000"),
            ("timer5-20ms", "/dev/timer5 20000 2000"),
            ("timer6-1ms", "/dev/timer6 1000 2000"),
            ("timer6-20ms", "/dev/timer6 20000 2000"),
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
        ],
        "oneshot": [
            ("oneshot7-1ms", "/dev/oneshot7 1000"),
            ("oneshot7-20ms", "/dev/oneshot7 20000"),
            ("oneshot7-half", "/dev/oneshot7 0"),
        ],
    },
    "nucleo-l552ze": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
            ("lptimer3-1ms", "/dev/lptimer3 1000 2000"),
            ("lptimer3-20ms", "/dev/lptimer3 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
            ("lponeshot2-1ms", "/dev/lponeshot2 1000"),
            ("lponeshot2-20ms", "/dev/lponeshot2 20000"),
            ("lponeshot2-half", "/dev/lponeshot2 0"),
        ],
    },
    "nucleo-u083rc": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("lptimer2-1ms", "/dev/lptimer2 1000 2000"),
            ("lptimer2-20ms", "/dev/lptimer2 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
            ("lponeshot3-1ms", "/dev/lponeshot3 1000"),
            ("lponeshot3-20ms", "/dev/lponeshot3 20000"),
            ("lponeshot3-half", "/dev/lponeshot3 0"),
        ],
    },
    "nucleo-u3c5zi-q": {
        "timer": [
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
            ("lptimer3-1ms", "/dev/lptimer3 1000 2000"),
            ("lptimer3-20ms", "/dev/lptimer3 20000 2000"),
        ],
        "oneshot": [
            ("lponeshot2-1ms", "/dev/lponeshot2 1000"),
            ("lponeshot2-20ms", "/dev/lponeshot2 20000"),
            ("lponeshot2-half", "/dev/lponeshot2 0"),
            ("lponeshot4-1ms", "/dev/lponeshot4 1000"),
            ("lponeshot4-20ms", "/dev/lponeshot4 20000"),
            ("lponeshot4-half", "/dev/lponeshot4 0"),
        ],
    },
    "nucleo-l073rz": {
        "timer": [
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
        ],
    },
    "nucleo-l476rg": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
            ("lponeshot2-1ms", "/dev/lponeshot2 1000"),
            ("lponeshot2-20ms", "/dev/lponeshot2 20000"),
            ("lponeshot2-half", "/dev/lponeshot2 0"),
        ],
    },
    "nucleo-f303ze": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer7-1ms", "/dev/timer7 1000 2000"),
            ("timer7-20ms", "/dev/timer7 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
        ],
    },
    "nucleo-c071rb": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer3-1ms", "/dev/timer3 1000 2000"),
            ("timer3-20ms", "/dev/timer3 20000 2000"),
        ],
        "oneshot": [
            ("oneshot14-1ms", "/dev/oneshot14 1000"),
            ("oneshot14-20ms", "/dev/oneshot14 20000"),
            ("oneshot14-half", "/dev/oneshot14 0"),
            ("oneshot16-1ms", "/dev/oneshot16 1000"),
            ("oneshot16-20ms", "/dev/oneshot16 20000"),
            ("oneshot16-half", "/dev/oneshot16 0"),
        ],
    },
    "nucleo-h533re": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer7-1ms", "/dev/timer7 1000 2000"),
            ("timer7-20ms", "/dev/timer7 20000 2000"),
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
            ("lponeshot2-1ms", "/dev/lponeshot2 1000"),
            ("lponeshot2-20ms", "/dev/lponeshot2 20000"),
            ("lponeshot2-half", "/dev/lponeshot2 0"),
        ],
        "rng": [
            ("random", "/dev/random 40"),
        ],
    },
    "stm32h573i-dk": {
        "rtctime": [
            ("rtc0-3s", "/dev/rtc0 3000"),
        ],
        "rtcalarm": [
            ("rtc0-a-1s", "/dev/rtc0 0 1"),
            ("rtc0-a-3s", "/dev/rtc0 0 3"),
            ("rtc0-b-1s", "/dev/rtc0 1 1"),
            ("rtc0-b-3s", "/dev/rtc0 1 3"),
        ],
        "rtccancel": [
            ("rtc0-a", "/dev/rtc0 0 2"),
            ("rtc0-b", "/dev/rtc0 1 2"),
        ],
        "rtcperiodic": [
            ("rtc0-1000ms", "/dev/rtc0 1000 5000"),
            ("rtc0-250ms", "/dev/rtc0 250 3000"),
            ("rtc0-100ms", "/dev/rtc0 100 2000"),
        ],
    },
    "nucleo-h743zi": {
        "timer": [
            ("timer2-1ms", "/dev/timer2 1000 2000"),
            ("timer2-20ms", "/dev/timer2 20000 2000"),
            ("timer7-1ms", "/dev/timer7 1000 2000"),
            ("timer7-20ms", "/dev/timer7 20000 2000"),
            ("lptimer1-1ms", "/dev/lptimer1 1000 2000"),
            ("lptimer1-20ms", "/dev/lptimer1 20000 2000"),
            ("lptimer3-1ms", "/dev/lptimer3 1000 2000"),
            ("lptimer3-20ms", "/dev/lptimer3 20000 2000"),
            ("lptimer5-1ms", "/dev/lptimer5 1000 2000"),
            ("lptimer5-20ms", "/dev/lptimer5 20000 2000"),
        ],
        "oneshot": [
            ("oneshot6-1ms", "/dev/oneshot6 1000"),
            ("oneshot6-20ms", "/dev/oneshot6 20000"),
            ("oneshot6-half", "/dev/oneshot6 0"),
            ("lponeshot2-1ms", "/dev/lponeshot2 1000"),
            ("lponeshot2-20ms", "/dev/lponeshot2 20000"),
            ("lponeshot2-half", "/dev/lponeshot2 0"),
            ("lponeshot4-1ms", "/dev/lponeshot4 1000"),
            ("lponeshot4-20ms", "/dev/lponeshot4 20000"),
            ("lponeshot4-half", "/dev/lponeshot4 0"),
        ],
        "rng": [
            ("random", "/dev/random 40"),
        ],
    },
}


def board() -> str:
    """Return the board under test (``CONFIG_ARCH_BOARD_CUSTOM_NAME``).

    :return: board name, empty if there is no product (e.g. at import
        outside NTFC)
    """
    try:
        conf = pytest.product.core(0).conf
    except AttributeError:
        return ""
    return str(conf.kv_check("CONFIG_ARCH_BOARD_CUSTOM_NAME") or "")


def entries(feature: str) -> List[Entry]:
    """Return the entries of a feature for the board under test.

    :param feature: feature table name, e.g. ``timer``
    :return: entries, empty if the board has none
    """
    return list(RIGS.get(board(), {}).get(feature, []))


def entry_id(entry: Entry) -> str:
    """Return the pytest id of an entry.

    :param entry: board entry
    :return: entry id
    """
    return entry[0]


def hwtl(args: str, timeout: int = 60) -> str:
    """Run one hwtl subcommand and return its verdict line.

    :param args: hwtl arguments, subcommand first
    :param timeout: seconds to wait for the verdict
    :return: verdict line
    """
    out = (
        pytest.product.core(0)
        .sendCommandReadUntilPattern(
            f"hwtl {args}", pattern=VERDICT, timeout=timeout
        )
        .output
    )
    found = re.search(VERDICT, out)
    assert found, out
    return found.group(0)


def run_entry(feature: str, entry: Entry, timeout: int = 60) -> None:
    """Run one board entry and assert its PASS verdict.

    :param feature: hwtl subcommand
    :param entry: board entry
    :param timeout: seconds to wait for the verdict
    """
    _, args = entry
    verdict = hwtl(f"{feature} {args}", timeout)
    assert verdict.startswith("hwtl: PASS"), verdict
