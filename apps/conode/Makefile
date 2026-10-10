############################################################################
# apps/conode/Makefile
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

include $(APPDIR)/Make.defs

PROGNAME  = $(CONFIG_TESTLAB_CONODE_PROGNAME)
PRIORITY  = $(CONFIG_TESTLAB_CONODE_PRIORITY)
STACKSIZE = $(CONFIG_TESTLAB_CONODE_STACKSIZE)
MODULE    = $(CONFIG_TESTLAB_CONODE)

CFLAGS += -DHAVE_CONFIG_H=1

# Lely headers use bare "#if __cplusplus"

CFLAGS += -Wno-undef

MAINSRC = conode_main.c

include $(APPDIR)/Application.mk
