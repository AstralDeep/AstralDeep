"""Tests for the global chain budget (backend/orchestrator/chain_authority.py): one
per-turn ceiling over cumulative depth, hop count, and wall clock across all hops,
reset at turn start.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.chain_authority import ChainBudget  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402


def test_budget_is_per_turn_not_global():
    o = MagicMock()
    o._chain_budgets = {}
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)
    a = o._chain_budget_for("chat-a")
    b = o._chain_budget_for("chat-b")
    assert a is not b
    for _ in range(a.max_hops):
        assert a.charge(1) is None
    assert a.charge(1) == "hop_budget_exhausted"
    assert b.charge(1) is None


def test_global_budget_recreated_when_exhausted():
    o = MagicMock()
    o._chain_budgets = {}
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)
    first = o._chain_budget_for(None)
    first.wall_clock_s = 0.0
    assert first.exhausted() is not None
    second = o._chain_budget_for(None)
    assert second is not first
    assert second.exhausted() is None


def test_chat_keyed_budget_not_recreated_while_live():
    o = MagicMock()
    o._chain_budgets = {}
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)
    first = o._chain_budget_for("c1")
    first.wall_clock_s = 0.0
    second = o._chain_budget_for("c1")
    assert second is first


def test_new_turn_resets_the_budget():
    o = MagicMock()
    o._chain_budgets = {}
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)
    first = o._chain_budget_for("c1")
    first.charge(1)
    assert first.spent_hops == 1
    o._chain_budgets.pop("c1", None)
    second = o._chain_budget_for("c1")
    assert second is not first
    assert second.spent_hops == 0


def test_depth_bound_composes_with_the_048_bound():
    from orchestrator.agent_authority import DEFAULT_MAX_DELEGATION_DEPTH

    b = ChainBudget(turn_id="t")
    assert b.max_depth == DEFAULT_MAX_DELEGATION_DEPTH
    assert b.charge(DEFAULT_MAX_DELEGATION_DEPTH) is None
    assert b.charge(DEFAULT_MAX_DELEGATION_DEPTH + 1) == "depth_exceeded"
