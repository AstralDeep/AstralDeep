"""T005 (056-delegated-agent-chaining): ChainBudget + MachineTurnAuthority.

The chain budget is the global per-turn ceiling over every hop and sub-task
(FR-021); machine-turn authority is the one shared consent-derivation seam all
machine-turn classes inherit (FR-012), fail-closed on missing/revoked/expired
consent and on an empty (consented ∩ current) scope set (FR-013), with
revocation re-checked at derivation time (FR-006).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.chain_authority import (  # noqa: E402
    AuthoritySkip,
    ChainBudget,
    MachineAuthority,
    MachineTurnAuthority,
)
from orchestrator.delegation import DEFAULT_MAX_DELEGATION_DEPTH  # noqa: E402


# --------------------------------------------------------------------------- #
# ChainBudget
# --------------------------------------------------------------------------- #

def test_budget_charges_until_hop_limit():
    b = ChainBudget(turn_id="t1", max_hops=3, wall_clock_s=999)
    assert b.charge(1) is None
    assert b.charge(1) is None
    assert b.charge(1) is None
    assert b.charge(1) == "hop_budget_exhausted"
    assert b.spent_hops == 3


def test_budget_refuses_over_depth():
    b = ChainBudget(turn_id="t1")
    assert b.charge(DEFAULT_MAX_DELEGATION_DEPTH) is None
    assert b.charge(DEFAULT_MAX_DELEGATION_DEPTH + 1) == "depth_exceeded"


def test_budget_wall_clock_exhaustion():
    b = ChainBudget(turn_id="t1", wall_clock_s=0.0)
    assert b.charge(1) == "wall_clock_exhausted"
    assert b.exhausted() == "wall_clock_exhausted"


def test_subtree_slice_debits_parent():
    parent = ChainBudget(turn_id="t1", max_hops=4, wall_clock_s=999)
    child = parent.slice(max_hops=3)
    for _ in range(3):
        assert child.charge(1) is None
    # Child slice exhausted its own allotment.
    assert child.charge(1) == "hop_budget_exhausted"
    # Every child charge also debited the parent (global ceiling holds).
    assert parent.spent_hops == 3
    assert parent.charge(1) is None
    assert parent.charge(1) == "hop_budget_exhausted"


def test_subtree_slice_cannot_exceed_parent_ceiling():
    parent = ChainBudget(turn_id="t1", max_hops=2, wall_clock_s=999)
    child = parent.slice(max_hops=10)
    assert child.max_hops == 2  # clamped to the global ceiling
    assert child.charge(1) is None
    assert child.charge(1) is None
    assert child.charge(1) == "hop_budget_exhausted"


# --------------------------------------------------------------------------- #
# MachineTurnAuthority.derive
# --------------------------------------------------------------------------- #

def _mta(*, grant_valid=True, latest=None, mint="fresh-token",
         current_scopes=None):
    orch = MagicMock()
    # Callers describe the user's CURRENT grants as a {scope: enabled} dict, but
    # derive reads the EFFECTIVE list (get_enabled_scope_names) so the feature-040
    # safe baseline is not mistaken for "no grants" — a raw agent_scopes read is
    # empty for the post-040 default population.
    _scopes = ({"tools:read": True, "tools:search": True}
               if current_scopes is None else current_scopes)
    orch.tool_permissions.get_agent_scopes = MagicMock(return_value=dict(_scopes))
    orch.tool_permissions.get_enabled_scope_names = MagicMock(
        return_value=[s for s, on in _scopes.items() if on])
    grants = MagicMock()
    grants.is_valid = MagicMock(return_value=grant_valid)
    grants.latest_valid_for = MagicMock(return_value=latest)
    if isinstance(mint, Exception):
        grants.mint_access_token = AsyncMock(side_effect=mint)
    else:
        grants.mint_access_token = AsyncMock(return_value=mint)
    return MachineTurnAuthority(orch, grants), orch, grants


@pytest.mark.asyncio
async def test_derive_success_shape():
    mta, _, grants = _mta()
    out = await mta.derive(
        user_id="u1", agent_id="a1",
        consented_scopes=["tools:read", "tools:write"],
        grant_id="g1", turn_class="scheduled_job")
    assert isinstance(out, MachineAuthority)
    assert out.access_token == "fresh-token"
    # Narrowed to (consented ∩ current): write consented but not current.
    assert out.allowed_scopes == ["tools:read"]
    assert out.principal == "machine:scheduled_job"
    assert out.consent_ref == "g1"
    claims = out.machine_claims()
    assert claims == {"sub": "u1", "machine_class": "scheduled_job",
                      "consent_ref": "g1"}
    assert "fresh-token" not in str(claims)  # no token bytes in the marker
    grants.mint_access_token.assert_awaited_once_with("g1")


@pytest.mark.asyncio
async def test_derive_skips_on_missing_consent():
    mta, _, _ = _mta(latest=None)
    out = await mta.derive(user_id="u1", agent_id="a1", consented_scopes=[],
                           grant_id=None, turn_class="parser_replay")
    assert isinstance(out, AuthoritySkip)
    assert out.reason == "missing_consent"


@pytest.mark.asyncio
async def test_derive_falls_back_to_latest_valid_grant():
    mta, _, grants = _mta(latest="g-standing")
    out = await mta.derive(user_id="u1", agent_id=None, consented_scopes=None,
                           grant_id=None, turn_class="draft_self_test")
    assert isinstance(out, MachineAuthority)
    assert out.consent_ref == "g-standing"
    assert out.principal == "machine:draft_self_test"
    grants.latest_valid_for.assert_called_once_with("u1", None)


@pytest.mark.asyncio
async def test_derive_skips_on_revoked_or_expired():
    mta, _, _ = _mta(grant_valid=False)
    out = await mta.derive(user_id="u1", agent_id="a1", consented_scopes=[],
                           grant_id="g1", turn_class="scheduled_job")
    assert isinstance(out, AuthoritySkip)
    assert out.reason == "revoked_or_expired"


@pytest.mark.asyncio
async def test_derive_skips_on_mint_failure():
    mta, _, _ = _mta(mint=RuntimeError("keycloak-side revocation"))
    out = await mta.derive(user_id="u1", agent_id="a1", consented_scopes=[],
                           grant_id="g1", turn_class="scheduled_job")
    assert isinstance(out, AuthoritySkip)
    assert out.reason == "mint_failed"
    assert "keycloak" in out.detail


@pytest.mark.asyncio
async def test_derive_skips_on_empty_intersection():
    mta, _, _ = _mta(current_scopes={"tools:read": False})
    out = await mta.derive(
        user_id="u1", agent_id="a1", consented_scopes=["tools:read"],
        grant_id="g1", turn_class="scheduled_job")
    assert isinstance(out, AuthoritySkip)
    assert out.reason == "empty_scopes"


@pytest.mark.asyncio
async def test_derive_agentless_job_has_no_scope_skip():
    """A job with no agent has no scope set to intersect — not a skip."""
    mta, _, _ = _mta()
    out = await mta.derive(user_id="u1", agent_id=None, consented_scopes=None,
                           grant_id="g1", turn_class="scheduled_job")
    assert isinstance(out, MachineAuthority)
    assert out.allowed_scopes == []


@pytest.mark.asyncio
async def test_derive_refuses_unknown_turn_class():
    mta, _, _ = _mta()
    out = await mta.derive(user_id="u1", agent_id="a1", consented_scopes=[],
                           grant_id="g1", turn_class="cron_gremlin")
    assert isinstance(out, AuthoritySkip)
