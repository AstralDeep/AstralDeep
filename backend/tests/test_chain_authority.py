"""Tests for backend/orchestrator/chain_authority.py: ChainBudget's per-turn
hop/depth/wall-clock ceiling and MachineTurnAuthority.derive's fail-closed consent
derivation on missing, revoked, expired, or empty-intersection scopes.
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
    MachineTurnAuthority,
)
from orchestrator.agent_authority import DEFAULT_MAX_DELEGATION_DEPTH  # noqa: E402


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


def _mta(*, grant_valid=True, latest=None, mint="fresh-token",
         current_scopes=None):
    orch = MagicMock()
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
    return MachineTurnAuthority(orch), orch, grants


@pytest.mark.asyncio
async def test_derive_skips_on_missing_consent():
    mta, _, _ = _mta(latest=None)
    out = await mta.derive(user_id="u1", agent_id="a1", consented_scopes=[],
                           grant_id=None, turn_class="parser_replay")
    assert isinstance(out, AuthoritySkip)
    assert out.reason == "missing_consent"


@pytest.mark.asyncio
async def test_derive_refuses_unknown_turn_class():
    mta, _, _ = _mta()
    out = await mta.derive(user_id="u1", agent_id="a1", consented_scopes=[],
                           grant_id="g1", turn_class="cron_gremlin")
    assert isinstance(out, AuthoritySkip)
