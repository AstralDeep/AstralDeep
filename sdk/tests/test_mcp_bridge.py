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


def _gated_bridge(fake_server, monkeypatch):
    import threading
    from astral_sdk.errors import AstralHTTPError

    release = threading.Event()
    dequeued = threading.Event()

    def gated_get_operation(operation_id, *args, **kwargs):
        dequeued.set()
        release.wait(timeout=30)
        raise AstralHTTPError("work_not_found", code="work_not_found")

    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    monkeypatch.setattr(bridge._client, "get_operation", gated_get_operation)
    return bridge, release, dequeued


def _feed_lines(bridge, script, release, dequeued):
    import os
    import threading
    import time

    read_fd, write_fd = os.pipe()
    reader = io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0), encoding="utf-8")
    writer = io.TextIOWrapper(os.fdopen(write_fd, "wb", buffering=0), encoding="utf-8")

    def _feed():
        for chunk in script:
            kind, payload = chunk
            if kind == "WAIT_DEQUEUED":
                assert dequeued.wait(timeout=10)
                continue
            writer.write(payload + "\n")
            writer.flush()
            time.sleep(0.005)
        time.sleep(0.2)
        release.set()
        time.sleep(0.5)
        writer.close()

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        out = io.StringIO()
        bridge.serve_stdio(reader, out)
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    finally:
        bridge.close()
        reader.close()


def test_request_intake_is_bounded_while_dispatch_is_blocked(fake_server, monkeypatch):
    bridge, release, dequeued = _gated_bridge(fake_server, monkeypatch)
    script = [("RAW", json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                                 "params": {"_meta": META, "name": "astral_get_operation",
                                            "arguments": {"operation_id": "op-1"}}})),
              ("WAIT_DEQUEUED", None)]
    script += [("RAW", json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/list",
                                  "params": {"_meta": META}}))
               for index in range(1, 301)]
    responses = _feed_lines(bridge, script, release, dequeued)
    overload = [response for response in responses if "error" in response]
    answered = [response for response in responses if "result" in response]
    assert {response["id"] for response in overload} == set(range(257, 301))
    assert all(response["error"]["code"] == -32603 for response in overload)
    assert len(answered) == 257
    assert {response["id"] for response in answered} == {0, *range(1, 257)}
    assert bridge._stdio_queue is not None
    assert bridge._stdio_queue.maxsize <= 257


def test_cancellation_is_processed_while_intake_is_exhausted(fake_server, monkeypatch):
    bridge, release, dequeued = _gated_bridge(fake_server, monkeypatch)
    script = [("RAW", json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                                 "params": {"_meta": META, "name": "astral_get_operation",
                                            "arguments": {"operation_id": "op-1"}}})),
              ("WAIT_DEQUEUED", None)]
    script += [("RAW", json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/list",
                                  "params": {"_meta": META}}))
               for index in range(1, 258)]
    script.append(("RAW", json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                      "params": {"requestId": 0}})))
    responses = _feed_lines(bridge, script, release, dequeued)
    answered_ids = {response["id"] for response in responses if "result" in response}
    assert 0 not in answered_ids
    assert answered_ids == set(range(1, 257))
    overload = [response for response in responses if "error" in response]
    assert {response["id"] for response in overload} == {257}


def test_oversized_frame_is_rejected_and_the_loop_survives(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        oversized = "x" * (2 ** 20 + 1)
        followup = json.dumps({"jsonrpc": "2.0", "id": "after-oversize", "method": "tools/list",
                               "params": {"_meta": META}})
        in_stream = io.StringIO(oversized + "\n" + followup + "\n")
        out_stream = io.StringIO()
        bridge.serve_stdio(in_stream, out_stream)
        lines = [json.loads(line) for line in out_stream.getvalue().splitlines() if line.strip()]
        assert lines[0]["error"]["code"] == -32600
        assert "frame exceeds the maximum line size" in lines[0]["error"]["message"]
        assert lines[1]["id"] == "after-oversize"
        assert lines[1]["result"]["resultType"] == "complete"
    finally:
        bridge.close()


def test_rejected_and_oversized_records_do_not_buffer_in_the_reader_queue(fake_server, monkeypatch):
    import os
    import threading
    import time

    bridge, release, dequeued = _gated_bridge(fake_server, monkeypatch)

    read_fd, write_fd = os.pipe()
    reader = io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0), encoding="utf-8")
    writer = io.TextIOWrapper(os.fdopen(write_fd, "wb", buffering=0), encoding="utf-8")

    oversized = "x" * (2 ** 20 + 1)

    def _feed():
        first = json.dumps({"jsonrpc": "2.0", "id": "blocker", "method": "tools/call",
                            "params": {"_meta": META, "name": "astral_get_operation",
                                       "arguments": {"operation_id": "op-block"}}})
        writer.write(first + "\n")
        writer.flush()
        assert dequeued.wait(timeout=10)

        for _ in range(256):
            writer.write(json.dumps({"jsonrpc": "2.0", "id": f"q-{_}", "method": "tools/list",
                                     "params": {"_meta": META}}) + "\n")
        writer.flush()
        time.sleep(0.1)

        for _ in range(5000):
            writer.write(oversized + "\n")
            writer.write("not json\n")
        writer.write(json.dumps({"jsonrpc": "2.0", "id": "overflow", "method": "tools/list",
                                 "params": {"_meta": META}}) + "\n")
        writer.flush()

        time.sleep(0.3)
        assert bridge._stdio_queue is not None
        assert bridge._stdio_queue.qsize() <= 256
        assert bridge._stdio_queue.maxsize <= 257

        release.set()
        time.sleep(0.5)
        writer.close()

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        out = io.StringIO()
        bridge.serve_stdio(reader, out)
        responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        assert len(responses) == 1 + 256 + 10001
    finally:
        bridge.close()
        reader.close()


def test_eof_never_blocks_when_queue_is_full(fake_server, monkeypatch):
    bridge, release, dequeued = _gated_bridge(fake_server, monkeypatch)
    script = [("RAW", json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                                 "params": {"_meta": META, "name": "astral_get_operation",
                                            "arguments": {"operation_id": "op-1"}}})),
              ("WAIT_DEQUEUED", None)]
    script += [("RAW", json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/list",
                                  "params": {"_meta": META}}))
               for index in range(1, 257)]
    responses = _feed_lines(bridge, script, release, dequeued)
    answered = [r for r in responses if "result" in r]
    assert len(answered) == 257


def test_cancellation_latency_under_saturated_queue(fake_server, monkeypatch):
    import os
    import threading
    import time

    bridge, release, dequeued = _gated_bridge(fake_server, monkeypatch)

    read_fd, write_fd = os.pipe()
    reader = io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0), encoding="utf-8")
    writer = io.TextIOWrapper(os.fdopen(write_fd, "wb", buffering=0), encoding="utf-8")

    def _feed():
        writer.write(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                                "params": {"_meta": META, "name": "astral_get_operation",
                                           "arguments": {"operation_id": "op-1"}}}) + "\n")
        writer.flush()
        assert dequeued.wait(timeout=10)
        for i in range(1, 257):
            writer.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/list",
                                     "params": {"_meta": META}}) + "\n")
        writer.flush()
        time.sleep(0.05)
        writer.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                 "params": {"requestId": 0}}) + "\n")
        writer.flush()
        time.sleep(0.05)
        release.set()
        time.sleep(0.5)
        writer.close()

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        out = io.StringIO()
        bridge.serve_stdio(reader, out)
        responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        answered_ids = {r["id"] for r in responses if "result" in r}
        assert 0 not in answered_ids
        assert answered_ids == set(range(1, 257))
    finally:
        bridge.close()
        reader.close()


def test_missing_submit_fields_are_input_validation_errors(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    try:
        request = {
            "jsonrpc": "2.0", "id": "sub-1", "method": "tools/call",
            "params": {"_meta": META, "name": "astral_submit_operation", "arguments": {}},
        }
        response = bridge.handle(request)
        assert response["id"] == "sub-1"
        assert response["result"]["isError"] is True
        assert "missing required argument(s)" in response["result"]["content"][0]["text"]
        assert "idempotency_key" in response["result"]["content"][0]["text"]
        assert "-32603" not in json.dumps(response)
    finally:
        bridge.close()


def test_request_tracker_lifecycle_transitions_are_isolated_per_finish():
    from astral_sdk.mcp_bridge import _RequestTracker

    tracker = _RequestTracker()
    tracker.register(1)
    tracker.register(2)
    tracker.cancel(1)
    assert tracker.finish(1) is True
    assert tracker.finish(1) is False
    assert tracker.finish(2) is False
    tracker.register(1)
    assert tracker.finish(1) is False
