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

"""Smoke test: the device boots to NSH and answers commands."""

import pytest
from _net_common import nettl_client, nettl_server


def test_uname() -> None:
    """Check that NSH reports a NuttX system."""
    ret = pytest.product.sendCommand("uname -a", "NuttX", timeout=10)
    assert ret == 0


@pytest.mark.cmd_check("nettl_main")
def test_nettl_loopback_tcp() -> None:
    """Run nettl TCP echo over loopback on one node."""
    nettl_server(0, False, 5100)
    verdict = nettl_client(0, "127.0.0.1", False, 5100, 8192)
    assert verdict == "nettl: PASS tx=8192 rx=8192 err=0"


@pytest.mark.cmd_check("nettl_main")
def test_nettl_loopback_udp() -> None:
    """Run nettl UDP echo over loopback on one node."""
    nettl_server(0, True, 5101)
    verdict = nettl_client(0, "127.0.0.1", True, 5101, 50)
    assert verdict == "nettl: PASS tx=50 rx=50 lost=0 err=0"
