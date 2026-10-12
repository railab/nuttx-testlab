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

"""Shared file system helpers: ``fstl`` verdicts and its data pattern.

``fstl`` (``apps/fstl``) prints one ``fstl: PASS|FAIL <check> ...``
line per run. Its data pattern: the 4-byte word at offset ``w`` (a
multiple of 4) holds ``w ^ key(seed)``, little-endian, with
``key(seed) = seed * 0x9e3779b9`` (32 bits).
"""

import re
import struct
from typing import Any, Dict

import pytest

VERDICT_RE = r"fstl: (PASS|FAIL) [^\r\n]*"
PROMPT_RE = r"nsh> "


def core() -> Any:
    """Return the NTFC core handler of the node.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.product.core(0)


def kv(name: str) -> Any:
    """Return a Kconfig value of the node's build.

    :param name: option name, e.g. ``CONFIG_TESTLAB_ELFTL_STACKSIZE``
    :return: the value, ``False`` if it is not set
    """
    return core().conf.kv_check(name)


def run(cmd: str, timeout: int = 30) -> str:
    """Run an NSH command and return its output up to the next prompt.

    :param cmd: command line
    :param timeout: seconds to wait for the prompt
    :return: output, command echo included
    """
    ret = core().sendCommandReadUntilPattern(
        cmd, pattern=PROMPT_RE, timeout=timeout
    )
    return str(ret.output)


def verdict_of(output: str, regex: str = VERDICT_RE) -> str:
    """Return the first verdict line in some output.

    :param output: device output
    :param regex: verdict line pattern
    :return: the line without line end, empty if there is none
    """
    found = re.search(regex, output)
    return found.group(0).strip() if found else ""


def fstl(args: str, timeout: int = 60) -> str:
    """Run ``fstl`` and return its verdict line.

    :param args: check name and arguments
    :param timeout: seconds to wait for the verdict
    :return: verdict line, empty if there is none
    """
    ret = core().sendCommandReadUntilPattern(
        f"fstl {args}", pattern=VERDICT_RE, timeout=timeout
    )
    return verdict_of(str(ret.output))


def fstl_pass(args: str, timeout: int = 60) -> Dict[str, str]:
    """Run ``fstl``, require a PASS verdict and return its fields.

    :param args: check name and arguments
    :param timeout: seconds to wait for the verdict
    :return: ``key=value`` fields of the verdict line
    """
    verdict = fstl(args, timeout)
    assert verdict.startswith("fstl: PASS"), f"fstl {args}: {verdict!r}"
    return fields(verdict)


def fields(line: str) -> Dict[str, str]:
    """Return the ``key=value`` fields of a verdict line.

    :param line: verdict line
    :return: field values by key
    """
    return dict(re.findall(r"(\w+)=(\S+)", line))


def pattern(offset: int, length: int, seed: int = 0) -> bytes:
    """Return ``fstl`` pattern bytes.

    :param offset: file offset of the first byte
    :param length: number of bytes
    :param seed: pattern seed
    :return: pattern bytes
    """
    key = (seed * 0x9E3779B9) & 0xFFFFFFFF
    first = offset & ~3
    nwords = (offset + length - first + 3) // 4
    words = [(first + 4 * i) ^ key for i in range(nwords)]
    data = struct.pack(f"<{nwords}I", *words)
    start = offset - first
    return data[start : start + length]
