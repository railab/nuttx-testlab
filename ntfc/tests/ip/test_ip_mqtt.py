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

"""MQTT: NuttX clients against a mosquitto broker on the host.

Paho MQTT C (netutils/paho_mqtt): ``mqtt_pub`` publishes one message and
exits; ``mqtt_sub`` runs in the background until SIGINT and, with
``-v``, prints ``<len> <topic>`` + TAB + payload per message.

MQTT-C (netutils/mqttc, examples/mqttc): ``mqttc_pub`` publishes ``-n``
messages 5 s apart; ``mqttc_sub`` prints ``Received: <payload>``,
reconnects and resubscribes after a lost connection, and exits on
``q`` from the console.

The broker log shows the flags of every PUBLISH it receives, e.g.
``Received PUBLISH from n0p (d0, q1, r0, m1, 'tl/q1', ... (5 bytes))``.
"""

import re
import subprocess
import time
from types import TracebackType
from typing import Any, Iterator, List, Optional, Tuple, Type

import pytest
from _host_services import MosquittoBroker
from _net_common import HOST_IP

pytestmark = [pytest.mark.dep_config("CONFIG_NET_TCP")]

MQTTC_PUB_CMAKE_BUG = (
    "examples/mqttc: CMakeLists.txt names the publisher "
    "CONFIG_EXAMPLES_MQTTC_PUB_PROGNAME, which Kconfig does not define "
    "(it is EXAMPLES_MQTTC_PROGNAME), so the CMake build has no mqttc_pub"
)

PAHO_PUB_OPTS_BUG = (
    "netutils/paho_mqtt: paho_c_pub.c keeps its options in a global "
    "struct that getopts() only sets, never resets; NuttX does not "
    "re-initialise .data between runs of a builtin, so -r sticks"
)

EXIT_SIGTERM_BUG = (
    "sched/group: with GROUP_KILL_CHILDREN_TIMEOUT_MS < 0 (default with "
    "SIG_DEFAULT) exit() sends SIGTERM to the other threads and waits "
    "forever for them; mqtt_pub handles SIGTERM, so they never exit"
)

HOST_SUB = "tl-hsub"
HOST_PUB = "tl-hpub"
TIMEOUT = 20
TCP_ROOM = 4


def _core(node: int) -> Any:
    """Return the NTFC core handler of a node.

    :param node: product index
    :return: ``ProductCore`` for the product's core 0
    """
    return pytest.products[node].core(0)


def _run(node: int, cmd: str, timeout: int = 30) -> str:
    """Run a foreground command on a node until the prompt returns.

    :param node: product index
    :param cmd: NSH command line
    :param timeout: seconds to wait for the prompt
    :return: command output
    """
    ret = _core(node).sendCommandReadUntilPattern(
        cmd, pattern=r"nsh> ", timeout=timeout
    )
    return str(ret.output)


def _published(client: str, topic: str, qos: int, retain: bool) -> str:
    """Return the broker log pattern of a received PUBLISH.

    :param client: client id
    :param topic: topic name
    :param qos: QoS level
    :param retain: retain flag
    :return: regex
    """
    return (
        rf"Received PUBLISH from {client} \(d0, q{qos}, r{int(retain)}, "
        rf"m\d+, '{re.escape(topic)}'"
    )


@pytest.fixture(scope="module")
def broker() -> Iterator[MosquittoBroker]:
    """Run mosquitto on the host bridge for this module."""
    with MosquittoBroker(HOST_IP) as mosquitto:
        yield mosquitto


def _tcp_free(node: int) -> int:
    """Return the number of free TCP connections of a node.

    :param node: product index
    :return: ``CONFIG_NET_TCP_PREALLOC_CONNS`` minus ``/proc/net/tcp`` rows
    """
    total = int(_core(node).conf.kv_check("CONFIG_NET_TCP_PREALLOC_CONNS"))
    out = _run(node, "cat /proc/net/tcp", timeout=10)
    return total - len(re.findall(r"^ *\d+: ", out, re.MULTILINE))


@pytest.fixture(autouse=True)
def tcp_room() -> None:
    """Wait until both nodes have free TCP connections.

    Every MQTT session is a short TCP connection; closed ones stay in
    TIME_WAIT for 2 minutes and are not recycled while
    ``CONFIG_NET_SOLINGER`` is set, so earlier tests may have used up
    the preallocated pool.
    """
    for node in (0, 1):
        deadline = time.monotonic() + 130
        while _tcp_free(node) < TCP_ROOM and time.monotonic() < deadline:
            time.sleep(5)


class HostSub:
    """``mosquitto_sub`` on the host printing ``<qos> <retain> <payload>``.

    :param broker: broker fixture
    :param topic: topic filter
    :param count: messages to wait for
    """

    def __init__(
        self, broker: MosquittoBroker, topic: str, count: int
    ) -> None:
        """Subscribe at QoS 2 and wait for the SUBACK.

        :param broker: broker fixture
        :param topic: topic filter
        :param count: messages to wait for
        """
        mark = broker.mark()
        self.proc = subprocess.Popen(  # noqa: S603
            [  # noqa: S607
                "mosquitto_sub",
                *("-h", HOST_IP, "-i", HOST_SUB, "-t", topic, "-q", "2"),
                *("-C", str(count), "-W", str(TIMEOUT), "-F", "%q %r %p"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert broker.wait(rf"Sending SUBACK to {HOST_SUB}$", mark)

    def messages(self) -> List[str]:
        """Wait for the subscriber to exit.

        :return: received messages, one ``<qos> <retain> <payload>`` each
        """
        out, _ = self.proc.communicate(timeout=TIMEOUT + 10)
        return out.splitlines()

    def __enter__(self) -> "HostSub":
        """Return the subscriber.

        :return: this subscriber
        """
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        """Kill the subscriber if it is still running.

        :param exc_type: exception type, if any
        :param exc: exception, if any
        :param tb: traceback, if any
        """
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.communicate()


def _host_pub(topic: str, msg: str, qos: int, retain: bool = False) -> None:
    """Publish one message from the host.

    :param topic: topic name
    :param msg: payload, empty for a zero-length message
    :param qos: QoS level
    :param retain: set the retain flag
    """
    payload = ["-m", msg] if msg else ["-n"]
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "mosquitto_pub",
            *("-h", HOST_IP, "-i", HOST_PUB, "-t", topic, "-q", str(qos)),
            *payload,
            *(["-r"] if retain else []),
        ],
        check=True,
        timeout=TIMEOUT,
    )


def _start_bg(node: int, cmd: str, pattern: str) -> Tuple[int, str]:
    """Start a command in the background and read until a pattern.

    :param node: product index
    :param cmd: NSH command line, without ``&``
    :param pattern: output pattern to wait for
    :return: PID of the task and the output read
    """
    out = str(
        _core(node)
        .sendCommandReadUntilPattern(
            f"{cmd} &", pattern=pattern, timeout=TIMEOUT
        )
        .output
    )
    found = re.search(rf"{cmd.split()[0]} \[(\d+):", out)
    assert found, out
    return int(found.group(1)), out


def _wait_exit(node: int, pid: int, timeout: float = 15) -> bool:
    """Wait for a task to exit; kill it if it does not.

    :param node: product index
    :param pid: task PID
    :param timeout: seconds to wait
    :return: True when the task exited on its own
    """
    core = _core(node)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if core.sendCommand(f"kill -0 {pid}", "no such task", timeout=2) == 0:
            return True
        time.sleep(0.5)
    core.sendCommand(f"kill -9 {pid}", timeout=5)
    return False


def _paho_pub(
    node: int, topic: str, msg: str, qos: int, retain: bool = False
) -> str:
    """Publish one message with ``mqtt_pub`` on a node.

    Runs in the background: after a failed connect ``mqtt_pub`` waits
    forever.

    :param node: product index
    :param topic: topic name
    :param msg: payload (no spaces)
    :param qos: QoS level
    :param retain: set the retain flag
    :return: command output
    """
    flags = " -r" if retain else ""
    pid, out = _start_bg(
        node,
        f"mqtt_pub -h {HOST_IP} -i n{node}p -t {topic} -q {qos}{flags} "
        f"-v -m {msg}",
        r"Publish succeeded|failed",
    )
    exited = _wait_exit(node, pid)
    assert "Publish succeeded" in out, out
    assert exited, "mqtt_pub did not exit"
    return out


def _paho_sub_start(
    broker: MosquittoBroker,
    node: int,
    topic: str,
    qos: int,
    expect: str = "",
) -> int:
    """Start ``mqtt_sub`` in the background and wait for its SUBACK.

    :param broker: broker fixture
    :param node: product index
    :param topic: topic filter
    :param qos: maximum QoS
    :param expect: payload of a retained message expected on subscribe
    :return: PID of the subscriber task
    """
    mark = broker.mark()
    pattern = _paho_msg_re(topic, expect) if expect else r"mqtt_sub \[\d+:"
    pid, out = _start_bg(
        node,
        f"mqtt_sub -h {HOST_IP} -i n{node}s -t {topic} -q {qos} -v",
        pattern,
    )
    assert broker.wait(rf"Sending SUBACK to n{node}s$", mark), out
    assert re.search(pattern, out), out
    return pid


def _paho_sub_stop(broker: MosquittoBroker, node: int, pid: int) -> bool:
    """Stop ``mqtt_sub`` with SIGINT.

    :param broker: broker fixture
    :param node: product index
    :param pid: PID of the subscriber task
    :return: True when the subscriber sent DISCONNECT and exited
    """
    mark = broker.mark()
    _core(node).sendCommand(f"kill -2 {pid}", timeout=10)
    sent = broker.wait(rf"Received DISCONNECT from n{node}s$", mark)
    return _wait_exit(node, pid) and bool(sent)


def _paho_msg_re(topic: str, msg: str) -> str:
    """Return the ``mqtt_sub -v`` output pattern of a message.

    :param topic: topic name
    :param msg: payload
    :return: regex
    """
    return rf"{len(msg)} {re.escape(topic)}\t{re.escape(msg)}"


def _received(node: int, regex: str) -> bool:
    """Wait for a background subscriber on a node to print a message.

    :param node: product index
    :param regex: message pattern
    :return: True when the message was printed
    """
    ret = _core(node).readUntilPattern(regex, timeout=TIMEOUT)
    return re.search(regex, ret.output) is not None


@pytest.mark.cmd_check("mqtt_pub_main")
@pytest.mark.parametrize("qos", [0, 1, 2])
def test_mqtt_pub(broker: MosquittoBroker, qos: int) -> None:
    """``mqtt_pub`` delivers one message at the given QoS to the host.

    :param broker: broker fixture
    :param qos: QoS level
    """
    topic, msg = f"tl/q{qos}", f"paho-qos{qos}-payload"
    with HostSub(broker, topic, 1) as sub:
        mark = broker.mark()
        _paho_pub(0, topic, msg, qos)
        assert sub.messages() == [f"{qos} 0 {msg}"]
    assert broker.wait(_published("n0p", topic, qos, False), mark)


@pytest.mark.cmd_check("mqtt_pub_main")
def test_mqtt_pub_retained(broker: MosquittoBroker) -> None:
    """A retained ``mqtt_pub`` message reaches a later host subscriber.

    :param broker: broker fixture
    """
    topic, msg = "tl/ret", "paho-retained"
    mark = broker.mark()
    _paho_pub(0, topic, msg, 1, retain=True)
    try:
        assert broker.wait(_published("n0p", topic, 1, True), mark)
        with HostSub(broker, topic, 1) as sub:
            assert sub.messages() == [f"1 1 {msg}"]
    finally:
        _host_pub(topic, "", 1, retain=True)


@pytest.mark.cmd_check("mqtt_pub_main")
@pytest.mark.xfail(strict=True, reason=PAHO_PUB_OPTS_BUG)
def test_mqtt_pub_not_retained(broker: MosquittoBroker) -> None:
    """``mqtt_pub`` without ``-r`` after a ``-r`` run does not retain.

    :param broker: broker fixture
    """
    topic = "tl/noret"
    _paho_pub(0, topic, "first", 1, retain=True)
    mark = broker.mark()
    _paho_pub(0, topic, "second", 1)
    try:
        found = broker.wait(_published("n0p", topic, 1, False), mark, 5)
        assert found, broker.text(mark)
    finally:
        _host_pub(topic, "", 1, retain=True)


@pytest.mark.cmd_check("mqtt_sub_main")
def test_mqtt_sub(broker: MosquittoBroker) -> None:
    """``mqtt_sub`` prints a retained message, then live QoS 0/1/2 ones.

    :param broker: broker fixture
    """
    topic = "tl/sub"
    _host_pub(topic, "host-retained", 1, retain=True)
    pid = _paho_sub_start(broker, 0, topic, 2, expect="host-retained")
    try:
        for qos in (0, 1, 2):
            msg = f"host-qos{qos}"
            _host_pub(topic, msg, qos)
            assert _received(0, _paho_msg_re(topic, msg)), msg
    finally:
        stopped = _paho_sub_stop(broker, 0, pid)
        _host_pub(topic, "", 1, retain=True)
    assert stopped


@pytest.mark.cmd_check("mqtt_pub_main")
@pytest.mark.cmd_check("mqtt_sub_main")
def test_mqtt_node_to_node(broker: MosquittoBroker) -> None:
    """node0 ``mqtt_pub`` messages reach node1 ``mqtt_sub`` via the host.

    :param broker: broker fixture
    """
    topic = "tl/n2n"
    pid = _paho_sub_start(broker, 1, topic, 2)
    try:
        for qos in (0, 1, 2):
            msg = f"node0-qos{qos}"
            _paho_pub(0, topic, msg, qos)
            assert _received(1, _paho_msg_re(topic, msg)), msg
    finally:
        stopped = _paho_sub_stop(broker, 1, pid)
    assert stopped


@pytest.mark.dep_config("CONFIG_EXAMPLES_MQTTC")
@pytest.mark.xfail(strict=True, reason=MQTTC_PUB_CMAKE_BUG)
@pytest.mark.parametrize("qos", [0, 1])
def test_mqttc_pub(broker: MosquittoBroker, qos: int) -> None:
    """``mqttc_pub -n 2`` delivers both messages at the given QoS.

    :param broker: broker fixture
    :param qos: QoS level
    """
    topic, msg = f"tl/c{qos}", f"mqttc-qos{qos}"
    cmd = f"mqttc_pub -h {HOST_IP} -t {topic} -q {qos} -n 2 -m {msg}"
    with HostSub(broker, topic, 2) as sub:
        out = _run(0, cmd)
        assert out.count("Success: Published to broker!") == 2, out
        assert sub.messages() == [f"{qos} 0 {msg}"] * 2


@pytest.mark.cmd_check("mqttc_sub_main")
def test_mqttc_sub_reconnect(broker: MosquittoBroker) -> None:
    """``mqttc_sub`` receives, resubscribes after a broker restart, quits.

    :param broker: broker fixture
    """
    topic = "tl/csub"
    mark = broker.mark()
    out = (
        _core(0)
        .sendCommandReadUntilPattern(
            f"mqttc_sub -h {HOST_IP} -t {topic} -q 1",
            pattern="Listening for",
            timeout=60,
        )
        .output
    )
    try:
        assert "Listening for" in out, out
        assert broker.wait(r"Sending SUBACK to auto-", mark)
        _host_pub(topic, "before-restart", 1)
        assert _received(0, r"Received: before-restart")
        broker.stop()
        mark = broker.mark()
        broker.start()
        assert broker.wait(r"Sending SUBACK to auto-", mark, 30)
        _host_pub(topic, "after-restart", 1)
        assert _received(0, r"Received: after-restart")
    finally:
        quit_ret = _core(0).sendCommand("q", timeout=15)
    assert quit_ret == 0


@pytest.mark.cmd_check("mqtt_pub_main")
def test_mqtt_no_broker(
    broker: MosquittoBroker, request: pytest.FixtureRequest
) -> None:
    """``mqtt_pub`` reports a refused connect and exits on SIGINT.

    It does not retry the connect and waits for a signal. Runs last: a
    task stuck in ``exit()`` cannot be removed.

    :param broker: broker fixture
    :param request: pytest request, to mark the expected failure
    """
    wait = _core(0).conf.kv_check("CONFIG_GROUP_KILL_CHILDREN_TIMEOUT_MS")
    if wait and int(wait) < 0:
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=EXIT_SIGTERM_BUG)
        )
    broker.stop()
    try:
        pid, out = _start_bg(
            0,
            f"mqtt_pub -h {HOST_IP} -i n0p -t tl/none -q 1 -v -m x",
            r"Connect failed",
        )
        assert "Connect failed" in out, out
        _core(0).sendCommand(f"kill -2 {pid}", timeout=10)
        assert _wait_exit(0, pid), "mqtt_pub did not exit on SIGINT"
    finally:
        broker.start()
