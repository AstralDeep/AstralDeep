"""Taint gate (C-S2) wired into the REAL dispatch path, ``FF_TAINT_TRACKING``
ON — the production-readiness fixes for turning the flag on:

1. A read-only page fetch is NOT a sink: ``web_search -> fetch_page(<result
   url>)`` in the same chat is no longer denied.
2. User intent: a value the user typed VERBATIM in the current message is
   user-supplied for the check, even if a prior untrusted source emitted it.
3. A taint denial for a built-in agent leaves an ``agent_tool_call`` audit
   row (``tool.<name>.denied``), recorded non-blocking.

A call that passes the gate falls through to the "No agent available"
sentinel (the tool's agent isn't registered), which proves it got past.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

URL = "https://example.org/some/article"


@pytest.fixture
def orch(orchestrator_factory, monkeypatch):
    monkeypatch.setenv("FF_TAINT_TRACKING", "true")
    o = orchestrator_factory()
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    return o


@pytest.fixture
def recorded(monkeypatch):
    """Capture every audit row the process-wide recorder receives."""
    from audit import recorder as rec_mod
    rows = []
    fake = MagicMock()

    async def _record(ev):
        rows.append(ev)
    fake.record = _record
    prev = rec_mod.get_recorder()
    rec_mod.set_recorder(fake)
    try:
        yield rows
    finally:
        rec_mod.set_recorder(prev)


def _tc(tool, args=None):
    return SimpleNamespace(
        function=SimpleNamespace(name=tool, arguments=json.dumps(args or {}))
    )


async def _dispatch(orch, tool, args, *, request="", user="u1",
                    agent="web-research-1", chat="c1"):
    orch._active_request = {chat: request}
    ws = MagicMock()
    return await orch.execute_single_tool(
        ws, _tc(tool, args), {tool: agent}, chat, user_id=user)


def _err(resp):
    return ((resp.error or {}).get("message", "")) if resp is not None else ""


def _taint_search_result(orch, chat="c1"):
    """Simulate web_search having emitted URL as a plain string leaf."""
    from orchestrator import taint
    tracker = orch._taint_tracker(chat)
    tracker.record_output([{"type": "text", "content": URL}],
                          taint.classify_source("web-research-1", "web_search"),
                          taint.TRUSTED)
    assert tracker.trust_of(URL) == taint.UNTRUSTED
    return tracker


# --------------------------------------------------------------------------- #
# (1) read-only fetch is not a sink
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_search_then_fetch_page_is_not_denied(orch):
    _taint_search_result(orch)
    resp = await _dispatch(orch, "fetch_page", {"url": URL},
                           request="research quantum error correction")
    assert "blocked" not in _err(resp)
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
async def test_search_then_summarize_url_is_not_denied(orch):
    _taint_search_result(orch)
    resp = await _dispatch(orch, "summarize_url", {"url": URL},
                           request="summarise the top hit", agent="summarizer-1")
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
async def test_untrusted_value_into_a_real_sink_is_still_denied(orch):
    _taint_search_result(orch)
    resp = await _dispatch(orch, "send_email", {"to": "bob@x", "body": URL},
                           request="email bob the link")
    assert "blocked" in _err(resp)
    assert "untrusted" in _err(resp)


# --------------------------------------------------------------------------- #
# (2) user-intent exemption
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_value_typed_by_user_passes_the_sink(orch):
    _taint_search_result(orch)
    resp = await _dispatch(orch, "send_email", {"to": "bob@x", "body": URL},
                           request=f"email bob this link: {URL}")
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
async def test_user_exemption_is_exact_not_fuzzy(orch):
    _taint_search_result(orch)
    resp = await _dispatch(orch, "send_email", {"to": "bob@x", "body": URL},
                           request="email bob the example.org article")
    assert "blocked" in _err(resp)


@pytest.mark.asyncio
async def test_user_exemption_reads_the_contextvar_request(orch):
    """handle_chat_message threads the turn's text through the task-local
    context var; the gate must honour it even when the per-chat map is
    empty."""
    from orchestrator import orchestrator as om
    _taint_search_result(orch)
    token = om._ACTIVE_REQUEST_TEXT.set(f"send {URL} to bob")
    try:
        orch._active_request = {}
        ws = MagicMock()
        resp = await orch.execute_single_tool(
            ws, _tc("send_email", {"body": URL}), {"send_email": "a1"},
            "c1", user_id="u1")
    finally:
        om._ACTIVE_REQUEST_TEXT.reset(token)
    assert "No agent available" in _err(resp)


# --------------------------------------------------------------------------- #
# (3) denial is audited
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_taint_denial_leaves_an_audit_row(orch, recorded):
    _taint_search_result(orch)
    resp = await _dispatch(orch, "send_email", {"to": "bob@x", "body": URL},
                           request="email bob the link")
    assert "blocked" in _err(resp)
    denied = [r for r in recorded if r.action_type == "tool.send_email.denied"]
    assert len(denied) == 1
    row = denied[0]
    assert row.event_class == "agent_tool_call"
    assert row.agent_id == "web-research-1"
    assert row.outcome == "failure"
    assert row.conversation_id == "c1"
    assert row.actor_user_id == "u1"
    assert row.inputs_meta.get("gate") == "taint"
    assert "untrusted" in (row.outcome_detail or "")
    # Never the values — only arg metadata.
    assert URL not in json.dumps(row.inputs_meta)
    assert "bob@x" not in json.dumps(row.inputs_meta)


@pytest.mark.asyncio
async def test_taint_denial_audit_uses_session_claims(orch, recorded):
    _taint_search_result(orch)
    orch._active_request = {"c1": "email bob"}
    ws = MagicMock()
    orch.ui_sessions[ws] = {"sub": "session-sub"}
    resp = await orch.execute_single_tool(
        ws, _tc("send_email", {"body": URL}), {"send_email": "web-research-1"},
        "c1", user_id="u1")
    assert "blocked" in _err(resp)
    denied = [r for r in recorded if r.action_type == "tool.send_email.denied"]
    assert denied and denied[0].actor_user_id == "session-sub"


@pytest.mark.asyncio
async def test_taint_denial_audit_failure_does_not_change_the_refusal(orch, monkeypatch):
    from audit import recorder as rec_mod
    boom = MagicMock()

    async def _record(ev):
        raise RuntimeError("audit down")
    boom.record = _record
    prev = rec_mod.get_recorder()
    rec_mod.set_recorder(boom)
    try:
        _taint_search_result(orch)
        resp = await _dispatch(orch, "send_email", {"body": URL}, request="email bob")
    finally:
        rec_mod.set_recorder(prev)
    assert "blocked" in _err(resp)


@pytest.mark.asyncio
async def test_allowed_call_emits_no_denied_row(orch, recorded):
    _taint_search_result(orch)
    await _dispatch(orch, "fetch_page", {"url": URL}, request="read it")
    assert not [r for r in recorded if r.action_type.endswith(".denied")]


# --------------------------------------------------------------------------- #
# escalate semantics unchanged: internal data still flows
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_internal_data_into_a_sink_is_not_enforced(orch):
    from orchestrator import taint
    orch._taint_tracker("c1").mark("internal-value", taint.INTERNAL)
    resp = await _dispatch(orch, "send_email", {"body": "internal-value"},
                           request="email it")
    assert "No agent available" in _err(resp)
