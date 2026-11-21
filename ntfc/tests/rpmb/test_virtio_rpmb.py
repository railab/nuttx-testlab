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

"""virtio RPMB device tests.

QEMU's generic ``vhost-user-device`` (VIRTIO device ID 28) is served by
``_vhost_user.py``: an RPMB device of 128 KiB in 256-byte half sectors.
Each test starts from a factory-new device (no key, write counter 0).
``rpmbtl`` on the node sends one request with ``MMC_IOC_MULTI_CMD`` and
prints the response frame; the test computes and checks the MACs and
reads the device state.

Expectations come from the VIRTIO 1.3 RPMB device section, which relies
on the eMMC RPMB frame definitions.
"""

import hashlib
import hmac
import re
import struct
from typing import Dict, Iterator, Tuple

import pytest
from _vhost_user import VhostCtl

TIMEOUT = 10
VERDICT_RE = r"rpmbtl: (PASS|FAIL)[^\r\n]*"
KEY = bytes(range(32))
NONCE = bytes.fromhex("00112233445566778899aabbccddeeff")

REQ_DATA_WRITE = 0x0003
REQ_RESULT_READ = 0x0005
RESP_PROGRAM_KEY = 0x0100
RESP_GET_COUNTER = 0x0200
RESP_DATA_WRITE = 0x0300
RESP_DATA_READ = 0x0400
RES_OK = 0x0000
RES_AUTH_FAILURE = 0x0002
RES_NO_AUTH_KEY = 0x0007


@pytest.fixture
def ctl() -> Iterator[VhostCtl]:
    """Connect to the back-end with a factory-new device and empty log.

    :return: control client
    """
    client = VhostCtl()
    client("factory")
    client("clear")
    yield client
    client.close()


def _rpmbtl(args: str) -> Tuple[Dict[str, int], bytes]:
    """Run ``rpmbtl`` and return the response frame.

    :param args: rpmbtl arguments
    :return: response fields, response bytes 196..511 (MAC to the end)
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        f"rpmbtl {args}", pattern=VERDICT_RE, timeout=TIMEOUT
    )
    out = str(ret.output)
    found = re.search(VERDICT_RE, out)
    assert found and found.group(1) == "PASS", out
    line = re.search(r"rpmbtl: (resp=[^\r\n]*)", out)
    assert line, out
    fields = {k: int(v, 0) for k, v in re.findall(r"(\w+)=(\w+)", line[1])}
    mac = re.search(r"rpmbtl: mac=([0-9a-f]{64})", out)
    frame = re.search(r"rpmbtl: frame=([0-9a-f]{568})", out)
    assert mac and frame, out
    return fields, bytes.fromhex(mac[1]) + bytes.fromhex(frame[1])


def _mac(tail: bytes) -> bytes:
    """Compute the MAC of one frame.

    :param tail: frame bytes 228..511 (data to req_resp)
    :return: HMAC-SHA256 with ``KEY``
    """
    return hmac.new(KEY, tail, hashlib.sha256).digest()


def _data(seed: int) -> bytes:
    """Return the half sector ``rpmbtl -s seed`` writes.

    :param seed: data seed
    :return: 256 bytes, byte ``i`` is ``(seed + i * 31 + 7) & 0xff``
    """
    return bytes((seed + i * 31 + 7) & 0xFF for i in range(256))


def _program_key() -> None:
    """Program ``KEY`` and check the result."""
    resp, _ = _rpmbtl(f"-k {KEY.hex()}")
    assert (resp["resp"], resp["result"]) == (RESP_PROGRAM_KEY, RES_OK)


def _write(address: int, counter: int, seed: int, good: bool = True) -> int:
    """Send an authenticated write of one half sector.

    :param address: half sector
    :param counter: write counter for the request
    :param seed: data seed
    :param good: send the right MAC, else a corrupted one
    :return: result of the response
    """
    tail = _data(seed) + bytes(16)
    tail += struct.pack(">IHHHH", counter, address, 1, 0, REQ_DATA_WRITE)
    mac = bytearray(_mac(tail))
    if not good:
        mac[0] ^= 0xFF
    resp, frame = _rpmbtl(
        f"-w {address} -n {counter} -s {seed} -m {bytes(mac).hex()}"
    )
    assert resp["resp"] == RESP_DATA_WRITE
    assert frame[:32] == _mac(frame[32:])
    return resp["result"]


def test_virtio_rpmb_registered() -> None:
    """Check that the device got its NuttX driver node ``/dev/rpmb``.

    VIRTIO 1.3: "virtio-rpmb is a virtio based RPMB (Replay Protected
    Memory Block) device"; NuttX registers it as ``/dev/rpmb``.
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        "ls /dev", pattern=r"nsh> ", timeout=TIMEOUT
    )
    out = str(ret.output)
    assert re.search(r"\brpmb\b", out), out


def test_virtio_rpmb_counter_without_key(ctl: VhostCtl) -> None:
    """Read the write counter before a key is programmed.

    VIRTIO 1.3, Get Write Counter: "If the authentication key is not yet
    programmed then VIRTIO_RPMB_RES_NO_AUTH_KEY SHOULD be returned in
    result"; the request is one 512-byte frame and the response one
    frame ("The frame is in size of 512B").
    """
    resp, _ = _rpmbtl(f"-c {NONCE.hex()}")
    assert (resp["resp"], resp["result"]) == (
        RESP_GET_COUNTER,
        RES_NO_AUTH_KEY,
    )
    (req,) = ctl("log")
    assert (req["readable"], req["writable"], req["wlen"]) == (1, 1, 512)


def test_virtio_rpmb_program_key(ctl: VhostCtl) -> None:
    """Program the authentication key with a result read.

    VIRTIO 1.3: "A program key or write request can also combine with a
    result read (VIRTIO_RPMB_REQ_RESULT_READ) for a returned result";
    the device returns "the response (VIRTIO_RPMB_RESP_PROGRAM_KEY), the
    calculated MAC and the result". ``key_mac``: "If the request is
    VIRTIO_RPMB_REQ_PROGRAM_KEY, it's used as an authentication key."
    """
    _program_key()
    assert ctl("state", address=0)["key"] == KEY.hex()
    (req,) = ctl("log")
    assert [f["req_resp"] for f in req["frames"]] == [1, REQ_RESULT_READ]
    assert (req["readable"], req["writable"]) == (2, 1)


def test_virtio_rpmb_write_counter(ctl: VhostCtl) -> None:
    """Read the write counter of a new device with a nonce.

    VIRTIO 1.3: the device returns "the writer counter, a copy of the
    nonce received in the request, the calculated MAC and the result";
    the counter "MUST be initialized to zero"; the MAC is HMAC SHA-256
    over "the concatenation of the fields in the RPMB frames excluding
    stuff bytes and the MAC itself".
    """
    _program_key()
    resp, frame = _rpmbtl(f"-c {NONCE.hex()}")
    assert (resp["resp"], resp["result"]) == (RESP_GET_COUNTER, RES_OK)
    assert resp["counter"] == 0
    assert frame[32 + 256 : 32 + 272] == NONCE
    assert frame[:32] == _mac(frame[32:])


def test_virtio_rpmb_authenticated_write(ctl: VhostCtl) -> None:
    """Write one half sector with a valid MAC and write counter.

    VIRTIO 1.3, Data Write: "If the MAC and write counter comparisons are
    matched then the write request is considered to be authenticated.
    The data from the request SHOULD be written to the address indicated
    in the request and the write counter SHOULD be incremented by 1."
    """
    _program_key()
    assert _write(5, 0, 0x11) == RES_OK
    state = ctl("state", address=5)
    assert state["counter"] == 1
    assert state["data"] == _data(0x11).hex()


def test_virtio_rpmb_write_bad_mac(ctl: VhostCtl) -> None:
    """Write one half sector with a corrupted MAC.

    VIRTIO 1.3, Data Write: "If the two MAC's are different then
    VIRTIO_RPMB_RES_AUTH_FAILURE SHOULD be returned in result"; the data
    and the write counter stay unchanged.
    """
    _program_key()
    assert _write(6, 0, 0x22, good=False) == RES_AUTH_FAILURE
    state = ctl("state", address=6)
    assert state["counter"] == 0
    assert state["data"] == bytes(256).hex()


def test_virtio_rpmb_authenticated_read(ctl: VhostCtl) -> None:
    """Read back a written half sector with a nonce.

    VIRTIO 1.3, Data Read: the device returns "the block count, a copy
    of the nonce received in the request, the address, the data, the
    calculated MAC and the result".
    """
    _program_key()
    assert _write(7, 0, 0x33) == RES_OK
    resp, frame = _rpmbtl(f"-r 7 -o {NONCE.hex()}")
    assert (resp["resp"], resp["result"]) == (RESP_DATA_READ, RES_OK)
    assert (resp["address"], resp["count"]) == (7, 1)
    assert frame[32:288] == _data(0x33)
    assert frame[288:304] == NONCE
    assert frame[:32] == _mac(frame[32:])
