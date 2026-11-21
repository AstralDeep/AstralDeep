"""Tests for orchestrator/chain_authority.py: a machine binding's consented scope stays
a ceiling even as a user's live grants grow, malformed or invalid bindings deny every
tool, and the shared gate denies before credentials or dispatch.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

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


def _mesh_turn(tools, scopes=("tools:read", "tools:write"), agent_id=None):
    return replace(
        _authority(list(scopes)), turn_class="mesh_request", principal="machine:mesh_request",
        agent_id=agent_id, allowed_tools=tools,
    )


def _definition(skill_id):
    from orchestrator.node_mesh_calls import skill_definition

    return skill_definition(SimpleNamespace(id=skill_id))


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


