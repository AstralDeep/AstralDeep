"""Tests for orchestrator/chain_authority.py and subtasks.py: host meta-tools stay
bounded by a machine task's scope, mutating meta-tools require attended policy, and
an unrecognized host tool fails closed.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.chain_authority import MachineAuthority, machine_scope_ceiling
from orchestrator.orchestrator import Orchestrator
from shared.protocol import MCPResponse


META_TOOLS = [
    ("__memory__", "remember", "orchestrator.memory_chat"),
    ("__orchestrator__", "create_capability", "orchestrator.agentic_creation"),
    ("__orchestrator__", "extend_agent", "orchestrator.agentic_creation"),
    ("__scheduler__", "schedule_recurring_task", "orchestrator.scheduling_chat"),
    ("__persistent_assignments__", "ongoing_agent", "persistent_agents.chat_tools"),
]
READ_TOOLS = [
    ("__memory__", "memory_get", "orchestrator.memory_chat"),
    ("__memory__", "memory_search", "orchestrator.memory_chat"),
    ("__desktop_codegen__", "offer_desktop_codegen", "orchestrator.desktop_codegen"),
]
SUBTASK = ("__subtasks__", "delegate_subtasks", "orchestrator.subtasks")


def _host(*, scopes=(), turn_class="scheduled_job", standing=False, agent_id=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch.ui_sessions = {}
    orch.send_ui_render = AsyncMock()
    orch._audit_gate_denial = AsyncMock()
    socket = type("Socket", (), {})()
    authority = MachineAuthority(
        access_token="review-token", allowed_scopes=list(scopes),
        principal=f"machine:{turn_class}", user_id="owner", consent_ref="grant",
        turn_class=turn_class, scope_ceiling_required=not standing, agent_id=agent_id,
    )
    orch._bind_machine_turn(socket, authority)
    return orch, socket, authority


async def _dispatch(orch, socket, tool, path):
    agent_id, tool_name, _ = tool
    call = SimpleNamespace(function=SimpleNamespace(
        name=tool_name, arguments=json.dumps({"value": "test preference"}),
    ))
    method = orch.execute_single_tool if path == "single" else orch.execute_parallel_tools
    result = await method(
        socket, call if path == "single" else [call], {tool_name: agent_id},
        chat_id="chat", user_id="owner",
    )
    return result if path == "single" else result[0]


def _handler(monkeypatch, tool):
    handler = AsyncMock(return_value=MCPResponse(result={"handler_ran": True}))
    monkeypatch.setattr(importlib.import_module(tool[2]), "handle_meta_tool", handler)
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "parallel"])
@pytest.mark.parametrize("tool", META_TOOLS)
@pytest.mark.parametrize("turn_class", ["scheduled_job", "persistent_assignment"])
async def test_mutating_meta_tools_require_attended_policy(monkeypatch, path, tool, turn_class):
    orch, socket, _ = _host(scopes=("tools:read", "tools:write", "tools:system"),
                            turn_class=turn_class)
    handler = _handler(monkeypatch, tool)
    result = await _dispatch(orch, socket, tool, path)
    assert result.error["code"] == "machine_meta_scope_denied"
    handler.assert_not_awaited()
    assert orch._audit_gate_denial.await_args.kwargs["gate"] == "machine_scope"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "parallel"])
@pytest.mark.parametrize("tool", META_TOOLS + READ_TOOLS + [SUBTASK])
async def test_explicit_empty_denies_every_meta_handler(monkeypatch, path, tool):
    orch, socket, _ = _host()
    handler = _handler(monkeypatch, tool)
    result = await _dispatch(orch, socket, tool, path)
    assert result.error["code"] == "machine_meta_scope_denied"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "parallel"])
@pytest.mark.parametrize("tool", READ_TOOLS + [SUBTASK])
async def test_read_discovery_and_scoped_decomposition_remain_available(monkeypatch, path, tool):
    orch, socket, _ = _host(scopes=("tools:read",))
    handler = _handler(monkeypatch, tool)
    result = await _dispatch(orch, socket, tool, path)
    assert result.result == {"handler_ran": True}
    handler.assert_awaited_once()
    orch._audit_gate_denial.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "parallel"])
@pytest.mark.parametrize("kind", ["interactive", "parser_replay", "draft_self_test"])
async def test_interactive_and_unspecified_standing_consent_keep_existing_behavior(monkeypatch, path, kind):
    orch, socket, _ = _host(
        turn_class=kind if kind != "interactive" else "scheduled_job",
        standing=kind != "interactive",
    )
    if kind == "interactive":
        orch.ui_sessions[socket] = {"sub": "owner"}
    handler = _handler(monkeypatch, META_TOOLS[0])
    result = await _dispatch(orch, socket, META_TOOLS[0], path)
    assert result.result == {"handler_ran": True}
    handler.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_class", ["parser_replay", "draft_self_test"])
async def test_standing_class_explicit_empty_is_still_denied(monkeypatch, turn_class):
    orch, socket, _ = _host(turn_class=turn_class)
    handler = _handler(monkeypatch, META_TOOLS[0])
    result = await _dispatch(orch, socket, META_TOOLS[0], "single")
    assert result.error["code"] == "machine_meta_scope_denied"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "agent", "missing", "list", "nonread"])
async def test_discovery_does_not_broaden_owner_agent_or_scope(monkeypatch, change):
    orch, socket, authority = _host(scopes=("tools:read",))
    binding = orch.ui_sessions[socket]
    if change == "owner":
        binding["sub"] = "different-owner"
    elif change == "agent":
        orch._bind_machine_turn(socket, replace(authority, agent_id="specific-agent"))
    elif change == "missing":
        binding.pop("_machine_authority_scopes")
    elif change == "list":
        binding["_machine_authority_scopes"] = ["tools:read"]
    else:
        orch._bind_machine_turn(socket, replace(authority, allowed_scopes=["tools:search"]))
    handler = _handler(monkeypatch, READ_TOOLS[0])
    result = await _dispatch(orch, socket, READ_TOOLS[0], "single")
    assert result.error["code"] == "machine_meta_scope_denied"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "agent", 1])
async def test_real_agent_calls_keep_the_shared_real_agent_gate(agent_id):
    orch, socket, _ = _host()
    assert await orch._guard_machine_meta_tool(socket, agent_id, "read", {}, "chat", "owner") is None


@pytest.mark.asyncio
async def test_unrecognized_host_tool_is_fail_closed():
    orch, socket, _ = _host(scopes=("tools:read",))
    refusal = await orch._guard_machine_meta_tool(socket, "__future__", "new_tool", {}, "chat", "owner")
    assert refusal.error["code"] == "machine_meta_scope_denied"


@pytest.mark.asyncio
async def test_subtask_keeps_private_ceiling_and_agent_binding(monkeypatch):
    from orchestrator import subtasks
    from orchestrator.chain_authority import ChainBudget

    orch, socket, _ = _host(scopes=("tools:read",), agent_id="specific-agent")
    orch.history = SimpleNamespace(create_chat=MagicMock(return_value="child-chat"))
    orch._chain_budgets = {}
    seen = []

    async def child_turn(child, *args, **kwargs):
        binding = orch.ui_sessions[child]
        seen.append(dict(binding))
        assert machine_scope_ceiling(binding) == frozenset({"tools:read"})
        assert binding["_machine_authority_agent"] == "specific-agent"
        refusal = await orch._guard_machine_meta_tool(
            child, "__memory__", "remember", {}, "child-chat", "owner",
        )
        assert refusal.error["code"] == "machine_meta_scope_denied"
        await child.send_json({"type": "chat_message", "payload": {"text": "read result"}})

    orch.handle_chat_message = child_turn
    monkeypatch.setattr(subtasks, "_audit_subtask", AsyncMock())
    monkeypatch.setattr(subtasks, "_progress", AsyncMock())
    assert await orch._guard_machine_meta_tool(
        socket, "__subtasks__", "delegate_subtasks", {}, "chat", "owner",
    ) is None
    result = await subtasks._run_one(
        orch, {"title": "Read", "instruction": "Read approved source"},
        user_id="owner", parent_chat_id="chat", parent_ws=socket,
        allowed_tools=["read"], budget=ChainBudget(turn_id="turn", chat_id="chat"),
        correlation_id="review",
    )
    assert result.status == "ok"
    assert len(seen) == 1
    assert list(orch.ui_sessions) == [socket]
