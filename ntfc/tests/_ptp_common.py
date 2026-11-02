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
"""PTP helpers: linuxptp on the host, a PTP sniffer and ``ptptl``.

The node runs ``netutils/ptpd`` through the in-repo ``ptptl`` app, which
prints one ``ptptl: status ...`` line per second and a verdict line at
the end. The host runs ``ptp4l`` (always ``free_running``, so the host
clock is never touched) and reads its data sets with ``pmc``.

The sniffer decodes the PTP message header (IEEE 1588-2008 13.3.1,
Table 18), the Timestamp type (5.3.3: 48-bit seconds, 32-bit
nanoseconds), the Announce body (13.5.2, Table 25) and the
requestingPortIdentity of Delay_Resp (13.8.2, Table 29).
"""

import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, List, Optional, Tuple, Type

from _net_common import _core

BRIDGE = "tl-br0"
PTP_IFACE = (
    "tl-ptp0"  # veth of testenv/ip-pair.sh; the bridge has no TX timestamps
)

PTP_UDP_EVENT = 319  # 1588 Annex D.3: event messages
PTP_UDP_GENERAL = 320  # 1588 Annex D.3: general messages
PTP_MCAST = "224.0.1.129"  # 1588 Annex D.3 Table D.1
PTP_PDELAY_MCAST = "224.0.0.107"  # 1588 Annex D.3 Table D.1
PTP_ETHERTYPE = 0x88F7  # 1588 Annex F.2
PTP_PDELAY_MAC = "01:80:c2:00:00:0e"  # 1588 Annex F.3 Table F.3
ETH_P_IP = 0x0800

MSG_SYNC = 0
MSG_DELAY_REQ = 1
MSG_PDELAY_REQ = 2
MSG_PDELAY_RESP = 3
MSG_FOLLOW_UP = 8
MSG_DELAY_RESP = 9
MSG_PDELAY_RESP_FUP = 10
MSG_ANNOUNCE = 11

FLAG_TWO_STEP = 0x0200  # 1588 13.3.2.6 Table 20: octet 0 bit 1

HEADER_LEN = 34
TIMESTAMP_LEN = 10

STATUS_RE = re.compile(
    r"ptptl: status t=(?P<t>\d+) valid=(?P<valid>\d) gm=(?P<gm>[0-9a-f]{16}) "
    r"p1=(?P<p1>\d+) class=(?P<cls>\d+) steps=(?P<steps>\d+) "
    r"delta=(?P<delta>-?\d+) adj=(?P<adj>-?\d+) drift=(?P<drift>-?\d+) "
    r"path=(?P<path>-?\d+) ann=(?P<ann>-?\d+) sync=(?P<sync>-?\d+) "
    r"dreq=(?P<dreq>-?\d+) pdreq=(?P<pdreq>-?\d+) txsync=(?P<txsync>-?\d+) "
    r"txann=(?P<txann>-?\d+) now=(?P<sec>\d+)\.(?P<nsec>\d{9})"
)
VERDICT_RE = (
    r"ptptl: (PASS|FAIL) synced=(\d) first=(-?\d+) stop=(-?\d+) exit=(\d+)"
)
_MAC_RE = re.compile(r"HWaddr ([0-9a-fA-F:]{17})")

_RUNNING: List[Tuple[int, float]] = []


def clock_identity(mac: str) -> str:
    """Return the clockIdentity derived from a MAC address.

    IEEE 1588-2008 7.5.2.2.2: an EUI-48 is mapped to the EUI-64
    clockIdentity by inserting ``0xFF 0xFE`` after its third octet.

    :param mac: ``aa:bb:cc:dd:ee:ff``
    :return: 16 lowercase hex digits
    """
    octets = mac.lower().split(":")
    return "".join(octets[:3]) + "fffe" + "".join(octets[3:])


def node_mac(node: int, dev: str = "eth0") -> str:
    """Return the MAC address of a node interface.

    :param node: product index
    :param dev: interface name
    :return: ``aa:bb:cc:dd:ee:ff`` in lower case
    """
    ret = _core(node).sendCommandReadUntilPattern(
        f"ifconfig {dev}", pattern=r"HWaddr [0-9a-fA-F:]{17}", timeout=10
    )
    found = _MAC_RE.search(str(ret.output))
    assert found, f"no HWaddr in: {ret.output}"
    return found.group(1).lower()


@dataclass
class PtpMsg:
    """One decoded PTP message seen on the bridge."""

    t: float  # host CLOCK_REALTIME at reception
    src_mac: str
    l2: bool  # IEEE 802.3 transport (EtherType 0x88F7)
    dst_port: Optional[int]  # UDP destination port, None for L2
    msgtype: int
    version: int
    domain: int
    flags: int
    correction: int
    clock_id: str  # sourcePortIdentity.clockIdentity
    port_num: int  # sourcePortIdentity.portNumber
    seq: int
    control: int
    log_interval: int
    origin: Optional[float]  # first body Timestamp in seconds
    announce: Optional[Dict[str, int]]
    req_id: Optional[str]  # requestingPortIdentity.clockIdentity


def _timestamp(data: bytes) -> float:
    """Decode a 1588 5.3.3 Timestamp.

    :param data: 10 bytes
    :return: seconds
    """
    hi, lo, nsec = struct.unpack("!HIi", data[:TIMESTAMP_LEN])
    return (hi << 32 | lo) + nsec / 1e9


def _announce(body: bytes) -> Dict[str, int]:
    """Decode the Announce body after originTimestamp (1588 Table 25).

    :param body: 20 bytes starting at currentUtcOffset
    :return: field values
    """
    (
        utc_offset,
        priority1,
        clock_class,
        accuracy,
        variance,
        priority2,
        gm_id,
        steps_removed,
        time_source,
    ) = struct.unpack("!hxBBBHB8sHB", body[:20])
    return {
        "utc_offset": utc_offset,
        "priority1": priority1,
        "clock_class": clock_class,
        "accuracy": accuracy,
        "variance": variance,
        "priority2": priority2,
        "gm_id": gm_id.hex(),
        "steps_removed": steps_removed,
        "time_source": time_source,
    }


def decode(frame: bytes, t: float) -> Optional[PtpMsg]:
    """Decode a PTP message from an Ethernet frame.

    :param frame: raw frame
    :param t: reception time
    :return: the message, or None for other traffic
    """
    if len(frame) < 14 + HEADER_LEN:
        return None
    (etype,) = struct.unpack("!H", frame[12:14])
    src_mac = ":".join(f"{b:02x}" for b in frame[6:12])
    dst_port: Optional[int] = None
    if etype == PTP_ETHERTYPE:
        ptp = frame[14:]
    elif etype == ETH_P_IP and frame[23] == 17:
        ihl = (frame[14] & 0x0F) * 4
        udp = 14 + ihl
        (dst_port,) = struct.unpack("!H", frame[udp + 2 : udp + 4])
        if dst_port not in (PTP_UDP_EVENT, PTP_UDP_GENERAL):
            return None
        ptp = frame[udp + 8 :]
    else:
        return None
    if len(ptp) < HEADER_LEN:
        return None
    return _message(ptp, t, src_mac, etype == PTP_ETHERTYPE, dst_port)


def _message(
    ptp: bytes, t: float, src_mac: str, l2: bool, dst_port: Optional[int]
) -> PtpMsg:
    """Decode the common header and the per-type body fields.

    :param ptp: PTP message bytes
    :param t: reception time
    :param src_mac: Ethernet source
    :param l2: 802.3 transport
    :param dst_port: UDP destination port or None
    :return: decoded message
    """
    (
        type_byte,
        ver_byte,
        _length,
        domain,
        flags,
        correction,
        clock_id,
        port_num,
        seq,
        control,
        log_interval,
    ) = struct.unpack("!BBHBxHq4x8sHHBb", ptp[:HEADER_LEN])
    msgtype = type_byte & 0x0F
    body = ptp[HEADER_LEN:]
    origin = _timestamp(body) if len(body) >= TIMESTAMP_LEN else None
    announce = None
    req_id = None
    if msgtype == MSG_ANNOUNCE and len(body) >= 30:
        announce = _announce(body[TIMESTAMP_LEN:])
    if msgtype in (MSG_DELAY_RESP, MSG_PDELAY_RESP, MSG_PDELAY_RESP_FUP):
        if len(body) >= 20:
            req_id = body[TIMESTAMP_LEN : TIMESTAMP_LEN + 8].hex()
    return PtpMsg(
        t=t,
        src_mac=src_mac,
        l2=l2,
        dst_port=dst_port,
        msgtype=msgtype,
        version=ver_byte & 0x0F,
        domain=domain,
        flags=flags,
        correction=correction,
        clock_id=clock_id.hex(),
        port_num=port_num,
        seq=seq,
        control=control,
        log_interval=log_interval,
        origin=origin,
        announce=announce,
        req_id=req_id,
    )


class PtpSniffer:
    """Record every PTP message on the bridge, both directions."""

    def __init__(self, iface: str = BRIDGE) -> None:
        """Open the capture socket.

        :param iface: interface to capture on
        """
        self.iface = iface
        self.msgs: List[PtpMsg] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)
        )
        self._sock.bind((iface, 0))
        self._sock.settimeout(0.1)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "PtpSniffer":
        """Start capturing."""
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Stop capturing."""
        self._stop.set()
        self._thread.join(2)
        self._sock.close()

    def _loop(self) -> None:
        """Receive frames until stopped."""
        while not self._stop.is_set():
            try:
                frame = self._sock.recv(2048)
            except socket.timeout:
                continue
            msg = decode(frame, time.time())
            if msg:
                with self._lock:
                    self.msgs.append(msg)

    def messages(
        self, msgtype: Optional[int] = None, src: Optional[str] = None
    ) -> List[PtpMsg]:
        """Return the captured messages, optionally filtered.

        :param msgtype: message type to keep
        :param src: source MAC to keep
        :return: messages in capture order
        """
        with self._lock:
            msgs = list(self.msgs)
        return [
            m
            for m in msgs
            if (msgtype is None or m.msgtype == msgtype)
            and (src is None or m.src_mac == src)
        ]

    def wait(
        self,
        msgtype: int,
        count: int,
        timeout: float,
        src: Optional[str] = None,
    ) -> List[PtpMsg]:
        """Wait until ``count`` messages of a type were captured.

        :param msgtype: message type
        :param count: number of messages
        :param timeout: seconds
        :param src: source MAC filter
        :return: the messages seen so far (may be fewer on timeout)
        """
        deadline = time.monotonic() + timeout
        msgs = self.messages(msgtype, src)
        while len(msgs) < count and time.monotonic() < deadline:
            time.sleep(0.2)
            msgs = self.messages(msgtype, src)
        return msgs


def offsets_ms(msgs: List[PtpMsg]) -> List[float]:
    """Return host time minus the originTimestamp of each message, in ms.

    The originTimestamp of Sync, Follow_Up and Delay_Req is the sender's
    clock at transmission (1588 13.6.2, 13.7.2, 13.8.1), so on a local
    link this is the sender's clock offset from the host, within the
    one-way delay.

    :param msgs: messages carrying an originTimestamp
    :return: offsets in milliseconds
    """
    return [(m.t - m.origin) * 1000 for m in msgs if m.origin is not None]


class Ptp4l:
    """``ptp4l`` on the bridge with software timestamps, free running.

    ``free_running 1`` keeps ptp4l from touching the host clock in any
    state; as a master it still serves its clock, as a slave it still
    measures the offset to the master.
    """

    def __init__(
        self,
        role: str = "master",
        priority1: int = 128,
        delay: str = "e2e",
        transport: str = "udp",
        iface: str = PTP_IFACE,
    ) -> None:
        """Write the configuration file.

        :param role: ``master`` (BMCA, may lose to a better clock) or
         ``slave`` (``clientOnly 1``)
        :param priority1: announced priority1
        :param delay: ``e2e`` or ``p2p``
        :param transport: ``udp`` (UDPv4) or ``l2`` (IEEE 802.3)
        :param iface: interface
        """
        self.iface = iface
        self._dir = tempfile.TemporaryDirectory(prefix="tl-ptp-")
        base = Path(self._dir.name)
        self.log = base / "ptp4l.log"
        self.uds = base / "uds"
        self._cfg = base / "ptp4l.conf"
        self._cfg.write_text(
            "[global]\n"
            "time_stamping software\n"
            f"network_transport {'L2' if transport == 'l2' else 'UDPv4'}\n"
            f"delay_mechanism {delay.upper()}\n"
            f"priority1 {priority1}\n"
            f"clientOnly {1 if role == 'slave' else 0}\n"
            "free_running 1\n"
            "logAnnounceInterval 1\n"
            "logSyncInterval 0\n"
            "logMinDelayReqInterval 0\n"
            "logMinPdelayReqInterval 0\n"
            "announceReceiptTimeout 3\n"
            "tx_timestamp_timeout 50\n"
            f"uds_address {self.uds}\n"
        )
        self._proc: Optional[subprocess.Popen[bytes]] = None

    def __enter__(self) -> "Ptp4l":
        """Start ptp4l."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Stop ptp4l."""
        self.stop()

    def start(self) -> None:
        """Start ptp4l and wait for its management socket."""
        self._proc = subprocess.Popen(  # noqa: S603
            [  # noqa: S607
                "ptp4l",
                "-f",
                str(self._cfg),
                "-i",
                self.iface,
                "-m",
                "-q",
            ],
            stdout=self.log.open("wb"),
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.uds.exists():
                return
            if self._proc.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError(f"ptp4l did not start: {self.text()}")

    def stop(self) -> None:
        """Stop ptp4l."""
        if self._proc:
            self._proc.terminate()
            self._proc.wait(10)
            self._proc = None

    def text(self) -> str:
        """Return the ptp4l log.

        :return: log text
        """
        return self.log.read_text(errors="replace")

    def pmc(self, dataset: str) -> Dict[str, str]:
        """Query one management data set of the local ptp4l.

        :param dataset: e.g. ``PORT_DATA_SET``
        :return: field name to value, from the first response
        """
        out = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "pmc",
                "-u",
                "-b",
                "0",
                "-f",
                str(self._cfg),
                f"GET {dataset}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        fields: Dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if line.startswith("\t") and len(parts) == 2:
                fields.setdefault(parts[0], parts[1])
        assert fields, f"pmc GET {dataset}: {out!r}"
        return fields

    def port_state(self) -> str:
        """Return the portState of the ptp4l port.

        :return: e.g. ``MASTER``, ``SLAVE``, ``LISTENING``
        """
        return self.pmc("PORT_DATA_SET")["portState"]


def status_fields(line: "re.Match[str]") -> Dict[str, Any]:
    """Convert a matched status line to typed fields.

    :param line: :data:`STATUS_RE` match
    :return: integer fields and ``now`` as float seconds
    """
    fields: Dict[str, Any] = {
        k: int(v) for k, v in line.groupdict().items() if k != "gm"
    }
    fields["gm"] = line.group("gm")
    fields["now"] = fields["sec"] + fields["nsec"] / 1e9
    return fields


def ptptl_start(node: int, args: str, runtime: int = 30) -> float:
    """Start ``ptptl`` in the background on a node.

    :param node: product index
    :param args: options, without ``-t``
    :param runtime: seconds the app runs (``-t``)
    :return: host ``time.monotonic()`` when the daemon started; the
     ``t`` of a status line counts seconds from this moment
    """
    ret = _core(node).sendCommand(
        f"ptptl {args} -t {runtime} &", "ptptl: started", timeout=10
    )
    started = time.monotonic()
    assert ret == 0, f"ptptl did not start on node{node}"
    _RUNNING.append((node, started + runtime + 15))
    return started


def ptptl_wait(
    node: int, timeout: float
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Wait for the verdict line of the ``ptptl`` run on a node.

    :param node: product index
    :param timeout: seconds
    :return: verdict fields (``verdict``, ``synced``, ``first``, ``stop``,
     ``exit``) and every status line as typed fields
    """
    ret = _core(node).readUntilPattern(VERDICT_RE, timeout=timeout)
    _RUNNING[:] = [r for r in _RUNNING if r[0] != node]
    out = str(ret.output)
    found = re.search(VERDICT_RE, out)
    assert found, f"no ptptl verdict on node{node}: {out}"
    verdict = {
        "verdict": found.group(1),
        "synced": int(found.group(2)),
        "first": int(found.group(3)),
        "stop": int(found.group(4)),
        "exit": int(found.group(5)),
    }
    status = [status_fields(m) for m in STATUS_RE.finditer(out)]
    print(f"node{node} ptptl: {verdict}, {len(status)} status lines")
    return verdict, status


def ptptl_cleanup() -> None:
    """Wait for ``ptptl`` runs a failed test left behind."""
    while _RUNNING:
        node, deadline = _RUNNING.pop()
        remaining = max(deadline - time.monotonic(), 1)
        try:
            _core(node).readUntilPattern(VERDICT_RE, timeout=remaining)
        except Exception:  # noqa: BLE001  # best effort
            pass
