"""Tests for orchestrator/mcp_authz.py, mcp_server_endpoint.py and supervisor.py: the
realm-role entry gate, an honest refusal when the signing key is unset, and the
supervisor's intent check for destructive tools called over MCP.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import mcp_authz, supervisor


async def _authorize(required=("mcp:discover",)):
    return await mcp_authz.authorize_mcp_request(
        headers={"authorization": "Bearer opaque"},
        query_params={},
        cookies={},
        required_scopes=required,
    )


def test_realm_roles_helper_is_defensive():
    assert mcp_authz.realm_roles({}) == frozenset()
    assert mcp_authz.realm_roles({"realm_access": None}) == frozenset()
    assert mcp_authz.realm_roles({"realm_access": {"roles": ["user", 7]}}) == {"user"}
    assert mcp_authz.MCP_ENTRY_ROLES == {"user", "admin"}


@pytest.mark.parametrize(
    "tool", ["delete_records", "send-email", "drop_table", "purge.cache", "pay_invoice"]
)
def test_mcp_intent_text_aligns_destructive_tool_named_by_caller(tool):
    text = supervisor.mcp_intent_text(tool)
    assert text.startswith("call tool ")
    assert supervisor.intent_aligned(text, tool)
    assert not supervisor.intent_aligned("show me my dashboard", tool)
    assert not supervisor.intent_aligned("", tool)


def test_mcp_intent_text_does_not_fabricate_intent_for_listed_tools():
    assert not supervisor.intent_aligned(
        supervisor.mcp_intent_text("nuke_everything"),
        "nuke_everything",
        destructive_tools={"nuke_everything"},
    )


@pytest.fixture
def gate_orch(orchestrator_factory):
    os.environ["OPENAI_API_KEY"] = "test-key"
    o = orchestrator_factory()
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.register_tool_scopes("a1", {
        "delete_records": "tools:read", "send-email": "tools:read"})
    o.tool_permissions.register_tool_scopes("remote-compute-1", {
        "delete_path": "tools:write"})
    o.agents["remote-compute-1"] = object()
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    return o


def _tc(tool, args=None):
    return SimpleNamespace(function=SimpleNamespace(name=tool, arguments=json.dumps(args or {})))


def _err(resp):
    return ((resp.error or {}).get("message", "")) if resp is not None else ""


@pytest.mark.asyncio
async def test_supervisor_over_mcp_accepts_explicitly_named_destructive_tool(
    gate_orch, monkeypatch
):
    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "true")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    gate_orch._active_request = {}
    resp = await gate_orch.execute_mcp_tool(
        claims={"sub": "u1", "realm_access": {"roles": ["user"]}},
        user_id="u1",
        agent_id="a1",
        tool_name="delete_records",
        arguments={},
    )
    assert "didn't ask for" not in _err(resp)
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
async def test_supervisor_over_chat_still_refuses_unrequested_destructive(
    gate_orch, monkeypatch
):
    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "true")
    gate_orch._active_request = {"c1": "show me my dashboard"}
    ws = MagicMock()
    resp = await gate_orch.execute_single_tool(
        ws, _tc("delete_records"), {"delete_records": "a1"}, "c1", user_id="u1"
    )
    assert "didn't ask for" in _err(resp)


@pytest.mark.asyncio
async def test_mcp_destructive_remote_compute_refusal_survives_supervisor_change(
    gate_orch, monkeypatch
):
    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "true")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    from orchestrator import remote_confirmation

    monkeypatch.setattr(
        remote_confirmation, "is_destructive_unattended", lambda tool, args, agent: True
    )
    resp = await gate_orch.execute_mcp_tool(
        claims={"sub": "u1", "realm_access": {"roles": ["user"]}},
        user_id="u1",
        agent_id="remote-compute-1",
        tool_name="delete_path",
        arguments={"path": "/tmp/x"},
    )
    assert "unattended MCP channel" in _err(resp)
