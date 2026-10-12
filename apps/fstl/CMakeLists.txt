# ##############################################################################
# apps/fstl/CMakeLists.txt
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations under
# the License.
#
# ##############################################################################

if(CONFIG_TESTLAB_FSTL)
  nuttx_add_application(
    NAME
    ${CONFIG_TESTLAB_FSTL_PROGNAME}
    SRCS
    fstl_main.c
    fstl_io.c
    fstl_dir.c
    STACKSIZE
    ${CONFIG_TESTLAB_FSTL_STACKSIZE}
    PRIORITY
    ${CONFIG_TESTLAB_FSTL_PRIORITY}
    MODULE
    ${CONFIG_TESTLAB_FSTL})
endif()
