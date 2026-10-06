"""MCP 2026-07-28 protocol fixtures for the Bridge: a standards-aware stdio host
pins discovery, tool descriptors, result envelopes, error mapping and the
per-request _meta lifecycle to the wire shapes the specification requires.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import uuid

import pytest

from astral_sdk.errors import AstralHTTPError
from astral_sdk.mcp_bridge import Bridge, _official_handlers
from astral_sdk.tools import ASTRAL_TOOLS, CONTRACT, function_schema

PROTOCOL_VERSION = CONTRACT["mcp"]["protocol_version"]
SUPPORTED_VERSIONS = tuple(CONTRACT["mcp"]["supported_protocol_versions"])

_BRIDGE_CHILD = (
    "import sys;"
    "from astral_sdk.mcp_bridge import Bridge;"
    "bridge = Bridge.connect(sys.argv[1], sys.argv[2]);"
    "bridge.serve_stdio();"
    "bridge.close()"
)


def _meta(protocol_version: str = PROTOCOL_VERSION, *, omit_required: str | None = None) -> dict:
    meta = {}
    if omit_required != "protocolVersion":
        meta["io.modelcontextprotocol/protocolVersion"] = protocol_version
    if omit_required != "clientCapabilities":
        meta["io.modelcontextprotocol/clientCapabilities"] = {}
    return meta


def _request(method: str, *, params: dict | None = None, request_id: str | int = 1,
             meta: dict | None = "default") -> dict:
    filled = dict(params or {})
    if meta == "default":
        filled["_meta"] = _meta()
    elif meta is not None:
        filled["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": filled}


def _served_lines(bridge: Bridge, *messages: dict) -> list[dict]:
    in_stream = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
    out_stream = io.StringIO()
    bridge.serve_stdio(in_stream, out_stream)
    return [json.loads(line) for line in out_stream.getvalue().splitlines() if line.strip()]


def _bridge(fake_server) -> Bridge:
    return Bridge.connect(fake_server.base_url, fake_server.state.valid_token)


def _assert_complete_envelope(result: dict, *, exact_keys: set[str]) -> None:
    assert result["resultType"] == "complete"
    assert set(result) == exact_keys


def test_invalid_request_envelopes_are_rejected_without_dispatch(fake_server):
    bridge = _bridge(fake_server)
    try:
        method_not_string = {"jsonrpc": "2.0", "id": 1, "method": 123}
        params_not_object = _request("tools/list", request_id=2)
        params_not_object["params"] = "params"
        null_params = _request("tools/list", request_id=3)
        null_params["params"] = None
        valid = _request("tools/list", request_id=4)
        responses = _served_lines(bridge, method_not_string, params_not_object, null_params, valid)
    finally:
        bridge.close()
    assert [response["error"]["code"] for response in responses[:3]] == [-32600, -32600, -32602]
    assert responses[3]["id"] == 4


def test_malformed_meta_objects_are_invalid_params(fake_server):
    bridge = _bridge(fake_server)
    try:
        meta_not_object = _request("tools/list", meta="nope", request_id=1)
        version_not_string = _request("tools/list", meta={
            "io.modelcontextprotocol/protocolVersion": 123,
            "io.modelcontextprotocol/clientCapabilities": {}}, request_id=2)
        capabilities_not_object = _request("tools/list", meta={
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": "nope"}, request_id=3)
        responses = _served_lines(bridge, meta_not_object, version_not_string, capabilities_not_object)
    finally:
        bridge.close()
    assert [response["error"]["code"] for response in responses] == [-32602] * 3


def test_tool_call_params_shape_is_validated(fake_server):
    bridge = _bridge(fake_server)
    try:
        name_not_string = _request("tools/call", params={"name": 7, "arguments": {}}, request_id=1)
        arguments_not_object = _request("tools/call", params={"name": "astral_get_operation",
                                                              "arguments": "arguments"}, request_id=2)
        missing_arguments_key = _request("tools/call", params={"name": "astral_no_such_tool"}, request_id=3)
        responses = _served_lines(bridge, name_not_string, arguments_not_object, missing_arguments_key)
    finally:
        bridge.close()
    assert [response["error"]["code"] for response in responses] == [-32602] * 3


def test_unexpected_dispatch_failure_is_a_bounded_internal_error(fake_server, monkeypatch):
    bridge = _bridge(fake_server)
    monkeypatch.setattr(bridge._client, "poll_operation", _synthetic_crash)
    try:
        request = _request("tools/call", params={"name": "astral_get_operation_events",
                                                 "arguments": {"operation_id": "op-1"}}, request_id=1)
        response = _served_lines(bridge, request)[0]
    finally:
        bridge.close()
    assert response["error"]["code"] == -32603
    assert "synthetic dispatch crash" in response["error"]["message"]


def _synthetic_crash(*args, **kwargs):
    raise RuntimeError("synthetic dispatch crash")


def test_cancellation_with_invalid_request_id_is_ignored(fake_server):
    bridge = _bridge(fake_server)
    try:
        invalid = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": True}}
        survivor = _request("tools/list", request_id="after-invalid")
        responses = _served_lines(bridge, invalid, survivor)
    finally:
        bridge.close()
    assert [response["id"] for response in responses] == ["after-invalid"]


def test_unknown_cancellation_is_ignored_and_the_id_stays_usable(fake_server):
    bridge = _bridge(fake_server)
    try:
        cancellation = {"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": "req-7", "reason": "host timeout"}}
        later = _request("tools/list", request_id="req-7")
        responses = _served_lines(bridge, cancellation, later)
    finally:
        bridge.close()
    assert [response["id"] for response in responses] == ["req-7"]
    assert responses[0]["result"]["resultType"] == "complete"


def test_cancellation_after_the_response_was_sent_is_ignored(fake_server):
    bridge = _bridge(fake_server)
    try:
        first = _request("tools/list", request_id="req-1")
        late_cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled",
                       "params": {"requestId": "req-1"}}
        second = _request("tools/list", request_id="req-2")
        responses = _served_lines(bridge, first, late_cancel, second)
    finally:
        bridge.close()
    assert [response["id"] for response in responses] == ["req-1", "req-2"]


def _piped_stdio_lines(fake_server, monkeypatch, script, delay):
    import os
    import time

    read_fd, write_fd = os.pipe()
    reader = io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0), encoding="utf-8")
    writer = io.TextIOWrapper(os.fdopen(write_fd, "wb", buffering=0), encoding="utf-8")

    def slow_get_operation(operation_id, *args, **kwargs):
        time.sleep(delay)
        raise AstralHTTPError("work_not_found", code="work_not_found")

    bridge = _bridge(fake_server)
    monkeypatch.setattr(bridge._client, "get_operation", slow_get_operation)

    def _feed():
        for line in script:
            writer.write(line + "\n")
            writer.flush()
            time.sleep(0.02)
        time.sleep(delay * 2)
        writer.close()

    import threading
    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        out = io.StringIO()
        bridge.serve_stdio(reader, out)
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    finally:
        bridge.close()
        reader.close()


def test_cancellation_during_dispatch_suppresses_only_that_response(fake_server, monkeypatch):
    script = [
        json.dumps(_request("tools/call", params={"name": "astral_get_operation",
                                                  "arguments": {"operation_id": "op-1"}}, request_id="req-7")),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "req-7"}}),
        json.dumps(_request("tools/list", request_id="req-8")),
    ]
    responses = _piped_stdio_lines(fake_server, monkeypatch, script, delay=0.4)
    assert [response["id"] for response in responses] == ["req-8"]
    assert responses[0]["result"]["resultType"] == "complete"


def test_cancellation_for_another_id_during_dispatch_is_ignored(fake_server, monkeypatch):
    script = [
        json.dumps(_request("tools/call", params={"name": "astral_get_operation",
                                                  "arguments": {"operation_id": "op-1"}}, request_id="req-7")),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "other"}}),
    ]
    responses = _piped_stdio_lines(fake_server, monkeypatch, script, delay=0.3)
    assert [response["id"] for response in responses] == ["req-7"]
    assert responses[0]["result"]["isError"] is True


def test_fractional_request_id_is_rejected_as_malformed(fake_server):
    bridge = _bridge(fake_server)
    try:
        fractional = {"jsonrpc": "2.0", "id": 1.5, "method": "server/discover",
                      "params": {"_meta": _meta()}}
        response = _served_lines(bridge, fractional)[0]
    finally:
        bridge.close()
    assert response["error"]["code"] == -32600
    assert "result" not in response


def test_discover_reports_versions_capabilities_and_server_info(fake_server):
    bridge = _bridge(fake_server)
    try:
        response = _served_lines(bridge, _request("server/discover"))[0]
    finally:
        bridge.close()
    result = response["result"]
    _assert_complete_envelope(result, exact_keys={"resultType", "supportedVersions", "capabilities", "_meta"})
    assert result["supportedVersions"] == list(SUPPORTED_VERSIONS)
    assert "tools" in result["capabilities"]
    server_info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert server_info["name"] == "astral-sdk-bridge"
    assert isinstance(server_info["version"], str) and server_info["version"]


def test_tools_list_serves_mcp_descriptors_with_input_schema(fake_server):
    bridge = _bridge(fake_server)
    try:
        response = _served_lines(bridge, _request("tools/list"))[0]
    finally:
        bridge.close()
    result = response["result"]
    _assert_complete_envelope(result, exact_keys={"resultType", "tools"})
    assert {tool["name"] for tool in result["tools"]} == set(CONTRACT["dispatchable_tool_names"])
    for tool in result["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["inputSchema"] == ASTRAL_TOOLS[tool["name"]]["input_schema"]
        assert tool["inputSchema"].get("type") == "object"


def test_tools_call_success_is_a_complete_content_envelope(fake_server):
    bridge = _bridge(fake_server)
    try:
        request = _request("tools/call", params={"name": "astral_submit_operation",
            "arguments": {"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"}},
            request_id="call-1")
        response = _served_lines(bridge, request)[0]
    finally:
        bridge.close()
    result = response["result"]
    _assert_complete_envelope(result, exact_keys={"resultType", "content", "structuredContent", "isError"})
    assert response["id"] == "call-1"
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": json.dumps(result["structuredContent"])}]
    assert result["structuredContent"]["created"] is True


def test_unknown_tool_is_invalid_params_not_internal_error(fake_server):
    bridge = _bridge(fake_server)
    try:
        request = _request("tools/call", params={"name": "astral_no_such_tool", "arguments": {}})
        response = _served_lines(bridge, request)[0]
    finally:
        bridge.close()
    assert response["error"]["code"] == -32602


def test_missing_required_argument_is_invalid_params_and_the_loop_survives(fake_server):
    bridge = _bridge(fake_server)
    try:
        broken = _request("tools/call", params={"name": "astral_get_operation", "arguments": {}}, request_id=1)
        followup = _request("tools/list", request_id=2)
        responses = _served_lines(bridge, broken, followup)
    finally:
        bridge.close()
    assert responses[0]["error"]["code"] == -32602
    assert responses[1]["id"] == 2
    assert responses[1]["result"]["resultType"] == "complete"


def test_upstream_tool_failure_is_a_tool_execution_error(fake_server):
    bridge = _bridge(fake_server)
    try:
        request = _request("tools/call", params={"name": "astral_get_operation",
            "arguments": {"operation_id": str(uuid.uuid4())}})
        response = _served_lines(bridge, request)[0]
    finally:
        bridge.close()
    assert "error" not in response
    result = response["result"]
    _assert_complete_envelope(result, exact_keys={"resultType", "content", "structuredContent", "isError"})
    assert result["isError"] is True
    assert "work_not_found" in result["content"][0]["text"]
    assert result["structuredContent"] == {"code": "work_not_found"}


def test_request_without_required_meta_is_invalid_params(fake_server):
    bridge = _bridge(fake_server)
    try:
        response = _served_lines(bridge, _request("tools/list", meta=None))[0]
    finally:
        bridge.close()
    assert response["error"]["code"] == -32602


def test_request_missing_client_capabilities_is_invalid_params(fake_server):
    bridge = _bridge(fake_server)
    try:
        response = _served_lines(bridge, _request("tools/list", meta=_meta(omit_required="clientCapabilities")))[0]
    finally:
        bridge.close()
    assert response["error"]["code"] == -32602


def test_unsupported_protocol_version_lists_supported_ones(fake_server):
    bridge = _bridge(fake_server)
    try:
        response = _served_lines(bridge, _request("tools/list", meta=_meta("1900-01-01")))[0]
    finally:
        bridge.close()
    error = response["error"]
    assert error["code"] == -32022
    assert error["data"]["supported"] == list(SUPPORTED_VERSIONS)
    assert error["data"]["requested"] == "1900-01-01"


def test_initialize_handshake_is_rejected_naming_supported_versions(fake_server):
    bridge = _bridge(fake_server)
    try:
        legacy = {"jsonrpc": "2.0", "id": "legacy-1", "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                             "clientInfo": {"name": "legacy-host", "version": "1.0"}}}
        response = _served_lines(bridge, legacy)[0]
    finally:
        bridge.close()
    assert "result" not in response
    assert response["id"] == "legacy-1"
    assert response["error"]["data"]["supported"] == list(SUPPORTED_VERSIONS)


def test_malformed_envelope_lines_do_not_terminate_the_stdio_loop(fake_server):
    bridge = _bridge(fake_server)
    try:
        scalar = 42
        array = [1, 2]
        wrong_version = {"jsonrpc": "1.0", "id": 5, "method": "tools/list"}
        no_method = {"jsonrpc": "2.0", "id": 6}
        null_id = {"jsonrpc": "2.0", "id": None, "method": "tools/list"}
        good = _request("tools/list", request_id=7)
        responses = _served_lines(bridge, scalar, array, wrong_version, no_method, null_id, good)
    finally:
        bridge.close()
    assert [response["error"]["code"] for response in responses[:5]] == [-32600] * 5
    assert responses[5]["id"] == 7
    assert responses[5]["result"]["resultType"] == "complete"


def test_malformed_cancellation_notification_is_ignored(fake_server):
    bridge = _bridge(fake_server)
    try:
        malformed = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}
        followup = _request("tools/list", request_id="ok-1")
        responses = _served_lines(bridge, malformed, followup)
    finally:
        bridge.close()
    assert [response["id"] for response in responses] == ["ok-1"]


def test_notification_methods_never_get_a_response(fake_server):
    bridge = _bridge(fake_server)
    try:
        initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert _served_lines(bridge, initialized) == []
    finally:
        bridge.close()


def test_identical_tools_list_requests_return_identical_results(fake_server):
    bridge = _bridge(fake_server)
    try:
        first = _served_lines(bridge, _request("tools/list", request_id=1))[0]
        second = _served_lines(bridge, _request("tools/list", request_id=2))[0]
    finally:
        bridge.close()
    assert first["result"] == second["result"]


def test_framework_function_schemas_keep_their_parameters_shape():
    schema = function_schema("astral_get_operation")
    assert set(schema) == {"name", "description", "parameters"}
    assert "inputSchema" not in schema


def test_stdio_host_discovers_lists_and_calls_over_a_real_subprocess(fake_server):
    requests = [
        _request("server/discover", request_id="d-1"),
        _request("tools/list", request_id="l-1"),
        _request("tools/call", params={"name": "astral_submit_operation",
            "arguments": {"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"}},
            request_id="c-1"),
    ]
    run = subprocess.run(
        [sys.executable, "-c", _BRIDGE_CHILD, fake_server.base_url, fake_server.state.valid_token],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert run.returncode == 0
    lines = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    assert [response["id"] for response in lines] == ["d-1", "l-1", "c-1"]
    assert lines[0]["result"]["supportedVersions"] == list(SUPPORTED_VERSIONS)
    assert all("inputSchema" in tool for tool in lines[1]["result"]["tools"])
    call_result = lines[2]["result"]
    assert call_result["isError"] is False
    submitted_id = call_result["structuredContent"]["id"]
    assert submitted_id


def test_stdio_host_reads_the_submitted_operation_back(fake_server):
    submit = _request("tools/call", params={"name": "astral_submit_operation",
        "arguments": {"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"}},
        request_id="s-1")
    run = subprocess.run(
        [sys.executable, "-c", _BRIDGE_CHILD, fake_server.base_url, fake_server.state.valid_token],
        input=json.dumps(submit) + "\n",
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert run.returncode == 0
    submitted = json.loads(run.stdout.splitlines()[0])["result"]["structuredContent"]
    readback = _request("tools/call", params={"name": "astral_get_operation",
        "arguments": {"operation_id": submitted["id"]}}, request_id="g-1")
    run = subprocess.run(
        [sys.executable, "-c", _BRIDGE_CHILD, fake_server.base_url, fake_server.state.valid_token],
        input=json.dumps(readback) + "\n",
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert run.returncode == 0
    result = json.loads(run.stdout.splitlines()[0])["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["id"] == submitted["id"]
    assert result["structuredContent"]["title"] == "A"


def test_official_sdk_handlers_serve_the_same_protocol_shapes(fake_server):
    pytest.importorskip("mcp", reason="the official mcp package is an optional extra")
    from mcp import types

    bridge = _bridge(fake_server)
    try:
        list_tools, call_tool = _official_handlers(bridge)
        import asyncio
        listing = asyncio.run(list_tools(None, None))
        assert [tool.name for tool in listing.tools] == list(CONTRACT["dispatchable_tool_names"])
        for tool in listing.tools:
            assert tool.input_schema == ASTRAL_TOOLS[tool.name]["input_schema"]
        arguments = {"idempotency_key": str(uuid.uuid4()), "name": "A", "instructions": "B"}
        params = types.CallToolRequestParams(name="astral_submit_operation", arguments=arguments)
        result = asyncio.run(call_tool(None, params))
        assert result.is_error is False
        assert result.result_type == "complete"
        assert result.structured_content["created"] is True
        assert result.content[0].text == json.dumps(result.structured_content)
        unknown = types.CallToolRequestParams(name="astral_no_such_tool", arguments={})
        with pytest.raises(Exception) as exc_info:
            asyncio.run(call_tool(None, unknown))
        assert exc_info.value.code == -32602
    finally:
        bridge.close()


def test_official_sdk_upstream_failure_is_a_tool_execution_error(fake_server):
    pytest.importorskip("mcp", reason="the official mcp package is an optional extra")
    from mcp import types

    bridge = _bridge(fake_server)
    try:
        _list_tools, call_tool = _official_handlers(bridge)
        import asyncio
        params = types.CallToolRequestParams(name="astral_get_operation",
                                             arguments={"operation_id": str(uuid.uuid4())})
        result = asyncio.run(call_tool(None, params))
        assert result.is_error is True
        assert "work_not_found" in result.content[0].text
        assert result.structured_content == {"code": "work_not_found"}
    finally:
        bridge.close()
