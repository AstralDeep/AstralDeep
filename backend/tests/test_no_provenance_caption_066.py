"""Regression test — owner decision (2026-08-03): chat replies carry NO
appended provenance caption.

Feature 030 appended a server-composed provenance chip ("Model knowledge
only …" / "Based on this turn's tool results …") to every final chat
render. The owner removed it; ``test_wiring_030.py`` pins the method's
absence at the unit level. This file pins the removal at the INTEGRATION
level for the rich-components final turn: parsed components go to the
canvas via ``_send_or_replace_components`` while the chat rail's summary
render is exactly ``list(leak_alerts) + chat_core`` — nothing appended.

Harness cloned from ``test_analysis_render_targets.py`` (the smallest
existing full in-process chat-turn rig with the ``_call_llm`` seam).
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

USER = "prov-caption-user"


@pytest.fixture
def orch():
    from orchestrator.orchestrator import Orchestrator

    o = Orchestrator()
    # Feature 054: chat turns pre-flight the acting user's PERSISTED LLM
    # config (env vars are inert) — seed the fixture user so turns proceed.
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


def _register(o, tool_id="forecast_tool", agent_id="a-1"):
    from shared.protocol import AgentCard, AgentSkill
    o.agent_cards[agent_id] = AgentCard(
        name="t", description="d", agent_id=agent_id,
        skills=[AgentSkill(name="forecast", description="s", id=tool_id,
                           input_schema={"type": "object"})])
    o.agents[agent_id] = MagicMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed.return_value = True


def _ws(o, user_id=USER):
    ws = MagicMock()
    o.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    return ws


def _msg(content=None, tool_calls=None, reasoning=None):
    return SimpleNamespace(role="assistant", content=content,
                           tool_calls=tool_calls, reasoning_content=reasoning)


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


async def _chat(o):
    chat_id = f"caption-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id=USER)
    return chat_id


async def _cleanup(o, chat_id):
    await asyncio.to_thread(o.history.delete_chat, chat_id, user_id=USER)


def _target_of(call) -> str:
    """The effective ui_render target of a recorded send_ui_render call."""
    if "target" in call.kwargs:
        return call.kwargs["target"]
    if len(call.args) > 2:
        return call.args[2]
    return "canvas"


def _components_json(call) -> str:
    return json.dumps(call.args[1] if len(call.args) > 1 else call.kwargs.get("components"))


@pytest.mark.asyncio
async def test_rich_components_chat_summary_has_no_provenance_caption(orch):
    """A final LLM reply of rich UI JSON routes components to the canvas and
    renders the chat summary WITHOUT the retired provenance caption."""
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)

    final_json = json.dumps([
        {"type": "chart", "title": "Daily highs",
         "data": {"x": [1, 2], "y": [72, 75]}},
    ])

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(content=final_json), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "chart the weather", chat_id, user_id=USER)

    # The rich-components branch ran: the parsed chart went to the canvas path
    # (generic "chart" is normalized to "plotly_chart" during validation).
    orch._send_or_replace_components.assert_awaited()
    canvas_sent = orch._send_or_replace_components.await_args.args[1]
    assert any(c.get("type") in ("chart", "plotly_chart")
               and c.get("title") == "Daily highs" for c in canvas_sent)

    # …and the chat rail got the summary render (leak_alerts + chat_core).
    renders = orch.send_ui_render.await_args_list
    chat_renders = [c for c in renders if _target_of(c) == "chat"]
    assert chat_renders, "expected the chat-summary ui_render"

    # Owner decision pinned: NO provenance caption in anything rendered this
    # turn — neither the model-only wording nor the tool-grounded wording.
    for call in renders:
        payload = _components_json(call)
        assert "Model knowledge only" not in payload
        assert "Based on this turn's tool results" not in payload
    await _cleanup(orch, chat_id)
