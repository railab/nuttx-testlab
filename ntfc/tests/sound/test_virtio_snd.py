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

"""virtio sound device tests.

QEMU gives the node a ``virtio-sound`` device with one output and one
input stream (``/dev/audio/pcm0p``, ``/dev/audio/pcm0c``). With the
``wav`` audiodev and the mixing engine off, QEMU appends the raw bytes
of every played buffer to a file, which the test reads back; with the
``none`` audiodev the input stream records silence in real time.
``sndtl`` on the node drives the NuttX audio ioctls.

Expectations come from the VIRTIO 1.3 sound device section and the
NuttX audio interface (``include/nuttx/audio/audio.h``,
``Documentation/os/audio``).
"""

import re
import time
from typing import Dict, Tuple

import pytest
from _net_common import pattern

PLAY = "/dev/audio/pcm0p"
CAPTURE = "/dev/audio/pcm0c"
TIMEOUT = 20

VERDICT_RE = r"sndtl: (PASS|FAIL) [^\r\n]*"
BUFINFO_RE = r"sndtl: bufinfo nbuffers=(\d+) size=(\d+)"

AUDIO_TYPE_INPUT = 0x01
AUDIO_TYPE_OUTPUT = 0x02
AUDIO_SAMP_RATE_48K = 0x0100

FILL = 0xA5
FLUSH_BYTES = 16384
MARKLEN = 7

END_OF_STREAM_BUG = (
    "drivers/virtio/virtio-snd.c:433: virtio_snd_send_pcm() always "
    "gives the device period_bytes of samples, not the apb's nbytes, "
    "so the device also plays the unused end of the last buffer"
)

RATES_BUG = (
    "drivers/virtio/virtio-snd.c:745: virtio_snd_getcaps() stores the "
    "16-bit AUDIO_SAMP_RATE_* mask in the 8-bit ac_controls.b[0], which "
    "drops AUDIO_SAMP_RATE_48K and all higher rates"
)

_IDS = iter(range(1, 256))


def _args() -> str:
    """Return the QEMU arguments of the product.

    :return: ``exec_args`` of core 0
    """
    return str(pytest.product.core(0).conf.exec_args)


def _wav_path() -> str:
    """Return the file the ``wav`` audiodev writes, or skip.

    :return: path given to ``-audiodev wav,path=``
    """
    found = re.search(r"-audiodev\s+wav,\S*?path=([^,\s]+)", _args())
    if not found:
        pytest.skip("no wav audiodev in this product")
    return found.group(1)


def _need_capture() -> None:
    """Skip unless the input stream records from the ``none`` audiodev."""
    if not re.search(r"-audiodev\s+none,", _args()):
        pytest.skip("no none audiodev in this product")


def _sndtl(args: str) -> Tuple[str, str]:
    """Run ``sndtl`` and return its verdict line and output.

    :param args: sndtl arguments
    :return: verdict line, whole output
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        f"sndtl {args}", pattern=VERDICT_RE, timeout=TIMEOUT
    )
    out = str(ret.output)
    found = re.search(VERDICT_RE, out)
    assert found, out
    return found.group(0), out


def _fields(verdict: str) -> Dict[str, int]:
    """Parse the ``key=value`` fields of a verdict line.

    :param verdict: sndtl verdict line
    :return: field values
    """
    return {k: int(v, 0) for k, v in re.findall(r"(\w+)=(\w+)", verdict)}


def _period(dev: str, extra: str = "") -> int:
    """Return the buffer size ``AUDIOIOC_GETBUFFERINFO`` reports.

    :param dev: audio device
    :param extra: sndtl stream format options
    :return: buffer size in bytes, after configuring that format
    """
    verdict, out = _sndtl(f"-i {dev} {extra}")
    assert verdict == "sndtl: PASS info", out
    found = re.search(BUFINFO_RE, out)
    assert found, out
    return int(found.group(2))


def _expected(sid: int, length: int) -> bytes:
    """Return the bytes ``sndtl -s sid`` plays.

    :param sid: marker id
    :param length: stream length
    :return: ``SNDTL<id>`` marker followed by the nettl pattern
    """
    mark = b"SNDTL" + f"{sid:02x}".encode()
    return (mark + pattern(MARKLEN, length - MARKLEN))[:length]


def _stream_end(data: bytes, length: int) -> int:
    """Return where a stream ends: at the next filler or marker.

    :param data: file data from the stream's marker on
    :param length: bytes the node played; the search starts there
    :return: stream length as received, -1 while not yet written
    """
    ends = [data.find(end, length) for end in (bytes([FILL]), b"SNDTL")]
    ends = [end for end in ends if end >= 0]
    return min(ends) if ends else -1


def _received(sid: int, length: int, extra: str = "") -> bytes:
    """Return what the device played from the stream with marker ``sid``.

    A filler stream of ``FILL`` bytes in the same format follows, so
    QEMU's buffered file writes reach the file (a new format makes QEMU
    start the file again).

    :param sid: marker id
    :param length: bytes the node played
    :param extra: sndtl stream format options of the stream
    :return: bytes of the stream as received, up to the next stream
    """
    verdict, _ = _sndtl(f"-p {PLAY} -l {FLUSH_BYTES} -f {FILL} {extra}")
    assert verdict.startswith("sndtl: PASS"), verdict
    mark = _expected(sid, MARKLEN)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with open(_wav_path(), "rb") as wav:
            data = wav.read()
        pos = data.rfind(mark)
        end = _stream_end(data[pos:], length) if pos >= 0 else -1
        if end >= 0:
            return data[pos : pos + end]
        time.sleep(0.2)
    raise AssertionError(f"stream {sid} not in {_wav_path()}")


def _play(length: int, extra: str = "") -> int:
    """Play a marked stream and return its marker id.

    :param length: bytes to play
    :param extra: additional sndtl options
    :return: marker id
    """
    _wav_path()
    sid = next(_IDS)
    verdict, _ = _sndtl(f"-p {PLAY} -l {length} -s {sid} {extra}")
    assert verdict.startswith(f"sndtl: PASS play bytes={length}"), verdict
    return sid


def _query(dev: str) -> Dict[str, int]:
    """Return the capabilities sndtl reads with ``AUDIOIOC_GETCAPS``.

    :param dev: audio device
    :return: ``type``, ``formats``, ``channels`` and ``rates`` fields
    """
    verdict, out = _sndtl(f"-q {dev}")
    assert verdict == "sndtl: PASS query", out
    caps: Dict[str, int] = {}
    for line in re.findall(r"sndtl: caps ([^\r\n]*)", out):
        caps.update(_fields(line.replace(",", " ")))
    return caps


def test_virtio_snd_registered() -> None:
    """Check that each stream got its NuttX audio device.

    VIRTIO 1.3, PCM stream information: ``direction`` "indicates the
    direction of data flow (VIRTIO_SND_D_*)"; QEMU's ``virtio-sound``
    has one output and one input stream. ``Documentation/os/audio``:
    ``audio_register()`` "creates the device node (``/dev/audio/<name>``".
    """
    ret = pytest.product.core(0).sendCommandReadUntilPattern(
        "ls /dev/audio", pattern=r"nsh> ", timeout=TIMEOUT
    )
    out = str(ret.output)
    assert re.search(r"\bpcm0p\b", out), out
    assert re.search(r"\bpcm0c\b", out), out


def test_virtio_snd_stream_directions() -> None:
    """Query the type of the output and the input device.

    VIRTIO 1.3: ``direction`` "indicates the direction of data flow";
    ``audio.h``: an ``AUDIO_TYPE_QUERY`` query reports the device type,
    ``AUDIO_TYPE_OUTPUT`` or ``AUDIO_TYPE_INPUT``.
    """
    assert _query(PLAY)["type"] == AUDIO_TYPE_OUTPUT
    assert _query(CAPTURE)["type"] == AUDIO_TYPE_INPUT


@pytest.mark.xfail(strict=True, reason=RATES_BUG)
def test_virtio_snd_caps_rates() -> None:
    """Play at 48 kHz, then expect 48 kHz among the reported rates.

    VIRTIO 1.3, stream parameters: "The driver MUST NOT set the feature,
    format, and rate that are not specified in the stream
    configuration", and ``rates`` "indicates a supported frame rate bit
    map", so a 48 kHz stream the device accepts is in that map; the
    driver reports it as ``AUDIO_SAMP_RATE_48K`` (``audio.h``).
    """
    _play(4 * 1920, "-r 48000")
    assert _query(PLAY)["rates"] & AUDIO_SAMP_RATE_48K


@pytest.mark.parametrize(
    "rate,channels,bits",
    [(8000, 1, 8), (22050, 1, 16), (44100, 2, 16), (48000, 2, 32)],
)
def test_virtio_snd_play_periods(rate: int, channels: int, bits: int) -> None:
    """Play three full buffers and compare what the device received.

    VIRTIO 1.3, PCM output stream: the header "is followed by a
    device-readable buffer containing PCM frames for writing to the
    device", and "The device MUST NOT complete the I/O request until the
    buffer is totally consumed"; the stream arrives in order, exactly.
    """
    fmt = f"-r {rate} -n {channels} -b {bits}"
    length = 3 * _period(PLAY, fmt)
    sid = _play(length, fmt)
    assert _received(sid, length, fmt) == _expected(sid, length)


@pytest.mark.xfail(strict=True, reason=END_OF_STREAM_BUG)
def test_virtio_snd_play_end_of_stream() -> None:
    """Play two and a half buffers and count the bytes received.

    VIRTIO 1.3, PCM output stream: "The driver SHOULD populate the tx
    queue with period_bytes sized buffers. The only exception is the end
    of the stream." ``audio.h``: the apb ``nbytes`` is "The number of
    bytes used", so the last, half full buffer plays only those bytes.
    """
    period = _period(PLAY)
    length = 2 * period + period // 2
    sid = _play(length)
    got = _received(sid, length)
    assert len(got) == length
    assert got == _expected(sid, length)


def test_virtio_snd_play_pause_resume() -> None:
    """Pause a stream for 300 ms after two buffers, then resume it.

    ``Documentation/os/audio``: ``AUDIOIOC_PAUSE`` / ``AUDIOIOC_RESUME``
    "Suspend and resume transferring"; VIRTIO 1.3: the device "MUST NOT
    complete the I/O request until the buffer is totally consumed", so
    no buffer is lost or played twice across the pause.
    """
    length = 6 * _period(PLAY)
    sid = _play(length, "-P 2 -W 300")
    assert _received(sid, length) == _expected(sid, length)


def test_virtio_snd_play_again() -> None:
    """Play two streams one after the other on the same device.

    VIRTIO 1.3: VIRTIO_SND_R_PCM_RELEASE "Release a stream" and the
    device "MUST complete all pending I/O messages"; the next stream is
    set up again with VIRTIO_SND_R_PCM_SET_PARAMS and PREPARE, and plays
    complete.
    """
    length = 3 * _period(PLAY)
    first = _play(length)
    second = _play(length)
    assert _received(second, length) == _expected(second, length)
    assert _received(first, length) == _expected(first, length)


def test_virtio_snd_capture_full_buffers() -> None:
    """Record three buffers of silence from the input stream.

    VIRTIO 1.3, PCM input stream: "The device MUST NOT complete the I/O
    request until the buffer is full." sndtl fills each buffer with
    0xff before it is queued, so silence (0) in every byte shows the
    device wrote all of it.
    """
    _need_capture()
    length = 3 * _period(CAPTURE)
    verdict, out = _sndtl(f"-c {CAPTURE} -l {length}")
    assert verdict.startswith(f"sndtl: PASS capture bytes={length}"), out
    assert _fields(verdict)["nonzero"] == 0, verdict


def test_virtio_snd_capture_rate() -> None:
    """Record one second of 8 kHz mono 16-bit frames and time it.

    VIRTIO 1.3, stream parameters: ``rate`` "specifies a selected frame
    rate"; an input device records frames at that rate, so 16000 bytes
    (8000 frames of 2 bytes) take about one second.
    """
    _need_capture()
    verdict, out = _sndtl(f"-c {CAPTURE} -l 16000 -r 8000 -n 1 -b 16")
    assert verdict.startswith("sndtl: PASS capture bytes=16000"), out
    assert 750 <= _fields(verdict)["ms"] <= 1600, verdict
