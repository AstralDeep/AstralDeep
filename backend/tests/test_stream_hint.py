"""Checks stream elapsed bookkeeping in shared/base_agent.py and shared/stream_sdk.py.
Test agents exercise replay, cancellation, bounds, and protected-wire isolation.
"""

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.base_agent import _STREAM_FIRST_SEEN_LIMIT, BaseA2AAgent
from shared.feature_flags import flags
from shared.protocol import MCP_PROTOCOL_VERSION, MCPRequest, ToolStreamCancel
from shared.stream_sdk import StreamComponents, stream_seconds_left, streaming_tool

RECORDED: list = []


@streaming_tool(
    name="live_hint_probe", description="records its arguments and closes at once",
    input_schema={"type": "object", "properties": {}},
)
async def _hint_probe(args, credentials):
    RECORDED.append(dict(args))
    yield StreamComponents(components=[{"type": "metric", "title": "M", "value": "final"}], terminal=True)


@streaming_tool(
    name="live_hint_endless", description="ends through ToolStreamEnd without a terminal chunk",
    input_schema={"type": "object", "properties": {}},
)
async def _hint_endless(args, credentials):
    RECORDED.append(dict(args))
    yield StreamComponents(components=[{"type": "metric", "title": "M", "value": "start"}])


@streaming_tool(
    name="live_hint_held", description="waits until it is cancelled",
    input_schema={"type": "object", "properties": {}},
)
async def _hint_held(args, credentials):
    RECORDED.append(dict(args))
    await asyncio.Event().wait()
    yield StreamComponents(components=[])


class HintProbeAgent(BaseA2AAgent):
    agent_id = "hint-probe-1"
    service_name = "Hint Probe"
    description = "probe for the stream elapsed hint"
    skill_tags = ["test"]


def _request(stream_id, name="live_hint_probe", request_id="req-1"):
    return MCPRequest(
        request_id=request_id,
        method="tools/call",
        params={"name": name, "arguments": {}, "_stream": True, "_stream_id": stream_id},
        protocol_version=MCP_PROTOCOL_VERSION,
        caller_capabilities={},
        caller_info={"name": "stream hint tests"},
    )


class _FakeWS:
    async def send_text(self, text):
        return None


@pytest.fixture
def recorded():
    RECORDED.clear()
    yield RECORDED
    RECORDED.clear()


@pytest.fixture
def push_flags():
    prior = flags._flags["tool_streaming"]
    flags._flags["tool_streaming"] = True
    yield
    flags._flags["tool_streaming"] = prior


@pytest.fixture
def agent(monkeypatch, push_flags):
    monkeypatch.setattr(
        BaseA2AAgent, "_init_crypto",
        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}),
    )
    probe = HintProbeAgent(mcp_server=SimpleNamespace(tools={
        "live_hint_probe": {"function": _hint_probe},
        "live_hint_endless": {"function": _hint_endless},
        "live_hint_held": {"function": _hint_held},
    }), port=8997)
    yield probe
    for entry in list(probe._active_streams.values()):
        task = entry[0]
        if not task.done():
            task.cancel()
    for task in list(probe._stream_wrapper_tasks):
        task.cancel()


async def _drain(agent):
    pending = [
        task for task in agent._stream_wrapper_tasks if not task.done()
    ]
    if pending:
        await asyncio.wait(pending)


async def test_the_first_dispatch_reports_no_elapsed_time(agent, recorded):
    await agent.handle_mcp_request(_FakeWS(), _request("stream-aaa"))
    await _drain(agent)
    assert recorded[-1]["_stream_elapsed_s"] == 0.0
    assert agent._stream_first_seen["stream-aaa"] <= time.monotonic()


async def test_a_redispatch_of_the_same_stream_id_reports_elapsed_time(agent, recorded):
    await agent.handle_mcp_request(_FakeWS(), _request("stream-aaa"))
    await _drain(agent)
    agent._stream_first_seen["stream-aaa"] = time.monotonic() - 120
    await agent.handle_mcp_request(_FakeWS(), _request("stream-aaa", request_id="req-2"))
    await _drain(agent)
    assert recorded[-1]["_stream_elapsed_s"] >= 120.0


async def test_another_stream_id_starts_from_zero_again(agent, recorded):
    agent._stream_first_seen["stream-old"] = time.monotonic() - 300
    await agent.handle_mcp_request(_FakeWS(), _request("stream-new"))
    await _drain(agent)
    assert recorded[-1]["_stream_elapsed_s"] == 0.0


async def test_a_freshly_created_stream_id_never_goes_negative(agent, recorded):
    agent._stream_first_seen["stream-ahead"] = time.monotonic() + 500
    await agent.handle_mcp_request(_FakeWS(), _request("stream-ahead"))
    await _drain(agent)
    assert recorded[-1]["_stream_elapsed_s"] == 0.0


async def test_a_stream_that_ends_through_tool_stream_end_releases_its_bookkeeping(agent, recorded):
    await agent.handle_mcp_request(_FakeWS(), _request("stream-end", name="live_hint_endless"))
    await _drain(agent)
    assert "stream-end" not in agent._stream_first_seen


async def test_a_cancelled_stream_keeps_its_bookkeeping_for_a_resume(agent, recorded):
    await agent.handle_mcp_request(_FakeWS(), _request("stream-hold", name="live_hint_held"))
    for _ in range(200):
        if recorded:
            break
        await asyncio.sleep(0.01)
    assert "stream-hold" in agent._stream_first_seen
    await agent._handle_stream_cancel(ToolStreamCancel(request_id="req-1", stream_id="stream-hold"))
    await _drain(agent)
    assert "stream-hold" in agent._stream_first_seen


async def test_the_bookkeeping_stays_bounded(agent, recorded):
    for index in range(_STREAM_FIRST_SEEN_LIMIT):
        agent._stream_first_seen[f"stream-old-{index}"] = float(index)
    await agent.handle_mcp_request(_FakeWS(), _request("stream-new"))
    await _drain(agent)
    assert len(agent._stream_first_seen) <= _STREAM_FIRST_SEEN_LIMIT
    assert "stream-old-0" not in agent._stream_first_seen
    assert "stream-new" in agent._stream_first_seen


async def test_the_hint_never_reaches_the_protected_wire_snapshot(agent, recorded):
    msg = _request("stream-wire")
    await agent.handle_mcp_request(_FakeWS(), msg)
    await _drain(agent)
    assert "_stream_elapsed_s" not in (msg._protected_wire_arguments or {}).get("arguments", {})
    assert recorded[-1]["_stream_elapsed_s"] == 0.0


def test_stream_seconds_left_ignores_missing_and_unusable_hints():
    assert stream_seconds_left({}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": None}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": "2"}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": True}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": -1}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": 0}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": float("nan")}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": float("inf")}, 5.0) == 5.0
    assert stream_seconds_left({"_stream_elapsed_s": 2}, 5.0) == 3.0
    assert stream_seconds_left({"_stream_elapsed_s": 9}, 5.0) == 0.0
