"""Feature 055 US1 — the first-turn loading contract (server side).

Root cause under test: the turn-start welcome-blanking ``ui_render []``
reached the web client one RTT after send and destroyed its optimistic
skeleton (hideSkeleton + setHTML), leaving a blank canvas for the whole first
turn. With FF_FIRST_TURN_CONTRACT on, the frame is not sent — clients purge
the wel_-identified welcome components locally. Flag off restores the legacy
frame byte-for-byte. Also covers the all-tools-denied loop exit, which
previously ended the turn without a terminal ``chat_status done`` (stuck
skeletons on every client).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.feature_flags import flags  # noqa: E402

USER = "first-turn-user"


@pytest.fixture
def orch(orchestrator_factory):
    o = orchestrator_factory()
    o._llm_store.set_sync(USER, provider="custom",
                          base_url="http://test.invalid/v1",
                          model="test-model", api_key="test-key")
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o._record_llm_call = AsyncMock()
    o._record_llm_unconfigured = AsyncMock()
    o._safe_send = AsyncMock()
    o.send_ui_render = AsyncMock()
    hb = MagicMock()
    hb.cancel = MagicMock()
    o._start_heartbeat = AsyncMock(return_value=hb)
    o._send_or_replace_components = AsyncMock(return_value=[])
    o._emit_llm_usage_report = AsyncMock()
    o._deliver_round_components = AsyncMock(return_value=[])
    return o


def _ws(o, user_id=USER):
    ws = MagicMock()
    o.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    return ws


# ── _retire_welcome_canvas ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_on_sends_no_blanking_frame(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "first_turn_contract", True)
    ws = _ws(orch)
    orch._ws_welcome[id(ws)] = True

    await orch._retire_welcome_canvas(ws)

    orch.send_ui_render.assert_not_awaited()
    assert id(ws) not in orch._ws_welcome, "bookkeeping must still pop"


@pytest.mark.asyncio
async def test_flag_off_restores_legacy_blanking_frame(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "first_turn_contract", False)
    ws = _ws(orch)
    orch._ws_welcome[id(ws)] = True

    await orch._retire_welcome_canvas(ws)

    orch.send_ui_render.assert_awaited_once_with(ws, [])
    assert id(ws) not in orch._ws_welcome


@pytest.mark.asyncio
async def test_non_welcome_socket_is_a_noop_under_either_flag(orch, monkeypatch):
    for flag_value in (True, False):
        monkeypatch.setitem(flags._flags, "first_turn_contract", flag_value)
        ws = _ws(orch)
        await orch._retire_welcome_canvas(ws)
    orch.send_ui_render.assert_not_awaited()


@pytest.mark.asyncio
async def test_blanking_send_failure_never_raises(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "first_turn_contract", False)
    ws = _ws(orch)
    orch._ws_welcome[id(ws)] = True
    orch.send_ui_render = AsyncMock(side_effect=RuntimeError("socket gone"))

    await orch._retire_welcome_canvas(ws)  # must not raise

    assert id(ws) not in orch._ws_welcome


# ── all-tools-denied loop exit sends a terminal done ──────────────────────


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content,
                           tool_calls=tool_calls, reasoning_content=None)


def _tc(name="forecast_tool", cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments="{}"))


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


def _register(o, tool_id="forecast_tool", agent_id="a-1"):
    from shared.protocol import AgentCard, AgentSkill
    o.agent_cards[agent_id] = AgentCard(
        name="t", description="d", agent_id=agent_id,
        skills=[AgentSkill(name="forecast", description="s", id=tool_id,
                           input_schema={"type": "object"})])
    o.agents[agent_id] = MagicMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed.return_value = True


def _done_statuses(o):
    out = []
    for call in o._safe_send.await_args_list:
        try:
            frame = json.loads(call.args[1])
        except Exception:
            continue
        if frame.get("type") == "chat_status" and frame.get("status") == "done":
            out.append(frame)
    return out


@pytest.mark.asyncio
async def test_denied_break_sends_done(orch, monkeypatch):
    for mod in ("agentic_creation", "scheduling_chat", "memory_chat",
                "desktop_codegen"):
        monkeypatch.setattr(f"orchestrator.{mod}.should_inject",
                            lambda draft_agent_id: False)
    _register(orch)
    ws = _ws(orch)
    chat_id = f"ft-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id=USER)
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result=None, error={"message": "This tool is restricted by your permissions."},
        ui_components=[], correlation_id=None))

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(tool_calls=[_tc()]), _usage()

    orch._call_llm = fake_llm
    try:
        await orch.handle_chat_message(ws, "keep trying", chat_id, user_id=USER)
        assert _done_statuses(orch), (
            "the all-tools-denied exit must send a terminal chat_status done "
            "(clients key their loading-state teardown on it)")
    finally:
        await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id=USER)
