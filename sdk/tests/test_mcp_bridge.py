"""Tests for astral_sdk.mcp_bridge.Bridge: tools/list, tools/call, JSON-RPC error
mapping, and the stdio request/response loop against the local fake server.
"""

from __future__ import annotations

import io
import json
import uuid

from astral_sdk.client import AstralClient
from astral_sdk.mcp_bridge import Bridge


def test_tools_list_returns_the_function_schemas(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response["id"] == 1
        names = {tool["name"] for tool in response["result"]["tools"]}
        assert "astral_submit_operation" in names
    finally:
        bridge.close()


def test_tools_call_submit_round_trips_through_the_bridge(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {
            "jsonrpc": "2.0", "id": "req-1", "method": "tools/call",
            "params": {"name": "astral_submit_operation",
                      "arguments": {"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"}},
        }
        response = bridge.handle(request)
        assert response["id"] == "req-1"
        assert response["result"]["created"] is True
        assert response["result"]["title"] == "A"
    finally:
        bridge.close()


def test_tool_error_becomes_a_json_rpc_error(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "astral_get_operation", "arguments": {"operation_id": str(uuid.uuid4())}}}
        response = bridge.handle(request)
        assert "error" in response
        assert response["error"]["data"]["code"] == "work_not_found"
    finally:
        bridge.close()


def test_unknown_method_is_a_method_error():
    bridge = Bridge(AstralClient.__new__(AstralClient))
    response = bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "not/a/method"})
    assert response["error"]["code"] == -32601


def test_a_notification_without_id_gets_no_response():
    bridge = Bridge(AstralClient.__new__(AstralClient))
    assert bridge.handle({"jsonrpc": "2.0", "method": "not/a/method"}) is None


def test_serve_stdio_processes_one_line_and_writes_one_response(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}
        in_stream = io.StringIO(json.dumps(request) + "\n")
        out_stream = io.StringIO()
        bridge.serve_stdio(in_stream, out_stream)
        lines = [line for line in out_stream.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["id"] == 9
    finally:
        bridge.close()


def test_serve_stdio_recovers_from_malformed_json_lines(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        in_stream = io.StringIO("not json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        out_stream = io.StringIO()
        bridge.serve_stdio(in_stream, out_stream)
        lines = [json.loads(line) for line in out_stream.getvalue().splitlines() if line.strip()]
        assert lines[0]["error"]["code"] == -32700
        assert lines[1]["id"] == 1
    finally:
        bridge.close()
