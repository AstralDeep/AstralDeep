"""Exercises terminal REST and manager security boundaries without starting child processes.
Authenticated route controls cover feature denial, governed dispatch and authority loss before delivery.
"""

import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agents.terminal_shell.mcp_tools import TOOL_REGISTRY
from orchestrator import terminal_api, terminal_sessions
from orchestrator.auth import get_web_or_bearer_user_payload
from orchestrator.tool_security import ThreatCategory, ToolSecurityAnalyzer
from shared.feature_flags import flags
from shared.protocol import MCPResponse


OWNER = "terminal-owner"
SESSION = "retained-session"
PAYLOAD = {
    "session_id": SESSION,
    "offset": 0,
    "next_offset": 4,
    "data_b64": base64.b64encode(b"\x00\xff\x1bA").decode(),
    "bytes": 4,
    "truncated": False,
    "eof": False,
    "session": {"session_id": SESSION, "cols": 80, "rows": 24},
}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)


def _claims(**extra):
    return {
        "sub": OWNER,
        "exp": int(time.time()) + 3600,
        "realm_access": {"roles": ["user"]},
        **extra,
    }


def _client(monkeypatch, *, claims=None, resolver=None, dispatch=None):
    calls = []

    class RetainedManager:
        def __getattr__(self, name):
            def invoke(*args, **kwargs):
                calls.append((name, args, kwargs))
                if name == "list_sessions":
                    return [{"session_id": SESSION}]
                if name == "read":
                    return dict(PAYLOAD)
                return {"session_id": SESSION, "closed": True}
            return invoke

    orch = SimpleNamespace(
        terminal_sessions=RetainedManager(),
        execute_authorized_tool=dispatch or AsyncMock(return_value=MCPResponse(result=dict(PAYLOAD))),
        tool_permissions=SimpleNamespace(is_tool_allowed=Mock(return_value=True)),
        security_flags={},
    )
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(terminal_api.terminal_router)
    if resolver is not None:
        app.dependency_overrides[get_web_or_bearer_user_payload] = resolver
    else:
        app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: claims if claims is not None else _claims()
    async def fresh(request, credentials=None):
        return await resolver() if resolver is not None else claims if claims is not None else _claims()

    monkeypatch.setattr(terminal_api, "get_web_or_bearer_user_payload", fresh)
    return TestClient(app), orch, calls


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/terminals", None),
    ("post", "/api/terminals", {}),
    ("post", f"/api/terminals/{SESSION}/input", {"data_b64": "eAo="}),
    ("get", f"/api/terminals/{SESSION}/output", None),
    ("post", f"/api/terminals/{SESSION}/resize", {"cols": 80, "rows": 24}),
])
def test_feature_off_refuses_routes_without_manager_effects(monkeypatch, method, path, body):
    monkeypatch.setitem(flags._flags, "terminal_shell", False)
    client, orch, calls = _client(monkeypatch)
    response = client.request(method, path, **({"json": body} if body is not None else {}))
    assert response.status_code == 404
    assert response.json()["error"] == "terminal_disabled"
    assert not calls
    orch.execute_authorized_tool.assert_not_awaited()


def test_default_create_refuses_before_workspace_pty_or_process(enabled, monkeypatch):
    effects = []

    def forbidden(*args, **kwargs):
        effects.append(True)
        raise AssertionError("unsupported host execution was attempted")

    monkeypatch.setattr(terminal_sessions, "_child_cwd", forbidden, raising=False)
    monkeypatch.setattr(terminal_sessions.os, "openpty", forbidden)
    manager = terminal_sessions.TerminalSessionManager()
    with pytest.raises(terminal_sessions.TerminalError) as refused:
        manager.create(OWNER, command="printf synthetic")
    assert (refused.value.code, refused.value.status_code) == ("terminal_isolation_unavailable", 503)
    assert not effects


def test_default_write_refuses_before_accessing_any_session(enabled, monkeypatch):
    manager = terminal_sessions.TerminalSessionManager()
    effects = []

    def forbidden(*args):
        effects.append(True)
        raise AssertionError("unsupported host terminal input was attempted")

    monkeypatch.setattr(manager, "_get_live", forbidden)
    with pytest.raises(terminal_sessions.TerminalError) as refused:
        manager.write(OWNER, SESSION, "eAo=")
    assert (refused.value.code, refused.value.status_code) == ("terminal_isolation_unavailable", 503)
    assert not effects


def test_authenticated_capability_reports_unsupported_execution(enabled, monkeypatch):
    client, orch, calls = _client(monkeypatch)
    response = client.get("/api/terminals/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True, "available": False, "reason": "terminal_isolation_unavailable",
    }
    assert not calls
    orch.execute_authorized_tool.assert_not_awaited()


def test_safe_read_uses_full_dispatch_and_preserves_exact_binary_payload(enabled, monkeypatch):
    client, orch, calls = _client(monkeypatch)
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert response.status_code == 200
    assert response.json() == PAYLOAD
    assert not calls
    orch.execute_authorized_tool.assert_awaited_once()
    kwargs = orch.execute_authorized_tool.await_args.kwargs
    assert kwargs["channel"] == "rest"
    assert kwargs["user_id"] == OWNER
    assert kwargs["claims"]["sub"] == OWNER
    assert kwargs["agent_id"] == "terminal-shell-1"
    assert kwargs["tool_name"] == "terminal_read"


def test_read_preserves_truthful_optional_exit_fields(enabled, monkeypatch):
    payload = {**PAYLOAD, "eof": True, "session": None, "exit_code": 3, "close_reason": "process_exited"}
    client, orch, calls = _client(
        monkeypatch, dispatch=AsyncMock(return_value=MCPResponse(result=payload)))
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert response.status_code == 200
    assert response.json()["exit_code"] == 3
    assert response.json()["close_reason"] == "process_exited"
    assert response.json()["session"] is None


def test_owner_revoked_during_dispatch_cannot_receive_retained_output(enabled, monkeypatch):
    revoked = False

    async def identity():
        if revoked:
            raise HTTPException(status_code=401, detail="Session revoked")
        return _claims()

    async def dispatch(**kwargs):
        nonlocal revoked
        revoked = True
        return MCPResponse(result=dict(PAYLOAD))

    client, orch, calls = _client(monkeypatch, resolver=identity, dispatch=AsyncMock(side_effect=dispatch))
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert response.status_code == 401
    assert "data_b64" not in response.text
    assert not calls


@pytest.mark.parametrize("extra", [{"act": {"sub": "agent"}}, {"machine_class": "framework"}])
def test_nonhuman_owner_claims_are_refused_before_dispatch(enabled, monkeypatch, extra):
    client, orch, calls = _client(monkeypatch, claims=_claims(**extra))
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert response.status_code == 403
    assert not calls
    orch.execute_authorized_tool.assert_not_awaited()


@pytest.mark.parametrize("name", ["start_terminal", "terminal_write"])
def test_arbitrary_execution_is_system_scoped_and_hard_security_flagged(name):
    entry = TOOL_REGISTRY[name]
    assert entry["scope"] == "tools:system"
    assert entry["destructive"] == "always"
    flag = ToolSecurityAnalyzer().analyze_tool(name, entry["description"], entry["input_schema"])
    assert flag is not None
    assert flag.category is ThreatCategory.CODE_EXECUTION
    assert flag.blocked is True


@pytest.mark.parametrize("method,path,body,tool,arguments,result", [
    ("get", "/api/terminals", None, "list_terminals", {}, {"sessions": []}),
    ("post", f"/api/terminals/{SESSION}/resize", {"cols": 100, "rows": 40}, "terminal_resize",
     {"session_id": SESSION, "cols": 100, "rows": 40}, {"session": {"session_id": SESSION, "cols": 100, "rows": 40}}),
    ("post", f"/api/terminals/{SESSION}/interrupt", None, "terminal_interrupt", {"session_id": SESSION},
     {"session": {"session_id": SESSION}}),
    ("delete", f"/api/terminals/{SESSION}", None, "close_terminal", {"session_id": SESSION},
     {"session_id": SESSION, "closed": True, "reason": "owner_close"}),
])
def test_safe_routes_use_governed_dispatch_without_a_parallel_manager_path(
    enabled, monkeypatch, method, path, body, tool, arguments, result,
):
    dispatch = AsyncMock(return_value=MCPResponse(result=result))
    client, orch, calls = _client(monkeypatch, dispatch=dispatch)
    response = client.request(method, path, **({"json": body} if body is not None else {}))
    assert response.status_code == 200
    assert response.json() == result.get("session", result)
    assert dispatch.await_args.kwargs["tool_name"] == tool
    assert dispatch.await_args.kwargs["arguments"] == arguments
    assert not calls


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/terminals", {}),
    ("post", f"/api/terminals/{SESSION}/input", {"data_b64": "eAo="}),
])
def test_execution_routes_report_unavailable_without_dispatch_or_manager_effects(enabled, monkeypatch, method, path, body):
    client, orch, calls = _client(monkeypatch)
    response = client.request(method, path, json=body)
    assert (response.status_code, response.json()) == (503, {"error": "terminal_isolation_unavailable"})
    assert response.headers["cache-control"] == "no-store"
    assert not calls
    orch.execute_authorized_tool.assert_not_awaited()


@pytest.mark.parametrize("result,error,expected", [
    (None, {"message": "policy refused"}, (403, "terminal_dispatch_refused")),
    (None, {"code": -32603, "data": {"terminal_code": "terminal_session_not_found", "http_status": 404}},
     (404, "terminal_session_not_found")),
    (None, {"data": {"terminal_code": "terminal_permission_denied", "http_status": True}},
     (403, "terminal_permission_denied")),
    (None, {"data": "malformed"}, (403, "terminal_dispatch_refused")),
    (None, None, (503, "terminal_invalid_result")),
    ({"unexpected": "private field"}, None, (503, "terminal_invalid_result")),
])
def test_read_refusals_and_invalid_dispatch_data_never_leak_terminal_fields(enabled, monkeypatch, result, error, expected):
    dispatch = AsyncMock(return_value=MCPResponse(result=result, error=error))
    client, orch, calls = _client(monkeypatch, dispatch=dispatch)
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert (response.status_code, response.json()["error"]) == expected
    assert "private field" not in response.text
    assert not calls


@pytest.mark.parametrize("outcome", [None, RuntimeError("synthetic private state")])
def test_missing_or_failed_dispatch_is_unavailable_without_exposing_exception_text(enabled, monkeypatch, outcome):
    dispatch = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=None)
    client, orch, calls = _client(monkeypatch, dispatch=dispatch)
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert response.status_code == 503
    assert "synthetic private state" not in response.text


@pytest.mark.parametrize("replacement,expected_status", [
    ({"sub": "other"}, 403), ({"exp": 0}, 401), ({"realm_access": {"roles": []}}, 403),
])
def test_identity_changes_during_dispatch_deny_output_delivery(enabled, monkeypatch, replacement, expected_status):
    current = _claims()

    async def identity():
        return current

    async def dispatch(**kwargs):
        nonlocal current
        current = {**current, **replacement}
        return MCPResponse(result=dict(PAYLOAD))

    client, orch, calls = _client(monkeypatch, resolver=identity, dispatch=AsyncMock(side_effect=dispatch))
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert response.status_code == expected_status
    assert "data_b64" not in response.text


def test_feature_can_be_revoked_while_dispatch_is_awaited(enabled, monkeypatch):
    async def dispatch(**kwargs):
        monkeypatch.setitem(flags._flags, "terminal_shell", False)
        return MCPResponse(result=dict(PAYLOAD))

    client, orch, calls = _client(monkeypatch, dispatch=AsyncMock(side_effect=dispatch))
    response = client.get(f"/api/terminals/{SESSION}/output")
    assert (response.status_code, response.json()) == (404, {"error": "terminal_disabled"})


@pytest.mark.parametrize("claims,status", [({}, 401), ({"sub": ""}, 401), (_claims(act={}), 403),
                                         (_claims(machine_class=""), 403), (_claims(exp=0), 401)])
def test_missing_malformed_or_nonhuman_identity_is_denied_before_any_operation(enabled, monkeypatch, claims, status):
    client, orch, calls = _client(monkeypatch, claims=claims)
    response = client.get("/api/terminals/capabilities")
    assert response.status_code == status
    assert not calls


def test_capability_remains_truthful_when_feature_is_off(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", False)
    client, orch, calls = _client(monkeypatch)
    assert client.get("/api/terminals/capabilities").json() == {
        "enabled": False, "available": False, "reason": "terminal_disabled",
    }


def test_bounded_request_validation_stays_a_client_error(enabled, monkeypatch):
    client, orch, calls = _client(monkeypatch)
    assert client.post("/api/terminals", json={"cols": 0}).status_code == 422
    assert client.post(f"/api/terminals/{SESSION}/input", json={"data_b64": "A" * 21849}).status_code == 422
    assert client.get(f"/api/terminals/{SESSION}/output", params={"wait_seconds": 26}).status_code == 422
    assert not calls
