"""Tests for shared/protocol.py streaming messages: ToolStreamData/Cancel/End round-trip
serialization, the MCPRequest _stream flag, and validate_streaming_metadata's
acceptance and rejection rules.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.protocol import (
    Message,
    MCPRequest,
    ToolStreamData,
    ToolStreamCancel,
    ToolStreamEnd,
    validate_streaming_metadata,
)


class TestToolStreamDataRoundTrip:
    def test_minimal_chunk_round_trips(self):
        msg = ToolStreamData(
            request_id="req-1",
            stream_id="stream-abc",
            agent_id="weather",
            tool_name="live_temperature",
            seq=1,
            components=[{"type": "metric", "id": "stream-abc", "value": "12C"}],
        )
        encoded = msg.to_json()
        decoded = Message.from_json(encoded)
        assert isinstance(decoded, ToolStreamData)
        assert decoded.request_id == "req-1"
        assert decoded.stream_id == "stream-abc"
        assert decoded.seq == 1
        assert decoded.components[0]["value"] == "12C"
        assert decoded.terminal is False
        assert decoded.error is None

    def test_chunk_with_error_round_trips(self):
        msg = ToolStreamData(
            request_id="req-2",
            stream_id="stream-x",
            agent_id="weather",
            tool_name="live_temperature",
            seq=42,
            components=[],
            error={
                "code": "upstream_unavailable",
                "message": "weather API down",
                "phase": "reconnecting",
                "attempt": 2,
                "next_retry_at_ms": 1759981239000,
                "retryable": False,
            },
        )
        encoded = msg.to_json()
        decoded = Message.from_json(encoded)
        assert isinstance(decoded, ToolStreamData)
        assert decoded.error["phase"] == "reconnecting"
        assert decoded.error["attempt"] == 2

    def test_terminal_chunk(self):
        msg = ToolStreamData(
            request_id="r", stream_id="s", agent_id="a", tool_name="t",
            seq=99, components=[{"type": "metric", "id": "s"}], terminal=True,
        )
        decoded = Message.from_json(msg.to_json())
        assert decoded.terminal is True


class TestToolStreamCancel:
    def test_round_trip(self):
        msg = ToolStreamCancel(request_id="r1", stream_id="s1")
        decoded = Message.from_json(msg.to_json())
        assert isinstance(decoded, ToolStreamCancel)
        assert decoded.request_id == "r1"
        assert decoded.stream_id == "s1"


class TestToolStreamEnd:
    def test_round_trip(self):
        msg = ToolStreamEnd(request_id="r2", stream_id="s2")
        decoded = Message.from_json(msg.to_json())
        assert isinstance(decoded, ToolStreamEnd)
        assert decoded.request_id == "r2"
        assert decoded.stream_id == "s2"


class TestMCPRequestStreamingFlag:
    def test_request_with_stream_flag_round_trips(self):
        req = MCPRequest(
            request_id="req-stream",
            method="tools/call",
            params={
                "name": "live_temperature",
                "arguments": {"lat": 51.5, "lon": -0.12},
                "_stream": True,
                "_stream_id": "stream-7c2a1f",
            },
        )
        decoded = Message.from_json(req.to_json())
        assert isinstance(decoded, MCPRequest)
        assert decoded.params["_stream"] is True
        assert decoded.params["_stream_id"] == "stream-7c2a1f"
        assert decoded.params["arguments"]["lat"] == 51.5

    def test_request_without_stream_keys_still_works(self):
        req = MCPRequest(
            request_id="r",
            method="tools/call",
            params={"name": "get_current_weather", "arguments": {}},
        )
        decoded = Message.from_json(req.to_json())
        assert isinstance(decoded, MCPRequest)
        assert "_stream" not in decoded.params


class TestValidateStreamingMetadata:
    def test_non_streamable_is_noop(self):
        validate_streaming_metadata({})
        validate_streaming_metadata({"streamable": False})

    def test_valid_push_metadata(self):
        validate_streaming_metadata({
            "streamable": True,
            "streaming_kind": "push",
            "max_fps": 30,
            "min_fps": 5,
            "max_chunk_bytes": 65536,
        })

    def test_valid_poll_metadata(self):
        validate_streaming_metadata({
            "streamable": True,
            "streaming_kind": "poll",
            "default_interval_s": 5,
        })

    def test_missing_kind_is_rejected(self):
        with pytest.raises(ValueError, match="streaming_kind"):
            validate_streaming_metadata({"streamable": True})

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValueError, match="streaming_kind"):
            validate_streaming_metadata({
                "streamable": True, "streaming_kind": "magic",
            })

    def test_invalid_fps_clamp_rejected(self):
        with pytest.raises(ValueError, match="fps"):
            validate_streaming_metadata({
                "streamable": True, "streaming_kind": "push",
                "min_fps": 10, "max_fps": 5,
            })

    def test_negative_fps_rejected(self):
        with pytest.raises(ValueError, match="fps"):
            validate_streaming_metadata({
                "streamable": True, "streaming_kind": "push",
                "min_fps": 0, "max_fps": 30,
            })

    def test_oversized_chunk_cap_rejected(self):
        with pytest.raises(ValueError, match="1 MiB"):
            validate_streaming_metadata({
                "streamable": True, "streaming_kind": "push",
                "max_chunk_bytes": (1 << 20) + 1,
            })

    def test_negative_max_chunk_bytes_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            validate_streaming_metadata({
                "streamable": True, "streaming_kind": "push",
                "max_chunk_bytes": -1,
            })

    def test_poll_negative_interval_rejected(self):
        with pytest.raises(ValueError, match="default_interval_s"):
            validate_streaming_metadata({
                "streamable": True, "streaming_kind": "poll",
                "default_interval_s": -1,
            })
