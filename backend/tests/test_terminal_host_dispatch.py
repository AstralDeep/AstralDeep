"""Checks that terminal host authority survives local dispatch and never reaches transport or recording.
The actual bundled agent reads retained synthetic sessions without starting a process.
"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.terminal_shell import mcp_tools
from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent
from orchestrator import evidence_context
from orchestrator.orchestrator import Orchestrator
from orchestrator.terminal_security import bind_terminal_authority
from shared.feature_flags import flags
from shared.protocol import MCPRequest

AGENT = "terminal-shell-1"
TOOL = "list_terminals"


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    monkeypatch.setitem(flags._flags, "tool_streaming", False)
    monkeypatch.setattr(mcp_tools, "_ORCH", None)
    monkeypatch.setattr(TerminalShellAgent, "_init_crypto", lambda self: setattr(self, "_public_key_jwk", {}))
    orch = Orchestrator.__new__(Orchestrator)
    socket = object()
    orch.ui_sessions = {socket: {"sub": "owner", "exp": time.time() + 600, "realm_access": {"roles": ["user"]}}}
    orch.security_flags = {}
    orch.tool_permissions = SimpleNamespace(is_tool_allowed=Mock(return_value=True), get_tool_scope=Mock(return_value="tools:read"))
    orch.terminal_sessions = SimpleNamespace(list_sessions=Mock(return_value=[]))
    orch.pending_requests = {}
    orch._pending_request_agent = {}
    orch.pending_ui_sockets = {}
    orch._dispatch_context = {}
    orch._chat_recorders = {}
    orch.agent_urls = {}
    orch.agents = {}
    orch.a2a_clients = {}
    orch._copy_control = threading.Lock()
    agent = TerminalShellAgent(orchestrator=orch)
    orch.local_agents = {AGENT: agent}
    orch.agent_cards = {AGENT: agent.card}
    authority = bind_terminal_authority(orch, socket, "owner", TOOL)
    args = {"user_id": "owner", "_terminal_authority": authority}
    return SimpleNamespace(orch=orch, socket=socket, authority=authority, args=args, agent=agent)


@pytest.mark.asyncio
async def test_actual_local_agent_uses_exact_host_authority_without_copy_or_wire(host, monkeypatch):
    original = host.agent.handle_mcp_request
    observed = []

    async def inspect_request(socket, request):
        observed.append(getattr(request, "_terminal_authority", None))
        assert "_terminal_authority" not in request.params["arguments"]
        json.dumps(request.params)
        await original(socket, request)

    monkeypatch.setattr(host.agent, "handle_mcp_request", inspect_request)
    monkeypatch.setattr(MCPRequest, "to_json", Mock(side_effect=AssertionError("request must stay local")))
    result = await host.orch._execute_in_process(AGENT, TOOL, host.args, ui_websocket=host.socket)
    assert result.error is None
    assert result.result == {"sessions": []}
    assert observed == [host.authority] and observed[0] is host.authority
    host.orch.terminal_sessions.list_sessions.assert_called_once_with("owner")
    assert not host.orch.pending_requests and not host.orch._dispatch_context


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["missing", "foreign", "kill_switch"])
async def test_unavailable_local_placement_refuses_before_recording_or_any_transport(host, monkeypatch, loss):
    if loss == "missing":
        host.orch.local_agents.clear()
    elif loss == "foreign":
        host.orch.local_agents[AGENT] = SimpleNamespace(handle_mcp_request=AsyncMock())
    else:
        monkeypatch.setitem(flags._flags, "inprocess_agents", False)
    recorder = SimpleNamespace(start=AsyncMock())
    host.orch._chat_recorders[id(host.socket)] = recorder
    host.orch.agents[AGENT] = SimpleNamespace(send=AsyncMock())
    host.orch._dispatch_tool_call = AsyncMock(side_effect=AssertionError("placement must be checked first"))
    result = await host.orch.execute_tool_and_wait(AGENT, TOOL, host.args, ui_websocket=host.socket)
    assert result.error["code"] == "terminal_transport_unavailable"
    assert result.error["retryable"] is False
    recorder.start.assert_not_awaited()
    host.orch._dispatch_tool_call.assert_not_awaited()
    host.orch.terminal_sessions.list_sessions.assert_not_called()


@pytest.mark.asyncio
async def test_host_authority_is_excluded_from_recorder_and_governed_arguments(host, monkeypatch):
    recorded = []
    governed = []

    async def start(kind, name, args):
        recorded.append(json.loads(json.dumps(args)))
        return "step"

    recorder = SimpleNamespace(start=start, is_terminal=Mock(return_value=False), complete=AsyncMock(), error=AsyncMock())
    host.orch._chat_recorders[id(host.socket)] = recorder

    async def govern(socket, agent, tool, args, **kwargs):
        governed.append(json.loads(json.dumps(args)))
        return await kwargs["invoke"]({})

    monkeypatch.setattr(host.orch, "_execute_governed_attempt", govern)
    result = await host.orch.execute_tool_and_wait(AGENT, TOOL, host.args, ui_websocket=host.socket)
    assert result.error is None
    assert recorded == governed == [{"user_id": "owner"}]
    recorder.complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["websocket", "a2a"])
async def test_terminal_direct_transport_entry_refuses_without_serialization(host, monkeypatch, transport):
    monkeypatch.setattr(MCPRequest, "to_json", Mock(side_effect=AssertionError("no terminal request wire")))
    host.orch.agents[AGENT] = SimpleNamespace(send=AsyncMock())
    host.orch.a2a_clients[AGENT] = "https://example.invalid/terminal"
    method = getattr(host.orch, "_execute_via_" + transport)
    result = await method(AGENT, TOOL, host.args, timeout=0.01)
    assert result.error["code"] == "terminal_transport_unavailable"
    assert result.error["retryable"] is False
    host.orch.agents[AGENT].send.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_stream_refuses_before_preparation_or_wire(host):
    host.orch._authorize_and_prepare = AsyncMock(side_effect=AssertionError("terminal has no stream contract"))
    with pytest.raises(PermissionError, match="terminal_transport_unavailable"):
        await host.orch._dispatch_stream_request(AGENT, TOOL, host.args, "stream", "owner", host.socket, "chat")
    host.orch._authorize_and_prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_placement_loss_during_governance_never_falls_back_or_records_authority(host, monkeypatch):
    async def govern(socket, agent, tool, args, **kwargs):
        assert "_terminal_authority" not in args
        host.orch.local_agents.clear()
        host.orch.agents[AGENT] = SimpleNamespace(send=AsyncMock())
        return await kwargs["invoke"]({})

    monkeypatch.setattr(host.orch, "_execute_governed_attempt", govern)
    result = await host.orch._dispatch_tool_call(AGENT, TOOL, host.args, 1.0, host.socket)
    assert result.error["code"] == "terminal_transport_unavailable"
    host.orch.agents[AGENT].send.assert_not_awaited()
    host.orch.terminal_sessions.list_sessions.assert_not_called()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["single", "audited"])
async def test_completed_terminal_dispatch_keeps_host_authority_out_of_hooks_and_evidence(host, monkeypatch, entry):
    monkeypatch.setitem(flags._flags, "hook_system", True)
    monkeypatch.setitem(flags._flags, "taint_tracking", False)
    host.orch.hooks = SimpleNamespace(emit=AsyncMock())
    captured = []

    async def pack(result, **kwargs):
        captured.append(json.loads(json.dumps(kwargs["args"])))
        return result

    monkeypatch.setattr(evidence_context, "get_context", lambda owner: SimpleNamespace(pack_result=pack))
    if entry == "single":
        prepared = SimpleNamespace(args=host.args, stream_params={}, cap_job_id=None, hop_correlation_id=None)
        host.orch._authorize_and_prepare = AsyncMock(return_value=prepared)
        call = SimpleNamespace(function=SimpleNamespace(name=TOOL, arguments='{"user_id":"owner"}'))
        result = await host.orch.execute_single_tool(host.socket, call, {TOOL: AGENT}, user_id="owner")
    else:
        result = await host.orch._execute_with_retry_audited(host.socket, AGENT, TOOL, host.args, user_id="owner")
    assert result.error is None
    assert captured == [{"user_id": "owner"}]
    hook = host.orch.hooks.emit.await_args.args[0]
    assert json.loads(json.dumps(hook.tool_args)) == {"user_id": "owner"}
    host.orch.terminal_sessions.list_sessions.assert_called_once_with("owner")
