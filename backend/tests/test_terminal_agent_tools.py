"""Tests terminal tool authority, retained-session rendering and explicit execution refusal.
No fixture starts a production shell or represents an unqualified executor as available.
"""

import base64
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agents.terminal_shell import mcp_tools
from agents.terminal_shell.mcp_server import MCPServer, bind_owner_context
from agents.terminal_shell.mcp_tools import TOOL_REGISTRY
from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent
from orchestrator import terminal_security as security
from orchestrator.terminal_security import TerminalError, bind_terminal_authority
from shared.feature_flags import flags
from shared.protocol import MCPRequest

OWNER = "owner"
SESSION = "retained"


@pytest.fixture
def retained(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    socket = object()
    claims = {"sub": OWNER, "exp": time.time() + 3600, "realm_access": {"roles": ["user"]}}
    info = {"session_id": SESSION, "cols": 80, "rows": 24, "idle_seconds": 0, "ring_start": 0, "ring_end": 4}
    payload = {"session_id": SESSION, "offset": 0, "next_offset": 4, "data_b64": base64.b64encode(b"\x00\xff\x1bA").decode(),
               "bytes": 4, "truncated": False, "eof": False, "session": info}
    manager = SimpleNamespace(
        list_sessions=Mock(return_value=[info]), read=Mock(return_value=payload),
        resize=Mock(return_value=info), interrupt=Mock(return_value=info),
        close=Mock(return_value={"session_id": SESSION, "closed": True, "reason": "owner_close"}),
        create=Mock(side_effect=AssertionError("no execution")), write=Mock(side_effect=AssertionError("no input")),
    )
    orch = SimpleNamespace(terminal_sessions=manager, ui_sessions={socket: claims}, security_flags={},
                           tool_permissions=SimpleNamespace(is_tool_allowed=Mock(return_value=True)))
    mcp_tools.register_deps(orch)

    def invoke(name, **arguments):
        authority = bind_terminal_authority(orch, socket, OWNER, name)
        return TOOL_REGISTRY[name]["function"](user_id=OWNER, _terminal_authority=authority, **arguments)

    yield SimpleNamespace(orch=orch, manager=manager, socket=socket, claims=claims, invoke=invoke, payload=payload)
    mcp_tools.register_deps(None)


class TestAgentWiring:
    def test_flag_gates_discovery_and_reserves_the_agent_id(self, monkeypatch):
        from orchestrator import local_agents
        from shared.feature_flags import flags
        monkeypatch.setattr(flags, "is_enabled", lambda name: False)
        assert "terminal_shell" not in local_agents.discover_built_in_agent_dirs()
        assert "terminal_shell" in local_agents._TERMINAL_SHELL_AGENT_DIRS
        assert "terminal-shell-1" in local_agents.FIRST_PARTY_PUBLIC_AGENT_IDS

    def _registration(self, monkeypatch):
        from orchestrator import local_agents
        from shared import attachment_materializer, attachment_resolver
        attempted: list = []
        registered: list = []

        def _bind(runtime, repositories, blobs):
            return True

        def _bind_materializer(service):
            return True

        async def _register(ws, msg):
            registered.append(msg.agent_card.agent_id)

        monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs",
                            lambda *a, **k: [])
        monkeypatch.setattr(local_agents, "_load_agent_class",
                            lambda d: attempted.append(d) or TerminalShellAgent)
        monkeypatch.setattr(attachment_resolver, "register_plane_runtime", _bind)
        monkeypatch.setattr(attachment_materializer,
                            "register_materialization_service", _bind_materializer)
        plane = SimpleNamespace(
            runtime=object(), repositories=object(), blobs=object(),
            attachment_materializer=object())
        orch = SimpleNamespace(
            local_agents={}, register_agent=_register,
            runtime_composition=SimpleNamespace(plane=plane))
        return orch, attempted, registered

    async def _assert_registered(self, monkeypatch):
        monkeypatch.setenv("AGENT_KEY_PATH", str(Path(self._tmp) / "agent.pem"))
        orch, attempted, registered = self._registration(monkeypatch)
        from orchestrator import local_agents
        assert await local_agents.register_built_ins(orch) == ["terminal-shell-1"]
        assert attempted == ["terminal_shell"] and registered == ["terminal-shell-1"]
        agent = orch.local_agents["terminal-shell-1"]
        assert {s.id for s in agent.card.skills} == set(TOOL_REGISTRY)
        response = agent.mcp_server.process_request(MCPRequest(
            request_id="req_agent", method="tools/call",
            params={"name": "list_terminals", "arguments": {"user_id": OWNER}}))
        assert response.error and "terminal_authority_required" in response.error["message"]

    async def test_flag_on_registration_binds_the_orchestrator(self, monkeypatch, tmp_path):
        from shared.feature_flags import flags
        self._tmp = str(tmp_path)
        monkeypatch.setattr(flags, "is_enabled", lambda name: name == "terminal_shell")
        await self._assert_registered(monkeypatch)

    async def test_flag_off_registration_never_loads_the_agent(self, monkeypatch, tmp_path):
        from shared.feature_flags import flags
        self._tmp = str(tmp_path)
        monkeypatch.setattr(flags, "is_enabled", lambda name: False)
        orch, attempted, registered = self._registration(monkeypatch)
        from orchestrator import local_agents
        assert await local_agents.register_built_ins(orch) == []
        assert attempted == [] and registered == [] and orch.local_agents == {}

    async def test_flag_check_failure_leaves_the_agent_unregistered(self, monkeypatch, tmp_path):
        from shared.feature_flags import flags
        self._tmp = str(tmp_path)

        def _boom(name):
            if name == "terminal_shell":
                raise RuntimeError("flag store unavailable")
            return False

        monkeypatch.setattr(flags, "is_enabled", _boom)
        orch, attempted, registered = self._registration(monkeypatch)
        from orchestrator import local_agents
        assert await local_agents.register_built_ins(orch) == []
        assert attempted == [] and registered == [] and orch.local_agents == {}

    def test_agent_requires_the_orchestrator(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "agent.pem"))
        with pytest.raises(RuntimeError):
            TerminalShellAgent()


class TestRegistryContract:
    def test_registry_preserves_the_complete_surface_and_scopes(self):
        assert set(TOOL_REGISTRY) == {"list_terminals", "start_terminal", "terminal_read", "terminal_write",
                                     "terminal_resize", "terminal_interrupt", "close_terminal"}
        for name, entry in TOOL_REGISTRY.items():
            expected = "tools:system" if name in security.EXECUTION_TOOLS else (
                "tools:read" if name in {"list_terminals", "terminal_read"} else "tools:write")
            assert entry["scope"] == expected
            assert entry["destructive"] == ("always" if name in security.EXECUTION_TOOLS else "never")
            assert entry["retryable"] is False
            assert "user_id" not in entry["input_schema"]["properties"]
            assert "_terminal_authority" not in entry["input_schema"]["properties"]
            assert any(p.kind == inspect.Parameter.VAR_KEYWORD for p in inspect.signature(entry["function"]).parameters.values())

    def test_mcp_registry_and_unknown_methods(self):
        server = MCPServer()
        response = server.process_request(MCPRequest(request_id="r", method="tools/list"))
        assert {x["name"] for x in response.result["tools"]} == set(TOOL_REGISTRY)
        for method, params in [("bad", {}), ("tools/call", {"name": "bad"})]:
            assert server.process_request(MCPRequest(request_id="r", method=method, params=params)).error


def test_dispatch_context_overwrites_model_owner_but_not_authority():
    arguments = {"user_id": "attacker"}
    bind_owner_context(arguments, OWNER)
    assert arguments == {"user_id": OWNER}


@pytest.mark.parametrize("name", sorted(TOOL_REGISTRY))
@pytest.mark.parametrize("authority", [None, {}, "pretend-authority"])
def test_direct_or_json_forged_authority_cannot_operate_a_terminal(retained, name, authority):
    result = TOOL_REGISTRY[name]["function"](user_id=OWNER, _terminal_authority=authority)
    assert result["_data"]["code"] == "terminal_authority_required"
    assert all(not x.called for x in vars(retained.manager).values())


@pytest.mark.parametrize("name", sorted(security.EXECUTION_TOOLS))
def test_execution_tools_report_unavailable_without_touching_the_manager(retained, name):
    result = retained.invoke(name, command="printf synthetic", data="x")
    assert result["_data"]["code"] == "terminal_isolation_unavailable"
    assert result["_data"]["status_code"] == 503
    retained.manager.create.assert_not_called()
    retained.manager.write.assert_not_called()


def test_retained_read_preserves_raw_bytes_and_sanitizes_ui(retained):
    result = retained.invoke("terminal_read", session_id=SESSION)
    assert result["_data"]["data_b64"] == retained.payload["data_b64"]
    assert result["_data"]["session"] == retained.payload["session"]
    text = json.dumps(result["_ui_components"])
    assert "\\u001b" not in text and "\\u0000" not in text
    assert "TerminalAuthority" not in json.dumps(result)
    retained.manager.read.assert_called_once_with(OWNER, SESSION, offset=0, wait_seconds=1.0)


@pytest.mark.parametrize("change,expected", [
    ("owner", "terminal_owner_required"), ("expiry", "terminal_authority_expired"),
    ("permission", "terminal_permission_denied"), ("flag", "terminal_disabled"),
    ("hard_flag", "terminal_security_blocked"),
])
def test_authority_loss_during_read_discards_output(retained, monkeypatch, change, expected):
    def read(*args, **kwargs):
        if change == "owner":
            retained.orch.ui_sessions.pop(retained.socket)
        elif change == "expiry":
            retained.claims["exp"] = 0
        elif change == "permission":
            retained.orch.tool_permissions.is_tool_allowed.return_value = False
        elif change == "flag":
            monkeypatch.setitem(flags._flags, "terminal_shell", False)
        else:
            retained.orch.security_flags[security.AGENT_ID] = {"terminal_read": {"blocked": True}}
        return retained.payload
    retained.manager.read.side_effect = read
    result = retained.invoke("terminal_read", session_id=SESSION)
    assert result["_data"]["code"] == expected
    assert "data_b64" not in result["_data"]
    assert "text" not in result["_data"]


@pytest.mark.parametrize("claims", [None, {}, {"sub": "other"}, {"exp": True}, {"exp": float("nan")},
                                    {"exp": float("inf")}, {"exp": None}, {"nbf": float("nan")},
                                    {"nbf": time.time() + 10000}, {"act": {}}, {"machine_class": ""},
                                    {"realm_access": {"roles": []}}, {"realm_access": None}])
def test_malformed_expired_or_nonhuman_current_claims_are_refused(retained, claims):
    if claims is None:
        retained.orch.ui_sessions.pop(retained.socket)
    else:
        retained.claims.update(claims)
        if claims == {}:
            retained.claims.pop("sub")
    with pytest.raises(TerminalError):
        bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")


def test_security_block_cannot_be_overridden_by_an_allowed_or_safe_permission(retained):
    retained.orch.security_flags[security.AGENT_ID] = {"terminal_read": {"blocked": True}}
    with pytest.raises(TerminalError, match="terminal_security_blocked"):
        bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")
    retained.orch.tool_permissions.is_tool_allowed.assert_not_called()


def test_each_authority_validation_escapes_a_stale_turn_permission_memo(retained):
    from orchestrator.tool_permissions import ToolPermissionManager, turn_permission_memo
    permissions = ToolPermissionManager.__new__(ToolPermissionManager)
    permissions.get_tool_scope = lambda *args: "tools:read"
    permissions._resolve_tool_allowed = Mock(return_value=True)
    retained.orch.tool_permissions = permissions
    with turn_permission_memo():
        assert permissions.is_tool_allowed(OWNER, security.AGENT_ID, "terminal_read")
        authority = bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")
        permissions._resolve_tool_allowed.return_value = False
        with pytest.raises(TerminalError, match="terminal_permission_denied"):
            authority.assert_current("terminal_read")
        assert permissions.is_tool_allowed(OWNER, security.AGENT_ID, "terminal_read")


@pytest.mark.parametrize("failure", [False, RuntimeError("policy storage unavailable")])
def test_permission_unavailability_fails_closed(retained, failure):
    if failure is False:
        retained.orch.tool_permissions.is_tool_allowed.return_value = False
    else:
        retained.orch.tool_permissions.is_tool_allowed.side_effect = failure
    with pytest.raises(TerminalError, match="terminal_permission_denied"):
        bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")


def test_wrong_owner_tool_and_orchestrator_cannot_reuse_authority(retained):
    authority = bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")
    with pytest.raises(TerminalError, match="terminal_authority_invalid"):
        authority.assert_current("close_terminal")
    assert mcp_tools.terminal_read(user_id="other", session_id=SESSION, _terminal_authority=authority)["_data"]["code"] == "terminal_owner_required"
    mcp_tools.register_deps(SimpleNamespace())
    assert mcp_tools.terminal_read(user_id=OWNER, session_id=SESSION, _terminal_authority=authority)["_data"]["code"] == "terminal_authority_required"


@pytest.mark.parametrize("sessions", [[], [{"session_id": "s", "cols": 80, "rows": 24}]])
def test_retained_lists_and_cleanup_return_truthful_owner_results(retained, sessions):
    retained.manager.list_sessions.return_value = sessions
    assert retained.invoke("list_terminals")["_data"]["sessions"] == sessions
    for name, extra in [("terminal_resize", {"cols": 100, "rows": 40}), ("terminal_interrupt", {}), ("close_terminal", {})]:
        result = retained.invoke(name, session_id=SESSION, **extra)
        assert result["_ui_components"]
    retained.manager.close.assert_called_once_with(OWNER, SESSION)


@pytest.mark.parametrize("payload", [
    {"data_b64": "", "truncated": True, "eof": False},
    {"data_b64": "", "eof": True}, {"data_b64": "", "eof": False},
])
def test_empty_truncated_and_eof_output_are_rendered_without_fake_content(retained, payload):
    retained.manager.read.return_value = {**retained.payload, **payload}
    assert retained.invoke("terminal_read", session_id=SESSION)["_ui_components"]


@pytest.mark.parametrize("arguments,code", [
    ({}, "terminal_session_invalid"), ({"session_id": "x" * 65}, "terminal_session_invalid"),
    ({"session_id": SESSION, "offset": True}, "terminal_offset_invalid"),
    ({"session_id": SESSION, "offset": -1}, "terminal_offset_invalid"),
    ({"session_id": SESSION, "wait_seconds": float("nan")}, "terminal_wait_invalid"),
    ({"session_id": SESSION, "wait_seconds": 26}, "terminal_wait_invalid"),
])
def test_retained_read_input_denials_precede_manager_access(retained, arguments, code):
    result = retained.invoke("terminal_read", **arguments)
    assert result["_data"]["code"] == code
    retained.manager.read.assert_not_called()


def test_manager_refusal_reaches_mcp_as_typed_nonretryable_error(retained):
    retained.manager.read.side_effect = TerminalError("terminal_session_not_found", 404)
    authority = bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")
    result = MCPServer().process_request(MCPRequest(request_id="r", method="tools/call", params={
        "name": "terminal_read", "arguments": {"session_id": SESSION, "user_id": OWNER, "_terminal_authority": authority},
    }))
    assert result.error["data"] == {"terminal_code": "terminal_session_not_found", "http_status": 404}
    assert result.error["retryable"] is False


def test_missing_manager_and_unexpected_failure_do_not_return_terminal_content(retained):
    retained.orch.terminal_sessions = None
    assert retained.invoke("list_terminals")["_data"]["code"] == "terminal_unavailable"
    retained.orch.terminal_sessions = retained.manager
    retained.manager.list_sessions.side_effect = RuntimeError("synthetic retained data must not leak")
    authority = bind_terminal_authority(retained.orch, retained.socket, OWNER, "list_terminals")
    result = MCPServer().process_request(MCPRequest(request_id="r", method="tools/call", params={
        "name": "list_terminals", "arguments": {"user_id": OWNER, "_terminal_authority": authority},
    }))
    assert "synthetic retained data" not in json.dumps({"error": result.error, "result": result.result, "ui_components": result.ui_components})


def test_feature_lookup_failure_and_missing_current_state_fail_closed(retained, monkeypatch):
    monkeypatch.setattr(flags, "is_enabled", Mock(side_effect=RuntimeError("unavailable")))
    assert security.terminal_capability() == {"enabled": False, "available": False, "reason": "terminal_disabled"}
    with pytest.raises(TerminalError, match="terminal_disabled"):
        bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")
    monkeypatch.setattr(flags, "is_enabled", lambda _: True)
    retained.orch.ui_sessions = None
    with pytest.raises(TerminalError, match="terminal_owner_required"):
        bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")


@pytest.mark.parametrize("flags_value", [None, {security.AGENT_ID: None}, {security.AGENT_ID: {"terminal_read": None}},
                                        {security.AGENT_ID: {"terminal_read": {"blocked": None}}}])
def test_unavailable_hard_security_state_fails_closed(retained, flags_value):
    retained.orch.security_flags = flags_value
    with pytest.raises(TerminalError, match="terminal_security_blocked"):
        bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")


def test_plain_availability_validation_never_adds_a_host_authority_object(retained):
    security.assert_terminal_identity(retained.orch, retained.socket, OWNER)
    assert security.assert_terminal_execution_available("terminal_read") is None
    for tool in security.EXECUTION_TOOLS:
        with pytest.raises(TerminalError, match="terminal_isolation_unavailable"):
            security.assert_terminal_execution_available(tool)


@pytest.mark.parametrize("change,code", [
    ("owner", "terminal_owner_required"), ("expiry", "terminal_authority_expired"),
    ("hard_flag", "terminal_security_blocked"), ("feature", "terminal_disabled"),
])
def test_authority_lost_while_permission_storage_is_awaited_cannot_authorize_effects(retained, monkeypatch, change, code):
    def permission(*args):
        if change == "owner":
            retained.orch.ui_sessions.pop(retained.socket)
        elif change == "expiry":
            retained.claims["exp"] = 0
        elif change == "hard_flag":
            retained.orch.security_flags[security.AGENT_ID] = {"terminal_read": {"blocked": True}}
        else:
            monkeypatch.setitem(flags._flags, "terminal_shell", False)
        return True

    retained.orch.tool_permissions.is_tool_allowed.side_effect = permission
    with pytest.raises(TerminalError, match=code):
        bind_terminal_authority(retained.orch, retained.socket, OWNER, "terminal_read")
    retained.manager.read.assert_not_called()
