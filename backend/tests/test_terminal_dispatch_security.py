"""Checks terminal confirmation, taint and authenticated dispatch boundaries.
Uses the ordinary orchestrator gate without launching a shell.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator import remote_confirmation, taint
from orchestrator.terminal_security import TerminalAuthority
from orchestrator.tool_permissions import _TURN_PERMISSION_MEMO
from orchestrator.orchestrator import GateRefusal
from shared.feature_flags import flags
from shared.protocol import RegisterAgent
from tests.test_cresco_dispatch_096 import orch as base_orch  # noqa: F401
from tests.test_terminal_ssh_runtime import isolated as isolated


@pytest.fixture
def orch(base_orch):  # noqa: F811
    from agents.cresco.mcp_tools import TOOL_REGISTRY as CRESCO_TOOLS
    from agents.terminal_shell.mcp_tools import TOOL_REGISTRY as TERMINAL_TOOLS
    from orchestrator.tool_permissions import ToolPermissionManager

    permissions = base_orch.tool_permissions
    permissions.get_tool_scope_map = ToolPermissionManager.get_tool_scope_map.__get__(permissions)
    permissions.register_tool_scopes = ToolPermissionManager.register_tool_scopes.__get__(permissions)
    for agent, tools in (("terminal-shell-1", TERMINAL_TOOLS), ("cresco-1", CRESCO_TOOLS)):
        base_orch.tool_permissions.register_tool_scopes(agent, {name: tool["scope"] for name, tool in tools.items()})
    return base_orch


@pytest.mark.parametrize("tool", ["start_terminal", "terminal_write"])
def test_terminal_execution_requires_confirmation_and_is_a_taint_sink(tool):
    assert remote_confirmation.classification_for(tool, "terminal-shell-1") == "always"
    assert remote_confirmation.is_destructive_unattended(tool, {}, "terminal-shell-1")
    assert taint.is_sink("terminal-shell-1", tool)


def test_terminal_output_is_untrusted_and_reading_is_not_a_sink():
    assert taint.classify_source("terminal-shell-1", "terminal_read") == taint.UNTRUSTED
    assert not taint.is_sink("terminal-shell-1", "terminal_read")


@pytest.mark.parametrize("channel", ["rest", "mcp", "a2a", "background", "scheduled", "chained"])
def test_noninteractive_claims_cannot_approve_terminal_execution(channel):
    socket = object()
    orchestrator = SimpleNamespace(ui_sessions={socket: {
        "sub": "owner", "exp": time.time() + 600, "_invocation_channel": channel,
    }})
    result = remote_confirmation.evaluate(orchestrator, socket, "terminal-shell-1",
                                          "terminal_write", {"data": "echo value"}, None, "owner")
    assert result is not None and result[0].startswith("unattended_refused")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,expected", [(False, "terminal_disabled"), (True, "terminal_isolation_unavailable")])
async def test_real_dispatch_refuses_execution_before_approval_or_transport(orch, monkeypatch, enabled, expected):
    from agents.terminal_shell.mcp_tools import TOOL_REGISTRY as _TERMINAL_TOOLS

    orch.tool_permissions.register_tool_scopes(
        "terminal-shell-1", {name: info["scope"] for name, info in _TERMINAL_TOOLS.items()})
    monkeypatch.setitem(flags._flags, "terminal_shell", enabled)
    monkeypatch.setenv("FF_TERMINAL_SHELL", "true" if enabled else "false")
    socket = MagicMock()
    orch.ui_sessions[socket] = {"sub": "u1", "exp": time.time() + 600,
                                "realm_access": {"roles": ["user"]}}
    confirmation = MagicMock(side_effect=AssertionError("unavailable execution must not request approval"))
    monkeypatch.setattr(remote_confirmation, "evaluate", confirmation)
    call = SimpleNamespace(function=SimpleNamespace(name="start_terminal", arguments=json.dumps({})))
    result = await orch.execute_single_tool(socket, call, {"start_terminal": "terminal-shell-1"},
                                            "chat-1", user_id="u1")
    assert result.error and result.error["code"] == expected
    confirmation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_gate_binds_fresh_host_authority_after_normal_permission_checks(orch, monkeypatch, tmp_path, revoked):
    from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent

    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    monkeypatch.setenv("FF_TERMINAL_SHELL", "true")
    monkeypatch.setenv("AGENT_API_KEY", "test-terminal-host")
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "personal")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(TerminalShellAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    agent = TerminalShellAgent(orchestrator=orch)
    await orch.register_agent(None, RegisterAgent(agent_card=agent.card, api_key="test-terminal-host"))
    assert orch.agent_cards[agent.agent_id] is agent.card
    orch.local_agents[agent.agent_id] = agent
    socket = object()
    orch.ui_sessions[socket] = {"sub": "u1", "exp": time.time() + 600,
                                "realm_access": {"roles": ["user"]}}
    fresh_checks = []

    def permission(*args):
        if _TURN_PERMISSION_MEMO.get() is None:
            return True
        fresh_checks.append(args)
        return not revoked

    orch.tool_permissions.is_tool_allowed = MagicMock(side_effect=permission)
    arguments = {"user_id": "foreign", "_terminal_authority": "forged"}
    prepared = await orch._authorize_and_prepare(socket, "terminal-shell-1", "list_terminals",
                                                 arguments, user_id="u1", auto_subscribe_stream=False)
    if revoked:
        assert isinstance(prepared, GateRefusal)
        assert prepared.response.error.get("code") == "terminal_permission_denied", prepared.response.error
    else:
        assert not isinstance(prepared, GateRefusal), prepared.response.error
        authority = prepared.args["_terminal_authority"]
        assert type(authority) is TerminalAuthority
        assert authority.owner_id == prepared.args["user_id"] == "u1"
        assert authority.matches(orch)
        assert prepared.stream_params["_terminal_authority"] == "forged"
    assert fresh_checks == [("u1", "terminal-shell-1", "list_terminals")]
    assert orch.tool_permissions.is_tool_allowed.call_count > len(fresh_checks)


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["profile", "target"])
async def test_terminal_change_during_permission_check_cannot_start_an_attended_action(orch, isolated, loss):
    from dataclasses import replace

    from agents.terminal_shell.mcp_tools import TOOL_REGISTRY as _TERMINAL_TOOLS
    from orchestrator.terminal_sessions import TerminalSessionManager

    orch.tool_permissions.register_tool_scopes(
        "terminal-shell-1", {name: info["scope"] for name, info in _TERMINAL_TOOLS.items()})
    socket = object()
    orch.ui_sessions[socket] = {"sub": "owner", "exp": time.time() + 600,
                                "realm_access": {"roles": ["user"]}}
    orch.terminal_sessions = TerminalSessionManager(orchestrator=orch)
    orch.security_flags["terminal-shell-1"] = {"start_terminal": {"blocked": False}}

    def permission(*args):
        if loss == "profile":
            isolated.profile.unlink()
        else:
            isolated.state.target = replace(isolated.target, address="changed.test")
        return True

    orch.tool_permissions.is_tool_allowed = permission
    result = await orch._run_gate_stack(socket, "terminal-shell-1", "start_terminal", {}, user_id="owner")
    assert isinstance(result, GateRefusal)
    message = "This remote operation requires attended authorization, which this node does not provide."
    assert result.response.error == {"message": message, "retryable": False}
    assert result.render_components[0]["message"] == message
    assert isolated.state.connects == [] and orch.terminal_sessions._sessions == {}


@pytest.mark.asyncio
async def test_terminal_confirmation_catch_does_not_reclassify_another_agent_failure(orch, monkeypatch):
    from orchestrator.terminal_security import TerminalError

    def failure(*args):
        raise TerminalError("terminal_profile_invalid", 503)

    monkeypatch.setattr(remote_confirmation, "evaluate", failure)
    with pytest.raises(TerminalError, match="terminal_profile_invalid"):
        await orch._run_gate_stack(object(), "cresco-1", "cresco_list_regions", {}, user_id="u1")
