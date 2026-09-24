"""Tests for shared/agent_runtime.py's AgentRuntime.call_agent_tool: emits an
agent_hop_request frame over the agent's socket, correlates the response future, and
never raises past a failed send or timeout.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.agent_runtime import AgentRuntime  # noqa: E402
from shared.protocol import MCPRequest, MCPResponse  # noqa: E402


class _FakeWS:
    def __init__(self, on_frame=None, raise_on_send=None):
        self.frames = []
        self._on_frame = on_frame
        self._raise = raise_on_send

    async def send_text(self, text):
        if self._raise is not None:
            raise self._raise
        self.frames.append(json.loads(text))
        if self._on_frame is not None:
            await self._on_frame(self, json.loads(text))


def _runtime(ws):
    msg = MCPRequest(request_id="req-parent-1", method="tools/call",
                     params={"name": "research_brief",
                             "arguments": {"user_id": "u1"}})
    return AgentRuntime(ws=ws, msg=msg, agent_id="web-research-1",
                        loop=asyncio.get_event_loop())


@pytest.mark.asyncio
async def test_frame_shape_and_response_correlation():
    async def orchestrator_side(ws, frame):
        fut = ws._hop_futures[frame["request_id"]]
        fut.set_result(MCPResponse(request_id=frame["request_id"],
                                   result="peer-says-hi"))

    ws = _FakeWS(on_frame=orchestrator_side)
    rt = _runtime(ws)
    resp = await rt.call_agent_tool("summarizer-1", "summarize_text",
                                    {"text": "hello"})
    assert resp.result == "peer-says-hi"
    frame = ws.frames[0]
    assert frame["type"] == "agent_hop_request"
    assert frame["parent_request_id"] == "req-parent-1"
    assert frame["initiator_agent_id"] == "web-research-1"
    assert frame["callee_agent_id"] == "summarizer-1"
    assert frame["tool_name"] == "summarize_text"
    assert frame["arguments"] == {"text": "hello"}
    assert "_delegation_token" not in json.dumps(frame)
    assert "user_id" not in frame
    assert frame["request_id"] not in ws._hop_futures


@pytest.mark.asyncio
async def test_send_failure_returns_error_not_raise():
    ws = _FakeWS(raise_on_send=RuntimeError("socket gone"))
    rt = _runtime(ws)
    resp = await rt.call_agent_tool("summarizer-1", "summarize_text", {})
    assert resp.error is not None
    assert "socket gone" in resp.error["message"]


@pytest.mark.asyncio
async def test_timeout_returns_honest_error():
    ws = _FakeWS()
    rt = _runtime(ws)
    resp = await rt.call_agent_tool("summarizer-1", "summarize_text", {},
                                    timeout=0.05)
    assert resp.error is not None
    assert "timed out" in resp.error["message"]
    assert resp.error.get("retryable") is True
    assert not ws._hop_futures


@pytest.mark.asyncio
async def test_error_response_passes_through():
    async def orchestrator_refuses(ws, frame):
        ws._hop_futures[frame["request_id"]].set_result(MCPResponse(
            request_id=frame["request_id"],
            error={"message": "Hop refused: chain budget exhausted",
                   "retryable": False}))

    ws = _FakeWS(on_frame=orchestrator_refuses)
    rt = _runtime(ws)
    resp = await rt.call_agent_tool("summarizer-1", "summarize_text", {})
    assert "budget exhausted" in resp.error["message"]


@pytest.mark.asyncio
async def test_transport_without_attribute_support_degrades_honestly():
    class _Sealed:
        __slots__ = ()
        async def send_text(self, text):  # pragma: no cover
            raise AssertionError("should not send")

    rt = _runtime(_Sealed())
    resp = await rt.call_agent_tool("summarizer-1", "summarize_text", {})
    assert resp.error is not None
    assert "correlate" in resp.error["message"]
