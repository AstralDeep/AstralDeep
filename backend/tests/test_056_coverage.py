"""Unit-level coverage for branches the story tests reach only via live verification:
summarizer's peer-fetch hop, offline-grant and session-store lookups, base-agent
hop-response resolution, and protocol/delegation codecs.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _PlaneRuntime:
    def __init__(self, **repositories):
        self.repositories = SimpleNamespace(**repositories)

    @contextmanager
    def transaction(self):
        yield object()


class _OfflineGrantRepository:
    def __init__(self, reference):
        self.reference = reference
        self.calls = []

    def find_latest_valid(self, _transaction, **values):
        self.calls.append(values)
        return self.reference


class _RepositoryContext:
    def __init__(self, repository):
        self.repository = repository

    @contextmanager
    def transaction(self):
        yield object()


class _SessionRepository:
    def __init__(self, record):
        self.record = record
        self.calls = []

    def get_latest_live_for_owner(self, _transaction, **values):
        self.calls.append(values)
        return self.record


def _peer_runtime(resp):
    loop = asyncio.new_event_loop()

    async def _call(callee, tool, args, *, timeout=30.0):
        return resp

    rt = SimpleNamespace(call_agent_tool=_call, loop=loop)
    return rt, loop


def _run_with_loop(fn, loop):
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def _peer_kwargs(runtime, scope="tools:read tool:fetch_page"):
    from jose import jwt

    token = jwt.encode({"scope": scope}, "test-only-scope-hint", algorithm="HS256")
    return {"_runtime": runtime, "_delegation_token": token}


@pytest.mark.parametrize("scope", [None, [], "", "tools:write", "tool:fetch_page",
                                  "tools:read tool:summarize_text"])
def test_fetch_via_peer_skips_ineligible_scope_without_attempting_hop(scope):
    from agents.summarizer import mcp_tools

    runtime = SimpleNamespace(call_agent_tool=MagicMock())
    assert mcp_tools._fetch_via_peer("http://x", _peer_kwargs(runtime, scope)) is None
    runtime.call_agent_tool.assert_not_called()


@pytest.mark.parametrize("token", [None, 123, "", "malformed-token"])
def test_fetch_via_peer_skips_unreadable_authority_without_attempting_hop(token):
    from agents.summarizer import mcp_tools

    runtime = SimpleNamespace(call_agent_tool=MagicMock())
    assert mcp_tools._fetch_via_peer(
        "http://x", {"_runtime": runtime, "_delegation_token": token},
    ) is None
    runtime.call_agent_tool.assert_not_called()


@pytest.mark.parametrize("scope", ["tools:read", "tools:read tool:fetch_page"])
def test_fetch_via_peer_extracts_page_text(scope):
    from agents.summarizer import mcp_tools
    from shared.protocol import MCPResponse

    card = {"type": "card", "content": [
        {"type": "text", "content": "source: http://x"},
        {"type": "text", "content": "the readable page body"},
    ]}
    resp = MCPResponse(result={"title": "X Title"}, ui_components=[card])
    rt, loop = _peer_runtime(resp)
    out = _run_with_loop(
        lambda: mcp_tools._fetch_via_peer("http://x", _peer_kwargs(rt, scope)), loop)
    assert out == ("X Title", "the readable page body")


def test_fetch_via_peer_no_runtime_returns_none():
    from agents.summarizer import mcp_tools
    assert mcp_tools._fetch_via_peer("http://x", {}) is None
    assert mcp_tools._fetch_via_peer("http://x", {"_runtime": object()}) is None


def test_fetch_via_peer_refused_hop_returns_none():
    from agents.summarizer import mcp_tools
    from shared.protocol import MCPResponse

    rt, loop = _peer_runtime(MCPResponse(error={"message": "Hop refused"}))
    out = _run_with_loop(
        lambda: mcp_tools._fetch_via_peer("http://x", _peer_kwargs(rt)), loop)
    assert out is None


def test_fetch_via_peer_empty_text_returns_none():
    from agents.summarizer import mcp_tools
    from shared.protocol import MCPResponse

    card = {"type": "card", "content": [
        {"type": "text", "content": "src"}, {"type": "text", "content": "   "}]}
    rt, loop = _peer_runtime(MCPResponse(result={}, ui_components=[card]))
    out = _run_with_loop(
        lambda: mcp_tools._fetch_via_peer("http://x", _peer_kwargs(rt)), loop)
    assert out is None


def test_summarize_url_uses_peer_fetch(monkeypatch):
    from agents.summarizer import mcp_tools

    monkeypatch.setattr(mcp_tools, "_fetch_via_peer",
                        lambda url, kw: ("T", "hopped body text"))
    captured = {}
    monkeypatch.setattr(mcp_tools, "_summarize_fetched",
                        lambda url, title, text, kw: captured.update(
                            url=url, title=title, text=text) or {"_ui_components": []})
    mcp_tools.summarize_url(url="http://x")
    assert captured == {"url": "http://x", "title": "T", "text": "hopped body text"}


def test_summarize_url_empty_url_errors():
    from agents.summarizer import mcp_tools
    out = mcp_tools.summarize_url(url="")
    assert any(c.get("variant") == "error" for c in out["_ui_components"])


def test_summarize_fetched_empty_text_errors():
    from agents.summarizer import mcp_tools
    out = mcp_tools._summarize_fetched("http://x", "T", "   ", {})
    assert any(c.get("variant") == "error" for c in out["_ui_components"])


def test_resolve_hop_response_sets_future():
    from shared.base_agent import BaseA2AAgent
    from shared.protocol import AgentHopResponse

    agent = BaseA2AAgent.__new__(BaseA2AAgent)
    agent._logger = MagicMock()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    ws = SimpleNamespace(_hop_futures={"hop-1": fut})
    agent._resolve_hop_response(ws, AgentHopResponse(
        request_id="hop-1", response={"result": "peer-ok", "error": None,
                                      "ui_components": None}))
    assert fut.done() and fut.result().result == "peer-ok"
    loop.close()


def test_resolve_hop_response_unknown_id_is_dropped():
    from shared.base_agent import BaseA2AAgent
    from shared.protocol import AgentHopResponse

    agent = BaseA2AAgent.__new__(BaseA2AAgent)
    agent._logger = MagicMock()
    ws = SimpleNamespace(_hop_futures={})
    agent._resolve_hop_response(ws, AgentHopResponse(request_id="ghost"))
    agent._logger.warning.assert_called_once()


def test_from_json_decodes_hop_frames():
    from shared.protocol import AgentHopRequest, AgentHopResponse, Message

    req = Message.from_json(AgentHopRequest(
        request_id="h1", parent_request_id="p1", initiator_agent_id="a",
        callee_agent_id="b", tool_name="t", arguments={"q": 1}).to_json())
    assert isinstance(req, AgentHopRequest) and req.callee_agent_id == "b"
    resp = Message.from_json(AgentHopResponse(
        request_id="h1", response={"result": "ok"}).to_json())
    assert isinstance(resp, AgentHopResponse) and resp.response == {"result": "ok"}


def test_encode_decode_roundtrip(tmp_path, monkeypatch):
    import time

    from orchestrator import agent_authority as dg
    from shared import node_identity

    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path / "node"))
    node_identity.reset_cache()
    try:
        now = int(time.time())
        payload = {"sub": "u1", "act": {"sub": "agent:a"}, "scope": "tools:read",
                   "iat": now, "exp": now + 60, "aud": ["agent-service"], "delegation": True}
        token = dg.encode_delegation_payload(payload)
        assert token.count(".") == 2
        decoded = dg.decode_token_payload(token)
        assert {key: decoded[key] for key in payload if key != "aud"} == {
            key: value for key, value in payload.items() if key != "aud"}
        assert "agent-service" in decoded["aud"]
        assert dg.verify_signed_delegation(token, audience="agent-service")["sub"] == "u1"
    finally:
        node_identity.reset_cache()


def test_decode_rejects_malformed():
    from orchestrator import agent_authority as dg

    assert dg.decode_token_payload("") is None
    assert dg.decode_token_payload("not-a-token") is None
    assert dg.decode_token_payload("a.b") is None


@pytest.fixture
def orch():
    from orchestrator.orchestrator import Orchestrator

    fake = SimpleNamespace()
    fake._deliver_hop_response = MethodType(
        Orchestrator._deliver_hop_response,
        fake,
    )
    fake.execute_parallel_tools = MethodType(
        Orchestrator.execute_parallel_tools,
        fake,
    )
    fake._guard_machine_meta_tool = MethodType(
        Orchestrator._guard_machine_meta_tool,
        fake,
    )
    fake.ui_sessions = {}
    return fake


@pytest.mark.asyncio
async def test_deliver_hop_response_networked_sends_frame(orch):
    from shared.protocol import MCPResponse

    sent = {}

    class _NetWS:
        _hop_futures = {}

        async def send(self, text):
            sent["frame"] = text

    await orch._deliver_hop_response(
        _NetWS(), "hop-1", MCPResponse(result="peer-ok"))
    import json
    frame = json.loads(sent["frame"])
    assert frame["type"] == "agent_hop_response"
    assert frame["response"]["result"] == "peer-ok"


@pytest.mark.asyncio
async def test_deliver_hop_response_no_route_logs(orch):
    from shared.protocol import MCPResponse
    await orch._deliver_hop_response(
        SimpleNamespace(), "hop-1", MCPResponse(result="x"))


@pytest.mark.asyncio
async def test_deliver_hop_response_send_failure_swallowed(orch):
    from shared.protocol import MCPResponse

    class _BadWS:
        async def send(self, text):
            raise RuntimeError("socket dead")

    await orch._deliver_hop_response(_BadWS(), "hop-1", MCPResponse(result="x"))


@pytest.mark.asyncio
async def test_subtasks_dispatch_from_parallel_batch(orch, monkeypatch):
    import json

    from shared.feature_flags import flags
    from shared.protocol import MCPResponse
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)

    from orchestrator import subtasks as _st
    seen = {}

    async def _meta(o, tool, args, *, user_id, chat_id, websocket):
        seen["parent_tools"] = args.get("_parent_tools")
        return MCPResponse(result="subtasks-ok")

    monkeypatch.setattr(_st, "handle_meta_tool", _meta)
    tc = SimpleNamespace(function=SimpleNamespace(
        name="delegate_subtasks", arguments=json.dumps({"subtasks": []})))
    websocket = MagicMock()
    orch.ui_sessions[websocket] = {"sub": "u1"}
    results = await orch.execute_parallel_tools(
        websocket, [tc],
        {"delegate_subtasks": "__subtasks__", "web_search": "web-research-1"},
        "c1", user_id="u1")
    assert results[0].result == "subtasks-ok"
    assert "web_search" in seen["parent_tools"]
