"""Drives examples/mcp-progress-server over a real loopback socket: stateless discovery, tool calls, progress before the result, and refusal of an unsupported protocol version.
It also checks that the example's requirements are pinned, and needs no network beyond 127.0.0.1 and no secret.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from tests.fixtures.connections.mcp_server import PROTOCOL_VERSION, VERSION_KEY

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "mcp-progress-server"
HEADERS = {"mcp-protocol-version": PROTOCOL_VERSION}


def _load():
    spec = importlib.util.spec_from_file_location("mcp_progress_example", EXAMPLE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def base():
    module = _load()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


def rpc(method, params=None, ident=1, meta=None, headers=HEADERS):
    body_params = dict(params or {})
    body_params["_meta"] = {VERSION_KEY: PROTOCOL_VERSION, **(meta or {})}
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": body_params}, headers


def post(base, method, params=None, **kwargs):
    body, headers = rpc(method, params, **kwargs)
    return httpx.post(base, json=body, headers=headers, timeout=10)


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_discover_lists_only_the_pinned_revision(base):
    reply = httpx.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, timeout=10).json()
    assert reply["result"]["supportedVersions"] == [PROTOCOL_VERSION]


def test_tools_list_declares_both_tools(base):
    reply = post(base, "tools/list").json()
    assert reply["result"]["resultType"] == "complete"
    assert [tool["name"] for tool in reply["result"]["tools"]] == ["echo", "long_task"]


def test_echo_returns_its_text(base):
    reply = post(base, "tools/call", {"name": "echo", "arguments": {"text": "hi"}}).json()
    assert reply["result"]["content"] == [{"type": "text", "text": "hi"}]
    assert reply["result"]["isError"] is False


def test_long_task_sends_progress_before_its_result(base):
    response = post(
        base,
        "tools/call",
        {"name": "long_task", "arguments": {"steps": 3}},
        meta={"progressToken": "t1"},
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = events(response)
    assert [m["method"] for m in messages[:-1]] == ["notifications/progress"] * 3
    assert [m["params"]["progress"] for m in messages[:-1]] == [1, 2, 3]
    assert {m["params"]["progressToken"] for m in messages[:-1]} == {"t1"}
    assert messages[-1]["result"]["content"][0]["text"] == "finished 3 steps"


def test_long_task_without_a_token_sends_only_the_result(base):
    messages = events(post(base, "tools/call", {"name": "long_task", "arguments": {"steps": 2}}))
    assert len(messages) == 1
    assert messages[0]["result"]["isError"] is False


@pytest.mark.parametrize("arguments", [{}, {"steps": 0}, {"steps": 21}, {"steps": True}, {"steps": 1, "step_seconds": 9}])
def test_long_task_refuses_bad_arguments(base, arguments):
    reply = post(base, "tools/call", {"name": "long_task", "arguments": arguments}).json()
    assert reply["error"]["code"] == -32602


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"name": "echo", "arguments": {"text": 3}}, -32602),
        ({"name": "nothing"}, -32602),
    ],
)
def test_tools_call_refuses_bad_calls(base, params, code):
    assert post(base, "tools/call", params).json()["error"]["code"] == code


def test_unknown_method_is_refused(base):
    assert post(base, "resources/list").json()["error"]["code"] == -32601


def test_an_unsupported_protocol_version_is_refused(base):
    body, _ = rpc("tools/list")
    for headers, meta in (({"mcp-protocol-version": "2025-11-25"}, PROTOCOL_VERSION), (HEADERS, "2025-11-25"), ({}, PROTOCOL_VERSION)):
        body["params"]["_meta"] = {VERSION_KEY: meta}
        response = httpx.post(base, json=body, headers=headers, timeout=10)
        assert response.status_code == 400
        assert response.json() == {"error": "UnsupportedProtocolVersionError", "supportedVersions": [PROTOCOL_VERSION]}


def test_a_request_with_no_meta_is_refused(base):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(base, json=body, headers=HEADERS, timeout=10).status_code == 400


@pytest.mark.parametrize("raw", [b"{", b"[]", b"3"])
def test_malformed_bodies_are_refused(base, raw):
    response = httpx.post(base, content=raw, headers={"content-type": "application/json", **HEADERS}, timeout=10)
    assert response.status_code == 400


def test_requirements_are_pinned():
    lines = [line for line in (EXAMPLE / "requirements.txt").read_text().splitlines() if line.strip()]
    assert lines
    assert all("==" in line and not any(mark in line for mark in "<>~*,") for line in lines)
