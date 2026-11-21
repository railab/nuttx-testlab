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

"""Shared helpers for the two-node RPMSG tests.

Node 0 (product 0) is the rptun master, node 1 its remote; each node
names the other by its CPU name, ``CONFIG_RPMSG_LOCAL_CPUNAME``.
"""

import re
from typing import Any

import pytest

PROMPT_RE = r"nsh> "


def core(node: int) -> Any:
    """Return the NTFC core handler of a node.

    :param node: product index
    :return: ``ProductCore`` of the product's core 0
    """
    return pytest.products[node].core(0)


def cpuname(node: int) -> str:
    """Return the RPMSG CPU name of a node.

    :param node: product index
    :return: ``CONFIG_RPMSG_LOCAL_CPUNAME`` without quotes
    """
    return str(core(node).conf.kv_check("CONFIG_RPMSG_LOCAL_CPUNAME")).strip(
        '"'
    )


def run(node: int, cmd: str, timeout: int = 30) -> str:
    """Run an NSH command on a node and return its output.

    :param node: product index
    :param cmd: command line
    :param timeout: seconds to wait for the next prompt
    :return: output up to the prompt, command echo included
    """
    ret = core(node).sendCommandReadUntilPattern(
        cmd, pattern=PROMPT_RE, timeout=timeout
    )
    return str(ret.output)


def verdict(node: int, cmd: str, regex: str, timeout: int = 60) -> str:
    """Run a command on a node and return its verdict line.

    :param node: product index
    :param cmd: command line
    :param regex: verdict line pattern
    :param timeout: seconds to wait for the verdict
    :return: the verdict line, empty if there is none
    """
    ret = core(node).sendCommandReadUntilPattern(
        cmd, pattern=regex, timeout=timeout
    )
    found = re.search(regex, str(ret.output))
    return found.group(0).strip() if found else ""
