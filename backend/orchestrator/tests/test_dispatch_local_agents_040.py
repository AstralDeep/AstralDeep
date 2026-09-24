"""Regression tests for orchestrator/orchestrator.py's execute_single_tool dispatching
to in-process (websocket=None) built-in agents, which its availability guard
previously missed while execute_parallel_tools already handled them.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard, AgentSkill, MCPResponse


def _make_card(agent_id: str, tools: list) -> AgentCard:
    return AgentCard(
        name=agent_id,
        description="",
        agent_id=agent_id,
        skills=[AgentSkill(id=t, name=t, description="", input_schema={}) for t in tools],
        metadata={},
    )


def _build_orch() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = {"dice-roller-1": _make_card("dice-roller-1", ["roll_dice"])}
    orch.agents = {}
    orch.a2a_clients = {}
    orch.local_agents = {"dice-roller-1": MagicMock()}
    orch.agent_urls = {}
    orch.security_flags = {}
    orch.ui_sessions = {}

    orch.concurrency_cap = MagicMock()
    orch.concurrency_cap.acquire = AsyncMock(return_value=True)
    orch.concurrency_cap.release = AsyncMock()
    orch.concurrency_cap.inflight_jobs = MagicMock(return_value=[])
    orch.concurrency_cap.max_per_user_agent = 3
    orch._pending_cap_entries = {}

    db = MagicMock()
    db.get_user_disabled_agents.return_value = []
    db.get_chat_agent.return_value = None
    db.get_user_tool_selection.return_value = None
    orch.history = MagicMock()
    orch.history.db = db
    orch.history.get_file_mappings = MagicMock(return_value={})

    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    orch.credential_manager = MagicMock()
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value={})
    orch._llm_store = MagicMock()
    orch._llm_store.get = AsyncMock(return_value=None)
    orch._llm_store.get_system = AsyncMock(return_value=None)
    orch.hooks = MagicMock()
    orch.hooks.emit = AsyncMock(return_value=SimpleNamespace(action=None, modified_args=None, reason=None))

    orch._get_delegation_token = AsyncMock(return_value=None)
    orch._delegation_required = MagicMock(return_value=False)
    orch._is_long_running_tool = MagicMock(return_value=False)
    orch._map_file_paths = lambda chat_id, args, user_id="legacy": args
    orch._active_request = {}
    orch._job_context = {}
    orch._taint_tracker = MagicMock()
    orch._execute_with_retry = AsyncMock(
        return_value=MCPResponse(result={"content": [{"type": "text", "text": "rolled"}]})
    )

    orch._rendered_ui = []

    async def _capture_render(websocket, components, target=None):
        orch._rendered_ui.append({"target": target, "components": components})

    orch.send_ui_render = _capture_render
    return orch


def _make_tool_call(name: str, args: dict = None):
    return SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name=name, arguments=json.dumps(args or {})),
    )


def _pin_gates_off(monkeypatch) -> None:
    for flag in ("FF_HITL_HIGHRISK", "FF_TAINT_TRACKING", "FF_RUNTIME_SUPERVISOR"):
        monkeypatch.setenv(flag, "false")


@pytest.mark.asyncio
async def test_single_tool_dispatch_reaches_in_process_agent(monkeypatch) -> None:
    _pin_gates_off(monkeypatch)
    orch = _build_orch()
    result = await orch.execute_single_tool(
        websocket=MagicMock(),
        tool_call=_make_tool_call("roll_dice", {"n": 3}),
        tool_to_agent={"roll_dice": "dice-roller-1"},
        chat_id="chat-1",
        user_id="alice",
    )

    assert orch._execute_with_retry.await_count == 1
    call = orch._execute_with_retry.await_args
    assert "dice-roller-1" in (list(call.args) + list(call.kwargs.values()))
    assert result is not None
    assert not (result.error and "No agent available" in result.error.get("message", ""))
    assert all(
        "No agent available" not in json.dumps(r["components"])
        for r in orch._rendered_ui
    )


@pytest.mark.asyncio
async def test_unknown_tool_still_gets_no_agent_alert(monkeypatch) -> None:
    _pin_gates_off(monkeypatch)
    orch = _build_orch()
    orch._find_tool_owner = MagicMock(return_value=None)
    result = await orch.execute_single_tool(
        websocket=MagicMock(),
        tool_call=_make_tool_call("phantom_tool"),
        tool_to_agent={},
        chat_id="chat-1",
        user_id="alice",
    )
    assert result.error and "No agent available" in result.error["message"]
    assert orch._execute_with_retry.await_count == 0
