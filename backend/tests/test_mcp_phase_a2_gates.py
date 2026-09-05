"""Pre-enablement fixes for ``FF_MCP_SERVER`` (Phase A2).

Three gates that behaved differently over MCP than over the web/WS channel:

1. the realm-role entry requirement (``user`` / ``admin``) the web callback
   enforces but ``/mcp`` did not;
2. the delegation refusal text when the server signing key is unset (the MCP
   mint is local, so "register the tools:* client scopes" was misleading);
3. the runtime supervisor's intent check, which saw EMPTY request text over
   MCP and refused every ``delete_``/``send_``/... tool.
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import mcp_authz, supervisor
from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard, AgentSkill


# --------------------------------------------------------------------------- #
# (1) realm-role gate                                                          #
# --------------------------------------------------------------------------- #


async def _authorize(required=("mcp:discover",)):
    return await mcp_authz.authorize_mcp_request(
        headers={"authorization": "Bearer opaque"},
        query_params={},
        cookies={},
        required_scopes=required,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "u1", "scope": "mcp:tools:invoke"},
        {"sub": "u1", "scope": "mcp:tools:invoke", "realm_access": {}},
        {"sub": "u1", "scope": "mcp:tools:invoke", "realm_access": {"roles": []}},
        {"sub": "u1", "scope": "mcp:tools:invoke", "realm_access": {"roles": ["guest"]}},
        {"sub": "u1", "scope": "mcp:tools:invoke", "realm_access": {"roles": "user"}},
        {"sub": "u1", "scope": "mcp:tools:invoke", "realm_access": "user"},
        # Client roles are NOT an entry grant: only realm_access counts.
        {
            "sub": "u1",
            "scope": "mcp:tools:invoke",
            "resource_access": {"astral-mcp": {"roles": ["user"]}},
        },
    ],
)
async def test_token_without_entry_realm_role_is_refused_403(monkeypatch, claims):
    async def decode(_token):
        return dict(claims)

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", decode)
    with pytest.raises(mcp_authz.MCPAuthError) as raised:
        await _authorize(("mcp:tools:invoke",))
    err = raised.value
    assert err.status_code == 403
    assert err.error == "insufficient_scope"
    assert err.required_scopes == ("mcp:tools:invoke",)
    # No claim value leaks through the description or the challenge header.
    for leaked in ("u1", "guest", "astral-mcp"):
        assert leaked not in err.description
    challenge = mcp_authz.challenge_header(
        "https://mcp.test", error=err.error, required_scopes=err.required_scopes
    )
    assert 'error="insufficient_scope"' in challenge
    assert "u1" not in challenge and "guest" not in challenge


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_token_with_entry_realm_role_is_admitted(monkeypatch, role):
    async def decode(_token):
        return {
            "sub": "u1",
            "scope": "mcp:tools:invoke",
            "realm_access": {"roles": ["other", role]},
        }

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", decode)
    claims = await _authorize(("mcp:tools:invoke",))
    assert claims["sub"] == "u1"


@pytest.mark.asyncio
async def test_role_gate_precedes_scope_gate(monkeypatch):
    """An un-admitted account gets the role refusal, not a scope hint."""

    async def decode(_token):
        return {"sub": "u1", "scope": "mcp:discover"}

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", decode)
    with pytest.raises(mcp_authz.MCPAuthError) as raised:
        await _authorize(("mcp:tools:invoke",))
    assert raised.value.description == "account lacks the required realm role"


@pytest.mark.asyncio
async def test_mock_dev_token_still_passes_role_gate(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    claims = await mcp_authz.authorize_mcp_request(
        headers={"authorization": "Bearer dev-mcp-token"},
        query_params={},
        cookies={},
        required_scopes=mcp_authz.MCP_SCOPES,
    )
    assert claims["sub"] == "test_user"
    assert mcp_authz.has_mcp_entry_role(claims)


def test_realm_roles_helper_is_defensive():
    assert mcp_authz.realm_roles({}) == frozenset()
    assert mcp_authz.realm_roles({"realm_access": None}) == frozenset()
    assert mcp_authz.realm_roles({"realm_access": {"roles": ["user", 7]}}) == {"user"}
    assert mcp_authz.MCP_ENTRY_ROLES == {"user", "admin"}


@pytest.mark.asyncio
async def test_endpoint_returns_403_and_challenge_for_roleless_token(monkeypatch):
    """End-to-end through the mounted endpoint: a signed token with no entry
    role yields 403 + WWW-Authenticate, and the body carries no claims."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator import mcp_server_endpoint as endpoint_module
    from orchestrator.mcp_server_endpoint import install_mcp_server

    monkeypatch.setenv("FF_MCP_SERVER", "1")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://mcp.test")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.test/realm")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")

    async def decode(_token):
        return {"sub": "secret-subject", "scope": "mcp:tools:invoke"}

    monkeypatch.setattr(mcp_authz, "decode_mcp_token", decode)
    app = FastAPI()
    orchestrator = MagicMock()
    orchestrator.history = None
    install_mcp_server(app, orchestrator, public_base_url="http://mcp.test")
    assert endpoint_module.authorize_mcp_request is mcp_authz.authorize_mcp_request
    client = TestClient(app)
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    response = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer opaque",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        },
        content=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
        ),
    )
    assert response.status_code == 403, response.text
    assert 'error="insufficient_scope"' in response.headers.get("www-authenticate", "")
    assert "secret-subject" not in response.text


# --------------------------------------------------------------------------- #
# (2) honest refusal when the signing key is unset                             #
# --------------------------------------------------------------------------- #


def _mcp_orchestrator():
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = {
        "reader-1": AgentCard(
            name="Reader",
            description="",
            agent_id="reader-1",
            skills=[AgentSkill(id="read_value", name="Read", description="", scope="tools:read")],
        )
    }
    orch.security_flags = {}
    orch.tool_permissions = SimpleNamespace(
        is_tool_allowed=lambda *args: True,
        get_enabled_scope_names=lambda *args: ["tools:read"],
    )
    orch.delegation = SimpleNamespace(agent_service_client_id="astral-agent-service")
    return orch


@pytest.mark.asyncio
async def test_mcp_mint_without_signing_key_marks_fault_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.delenv("DELEGATION_CHILD_SIGNING_KEY", raising=False)
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    orch = _mcp_orchestrator()
    invocation = object()
    session = {"sub": "u1", "_invocation_channel": "mcp"}
    orch.ui_sessions = {invocation: session}

    with caplog.at_level(logging.WARNING, logger="orchestrator.orchestrator"):
        first = await Orchestrator._get_delegation_token(orch, invocation, "reader-1", "u1")
        second = await Orchestrator._get_delegation_token(orch, invocation, "reader-1", "u1")
    assert first is None and second is None
    assert session["_delegation_fault"] == "signing_key_unset"
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "signing key is not configured" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "MEMORY_HMAC_KEY=" not in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_mcp_mint_with_signing_key_has_no_fault(monkeypatch):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("DELEGATION_CHILD_SIGNING_KEY", "k" * 32)
    orch = _mcp_orchestrator()
    invocation = object()
    session = {"sub": "u1", "_invocation_channel": "mcp"}
    orch.ui_sessions = {invocation: session}
    token = await Orchestrator._get_delegation_token(orch, invocation, "reader-1", "u1")
    assert token
    assert "_delegation_fault" not in session
    assert ("k" * 32) not in token


@pytest.mark.asyncio
async def test_mcp_refusal_names_signing_key_not_idp_scopes(orchestrator_factory, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    monkeypatch.delenv("DELEGATION_CHILD_SIGNING_KEY", raising=False)
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    os.environ["OPENAI_API_KEY"] = "test-key"
    orch = orchestrator_factory()
    orch.audit_recorder = MagicMock()
    orch.audit_recorder.record = AsyncMock()
    orch.send_ui_render = AsyncMock()
    orch.agent_cards["reader-1"] = _mcp_orchestrator().agent_cards["reader-1"]
    # Present the agent as an in-process built-in so dispatch reaches the
    # delegation gate (which sits after the "No agent available" check); the
    # refusal must fire BEFORE any agent code runs.
    orch.local_agents["reader-1"] = MagicMock()
    orch._execute_in_process = AsyncMock(side_effect=AssertionError("must not dispatch"))
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    orch.tool_permissions.get_enabled_scope_names = MagicMock(return_value=["tools:read"])
    orch._map_file_paths = lambda cid, a, **k: a
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)

    result = await orch.execute_mcp_tool(
        claims={"sub": "u1", "realm_access": {"roles": ["user"]}},
        user_id="u1",
        agent_id="reader-1",
        tool_name="read_value",
        arguments={},
    )
    message = (result.error or {}).get("message", "")
    assert "server delegation signing key is not configured" in message
    assert "RFC 8693" not in message
    assert "tools:* client scopes" not in message
    assert "DELEGATION_CHILD_SIGNING_KEY" in message


# --------------------------------------------------------------------------- #
# (3) supervisor intent over MCP                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tool", ["delete_records", "send-email", "drop_table", "purge.cache", "pay_invoice"]
)
def test_mcp_intent_text_aligns_destructive_tool_named_by_caller(tool):
    text = supervisor.mcp_intent_text(tool)
    assert text.startswith("call tool ")
    assert supervisor.intent_aligned(text, tool)
    # Over chat the same tool with unrelated words is still refused.
    assert not supervisor.intent_aligned("show me my dashboard", tool)
    assert not supervisor.intent_aligned("", tool)


def test_mcp_intent_text_does_not_fabricate_intent_for_listed_tools():
    """An injected destructive-tool catalogue entry whose NAME carries no verb
    still needs an intent verb: the synthesized text is only the tool name."""
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
    # Past the supervisor: falls through to the no-agent sentinel.
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
    """The 063 unattended-channel refusal is independent of the supervisor."""
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
