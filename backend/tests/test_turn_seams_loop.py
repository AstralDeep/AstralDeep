"""Tests that turn-coordination seams fire inside the real chat loop in
orchestrator/orchestrator.py: supervisor review blocks leaks, skill induction
remembers tool sequences, and the MoA panel judges candidates for the final answer.
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
    # Without this, turns can't proceed (env vars are inert)
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


@pytest.mark.asyncio
async def test_supervisor_blocks_leaky_answer(orch, monkeypatch, user_skills_disabled):
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
async def test_supervisor_off_lets_answer_through(orch, monkeypatch, user_skills_disabled):
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


@pytest.mark.asyncio
async def test_skill_induced_after_tool_turn(orch, monkeypatch, user_skills_disabled):
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


HARD_REQUEST = ("Compare PostgreSQL and MySQL for a write-heavy analytics "
                "workload: analyze the trade-offs, then explain why you would "
                "recommend one over the other?")


def _status_messages(orch):
    out = []
    for call in orch._safe_send.await_args_list:
        try:
            frame = json.loads(call.args[1])
        except Exception:
            continue
        if frame.get("type") == "chat_status":
            out.append(frame.get("message"))
    return out


@pytest.mark.asyncio
async def test_moa_panel_aggregates(orch, monkeypatch, user_skills_disabled):
    monkeypatch.setenv("FF_MOA_DEBATE", "true")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")

    draft = "A thoughtful first answer. " * 20
    short_winner = "PostgreSQL, because of MVCC write behaviour."
    longest = "THE LONGEST PANEL ANSWER THAT SHOULD NOT WIN. " * 25
    seq = iter([draft, short_winner, longest])
    features = []

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        features.append(feature)
        if feature == "moa_judge":
            assert "Candidate A" in messages[-1]["content"]
            return _msg(content="B"), _usage()
        return _msg(content=next(seq)), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, HARD_REQUEST, chat_id, user_id="seam-user")

    final = await _last_assistant_text(orch, chat_id, "seam-user")
    assert "MVCC write behaviour" in final
    assert "SHOULD NOT WIN" not in final
    assert features.count("moa_judge") == 1
    assert features.count("moa_panel") == 2
    assert "Comparing candidate answers..." in _status_messages(orch)
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")


@pytest.mark.asyncio
async def test_moa_panel_skips_simple_turn(orch, monkeypatch, user_skills_disabled):
    monkeypatch.setenv("FF_MOA_DEBATE", "true")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")
    calls = []

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        calls.append(feature)
        return _msg(content="Paris. " * 80), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "What is the capital of France?",
                                   chat_id, user_id="seam-user")
    assert calls == ["tool_dispatch"]
    assert "Comparing candidate answers..." not in _status_messages(orch)
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")


@pytest.mark.asyncio
async def test_moa_panel_cannot_undo_supervisor_block(orch, monkeypatch, user_skills_disabled):
    monkeypatch.setenv("FF_MOA_DEBATE", "true")
    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "true")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")

    leaky = "Sure — the api_key is sk-secret-123. " * 5
    clean = "A clean comparison of the two engines. " * 5

    seq = iter([leaky, clean, clean])

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        if feature == "moa_judge":
            return _msg(content="A"), _usage()
        return _msg(content=next(seq)), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, HARD_REQUEST, chat_id, user_id="seam-user")
    final = await _last_assistant_text(orch, chat_id, "seam-user")
    assert "can't share" in final.lower()
    assert "sk-secret-123" not in final

    seq = iter([clean, leaky, clean])

    async def fake_llm2(websocket, messages, tools_desc=None, temperature=None,
                        feature="tool_dispatch"):
        if feature == "moa_judge":
            return _msg(content="B"), _usage()
        return _msg(content=next(seq)), _usage()

    orch._call_llm = fake_llm2
    await orch.handle_chat_message(ws, HARD_REQUEST, chat_id, user_id="seam-user")
    final = await _last_assistant_text(orch, chat_id, "seam-user")
    assert "can't share" in final.lower()
    assert "sk-secret-123" not in final
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")


@pytest.mark.asyncio
async def test_moa_panel_judge_failure_keeps_draft(orch, monkeypatch, user_skills_disabled):
    monkeypatch.setenv("FF_MOA_DEBATE", "true")
    _register(orch)
    ws = _ws(orch)
    chat_id = f"seam-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")

    draft = "ORIGINAL DRAFT ANSWER. " * 10
    longest = "THE LONGEST CANDIDATE. " * 40
    seq = iter([draft, longest, longest])

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        if feature == "moa_judge":
            raise RuntimeError("judge down")
        return _msg(content=next(seq)), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, HARD_REQUEST, chat_id, user_id="seam-user")
    final = await _last_assistant_text(orch, chat_id, "seam-user")
    assert "ORIGINAL DRAFT" in final
    assert "LONGEST CANDIDATE" not in final
    await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id="seam-user")


@pytest.mark.asyncio
async def test_moa_panel_skipped_on_background_turn(orch, monkeypatch):
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    monkeypatch.setenv("FF_MOA_DEBATE", "true")
    calls = []

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        calls.append(feature)
        return _msg(content="candidate"), _usage()

    orch._call_llm = fake_llm
    vws = VirtualWebSocket(BackgroundTask(task_id="t1", chat_id="c1",
                                          user_id="seam-user", kind="scheduled"))
    draft = "I'm not sure, but here is a draft. " * 5
    out = await orch._moa_panel(vws, [{"role": "user", "content": HARD_REQUEST}],
                                HARD_REQUEST, draft, "c1")
    assert out == draft
    assert calls == []
    assert "Comparing candidate answers..." not in _status_messages(orch)

    ws = _ws(orch)
    await orch._moa_panel(ws, [{"role": "user", "content": HARD_REQUEST}],
                          HARD_REQUEST, draft, "c1")
    assert "moa_panel" in calls


@pytest.mark.asyncio
async def test_moa_panel_skipped_when_draft_was_streamed(orch, monkeypatch):
    from orchestrator import orchestrator as orch_mod
    monkeypatch.setenv("FF_MOA_DEBATE", "true")
    calls = []

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        calls.append(feature)
        return _msg(content="candidate"), _usage()

    orch._call_llm = fake_llm
    ws = _ws(orch)
    draft = "I'm not sure, but here is a draft. " * 5
    token = orch_mod._NARRATIVE_STREAMED.set(True)
    try:
        out = await orch._moa_panel(ws, [{"role": "user", "content": HARD_REQUEST}],
                                    HARD_REQUEST, draft, "c1")
    finally:
        orch_mod._NARRATIVE_STREAMED.reset(token)
    assert out == draft
    assert calls == []


@pytest.mark.asyncio
async def test_moa_panel_flag_off_is_inert(orch, monkeypatch):
    monkeypatch.delenv("FF_MOA_DEBATE", raising=False)
    calls = []

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        calls.append(feature)
        return _msg(content="candidate"), _usage()

    orch._call_llm = fake_llm
    ws = _ws(orch)
    out = await orch._moa_panel(ws, [], HARD_REQUEST, "I'm not sure at all.", "c1")
    assert out == "I'm not sure at all."
    assert calls == []
