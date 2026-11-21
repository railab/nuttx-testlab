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

"""virtio SPI controller tests.

QEMU's generic ``vhost-user-device`` (VIRTIO device ID 45) is served by
``_vhost_user.py``: an SPI controller with two chip selects, 8 and 16
bit words, all four clock modes and a 10 MHz maximum clock. Every chip
select has MISO wired to MOSI, so a full-duplex transfer reads back what
it wrote; a half-duplex read returns ``(i * 7 + 1) & 0xff`` for byte
``i``. The test reads every transfer head the device received. ``spitl``
on the node sends ``SPIIOC_TRANSFER``.

Expectations come from the VIRTIO 1.3 SPI controller device section and
the NuttX SPI interface (``include/nuttx/spi/spi.h``,
``include/nuttx/spi/spi_transfer.h``).
"""

import re
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from _vhost_user import VhostCtl

TIMEOUT = 10
VERDICT_RE = r"spitl: (PASS|FAIL)[^\r\n]*"
MAX_FREQ_HZ = 10000000

VIRTIO_SPI_CPHA = 1 << 0
VIRTIO_SPI_CPOL = 1 << 1

KEEP_CS_BUG = (
    "drivers/virtio/virtio-spi.c:329: virtio_spi_select() ignores "
    "de-selects and head.cs_change stays 1 (virtio-spi.c:636), so a "
    "transfer the caller keeps selected after (spi_trans_s.deselect "
    "false) is sent with cs_change 1, chip select deasserted"
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


def _spitl(args: str) -> Tuple[str, List[str]]:
    """Run ``spitl`` and return its verdict and the bytes read.

    :param args: spitl arguments
    :return: verdict line, one hex string per transfer that read
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        f"spitl {args}", pattern=VERDICT_RE, timeout=TIMEOUT
    )
    out = str(ret.output)
    found = re.search(VERDICT_RE, out)
    assert found, out
    return found.group(0), re.findall(r"spitl: rx\d=([0-9a-f]*)", out)


def _heads(ctl: VhostCtl, count: int) -> List[Dict[str, Any]]:
    """Return the transfer heads the device received.

    :param ctl: control client
    :param count: number of transfers expected
    :return: logged heads
    """
    log = ctl("log")
    assert len(log) == count, log
    return log


def test_virtio_spi_registered() -> None:
    """Check that the controller got its NuttX SPI character device.

    ``spi_transfer.h``: ``spi_register()`` "Create and register the SPI
    character driver", "registered as /dev/spiN"; the virtio controller
    registers bus 0 when it is probed.
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        "ls /dev", pattern=r"nsh> ", timeout=TIMEOUT
    )
    out = str(ret.output)
    assert re.search(r"\bspi0\b", out), out


def test_virtio_spi_full_duplex(ctl: VhostCtl) -> None:
    """Exchange five bytes in one full-duplex transfer.

    VIRTIO 1.3: "For full-duplex transfer, the Virtio SPI driver MUST
    send struct virtio_spi_transfer_head, tx_buf, rx_buf and struct
    virtio_spi_transfer_result to the SPI Virtio Device in that order",
    and "MUST guarantee that the buffer size of tx_buf and rx_buf is the
    same".
    """
    verdict, rx = _spitl("-x 0102a3b4c5")
    assert verdict == "spitl: PASS"
    assert rx == ["0102a3b4c5"]
    (head,) = _heads(ctl, 1)
    assert head["tx"] == "0102a3b4c5" and head["rxlen"] == 5
    assert (head["readable"], head["writable"]) == (2, 2)


def test_virtio_spi_half_duplex_write(ctl: VhostCtl) -> None:
    """Send three bytes without reading.

    VIRTIO 1.3: "For half-duplex write, the Virtio SPI driver MUST send
    struct virtio_spi_transfer_head, tx_buf and struct
    virtio_spi_transfer_result to the SPI Virtio Device in that order."
    """
    verdict, rx = _spitl("-w 5a5b5c")
    assert verdict == "spitl: PASS" and rx == []
    (head,) = _heads(ctl, 1)
    assert head["tx"] == "5a5b5c" and head["rxlen"] == 0
    assert (head["readable"], head["writable"]) == (2, 1)


def test_virtio_spi_half_duplex_read(ctl: VhostCtl) -> None:
    """Read four bytes without sending.

    VIRTIO 1.3: "For half-duplex read, the Virtio SPI driver MUST send
    struct virtio_spi_transfer_head, rx_buf and struct
    virtio_spi_transfer_result to the SPI Virtio Device in that order";
    "rx_buf is filled by the Virtio SPI device".
    """
    verdict, rx = _spitl("-r 4")
    assert verdict == "spitl: PASS"
    assert rx == [bytes((i * 7 + 1) & 0xFF for i in range(4)).hex()]
    (head,) = _heads(ctl, 1)
    assert head["tx"] == "" and head["rxlen"] == 4
    assert (head["readable"], head["writable"]) == (1, 2)


def test_virtio_spi_transfer_params(ctl: VhostCtl) -> None:
    """Send with chip select 1, mode 3, 16-bit words at 2 MHz.

    VIRTIO 1.3, transfer head: ``chip_select_id`` "indicates the
    chipselect index to use", ``bits_per_word`` "the number of bits in
    each SPI transfer word", ``mode`` bit 0 CPHA and bit 1 CPOL, ``freq``
    "the SPI transfer speed in Hz". ``spi.h``: SPIDEV_MODE3 is "CPOL=1
    CPHA=1".
    """
    verdict, rx = _spitl("-c 1 -m 3 -b 16 -f 2000000 -x 11223344")
    assert verdict == "spitl: PASS" and rx == ["11223344"]
    (head,) = _heads(ctl, 1)
    assert head["cs"] == 1
    assert head["bits"] == 16
    assert head["mode"] & 3 == VIRTIO_SPI_CPHA | VIRTIO_SPI_CPOL
    assert head["freq"] == 2000000


def test_virtio_spi_freq_limit(ctl: VhostCtl) -> None:
    """Ask for 20 MHz on a controller that supports 10 MHz.

    VIRTIO 1.3: ``max_freq_hz`` "is the maximum clock rate supported",
    and the device "MUST set struct virtio_spi_transfer_result as
    VIRTIO_SPI_PARAM_ERR if not all parameters are valid"; the transfer
    succeeds only at a supported rate.
    """
    verdict, _ = _spitl("-f 20000000 -x 00ff")
    assert verdict == "spitl: PASS"
    (head,) = _heads(ctl, 1)
    assert 0 < head["freq"] <= MAX_FREQ_HZ


def test_virtio_spi_deselect_between(ctl: VhostCtl) -> None:
    """Send two transfers, deselecting after the first.

    VIRTIO 1.3: ``cs_change`` "indicates whether to deselect device
    before starting the next SPI transfer, 0 means chipselect keep
    asserted and 1 means chipselect deasserted then asserted again".
    ``spi_transfer.h``: ``deselect`` is "De-select after transfer".
    """
    verdict, _ = _spitl("-x 0102 -x 0304")
    assert verdict == "spitl: PASS"
    first, _ = _heads(ctl, 2)
    assert first["cs_change"] == 1


@pytest.mark.xfail(strict=True, reason=KEEP_CS_BUG)
def test_virtio_spi_keep_cs_between(ctl: VhostCtl) -> None:
    """Send two transfers, keeping chip select asserted between them.

    VIRTIO 1.3: ``cs_change`` "0 means chipselect keep asserted";
    ``spi_transfer.h``: a transfer with ``deselect`` false is not
    de-selected after it, so the first request carries ``cs_change`` 0.
    """
    verdict, _ = _spitl("-K -x 0102 -x 0304")
    assert verdict == "spitl: PASS"
    first, _ = _heads(ctl, 2)
    assert first["cs_change"] == 0
