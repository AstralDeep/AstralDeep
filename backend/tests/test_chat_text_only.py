"""Tests for feature 008-llm-text-only-chat.

Covers the orchestrator's text-only fallback in :meth:`handle_chat_message`
and the additive ``tools_available_for_user`` flag on the
``agent_list`` WebSocket message.

These tests touch a real Postgres-backed ``HistoryManager`` (the
orchestrator constructor requires it). They patch every LLM-dependent
and websocket-dependent surface so no actual LLM call is made.

References:
- specs/008-llm-text-only-chat/spec.md
- specs/008-llm-text-only-chat/contracts/ws-agent-list.md
- specs/008-llm-text-only-chat/contracts/audit-event-text-only.md
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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def orchestrator(orchestrator_factory):
    """Real Orchestrator instance with the fixture user's PERSISTED LLM
    config seeded (so the 054 pre-flight resolver succeeds), but every
    outbound side effect (LLM call, websocket send, audit recorder)
    replaced with a MagicMock/AsyncMock so we can introspect what was
    attempted.
    """
    orch = await asyncio.to_thread(orchestrator_factory)
    try:
        # Feature 054: chat turns pre-flight the acting user's persisted
        # record (env vars are inert) — seed the fixture user.
        await asyncio.to_thread(
            orch._llm_store.set_sync,
            "text-only-test-user",
            provider="custom",
            base_url="http://test.invalid/v1",
            model="test-model",
            api_key="test-key",
        )
        # Replace audit recorder with an AsyncMock so .record() is awaitable.
        orch.audit_recorder = MagicMock()
        orch.audit_recorder.record = AsyncMock()
        orch._record_llm_call = AsyncMock()
        orch._record_llm_unconfigured = AsyncMock()

        # Per-call surfaces patched on the instance so each test starts clean.
        orch._safe_send = AsyncMock()
        orch.send_ui_render = AsyncMock()
        # Heartbeat task: return a MagicMock whose .cancel() is a sync Mock
        # (cancel() on a real asyncio.Task is synchronous — using AsyncMock
        # would mark it as a coroutine and trip a "never awaited" warning).
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
    """Concatenate every components payload pushed via ``send_ui_render``
    into one searchable string. Avoids JSON-serializing the websocket
    arg (which is a MagicMock and not JSON-serializable)."""
    chunks: list[str] = []
    for call in orchestrator.send_ui_render.call_args_list:
        # send_ui_render(websocket, components, *, target=...). The
        # second positional arg is the components list.
        if len(call.args) >= 2:
            chunks.append(json.dumps(call.args[1], default=str))
    return "\n".join(chunks)


def _fake_websocket(orchestrator, user_id="text-only-test-user"):
    """Build a websocket-shaped MagicMock with a populated ui_session
    so ``_get_user_id`` and ``_llm_audit_principals`` resolve cleanly.
    """
    ws = MagicMock()
    orchestrator.ui_sessions[ws] = {
        "sub": user_id,
        "preferred_username": user_id,
    }
    return ws


def _llm_message(content: str = "Paris.", tool_calls=None):
    """Mimic an OpenAI chat-completion message object enough that
    ``handle_chat_message`` can route it as a final-text response."""
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


# ---------------------------------------------------------------------------
# compute_tools_available_for_user — pure helper
# ---------------------------------------------------------------------------

class TestComputeToolsAvailableForUser:
    """Phase 2 (T002) — validates the per-user tool-availability helper."""

    def test_returns_false_when_no_agents_registered(self, orchestrator):
        # Fresh orchestrator: no agents, no security flags.
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
        # Patch tool_permissions to allow the tool.
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
        """Agent in agent_cards but not in self.agents must be ignored."""
        from shared.protocol import AgentCard, AgentSkill
        card = AgentCard(
            name="t", description="d", agent_id="a-1",
            skills=[AgentSkill(name="t1", description="d", id="t1", input_schema={})],
        )
        orchestrator.agent_cards["a-1"] = card
        # NOT adding to self.agents — agent is not connected.
        orchestrator.tool_permissions = MagicMock()
        orchestrator.tool_permissions.is_tool_allowed.return_value = True

        assert orchestrator.compute_tools_available_for_user("u") is False

    def test_draft_scope_only_considers_target_agent(self, orchestrator):
        from shared.protocol import AgentCard, AgentSkill
        # Two agents; only one is the draft.
        for a_id in ("a-1", "a-2"):
            orchestrator.agent_cards[a_id] = AgentCard(
                name=a_id, description="d", agent_id=a_id,
                skills=[AgentSkill(name="t", description="d", id=f"t-{a_id}", input_schema={})],
            )
            orchestrator.agents[a_id] = MagicMock()
        orchestrator.tool_permissions = MagicMock()
        # Allow tools on a-1 but not on a-2.
        orchestrator.tool_permissions.is_tool_allowed.side_effect = (
            lambda u, agent, tool: agent == "a-1"
        )

        # When draft scope is a-2 (the disallowed one), result is False.
        assert orchestrator.compute_tools_available_for_user(
            "u", draft_agent_id="a-2"
        ) is False
        # When draft scope is a-1, result is True.
        assert orchestrator.compute_tools_available_for_user(
            "u", draft_agent_id="a-1"
        ) is True


# ---------------------------------------------------------------------------
# US1 — handle_chat_message text-only path
# ---------------------------------------------------------------------------

class TestHandleChatMessageTextOnly:
    """US1 (T004–T010) — text-only dispatch when no tools are available."""

    @pytest.mark.asyncio
    async def test_dispatches_text_only_when_no_tools(self, orchestrator):
        """FR-001/FR-002: with zero connected agents, the chat dispatches
        with empty tools_desc, includes the FR-006a addendum in the
        system prompt, sends NO 'No agents connected' alert, and writes
        the assistant reply to history."""
        from orchestrator.orchestrator import TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM

        ws = _fake_websocket(orchestrator)
        chat_id = f"text-only-{uuid.uuid4().hex[:8]}"
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

        await orchestrator.handle_chat_message(
            ws, "What is the capital of France?", chat_id,
            user_id="text-only-test-user",
        )

        # Tools list must be empty for the text-only branch.
        assert not captured_call.get("tools_desc"), (
            f"Expected empty tools_desc for text-only path, got {captured_call.get('tools_desc')!r}"
        )

        # System prompt MUST include the FR-006a addendum.
        sys_msg = captured_call["messages"][0]
        assert sys_msg["role"] == "system"
        assert TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM.strip() in sys_msg["content"], (
            "system prompt must include the FR-006a text-only addendum"
        )

        # No 'No agents connected' Alert was emitted on the failure path.
        rendered_text = _rendered_components_text(orchestrator)
        assert "No agents connected" not in rendered_text, (
            "legacy 'No agents connected' warning must NOT fire on the text-only path"
        )

        # Assistant reply was persisted to history.
        chat = await asyncio.to_thread(
            orchestrator.history.get_chat, chat_id, user_id="text-only-test-user")
        assert chat is not None
        roles = [m["role"] for m in chat["messages"]]
        assert "assistant" in roles, "assistant reply must be saved to history"

        # Cleanup.
        await asyncio.to_thread(
            orchestrator.history.delete_chat, chat_id, user_id="text-only-test-user")

    @pytest.mark.asyncio
    async def test_text_only_dispatch_emits_correct_audit_feature_tag(self, orchestrator):
        """FR-009 + contracts/audit-event-text-only.md: text-only turns
        propagate ``feature='chat_dispatch_text_only'`` into _call_llm so
        the audit recorder emits a distinguishable event."""
        ws = _fake_websocket(orchestrator)
        chat_id = f"text-only-audit-{uuid.uuid4().hex[:8]}"
        await asyncio.to_thread(
            orchestrator.history.create_chat, chat_id, user_id="text-only-test-user")
        captured_features = []

        async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                                feature: str = "tool_dispatch"):
            captured_features.append(feature)
            return _llm_message("ok"), _llm_usage()

        orchestrator._call_llm = fake_call_llm

        await orchestrator.handle_chat_message(
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
        """FR-010: a draft test chat that has no usable tools shows the
        existing draft-diagnostic warning rather than silently falling
        through to text-only mode."""
        ws = _fake_websocket(orchestrator)
        chat_id = f"text-only-draft-{uuid.uuid4().hex[:8]}"
        await asyncio.to_thread(
            orchestrator.history.create_chat, chat_id, user_id="text-only-test-user")
        called = {"count": 0}

        async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                                feature: str = "tool_dispatch"):
            called["count"] += 1
            return _llm_message("should-not-be-called"), _llm_usage()

        orchestrator._call_llm = fake_call_llm

        await orchestrator.handle_chat_message(
            ws, "test draft", chat_id,
            user_id="text-only-test-user",
            draft_agent_id="a-not-registered",
        )

        # _call_llm must NOT fire on the draft scope short-circuit.
        assert called["count"] == 0, (
            "draft test chat with no tools must NOT enter the text-only branch"
        )
        # An Alert was rendered explaining the draft has no tools.
        rendered_text = _rendered_components_text(orchestrator)
        assert "draft" in rendered_text.lower(), (
            f"draft-scope short-circuit must surface a draft-specific alert; "
            f"got: {rendered_text!r}"
        )

        await asyncio.to_thread(
            orchestrator.history.delete_chat, chat_id, user_id="text-only-test-user")

    @pytest.mark.asyncio
    async def test_tool_augmented_dispatch_does_not_inject_addendum(self, orchestrator):
        """FR-011: turns with at least one available tool must NOT have
        the text-only addendum in the system prompt and MUST tag
        feature='tool_dispatch'."""
        from orchestrator.orchestrator import TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM
        from shared.protocol import AgentCard, AgentSkill

        # Register a connected agent with one allowed tool.
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
        chat_id = f"tool-aug-{uuid.uuid4().hex[:8]}"
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

        await orchestrator.handle_chat_message(
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


# ---------------------------------------------------------------------------
# US2 — agent_list payload extension
# ---------------------------------------------------------------------------

class TestAgentListPayload:
    """US2 (T011–T012) — additive ``tools_available_for_user`` flag."""

    @pytest.mark.asyncio
    async def test_includes_false_when_no_agents(self, orchestrator):
        """contracts/ws-agent-list.md: with zero registered agents,
        the broadcast field must be False."""
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
        # Make all permission lookups permissive.
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
