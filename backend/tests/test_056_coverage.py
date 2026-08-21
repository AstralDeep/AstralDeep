"""056 coverage: exercise the product branches the story tests reach only via
live verification (which doesn't feed coverage.xml) — the summarizer peer-fetch
hop, the offline-grant/session-store lookups, the base-agent hop-response
resolver, the protocol frame decode, and the delegation encode/decode helpers.

These are unit-level pins on real branches, not new behavior.
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


# --------------------------------------------------------------------------- #
# summarizer._fetch_via_peer / _summarize_fetched (US1 first call site)
# --------------------------------------------------------------------------- #

def _peer_runtime(resp):
    """A runtime whose call_agent_tool resolves to ``resp`` on the caller loop."""
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


def test_fetch_via_peer_extracts_page_text():
    from agents.summarizer import mcp_tools
    from shared.protocol import MCPResponse

    card = {"type": "card", "content": [
        {"type": "text", "content": "source: http://x"},
        {"type": "text", "content": "the readable page body"},
    ]}
    resp = MCPResponse(result={"title": "X Title"}, ui_components=[card])
    rt, loop = _peer_runtime(resp)
    out = _run_with_loop(
        lambda: mcp_tools._fetch_via_peer("http://x", {"_runtime": rt}), loop)
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
        lambda: mcp_tools._fetch_via_peer("http://x", {"_runtime": rt}), loop)
    assert out is None


def test_fetch_via_peer_empty_text_returns_none():
    from agents.summarizer import mcp_tools
    from shared.protocol import MCPResponse

    card = {"type": "card", "content": [
        {"type": "text", "content": "src"}, {"type": "text", "content": "   "}]}
    rt, loop = _peer_runtime(MCPResponse(result={}, ui_components=[card]))
    out = _run_with_loop(
        lambda: mcp_tools._fetch_via_peer("http://x", {"_runtime": rt}), loop)
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


# --------------------------------------------------------------------------- #
# OfflineGrantStore.latest_valid_for
# --------------------------------------------------------------------------- #

def test_latest_valid_for_prefers_agent_grant():
    from orchestrator.offline_grant import OfflineGrantStore

    repository = _OfflineGrantRepository(
        SimpleNamespace(grant_id="grant-agent")
    )
    runtime = _PlaneRuntime(offline_grants=repository)
    store = OfflineGrantStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    assert store.latest_valid_for("u1", "web-research-1") == "grant-agent"
    assert repository.calls[0]["owner_id"] == "u1"
    assert repository.calls[0]["agent_id"] == "web-research-1"
    assert isinstance(repository.calls[0]["as_of"], int)


def test_latest_valid_for_falls_back_to_any_grant():
    from orchestrator.offline_grant import OfflineGrantStore

    repository = _OfflineGrantRepository(SimpleNamespace(grant_id="grant-any"))
    runtime = _PlaneRuntime(offline_grants=repository)
    store = OfflineGrantStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    assert store.latest_valid_for("u1", "web-research-1") == "grant-any"
    assert len(repository.calls) == 1
    assert repository.calls[0]["owner_id"] == "u1"
    assert repository.calls[0]["agent_id"] == "web-research-1"


def test_latest_valid_for_none_when_absent():
    from orchestrator.offline_grant import OfflineGrantStore

    repository = _OfflineGrantRepository(None)
    runtime = _PlaneRuntime(offline_grants=repository)
    store = OfflineGrantStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    assert store.latest_valid_for("u1", None) is None
    assert repository.calls[0]["agent_id"] is None


# --------------------------------------------------------------------------- #
# WebSessionStore.latest_refresh_token_for
# --------------------------------------------------------------------------- #

def test_latest_refresh_token_for_reads_live_session(monkeypatch):
    from orchestrator import session_store

    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    record = SimpleNamespace(
        session_id="sid-1",
        owner_id="u1",
        access_token_ciphertext="at-abc",
        refresh_token_ciphertext="rt-abc",
        interactive_anchor=1,
        hard_expires_at=10_000,
        last_refresh_at=2,
        resumed=False,
        created_at=1,
    )
    repository = _SessionRepository(record)
    store = session_store.WebSessionStore(
        session_context=_RepositoryContext(repository),
        revocation_context=_RepositoryContext(SimpleNamespace()),
    )
    assert store.latest_refresh_token_for("u1") == "rt-abc"
    assert repository.calls[0]["owner_id"] == "u1"
    assert isinstance(repository.calls[0]["observed_at"], int)


def test_latest_refresh_token_for_none_without_session(monkeypatch):
    from orchestrator import session_store

    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    repository = _SessionRepository(None)
    store = session_store.WebSessionStore(
        session_context=_RepositoryContext(repository),
        revocation_context=_RepositoryContext(SimpleNamespace()),
    )
    assert store.latest_refresh_token_for("u1") is None


# --------------------------------------------------------------------------- #
# BaseA2AAgent._resolve_hop_response
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# protocol.Message.from_json — the hop frames
# --------------------------------------------------------------------------- #

def test_from_json_decodes_hop_frames():
    from shared.protocol import AgentHopRequest, AgentHopResponse, Message

    req = Message.from_json(AgentHopRequest(
        request_id="h1", parent_request_id="p1", initiator_agent_id="a",
        callee_agent_id="b", tool_name="t", arguments={"q": 1}).to_json())
    assert isinstance(req, AgentHopRequest) and req.callee_agent_id == "b"
    resp = Message.from_json(AgentHopResponse(
        request_id="h1", response={"result": "ok"}).to_json())
    assert isinstance(resp, AgentHopResponse) and resp.response == {"result": "ok"}


# --------------------------------------------------------------------------- #
# delegation encode/decode helpers
# --------------------------------------------------------------------------- #

def test_encode_decode_roundtrip():
    from orchestrator import delegation as dg

    payload = {"sub": "u1", "act": {"sub": "agent:a"}, "scope": "tools:read",
               "exp": 123, "delegation": True}
    token = dg.encode_delegation_payload(payload)
    assert token.count(".") == 2
    assert dg.decode_token_payload(token) == payload


def test_decode_rejects_malformed():
    from orchestrator import delegation as dg

    assert dg.decode_token_payload("") is None
    assert dg.decode_token_payload("not-a-token") is None
    assert dg.decode_token_payload("a.b") is None  # wrong segment count


def test_child_signing_key_prefers_env(monkeypatch):
    from orchestrator import delegation as dg

    monkeypatch.setenv("DELEGATION_CHILD_SIGNING_KEY", "dedicated-key")
    assert dg._child_signing_key() == b"dedicated-key"
    monkeypatch.delenv("DELEGATION_CHILD_SIGNING_KEY")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "shared-key")
    assert dg._child_signing_key() == b"shared-key"
    monkeypatch.delenv("MEMORY_HMAC_KEY")
    assert dg._child_signing_key() == b"mock-delegation-secret"


# --------------------------------------------------------------------------- #
# Orchestrator._deliver_hop_response — the NETWORKED-initiator path
# --------------------------------------------------------------------------- #

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
    return fake


@pytest.mark.asyncio
async def test_deliver_hop_response_networked_sends_frame(orch):
    """A networked initiator (a `send`, no matching future) gets an
    agent_hop_response frame."""
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
    # No _hop_futures, no send — nothing to deliver to; must not raise.
    await orch._deliver_hop_response(
        SimpleNamespace(), "hop-1", MCPResponse(result="x"))


@pytest.mark.asyncio
async def test_deliver_hop_response_send_failure_swallowed(orch):
    from shared.protocol import MCPResponse

    class _BadWS:
        async def send(self, text):
            raise RuntimeError("socket dead")

    # Must swallow the delivery error (best-effort), not raise.
    await orch._deliver_hop_response(_BadWS(), "hop-1", MCPResponse(result="x"))


@pytest.mark.asyncio
async def test_subtasks_dispatch_from_parallel_batch(orch, monkeypatch):
    """The __subtasks__ meta-tool dispatches from a parallel batch, computing
    _parent_tools from the turn's real-agent tools."""
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
    results = await orch.execute_parallel_tools(
        MagicMock(), [tc],
        {"delegate_subtasks": "__subtasks__", "web_search": "web-research-1"},
        "c1", user_id="u1")
    assert results[0].result == "subtasks-ok"
    assert "web_search" in seen["parent_tools"]
