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

"""Shared pytest setup for nuttx-testlab NTFC tests."""

import os
import sys
from typing import Iterator

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _can_common import cantl_cleanup  # noqa: E402
from _net_common import nettl_cleanup  # noqa: E402
from _netleak import net_leak  # noqa: E402, F401 - fixture


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Iterator[None]:
    """Keep the call phase report as ``item.rep_call`` for fixtures.

    :param item: test item
    """
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if report.when == "call":
        item.rep_call = report  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def nettl_servers() -> Iterator[None]:
    """Stop background nettl servers after each test."""
    yield
    nettl_cleanup()


@pytest.fixture(autouse=True)
def cantl_receivers() -> Iterator[None]:
    """Let background cantl receivers left by a failed test exit."""
    yield
    cantl_cleanup()
