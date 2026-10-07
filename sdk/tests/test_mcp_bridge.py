"""Tests for astral_sdk.mcp_bridge.Bridge: the MCP 2026-07-28 tools/list, server/discover
and tools/call envelopes, bounded JSON-RPC protocol errors, and the stdio
request/response loop against the local fake server.
"""

from __future__ import annotations

import io
import json
import uuid

import pytest

from astral_sdk.client import AstralClient
from astral_sdk.mcp_bridge import Bridge
from tests.fake_server import FakeAstralState

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _request(method, **params):
    params.setdefault("_meta", META)
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def test_tools_list_returns_mcp_tool_descriptors(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        response = bridge.handle(_request("tools/list"))
        assert response["id"] == 1
        result = response["result"]
        validate_mcp("ListToolsResult", result)
        names = {tool["name"] for tool in result["tools"]}
        assert "astral_submit_operation" in names
        for tool in result["tools"]:
            validate_mcp("Tool", tool)
            assert "parameters" not in tool
            assert tool["inputSchema"]["type"] == "object"
            assert tool["inputSchema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert tool["_meta"]["astral/agentId"] == "__work__"
    finally:
        bridge.close()


def test_discover_advertises_the_declared_protocol(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        response = bridge.handle(_request("server/discover"))
        result = response["result"]
        validate_mcp("DiscoverResult", result)
        assert result["supportedVersions"] == ["2026-07-28"]
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 60000
        assert result["cacheScope"] == "private"
        assert result["capabilities"]["tools"] == {}
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "astral-sdk-bridge"
    finally:
        bridge.close()


def test_tools_call_submit_round_trips_through_the_bridge(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {
            "jsonrpc": "2.0", "id": "req-1", "method": "tools/call",
            "params": {"_meta": META, "name": "astral_submit_operation",
                       "arguments": {"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"}},
        }
        response = bridge.handle(request)
        assert response["id"] == "req-1"
        result = response["result"]
        validate_mcp("CallToolResult", result)
        assert result["resultType"] == "complete"
        assert result["isError"] is False
        assert result["structuredContent"]["created"] is True
        assert result["structuredContent"]["title"] == "A"
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    finally:
        bridge.close()


def test_tool_error_is_a_completed_error_result(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"_meta": META, "name": "astral_get_operation",
                             "arguments": {"operation_id": str(uuid.uuid4())}}}
        response = bridge.handle(request)
        result = response["result"]
        validate_mcp("CallToolResult", result)
        assert result["isError"] is True
        assert result["resultType"] == "complete"
        assert result["content"][0]["text"] == "work_not_found"
        assert result["structuredContent"] == {}
    finally:
        bridge.close()


def test_unauthorized_scope_is_a_completed_error_result(fake_server_factory):
    state = FakeAstralState(scopes=frozenset({"operations.submit"}))
    server = fake_server_factory(state)
    bridge = Bridge.connect(server.base_url, server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"_meta": META, "name": "astral_get_operation",
                             "arguments": {"operation_id": str(uuid.uuid4())}}}
        response = bridge.handle(request)
        assert response["result"]["isError"] is True
        assert response["result"]["content"][0]["text"] == "framework_scope_required"
    finally:
        bridge.close()


def test_unsupported_protocol_version_is_refused_with_the_supported_list(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                  "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25",
                                       "io.modelcontextprotocol/clientCapabilities": {}}}}
        response = bridge.handle(request)
        assert response["error"]["code"] == -32022
        assert response["error"]["data"] == {
            "requested": "2025-11-25", "supported": ["2026-07-28"]}
    finally:
        bridge.close()


@pytest.mark.parametrize("params", [
    None,
    {},
    {"_meta": {}},
    {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
               "io.modelcontextprotocol/clientCapabilities": "yes"}},
])
def test_missing_request_metadata_is_invalid_params(fake_server, params):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        response = bridge.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list",
                                  "params": params})
        assert response["error"]["code"] == -32602
    finally:
        bridge.close()


@pytest.mark.parametrize("params", [
    {"_meta": META},
    {"_meta": META, "name": ""},
    {"_meta": META, "name": None},
    {"_meta": META, "name": "astral_get_operation", "arguments": ["operation_id"]},
])
def test_malformed_tools_call_params_are_invalid_params(fake_server, params):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        response = bridge.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                  "params": params})
        assert response["error"]["code"] == -32602
    finally:
        bridge.close()


def test_transport_level_failure_is_a_bounded_error_result(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        fake_server.state.fail_next_n = 10
        exhausted = bridge.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                   "params": {"_meta": META, "name": "astral_list_operations",
                                              "arguments": {}}})
        assert exhausted["result"]["isError"] is True
        assert "exhausted retries" in exhausted["result"]["content"][0]["text"]
        validate_mcp("CallToolResult", exhausted["result"])
        fake_server.state.fail_next_n = 0
        alive = bridge.handle(_request("tools/list"))
        assert "tools" in alive["result"]
    finally:
        bridge.close()


def test_invalid_tool_arguments_are_bounded_error_results(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        missing = bridge.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                 "params": {"_meta": META, "name": "astral_get_operation",
                                            "arguments": {}}})
        assert missing["result"]["isError"] is True
        validate_mcp("CallToolResult", missing["result"])
        unknown = bridge.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                 "params": {"_meta": META, "name": "astral_delete_operation",
                                            "arguments": {}}})
        assert unknown["result"]["isError"] is True
        assert "astral_delete_operation" in unknown["result"]["content"][0]["text"]
        after = bridge.handle(_request("tools/list"))
        assert "tools" in after["result"]
    finally:
        bridge.close()


@pytest.mark.parametrize("request_body", [
    [1, 2, 3],
    "hello",
    {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 1},
    {"jsonrpc": "2.0", "id": 1, "method": 123},
    {"jsonrpc": "2.0", "id": None, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"},
])
def test_malformed_envelopes_are_bounded_protocol_errors(request_body):
    bridge = Bridge(AstralClient.__new__(AstralClient))
    response = bridge.handle(request_body)
    assert response["error"]["code"] in (-32600, -32602)


def test_unknown_method_is_a_method_error():
    bridge = Bridge(AstralClient.__new__(AstralClient))
    response = bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "not/a/method"})
    assert response["error"]["code"] == -32601


def test_initialize_is_not_part_of_the_declared_protocol():
    bridge = Bridge(AstralClient.__new__(AstralClient))
    response = bridge.handle({"jsonrpc": "2.0", "id": 9, "method": "initialize",
                              "params": {"protocolVersion": "2026-07-28"}})
    assert response["error"]["code"] == -32601


def test_a_notification_without_id_gets_no_response():
    bridge = Bridge(AstralClient.__new__(AstralClient))
    assert bridge.handle({"jsonrpc": "2.0", "method": "not/a/method"}) is None
    assert bridge.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": 7, "reason": "gone"}}) is None


def test_serve_stdio_processes_one_line_and_writes_one_response(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        in_stream = io.StringIO(json.dumps(_request("tools/list")) + "\n")
        out_stream = io.StringIO()
        bridge.serve_stdio(in_stream, out_stream)
        lines = [line for line in out_stream.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["id"] == 1
        assert parsed["result"]["resultType"] == "complete"
    finally:
        bridge.close()


def test_serve_stdio_recovers_from_malformed_json_lines(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        payload = json.dumps(_request("tools/list"))
        in_stream = io.StringIO("not json\n" + payload + "\n")
        out_stream = io.StringIO()
        bridge.serve_stdio(in_stream, out_stream)
        lines = [json.loads(line) for line in out_stream.getvalue().splitlines() if line.strip()]
        assert lines[0]["error"]["code"] == -32700
        assert lines[1]["id"] == 1
    finally:
        bridge.close()


def test_serve_stdio_survives_malformed_envelopes_and_handles_cancellation_and_eof(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        lines = [
            json.dumps([1, 2, 3]),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 1, "reason": "gone"}}),
            "",
            "not json",
            json.dumps(_request("tools/list")),
        ]
        out_stream = io.StringIO()
        bridge.serve_stdio(io.StringIO("\n".join(lines) + "\n"), out_stream)
        responses = [json.loads(line) for line in out_stream.getvalue().splitlines()]
        assert len(responses) == 3
        assert responses[0]["error"]["code"] == -32600
        assert responses[1]["error"]["code"] == -32700
        assert responses[2]["result"]["resultType"] == "complete"
    finally:
        bridge.close()


def test_serve_stdio_defaults_to_the_live_streams(fake_server, monkeypatch):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_request("tools/list")) + "\n"))
        out_stream = io.StringIO()
        monkeypatch.setattr("sys.stdout", out_stream)
        bridge.serve_stdio()
        parsed = json.loads(out_stream.getvalue().strip())
        assert parsed["id"] == 1
        assert "tools" in parsed["result"]
    finally:
        bridge.close()
