"""Tests stdio cancellation, raw-frame bounds and output failures through the SDK bridge.
The existing local HTTP server verifies queued tool effects while events control transport ordering.
"""

from __future__ import annotations

import io
import json
import threading

import pytest

from astral_sdk.mcp_bridge import Bridge, _MAX_LINE_CHARS, _RequestTracker

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _tool_request(request_id, name, arguments):
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"_meta": META, "name": name, "arguments": arguments},
    }) + "\n"


class _OrderedInput(io.StringIO):
    def __init__(self, payload, blocked, at_eof=lambda: None):
        super().__init__(payload)
        self._first_boundary = payload.index("\n") + 1
        self._blocked = blocked
        self._at_eof = at_eof

    def readline(self, size=-1):
        if self.tell() == self._first_boundary:
            assert self._blocked.wait(timeout=10)
        line = super().readline(size)
        if not line:
            self._at_eof()
        return line

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line


def _queued_submit(fake_server, monkeypatch, *, cancelled):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    blocked = threading.Event()
    release = threading.Event()
    original_get = bridge._client.get_operation

    def block_read(*args, **kwargs):
        blocked.set()
        assert release.wait(timeout=10)
        return original_get(*args, **kwargs)

    monkeypatch.setattr(bridge._client, "get_operation", block_read)
    payload = _tool_request(0, "astral_get_operation", {"operation_id": "blocker"})
    payload += _tool_request(1, "astral_submit_operation", {
        "idempotency_key": "queued-submission", "name": "Queued", "instructions": "Synthetic instructions",
    })
    if cancelled:
        payload += json.dumps({
            "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1},
        }) + "\n"
    incoming = _OrderedInput(payload, blocked, release.set)
    outgoing = io.StringIO()
    try:
        bridge.serve_stdio(incoming, outgoing)
        return [json.loads(line) for line in outgoing.getvalue().splitlines()]
    finally:
        release.set()
        incoming.close()
        outgoing.close()
        bridge.close()


def test_uncancelled_queued_submission_reaches_the_http_server(fake_server, monkeypatch):
    responses = _queued_submit(fake_server, monkeypatch, cancelled=False)
    assert [response["id"] for response in responses] == [0, 1]
    assert responses[0]["result"]["isError"] is True
    assert responses[1]["result"]["structuredContent"]["created"] is True
    assert len(fake_server.state.operations) == 1
    assert [request["body"]["params"]["name"] for request in fake_server.state.requests] == [
        "astral_get_operation", "astral_submit_operation",
    ]


def test_cancelled_queued_submission_never_reaches_the_http_server(fake_server, monkeypatch):
    responses = _queued_submit(fake_server, monkeypatch, cancelled=True)
    assert not fake_server.state.operations
    assert [request["body"]["params"]["name"] for request in fake_server.state.requests] == [
        "astral_get_operation",
    ]
    assert [response["id"] for response in responses] == [0]


def test_reader_error_write_failure_reaches_the_serving_caller(fake_server, monkeypatch):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    thread_failures = []
    monkeypatch.setattr(threading, "excepthook", thread_failures.append)

    class BrokenOutput(io.StringIO):
        def write(self, value):
            raise BrokenPipeError("synthetic stdout closed")

    incoming = io.StringIO("not json\n")
    outgoing = BrokenOutput()
    try:
        with pytest.raises(BrokenPipeError, match="synthetic stdout closed"):
            bridge.serve_stdio(incoming, outgoing)
        assert not thread_failures
        assert not fake_server.state.requests
    finally:
        incoming.close()
        outgoing.close()
        bridge.close()


def test_oversized_raw_frame_cannot_hide_in_leading_whitespace(fake_server):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}})

    class ObservedInput(io.StringIO):
        def __init__(self, payload):
            super().__init__(payload)
            self.read_bounds = []

        def readline(self, size=-1):
            self.read_bounds.append(size)
            return super().readline(size)

        def read(self, size=-1):
            self.read_bounds.append(size)
            return super().read(size)

        def __iter__(self):
            return self

        def __next__(self):
            line = self.readline()
            if not line:
                raise StopIteration
            return line

    incoming = ObservedInput(" " * (_MAX_LINE_CHARS + 1) + request + "\n" + request + "\n")
    outgoing = io.StringIO()
    try:
        bridge.serve_stdio(incoming, outgoing)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        assert len(responses) == 2
        assert incoming.read_bounds
        assert all(0 < size <= _MAX_LINE_CHARS + len("\r\n") + 1 for size in incoming.read_bounds)
        assert responses[0]["error"]["code"] == -32600
        assert responses[1]["id"] == 1
        assert responses[1]["result"]["resultType"] == "complete"
    finally:
        incoming.close()
        outgoing.close()
        bridge.close()


def test_error_output_backpressure_does_not_starve_pending_cancellation(fake_server, monkeypatch):
    bridge = Bridge.connect(fake_server.base_url, fake_server.state.valid_token)
    blocked = threading.Event()
    writing_error = threading.Event()
    response_finished = threading.Event()
    original_get = bridge._client.get_operation
    original_finish = _RequestTracker.finish

    def observe_finish(tracker, request_id):
        result = original_finish(tracker, request_id)
        if request_id == 0:
            response_finished.set()
        return result

    def block_read(*args, **kwargs):
        blocked.set()
        assert writing_error.wait(timeout=10)
        return original_get(*args, **kwargs)

    monkeypatch.setattr(bridge._client, "get_operation", block_read)
    monkeypatch.setattr(_RequestTracker, "finish", observe_finish)
    payload = _tool_request(0, "astral_get_operation", {"operation_id": "blocker"}) + "not json\n"
    payload += json.dumps({
        "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 0},
    }) + "\n"
    incoming = _OrderedInput(payload, blocked)

    class BlockedErrorOutput(io.StringIO):
        def write(self, value):
            if "error" in json.loads(value):
                writing_error.set()
                assert response_finished.wait(timeout=10)
            return super().write(value)

    outgoing = BlockedErrorOutput()
    try:
        bridge.serve_stdio(incoming, outgoing)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        assert all(response["id"] != 0 for response in responses)
        assert [response["error"]["code"] for response in responses] == [-32700]
    finally:
        writing_error.set()
        response_finished.set()
        incoming.close()
        outgoing.close()
        bridge.close()
