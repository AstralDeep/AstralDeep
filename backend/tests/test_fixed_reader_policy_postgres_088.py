"""Tests for the fixed-reader policy snapshot (orchestrator/tool_permissions.py) against
a real Plane-backed manager: stored precedence matches normal dispatch, and revoke
outranks cached turn/trust state.
"""

from types import SimpleNamespace

import pytest

from orchestrator.tool_permissions import FixedReaderPolicyError, ToolPermissionManager, turn_permission_memo
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

OWNER, AGENT, TOOL = "fixed-reader-pg", "web-research-1", "fetch_page"


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("fixed_reader_policy") as value:
        yield value


@pytest.fixture
def policy(runtime, monkeypatch):
    monkeypatch.setattr("shared.feature_flags.flags.is_enabled", lambda _: True)
    with runtime.transaction() as tx:
        for table in ("agent_scopes", "agent_trust", "agent_ownership", "tool_overrides", "user_preferences"):
            tx.execute(f"DELETE FROM {table}")
    manager = ToolPermissionManager(plane_runtime=runtime)
    manager.register_tool_scopes(AGENT, {TOOL: "tools:read"})
    orch = SimpleNamespace(tool_permissions=manager, agents={AGENT: object()}, local_agents={},
                           agent_cards={AGENT: SimpleNamespace(skills=[SimpleNamespace(id=TOOL)])},
                           security_flags={})
    return runtime, manager, orch


def check(policy):
    runtime, manager, orch = policy
    with runtime.transaction() as tx:
        return manager.assert_fixed_reader_current(
            tx, owner_id=OWNER, plane_runtime=runtime, orchestrator=orch, identity_claims={},
        )


@pytest.mark.parametrize("scope,kind,legacy,expected", [
    (False, True, False, True),
    (True, False, True, False),
    (True, None, False, False),
    (False, None, True, False),
    (True, None, None, True),
    (None, None, True, False),
])
def test_actual_stored_precedence_matches_normal_dispatch(policy, scope, kind, legacy, expected):
    runtime, manager, _ = policy
    if scope is not None:
        manager.set_agent_scopes(OWNER, AGENT, {"tools:read": scope})
    with runtime.transaction() as tx:
        for name, enabled in [("tools:read", kind), (None, legacy)]:
            if enabled is not None:
                runtime.repositories.tool_policy_state.set_tool_override(
                    tx, owner_id=OWNER, agent_id=AGENT, tool_name=TOOL,
                    permission_kind=name, enabled=enabled, updated_at=1,
                )
    assert manager.is_tool_allowed(OWNER, AGENT, TOOL) is expected
    if expected:
        assert check(policy) == "tools:read"
    else:
        with pytest.raises(FixedReaderPolicyError, match="assignment_scope_revoked"):
            check(policy)


@pytest.mark.parametrize("change", ["trust", "public", "scope", "disabled"])
def test_actual_revoke_outranks_existing_turn_and_trust_caches(policy, change):
    runtime, manager, _ = policy
    with runtime.transaction() as tx:
        tx.execute("INSERT INTO agent_trust(agent_id,is_safe) VALUES (%s,TRUE)", (AGENT,))
    with turn_permission_memo():
        assert manager.is_tool_allowed(OWNER, AGENT, TOOL)
        with runtime.transaction() as tx:
            if change == "trust":
                tx.execute("UPDATE agent_trust SET is_safe=FALSE WHERE agent_id=%s", (AGENT,))
            elif change == "public":
                tx.execute("INSERT INTO agent_ownership(agent_id,owner_email,is_public) "
                           "VALUES (%s,'synthetic@example.test',FALSE)", (AGENT,))
            elif change == "scope":
                runtime.repositories.tool_policy_state.set_scopes(
                    tx, owner_id=OWNER, agent_id=AGENT, scopes={"tools:read": False}, updated_at=1,
                )
            else:
                runtime.repositories.tool_policy_state.set_agent_disabled(
                    tx, owner_id=OWNER, agent_id=AGENT, disabled=True, updated_at=1,
                )
        assert manager.is_tool_allowed(OWNER, AGENT, TOOL)
        with pytest.raises(FixedReaderPolicyError, match="assignment_scope_revoked"):
            check(policy)
