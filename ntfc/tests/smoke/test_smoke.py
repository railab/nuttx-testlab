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

import re

import pytest
from _net_common import nettl_client, nettl_server

NSH_TOO_MANY_ARGS_BUG = (
    "nshlib/nsh_parse.c: nsh_parse_command() prints 'too many arguments' "
    "when argc > CONFIG_NSH_MAXARGUMENTS but still runs the command"
)

EXIT_SIGTERM_BUG = (
    "sched/group: with GROUP_KILL_CHILDREN_TIMEOUT_MS != 0 (default -1 "
    "with SIG_DEFAULT) exit() sends SIGTERM to the other threads, which "
    "runs their handlers, and with < 0 waits forever for them to exit"
)


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


def _hello_cmd(argc: int) -> str:
    """Build a ``hello`` command line with ``argc`` argv entries.

    Skips the test if the line does not fit in ``CONFIG_LINE_MAX``.

    :param argc: argv entries, the command name included
    :return: command line
    """
    cmd = "hello" + " x" * (argc - 1)
    line_max = pytest.product.core(0).conf.kv_check("CONFIG_LINE_MAX")
    if len(cmd) >= int(line_max or 80):
        pytest.skip(f"{len(cmd)} byte command exceeds CONFIG_LINE_MAX")
    return cmd


def _nsh_maxargs() -> int:
    """Return the maximum argc that NSH accepts.

    ``nshlib/nsh.h`` raises ``CONFIG_NSH_MAXARGUMENTS`` to 11 when
    networking and the ``ifconfig`` command are enabled.

    :return: maximum argc accepted by NSH
    """
    conf = pytest.product.core(0).conf
    value = conf.kv_check("CONFIG_NSH_MAXARGUMENTS")
    assert value, "CONFIG_NSH_MAXARGUMENTS not set"
    maxargs = int(value)
    if (
        maxargs < 11
        and conf.kv_check("CONFIG_NET")
        and not conf.kv_check("CONFIG_NSH_DISABLE_IFCONFIG")
    ):
        maxargs = 11
    return maxargs


@pytest.mark.cmd_check("hello_main")
def test_nsh_max_args() -> None:
    """A command with exactly CONFIG_NSH_MAXARGUMENTS argv entries runs."""
    out = (
        pytest.product.core(0)
        .sendCommandReadUntilPattern(
            _hello_cmd(_nsh_maxargs()), pattern="Hello, World", timeout=10
        )
        .output
    )
    assert "Hello, World" in out, out
    assert "too many arguments" not in out, out


@pytest.mark.cmd_check("hello_main")
@pytest.mark.xfail(strict=True, reason=NSH_TOO_MANY_ARGS_BUG)
def test_nsh_too_many_args() -> None:
    """A command with too many arguments fails and does not run.

    ``hello`` with CONFIG_NSH_MAXARGUMENTS + 1 argv entries must print
    ``nsh: hello: too many arguments`` and return to the prompt without
    running ``hello``.
    """
    out = (
        pytest.product.core(0)
        .sendCommandReadUntilPattern(
            _hello_cmd(_nsh_maxargs() + 1),
            pattern=r"too many arguments[\s\S]*nsh> ",
            timeout=10,
        )
        .output
    )
    assert re.search(r"nsh: hello: too many arguments", out), out
    assert "Hello, World" not in out, out


@pytest.mark.cmd_check("exittl_main")
def test_exit_ends_all_threads(request: pytest.FixtureRequest) -> None:
    """exit() ends the whole task without running signal handlers.

    ``exittl`` starts a child task whose second thread catches SIGTERM
    and waits in pause(); the child's main thread then calls exit(). The
    child must be gone within 5 s and the SIGTERM handler must not run.

    :param request: pytest request, to mark the expected failure
    """
    wait = pytest.product.core(0).conf.kv_check(
        "CONFIG_GROUP_KILL_CHILDREN_TIMEOUT_MS"
    )
    if wait and int(wait) != 0:
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=EXIT_SIGTERM_BUG)
        )
    out = (
        pytest.product.core(0)
        .sendCommandReadUntilPattern(
            "exittl", pattern=r"exittl: (PASS|FAIL)[^\r\n]*", timeout=15
        )
        .output
    )
    found = re.search(r"exittl: (PASS|FAIL)[^\r\n]*", out)
    assert found, out
    assert found.group(1) == "PASS", out
