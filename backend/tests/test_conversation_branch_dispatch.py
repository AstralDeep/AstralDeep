"""Tests that branch turn dispatch (edit-and-resend and regenerate replays) flows through
the ordinary chat dispatch with identity admission, tool permission gates and HITL review.
A persisted-turn dispatch calling a reviewed tool still raises its card. The persisted user
turn is never duplicated in the model context.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.protocol import AgentCard, AgentSkill
from tests.helpers.registered_human import registered_chat


@pytest.fixture
async def orchestrator(orchestrator_factory):
    orch = await asyncio.to_thread(orchestrator_factory)
    try:
        await asyncio.to_thread(
            orch._llm_store.set_sync,
            "branch-dispatch-user",
            provider="custom",
            base_url="http://test.invalid/v1",
            model="test-model",
            api_key="test-key",
        )
        orch.audit_recorder = MagicMock()
        orch.audit_recorder.record = AsyncMock()
        orch._record_llm_call = AsyncMock()
        orch._record_llm_unconfigured = AsyncMock()
        orch._safe_send = AsyncMock()
        orch.send_ui_render = AsyncMock()
        orch._send_or_replace_components = AsyncMock()
        orch._emit_llm_usage_report = AsyncMock()
        fake_heartbeat = MagicMock()
        fake_heartbeat.cancel = MagicMock()
        orch._start_heartbeat = AsyncMock(return_value=fake_heartbeat)
        yield orch
    finally:
        try:
            await asyncio.to_thread(orch._llm_store.clear_sync, "branch-dispatch-user")
        finally:
            await orch._close_started_services()


def _fake_websocket(orchestrator, user_id="branch-dispatch-user"):
    ws = MagicMock()
    orchestrator.ui_sessions[ws] = {
        "sub": user_id,
        "preferred_username": user_id,
    }
    return ws


def _llm_tool_call_message(tool, arguments):
    return SimpleNamespace(
        content="",
        tool_calls=[SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(name=tool, arguments=json.dumps(arguments)),
        )],
        reasoning_content=None,
    )


@pytest.fixture
def reviewed_email_agent(orchestrator, monkeypatch):
    from orchestrator import hitl
    from shared.feature_flags import flags

    monkeypatch.setattr(hitl, "hitl_enabled", lambda: True)
    monkeypatch.setattr("audit.hooks.get_recorder", lambda: None)
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    monkeypatch.setitem(flags._flags, "user_skills", False)
    orchestrator.agent_cards["email-agent"] = AgentCard(
        name="email", description="sends email", agent_id="email-agent",
        skills=[AgentSkill(
            name="send_email", description="send an email", id="send_email",
            input_schema={"type": "object"},
        )],
    )
    orchestrator.agents["email-agent"] = MagicMock()
    from orchestrator import tool_permissions as tp

    monkeypatch.setattr(tp.ToolPermissionManager, "is_tool_allowed",
                        lambda self, *args, **kwargs: True)
    monkeypatch.setattr(tp.ToolPermissionManager, "get_tool_scope_map",
                        lambda self, *args, **kwargs: {"send_email": "tools:write"})
    orchestrator._source_bound_read_tools = ()
    orchestrator._source_bound_de_escalations = ()
    return "email-agent"


def _persisted_dispatch(orchestrator, ws, chat_id, message, persisted_message_id):
    async def dispatch(websocket, text, chat, user_id, **kwargs):
        return await orchestrator._serialized_chat(
            websocket, text, chat, text,
            user_id=user_id,
            persisted_turn={"message_id": persisted_message_id},
        )

    return dispatch


async def test_persisted_turn_dispatch_still_raises_the_reviewed_tool_card(
        orchestrator, reviewed_email_agent):
    owner = "branch-dispatch-user"
    ws = _fake_websocket(orchestrator)
    chat_id = str(uuid.uuid4())
    await asyncio.to_thread(orchestrator.history.create_chat, chat_id, user_id=owner)
    await asyncio.to_thread(
        orchestrator.history.add_message, chat_id, "user",
        "Email alex@example.org about the launch", user_id=owner,
    )
    persisted_message_id = await asyncio.to_thread(
        orchestrator.history.get_latest_message_id, chat_id, owner,
    )
    orchestrator._ws_active_chat[id(ws)] = chat_id

    calls = {"n": 0}

    async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                            feature: str = "tool_dispatch", **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                _llm_tool_call_message(
                    "send_email", {"to": "alex@example.org", "body": "Launch update"}),
                SimpleNamespace(total_tokens=10),
            )
        return (
            SimpleNamespace(content="All done.", tool_calls=None,
                            reasoning_content=None),
            SimpleNamespace(total_tokens=5),
        )

    orchestrator._call_llm = fake_call_llm

    await registered_chat(
        orchestrator, ws, "Email alex@example.org about the launch", chat_id,
        user_id=owner,
        dispatch=_persisted_dispatch(
            orchestrator, ws, chat_id, "Email alex@example.org about the launch",
            persisted_message_id,
        ),
    )

    assert orchestrator._hitl_pending_calls, "reviewed tool must raise its card"
    pending = next(iter(orchestrator._hitl_pending_calls.values()))
    assert pending.chat == chat_id
    assert pending.tool == "send_email"
    chat = await asyncio.to_thread(
        orchestrator.history.get_chat, chat_id, user_id=owner)
    user_turns = [m for m in chat["messages"] if m["role"] == "user"]
    assert len(user_turns) == 1, "the persisted user turn must not be appended twice"


async def test_persisted_turn_context_carries_the_branch_history(orchestrator):
    owner = "branch-dispatch-user"
    ws = _fake_websocket(orchestrator)
    chat_id = str(uuid.uuid4())
    await asyncio.to_thread(orchestrator.history.create_chat, chat_id, user_id=owner)
    await asyncio.to_thread(
        orchestrator.history.add_message, chat_id, "user",
        "First question", user_id=owner,
    )
    persisted_message_id = await asyncio.to_thread(
        orchestrator.history.get_latest_message_id, chat_id, owner,
    )
    await asyncio.to_thread(
        orchestrator.history.add_message, chat_id, "assistant",
        "First answer", user_id=owner,
    )
    orchestrator._ws_active_chat[id(ws)] = chat_id

    captured = {}

    async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                            feature: str = "tool_dispatch", **kwargs):
        captured["messages"] = messages
        return (
            SimpleNamespace(content="Done.", tool_calls=None,
                            reasoning_content=None),
            SimpleNamespace(total_tokens=5),
        )

    orchestrator._call_llm = fake_call_llm

    await registered_chat(
        orchestrator, ws, "First question", chat_id, user_id=owner,
        dispatch=_persisted_dispatch(
            orchestrator, ws, chat_id, "First question", persisted_message_id,
        ),
    )

    user_entries = [m for m in captured["messages"] if m["role"] == "user"]
    assert [entry["content"] for entry in user_entries] == ["First question"], (
        "the persisted user turn must appear exactly once in the model context"
    )
