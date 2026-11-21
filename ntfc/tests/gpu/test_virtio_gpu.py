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

"""virtio GPU device tests.

QEMU gives the node a 2D ``virtio-gpu`` device with the resolution set
by ``xres=``/``yres=``; NuttX registers it as ``/dev/fb0``. ``fbtl`` on
the node draws through the framebuffer interface and the test reads
what the device shows with the QMP ``screendump`` command (PPM, 8-bit
RGB).

Expectations come from the VIRTIO 1.3 GPU device section and the NuttX
framebuffer interface (``include/nuttx/video/fb.h``,
``Documentation/os/graphics/framebuffer_char_driver.rst``).
"""

import os
import re
import tempfile
import time
from typing import Dict, Tuple

import pytest
from _qmp_common import Qmp

TIMEOUT = 10
VERDICT_RE = r"fbtl: (PASS|FAIL)[^\r\n]*"

FB_FMT_RGB32 = 13

RED = 0xFF0000
GREEN = 0x00FF00
BLUE = 0x0000FF
BLACK = 0x000000


def _resolution() -> Tuple[int, int]:
    """Return the display size given to QEMU.

    :return: ``xres``, ``yres`` of the virtio-gpu device
    """
    args = str(pytest.product.core(0).conf.exec_args)
    found = re.search(r"virtio-gpu-\w+,\S*?xres=(\d+),yres=(\d+)", args)
    assert found, f"no virtio-gpu xres/yres in {args!r}"
    return int(found.group(1)), int(found.group(2))


def _fbtl(args: str) -> str:
    """Run ``fbtl`` and check that it passed.

    :param args: fbtl arguments
    :return: fbtl output
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        f"fbtl {args}", pattern=VERDICT_RE, timeout=TIMEOUT
    )
    out = str(ret.output)
    found = re.search(VERDICT_RE, out)
    assert found and found.group(1) == "PASS", out
    return out


class _Screen:
    """One QMP ``screendump`` of the display, as 8-bit RGB pixels."""

    def __init__(self) -> None:
        """Take the screendump and parse the PPM file."""
        fd, path = tempfile.mkstemp(suffix=".ppm")
        os.close(fd)
        qmp = Qmp()
        try:
            qmp.execute("screendump", filename=path)
        finally:
            qmp.close()
        with open(path, "rb") as ppm:
            data = ppm.read()
        os.unlink(path)
        magic, size, maxval, self.pixels = data.split(b"\n", 3)
        assert magic == b"P6" and maxval == b"255", data[:32]
        self.width, self.height = (int(v) for v in size.split())

    def at(self, x: int, y: int) -> int:
        """Return the color of one pixel.

        :param x: column
        :param y: row
        :return: ``0xRRGGBB``
        """
        off = (y * self.width + x) * 3
        return int.from_bytes(self.pixels[off : off + 3], "big")


def _screen_shows(points: Dict[Tuple[int, int], int]) -> _Screen:
    """Wait until the display shows the given colors.

    The device may scan out on its own refresh, so the check is repeated
    for a short while.

    :param points: ``{(x, y): 0xRRGGBB}``
    :return: last screendump
    """
    deadline = time.monotonic() + 2
    while True:
        screen = _Screen()
        got = {p: screen.at(*p) for p in points}
        if got == points or time.monotonic() > deadline:
            assert got == points
            return screen
        time.sleep(0.1)


def _clear() -> None:
    """Show the first frame, filled with black."""
    xres, yres = _resolution()
    _fbtl(f"-f 0,0,{xres},{yres},{BLACK:06x} -p 0")


def test_virtio_gpu_display_info() -> None:
    """Read the video and plane info of ``/dev/fb0``.

    VIRTIO 1.3, GPU device operation: VIRTIO_GPU_CMD_GET_DISPLAY_INFO
    "Retrieve the current output configuration"; QEMU reports the
    ``xres``/``yres`` it was given. ``fb.h``: ``stride`` is the "Length
    of a line in bytes" and ``fblen`` the "Length of frame buffer memory
    in bytes".
    """
    out = _fbtl("-i")
    found = re.search(r"fbtl: info ([^\r\n]*)", out)
    assert found, out
    info = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", found.group(1))}
    xres, yres = _resolution()
    assert (info["xres"], info["yres"]) == (xres, yres)
    assert info["xvirt"] == xres and info["yvirt"] % yres == 0
    assert info["stride"] >= xres * info["bpp"] // 8
    assert info["fblen"] >= info["stride"] * info["yvirt"]
    if info["fmt"] == FB_FMT_RGB32:
        assert info["bpp"] == 32


def test_virtio_gpu_screen_size() -> None:
    """Check the size of the scanout the device shows.

    VIRTIO 1.3: VIRTIO_GPU_CMD_SET_SCANOUT "Set the scanout parameters
    for a single output", with the rectangle of the resource to show;
    the driver shows the whole display.
    """
    _clear()
    screen = _Screen()
    assert (screen.width, screen.height) == _resolution()


def test_virtio_gpu_fill_mmap() -> None:
    """Fill a rectangle through ``mmap()`` and update it.

    Framebuffer driver docs: an application typically draws through the
    ``mmap()``-ed framebuffer; ``fb.h``: ``FBIO_UPDATE`` "Update a
    rectangular region in the framebuffer". VIRTIO 1.3:
    VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D moves the guest memory into the
    host resource that the scanout shows.
    """
    _clear()
    _fbtl(f"-f 10,20,30,40,{RED:06x}")
    _screen_shows(
        {
            (10, 20): RED,
            (39, 59): RED,
            (9, 20): BLACK,
            (40, 20): BLACK,
            (10, 19): BLACK,
            (10, 60): BLACK,
        }
    )


def test_virtio_gpu_fill_write() -> None:
    """Fill a rectangle with ``lseek()`` and ``write()``, then update it.

    Framebuffer driver docs: "``write()`` puts data into the framebuffer
    memory and also updates the file position", and "``seek()`` sets
    the file position to any desired location within the framebuffer".
    """
    _clear()
    xres, yres = _resolution()
    _fbtl(f"-f {xres - 20},{yres - 10},20,10,{GREEN:06x} -w")
    _screen_shows(
        {
            (xres - 20, yres - 10): GREEN,
            (xres - 1, yres - 1): GREEN,
            (xres - 21, yres - 1): BLACK,
            (xres - 1, yres - 11): BLACK,
        }
    )


def test_virtio_gpu_pan_display() -> None:
    """Draw into the second frame, pan to it and back.

    Framebuffer driver docs: "Each frame occupies ``framesize = stride *
    yres`` bytes; frame ``i`` starts at ``i * framesize``"; ``fb.h``:
    ``FBIOPAN_DISPLAY`` "Pan display" to the frame at ``yoffset``.
    VIRTIO 1.3: VIRTIO_GPU_CMD_SET_SCANOUT sets the resource the output
    shows.
    """
    _clear()
    xres, yres = _resolution()
    _fbtl(f"-f 0,0,8,8,{RED:06x}")
    _fbtl(f"-f 0,{yres},{xres},{yres},{BLUE:06x}")
    _fbtl(f"-p {yres}")
    _screen_shows({(0, 0): BLUE, (xres - 1, yres - 1): BLUE})
    _fbtl("-p 0")
    _screen_shows({(0, 0): RED, (xres - 1, yres - 1): BLACK})
