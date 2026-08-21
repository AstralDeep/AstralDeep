"""End-to-end tests that the 033 turn-coordination seams fire inside the REAL
handle_chat_message ReAct loop (stubbed LLM + tool execution, real history).

Proves three representative seams behave when their flag is ON:
  * supervisor output review (C-S5) replaces a leaky drafted answer,
  * skill induction (C-N10) remembers a successful tool sequence,
  * the MoA panel (C-N9) aggregates candidates into the final answer.
The coordinator logic for every capability is unit-tested in test_turn_hooks.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def orch(orchestrator_factory):
    o = orchestrator_factory()
    # Feature 054: chat turns pre-flight the acting user's PERSISTED LLM
    # config (env vars are inert) — seed the fixture user so turns proceed.
    o._llm_store.set_sync("seam-user", provider="custom",
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
    o._send_or_replace_components = AsyncMock()
    o._emit_llm_usage_report = AsyncMock()
    o._deliver_round_components = AsyncMock(return_value=[])
    return o


def _register(o, tool_id="search_tool", agent_id="a-1"):
    from shared.protocol import AgentCard, AgentSkill
    o.agent_cards[agent_id] = AgentCard(
        name="t", description="d", agent_id=agent_id,
        skills=[AgentSkill(name="search", description="s", id=tool_id,
                           input_schema={"type": "object"})])
    o.agents[agent_id] = MagicMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed.return_value = True


def _ws(o, user_id="seam-user"):
    ws = MagicMock()
    o.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    return ws


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content,
                           tool_calls=tool_calls, reasoning_content=None)


def _tc(name="search_tool", cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments="{}"))


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


async def _last_assistant_text(o, chat_id, user_id):
    data = await asyncio.to_thread(o.history.get_chat, chat_id, user_id=user_id) or {}
    texts = []
    for m in data.get("messages", []):
        if m.get("role") == "assistant":
            texts.append(
                json.dumps(
                    m.get("content"),
                    default=lambda value: (
                        dict(value) if isinstance(value, Mapping) else str(value)
                    ),
                )
            )
    return texts[-1] if texts else ""


# --------------------------------------------------------------------------- #
# Supervisor output review (C-S5): a leaky drafted answer is blocked.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_supervisor_blocks_leaky_answer(orch, monkeypatch):
    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "true")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(content="Sure — the api_key is sk-secret-123."), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "what is the key?", chat_id, user_id="seam-user")

    final = await _last_assistant_text(orch, chat_id, "seam-user")
    assert "can't share" in final.lower()
    assert "sk-secret-123" not in final
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")


@pytest.mark.asyncio
async def test_supervisor_off_lets_answer_through(orch, monkeypatch):
    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "false")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(content="The weather is sunny."), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "weather?", chat_id, user_id="seam-user")
    assert "sunny" in (await _last_assistant_text(orch, chat_id, "seam-user")).lower()
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")


# --------------------------------------------------------------------------- #
# Skill induction (C-N10): a successful tool turn is remembered.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_skill_induced_after_tool_turn(orch, monkeypatch):
    monkeypatch.setenv("FF_SKILL_MEMORY", "true")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"ok": True}, error=None, ui_components=[], correlation_id=None))

    calls = {"n": 0}

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        calls["n"] += 1
        if calls["n"] == 1:
            return _msg(tool_calls=[_tc()]), _usage()
        return _msg(content="All done searching."), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "search the web for cats", chat_id, user_id="seam-user")

    store = orch._skill_store("seam-user")
    assert len(store) == 1
    assert "search_tool" in store[0].tools
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")


# --------------------------------------------------------------------------- #
# MoA panel (C-N9): a hard pure-reasoning answer is aggregated from candidates.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_moa_panel_aggregates(orch, monkeypatch):
    monkeypatch.setenv("FF_MOA_DEBATE", "true")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")

    draft = "A thoughtful first answer. " * 20            # >400 chars, no tools
    winner = "THE BEST AND LONGEST PANEL ANSWER. " * 25   # longest ⇒ wins
    short = "brief."
    seq = iter([draft, short, winner])

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(content=next(seq)), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "explain quantum entanglement in depth",
                                   chat_id, user_id="seam-user")

    final = await _last_assistant_text(orch, chat_id, "seam-user")
    assert "BEST AND LONGEST" in final
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")
