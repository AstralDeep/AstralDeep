"""Tests for the closed fixed-reader tool-policy snapshot
(orchestrator/tool_permissions.py, AstralPlane tool_policy repository):
explicit-precedence matching, visibility/draft-exclusion parity with normal runtime,
and fail-closed inputs.
"""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astralplane.repositories.tool_policy import FixedReaderPolicySnapshot, ScopeState, ToolOverrideState
from orchestrator.agent_identity import IDENTITY_TRUST_ENV
from orchestrator.tool_permissions import FixedReaderPolicyError, ToolPermissionManager, turn_permission_memo

OWNER, AGENT, TOOL, SCOPE = "fixed-owner", "web-research-1", "fetch_page", "tools:read"


@pytest.fixture
def policy(monkeypatch):
    tx = object()
    state = SimpleNamespace(snapshot=FixedReaderPolicySnapshot(
        OWNER, (), (), False, False, None, None, False, None,
    ), transactions=0, snapshots=0, forbid_transaction=False, safe_enabled=True)

    @contextmanager
    def transaction():
        assert not state.forbid_transaction, "a second pool transaction is forbidden"
        state.transactions += 1
        yield tx

    def snapshot(transaction, *, owner_id):
        assert transaction is tx and owner_id == OWNER
        state.snapshots += 1
        return state.snapshot

    def agent(transaction, *, agent_id):
        snap = state.snapshot
        if snap.user_agent_owner is None:
            return None
        return SimpleNamespace(owner_id=snap.user_agent_owner,
                               deleted_at=1 if snap.user_agent_deleted else None)

    repository = SimpleNamespace(
        lock_fixed_reader_policy_snapshot=snapshot,
        list_scopes=lambda *a, **k: state.snapshot.scopes,
        list_overrides=lambda *a, **k: state.snapshot.overrides,
    )
    agents = SimpleNamespace(
        get_agent_for_administration=agent,
        get_trust=lambda *a, **k: SimpleNamespace(is_safe=state.snapshot.is_safe),
        get_ownership=lambda *a, **k: None if state.snapshot.is_public is None else
            SimpleNamespace(is_public=state.snapshot.is_public),
    )
    runtime = SimpleNamespace(transaction=transaction, repositories=SimpleNamespace(
        tool_policy_state=repository, agents=agents,
    ))
    manager = ToolPermissionManager(plane_runtime=runtime)
    manager.register_tool_scopes(AGENT, {TOOL: SCOPE})
    card = SimpleNamespace(agent_id=AGENT, skills=[SimpleNamespace(id=TOOL)], metadata={})
    orch = SimpleNamespace(tool_permissions=manager, agents={AGENT: object()}, local_agents={},
                           agent_cards={AGENT: card}, security_flags={}, lifecycle_manager=object())
    monkeypatch.setattr("shared.feature_flags.flags.is_enabled", lambda _: state.safe_enabled)
    state.tx, state.runtime, state.manager, state.orch = tx, runtime, manager, orch
    return state


def check(state, **kwargs):
    return state.manager.assert_fixed_reader_current(
        state.tx, owner_id=OWNER, plane_runtime=state.runtime,
        orchestrator=state.orch, identity_claims={}, **kwargs,
    )


def scopes(enabled):
    return () if enabled is None else (ScopeState(OWNER, AGENT, SCOPE, enabled, 1),)


def overrides(kind, legacy):
    return tuple(ToolOverrideState(OWNER, AGENT, TOOL, scope, value, 1)
                 for scope, value in [(SCOPE, kind), (None, legacy)] if value is not None)


@pytest.mark.parametrize("kind", [None, False, True])
@pytest.mark.parametrize("legacy", [None, False, True])
@pytest.mark.parametrize("scope", [None, False, True])
def test_explicit_precedence_matches_runtime_and_never_opens_second_transaction(
    policy, kind, legacy, scope,
):
    policy.snapshot = replace(policy.snapshot, scopes=scopes(scope), overrides=overrides(kind, legacy))
    expected = kind if kind is not None else False if legacy is False else bool(scope)
    assert policy.manager.is_tool_allowed(OWNER, AGENT, TOOL) is expected
    assert policy.snapshots == 0
    previous = policy.transactions
    policy.forbid_transaction = True
    if expected:
        assert check(policy) == SCOPE
    else:
        with pytest.raises(FixedReaderPolicyError, match="^assignment_scope_revoked$"):
            check(policy)
    assert policy.transactions == previous and policy.snapshots == 1


@pytest.mark.parametrize("feature,safe,public,owner,deleted,expected", [
    (True, True, None, None, False, True),
    (True, True, True, None, False, True),
    (True, True, False, None, False, False),
    (False, True, True, None, False, False),
    (True, False, True, None, False, False),
    (False, False, False, OWNER, False, True),
    (True, True, True, "other", False, False),
    (True, True, True, OWNER, True, False),
    (True, False, False, "other", False, False),
])
def test_safe_owned_and_owner_isolation_defaults_match_normal_runtime(
    policy, feature, safe, public, owner, deleted, expected,
):
    policy.safe_enabled = feature
    policy.snapshot = replace(policy.snapshot, is_safe=safe, is_public=public,
                              user_agent_owner=owner, user_agent_deleted=deleted)
    assert policy.manager.is_tool_allowed(OWNER, AGENT, TOOL) is expected
    policy.forbid_transaction = True
    if expected:
        assert check(policy) == SCOPE
    else:
        with pytest.raises(FixedReaderPolicyError, match="assignment_scope_revoked"):
            check(policy)


@pytest.mark.parametrize("mutation", [
    "disconnected", "missing_card", "missing_skill", "blocked", "disabled",
    "owner", "deleted", "draft", "scope", "identity", "malformed_identity",
])
def test_current_visibility_gates_cannot_be_overridden_by_explicit_grant(policy, mutation):
    policy.snapshot = replace(policy.snapshot, overrides=overrides(True, None))
    if mutation == "disconnected":
        policy.orch.agents = {}
    elif mutation == "missing_card":
        policy.orch.agent_cards = {}
    elif mutation == "missing_skill":
        policy.orch.agent_cards[AGENT].skills = []
    elif mutation == "blocked":
        policy.orch.security_flags = {AGENT: {TOOL: {"blocked": True}}}
    elif mutation == "disabled":
        policy.snapshot = replace(policy.snapshot, disabled=True)
    elif mutation == "owner":
        policy.snapshot = replace(policy.snapshot, user_agent_owner="other")
    elif mutation == "deleted":
        policy.snapshot = replace(policy.snapshot, user_agent_owner=OWNER, user_agent_deleted=True)
    elif mutation == "draft":
        policy.snapshot = replace(policy.snapshot, draft_status="pending")
    elif mutation == "scope":
        policy.manager.register_tool_scopes(AGENT, {TOOL: "tools:write"})
    elif mutation == "identity":
        policy.orch.agent_cards[AGENT].metadata = {"required_identity_claims": ["orcid"]}
    else:
        policy.orch.agent_cards[AGENT].metadata = {"required_identity_claims": "orcid"}
    with pytest.raises(FixedReaderPolicyError, match="assignment_scope_revoked"):
        check(policy)


@pytest.mark.parametrize("public,lifecycle", [(True, True), (False, False)])
def test_draft_exclusion_preserves_existing_public_or_no_lifecycle_semantics(policy, public, lifecycle):
    policy.snapshot = replace(policy.snapshot, scopes=scopes(True), draft_status="pending", is_public=public)
    if not lifecycle:
        del policy.orch.lifecycle_manager
    policy.orch.local_agents, policy.orch.agents = policy.orch.agents, {}
    assert check(policy) == SCOPE


def test_existing_verified_identity_predicate_is_used(policy, monkeypatch):
    policy.snapshot = replace(policy.snapshot, scopes=scopes(True))
    policy.orch.agent_cards[AGENT].metadata = {"required_identity_claims": ["orcid"]}
    monkeypatch.setenv(IDENTITY_TRUST_ENV, AGENT)
    assert policy.manager.assert_fixed_reader_current(
        policy.tx, owner_id=OWNER, plane_runtime=policy.runtime, orchestrator=policy.orch,
        identity_claims={"orcid": "0000-0002-1825-0097"},
    ) == SCOPE


def test_snapshot_bypasses_stale_turn_and_safe_caches(policy):
    policy.snapshot = replace(policy.snapshot, is_safe=True)
    with turn_permission_memo():
        assert policy.manager.is_tool_allowed(OWNER, AGENT, TOOL)
        policy.snapshot = replace(policy.snapshot, is_safe=False)
        assert policy.manager.is_tool_allowed(OWNER, AGENT, TOOL)
        with pytest.raises(FixedReaderPolicyError, match="assignment_scope_revoked"):
            check(policy)


@pytest.mark.parametrize("corruption", ["runtime", "agents_runtime", "manager", "snapshot", "owner", "error"])
def test_unavailable_or_foreign_inputs_fail_closed_without_data(policy, corruption):
    if corruption == "runtime":
        policy.runtime = object()
    elif corruption == "agents_runtime":
        policy.manager._agents._runtime = object()
    elif corruption == "manager":
        policy.orch.tool_permissions = object()
    elif corruption == "snapshot":
        policy.snapshot = SimpleNamespace(owner_id=OWNER)
    elif corruption == "owner":
        policy.snapshot = replace(policy.snapshot, owner_id="other")
    else:
        def failed(*a, **k):
            raise RuntimeError("sensitive policy content")
        policy.manager._policy.repository.lock_fixed_reader_policy_snapshot = failed
    with pytest.raises(FixedReaderPolicyError) as caught:
        check(policy)
    assert str(caught.value) == "assignment_source_permission_unavailable"


def test_memory_visibility_is_read_after_snapshot_lock_returns(policy):
    policy.snapshot = replace(policy.snapshot, scopes=scopes(True))
    original = policy.manager._policy.repository.lock_fixed_reader_policy_snapshot
    def changed(*a, **k):
        policy.orch.security_flags = {AGENT: {TOOL: {"blocked": True}}}
        return original(*a, **k)
    policy.manager._policy.repository.lock_fixed_reader_policy_snapshot = changed
    with pytest.raises(FixedReaderPolicyError, match="assignment_scope_revoked"):
        check(policy)
