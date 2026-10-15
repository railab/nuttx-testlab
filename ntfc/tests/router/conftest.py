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

"""Router scenario setup: both node interfaces addressed and up."""

from typing import List

import pytest
from _router_common import configure


@pytest.fixture(scope="module")
def router_ifs() -> List[str]:
    """Configure the node's two interfaces once per module.

    :return: node interface name per side (A, B)
    """
    return configure()
