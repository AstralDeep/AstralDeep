"""Tests for the supervisor and HITL gates wired into Orchestrator.execute_single_tool:
a blocked call returns the gate's alert, a passed call falls through to dispatch, and
both are no-ops with their flags off.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def orch(orchestrator_factory):
    os.environ["OPENAI_API_KEY"] = "test-key"
    o = orchestrator_factory()
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.register_tool_scopes("a1", {
        "delete_records": "tools:read", "search_web": "tools:read",
        "send_email": "tools:read", "delete_account": "tools:read",
        "get_weather": "tools:read"})
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    return o


def _tc(tool, args=None):
    return SimpleNamespace(
        function=SimpleNamespace(name=tool, arguments=json.dumps(args or {}))
    )


async def _dispatch(orch, tool, *, request="", flags=None, args=None,
                    user="u1", agent="a1", chat="c1"):
    for k, v in (flags or {}).items():
        os.environ[k] = "true" if v else "false"
    orch._active_request = {chat: request}
    ws = MagicMock()
    try:
        return await orch.execute_single_tool(
            ws, _tc(tool, args), {tool: agent}, chat, user_id=user)
    finally:
        for k in (flags or {}):
            os.environ.pop(k, None)


def _err(resp):
    return ((resp.error or {}).get("message", "")) if resp is not None else ""


@pytest.mark.asyncio
async def test_supervisor_blocks_unrequested_destructive(orch):
    resp = await _dispatch(orch, "delete_records", request="show me my dashboard",
                           flags={"FF_RUNTIME_SUPERVISOR": True})
    assert "didn't ask for" in _err(resp)


@pytest.mark.asyncio
async def test_supervisor_allows_when_intent_present(orch):
    resp = await _dispatch(orch, "delete_records",
                           request="please delete my old records",
                           flags={"FF_RUNTIME_SUPERVISOR": True})
    assert "didn't ask for" not in _err(resp)
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
async def test_supervisor_allows_non_destructive(orch):
    resp = await _dispatch(orch, "search_web", request="anything at all",
                           flags={"FF_RUNTIME_SUPERVISOR": True})
    assert "didn't ask for" not in _err(resp)
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
async def test_supervisor_off_is_noop(orch):
    resp = await _dispatch(orch, "delete_records", request="show me my dashboard",
                           flags={"FF_RUNTIME_SUPERVISOR": False})
    assert "didn't ask for" not in _err(resp)
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "request_text"), [
    ("send_email", "email bob"),
    ("delete_account", "delete it"),
])
async def test_hitl_refuses_egress_and_irreversible_actions_before_dispatch(orch, tool, request_text):
    orch._execute_with_retry = AsyncMock()
    resp = await _dispatch(orch, tool, request=request_text, flags={"FF_HITL_HIGHRISK": True})
    assert resp.error["code"] == "confirmation_required"
    assert resp.error["effect_state"] == "not_started"
    assert resp.error["retryable"] is False
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_hitl_allows_benign(orch):
    resp = await _dispatch(orch, "get_weather", request="weather?",
                           flags={"FF_HITL_HIGHRISK": True})
    assert "No agent available" in _err(resp)


@pytest.mark.asyncio
async def test_hitl_off_is_noop(orch):
    resp = await _dispatch(orch, "send_email", request="email bob",
                           flags={"FF_HITL_HIGHRISK": False})
    assert "No agent available" in _err(resp)
