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

RIGS: Dict[str, Dict[str, List[Any]]] = {}


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
