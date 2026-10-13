"""Tests for the bridge's optional official-SDK serving path: the Server registration and
its discover/list/call handlers reuse the shared MCP protocol adapters, and
serve_with_official_sdk() fails with an actionable ImportError without the package.
"""

from __future__ import annotations

import io
import json
import sys
import uuid

import pytest

from astral_sdk.client import AstralClient
from astral_sdk.mcp_bridge import Bridge

mcp = pytest.importorskip("mcp")
mcp_types = pytest.importorskip("mcp_types")

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def test_official_server_registers_the_three_declared_methods(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        server = bridge._official_server()
        assert set(server._request_handlers) >= {"server/discover", "tools/list", "tools/call"}
    finally:
        bridge.close()


async def test_official_discover_handler_matches_the_lightweight_result(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        result = await bridge._official_discover(None, None)
        assert isinstance(result, mcp_types.DiscoverResult)
        wire = result.model_dump(by_alias=True, mode="json", exclude_none=True)
        validate_mcp("DiscoverResult", wire)
        assert wire["supportedVersions"] == ["2026-07-28"]
        assert wire["resultType"] == "complete"
    finally:
        bridge.close()


async def test_official_list_tools_handler_serves_mcp_descriptors(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        result = await bridge._official_list_tools(None, None)
        assert isinstance(result, mcp_types.ListToolsResult)
        wire = result.model_dump(by_alias=True, mode="json", exclude_none=True)
        validate_mcp("ListToolsResult", wire)
        descriptor = next(tool for tool in wire["tools"] if tool["name"] == "astral_get_operation")
        assert descriptor["inputSchema"]["required"] == ["operation_id"]
        assert "parameters" not in descriptor
    finally:
        bridge.close()


async def test_official_call_tool_handler_wraps_success_and_failures(fake_server, validate_mcp):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        params = mcp_types.CallToolRequestParams(
            _meta=META, name="astral_submit_operation",
            arguments={"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"})
        success = await bridge._official_call_tool(None, params)
        assert isinstance(success, mcp_types.CallToolResult)
        wire = success.model_dump(by_alias=True, mode="json", exclude_none=True)
        validate_mcp("CallToolResult", wire)
        assert wire["isError"] is False
        assert wire["structuredContent"]["created"] is True

        missing = await bridge._official_call_tool(None, mcp_types.CallToolRequestParams(
            _meta=META, name="astral_get_operation", arguments={"operation_id": str(uuid.uuid4())}))
        assert missing.is_error is True
        assert missing.content[0].text == "work_not_found"

        invalid = await bridge._official_call_tool(None, mcp_types.CallToolRequestParams(
            _meta=META, name="astral_get_operation", arguments={}))
        assert invalid.is_error is True
        assert "operation_id" in invalid.content[0].text

        bare = await bridge._official_call_tool(None, mcp_types.CallToolRequestParams(
            _meta=META, name="astral_list_operations"))
        assert bare.is_error is False
        assert "operations" in bare.structured_content
    finally:
        bridge.close()


def test_serve_with_official_sdk_builds_and_runs_the_server(fake_server, monkeypatch):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    ran = []
    monkeypatch.setattr("asyncio.run", lambda coroutine: ran.append(coroutine))
    try:
        bridge.serve_with_official_sdk()
    finally:
        bridge.close()
    assert len(ran) == 1
    server = bridge._official_server()
    assert server.name == "astral-sdk-bridge"


def test_serve_with_official_sdk_requires_the_optional_package(fake_server, monkeypatch):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", None)
    try:
        with pytest.raises(ImportError, match="astral-sdk\\[mcp\\]"):
            bridge.serve_with_official_sdk()
    finally:
        bridge.close()


def test_lightweight_unexpected_dispatch_failure_is_a_bounded_result(fake_server):
    bridge = Bridge(AstralClient.__new__(AstralClient))
    response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"_meta": META, "name": "astral_get_operation",
                                         "arguments": {"operation_id": "x"}}})
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "tool execution failed"


def test_module_main_reads_env_and_serves_stdio(fake_server, monkeypatch):
    monkeypatch.setenv("ASTRAL_BASE_URL", fake_server.base_url)
    monkeypatch.setenv("ASTRAL_TOKEN", fake_server.state.valid_token)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}}) + "\n"))
    out_stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", out_stream)
    from astral_sdk import mcp_bridge

    assert mcp_bridge._main() == 0
    parsed = json.loads(out_stream.getvalue().strip())
    assert parsed["result"]["resultType"] == "complete"


def test_module_main_refuses_missing_configuration(monkeypatch):
    monkeypatch.delenv("ASTRAL_BASE_URL", raising=False)
    monkeypatch.delenv("ASTRAL_TOKEN", raising=False)
    from astral_sdk import mcp_bridge

    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    assert mcp_bridge._main() == 1
    assert json.loads(stderr.getvalue())["error"]
