"""Tests for Orchestrator.handle_chat_message's text-only fallback
(backend/orchestrator/orchestrator.py): dispatch with no tools available, the
system-prompt addendum, and the additive tools_available_for_user flag.
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

from tests.helpers.registered_human import registered_chat

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture
async def orchestrator(orchestrator_factory):
    orch = await asyncio.to_thread(orchestrator_factory)
    try:
        await asyncio.to_thread(
            orch._llm_store.set_sync,
            "text-only-test-user",
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
        fake_heartbeat = MagicMock()
        fake_heartbeat.cancel = MagicMock()
        orch._start_heartbeat = AsyncMock(return_value=fake_heartbeat)
        orch._send_or_replace_components = AsyncMock()
        orch._emit_llm_usage_report = AsyncMock()
        yield orch
    finally:
        try:
            await asyncio.to_thread(
                orch._llm_store.clear_sync,
                "text-only-test-user",
            )
        finally:
            await orch._close_started_services()


def _rendered_components_text(orchestrator) -> str:
    chunks: list[str] = []
    for call in orchestrator.send_ui_render.call_args_list:
        if len(call.args) >= 2:
            chunks.append(json.dumps(call.args[1], default=str))
    return "\n".join(chunks)


def _fake_websocket(orchestrator, user_id="text-only-test-user"):
    ws = MagicMock()
    orchestrator.ui_sessions[ws] = {
        "sub": user_id,
        "preferred_username": user_id,
    }
    return ws


def _llm_message(content: str = "Paris.", tool_calls=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
    )


def _llm_usage(total_tokens: int = 10):
    return SimpleNamespace(
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=total_tokens,
    )


class TestComputeToolsAvailableForUser:
    def test_returns_false_when_no_agents_registered(self, orchestrator):
        result = orchestrator.compute_tools_available_for_user("any-user")
        assert result is False

    def test_returns_true_when_user_has_one_allowed_tool(self, orchestrator):
        from shared.protocol import AgentCard, AgentSkill
        card = AgentCard(
            name="t", description="d", agent_id="a-1",
            skills=[AgentSkill(
                name="search", description="search", id="search_tool",
                input_schema={"type": "object"},
            )],
        )
        orchestrator.agent_cards["a-1"] = card
        orchestrator.agents["a-1"] = MagicMock()
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.return_value = True

        assert orchestrator.compute_tools_available_for_user("u") is True

    def test_returns_false_when_all_tools_security_blocked(self, orchestrator):
        from shared.protocol import AgentCard, AgentSkill
        card = AgentCard(
            name="t", description="d", agent_id="a-1",
            skills=[AgentSkill(name="t1", description="d", id="t1", input_schema={})],
        )
        orchestrator.agent_cards["a-1"] = card
        orchestrator.agents["a-1"] = MagicMock()
        orchestrator.security_flags["a-1"] = {"t1": {"blocked": True}}
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.return_value = True

        assert orchestrator.compute_tools_available_for_user("u") is False

    def test_returns_false_when_all_tools_permission_blocked(self, orchestrator):
        from shared.protocol import AgentCard, AgentSkill
        card = AgentCard(
            name="t", description="d", agent_id="a-1",
            skills=[AgentSkill(name="t1", description="d", id="t1", input_schema={})],
        )
        orchestrator.agent_cards["a-1"] = card
        orchestrator.agents["a-1"] = MagicMock()
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.return_value = False

        assert orchestrator.compute_tools_available_for_user("u") is False

    def test_skips_disconnected_agents(self, orchestrator):
        from shared.protocol import AgentCard, AgentSkill
        card = AgentCard(
            name="t", description="d", agent_id="a-1",
            skills=[AgentSkill(name="t1", description="d", id="t1", input_schema={})],
        )
        orchestrator.agent_cards["a-1"] = card
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.return_value = True

        assert orchestrator.compute_tools_available_for_user("u") is False

    def test_draft_scope_only_considers_target_agent(self, orchestrator):
        from shared.protocol import AgentCard, AgentSkill
        for a_id in ("a-1", "a-2"):
            orchestrator.agent_cards[a_id] = AgentCard(
                name=a_id, description="d", agent_id=a_id,
                skills=[AgentSkill(name="t", description="d", id=f"t-{a_id}", input_schema={})],
            )
            orchestrator.agents[a_id] = MagicMock()
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.side_effect = (
            lambda u, agent, tool: agent == "a-1"
        )

        assert orchestrator.compute_tools_available_for_user(
            "u", draft_agent_id="a-2"
        ) is False
        assert orchestrator.compute_tools_available_for_user(
            "u", draft_agent_id="a-1"
        ) is True


class TestHandleChatMessageTextOnly:
    @pytest.mark.asyncio
    async def test_dispatches_text_only_when_no_tools(self, orchestrator):
        from orchestrator.orchestrator import TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM

        ws = _fake_websocket(orchestrator)
        chat_id = str(uuid.uuid4())
        await asyncio.to_thread(
            orchestrator.history.create_chat, chat_id, user_id="text-only-test-user")
        captured_call = {}

        async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                                feature: str = "tool_dispatch"):
            captured_call["messages"] = messages
            captured_call["tools_desc"] = tools_desc
            captured_call["feature"] = feature
            return _llm_message("Paris."), _llm_usage()

        orchestrator._call_llm = fake_call_llm

        await registered_chat(orchestrator,
            ws, "What is the capital of France?", chat_id,
            user_id="text-only-test-user",
        )

        assert not captured_call.get("tools_desc"), (
            f"Expected empty tools_desc for text-only path, got {captured_call.get('tools_desc')!r}"
        )

        sys_msg = captured_call["messages"][0]
        assert sys_msg["role"] == "system"
        assert TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM.strip() in sys_msg["content"], (
            "system prompt must include the FR-006a text-only addendum"
        )

        rendered_text = _rendered_components_text(orchestrator)
        assert "No agents connected" not in rendered_text, (
            "legacy 'No agents connected' warning must NOT fire on the text-only path"
        )

        chat = await asyncio.to_thread(
            orchestrator.history.get_chat, chat_id, user_id="text-only-test-user")
        assert chat is not None
        roles = [m["role"] for m in chat["messages"]]
        assert "assistant" in roles, "assistant reply must be saved to history"

        await asyncio.to_thread(
            orchestrator.history.delete_chat, chat_id, user_id="text-only-test-user")

    @pytest.mark.asyncio
    async def test_text_only_dispatch_emits_correct_audit_feature_tag(self, orchestrator):
        ws = _fake_websocket(orchestrator)
        chat_id = str(uuid.uuid4())
        await asyncio.to_thread(
            orchestrator.history.create_chat, chat_id, user_id="text-only-test-user")
        captured_features = []

        async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                                feature: str = "tool_dispatch"):
            captured_features.append(feature)
            return _llm_message("ok"), _llm_usage()

        orchestrator._call_llm = fake_call_llm

        await registered_chat(orchestrator,
            ws, "hello", chat_id, user_id="text-only-test-user"
        )

        assert captured_features == ["chat_dispatch_text_only"], (
            f"text-only dispatch must tag feature='chat_dispatch_text_only', "
            f"got {captured_features!r}"
        )

        await asyncio.to_thread(
            orchestrator.history.delete_chat, chat_id, user_id="text-only-test-user")

    @pytest.mark.asyncio
    async def test_draft_chat_with_no_tools_does_not_fall_through(self, orchestrator):
        ws = _fake_websocket(orchestrator)
        chat_id = str(uuid.uuid4())
        await asyncio.to_thread(
            orchestrator.history.create_chat, chat_id, user_id="text-only-test-user")
        called = {"count": 0}

        async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                                feature: str = "tool_dispatch"):
            called["count"] += 1
            return _llm_message("should-not-be-called"), _llm_usage()

        orchestrator._call_llm = fake_call_llm

        await registered_chat(orchestrator,
            ws, "test draft", chat_id,
            user_id="text-only-test-user",
            draft_agent_id="a-not-registered",
        )

        assert called["count"] == 0, (
            "draft test chat with no tools must NOT enter the text-only branch"
        )
        rendered_text = _rendered_components_text(orchestrator)
        assert "draft" in rendered_text.lower(), (
            f"draft-scope short-circuit must surface a draft-specific alert; "
            f"got: {rendered_text!r}"
        )

        await asyncio.to_thread(
            orchestrator.history.delete_chat, chat_id, user_id="text-only-test-user")

    @pytest.mark.asyncio
    async def test_tool_augmented_dispatch_does_not_inject_addendum(self, orchestrator):
        from orchestrator.orchestrator import TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM
        from shared.protocol import AgentCard, AgentSkill

        card = AgentCard(
            name="t", description="d", agent_id="a-1",
            skills=[AgentSkill(
                name="search", description="search", id="search_tool",
                input_schema={"type": "object"},
            )],
        )
        orchestrator.agent_cards["a-1"] = card
        orchestrator.agents["a-1"] = MagicMock()
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.return_value = True

        ws = _fake_websocket(orchestrator)
        chat_id = str(uuid.uuid4())
        await asyncio.to_thread(
            orchestrator.history.create_chat, chat_id, user_id="text-only-test-user")
        captured = {}

        async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                                feature: str = "tool_dispatch"):
            captured["messages"] = messages
            captured["tools_desc"] = tools_desc
            captured["feature"] = feature
            return _llm_message("answer"), _llm_usage()

        orchestrator._call_llm = fake_call_llm

        await registered_chat(orchestrator,
            ws, "search something", chat_id, user_id="text-only-test-user"
        )

        assert captured["tools_desc"], "tool-augmented turn must have non-empty tools list"
        assert captured["feature"] == "tool_dispatch", (
            f"tool-augmented turn must tag feature='tool_dispatch', got {captured['feature']!r}"
        )
        sys_msg = captured["messages"][0]
        assert TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM.strip() not in sys_msg["content"], (
            "tool-augmented turn must NOT include the text-only addendum"
        )

        await asyncio.to_thread(
            orchestrator.history.delete_chat, chat_id, user_id="text-only-test-user")


class TestAgentListPayload:
    @pytest.mark.asyncio
    async def test_includes_false_when_no_agents(self, orchestrator):
        ws = _fake_websocket(orchestrator)
        sent = []
        orchestrator._safe_send = AsyncMock(side_effect=lambda w, payload: sent.append(payload))

        await orchestrator.send_agent_list(ws)

        assert len(sent) == 1, "send_agent_list must emit exactly one payload"
        payload = json.loads(sent[0])
        assert payload["type"] == "agent_list"
        assert "tools_available_for_user" in payload, (
            "agent_list must always include tools_available_for_user"
        )
        assert payload["tools_available_for_user"] is False
        assert payload["agents"] == []

    @pytest.mark.asyncio
    async def test_includes_true_when_user_has_at_least_one_allowed_tool(self, orchestrator):
        from shared.protocol import AgentCard, AgentSkill

        card = AgentCard(
            name="Search", description="d", agent_id="a-1",
            skills=[AgentSkill(
                name="search", description="search", id="search_tool",
                input_schema={"type": "object"},
            )],
        )
        orchestrator.agent_cards["a-1"] = card
        orchestrator.agents["a-1"] = MagicMock()
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.return_value = True
        orchestrator.tool_permissions.get_agent_scopes.return_value = {"tools:read": True}
        orchestrator.tool_permissions.get_tool_scope_map.return_value = {"search_tool": "tools:read"}
        orchestrator.tool_permissions.get_effective_permissions.return_value = {"search_tool": True}
        orchestrator.user_agent_registry.get_all_agent_ownership = MagicMock(
            return_value=[]
        )
        orchestrator._is_draft_agent = MagicMock(return_value=False)

        ws = _fake_websocket(orchestrator)
        sent = []
        orchestrator._safe_send = AsyncMock(side_effect=lambda w, payload: sent.append(payload))

        await orchestrator.send_agent_list(ws)

        payload = json.loads(sent[0])
        assert payload["tools_available_for_user"] is True, (
            f"flag should be True with one allowed tool, got payload={payload}"
        )
        assert any(a["id"] == "a-1" for a in payload["agents"])
