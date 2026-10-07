"""Idle-client card-button failure continuity regression tests.

Verifies that _handle_component_action, _handle_component_refine, and
_handle_component_restore raise _CardActionTerminalFailure on every refusal
and failure path so the outer _execute() machinery terminates the operation
as FAILED rather than COMPLETED.  Chat-surface alert must be sent before the
raise so the message is delivered even when the operation_status frame is
ignored by an idle client with no matching generation fence.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.orchestrator import Orchestrator, _CardActionTerminalFailure

# ---------------------------------------------------------------------------
# Minimal orchestrator stub
# ---------------------------------------------------------------------------

def _build_orch() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.security_flags = {}
    orch.ui_sessions = {}
    orch._ws_active_chat = {}
    orch._ws_timeline_mode = {}
    orch._workspace_locks = {}

    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)

    orch.workspace = MagicMock()
    orch.workspace.aget_by_component_id = AsyncMock(return_value=None)

    orch.rote = MagicMock()
    orch.rote.get_profile = MagicMock(
        return_value=SimpleNamespace(device_type=SimpleNamespace(value="desktop"))
    )

    orch._rendered_ui: list = []
    orch._sent_raw: list = []

    async def _capture_render(websocket, components, target=None, speak=True):
        orch._rendered_ui.append({"target": target, "components": components})

    async def _capture_send(websocket, data):
        orch._sent_raw.append(data)

    orch.send_ui_render = _capture_render
    orch._safe_send = _capture_send
    orch._audit_workspace_denial = AsyncMock()
    return orch


def _ws():
    ws = MagicMock()
    ws.__hash__ = lambda s: id(s)
    return ws


# ---------------------------------------------------------------------------
# _handle_component_action: refusal / failure cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_component_action_missing_context_raises() -> None:
    """Missing chat_id or component_id must raise _CardActionTerminalFailure."""
    orch = _build_orch()
    ws = _ws()
    with pytest.raises(_CardActionTerminalFailure) as exc_info:
        await orch._handle_component_action(ws, "alice", {})
    assert exc_info.value.terminal_code == "card_action_refused"
    assert any(r["target"] == "chat" for r in orch._rendered_ui)
    assert any("chat_status" in s for s in orch._sent_raw)


@pytest.mark.asyncio
async def test_component_action_component_not_found_raises() -> None:
    """Non-existent component must raise _CardActionTerminalFailure."""
    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value=None)
    with pytest.raises(_CardActionTerminalFailure) as exc_info:
        await orch._handle_component_action(
            ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
        )
    assert exc_info.value.terminal_code == "component_not_found"
    assert any(r["target"] == "chat" for r in orch._rendered_ui)


@pytest.mark.asyncio
async def test_component_action_permission_denied_raises() -> None:
    """Tool permission denial must raise _CardActionTerminalFailure with permission_denied code."""
    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value={
        "component_data": {
            "_source_agent": "general-1",
            "_source_tool": "read_spreadsheet",
        }
    })
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    with pytest.raises(_CardActionTerminalFailure) as exc_info:
        await orch._handle_component_action(
            ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
        )
    assert exc_info.value.terminal_code == "permission_denied"
    assert any(r["target"] == "chat" for r in orch._rendered_ui)


@pytest.mark.asyncio
async def test_component_action_tool_error_raises() -> None:
    """A tool result carrying an error must raise _CardActionTerminalFailure with tool_error code."""

    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value={
        "component_data": {
            "_source_agent": "general-1",
            "_source_tool": "read_spreadsheet",
            "_source_params": {},
        }
    })

    auth_result = SimpleNamespace(args={"q": "x"})
    error_result = SimpleNamespace(
        ui_components=None,
        error={"message": "Agent returned an error."},
    )

    @asynccontextmanager
    async def _fake_lock(chat_id):
        yield

    orch._workspace_mutation_lock = _fake_lock
    orch._authorize_and_prepare = AsyncMock(return_value=auth_result)
    orch._execute_with_retry_audited = AsyncMock(return_value=error_result)

    with pytest.raises(_CardActionTerminalFailure) as exc_info:
        await orch._handle_component_action(
            ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
        )
    assert exc_info.value.terminal_code == "tool_error"
    assert any(r["target"] == "chat" for r in orch._rendered_ui)


@pytest.mark.asyncio
async def test_component_action_timeline_readonly_raises() -> None:
    """Timeline-readonly denial must raise _CardActionTerminalFailure."""
    orch = _build_orch()
    ws = _ws()
    orch._ws_timeline_mode[id(ws)] = True
    with pytest.raises(_CardActionTerminalFailure):
        await orch._handle_component_action(
            ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
        )


@pytest.mark.asyncio
async def test_component_action_success_does_not_raise() -> None:
    """A successful component action must NOT raise _CardActionTerminalFailure."""

    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value={
        "component_data": {
            "_source_agent": "general-1",
            "_source_tool": "read_spreadsheet",
            "_source_params": {},
        }
    })

    auth_result = SimpleNamespace(args={})
    success_result = SimpleNamespace(
        ui_components=[{"type": "card", "id": "comp1"}],
        error=None,
    )

    @asynccontextmanager
    async def _fake_lock(chat_id):
        yield

    orch._workspace_mutation_lock = _fake_lock
    orch._authorize_and_prepare = AsyncMock(return_value=auth_result)
    orch._execute_with_retry_audited = AsyncMock(return_value=success_result)
    orch._send_or_replace_components = AsyncMock(return_value=[])
    orch.workspace.asnapshot = AsyncMock()

    await orch._handle_component_action(
        ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
    )
    sent_statuses = [s for s in orch._sent_raw if "chat_status" in s]
    assert sent_statuses


# ---------------------------------------------------------------------------
# _refine_restore_gate: refusal cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refine_restore_gate_feature_disabled_raises() -> None:
    """Disabled feature_flag must raise _CardActionTerminalFailure."""
    orch = _build_orch()
    ws = _ws()
    with pytest.raises(_CardActionTerminalFailure), patch("shared.feature_flags.flags.is_enabled", return_value=False):
        await orch._refine_restore_gate(
            ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
        )
    assert any(r["target"] == "chat" for r in orch._rendered_ui)


@pytest.mark.asyncio
async def test_refine_restore_gate_missing_context_raises() -> None:
    """Missing component context in refine/restore gate must raise."""
    orch = _build_orch()
    ws = _ws()
    with pytest.raises(_CardActionTerminalFailure), patch("shared.feature_flags.flags.is_enabled", return_value=True):
        await orch._refine_restore_gate(ws, "alice", {})


@pytest.mark.asyncio
async def test_refine_restore_gate_component_not_found_raises() -> None:
    """Non-existent component in refine/restore gate must raise."""
    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value=None)
    with patch("shared.feature_flags.flags.is_enabled", return_value=True), pytest.raises(_CardActionTerminalFailure) as exc_info:
        await orch._refine_restore_gate(
            ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
        )
    assert exc_info.value.terminal_code == "component_not_found"
    assert any(r["target"] == "chat" for r in orch._rendered_ui)


@pytest.mark.asyncio
async def test_refine_restore_gate_permission_denied_raises() -> None:
    """Permission denial in refine/restore gate must raise with permission_denied code."""
    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value={
        "component_data": {
            "_source_agent": "general-1",
            "_source_tool": "read_spreadsheet",
        }
    })
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    with patch("shared.feature_flags.flags.is_enabled", return_value=True), pytest.raises(_CardActionTerminalFailure) as exc_info:
        await orch._refine_restore_gate(
            ws, "alice", {"chat_id": "c1", "component_id": "comp1"}
        )
    assert exc_info.value.terminal_code == "permission_denied"


# ---------------------------------------------------------------------------
# _handle_component_refine: refusal / failure cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_component_refine_provider_not_configured_raises() -> None:
    """Unconfigured AI provider in component_refine must raise with provider_not_configured code."""
    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value={
        "component_data": {
            "_source_agent": "general-1",
            "_source_tool": "read_spreadsheet",
        }
    })
    orch.llm_configured_for = AsyncMock(return_value=False)
    orch._llm_audit_principals = MagicMock(return_value=("alice", "alice"))
    orch._record_llm_unconfigured = AsyncMock()
    orch.audit_recorder = MagicMock()
    with pytest.raises(_CardActionTerminalFailure) as exc_info, patch("shared.feature_flags.flags.is_enabled", return_value=True):
        await orch._handle_component_refine(
            ws, "alice",
            {"chat_id": "c1", "component_id": "comp1", "instruction": "make it red"},
        )
    assert exc_info.value.terminal_code == "provider_not_configured"
    assert any(r["target"] == "chat" for r in orch._rendered_ui)
    assert any("chat_status" in s for s in orch._sent_raw)


@pytest.mark.asyncio
async def test_component_refine_empty_instruction_raises() -> None:
    """Empty instruction in component_refine must raise _CardActionTerminalFailure."""
    orch = _build_orch()
    ws = _ws()
    orch.workspace.aget_by_component_id = AsyncMock(return_value={
        "component_data": {
            "_source_agent": "general-1",
            "_source_tool": "read_spreadsheet",
        }
    })
    with pytest.raises(_CardActionTerminalFailure) as exc_info, patch("shared.feature_flags.flags.is_enabled", return_value=True):
        await orch._handle_component_refine(
            ws, "alice",
            {"chat_id": "c1", "component_id": "comp1", "instruction": ""},
        )
    assert exc_info.value.terminal_code == "empty_instruction"
    assert any("chat_status" in s for s in orch._sent_raw)


# ---------------------------------------------------------------------------
# No message leakage: alert must target "chat" only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_component_action_alert_targets_chat_surface() -> None:
    """All refusal alerts must target the chat surface (not canvas or transient)."""
    orch = _build_orch()
    ws = _ws()
    with pytest.raises(_CardActionTerminalFailure):
        await orch._handle_component_action(ws, "alice", {})
    for render in orch._rendered_ui:
        assert render["target"] == "chat", (
            f"Alert targeted {render['target']!r} instead of 'chat'"
        )
