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

"""QEMU PCI serial cards with the NuttX PCI 16550 driver.

QEMU docs/specs/pci-serial.rst: ``pci-serial`` (1b36:0002) "PCI Region
0: IO bar, 8 bytes long, with the 16550 UART mapped to it.";
``pci-serial-2x`` and ``pci-serial-4x`` (1b36:0003, 1b36:0004) "PCI
Region 0: IO bar, with two or four 16550 UARTs mapped after each other.
The first is at offset 0, the second at offset 8, and so on."

NuttX drivers/serial/Kconfig-pci: ``16550_PCI_UARTn`` "will be
registered as /dev/ttySn"; ``16550_PCI_UARTn_PORT`` "Port in multi-port
device. Starts from 0."

Each card port ``p`` is a QEMU socket chardev at ``/tmp/tl-ps<p>.sock``
(see the session's config.yaml).  ``/dev/ttyS0`` is the ISA console.
"""

import re
import socket
import threading
import time
from typing import Any, Dict, Iterator, List

import pytest

pytestmark = pytest.mark.dep_config("CONFIG_16550_PCI_UART")

#: Ports per card, by PCI device ID.
CARD_PORTS = {0x0002: 1, 0x0003: 2, 0x0004: 4}

#: Bytes echoed per port.
COUNT = 512

#: Bytes sent before waiting for their echo.  Without flow control the
#: serial driver discards what does not fit its receive buffer
#: (``16550_PCI_UARTn_RXBUFSIZE``, default 256), and the emulated UART
#: receives at no line rate, so the host must not send more than that
#: ahead of the echo.
CHUNK = 64

SHARED_IRQ_BUG = (
    "drivers/serial/uart_pci_16550.c: every port of a multiport card "
    "attaches and enables the shared card interrupt, so opening a second "
    "port asserts in up_enable_irq() (intel64_irq.c, no IRQCHAIN)"
)

SOCK = "/tmp/tl-ps{}.sock"
VERDICT_RE = r"sertl: (?:PASS|FAIL) dev=/dev/ttyS\d+ [^\r\n]*"


def _core() -> Any:
    """Return the NTFC core handler of the node.

    :return: ``ProductCore`` for product 0, core 0
    """
    return pytest.products[0].core(0)


def _kv(name: str) -> str:
    """Return a Kconfig value of the node, empty if not set.

    :param name: Kconfig option name
    :return: option value
    """
    value = _core().conf.kv_check(name)
    return "" if value is False else str(value)


def _configured() -> Dict[int, int]:
    """Return the configured card ports and their tty numbers.

    :return: ``{card port: n}`` for every ``16550_PCI_UARTn``
    """
    ports: Dict[int, int] = {}
    for n in range(4):
        if _kv(f"CONFIG_16550_PCI_UART{n}"):
            ports[int(_kv(f"CONFIG_16550_PCI_UART{n}_PORT") or n)] = n
    return ports


def _card_ports() -> int:
    """Return the number of ports of the card in the session.

    :return: 1, 2 or 4
    """
    devices = {
        int(_kv(f"CONFIG_16550_PCI_UART{n}_DEVICE"), 16)
        for n in range(4)
        if _kv(f"CONFIG_16550_PCI_UART{n}")
    }
    assert len(devices) == 1, devices
    return CARD_PORTS[devices.pop()]


def _pattern(port: int) -> bytes:
    """Return the printable byte pattern sent to a card port.

    :param port: card port
    :return: ``COUNT`` bytes, different for every port
    """
    return bytes(65 + (port * 7 + i) % 26 for i in range(COUNT))


@pytest.fixture
def chardevs() -> Iterator[List[socket.socket]]:
    """Connect to the QEMU chardev of every card port.

    :return: connected sockets, index = card port
    """
    socks = []
    for port in range(_card_ports()):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCK.format(port))
        sock.settimeout(0.2)
        socks.append(sock)
    yield socks
    for sock in socks:
        sock.close()


def _receive(sock: socket.socket, nbytes: int, seconds: float) -> bytes:
    """Read ``nbytes`` from a chardev socket or until the time limit.

    :param sock: connected socket
    :param nbytes: bytes expected
    :param seconds: time limit
    :return: bytes received
    """
    data = b""
    end = time.monotonic() + seconds
    while len(data) < nbytes and time.monotonic() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        data += chunk
    return data


def test_pci_serial_registration() -> None:
    """Exactly the configured PCI UARTs are registered.

    PASS: ``/dev`` lists ``ttyS0`` (ISA console) and ``ttySn`` for every
    enabled ``16550_PCI_UARTn``, and no other ``ttyS`` node.
    """
    out = str(
        _core()
        .sendCommandReadUntilPattern("ls /dev", pattern=r"nsh> ", timeout=10)
        .output
    )
    found = {word for word in out.split() if word.startswith("ttyS")}
    want = {"ttyS0"} | {f"ttyS{n}" for n in _configured().values()}
    assert found == want, out


@pytest.mark.cmd_check("sertl_main")
def test_pci_serial_echo(chardevs: List[socket.socket]) -> None:
    """Every configured port talks to its own card port, all at once.

    ``sertl`` echoes ``COUNT`` bytes on each configured ``/dev/ttySn``.
    The host sends a pattern unique to each card port on all ports at
    the same time, ``CHUNK`` bytes at a time, each after the echo of the
    previous one.  PASS: each configured card port gets its own pattern
    back intact, an unconfigured port gets nothing, every ``sertl``
    passes.  A port at the wrong BAR offset would echo on another card
    port or not at all.
    """
    ports = _configured()
    if len(ports) > 1:
        pytest.skip(SHARED_IRQ_BUG)
    for n in ports.values():
        ret = _core().sendCommand(
            f"sertl -n {COUNT} -t 30 /dev/ttyS{n} &",
            f"sertl: ready dev=/dev/ttyS{n}",
            timeout=10,
        )
        assert ret == 0, f"sertl /dev/ttyS{n} did not start"

    got: Dict[int, bytes] = {}

    def run(port: int) -> None:
        pattern = _pattern(port)
        got[port] = b""
        for off in range(0, COUNT, CHUNK):
            chardevs[port].sendall(pattern[off : off + CHUNK])
            echo = _receive(chardevs[port], CHUNK, 2.0)
            got[port] += echo
            if len(echo) < CHUNK:
                break

    threads = [
        threading.Thread(target=run, args=(port,))
        for port in range(len(chardevs))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for port in range(len(chardevs)):
        want = _pattern(port) if port in ports else b""
        assert got[port] == want, (port, got[port][:64])

    verdicts: List[str] = []
    end = time.monotonic() + 40
    while len(verdicts) < len(ports) and time.monotonic() < end:
        out = _core().readUntilPattern(VERDICT_RE, timeout=40)
        verdicts += re.findall(VERDICT_RE, str(out.output))
    want = sorted(
        f"sertl: PASS dev=/dev/ttyS{n} rx={COUNT}" for n in ports.values()
    )
    assert sorted(verdicts) == want, verdicts
