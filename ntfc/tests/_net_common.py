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

"""Shared nettl helpers for smoke tests."""

import re
from typing import Any

import pytest

VERDICT_RE = r"nettl: (PASS|FAIL) tx=[^\r\n]*[\r\n]"


def _core(node: int) -> Any:
    """Return the NTFC core handler for a product.

    :param node: product index
    :return: ``ProductCore`` instance for the product's core 0
    """
    return pytest.products[node].core(0)


def nettl_server(node: int, udp: bool, port: int) -> None:
    """Start a background nettl server on a node.

    :param node: product index
    :param udp: use UDP instead of TCP
    :param port: listen port
    """
    proto = "-u " if udp else ""
    ret = _core(node).sendCommand(
        f"nettl -s {proto}-p {port} -t 30 &", "listening", timeout=10
    )
    assert ret == 0


def nettl_client(
    node: int, addr: str, udp: bool, port: int, count: int
) -> str:
    """Run nettl client on a node and return its verdict line.

    :param node: product index
    :param addr: server IPv4 address
    :param udp: use UDP instead of TCP
    :param port: server port
    :param count: bytes (TCP) or datagrams (UDP)
    :return: verdict line or empty string
    """
    proto = "-u " if udp else ""
    ret = _core(node).sendCommandReadUntilPattern(
        f"nettl -c {addr} {proto}-p {port} -n {count}",
        pattern=VERDICT_RE,
        timeout=120,
    )
    found = re.search(VERDICT_RE, ret.output)
    return found.group(0).rstrip("\r\n") if found else ""
