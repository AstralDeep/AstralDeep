"""Real integration tests for the supervisor (C-S5) and HITL (C-S11) gates
wired into Orchestrator.execute_single_tool.

These flip the feature flags ON and drive the REAL dispatch path. A blocked
call returns the gate's own alert; a call that passes the gate falls through to
the "No agent available" sentinel (the tool's agent isn't registered), which
proves it got past the gate. Flags OFF ⇒ the gate never fires.
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
    # Let everything past the permission gate so our gates are reachable.
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    # Keep the post-gate (passed) path deterministic.
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


# --------------------------------------------------------------------------- #
# Supervisor (C-S5): destructive tool the user did not ask for is held.
# --------------------------------------------------------------------------- #

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
    # Intent aligned → passes the gate → falls through to the no-agent sentinel.
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


# --------------------------------------------------------------------------- #
# HITL (C-S11): a risky (egress / irreversible) call is held for confirmation.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_hitl_blocks_egress(orch):
    resp = await _dispatch(orch, "send_email", request="email bob",
                           flags={"FF_HITL_HIGHRISK": True})
    msg = _err(resp)
    assert "confirm" in msg.lower()


@pytest.mark.asyncio
async def test_hitl_blocks_irreversible(orch):
    resp = await _dispatch(orch, "delete_account", request="delete it",
                           flags={"FF_HITL_HIGHRISK": True})
    assert "confirm" in _err(resp).lower()


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
