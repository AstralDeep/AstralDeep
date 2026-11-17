"""Tests for orchestrator/chain_authority.py: a machine binding's consented scope stays
a ceiling even as a user's live grants grow, malformed or invalid bindings deny every
tool, and the shared gate denies before credentials or dispatch.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.chain_authority import (
    MachineAuthority,
    machine_scope_ceiling,
    machine_session_binding,
)


def _authority(scopes=None):
    return MachineAuthority(
        access_token="consent-token", allowed_scopes=scopes if scopes is not None else ["tools:read"],
        principal="machine:scheduled_job", user_id="owner",
        consent_ref="grant", turn_class="scheduled_job",
    )


def _orchestrator():
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.ui_sessions = {}
    orch.security_flags = {}
    orch.agent_cards = {
        "agent": SimpleNamespace(skills=[
            SimpleNamespace(id="read"), SimpleNamespace(id="write"),
        ]),
    }
    orch.tool_permissions = SimpleNamespace(
        is_tool_allowed=MagicMock(return_value=True),
        get_tool_scope=MagicMock(side_effect=lambda agent, tool: {
            "read": "tools:read", "write": "tools:write",
        }[tool]),
        get_tool_scope_map=MagicMock(side_effect=lambda agent: {
            "read": "tools:read", "write": "tools:write"}),
        get_enabled_scope_names=MagicMock(return_value=["tools:read", "tools:write"]),
    )
    orch.delegation = SimpleNamespace(
        exchange_token_for_agent=AsyncMock(return_value={"access_token": "delegated"}),
    )
    orch._audit_gate_denial = AsyncMock()
    return orch


def test_binding_snapshots_scope_list_without_exposing_it_in_audit_claims():
    authority = _authority()
    binding = machine_session_binding(authority)
    authority.allowed_scopes.append("tools:write")
    assert machine_scope_ceiling(binding) == frozenset({"tools:read"})
    assert "_machine_authority_scopes" not in authority.machine_claims()
    assert "_raw_token" not in authority.machine_claims()


@pytest.mark.parametrize("changes", [
    {"turn_class": "unknown"}, {"user_id": ""},
    {"agent_id": ""}, {"agent_id": True},
    {"allowed_scopes": ["tools:unknown"]}, {"allowed_scopes": [True]},
    {"allowed_scopes": "tools:read"},
    {"scope_ceiling_required": False}, {"scope_ceiling_required": "false"},
    {"turn_class": "parser_replay", "scope_ceiling_required": False},
])
def test_invalid_derived_binding_is_refused(changes):
    with pytest.raises(ValueError, match="invalid machine authority"):
        machine_session_binding(replace(_authority(), **changes))


@pytest.mark.parametrize("session", [
    {"machine_class": "scheduled_job"},
    {"machine_class": "scheduled_job", "_machine_authority_scopes": ["tools:read"]},
    {"machine_class": "scheduled_job", "_machine_authority_scopes": ("unknown",)},
    {"machine_class": "scheduled_job", "_machine_authority_scopes": (True,)},
    {"machine_class": "unknown", "_machine_authority_scopes": ("tools:read",)},
    {"_machine_authority_scopes": ("tools:read",)},
    {"machine_class": "parser_replay"},
    {"machine_class": "draft_self_test"},
    {"machine_class": "scheduled_job", "_machine_authority_scopes": None},
    {"machine_class": "persistent_assignment", "_machine_authority_scopes": None},
])
def test_incomplete_or_malformed_machine_binding_denies_every_tool(session):
    assert machine_scope_ceiling(session) == frozenset()


def test_ordinary_session_has_no_machine_ceiling():
    assert machine_scope_ceiling({"sub": "owner"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "agent"])
async def test_current_grants_cannot_expand_consented_token_or_tool_authority(agent_id):
    orch = _orchestrator()
    socket = type("Socket", (), {})()
    orch._bind_machine_turn(socket, replace(_authority(), agent_id=agent_id))
    assert await orch._get_delegation_token(socket, "agent", "owner") == "delegated"
    orch.delegation.exchange_token_for_agent.assert_awaited_once_with(
        "consent-token", "agent", ["read"], "owner", ["tools:read"],
    )
    orch._unbind_machine_turn(socket)
    assert socket not in orch.ui_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_kind", ["empty", "malformed", "other_owner", "other_agent"])
async def test_zero_or_invalid_machine_authority_never_requests_a_token(binding_kind):
    orch = _orchestrator()
    socket = object()
    binding = machine_session_binding(_authority([]))
    if binding_kind == "malformed":
        binding.pop("_machine_authority_scopes")
    if binding_kind == "other_owner":
        binding = machine_session_binding(_authority())
        binding["sub"] = "somebody-else"
    if binding_kind == "other_agent":
        binding = machine_session_binding(replace(_authority(), agent_id="other-agent"))
    orch.ui_sessions[socket] = binding
    assert await orch._get_delegation_token(socket, "agent", "owner") is None
    orch.delegation.exchange_token_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_token_request_keeps_existing_permissions():
    orch = _orchestrator()
    socket = object()
    orch.ui_sessions[socket] = {"sub": "owner", "_raw_token": "interactive"}
    assert await orch._get_delegation_token(socket, "agent", "owner") == "delegated"
    orch.delegation.exchange_token_for_agent.assert_awaited_once_with(
        "interactive", "agent", ["read", "write"], "owner", ["tools:read", "tools:write"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_class", ["parser_replay", "draft_self_test"])
@pytest.mark.parametrize("explicit_empty", [False, True])
async def test_standing_paths_preserve_unspecified_but_never_widen_explicit_empty(turn_class, explicit_empty):
    orch = _orchestrator()
    socket = type("Socket", (), {})()
    authority = replace(
        _authority([]), turn_class=turn_class, principal=f"machine:{turn_class}",
        scope_ceiling_required=explicit_empty,
    )
    orch._bind_machine_turn(socket, authority)
    result = await orch._get_delegation_token(socket, "agent", "owner")
    if explicit_empty:
        assert result is None
        orch.delegation.exchange_token_for_agent.assert_not_awaited()
    else:
        assert result == "delegated"
        orch.delegation.exchange_token_for_agent.assert_awaited_once_with(
            "consent-token", "agent", ["read", "write"], "owner", ["tools:read", "tools:write"],
        )
    orch._unbind_machine_turn(socket)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "write", "empty", "lookup_failure", "owner_mismatch", "malformed", "other_agent",
])
async def test_shared_gate_denies_before_credentials_and_dispatch(case):
    from orchestrator.orchestrator import GateRefusal

    orch = _orchestrator()
    socket = object()
    binding = machine_session_binding(_authority([] if case == "empty" else None))
    if case == "malformed":
        binding.pop("_machine_authority_scopes")
    if case == "owner_mismatch":
        binding["sub"] = "somebody-else"
    if case == "lookup_failure":
        orch.tool_permissions.get_tool_scope.side_effect = RuntimeError("unavailable")
    if case == "other_agent":
        binding = machine_session_binding(replace(_authority(), agent_id="other-agent"))
    orch.ui_sessions[socket] = binding
    args = {}
    result = await orch._run_gate_stack(
        socket, "agent", "write" if case == "write" else "read", args, user_id="owner",
    )
    assert isinstance(result, GateRefusal)
    assert "outside this unattended task's approved permissions" in result.response.error["message"]
    assert result.response.error["retryable"] is False
    assert not args
    orch.delegation.exchange_token_for_agent.assert_not_awaited()
    orch.tool_permissions.is_tool_allowed.assert_not_called()
    orch._audit_gate_denial.assert_awaited_once()


@pytest.mark.asyncio
async def test_machine_exchange_failure_still_refuses_when_development_allows_unscoped_tools(monkeypatch):
    from orchestrator import hitl, policy, supervisor, taint
    from orchestrator.orchestrator import GateRefusal

    for module, name in (
        (policy, "policy_enabled"), (taint, "taint_enabled"),
        (supervisor, "supervisor_enabled"), (hitl, "hitl_enabled"),
    ):
        monkeypatch.setattr(module, name, lambda: False)
    orch = _orchestrator()
    socket = object()
    orch.ui_sessions[socket] = machine_session_binding(_authority())
    orch.agents = {"agent": object()}
    orch.local_agents = {}
    orch.credential_manager = SimpleNamespace(
        get_agent_credentials_encrypted=MagicMock(return_value=None),
    )
    orch._delegation_required = lambda: False
    orch._delegation_denied_for_permissions = AsyncMock(return_value=False)
    orch.delegation.exchange_token_for_agent.return_value = {"error": "exchange_failed"}
    result = await orch._run_gate_stack(socket, "agent", "read", {}, user_id="owner")
    assert isinstance(result, GateRefusal)
    assert "could not obtain delegated authorization" in result.response.error["message"]
    assert "DELEGATION_REQUIRED=false" not in result.response.error["message"]
    assert result.response.error["retryable"] is False


def _mesh_turn(tools, scopes=("tools:read", "tools:write"), agent_id=None):
    return replace(
        _authority(list(scopes)), turn_class="mesh_request", principal="machine:mesh_request",
        agent_id=agent_id, allowed_tools=tools,
    )


def _definition(skill_id):
    from orchestrator.node_mesh_calls import skill_definition

    return skill_definition(SimpleNamespace(id=skill_id))


def _mesh_orchestrator():
    orch = _orchestrator()
    orch.agent_cards["agent"].skills.append(SimpleNamespace(id="peek"))
    scopes = {"read": "tools:read", "write": "tools:write", "peek": "tools:read"}
    orch.tool_permissions.get_tool_scope.side_effect = lambda agent, tool: scopes[tool]
    orch.tool_permissions.get_tool_scope_map.side_effect = lambda agent: dict(scopes)
    return orch


@pytest.mark.parametrize("changes", [
    {"turn_class": "scheduled_job"},
    {"agent_id": "agent"},
    {"allowed_tools": [("agent", "read", "sha256:x")]},
    {"allowed_tools": (("agent", "read"),)},
    {"allowed_tools": (("agent", "read", ""),)},
    {"allowed_tools": (("agent", "read", 7),)},
])
def test_a_tool_list_binds_only_to_a_whole_mesh_turn(changes):
    authority = replace(_mesh_turn((("agent", "read", _definition("read")),)), **changes)
    with pytest.raises(ValueError, match="invalid machine authority"):
        machine_session_binding(authority)


def test_the_tool_list_admits_exact_tools_at_their_current_definition():
    from orchestrator.chain_authority import machine_tool_admitted

    orch = _mesh_orchestrator()
    whole_turn = machine_session_binding(_mesh_turn((("agent", "read", _definition("read")),)))
    assert machine_tool_admitted(orch, whole_turn, "agent", "read") is True
    assert machine_tool_admitted(orch, whole_turn, "agent", "peek") is False
    assert machine_tool_admitted(orch, whole_turn, "agent", "write") is False
    assert machine_tool_admitted(orch, whole_turn, "unknown", "read") is False
    orch.agent_cards["agent"].skills[0] = SimpleNamespace(id="read", description="changed")
    assert machine_tool_admitted(orch, whole_turn, "agent", "read") is False
    missing = machine_session_binding(_mesh_turn(None))
    assert machine_tool_admitted(orch, missing, "agent", "peek") is False
    peer_call = machine_session_binding(_mesh_turn(None, agent_id="agent"))
    assert machine_tool_admitted(orch, peer_call, "agent", "peek") is True
    scheduled = machine_session_binding(_authority())
    assert machine_tool_admitted(orch, scheduled, "agent", "peek") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("personal", [False, True])
@pytest.mark.parametrize("tools", ["granted", "empty", "missing", "changed"])
async def test_delegated_mesh_tools_never_exceed_the_exact_root_tools(monkeypatch, personal, tools):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from orchestrator import agent_authority as delegation
    from shared import node_identity

    identity = node_identity.NodeIdentity.from_private(Ed25519PrivateKey.generate())
    monkeypatch.setattr(node_identity, "is_personal", lambda: personal)
    monkeypatch.setattr(delegation, "_node_identity_or_config_error", lambda: identity)
    orch = _mesh_orchestrator()
    orch.delegation.agent_service_client_id = "test-agent-service"
    allowed = None if tools == "missing" else () if tools == "empty" else (("agent", "read", _definition("read")),)
    claims = machine_session_binding(_mesh_turn(allowed, scopes=("tools:read",)))
    if tools == "changed":
        orch.agent_cards["agent"].skills[0].description = "changed since approval"
    socket = object()
    orch.ui_sessions[socket] = claims
    token = await orch._get_delegation_token(socket, "agent", "owner")
    if tools != "granted":
        unexpected_credential = token is not None
        assert unexpected_credential is False
        orch.delegation.exchange_token_for_agent.assert_not_awaited()
    elif personal:
        payload = identity.verify_token(token, audience="test-agent-service")
        assert set(payload["scope"].split()) == {"tools:read", "tool:read"}
        orch.delegation.exchange_token_for_agent.assert_not_awaited()
    else:
        assert token == "delegated"
        orch.delegation.exchange_token_for_agent.assert_awaited_once_with(
            "consent-token", "agent", ["read"], "owner", ["tools:read"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("personal", [False, True])
@pytest.mark.parametrize("authority", ["interactive", "scheduled", "peer_call"])
async def test_other_authorities_keep_enabled_siblings_when_delegating(monkeypatch, personal, authority):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from orchestrator import agent_authority as delegation
    from shared import node_identity

    identity = node_identity.NodeIdentity.from_private(Ed25519PrivateKey.generate())
    monkeypatch.setattr(node_identity, "is_personal", lambda: personal)
    monkeypatch.setattr(delegation, "_node_identity_or_config_error", lambda: identity)
    orch = _mesh_orchestrator()
    orch.delegation.agent_service_client_id = "test-agent-service"
    claims = {"sub": "owner", "_raw_token": "interactive"}
    if authority == "scheduled":
        claims = machine_session_binding(_authority())
    elif authority == "peer_call":
        claims = machine_session_binding(_mesh_turn(None, scopes=("tools:read",), agent_id="agent"))
    socket = object()
    orch.ui_sessions[socket] = claims
    token = await orch._get_delegation_token(socket, "agent", "owner")
    expected = ["read", "write", "peek"] if authority == "interactive" else ["read", "peek"]
    if personal:
        payload = identity.verify_token(token, audience="test-agent-service")
        assert {scope for scope in payload["scope"].split() if scope.startswith("tool:")} == {
            "tool:" + tool for tool in expected
        }
        orch.delegation.exchange_token_for_agent.assert_not_awaited()
    else:
        assert token == "delegated"
        assert orch.delegation.exchange_token_for_agent.await_args.args[2] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["peek", "write"])
async def test_a_sibling_tool_sharing_a_granted_scope_is_refused_at_the_shared_gate(tool):
    from orchestrator.orchestrator import GateRefusal

    orch = _mesh_orchestrator()
    socket = object()
    orch.ui_sessions[socket] = machine_session_binding(_mesh_turn((("agent", "read", _definition("read")),)))
    args = {}
    result = await orch._run_gate_stack(socket, "agent", tool, args, user_id="owner")
    assert isinstance(result, GateRefusal)
    assert "outside this unattended task's approved permissions" in result.response.error["message"]
    assert orch._audit_gate_denial.await_args.kwargs == {"gate": "machine_scope", "detail": "outside_consented_scopes"}
    orch.delegation.exchange_token_for_agent.assert_not_awaited()
    orch.tool_permissions.is_tool_allowed.assert_not_called()


@pytest.mark.asyncio
async def test_a_listed_tool_passes_the_machine_gate_to_the_ordinary_gates(monkeypatch):
    from orchestrator import hitl, policy, supervisor, taint
    from orchestrator.orchestrator import GateRefusal

    for module, name in (
        (policy, "policy_enabled"), (taint, "taint_enabled"),
        (supervisor, "supervisor_enabled"), (hitl, "hitl_enabled"),
    ):
        monkeypatch.setattr(module, name, lambda: False)
    orch = _mesh_orchestrator()
    socket = object()
    orch.ui_sessions[socket] = machine_session_binding(_mesh_turn((("agent", "read", _definition("read")),)))
    orch.agents = {"agent": object()}
    orch.local_agents = {}
    orch.credential_manager = SimpleNamespace(get_agent_credentials_encrypted=MagicMock(return_value=None))
    orch._delegation_required = lambda: False
    orch._delegation_denied_for_permissions = AsyncMock(return_value=False)
    orch.delegation.exchange_token_for_agent.return_value = {"error": "exchange_failed"}
    result = await orch._run_gate_stack(socket, "agent", "read", {}, user_id="owner")
    assert isinstance(result, GateRefusal)
    assert "could not obtain delegated authorization" in result.response.error["message"]
    assert all(call.kwargs.get("gate") != "machine_scope" for call in orch._audit_gate_denial.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id,tool", [
    ("__memory__", "memory_get"), ("__memory__", "memory_search"),
    ("__subtasks__", "delegate_subtasks"), ("__skill_guidance__", "read_user_skill"),
    ("__desktop_codegen__", "offer_desktop_codegen"), ("__scheduler__", "create_schedule"),
])
@pytest.mark.parametrize("peer_call", [False, True])
async def test_every_host_meta_tool_is_refused_in_a_mesh_turn(agent_id, tool, peer_call):
    orch = _mesh_orchestrator()
    socket = object()
    tools = None if peer_call else (("agent", "read", _definition("read")),)
    orch.ui_sessions[socket] = machine_session_binding(_mesh_turn(tools, agent_id="agent" if peer_call else None))
    refusal = await orch._guard_machine_meta_tool(socket, agent_id, tool, {}, "chat", "owner")
    assert refusal is not None and refusal.error["code"] == "machine_meta_scope_denied"
    assert orch._audit_gate_denial.await_args.kwargs == {"gate": "machine_scope", "detail": "host_meta_tool_not_consented"}


@pytest.mark.asyncio
async def test_other_unattended_turns_keep_their_read_only_meta_tools():
    orch = _mesh_orchestrator()
    socket = object()
    orch.ui_sessions[socket] = machine_session_binding(replace(_authority(), agent_id=None))
    assert await orch._guard_machine_meta_tool(socket, "__memory__", "memory_get", {}, "chat", "owner") is None
    assert await orch._guard_machine_meta_tool(socket, "__subtasks__", "delegate_subtasks", {}, "chat", "owner") is None
