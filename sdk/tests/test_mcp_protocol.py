"""Tests for astral_sdk.mcp_protocol: descriptor and result adapters, envelope
validation rules, and error-code helpers shared by both bridge serving paths.
"""

from __future__ import annotations

import json

import pytest

from astral_sdk.mcp_protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    UNSUPPORTED_PROTOCOL_VERSION,
    SERVER_VERSION,
    ProtocolError,
    discover_result,
    error_response,
    negotiated_call_arguments,
    require_request_envelope,
    server_info,
    tool_descriptor,
    tool_descriptors,
    tool_error_result,
    tool_success_result,
    tools_list_result,
    valid_request_id,
)
from astral_sdk.tools import ASTRAL_TOOLS, MCP_PROTOCOL_VERSION, TOOL_NAMES

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def test_tool_descriptors_cover_the_catalog_with_mcp_schemas(validate_mcp):
    descriptors = tool_descriptors()
    assert [descriptor["name"] for descriptor in descriptors] == list(TOOL_NAMES)
    for descriptor in descriptors:
        validate_mcp("Tool", descriptor)
        spec = ASTRAL_TOOLS[descriptor["name"]]
        assert descriptor["description"] == spec["description"]
        assert descriptor["inputSchema"] == {**spec["input_schema"],
                                            "$schema": "https://json-schema.org/draft/2020-12/schema"}
        assert "parameters" not in descriptor
        assert descriptor["annotations"] == {
            "readOnlyHint": bool(spec["read_only"]), "destructiveHint": False}
        assert descriptor["_meta"] == {
            "astral/agentId": "__work__", "astral/requiredScope": spec["scope"]}


def test_tool_descriptor_is_isolated_from_the_contract():
    descriptor = tool_descriptor("astral_get_operation")
    descriptor["inputSchema"]["$schema"] = "mutated"
    assert tool_descriptor("astral_get_operation")["inputSchema"]["$schema"] != "mutated"


def test_tool_descriptor_defaults_a_non_object_schema(monkeypatch):
    from astral_sdk import mcp_protocol

    monkeypatch.setattr(mcp_protocol, "ASTRAL_TOOLS",
                        {"astral_x": {"description": "d", "scope": "s",
                                      "read_only": True, "input_schema": "broken"}})
    descriptor = tool_descriptor("astral_x")
    assert descriptor["inputSchema"] == {"type": "object", "properties": {},
                                         "$schema": "https://json-schema.org/draft/2020-12/schema"}


def test_discover_result_matches_the_declared_version(validate_mcp):
    result = discover_result()
    validate_mcp("DiscoverResult", result)
    assert result["supportedVersions"] == [MCP_PROTOCOL_VERSION]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 60000
    assert result["cacheScope"] == "private"
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == server_info()


def test_tools_list_result_carries_the_descriptors(validate_mcp):
    result = tools_list_result()
    validate_mcp("ListToolsResult", result)
    assert len(result["tools"]) == len(TOOL_NAMES)


def test_tool_success_result_wraps_structured_content(validate_mcp):
    structured = {"id": "op-1", "revision": 2}
    result = tool_success_result(structured)
    validate_mcp("CallToolResult", result)
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert result["structuredContent"] == structured
    assert json.loads(result["content"][0]["text"]) == structured
    assert result["content"][0]["type"] == "text"


def test_tool_result_text_is_bounded():
    huge = {"blob": "x" * 200_000}
    result = tool_success_result(huge)
    assert len(result["content"][0]["text"]) == 65_536
    failure = tool_error_result("y" * 200_000)
    assert len(failure["content"][0]["text"]) == 65_536


def test_tool_error_result_is_a_completed_error(validate_mcp):
    result = tool_error_result("work_not_found")
    validate_mcp("CallToolResult", result)
    assert result["isError"] is True
    assert result["structuredContent"] == {}
    assert result["content"][0]["text"] == "work_not_found"


def test_error_response_includes_data_only_when_present():
    bare = error_response(7, ProtocolError(INVALID_REQUEST, "bad"))
    assert bare == {"jsonrpc": "2.0", "id": 7,
                    "error": {"code": -32600, "message": "bad"}}
    rich = error_response("abc", ProtocolError(
        UNSUPPORTED_PROTOCOL_VERSION, "no",
        data={"requested": "1.0", "supported": ["2026-07-28"]}))
    assert rich["id"] == "abc"
    assert rich["error"]["data"] == {"requested": "1.0", "supported": ["2026-07-28"]}


def test_require_request_envelope_accepts_well_formed_params():
    assert require_request_envelope({"_meta": META, "name": "x"}) == {"_meta": META, "name": "x"}


@pytest.mark.parametrize("params", [
    None,
    "params",
    {},
    {"_meta": "meta"},
    {"_meta": {}},
    {"_meta": {"io.modelcontextprotocol/protocolVersion": 2026}},
    {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    {"_meta": {**META, "io.modelcontextprotocol/clientCapabilities": None}},
])
def test_require_request_envelope_rejects_malformed_metadata(params):
    with pytest.raises(ProtocolError) as raised:
        require_request_envelope(params)
    assert raised.value.code == INVALID_PARAMS


def test_require_request_envelope_refuses_unsupported_versions():
    with pytest.raises(ProtocolError) as raised:
        require_request_envelope({"_meta": {
            "io.modelcontextprotocol/protocolVersion": "2025-06-18",
            "io.modelcontextprotocol/clientCapabilities": {}}})
    assert raised.value.code == UNSUPPORTED_PROTOCOL_VERSION
    assert raised.value.data == {"requested": "2025-06-18", "supported": ["2026-07-28"]}


@pytest.mark.parametrize("params,expected", [
    ({"_meta": META, "name": "astral_get_operation", "arguments": {"operation_id": "op"}},
     ("astral_get_operation", {"operation_id": "op"})),
    ({"_meta": META, "name": "astral_list_operations"}, ("astral_list_operations", {})),
])
def test_negotiated_call_arguments_accepts_well_formed_calls(params, expected):
    assert negotiated_call_arguments(params) == expected


@pytest.mark.parametrize("params", [
    {},
    {"name": ""},
    {"name": 7},
    {"name": "astral_get_operation", "arguments": 5},
])
def test_negotiated_call_arguments_rejects_malformed_calls(params):
    with pytest.raises(ProtocolError) as raised:
        negotiated_call_arguments(params)
    assert raised.value.code == INVALID_PARAMS


@pytest.mark.parametrize("request_id,valid", [
    ("abc", True),
    (17, True),
    (False, False),
    (None, False),
    (1.5, False),
    ([1], False),
])
def test_valid_request_id_shape(request_id, valid):
    assert valid_request_id(request_id) is valid


def test_server_info_is_self_describing():
    info = server_info()
    assert info["name"] == "astral-sdk-bridge"
    assert info["version"] == SERVER_VERSION
