"""Proves the node's stdio launcher refuses in production with connection.sandbox_unavailable and, with a test-only sandbox backend, bridges the fixture stdio server over a bearer-guarded loopback endpoint.
It covers the bridge's refusals, JSON-RPC in both directions, the child's environment and its stop on removal, then drives the same launcher through the real kernel binding and the node's connection listing.
"""

from __future__ import annotations

import http.client
import json
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace as NS
from typing import Any, Dict, Optional

import astral_kernel as ak
import pytest

from orchestrator import node_connections, node_stdio_launcher
from orchestrator.node_stdio_launcher import (
    RUNS_ELSEWHERE,
    SANDBOX_UNAVAILABLE,
    LaunchRefused,
    LaunchSpec,
    NodeStdioLauncher,
)
from tests.helpers.dev_mesh import connection_reached, dev_node, until
from tests.helpers.stdio_sandbox import FixtureSandbox


def spec(env: Optional[Dict[str, str]] = None, ident: str = "conn-1") -> LaunchSpec:
    return LaunchSpec(
        id=ident, command="fixture", args=(), env=env or {}, egress=(), policy={"v": 1}, policy_digest="sha256:" + "0" * 64,
    )


def call(url: str, body: Any, bearer: Optional[str] = None, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None):
    request = urllib.request.Request(url, data=raw if raw is not None else json.dumps(body).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    if bearer is not None:
        request.add_header("Authorization", f"Bearer {bearer}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read()


def rpc(method: str, ident: int = 1, **params: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}


@pytest.fixture
def started():
    sandbox = FixtureSandbox()
    launcher = NodeStdioLauncher(sandbox)
    try:
        yield launcher, sandbox
    finally:
        launcher.stop_all()


def test_production_has_no_backend_and_every_launch_refuses():
    launcher = NodeStdioLauncher()
    assert launcher.refusal == SANDBOX_UNAVAILABLE
    with pytest.raises(LaunchRefused) as refused:
        launcher.start(spec())
    assert refused.value.code == SANDBOX_UNAVAILABLE
    assert launcher.running() == ()


def test_a_backend_that_cannot_spawn_refuses_without_leaking_the_environment(caplog):
    launcher = NodeStdioLauncher(FixtureSandbox(fail=True))
    assert launcher.refusal is None
    with caplog.at_level("DEBUG"), pytest.raises(LaunchRefused) as refused:
        launcher.start(spec({"API_TOKEN": "tok-secret-value"}))
    assert refused.value.code == SANDBOX_UNAVAILABLE
    assert "tok-secret-value" not in caplog.text and "API_TOKEN" not in caplog.text


def test_the_bridge_refuses_a_missing_or_wrong_bearer_and_a_browser_origin(started):
    launcher, sandbox = started
    url, bearer = launcher.start(spec())
    body = rpc("tools/list")
    assert call(url, body)[0] == 401
    assert call(url, body, "wrong-" + bearer)[0] == 401
    assert call(url, body, bearer, {"Origin": "http://evil.example"})[0] == 403
    assert call(url, body, bearer, {"Host": "evil.example"})[0] == 403
    assert call(url.replace("/mcp", "/other"), body, bearer)[0] == 404
    assert call(url, None, bearer, raw=b"not json")[0] == 400
    assert call(url, [body], bearer)[0] == 400
    assert call(url, {"id": 1}, bearer)[0] == 400
    connection = http.client.HTTPConnection("127.0.0.1", int(url.split(":")[2].split("/")[0]), timeout=5)
    connection.putrequest("POST", "/mcp")
    connection.putheader("Authorization", f"Bearer {bearer}")
    connection.putheader("Content-Length", str(node_stdio_launcher.MAX_REQUEST_BYTES + 1))
    connection.endheaders()
    assert connection.getresponse().status == 413
    connection.close()
    request = urllib.request.Request(url, method="GET")
    with pytest.raises(urllib.error.HTTPError) as method:
        urllib.request.urlopen(request, timeout=5)
    assert method.value.code == 405
    assert sandbox.children[0].poll() is None


def test_json_rpc_passes_in_both_directions(started):
    launcher, _ = started
    url, bearer = launcher.start(spec())
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
    status, reply = call(url, rpc("tools/list", 7), bearer)
    listed = json.loads(reply)
    assert status == 200 and listed["id"] == 7
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["search", "environment"]
    status, reply = call(url, rpc("tools/call", 8, name="search"), bearer)
    assert json.loads(reply)["result"]["content"][0]["text"] == "called search"
    status, reply = call(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, bearer)
    assert (status, reply) == (202, b"")
    status, reply = call(url, rpc("tools/call", 9, name="search"), bearer)
    assert status == 200 and json.loads(reply)["id"] == 9


def test_the_child_gets_only_the_environment_names_in_the_record(started):
    launcher, sandbox = started
    url, bearer = launcher.start(spec({"API_TOKEN": "a", "REGION": "b"}))
    _, reply = call(url, rpc("tools/call", 1, name="environment"), bearer)
    assert json.loads(json.loads(reply)["result"]["content"][0]["text"]) == ["API_TOKEN", "REGION"]
    assert dict(sandbox.specs[0].env) == {"API_TOKEN": "a", "REGION": "b"}
    assert "API_TOKEN" not in repr(sandbox.specs[0]) and bearer not in repr(sandbox.specs[0])


def test_removal_stops_the_child_and_the_endpoint(started):
    launcher, sandbox = started
    url, bearer = launcher.start(spec())
    child = sandbox.children[0]
    assert call(url, rpc("tools/list"), bearer)[0] == 200
    launcher.stop("conn-1")
    assert child.poll() is not None and launcher.running() == ()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=5)
    launcher.stop("conn-1")


def test_a_second_launch_of_the_same_connection_replaces_the_first(started):
    launcher, sandbox = started
    launcher.start(spec())
    first = sandbox.children[0]
    url, bearer = launcher.start(spec())
    assert first.poll() is not None and launcher.running() == ("conn-1",)
    assert call(url, rpc("tools/list"), bearer)[0] == 200


def test_a_child_that_exits_answers_bad_gateway(started):
    launcher, sandbox = started
    url, bearer = launcher.start(spec())
    sandbox.children[0].stop()
    assert call(url, rpc("tools/list"), bearer)[0] == 502


def test_a_child_that_never_answers_times_out_and_is_stopped(monkeypatch):
    monkeypatch.setattr(node_stdio_launcher, "REPLY_SECONDS", 0.5)
    sandbox = FixtureSandbox(argv=["/bin/cat"])
    silent = NodeStdioLauncher(sandbox)
    try:
        url, bearer = silent.start(spec(ident="silent"))
        assert call(url, rpc("tools/list"), bearer)[0] == 504
        assert sandbox.children[0].poll() is not None
    finally:
        silent.stop_all()


def test_the_host_adapter_returns_the_endpoint_and_turns_a_refusal_into_a_launch_failure(started):
    launcher, _ = started
    host = node_stdio_launcher.host_launcher(ak, launcher)
    request = ak.HostLaunchRequest(
        id="conn-2", command="fixture", args=[], env=[ak.ConnectionKeyEntry(name="REGION", value="b")], egress=[],
        policy_json=json.dumps({"v": 1}), policy_digest="sha256:" + "0" * 64,
    )
    launched = host.start(request)
    assert launched.url.startswith("http://127.0.0.1:") and launched.bearer
    host.stop("conn-2")
    assert launcher.running() == ()
    refusing = node_stdio_launcher.host_launcher(ak, NodeStdioLauncher())
    with pytest.raises(ak.HostLauncherError.Failed):
        refusing.start(request)
    bad = ak.HostLaunchRequest(
        id="conn-3", command="x", args=[], env=[], egress=[], policy_json="{", policy_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(ak.HostLauncherError.Failed):
        host.start(bad)


def test_the_listing_names_the_refusal_for_a_stdio_connection_this_node_would_run():
    this = NS(device="d1", label="n", state="not_signed_in", reason=None, issuer=None, ms=0)
    info = NS(
        id="c", name="n", kind="mcp-stdio", endpoint="fixture", local=True, scheme=None, issuer=None, scopes=[], flow="",
        egress=[], tools=[], pending_tools=None, approved_by="o", approved_by_removed_device=False, supported_here=False,
        unsupported=None, attention=None, this_device=this, other_devices=[], device="d1",
    )
    assert node_connections._connection(info, SANDBOX_UNAVAILABLE)["launch"] == SANDBOX_UNAVAILABLE
    info.device = "d2"
    assert node_connections._connection(info, SANDBOX_UNAVAILABLE)["launch"] == RUNS_ELSEWHERE
    info.kind = "mcp-http"
    assert node_connections._connection(info, SANDBOX_UNAVAILABLE)["launch"] is None


async def test_in_production_the_node_refuses_a_stdio_proposal_with_the_sandbox_code(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        assert mesh.node.stdio_refusal == SANDBOX_UNAVAILABLE
        with pytest.raises(ak.KernelError) as refused:
            await mesh.node.kernel.propose_stdio_connection("files", "fixture", [], [], [], None)
        assert refused.value.code == SANDBOX_UNAVAILABLE


async def test_a_stdio_connection_runs_through_the_real_kernel_and_the_node_lists_it(tmp_path, monkeypatch):
    sandbox = FixtureSandbox()
    owner_launcher = NodeStdioLauncher(sandbox)
    async with dev_node(tmp_path, monkeypatch) as mesh:
        try:
            mesh.owner.kernel.set_launcher(node_stdio_launcher.host_launcher(ak, owner_launcher))
            proposal = await mesh.owner.kernel.propose_stdio_connection(
                "files", "fixture", [], [ak.ConnectionKeyEntry(name="REGION", value="eu")], [], None,
            )
            assert [tool.name for tool in proposal.tools] == ["environment", "search"]
            assert owner_launcher.running() == ()
            info = mesh.owner.approve_connection(proposal, ["search"])
            await connection_reached(mesh, info.id)
            [seen] = (await node_connections.status(mesh.node))["connections"]
            assert (seen["kind"], seen["launch"], seen["supported_here"]) == ("mcp-stdio", RUNS_ELSEWHERE, False)
            mesh.owner.remove_connection(info.id)
            await until(lambda: not mesh.node.kernel.connections(), "the removal to reach the node")
        finally:
            owner_launcher.stop_all()
    assert not any(child.poll() is None for child in sandbox.children)


async def test_stopping_the_node_stops_every_running_child(tmp_path, monkeypatch):
    sandbox = FixtureSandbox()
    async with dev_node(tmp_path, monkeypatch) as mesh:
        mesh.node._launcher._backend = sandbox
        mesh.node._launcher.start(spec(ident="held"))
        assert mesh.node.stdio_refusal is None and sandbox.children[0].poll() is None
    assert sandbox.children[0].poll() is not None


def test_a_child_that_exits_during_startup_fails_the_start():
    sandbox = FixtureSandbox(argv=[sys.executable, "-c", "raise SystemExit(3)"])
    launcher = NodeStdioLauncher(sandbox)
    with pytest.raises(LaunchRefused) as raised:
        launcher.start(spec())
    assert raised.value.code == SANDBOX_UNAVAILABLE
    assert launcher.running() == ()
