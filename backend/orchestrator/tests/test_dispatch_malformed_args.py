"""Tests for orchestrator/orchestrator.py's hard-gate on malformed tool-call argument
JSON in execute_single_tool and execute_parallel_tools: never silently dispatch with
empty args, always return a retryable error.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard, AgentSkill


def _make_card(agent_id: str, tools: list, display_name: str = None) -> AgentCard:
    return AgentCard(
        name=display_name or agent_id,
        description="",
        agent_id=agent_id,
        skills=[AgentSkill(id=t, name=t, description="", input_schema={}) for t in tools],
        metadata={},
    )


def _build_orch() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = {
        "general-1": _make_card("general-1", ["read_spreadsheet", "ocr"], "General"),
    }
    orch.agents = {}
    orch.a2a_clients = {}
    orch.local_agents = {}
    orch.agent_urls = {}
    orch.security_flags = {}
    orch.ui_sessions = {}
    orch.credential_manager = MagicMock()
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value={})

    orch.concurrency_cap = MagicMock()
    orch.concurrency_cap.acquire = AsyncMock(return_value=True)
    orch.concurrency_cap.release = AsyncMock()
    orch.concurrency_cap.inflight_jobs = MagicMock(return_value=[])
    orch.concurrency_cap.max_per_user_agent = 3
    orch._pending_cap_entries = {}

    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)

    orch.history = MagicMock()
    orch.history.db = MagicMock()
    orch.history.get_file_mappings = MagicMock(return_value={})

    orch._map_file_paths = MagicMock(side_effect=lambda cid, args, user_id=None: args)
    orch._llm_store = MagicMock()
    orch._llm_store.get = AsyncMock(return_value=None)
    orch._llm_store.get_system = AsyncMock(return_value=None)

    orch._rendered_ui = []

    async def _capture_render(websocket, components, target=None):
        orch._rendered_ui.append({"target": target, "components": components})

    orch.send_ui_render = _capture_render
    return orch


def _make_tool_call(name: str, arguments: str):
    return SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name=name,
            arguments=arguments,
        ),
    )


@pytest.mark.asyncio
async def test_single_tool_malformed_json_returns_error_not_empty_args() -> None:
    orch = _build_orch()
    websocket = MagicMock()
    tool_to_agent = {"read_spreadsheet": "general-1"}
    tool_call = _make_tool_call("read_spreadsheet", "{attachment_id: abc,}")

    result = await orch.execute_single_tool(
        websocket=websocket,
        tool_call=tool_call,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )

    assert result is not None
    assert result.error is not None
    assert result.error["retryable"] is True
    assert "read_spreadsheet" in result.error["message"]
    assert "JSON" in result.error["message"]

    assert len(orch._rendered_ui) == 1
    alert = orch._rendered_ui[0]["components"][0]
    assert alert["type"] == "alert"
    assert alert["variant"] == "error"
    assert "read_spreadsheet" in alert["message"]


@pytest.mark.asyncio
async def test_single_tool_valid_json_still_dispatches_normally() -> None:
    orch = _build_orch()
    websocket = MagicMock()
    tool_to_agent = {"read_spreadsheet": "general-1"}
    tool_call = _make_tool_call("read_spreadsheet", json.dumps({"attachment_id": "abc"}))

    result = await orch.execute_single_tool(
        websocket=websocket,
        tool_call=tool_call,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )
    assert result is not None
    assert result.error is not None
    assert "JSON" not in result.error["message"]
    rendered_msgs = [c["message"] for c in orch._rendered_ui[0]["components"]]
    assert not any("not valid JSON" in m for m in rendered_msgs)


@pytest.mark.asyncio
async def test_parallel_tool_malformed_json_returns_error_not_empty_args() -> None:
    orch = _build_orch()
    websocket = MagicMock()
    tool_to_agent = {"read_spreadsheet": "general-1"}
    tool_calls = [_make_tool_call("read_spreadsheet", "{bad json,,}")]

    results = await orch.execute_parallel_tools(
        websocket=websocket,
        tool_calls=tool_calls,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )

    assert len(results) == 1
    result = results[0]
    assert result is not None
    assert result.error is not None
    assert result.error["retryable"] is True
    assert "read_spreadsheet" in result.error["message"]
    assert "JSON" in result.error["message"]
    assert result.ui_components is None
    assert orch._rendered_ui[0]["components"][0]["type"] == "alert"


@pytest.mark.asyncio
async def test_parallel_tool_mixed_valid_and_malformed() -> None:
    orch = _build_orch()
    websocket = MagicMock()
    tool_to_agent = {"read_spreadsheet": "general-1", "ocr": "general-1"}
    tool_calls = [
        _make_tool_call("read_spreadsheet", json.dumps({"attachment_id": "abc"})),
        _make_tool_call("ocr", "{broken json}"),
    ]

    results = await orch.execute_parallel_tools(
        websocket=websocket,
        tool_calls=tool_calls,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )

    assert len(results) == 2
    ocr_result = results[1]
    assert ocr_result is not None
    assert ocr_result.error is not None
    assert ocr_result.error["retryable"] is True
    assert "ocr" in ocr_result.error["message"]
    assert "JSON" in ocr_result.error["message"]
