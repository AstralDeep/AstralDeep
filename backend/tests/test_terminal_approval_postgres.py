"""Exercises terminal proposal persistence and approved execution against real Plane repositories.
Only the remote SSH connection is substituted while approval, Work leases and owner credentials remain real.
"""

import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.terminal_shell import mcp_tools
from orchestrator import remote_confirmation as rc, terminal_approvals as approvals
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.terminal_security import assert_terminal_delivery
from shared.protocol import MCPResponse
from tests.test_terminal_ssh_postgres import _execution, runtime as runtime


def _proposal(env, tool_name, arguments):
    request = approvals.PendingTerminalRequest(env.orch, "owner", tool_name, lambda: assert_terminal_delivery(
        env.orch, env.orch.ui_sessions[env.socket], "owner", tool_name))
    with approvals.request_scope(request):
        result = rc.evaluate(env.orch, None, "terminal-shell-1", tool_name, arguments, None, "owner")
    assert rc.is_approval_card_result(result[1])
    with env.plane.transaction() as transaction:
        row = env.plane.repositories.remote_operation_proposals.get(
            transaction, owner_id="owner", proposal_id=request.proposal_id)
        assert env.plane.repositories.remote_operation_proposals.get(
            transaction, owner_id="foreign", proposal_id=request.proposal_id) is None
    assert row.machine_id == env.machine
    assert row.conversation_id == request.conversation_id == "terminal-api:" + request.request_id
    assert 0 < len(row.summary) <= 2048
    assert "terminal.test:2222 as runner" in row.summary and env.fingerprint in row.summary
    assert "synthetic-initial" not in row.summary
    return request, result[1][0], row


@pytest.mark.asyncio
async def test_real_proposal_binds_selected_machine_and_consumes_once_before_pty_and_input(runtime, monkeypatch):
    env = runtime
    env.orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=env.plane, repositories=env.plane.repositories))
    env.orch.send_ui_render = AsyncMock(return_value=True)
    monkeypatch.setattr(mcp_tools, "_ORCH", env.orch)
    dispatched = []

    async def execute(socket, call, mapping, chat_id, *, user_id):
        assert socket is env.socket and user_id == "owner" and chat_id is None
        tool_name = call.function.name
        args = json.loads(call.function.arguments)
        assert rc.evaluate(env.orch, socket, "terminal-shell-1", tool_name, args, chat_id, user_id) is None
        _, claim = _execution(env, "terminal-approved-" + tool_name)
        token = _CONNECTION_OPERATION_CONTEXT.set({"execution_fence": claim.fence})
        try:
            authority = env.authority(tool_name)
            result = await asyncio.to_thread(getattr(mcp_tools, tool_name), **args,
                                             user_id=user_id, _terminal_authority=authority)
            dispatched.append(tool_name)
            return MCPResponse(result=result["_data"], ui_components=result["_ui_components"])
        finally:
            _CONNECTION_OPERATION_CONTEXT.reset(token)

    env.orch.execute_single_tool = execute
    request, card, row = _proposal(env, "start_terminal", {"command": "printf 'approved'"})
    assert env.channels == []
    assert row.arguments["runtime_binding"] == env.manager.runtime_binding("owner")
    assert env.manager.runtime_machine_id("owner", runtime_binding=row.arguments["runtime_binding"]) == env.machine
    payload = card["content"][-2]["payload"]
    await rc.handle_decision(env.orch, env.socket, "owner", payload)
    assert request.public("owner")["status"] == "succeeded"
    assert env.channels[0].effects[-1] == ("command", "printf 'approved'")
    with env.plane.transaction() as transaction:
        consumed = env.plane.repositories.remote_operation_proposals.get(
            transaction, owner_id="owner", proposal_id=request.proposal_id)
    assert consumed.status == "consumed" and consumed.consumed_at is not None
    await rc.handle_decision(env.orch, env.socket, "owner", payload)
    assert dispatched == ["start_terminal"]
    session_id = request.public("owner")["session"]["session_id"]
    raw = b"x" * 16384
    encoded = base64.b64encode(raw).decode("ascii")
    write, card, row = _proposal(env, "terminal_write", {"session_id": session_id, "data_b64": encoded})
    assert row.machine_id != session_id
    assert row.arguments["data_b64"] == encoded
    assert row.args_fingerprint == rc._fingerprint(dict(row.arguments))
    assert hashlib.sha256(raw).hexdigest() in card["content"][0]["content"]
    assert "16384" in card["content"][0]["content"]
    assert len(card["content"][0]["content"]) <= 2048
    await rc.handle_decision(env.orch, env.socket, "owner", card["content"][-2]["payload"])
    assert write.public("owner")["status"] == "succeeded"
    assert env.channels[0].effects[-1] == ("send", raw)
    await rc.handle_decision(env.orch, env.socket, "owner", card["content"][-2]["payload"])
    assert dispatched == ["start_terminal", "terminal_write"]
