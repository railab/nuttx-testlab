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
"""vhost-user device backends for the QEMU products, and their client.

Run as a script, it is the product's ``exec_path`` wrapper::

    _vhost_user.py --device i2c --socket S --ctl C -- qemu-system-... ...

It listens on ``S`` for QEMU's vhost-user connection (QEMU waits for it
while it creates the device), runs QEMU with the same stdio, serves one
emulated VIRTIO device and exits with QEMU's exit status. Tests read and
change the device state over the JSON-lines control socket ``C`` with
:class:`VhostCtl`.

Protocol: QEMU "Vhost-user Protocol" (docs/interop/vhost-user.rst) and
the VIRTIO 1.3 split virtqueues. Devices: the VIRTIO 1.3 I2C adapter, GPIO,
SPI controller and RPMB device sections.
"""

import argparse
import array
import hashlib
import hmac
import json
import mmap
import os
import re
import select
import socket
import struct
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

# vhost-user requests (front-end to back-end)

GET_FEATURES = 1
SET_FEATURES = 2
SET_OWNER = 3
RESET_OWNER = 4
SET_MEM_TABLE = 5
SET_VRING_NUM = 8
SET_VRING_ADDR = 9
SET_VRING_BASE = 10
GET_VRING_BASE = 11
SET_VRING_KICK = 12
SET_VRING_CALL = 13
SET_VRING_ERR = 14
GET_PROTOCOL_FEATURES = 15
SET_PROTOCOL_FEATURES = 16
GET_QUEUE_NUM = 17
SET_VRING_ENABLE = 18
GET_CONFIG = 24
SET_CONFIG = 25

F_PROTOCOL_FEATURES = 1 << 30
VIRTIO_F_VERSION_1 = 1 << 32
PROTOCOL_F_MQ = 1 << 0
PROTOCOL_F_CONFIG = 1 << 9

FLAG_VERSION = 0x1
FLAG_REPLY = 0x4
VRING_NOFD = 0x100

VIRTQ_DESC_F_NEXT = 1
VIRTQ_DESC_F_WRITE = 2

TIMEOUT = 10


class Memory:
    """Guest memory regions shared by QEMU (``SET_MEM_TABLE``)."""

    def __init__(self) -> None:
        """Start with no regions."""
        self.regions: List[Tuple[int, int, int, int, mmap.mmap]] = []

    def set_table(self, payload: bytes, fds: List[int]) -> None:
        """Map the regions of a ``SET_MEM_TABLE`` message.

        :param payload: message payload
        :param fds: one file descriptor per region
        """
        for region in self.regions:
            region[4].close()
        self.regions = []
        (count,) = struct.unpack_from("<I", payload)
        for i in range(count):
            gpa, size, uva, off = struct.unpack_from(
                "<QQQQ", payload, 8 + 32 * i
            )
            mem = mmap.mmap(fds[i], off + size, mmap.MAP_SHARED)
            os.close(fds[i])
            self.regions.append((gpa, size, uva, off, mem))

    def _find(self, addr: int, length: int, by_uva: bool) -> Tuple[Any, int]:
        """Find the mapping and offset of a guest or QEMU address.

        :param addr: guest physical address, or QEMU virtual address
        :param length: bytes accessed
        :param by_uva: ``addr`` is a QEMU virtual address
        :return: mapping, offset in it
        """
        for gpa, size, uva, off, mem in self.regions:
            base = uva if by_uva else gpa
            if base <= addr and addr + length <= base + size:
                return mem, off + addr - base
        raise ValueError(f"address {addr:#x} not in guest memory")

    def read(self, addr: int, length: int, by_uva: bool = False) -> bytes:
        """Read guest memory.

        :param addr: address
        :param length: bytes to read
        :param by_uva: ``addr`` is a QEMU virtual address
        :return: data
        """
        mem, off = self._find(addr, length, by_uva)
        return bytes(mem[off : off + length])

    def write(self, addr: int, data: bytes, by_uva: bool = False) -> None:
        """Write guest memory.

        :param addr: address
        :param data: data to write
        :param by_uva: ``addr`` is a QEMU virtual address
        """
        mem, off = self._find(addr, len(data), by_uva)
        mem[off : off + len(data)] = data


class Chain:
    """One descriptor chain taken from the available ring."""

    def __init__(
        self,
        head: int,
        readable: List[Tuple[int, int]],
        writable: List[Tuple[int, int]],
    ) -> None:
        """Keep the chain's buffers.

        :param head: index of the head descriptor
        :param readable: device-readable ``(addr, len)`` buffers
        :param writable: device-writable ``(addr, len)`` buffers
        """
        self.head = head
        self.readable = readable
        self.writable = writable

    def wlen(self) -> int:
        """Return the total size of the device-writable buffers.

        :return: bytes
        """
        return sum(length for _, length in self.writable)


class Virtqueue:
    """A split virtqueue the device processes on each kick."""

    def __init__(self, index: int, dev: "Device") -> None:
        """Set up an unconfigured queue.

        :param index: queue index
        :param dev: device that handles the requests
        """
        self.index = index
        self.dev = dev
        self.num = 0
        self.desc = self.avail = self.used = 0
        self.last_avail = 0
        self.used_idx = 0
        self.kick = -1
        self.call = -1
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start processing kicks."""
        self.stop()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop processing kicks."""
        self.running = False
        if self.thread is not None and self.thread is not (
            threading.current_thread()
        ):
            self.thread.join(1)
        self.thread = None

    def _run(self) -> None:
        """Wait for kicks and process the available ring."""
        while self.running:
            ready, _, _ = select.select([self.kick], [], [], 0.2)
            if not ready:
                continue
            try:
                os.read(self.kick, 8)
            except OSError:
                return
            with self.dev.lock:
                self.process()

    def _chain(self, head: int) -> Chain:
        """Walk the descriptor chain that starts at ``head``.

        :param head: descriptor index
        :return: the chain's buffers
        """
        mem = self.dev.mem
        readable: List[Tuple[int, int]] = []
        writable: List[Tuple[int, int]] = []
        idx = head
        for _ in range(self.num):
            addr, length, flags, nxt = struct.unpack(
                "<QIHH", mem.read(self.desc + 16 * idx, 16, True)
            )
            (writable if flags & VIRTQ_DESC_F_WRITE else readable).append(
                (addr, length)
            )
            if not flags & VIRTQ_DESC_F_NEXT:
                break
            idx = nxt
        return Chain(head, readable, writable)

    def process(self) -> None:
        """Hand every newly available chain to the device."""
        mem = self.dev.mem
        while self.running:
            (avail_idx,) = struct.unpack(
                "<H", mem.read(self.avail + 2, 2, True)
            )
            if avail_idx == self.last_avail:
                return
            slot = self.avail + 4 + 2 * (self.last_avail % self.num)
            (head,) = struct.unpack("<H", mem.read(slot, 2, True))
            self.last_avail = (self.last_avail + 1) & 0xFFFF
            self.dev.request(self, self._chain(head))

    def read(self, chain: Chain) -> bytes:
        """Return the device-readable bytes of a chain.

        :param chain: descriptor chain
        :return: data
        """
        return b"".join(self.dev.mem.read(a, n) for a, n in chain.readable)

    def push(self, chain: Chain, data: bytes) -> None:
        """Write the response into a chain and return it to the driver.

        :param chain: descriptor chain
        :param data: bytes for the device-writable buffers
        """
        mem = self.dev.mem
        rest = data
        for addr, length in chain.writable:
            if not rest:
                break
            mem.write(addr, rest[:length])
            rest = rest[length:]
        slot = self.used + 4 + 8 * (self.used_idx % self.num)
        mem.write(slot, struct.pack("<II", chain.head, len(data)), True)
        self.used_idx = (self.used_idx + 1) & 0xFFFF
        mem.write(self.used + 2, struct.pack("<H", self.used_idx), True)
        if self.call >= 0:
            os.write(self.call, struct.pack("<Q", 1))


class Device:
    """Base class of the emulated VIRTIO devices."""

    features = 0
    nqueues = 1

    def __init__(self) -> None:
        """Create the device state."""
        self.lock = threading.RLock()
        self.mem = Memory()
        self.log: List[Dict[str, Any]] = []

    def config(self) -> bytes:
        """Return the device configuration space.

        :return: configuration bytes
        """
        return b""

    def reset(self) -> None:
        """Reset the device state for a new driver."""

    def request(self, vq: Virtqueue, chain: Chain) -> None:
        """Process one request.

        :param vq: queue the request came on
        :param chain: request buffers
        """
        raise NotImplementedError

    def control(self, msg: Dict[str, Any]) -> Any:
        """Handle a control request from a test.

        :param msg: request
        :return: result
        """
        if msg["op"] == "log":
            return self.log
        if msg["op"] == "clear":
            self.log.clear()
            return None
        raise ValueError(f"unknown op {msg['op']}")


class I2cDevice(Device):
    """VIRTIO I2C adapter with 256-byte EEPROMs (AT24C02 protocol).

    A write's first byte sets the EEPROM word address and the following
    bytes are stored from there; a read returns bytes from the current
    word address on. Both advance the address, wrapping at 256.
    """

    features = 1 << 0  # VIRTIO_I2C_F_ZERO_LENGTH_REQUEST

    def __init__(self, addrs: List[int]) -> None:
        """Create the EEPROMs.

        :param addrs: 7-bit (``< 0x80``) or 10-bit (``| 0x8000``) addresses
        """
        super().__init__()
        self.eeproms = {a: bytearray(256) for a in addrs}
        self.pointer = dict.fromkeys(addrs, 0)
        self.fail_next = False

    @staticmethod
    def decode(addr: int) -> int:
        """Decode the ``addr`` field of an I2C request.

        :param addr: field value
        :return: 7-bit address, or 10-bit address ``| 0x8000``
        """
        if addr & 0xF9 == 0xF0:
            return 0x8000 | (addr >> 8) | ((addr >> 1) & 3) << 8
        return (addr >> 1) & 0x7F

    def request(self, vq: Virtqueue, chain: Chain) -> None:
        """Process one I2C request.

        :param vq: request queue
        :param chain: out header, optional buffer, in header
        """
        out = vq.read(chain)
        addr, _, flags = struct.unpack_from("<HHI", out)
        read = bool(flags & 2)
        length = chain.wlen() - 1 if read else len(out) - 8
        self.log.append(
            {
                "addr": addr,
                "flags": flags,
                "len": length,
                "readable": len(chain.readable),
                "writable": len(chain.writable),
                "data": out[8:].hex(),
            }
        )
        target = self.decode(addr)
        if self.fail_next or target not in self.eeproms:
            self.fail_next = bool(flags & 1)
            vq.push(chain, bytes(length) + b"\x01" if read else b"\x01")
            return
        self.fail_next = False
        rom, ptr = self.eeproms[target], self.pointer[target]
        if read:
            data = bytes(rom[(ptr + i) & 0xFF] for i in range(length))
            self.pointer[target] = (ptr + length) & 0xFF
            vq.push(chain, data + b"\x00")
            return
        payload = out[8:]
        if payload:
            ptr = payload[0]
            for i, byte in enumerate(payload[1:]):
                rom[(ptr + i) & 0xFF] = byte
            self.pointer[target] = (ptr + len(payload) - 1) & 0xFF
        vq.push(chain, b"\x00")

    def control(self, msg: Dict[str, Any]) -> Any:
        """Read or preset an EEPROM.

        :param msg: ``{"op": "mem", "addr": a}`` or
            ``{"op": "setmem", "addr": a, "data": hex}``
        :return: EEPROM contents as hex for ``mem``
        """
        if msg["op"] == "mem":
            return self.eeproms[msg["addr"]].hex()
        if msg["op"] == "setmem":
            self.eeproms[msg["addr"]][:] = bytes.fromhex(msg["data"])
            return None
        return super().control(msg)


class GpioLine:
    """State of one emulated GPIO line."""

    def __init__(self) -> None:
        """Start unconfigured, driven low from outside."""
        self.direction = 0
        self.value = 0
        self.level = 0
        self.irq_type = 0
        self.latched = False
        self.event: Optional[Tuple[Virtqueue, Chain]] = None


class GpioDevice(Device):
    """VIRTIO GPIO device whose input levels the test drives."""

    features = 1 << 0  # VIRTIO_GPIO_F_IRQ
    nqueues = 2

    def __init__(self, ngpio: int, names: List[str]) -> None:
        """Create the lines.

        :param ngpio: number of lines
        :param names: line names, empty for none
        """
        super().__init__()
        self.ngpio = ngpio
        self.names = b"".join(n.encode() + b"\0" for n in names)
        self.lines = [GpioLine() for _ in range(ngpio)]

    def config(self) -> bytes:
        """Return ``struct virtio_gpio_config``.

        :return: configuration bytes
        """
        return struct.pack("<HxxI", self.ngpio, len(self.names))

    def reset(self) -> None:
        """Forget all line configuration."""
        self.lines = [GpioLine() for _ in range(self.ngpio)]

    def _sensed(self, line: GpioLine) -> int:
        """Return the value sensed on a line.

        :param line: GPIO line
        :return: output value for an output, else the driven level
        """
        return line.value if line.direction == 1 else line.level

    def _fire(self, line: GpioLine, status: int) -> None:
        """Return a line's eventq buffers to the driver.

        :param line: GPIO line
        :param status: VIRTIO_GPIO_IRQ_STATUS_*
        """
        if line.event is not None:
            vq, chain = line.event
            line.event = None
            line.latched = False
            vq.push(chain, bytes([status]))

    def _level_active(self, line: GpioLine) -> bool:
        """Tell whether a level interrupt condition holds.

        :param line: GPIO line
        :return: True for a matching level
        """
        level = self._sensed(line)
        return bool(
            (line.irq_type & 4 and level) or (line.irq_type & 8 and not level)
        )

    def _request(self, kind: int, gpio: int, value: int) -> Tuple[int, int]:
        """Execute one requestq message.

        :param kind: VIRTIO_GPIO_MSG_*
        :param gpio: line number
        :param value: message value
        :return: status, value
        """
        if kind != 1 and gpio >= self.ngpio:
            return 1, 0
        line = self.lines[gpio] if kind != 1 else None
        if kind == 2:
            return 0, line.direction
        if kind == 3:
            if value == 0:
                self._fire(line, 0)
                self.lines[gpio] = GpioLine()
                self.lines[gpio].level = line.level
            else:
                line.direction = value
            return 0, 0
        if kind == 4:
            return 0, self._sensed(line)
        if kind == 5:
            line.value = value & 1
            return 0, 0
        if kind == 6:
            line.irq_type = value
            line.latched = False
            if value == 0:
                self._fire(line, 0)
            return 0, 0
        return 1, 0

    def request(self, vq: Virtqueue, chain: Chain) -> None:
        """Process a requestq message or an eventq unmask.

        :param vq: request or event queue
        :param chain: request and response buffers
        """
        data = vq.read(chain)
        if vq.index == 1:
            (gpio,) = struct.unpack_from("<H", data)
            self.log.append({"q": "event", "gpio": gpio})
            line = self.lines[gpio] if gpio < self.ngpio else None
            if line is None or line.irq_type == 0:
                vq.push(chain, b"\x00")
                return
            line.event = (vq, chain)
            if line.latched or self._level_active(line):
                self._fire(line, 1)
            return
        kind, gpio, value = struct.unpack_from("<HHI", data)
        self.log.append(
            {"q": "req", "type": kind, "gpio": gpio, "value": value}
        )
        if kind == 1:
            vq.push(chain, b"\x00" + self.names)
            return
        status, out = self._request(kind, gpio, value)
        vq.push(chain, bytes([status, out]))

    def drive(self, gpio: int, level: int) -> None:
        """Drive an input line from outside and sense interrupts.

        :param gpio: line number
        :param level: 0 or 1
        """
        line = self.lines[gpio]
        old, line.level = line.level, level
        if line.direction == 1 or old == level:
            return
        edge = 1 if level else 2
        if line.irq_type & edge:
            line.latched = True
            self._fire(line, 1)
        elif self._level_active(line):
            self._fire(line, 1)

    def control(self, msg: Dict[str, Any]) -> Any:
        """Read the lines or drive an input.

        :param msg: ``{"op": "lines"}`` or
            ``{"op": "drive", "gpio": n, "level": 0|1}``
        :return: per-line state for ``lines``
        """
        if msg["op"] == "lines":
            return [
                {
                    "direction": ln.direction,
                    "value": ln.value,
                    "irq_type": ln.irq_type,
                    "unmasked": ln.event is not None,
                }
                for ln in self.lines
            ]
        if msg["op"] == "drive":
            self.drive(msg["gpio"], msg["level"])
            return None
        return super().control(msg)


class SpiDevice(Device):
    """VIRTIO SPI controller with MISO wired to MOSI on every chip select.

    A full-duplex transfer returns its tx bytes as rx bytes, a half-duplex
    read returns ``(i * 7 + 1) & 0xff`` for byte ``i``. Every
    ``struct virtio_spi_transfer_head`` received is logged.
    """

    def __init__(self, cs_max: int) -> None:
        """Create the controller.

        :param cs_max: number of chip selects
        """
        super().__init__()
        self.cs_max = cs_max

    def config(self) -> bytes:
        """Return ``struct virtio_spi_config``.

        :return: configuration bytes
        """
        return struct.pack(
            "<BBBBIIIIIII",
            self.cs_max,  # cs_max_number
            1,  # cs_change_supported
            0,  # tx_nbits_supported: single only
            0,  # rx_nbits_supported
            0x80 | 0x8000,  # bits_per_word_mask: 8 and 16 bits
            0xF,  # mode_func_supported: CPHA/CPOL 0 and 1
            10000000,  # max_freq_hz
            0,  # max_word_delay_ns
            0,  # max_cs_setup_ns
            0,  # max_cs_hold_ns
            0,  # max_cs_inactive_ns
        )

    def request(self, vq: Virtqueue, chain: Chain) -> None:
        """Loop one transfer's tx bytes back as rx bytes.

        :param vq: request queue
        :param chain: head, tx buffer, rx buffer, result
        """
        data = vq.read(chain)
        fields = struct.unpack_from("<BBBBBxxxIIIIII", data)
        names = (
            "cs bits cs_change tx_nbits rx_nbits mode freq word_delay "
            "cs_setup cs_hold cs_inactive"
        ).split()
        head = dict(zip(names, fields))
        tx = data[32:]
        rxlen = chain.wlen() - 1
        head.update(
            tx=tx.hex(),
            rxlen=rxlen,
            readable=len(chain.readable),
            writable=len(chain.writable),
        )
        self.log.append(head)
        if head["cs"] >= self.cs_max:
            vq.push(chain, bytes(rxlen) + b"\x02")  # VIRTIO_SPI_PARAM_ERR
            return
        if tx:
            rx = (tx + bytes(rxlen))[:rxlen]
        else:
            rx = bytes((i * 7 + 1) & 0xFF for i in range(rxlen))
        vq.push(chain, rx + b"\x00")


class RpmbDevice(Device):
    """VIRTIO RPMB device with 128 KiB of 256-byte half sectors.

    Frames are ``struct virtio_rpmb_frame`` (512 bytes, big-endian
    fields); the MAC is HMAC-SHA256 with the programmed key over bytes
    228..511 of every frame of a request or response.
    """

    FRAME = 512
    BLOCK = 256

    def __init__(self) -> None:
        """Create the device with no key and write counter 0."""
        super().__init__()
        self.key: Optional[bytes] = None
        self.counter = 0
        self.data = bytearray(128 * 1024)

    def config(self) -> bytes:
        """Return ``struct virtio_rpmb_config``.

        :return: capacity 1 (128 KiB), max_wr_cnt 1, max_rd_cnt 1
        """
        return bytes([1, 1, 1])

    @staticmethod
    def fields(frame: bytes) -> Dict[str, Any]:
        """Decode the fields of one frame.

        :param frame: 512-byte frame
        :return: field values
        """
        counter, addr, count, result, kind = struct.unpack_from(
            ">IHHHH", frame, 500
        )
        return {
            "key_mac": frame[196:228].hex(),
            "data": frame[228:484].hex(),
            "nonce": frame[484:500].hex(),
            "counter": counter,
            "address": addr,
            "block_count": count,
            "result": result,
            "req_resp": kind,
        }

    def _mac(self, frames: List[bytes]) -> bytes:
        """Compute the MAC of frames.

        :param frames: 512-byte frames
        :return: HMAC-SHA256, zeros without a key
        """
        if self.key is None:
            return bytes(32)
        msg = b"".join(f[228:] for f in frames)
        return hmac.new(self.key, msg, hashlib.sha256).digest()

    def _frame(self, kind: int, result: int, **kw: Any) -> bytes:
        """Build a response frame with its MAC.

        :param kind: VIRTIO_RPMB_RESP_*
        :param result: VIRTIO_RPMB_RES_*
        :param kw: ``data``, ``nonce``, ``counter``, ``address``,
            ``block_count``
        :return: 512-byte frame
        """
        frame = bytearray(self.FRAME)
        frame[228:484] = kw.get("data", bytes(256))
        frame[484:500] = kw.get("nonce", bytes(16))
        struct.pack_into(
            ">IHHHH",
            frame,
            500,
            kw.get("counter", 0),
            kw.get("address", 0),
            kw.get("block_count", 0),
            result,
            kind,
        )
        frame[196:228] = self._mac([bytes(frame)])
        return bytes(frame)

    def _write(self, req: bytes) -> int:
        """Execute an authenticated data write.

        :param req: request frame
        :return: VIRTIO_RPMB_RES_*
        """
        f = self.fields(req)
        nblocks = len(self.data) // self.BLOCK
        if self.key is None:
            return 0x0007  # NO_AUTH_KEY
        if f["block_count"] != 1:
            return 0x0001  # GENERAL_FAILURE
        if f["address"] >= nblocks:
            return 0x0004  # ADDR_FAILURE
        if self._mac([req]) != req[196:228]:
            return 0x0002  # AUTH_FAILURE
        if f["counter"] != self.counter:
            return 0x0003  # COUNT_FAILURE
        off = f["address"] * self.BLOCK
        self.data[off : off + self.BLOCK] = req[228:484]
        self.counter += 1
        return 0

    def _respond(self, frames: List[bytes]) -> bytes:
        """Execute a request and return the response frames.

        :param frames: device-readable frames
        :return: device-writable bytes
        """
        req = frames[0]
        f = self.fields(req)
        kind = f["req_resp"]
        if kind == 0x0001:
            ok = self.key is None and f["block_count"] == 1
            if ok:
                self.key = req[196:228]
            return self._frame(0x0100, 0 if ok else 0x0005)
        if kind == 0x0002:
            result = 0x0007 if self.key is None else 0
            nonce = req[484:500]
            return self._frame(
                0x0200, result, counter=self.counter, nonce=nonce
            )
        if kind == 0x0003:
            result = self._write(req)
            return self._frame(
                0x0300, result, counter=self.counter, address=f["address"]
            )
        if kind == 0x0004:
            off = f["address"] * self.BLOCK
            ok = self.key is not None and off < len(self.data)
            return self._frame(
                0x0400,
                0 if ok else 0x0007,
                data=(
                    bytes(self.data[off : off + self.BLOCK])
                    if ok
                    else bytes(self.BLOCK)
                ),
                nonce=req[484:500],
                address=f["address"],
                block_count=1,
            )
        return self._frame(kind << 8, 0x0001)

    def request(self, vq: Virtqueue, chain: Chain) -> None:
        """Process one request: readable frames, one writable frame.

        :param vq: request queue
        :param chain: request frames and response buffer
        """
        data = vq.read(chain)
        frames = [
            data[i : i + self.FRAME] for i in range(0, len(data), self.FRAME)
        ]
        self.log.append(
            {
                "readable": len(chain.readable),
                "writable": len(chain.writable),
                "wlen": chain.wlen(),
                "frames": [self.fields(f) for f in frames],
            }
        )
        resp = self._respond(frames) if frames else b""
        vq.push(chain, resp[: chain.wlen()])

    def control(self, msg: Dict[str, Any]) -> Any:
        """Read the device state, or replace it by a factory-new device.

        :param msg: ``{"op": "state", "address": n}`` or
            ``{"op": "factory"}``
        :return: key, write counter and data of that half sector for
            ``state``
        """
        if msg["op"] == "factory":
            self.key = None
            self.counter = 0
            self.data[:] = bytes(len(self.data))
            return None
        if msg["op"] == "state":
            off = msg["address"] * self.BLOCK
            return {
                "key": self.key.hex() if self.key else None,
                "counter": self.counter,
                "data": self.data[off : off + self.BLOCK].hex(),
            }
        return super().control(msg)


class Backend:
    """vhost-user back-end serving one device on one connection."""

    def __init__(self, dev: Device) -> None:
        """Create the back-end.

        :param dev: emulated device
        """
        self.dev = dev
        self.vqs = [Virtqueue(i, dev) for i in range(dev.nqueues)]

    def _reply(self, conn: socket.socket, req: int, payload: bytes) -> None:
        """Send a reply.

        :param conn: vhost-user connection
        :param req: request type
        :param payload: reply payload
        """
        hdr = struct.pack("<III", req, FLAG_VERSION | FLAG_REPLY, len(payload))
        conn.sendall(hdr + payload)

    def _vring(self, req: int, payload: bytes, fds: List[int]) -> None:
        """Handle the ring set-up requests.

        :param req: request type
        :param payload: message payload
        :param fds: received file descriptors
        """
        if req in (SET_VRING_KICK, SET_VRING_CALL, SET_VRING_ERR):
            (val,) = struct.unpack_from("<Q", payload)
            vq = self.vqs[val & 0xFF]
            fd = -1 if val & VRING_NOFD or not fds else fds[0]
            if req == SET_VRING_KICK:
                vq.kick = fd
                vq.start()
            elif req == SET_VRING_CALL:
                vq.call = fd
            elif fd >= 0:
                os.close(fd)
            return
        index, num = struct.unpack_from("<II", payload)
        vq = self.vqs[index]
        if req == SET_VRING_NUM:
            vq.num = num
        elif req == SET_VRING_BASE:
            vq.last_avail = vq.used_idx = num
        elif req == SET_VRING_ADDR:
            vq.desc, vq.used, vq.avail = struct.unpack_from("<QQQ", payload, 8)

    def _handle(
        self, conn: socket.socket, req: int, payload: bytes, fds: List[int]
    ) -> None:
        """Handle one front-end request.

        :param conn: vhost-user connection
        :param req: request type
        :param payload: message payload
        :param fds: received file descriptors
        """
        dev = self.dev
        if req == GET_FEATURES:
            feat = VIRTIO_F_VERSION_1 | F_PROTOCOL_FEATURES | dev.features
            self._reply(conn, req, struct.pack("<Q", feat))
        elif req == GET_PROTOCOL_FEATURES:
            feat = PROTOCOL_F_MQ | PROTOCOL_F_CONFIG
            self._reply(conn, req, struct.pack("<Q", feat))
        elif req == GET_QUEUE_NUM:
            self._reply(conn, req, struct.pack("<Q", dev.nqueues))
        elif req == GET_CONFIG:
            off, size, flags = struct.unpack_from("<III", payload)
            cfg = (dev.config() + bytes(off + size))[off : off + size]
            self._reply(conn, req, struct.pack("<III", off, size, flags) + cfg)
        elif req == SET_MEM_TABLE:
            with dev.lock:
                dev.mem.set_table(payload, fds)
        elif req == GET_VRING_BASE:
            (index,) = struct.unpack_from("<I", payload)
            vq = self.vqs[index]
            vq.stop()
            with dev.lock:
                dev.reset()
            self._reply(conn, req, struct.pack("<II", index, vq.last_avail))
        elif req in (
            SET_VRING_NUM,
            SET_VRING_ADDR,
            SET_VRING_BASE,
            SET_VRING_KICK,
            SET_VRING_CALL,
            SET_VRING_ERR,
        ):
            self._vring(req, payload, fds)

    def serve(self, conn: socket.socket) -> None:
        """Handle requests until QEMU closes the connection.

        :param conn: vhost-user connection
        """
        fdsize = socket.CMSG_LEN(16 * array.array("i").itemsize)
        while True:
            hdr, anc, _, _ = conn.recvmsg(12, fdsize)
            if len(hdr) < 12:
                break
            fds: List[int] = []
            for level, kind, data in anc:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    arr = array.array("i")
                    arr.frombytes(data[: len(data) - len(data) % arr.itemsize])
                    fds.extend(arr)
            req, _, size = struct.unpack("<III", hdr)
            payload = b""
            while len(payload) < size:
                chunk = conn.recv(size - len(payload))
                if not chunk:
                    return
                payload += chunk
            self._handle(conn, req, payload, fds)
        for vq in self.vqs:
            vq.stop()


def _control(dev: Device, path: str) -> None:
    """Serve test control requests on a unix socket.

    :param dev: emulated device
    :param path: control socket path
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        with conn, conn.makefile("rw") as f:
            for line in f:
                try:
                    with dev.lock:
                        result = {"result": dev.control(json.loads(line))}
                except Exception as exc:  # noqa: B902 - report to the test
                    result = {"error": repr(exc)}
                f.write(json.dumps(result) + "\n")
                f.flush()


def _make_device(args: argparse.Namespace) -> Device:
    """Create the device named on the command line.

    :param args: parsed arguments
    :return: emulated device
    """
    if args.device == "i2c":
        return I2cDevice([0x50, 0x8000 | 0x2A5])
    if args.device == "gpio":
        return GpioDevice(8, [f"tl-line{i}" for i in range(8)])
    if args.device == "rpmb":
        return RpmbDevice()
    return SpiDevice(2)


def main() -> int:
    """Run QEMU with an emulated vhost-user device.

    :return: QEMU exit status
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("i2c", "gpio", "spi", "rpmb"))
    parser.add_argument("--socket", required=True)
    parser.add_argument("--ctl", required=True)
    parser.add_argument("qemu", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    for path in (args.socket, args.ctl):
        if os.path.exists(path):
            os.unlink(path)
    dev = _make_device(args)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.socket)
    srv.listen(1)
    threading.Thread(
        target=_control, args=(dev, args.ctl), daemon=True
    ).start()
    qemu = args.qemu[1:] if args.qemu[:1] == ["--"] else args.qemu
    child = subprocess.Popen(qemu)  # noqa: S603 - the product's QEMU

    def accept() -> None:
        """Serve QEMU's connections (a device reset reconnects)."""
        while True:
            conn, _ = srv.accept()
            with conn:
                Backend(dev).serve(conn)

    threading.Thread(target=accept, daemon=True).start()
    return child.wait()


class VhostCtl:
    """Test-side client of the back-end control socket."""

    def __init__(self) -> None:
        """Connect to the ``--ctl`` socket of the product's wrapper."""
        import pytest  # the wrapper runs without pytest

        args = str(pytest.product.core(0).conf.exec_args)
        found = re.search(r"--ctl\s+(\S+)", args)
        assert found, f"no --ctl in {args!r}"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(TIMEOUT)
        self.sock.connect(found.group(1))
        self.file = self.sock.makefile("rw")

    def __call__(self, op: str, **kwargs: Any) -> Any:
        """Run one control request.

        :param op: request name
        :param kwargs: request arguments
        :return: result
        """
        self.file.write(json.dumps(dict(op=op, **kwargs)) + "\n")
        self.file.flush()
        reply = json.loads(self.file.readline())
        assert "error" not in reply, reply
        return reply["result"]

    def close(self) -> None:
        """Close the control connection."""
        self.file.close()
        self.sock.close()


if __name__ == "__main__":
    sys.exit(main())
