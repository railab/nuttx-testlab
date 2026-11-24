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

"""CoAP: libcoap example client and server against libcoap on the host.

Node side: ``coap_client`` and ``coap_server`` (netutils/libcoap,
``NETUTILS_LIBCOAP_EXAMPLE``). Host side: ``coap-client-notls`` and
``coap-server-notls`` of the same libcoap version. node0 runs the
clients; node1 runs ``coap_server -d 10 -v 7`` for the whole module.

With ``-v 6`` a client prints every PDU it sends and receives, the
``-v 7`` servers print every PDU too, e.g.
``v:1 t:ACK c:2.05 i:0001 {01} [ Content-Format:text/plain ] :: 'x'``.
Without ``-v`` a client prints only the response payload, or the
response code and reason for an error response (``4.04 Not Found``).
On the nodes the checks use request PDUs and ``DEBG`` lines: a
response PDU is the last line a server prints for a request and can
stay in the stdout buffer until the next request.
"""

import re
import subprocess
import time
from typing import Any, Iterator, List, NamedTuple

import pytest
from _host_services import CoapServer
from _net_common import HOST_IP, HOST_IP6, NODE_IP6S, NODE_IPS

pytestmark = [
    pytest.mark.dep_config("CONFIG_NETUTILS_LIBCOAP_EXAMPLE"),
    pytest.mark.cmd_check("coap_client_main"),
    pytest.mark.cmd_check("coap_server_main"),
]

HOST_CLIENT = "coap-client-notls"
WAIT = 5
NO_SERVER_PORT = 5699
BLOCK_DATA = "0123456789abcdef" * 3
BIG_DATA = "".join(f"{i:03d}:" for i in range(75))
TIME_RE = r"[A-Z][a-z]{2} \d\d \d\d:\d\d:\d\d"
LOG_RE = r"\d\d:\d\d:\d\d\.\d{3} [A-Z]{4} "

PDU_RE = re.compile(
    r"v:1 t:(CON|NON|ACK|RST) c:(\w+|\d\.\d\d) i:[0-9a-f]+ \{[0-9a-f]*\} "
    r"\[([^\]]*)\](?: :: '([^']*)')?"
)


class Pdu(NamedTuple):
    """One PDU as printed by libcoap."""

    mtype: str
    code: str
    options: str
    data: str


def _core(node: int) -> Any:
    """Return the NTFC core handler of a node.

    :param node: product index
    :return: ``ProductCore`` for the product's core 0
    """
    return pytest.products[node].core(0)


def _pdus(text: str) -> List[Pdu]:
    """Return the PDUs printed in a libcoap log.

    :param text: log or console output
    :return: PDUs in order
    """
    return [Pdu(*(g or "" for g in m)) for m in PDU_RE.findall(text)]


def _responses(text: str) -> List[Pdu]:
    """Return the response PDUs printed in a libcoap log.

    :param text: log or console output
    :return: PDUs with a response code (``x.yy``) in order
    """
    return [p for p in _pdus(text) if re.match(r"[1-5]\.\d\d$", p.code)]


def _client(args: str, timeout: int = 30) -> str:
    """Run ``coap_client`` on node0 until the prompt returns.

    :param args: options and URI
    :param timeout: seconds to wait for the prompt
    :return: console output
    """
    ret = _core(0).sendCommandReadUntilPattern(
        f"coap_client -B {WAIT} {args}", pattern=r"nsh> ", timeout=timeout
    )
    return str(ret.output)


def _client_body(args: str) -> str:
    """Run ``coap_client`` on node0 and return what it printed.

    :param args: options and URI, without ``-v``
    :return: printed payload or error, without the command echo
    """
    out = _client(args)
    lines = [ln.strip() for ln in out.splitlines()]
    lines = [ln for ln in lines if ln and "coap_client" not in ln]
    lines = [ln for ln in lines if not re.search(LOG_RE, ln)]
    return "".join(ln for ln in lines if not ln.startswith("nsh>"))


def _host_client(*args: str) -> str:
    """Run ``coap-client-notls`` on the host.

    :param args: options and URI
    :return: stdout followed by stderr
    """
    ret = subprocess.run(  # noqa: S603
        [HOST_CLIENT, "-B", str(WAIT), *args],
        capture_output=True,
        text=True,
        timeout=WAIT + 30,
        check=False,
    )
    return ret.stdout + ret.stderr


def _node_mark(node: int) -> None:
    """Drop console output a background server printed so far.

    :param node: product index
    """
    assert _core(node).sendCommand("echo coap-mark", "coap-mark") == 0


def _node_log(node: int, regex: str, timeout: int = 10) -> str:
    """Read a node's console until a pattern.

    :param node: product index
    :param regex: pattern to wait for
    :param timeout: seconds to wait
    :return: console output read, empty if the pattern did not appear
    """
    ret = _core(node).readUntilPattern(regex, timeout=timeout)
    out = str(ret.output)
    return out if re.search(regex, out) else ""


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


@pytest.fixture(scope="module")
def host_server() -> Iterator[CoapServer]:
    """Run ``coap-server-notls`` on the host for this module."""
    with CoapServer() as server:
        yield server


@pytest.fixture(scope="module")
def node_server() -> Iterator[int]:
    """Run ``coap_server -d 10 -v 7`` on node1 for this module.

    :return: PID of the server task
    """
    out = str(
        _core(1)
        .sendCommandReadUntilPattern(
            "coap_server -d 10 -v 7 &",
            pattern=r"coap_server \[\d+:",
            timeout=10,
        )
        .output
    )
    found = re.search(r"coap_server \[(\d+):", out)
    assert found, out
    pid = int(found.group(1))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _responses(_host_client("-v", "6", f"coap://{NODE_IPS[1]}/time")):
            break
        time.sleep(1)
    else:
        pytest.fail("coap_server does not answer")
    yield pid
    _core(1).sendCommand(f"kill -2 {pid}", timeout=10)
    assert _wait_exit(1, pid), "coap_server did not exit on SIGINT"


@pytest.mark.parametrize(
    "non, rsp_type", [(False, "ACK"), (True, "NON")], ids=["con", "non"]
)
def test_coap_client_get(
    host_server: CoapServer, non: bool, rsp_type: str
) -> None:
    """node0 GET of ``/time`` from the host, confirmable or not.

    RFC 7252, 5.2.1: for a Confirmable request "the response is carried
    directly in the Acknowledgement message"; 5.2.3: "If the request
    message is Non-confirmable, then the response SHOULD be returned in
    a Non-confirmable message as well."  5.8.1: "Upon success, a 2.05
    (Content) or 2.03 (Valid) Response Code SHOULD be present in the
    response."

    :param host_server: host server fixture
    :param non: send a non-confirmable request
    :param rsp_type: expected response type
    """
    req_type = "NON" if non else "CON"
    mark = host_server.mark()
    out = _client(f"-v 6 {'-N ' if non else ''}coap://{HOST_IP}/time")
    pdus = _pdus(out)
    assert [(p.mtype, p.code) for p in pdus] == [
        (req_type, "GET"),
        (rsp_type, "2.05"),
    ], out
    assert re.fullmatch(TIME_RE, pdus[1].data), out
    log = host_server.wait(rf"^v:1 t:{rsp_type} c:2\.05 .*$", mark)
    assert log, host_server.text(mark)
    assert re.search(
        rf"^v:1 t:{req_type} c:GET .*Uri-Path:time",
        host_server.text(mark),
        re.MULTILINE,
    )


def test_coap_client_methods(host_server: CoapServer) -> None:
    """node0 PUT, POST, GET and DELETE of host resources.

    RFC 7252, 5.8.3 PUT: "If no resource exists, then the server MAY
    create a new resource with that URI, resulting in a 2.01 (Created)
    Response Code"; "If a resource exists ... a 2.04 (Changed) Response
    Code SHOULD be returned."  5.8.2 POST: "If a resource has been
    created on the server, the response returned by the server SHOULD
    have a 2.01 (Created) Response Code".  5.8.4 DELETE: "A 2.02
    (Deleted) Response Code SHOULD be used on success"; a GET of a
    deleted resource gets 4.04 (Not Found, 5.9.2.5).

    :param host_server: host server fixture
    """
    base = f"coap://{HOST_IP}/tl"
    steps = [
        ("-m put -e v1", "a", "2.01"),
        ("-m post -e p1", "b", "2.01"),
        ("-m put -e v2", "a", "2.04"),
        ("-m delete", "a", "2.02"),
        ("-m delete", "b", "2.02"),
    ]
    try:
        for opts, res, code in steps[:3]:
            mark = host_server.mark()
            rsp = _responses(_client(f"-v 6 {opts} {base}/{res}"))
            assert [p.code for p in rsp] == [code], (opts, res, rsp)
            assert host_server.wait(rf"^v:1 t:ACK c:{code} ", mark)
        assert _client_body(f"{base}/a") == "v2"
        assert _client_body(f"{base}/b") == "p1"
        for opts, res, code in steps[3:]:
            rsp = _responses(_client(f"-v 6 {opts} {base}/{res}"))
            assert [p.code for p in rsp] == [code], (opts, res, rsp)
        assert _client_body(f"{base}/a") == "4.04 Not Found"
    finally:
        _host_client("-m", "delete", f"{base}/a")
        _host_client("-m", "delete", f"{base}/b")


def test_coap_client_blockwise(host_server: CoapServer) -> None:
    """node0 Block1 PUT and Block2 GET of 48 bytes in 16-byte blocks.

    RFC 7959, 2.1: "the Block1 Option pertains to the request payload,
    and the Block2 Option pertains to the response payload"; 2.3: in
    descriptive usage "The NUM field in the option value describes what
    block number is contained in the payload of this message."  The
    blocks are numbered 0, 1, 2 and the payload is reassembled intact.

    :param host_server: host server fixture
    """
    uri = f"coap://{HOST_IP}/tl/blk"
    try:
        mark = host_server.mark()
        out = _client(f"-v 6 -b 16 -m put -e {BLOCK_DATA} {uri}")
        req = [p for p in _pdus(out) if p.code == "PUT"]
        assert req and all("Block1:" in p.options for p in req), out
        assert _responses(out)[-1].code == "2.01", out
        log = host_server.text(mark)
        blocks = re.findall(r"c:PUT .*Block1:(\d+)/", log)
        assert blocks == ["0", "1", "2"], log
        mark = host_server.mark()
        assert _client_body(f"-b 16 {uri}") == BLOCK_DATA
        log = host_server.text(mark)
        blocks = re.findall(r"t:ACK c:2\.05 .*Block2:(\d+)/.*Size2", log)
        assert blocks == ["0", "1", "2"], log
    finally:
        _host_client("-m", "delete", uri)


def test_coap_client_observe(host_server: CoapServer) -> None:
    """node0 observes ``/time`` for 4 s, then deregisters.

    RFC 7641, 4.2: "A 2.xx notification MUST include an Observe Option";
    3.6: "a client MAY explicitly deregister by issuing a GET request
    that ... includes an Observe Option with the value set to 1
    (deregister)."

    :param host_server: host server fixture
    """
    mark = host_server.mark()
    out = _client(f"-w -s 4 coap://{HOST_IP}/time")
    values = re.findall(TIME_RE, out)
    assert len(set(values)) >= 3, out
    log = host_server.text(mark)
    assert re.search(r"c:GET .*Observe:, Uri-Path:time", log), log
    notes = re.findall(r"c:2\.05 .*\[ Observe:(\d+),", log)
    assert len(notes) >= 3, log
    assert host_server.wait(r"c:GET .*Observe:1, Uri-Path:time", mark), log


def test_coap_client_no_server() -> None:
    """node0 gives up after ``-B 3`` when nothing answers on the port.

    No response PDU arrives, so the client prints a timeout and returns
    after its ``-B`` wait (3 s), not within it, and well before the
    RFC 7252, 4.8.2 MAX_TRANSMIT_WAIT of 93 s.
    """
    start = time.monotonic()
    out = _client(f"-v 6 -B 3 coap://{HOST_IP}:{NO_SERVER_PORT}/time")
    elapsed = time.monotonic() - start
    assert "timeout" in out, out
    assert not _responses(out), out
    assert 3 <= elapsed < 15, elapsed


@pytest.mark.dep_config("CONFIG_NET_IPv6")
def test_coap_client_ipv6(host_server: CoapServer) -> None:
    """node0 PUT and GET over IPv6.

    RFC 7252, 6.1: the coap URI host may be an IP-literal, i.e. an IPv6
    address; PUT creates the resource (2.01, 5.8.3) and GET returns the
    stored representation.  The host server sees node0's IPv6 address.

    :param host_server: host server fixture
    """
    uri = f"coap://[{HOST_IP6}]/tl/v6"
    try:
        mark = host_server.mark()
        rsp = _responses(_client(f"-v 6 -m put -e six {uri}"))
        assert [p.code for p in rsp] == ["2.01"], rsp
        assert _client_body(uri) == "six"
        log = host_server.text(mark)
        assert re.search(rf"\[{NODE_IP6S[0]}\]:\d+ .*UDP", log), log
    finally:
        _host_client("-m", "delete", uri)


def test_coap_server_discovery(node_server: int) -> None:
    """Host GET of node1 ``/.well-known/core`` lists its resources.

    RFC 6690, 1.2.1: discovery "is performed using a GET to
    ``/.well-known/core`` on the server, which returns a payload in the
    CoRE Link Format."  RFC 7641, 6: a link "MAY include the target
    attribute ``obs``" for an observable resource such as ``/time``.

    :param node_server: node1 server fixture
    """
    out = _host_client(f"coap://{NODE_IPS[1]}/.well-known/core")
    links = re.findall(r"<([^>]*)>", out)
    assert {"/time", "/example_data"} <= set(links), out
    assert re.search(r"</time>;[^,]*;obs", out), out


@pytest.mark.parametrize(
    "non, rsp_type", [(False, "ACK"), (True, "NON")], ids=["con", "non"]
)
def test_coap_server_get(node_server: int, non: bool, rsp_type: str) -> None:
    """Host GET of node1 ``/time``, confirmable or not.

    RFC 7252, 5.2.1 and 5.2.3: a CON request gets the response in the
    ACK, a NON request "SHOULD" get a NON response; 5.8.1: 2.05 on
    success.

    :param node_server: node1 server fixture
    :param non: send a non-confirmable request
    :param rsp_type: expected response type
    """
    req_type = "NON" if non else "CON"
    _node_mark(1)
    args = ["-v", "6", *(["-N"] if non else []), f"coap://{NODE_IPS[1]}/time"]
    out = _host_client(*args)
    pdus = _pdus(out)
    assert [(p.mtype, p.code) for p in pdus] == [
        (req_type, "GET"),
        (rsp_type, "2.05"),
    ], out
    assert re.fullmatch(TIME_RE, pdus[1].data), out
    assert _node_log(1, rf"v:1 t:{req_type} c:GET .*Uri-Path:time"), req_type


def test_coap_server_put_get(node_server: int) -> None:
    """Host PUT creates a node1 resource, GET reads it, DELETE removes it.

    RFC 7252, 5.8.3: PUT of a new URI MAY create it, "resulting in a
    2.01 (Created) Response Code"; 5.8.4: "A 2.02 (Deleted) Response
    Code SHOULD be used on success"; a GET afterwards gets 4.04.

    :param node_server: node1 server fixture
    """
    uri = f"coap://{NODE_IPS[1]}/tl/h"
    _node_mark(1)
    out = _host_client("-v", "6", "-m", "put", "-e", "hv1", uri)
    assert [p.code for p in _responses(out)] == ["2.01"], out
    assert _node_log(1, r"c:PUT .*Uri-Path:h.*:: 'hv1'"), "no PUT on node1"
    assert _host_client(uri).strip() == "hv1"
    out = _host_client("-v", "6", "-m", "delete", uri)
    assert [p.code for p in _responses(out)] == ["2.02"], out
    assert _host_client(uri).strip() == "4.04 Not Found"


def test_coap_server_not_found(node_server: int) -> None:
    """Host GET of an unknown node1 resource gets 4.04.

    RFC 7252, 5.9.2.5 (4.04 Not Found): "This Response Code is like HTTP
    404 'Not Found'."

    :param node_server: node1 server fixture
    """
    _node_mark(1)
    out = _host_client("-v", "6", f"coap://{NODE_IPS[1]}/no/such")
    assert [p.code for p in _responses(out)] == ["4.04"], out
    assert _node_log(1, r"unknown resource 'no/such', return 4\.04")


def test_coap_server_blockwise(node_server: int, tmp_path: Any) -> None:
    """Host Block1 PUT and Block2 GET of 300 bytes in 64-byte blocks.

    RFC 7959, 2.3: in descriptive usage "The NUM field in the option
    value describes what block number is contained in the payload of
    this message": node1 sees Block1 0..4 and returns Block2 0..4, and
    the reassembled payload equals the one written.

    :param node_server: node1 server fixture
    :param tmp_path: pytest temporary directory
    """
    uri = f"coap://{NODE_IPS[1]}/example_data"
    data = tmp_path / "big.txt"
    data.write_text(BIG_DATA)
    _node_mark(1)
    out = _host_client(
        "-v", "6", "-b", "64", "-m", "put", "-f", str(data), uri
    )
    assert _responses(out)[-1].code in ("2.01", "2.04"), out
    log = _node_log(1, r"c:PUT .*Block1:4/_/64")
    blocks = re.findall(r"c:PUT .*Block1:(\d+)/", log)
    assert blocks == ["0", "1", "2", "3", "4"], log
    out = _host_client("-v", "6", "-b", "64", uri)
    blocks = re.findall(r"c:2\.05 .*Block2:(\d+)/", out)
    assert blocks == ["0", "1", "2", "3", "4"], out
    assert _host_client("-b", "64", uri).strip() == BIG_DATA


def test_coap_server_observe(node_server: int) -> None:
    """Host observes node1 ``/time`` for 4 s, then deregisters.

    RFC 7641, 4.2: "A 2.xx notification MUST include an Observe Option";
    4.1: on a GET with Observe set to 1 (deregister) the server MUST
    "process the GET request as usual.  The resulting response MUST NOT
    include an Observe Option."

    :param node_server: node1 server fixture
    """
    _node_mark(1)
    out = _host_client(
        "-v", "6", "-w", "-s", "4", f"coap://{NODE_IPS[1]}/time"
    )
    rsp = _responses(out)
    notes = [p for p in rsp if "Observe:" in p.options]
    assert len({p.data for p in notes}) >= 3, out
    assert rsp and "Observe:" not in rsp[-1].options, out
    assert _node_log(1, r"c:GET .*Observe:1, Uri-Path:time"), "no deregister"


@pytest.mark.dep_config("CONFIG_NET_IPv6")
def test_coap_server_ipv6(node_server: int) -> None:
    """Host GET of node1 ``/time`` over IPv6.

    RFC 7252, 6.1: IP-literal host in the coap URI; 5.8.1: 2.05 on
    success.

    :param node_server: node1 server fixture
    """
    out = _host_client("-v", "6", f"coap://[{NODE_IP6S[1]}]/time")
    rsp = _responses(out)
    assert [p.code for p in rsp] == ["2.05"], out
    assert re.fullmatch(TIME_RE, rsp[0].data), out


def test_coap_node_to_node(node_server: int) -> None:
    """node0 PUT, GET and DELETE of a node1 resource.

    RFC 7252, 5.8.3 and 5.8.4: 2.01 (Created) for a new resource, GET
    returns it, 2.02 (Deleted) on success.

    :param node_server: node1 server fixture
    """
    uri = f"coap://{NODE_IPS[1]}/tl/n2n"
    _node_mark(1)
    rsp = _responses(_client(f"-v 6 -m put -e node0 {uri}"))
    assert [p.code for p in rsp] == ["2.01"], rsp
    assert _node_log(1, r"c:PUT .*Uri-Path:n2n.*:: 'node0'")
    assert _client_body(uri) == "node0"
    rsp = _responses(_client(f"-v 6 -m delete {uri}"))
    assert [p.code for p in rsp] == ["2.02"], rsp
