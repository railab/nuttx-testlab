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

"""CPython (apps/interpreters/python) on a NuttX node.

Each test writes a script to the node with NSH ``echo`` (short lines:
longer command lines make the NSH line editor redraw on every key) and
checks the ``PYOK ...`` line ``python`` prints. Script lines use single
quotes only: NSH double-quoted strings have no escapes.
"""

import base64
import hashlib
import re
import zlib
from typing import Any, List

import pytest
from _host_services import TcpEchoServer

pytestmark = [
    pytest.mark.dep_config("CONFIG_INTERPRETERS_CPYTHON"),
    pytest.mark.cmd_check("python_main"),
]

SCRIPT = "/tmp/t.py"
RESULT_RE = r"(?m)^PY(OK|FAIL)[^\r\n]*"
HOST_SLIRP = "10.0.2.2"  # the host as seen from QEMU user networking
MAX_LINE = 70


def _core() -> Any:
    """Return the NTFC core handler of the node.

    The product handler drops the command output on success.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.products[0].core(0)


def _run(lines: List[str], pattern: str = RESULT_RE) -> str:
    """Write a script on the node and run it with ``python``.

    :param lines: script lines without double quotes
    :param pattern: output pattern that ends the run
    :return: the first match of ``pattern`` or the whole output
    """
    redirect = ">"
    for line in lines:
        assert '"' not in line, "use single quotes in node Python code"
        assert len(line) <= MAX_LINE, f"script line too long: {line}"
        ret = _core().sendCommand(
            f'echo "{line}" {redirect} {SCRIPT}', timeout=10
        )
        assert ret == 0
        redirect = ">>"
    out = (
        _core()
        .sendCommandReadUntilPattern(
            f"python {SCRIPT}", pattern=pattern, timeout=180
        )
        .output
    )
    found = re.search(pattern, out)
    return found.group(0).strip() if found else out


def test_python_version() -> None:
    """The interpreter starts and reports Python 3."""
    out = _run(["import sys", "print('PYOK', sys.version_info.major)"])
    assert out == "PYOK 3", out


def test_python_stdlib() -> None:
    """Standard library modules give the same results as on the host."""
    out = _run(
        [
            "import json, math, struct, hashlib, zlib, base64, re",
            "d = b'nuttx' * 100",
            "r = [hashlib.sha256(d).hexdigest()[:16]]",
            "r.append(zlib.crc32(d))",
            "r.append(base64.b64encode(d[:9]).decode())",
            "r.append(struct.pack('<IH', 7, 9).hex())",
            "r.append(round(math.sqrt(2), 9))",
            "r.append(json.dumps({'a': [1, 2]}, separators=(',', ':')))",
            "r.append(re.sub('b+', '-', 'abbbc'))",
            "print('PYOK', *r)",
        ]
    )
    data = b"nuttx" * 100
    expect = " ".join(
        [
            "PYOK",
            hashlib.sha256(data).hexdigest()[:16],
            str(zlib.crc32(data)),
            base64.b64encode(data[:9]).decode(),
            "070000000900",
            "1.414213562",
            '{"a":[1,2]}',
            "a-c",
        ]
    )
    assert out == expect


def test_python_exception() -> None:
    """An uncaught exception prints a traceback and NSH keeps working."""
    out = _run(["x = 1", "print(x / 0)"], pattern=r"ZeroDivisionError")
    assert out == "ZeroDivisionError", out
    assert _core().sendCommand("echo alive", "alive", timeout=10) == 0


def test_python_files() -> None:
    """File I/O on the node: write, read back, list and remove."""
    out = _run(
        [
            "import os",
            "p = '/tmp/py_io.txt'",
            "with open(p, 'w') as f:",
            "    f.write('line' + chr(10) * 1)",
            "    f.writelines('x' + chr(10) for _ in range(999))",
            "n = len(open(p).read().splitlines())",
            "seen = 'py_io.txt' in os.listdir('/tmp')",
            "os.remove(p)",
            "print('PYOK', n, seen, os.path.exists(p))",
        ]
    )
    assert out == "PYOK 1000 True False", out


def test_python_script() -> None:
    """A script with a function and a loop runs to completion."""
    out = _run(
        [
            "def fib(n):",
            "    a, b = 0, 1",
            "    for _ in range(n):",
            "        a, b = b, a + b",
            "    return a",
            "print('PYOK', fib(90), sum(range(10000)))",
        ]
    )
    assert out == "PYOK 2880067194370816120 49995000", out


def test_python_threads() -> None:
    """Threads share a counter under a lock; sleep() is roughly right."""
    out = _run(
        [
            "import threading, time",
            "n = 0",
            "lock = threading.Lock()",
            "def work():",
            "    global n",
            "    for _ in range(1000):",
            "        with lock:",
            "            n += 1",
            "ts = [threading.Thread(target=work) for _ in range(4)]",
            "for t in ts: t.start()",
            "for t in ts: t.join()",
            "t0 = time.monotonic()",
            "time.sleep(0.5)",
            "dt = time.monotonic() - t0",
            "print('PYOK', n, 0.45 < dt < 1.5)",
        ]
    )
    assert out == "PYOK 4000 True", out


def test_python_thread_stack() -> None:
    """A thread has enough stack for C-level recursion in the interpreter.

    ``json.dumps`` of a 100-level nested list recurses in the C encoder.
    """
    out = _run(
        [
            "import json, threading",
            "r = list()",
            "d = list()",
            "for _ in range(100): d = [d]",
            "t = threading.Thread(target=lambda: r.append(json.dumps(d)))",
            "t.start()",
            "t.join()",
            "print('PYOK', len(r[0]))",
        ]
    )
    assert out == "PYOK 202", out


def test_python_socket_host() -> None:
    """A Python TCP client exchanges 64 KiB with a host echo server.

    The node reaches the host through QEMU user networking (10.0.2.2).
    """
    port = 5600
    with TcpEchoServer("0.0.0.0", port) as srv:
        out = _run(
            [
                "import socket",
                f"a = ('{HOST_SLIRP}', {port})",
                "s = socket.create_connection(a, 10)",
                "d = bytes(range(256)) * 256",
                "s.sendall(d)",
                "r = b''",
                "while len(r) < len(d):",
                "    r += s.recv(4096)",
                "s.close()",
                "print('PYOK', len(r), r == d)",
            ]
        )
    assert out == "PYOK 65536 True", out
    assert srv.connections == 1
