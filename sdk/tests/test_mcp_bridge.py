"""Tests for astral_sdk.mcp_bridge.Bridge: tools/list, tools/call, JSON-RPC error
mapping, and the stdio request/response loop against the local fake server, with
the per-request _meta the 2026-07-28 wire contract requires.
"""

from __future__ import annotations

import io
import json
import uuid

from astral_sdk.client import AstralClient
from astral_sdk.mcp_bridge import Bridge
from astral_sdk.tools import ASTRAL_TOOLS, CONTRACT

_META = {
    "io.modelcontextprotocol/protocolVersion": CONTRACT["mcp"]["protocol_version"],
    "io.modelcontextprotocol/clientCapabilities": {},
}


def test_tools_list_serves_mcp_descriptors(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _META}})
        assert response["id"] == 1
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        assert "astral_submit_operation" in tools
        assert tools["astral_submit_operation"]["inputSchema"] == ASTRAL_TOOLS["astral_submit_operation"]["input_schema"]
    finally:
        bridge.close()


def test_tools_call_wraps_the_result_in_the_content_envelope(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {
            "jsonrpc": "2.0", "id": "req-1", "method": "tools/call",
            "params": {"_meta": _META, "name": "astral_submit_operation",
                      "arguments": {"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"}},
        }
        response = bridge.handle(request)
        assert response["id"] == "req-1"
        assert response["result"]["structuredContent"]["created"] is True
        assert response["result"]["structuredContent"]["title"] == "A"
        assert response["result"]["isError"] is False
    finally:
        bridge.close()


def test_upstream_tool_failure_is_an_execution_error(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"_meta": _META, "name": "astral_get_operation",
                             "arguments": {"operation_id": str(uuid.uuid4())}}}
        response = bridge.handle(request)
        assert "error" not in response
        assert response["result"]["isError"] is True
        assert response["result"]["structuredContent"] == {"code": "work_not_found"}
    finally:
        bridge.close()


def test_unknown_method_is_a_method_error():
    bridge = Bridge(AstralClient.__new__(AstralClient))
    response = bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "not/a/method", "params": {"_meta": _META}})
    assert response["error"]["code"] == -32601


def test_a_notification_without_id_gets_no_response():
    bridge = Bridge(AstralClient.__new__(AstralClient))
    assert bridge.handle({"jsonrpc": "2.0", "method": "not/a/method"}) is None


def test_serve_stdio_processes_one_line_and_writes_one_response(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {"_meta": _META}}
        in_stream = io.StringIO(json.dumps(request) + "\n")
        out_stream = io.StringIO()
        bridge.serve_stdio(in_stream, out_stream)
        lines = [line for line in out_stream.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["id"] == 9
        assert parsed["result"]["resultType"] == "complete"
    finally:
        bridge.close()


def test_serve_stdio_recovers_from_malformed_json_lines(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _META}}
        in_stream = io.StringIO("not json\n" + json.dumps(request) + "\n")
        out_stream = io.StringIO()
        bridge.serve_stdio(in_stream, out_stream)
        lines = [json.loads(line) for line in out_stream.getvalue().splitlines() if line.strip()]
        assert lines[0]["error"]["code"] == -32700
        assert lines[1]["id"] == 1
    finally:
        bridge.close()
