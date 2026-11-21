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

"""virtio I2C adapter tests.

QEMU's ``vhost-user-i2c`` device is served by ``_vhost_user.py``: an
I2C adapter with a 256-byte EEPROM at 7-bit address 0x50 and one at
10-bit address 0x2a5 (AT24C02 protocol: the first written byte sets the
word address). The test reads every request the device received and the
EEPROM contents; ``i2ctl`` on the node sends ``I2CIOC_TRANSFER``.

Expectations come from the VIRTIO 1.3 I2C adapter device section and the
NuttX I2C character driver (``include/nuttx/i2c/i2c_master.h``).
"""

import re
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from _vhost_user import VhostCtl

TIMEOUT = 10
VERDICT_RE = r"i2ctl: (PASS|FAIL)[^\r\n]*"

EEPROM = 0x50
ABSENT = 0x51
EEPROM10 = 0x2A5

FLAGS_FAIL_NEXT = 1 << 0
FLAGS_M_RD = 1 << 1

FAIL_NEXT_BUG = (
    "drivers/virtio/virtio-i2c.c:361: virtio_i2c_transfer() stops at the "
    "first failed message and never queues the rest of the group, though "
    "the failed request had VIRTIO_I2C_FLAGS_FAIL_NEXT set; the device "
    "then fails the next request it gets, the first of the next transfer"
)


@pytest.fixture
def ctl() -> Iterator[VhostCtl]:
    """Connect to the device back-end with an empty request log.

    :return: control client
    """
    client = VhostCtl()
    client("clear")
    yield client
    client.close()


def _i2ctl(args: str) -> Tuple[str, List[str]]:
    """Run ``i2ctl`` and return its verdict and the bytes read.

    :param args: i2ctl arguments
    :return: verdict line, one hex string per read message
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        f"i2ctl {args}", pattern=VERDICT_RE, timeout=TIMEOUT
    )
    out = str(ret.output)
    found = re.search(VERDICT_RE, out)
    assert found, out
    return found.group(0), re.findall(r"i2ctl: rx=([0-9a-f]*)", out)


def _addr7(addr: int) -> int:
    """Encode a 7-bit address as the request ``addr`` field.

    :param addr: 7-bit address
    :return: A6..A0 in bits 7..1
    """
    return addr << 1


def _request(log: List[Dict[str, Any]], i: int) -> Dict[str, Any]:
    """Return one logged request.

    :param log: device request log
    :param i: request index
    :return: request fields
    """
    assert len(log) > i, log
    return log[i]


def test_virtio_i2c_registered() -> None:
    """Check that the adapter got its NuttX I2C character device.

    ``i2c_master.h``: ``i2c_register()`` "Create and register the I2C
    character driver", "registered as /dev/i2cN"; the virtio adapter
    registers bus 0 when it is probed.
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        "ls /dev", pattern=r"nsh> ", timeout=TIMEOUT
    )
    out = str(ret.output)
    assert re.search(r"\bi2c0\b", out), out


def test_virtio_i2c_write(ctl: VhostCtl) -> None:
    """Write four bytes from EEPROM word address 0x10 on.

    VIRTIO 1.3, I2C request queue: for a 7-bit address ``addr`` holds A6
    ... A0 in bits 7 ... 1; "If VIRTIO_I2C_FLAGS_M_RD bit is unset in the
    flags, then the request is called a write request", and "A driver,
    for a write request, puts the data to be written to the device into
    the buf". A single request is a group of one, so its FAIL_NEXT bit is
    clear ("a driver clears this bit on the final request").
    """
    verdict, _ = _i2ctl(f"-a {EEPROM:#x} -w 10a1b2c3d4")
    assert verdict == "i2ctl: PASS"
    log = ctl("log")
    assert len(log) == 1, log
    req = log[0]
    assert req["addr"] == _addr7(EEPROM)
    assert req["flags"] == 0
    assert req["data"] == "10a1b2c3d4"
    assert (req["readable"], req["writable"]) == (2, 1)
    rom = bytes.fromhex(ctl("mem", addr=EEPROM))
    assert rom[0x10:0x14] == bytes.fromhex("a1b2c3d4")


def test_virtio_i2c_write_read(ctl: VhostCtl) -> None:
    """Read three EEPROM bytes with a write-read transfer.

    VIRTIO 1.3: "The virtio I2C protocol supports write-read requests,
    i.e. an I2C write segment followed by a read segment ..., by grouping
    a list of requests together using the VIRTIO_I2C_FLAGS_FAIL_NEXT
    flag"; "a driver clears this bit on the final request and sets it on
    the other requests"; "A driver MUST set the VIRTIO_I2C_FLAGS_M_RD
    flag for a read operation, where the buffer is write-only for the
    device."
    """
    rom = bytes(range(256))
    ctl("setmem", addr=EEPROM, data=rom.hex())
    verdict, rx = _i2ctl(f"-a {EEPROM:#x} -w 40 -r 3")
    assert verdict == "i2ctl: PASS"
    assert rx == [rom[0x40:0x43].hex()]
    log = ctl("log")
    assert len(log) == 2, log
    write, read = log
    assert write["flags"] == FLAGS_FAIL_NEXT
    assert (write["readable"], write["writable"]) == (2, 1)
    assert read["flags"] == FLAGS_M_RD
    assert read["len"] == 3
    assert (read["readable"], read["writable"]) == (1, 2)
    assert write["addr"] == read["addr"] == _addr7(EEPROM)


def test_virtio_i2c_zero_length(ctl: VhostCtl) -> None:
    """Send a zero-length write to the EEPROM.

    VIRTIO 1.3: "The buf is optional and will not be present for a
    zero-length request, like the SMBus "Quick" command"; "A driver MUST
    NOT send the buf, for a zero-length request." The request is the out
    header and the in header only.
    """
    verdict, _ = _i2ctl(f"-a {EEPROM:#x} -z")
    assert verdict == "i2ctl: PASS"
    req = _request(ctl("log"), 0)
    assert req["len"] == 0
    assert (req["readable"], req["writable"]) == (1, 1)
    assert req["flags"] == 0


def test_virtio_i2c_ten_bit_address(ctl: VhostCtl) -> None:
    """Read from the EEPROM at 10-bit address 0x2a5.

    VIRTIO 1.3, ``addr`` for the 10-bit address mode: A7 ... A0 in bits
    15 ... 8, 1 1 1 1 0 in bits 7 ... 3, A9 A8 in bits 2 ... 1, bit 0 zero.
    """
    rom = bytes((i * 3) & 0xFF for i in range(256))
    ctl("setmem", addr=0x8000 | EEPROM10, data=rom.hex())
    verdict, rx = _i2ctl(f"-a {EEPROM10:#x} -T -w 20 -r 2")
    assert verdict == "i2ctl: PASS"
    assert rx == [rom[0x20:0x22].hex()]
    field = (EEPROM10 & 0xFF) << 8 | 0xF0 | (EEPROM10 >> 8) << 1
    assert [r["addr"] for r in ctl("log")] == [field, field]


def test_virtio_i2c_absent_device(ctl: VhostCtl) -> None:
    """Write to an address no device answers, then to the EEPROM.

    VIRTIO 1.3: the device writes "either VIRTIO_I2C_MSG_OK for success
    or VIRTIO_I2C_MSG_ERR for error"; ``i2c_master.h``, ``I2C_TRANSFER``:
    "a negated errno value on failure", so ``I2CIOC_TRANSFER`` fails. A
    single failed request ends its group, the next transfer works.
    """
    verdict, _ = _i2ctl(f"-a {ABSENT:#x} -w 0001")
    assert verdict.startswith("i2ctl: FAIL errno="), verdict
    assert _request(ctl("log"), 0)["addr"] == _addr7(ABSENT)
    verdict, _ = _i2ctl(f"-a {EEPROM:#x} -w 00")
    assert verdict == "i2ctl: PASS"


@pytest.mark.xfail(strict=True, reason=FAIL_NEXT_BUG)
def test_virtio_i2c_after_failed_group(ctl: VhostCtl) -> None:
    """Fail a write-read group, then read the EEPROM.

    VIRTIO 1.3, VIRTIO_I2C_FLAGS_FAIL_NEXT: "If this bit is set and a
    device fails to process the current request, it needs to fail the
    next request instead of attempting to execute it"; "For the remaining
    requests in the same group with the first failed one, ... a device
    needs to fail them". The group's read must reach the device, else
    the next transfer's first request is failed in its place.
    """
    verdict, _ = _i2ctl(f"-a {ABSENT:#x} -w 00 -r 1")
    assert verdict.startswith("i2ctl: FAIL"), verdict
    verdict, _ = _i2ctl(f"-a {EEPROM:#x} -w 00 -r 1")
    assert verdict == "i2ctl: PASS"
