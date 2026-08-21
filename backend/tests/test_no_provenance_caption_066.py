"""Regression test — owner decision (2026-08-03): chat replies carry NO
appended provenance caption.

Feature 030 appended a server-composed provenance chip ("Model knowledge
only …" / "Based on this turn's tool results …") to every final chat
render. The owner removed it; ``test_wiring_030.py`` pins the method's
absence at the unit level. This file pins the removal through the real
rich-components final-turn path: parsed components go to the
canvas via ``_send_or_replace_components`` while the chat rail's summary
render is exactly ``list(leak_alerts) + chat_core`` — nothing appended.

The harness is deliberately application-composition-free. This contract is
about deterministic parsing, authorization, provenance stamping, persistence
adaptation, and render routing; booting PostgreSQL, LETS, and every background
service adds no signal. Explicit owner-scoped in-memory stores retain the real
``Orchestrator`` methods at those boundaries without restoring a legacy DB.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

USER = "prov-caption-user"


class _WebSocket:
    """Hashable registered UI socket with no background-task authority."""

    task = None


class _InMemoryHistory:
    """Owner-scoped history boundary needed by one ordinary chat turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chats: dict[str, dict] = {}
        self._next_message_id = 1

    def _owned(self, chat_id: str, user_id: str) -> dict:
        chat = self._chats.get(chat_id)
        if chat is None or chat["user_id"] != user_id:
            raise PermissionError("chat is not owned by this user")
        return chat

    def create_chat(self, chat_id: str, *, user_id: str) -> None:
        with self._lock:
            if chat_id in self._chats:
                raise ValueError("chat already exists")
            self._chats[chat_id] = {
                "user_id": user_id,
                "agent_id": None,
                "messages": [],
            }

    def delete_chat(self, chat_id: str, *, user_id: str) -> None:
        with self._lock:
            self._owned(chat_id, user_id)
            del self._chats[chat_id]

    def get_chat_agent(self, chat_id: str, *, user_id: str):
        with self._lock:
            return self._owned(chat_id, user_id)["agent_id"]

    def add_message(
        self,
        chat_id: str,
        role: str,
        content,
        *,
        user_id: str,
    ) -> int:
        with self._lock:
            chat = self._owned(chat_id, user_id)
            message_id = self._next_message_id
            self._next_message_id += 1
            chat["messages"].append(
                {
                    "id": message_id,
                    "role": role,
                    "content": copy.deepcopy(content),
                }
            )
            return message_id

    def get_latest_message_id(self, chat_id: str, user_id: str | None = None):
        if user_id is None:
            raise PermissionError("owner_id is required")
        with self._lock:
            messages = self._owned(chat_id, user_id)["messages"]
            return messages[-1]["id"] if messages else None

    def get_chat(self, chat_id: str, *, user_id: str):
        with self._lock:
            chat = self._owned(chat_id, user_id)
            return {"messages": copy.deepcopy(chat["messages"])}

    def get_file_mappings(self, chat_id: str, *, user_id: str):
        with self._lock:
            self._owned(chat_id, user_id)
            return []


class _InMemoryWorkspace:
    """Owner-scoped component store for the real provenance/upsert method."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], list[dict]] = {}
        self.snapshots: list[tuple[str, str, str, int | None]] = []

    async def alive_rows(self, chat_id: str, user_id: str):
        return copy.deepcopy(self._rows.get((user_id, chat_id), []))

    def upsert(
        self,
        chat_id: str,
        user_id: str,
        components: list[dict],
        *,
        force_component_id: str | None = None,
    ) -> list[dict]:
        key = (user_id, chat_id)
        rows = self._rows.setdefault(key, [])
        operations = []
        for component in components:
            component_id = force_component_id or f"wc_{len(rows) + 1}"
            stored = copy.deepcopy(component)
            stored["component_id"] = component_id
            rows.append(
                {
                    "component_id": component_id,
                    "component_data": stored,
                }
            )
            operations.append(
                {
                    "component_id": component_id,
                    "component": copy.deepcopy(stored),
                    "created": True,
                }
            )
        return operations

    def snapshot(
        self,
        chat_id: str,
        user_id: str,
        *,
        cause: str,
        turn_message_id: int | None = None,
    ) -> None:
        if (user_id, chat_id) not in self._rows:
            raise PermissionError("workspace is not owned by this user")
        self.snapshots.append((user_id, chat_id, cause, turn_message_id))

    def components(self, chat_id: str, user_id: str) -> list[dict]:
        return [
            copy.deepcopy(row["component_data"])
            for row in self._rows.get((user_id, chat_id), [])
        ]


class _ToolPermissions:
    """Fail-closed owner/tool authorization double for visibility filtering."""

    def __init__(self) -> None:
        self.allowed_calls: list[tuple[str, str, str]] = []

    @staticmethod
    def get_tool_selection(_user_id: str, _agent_id: str):
        return None

    @staticmethod
    def list_disabled_agents(_user_id: str):
        return []

    def is_tool_allowed(self, user_id: str, agent_id: str, tool_id: str) -> bool:
        self.allowed_calls.append((user_id, agent_id, tool_id))
        return (user_id, agent_id, tool_id) == (USER, "a-1", "forecast_tool")


class _Heartbeat:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _InMemoryChatStepRecorder:
    """Typed no-I/O recorder retaining the real loop's phase lifecycle."""

    def __init__(self, **_values) -> None:
        self.completed: list[str] = []

    async def start(self, _kind: str, _label: str) -> str:
        return "phase-1"

    async def complete(self, step_id: str) -> None:
        self.completed.append(step_id)

    async def error(self, step_id: str, _message: str) -> None:
        self.completed.append(step_id)


@pytest.fixture
def orch(monkeypatch):
    from llm_config import LLMUnavailable
    from orchestrator import (
        agentic_creation,
        chat_steps,
        desktop_codegen,
        memory_chat,
        scheduling_chat,
        subtasks,
    )
    from orchestrator.orchestrator import Orchestrator
    from shared.feature_flags import flags

    for flag_name in (
        "agentic_creation",
        "context_engineering",
        "datamarking",
        "desktop_codegen",
        "knowledge_synthesis",
        "memory_chat",
        "message_compaction",
        "recursive_delegation",
        "scheduling_chat",
        "skill_packs",
        "task_state_machine",
    ):
        monkeypatch.setitem(flags._flags, flag_name, False)
    monkeypatch.setitem(flags._flags, "component_refine", True)
    for module in (
        agentic_creation,
        desktop_codegen,
        memory_chat,
        scheduling_chat,
        subtasks,
    ):
        monkeypatch.setattr(module, "should_inject", lambda _draft_id: False)
    monkeypatch.setattr(chat_steps, "ChatStepRecorder", _InMemoryChatStepRecorder)

    workspace_audit = AsyncMock()
    monkeypatch.setattr("audit.hooks.record_workspace_event", workspace_audit)

    o = Orchestrator.__new__(Orchestrator)
    o.history = _InMemoryHistory()
    o.workspace = _InMemoryWorkspace()
    o.ui_sessions = {}
    o.ui_clients = []
    o.agent_cards = {}
    o.agents = {}
    o.local_agents = {}
    o.security_flags = {}
    o.tool_permissions = _ToolPermissions()
    o.cancelled_sessions = {}
    o._chain_budgets = {}
    o._chat_recorders = {}
    o._ws_active_chat = {}
    o.token_usage = {}
    o.personalization_service = SimpleNamespace(
        build_prompt_fragment=lambda _user_id, *, skill_lines=None: ""
    )
    o._LLMUnavailable = LLMUnavailable

    async def resolve_owner_scoped_llm(websocket):
        claims = o.ui_sessions.get(websocket)
        if not isinstance(claims, dict) or claims.get("sub") != USER:
            raise LLMUnavailable("no owner-scoped test credential")
        return object(), "user", SimpleNamespace(model="test-model")

    o._resolve_llm_client_for = AsyncMock(side_effect=resolve_owner_scoped_llm)
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o._record_llm_call = AsyncMock()
    o._record_llm_unconfigured = AsyncMock()
    o._safe_send = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.send_ui_upsert = AsyncMock()
    hb = _Heartbeat()
    o._start_heartbeat = AsyncMock(return_value=hb)
    o._emit_llm_usage_report = AsyncMock()
    o._deliver_round_components = AsyncMock(return_value=[])
    o._notify_phi_if_detected = AsyncMock()
    o.summarize_chat_title = AsyncMock()
    o._design_turn_post_done = AsyncMock(return_value=None)
    real_upsert = o._send_or_replace_components
    o._send_or_replace_components = AsyncMock(wraps=real_upsert)
    o._workspace_audit = workspace_audit
    o._heartbeat = hb
    return o


def _register(o, tool_id="forecast_tool", agent_id="a-1"):
    from shared.protocol import AgentCard, AgentSkill
    o.agent_cards[agent_id] = AgentCard(
        name="t", description="d", agent_id=agent_id,
        skills=[AgentSkill(name="forecast", description="s", id=tool_id,
                           input_schema={"type": "object"})])
    o.agents[agent_id] = MagicMock()


def _ws(o, user_id=USER):
    ws = _WebSocket()
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
         "data": {"x": [1, 2], "y": [72, 75]},
         # Model-authored trust is untrusted input. The real upsert method
         # must replace this with the server-derived value.
         "provenance": "grounded"},
    ])

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(content=final_json), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "chart the weather", chat_id, user_id=USER)

    # The rich-components branch ran: the parsed chart went to the canvas path
    # (generic "chart" is normalized to "plotly_chart" during validation).
    orch._send_or_replace_components.assert_awaited()
    orch.send_ui_upsert.assert_awaited_once()
    canvas_sent = orch.workspace.components(chat_id, USER)
    assert any(c.get("type") in ("chart", "plotly_chart")
               and c.get("title") == "Daily highs" for c in canvas_sent)
    assert canvas_sent[0]["provenance"] == "generated"
    assert orch.tool_permissions.allowed_calls == [
        (USER, "a-1", "forecast_tool")
    ]
    orch._resolve_llm_client_for.assert_awaited_once_with(ws)

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
    assert all(_target_of(call) != "canvas" for call in renders)
    await asyncio.sleep(0)
    orch._workspace_audit.assert_awaited_once()
    assert orch._heartbeat.cancelled
    await _cleanup(orch, chat_id)
