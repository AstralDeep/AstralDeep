"""Integration test for the 5th dispatch gate added in feature 015 / DSML fix.

When the model emits a structured (OpenAI) tool call for a tool that was
filtered out at chat-time tool-list construction, `tool_to_agent.get(name)`
returns None and the dispatcher used to fall through to the generic
"No agent available" alert. The 5th gate now intercepts this case:
it consults `_diagnose_disabled_tool` and emits the friendly variant
that names the agent + tool + how to re-enable.
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


def _build_orch(*, disabled_agents=None, saved_selection=None,
                chat_to_agent=None) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = {
        "general-1": _make_card("general-1", ["read_spreadsheet", "ocr"], "General"),
    }
    orch.agents = {}  # no live agents — forces the 5th gate path
    orch.a2a_clients = {}
    orch.local_agents = {}  # feature 040: guard also consults in-process agents
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
    db.get_user_disabled_agents.return_value = disabled_agents or []
    db.get_chat_agent.side_effect = lambda c: (chat_to_agent or {}).get(c)
    db.get_user_tool_selection.side_effect = lambda u, a: (saved_selection or {}).get((u, a))
    orch.history = MagicMock()
    orch.history.db = db
    orch.history.get_file_mappings = MagicMock(return_value={})

    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    orch.credential_manager = MagicMock()
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value={})
    # Feature 054: dispatch resolves the call context's persisted LLM
    # config via orch._llm_store (async get/get_system); none here.
    orch._llm_store = MagicMock()
    orch._llm_store.get = AsyncMock(return_value=None)
    orch._llm_store.get_system = AsyncMock(return_value=None)
    orch.hooks = MagicMock()
    orch.hooks.emit = AsyncMock(return_value=SimpleNamespace(action=None, modified_args=None, reason=None))

    # Capture rendered UI components.
    orch._rendered_ui = []

    async def _capture_render(websocket, components, target=None):
        orch._rendered_ui.append({"target": target, "components": components})

    orch.send_ui_render = _capture_render
    return orch


def _make_tool_call(name: str, args: dict = None):
    """Build a minimal tool-call object matching OpenAI SDK's shape."""
    return SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(args or {}),
        ),
    )


@pytest.mark.asyncio
async def test_dispatch_blocked_when_tool_filtered_by_picker() -> None:
    """User has disabled `read_spreadsheet` in the picker → dispatch returns the friendly alert."""
    orch = _build_orch(
        chat_to_agent={"chat-1": "general-1"},
        saved_selection={("alice", "general-1"): ["ocr"]},  # read_spreadsheet excluded
    )
    websocket = MagicMock()

    # tool_to_agent omits the filtered tool — that's the realistic chat-time state.
    tool_to_agent = {"ocr": "general-1"}
    tool_call = _make_tool_call("read_spreadsheet", {"attachment_id": "abc"})

    result = await orch.execute_single_tool(
        websocket=websocket,
        tool_call=tool_call,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )

    # Friendly alert was rendered.
    assert len(orch._rendered_ui) == 1
    rendered = orch._rendered_ui[0]
    assert rendered["target"] == "chat"
    assert len(rendered["components"]) == 1
    alert = rendered["components"][0]
    assert alert["type"] == "alert"
    assert alert["variant"] == "warning"
    assert "read_spreadsheet" in alert["message"]
    assert "tool picker" in alert["message"]

    # Dispatch did not reach upstream. The alert was rendered separately so
    # the correlated error envelope stays unambiguous.
    assert result is not None
    assert result.error is not None
    assert "read_spreadsheet" in result.error["message"]
    assert result.ui_components is None


@pytest.mark.asyncio
async def test_dispatch_blocked_when_agent_disabled_by_user() -> None:
    orch = _build_orch(disabled_agents=["general-1"])
    websocket = MagicMock()
    tool_to_agent = {}  # everything filtered (whole agent disabled)
    tool_call = _make_tool_call("read_spreadsheet")

    result = await orch.execute_single_tool(
        websocket=websocket,
        tool_call=tool_call,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )

    alert = orch._rendered_ui[0]["components"][0]
    assert alert["variant"] == "warning"
    assert "General" in alert["message"]
    assert "Agents settings" in alert["message"]
    assert result.error is not None


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_falls_through_to_generic_gate() -> None:
    """A tool that no agent owns hits the existing 'No agent available' gate, not the 5th."""
    orch = _build_orch()
    websocket = MagicMock()
    tool_to_agent = {}
    tool_call = _make_tool_call("totally_made_up_tool")

    result = await orch.execute_single_tool(
        websocket=websocket,
        tool_call=tool_call,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )

    # Generic "No agent available" alert (existing behavior; not the 5th-gate alert).
    alert = orch._rendered_ui[0]["components"][0]
    assert alert["type"] == "alert"
    assert "No agent available" in alert["message"]
    assert "totally_made_up_tool" in alert["message"]
    assert result.error is not None


@pytest.mark.asyncio
async def test_dispatch_proceeds_when_tool_is_enabled_in_picker() -> None:
    """Sanity check — when the tool is allowed, the 5th gate doesn't fire."""
    orch = _build_orch(
        chat_to_agent={"chat-1": "general-1"},
        saved_selection={("alice", "general-1"): ["read_spreadsheet"]},
    )
    websocket = MagicMock()
    # tool IS in tool_to_agent → 5th gate's `not agent_id` precondition fails.
    tool_to_agent = {"read_spreadsheet": "general-1"}
    tool_call = _make_tool_call("read_spreadsheet")

    # No agents are connected so it'll still fall to "No agent available", but
    # NOT to the 5th-gate disabled alert.
    result = await orch.execute_single_tool(
        websocket=websocket,
        tool_call=tool_call,
        tool_to_agent=tool_to_agent,
        chat_id="chat-1",
        user_id="alice",
    )
    alert = orch._rendered_ui[0]["components"][0]
    assert "No agent available" in alert["message"]
    assert "tool picker" not in alert["message"]
    assert result.error is not None
