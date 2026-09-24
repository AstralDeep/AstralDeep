"""Tests for the global chain budget (backend/orchestrator/chain_authority.py): one
per-turn ceiling over cumulative depth, hop count, and wall clock across all hops and
sub-tasks, reset at turn start.
"""

from __future__ import annotations

import os
import sys
import types
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import subtasks  # noqa: E402
from orchestrator.chain_authority import ChainBudget  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from shared.feature_flags import flags  # noqa: E402


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)


@pytest.fixture
def orch(monkeypatch):
    o = MagicMock()
    o.history.create_chat = MagicMock(side_effect=lambda user_id=None, **k: "sub-chat")
    o.ui_sessions = {}
    o._safe_send = AsyncMock()
    o._chain_budgets = {}
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)

    async def _turn(vws, message, chat_id, **kw):
        await vws.send_json({"type": "chat_message", "payload": {"text": "ok"}})

    o.handle_chat_message = _turn
    from orchestrator import turn_guidance_authority as guidance

    parent = types.SimpleNamespace(origin=types.SimpleNamespace(owner_id="u1"))
    monkeypatch.setattr(guidance, "current_turn_guidance", lambda **kwargs: parent)
    monkeypatch.setattr(guidance, "inherit_turn_guidance", lambda *args, **kwargs: parent)
    monkeypatch.setattr(guidance, "use_turn_guidance", lambda *args, **kwargs: nullcontext())
    return o


SPECS = [{"title": f"t{i}", "instruction": f"do {i}"} for i in range(3)]


@pytest.mark.asyncio
async def test_exhausted_budget_yields_honest_partial_results(orch):
    orch._chain_budgets["c1"] = ChainBudget(turn_id="t", chat_id="c1",
                                            max_hops=2, wall_clock_s=999)
    resp = await subtasks.handle_meta_tool(
        orch, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=MagicMock())
    results = resp.result["subtasks"]
    ok = [r for r in results if r["status"] == "ok"]
    stopped = [r for r in results if r["status"] == "cancelled"]
    assert len(ok) == 2
    assert len(stopped) == 1
    assert "budget exhausted" in stopped[0]["detail"]
    assert orch._chain_budgets["c1"].spent_hops == 2


@pytest.mark.asyncio
async def test_wall_clock_exhaustion_stops_the_tree(orch):
    orch._chain_budgets["c1"] = ChainBudget(turn_id="t", chat_id="c1",
                                            max_hops=99, wall_clock_s=0.0)
    resp = await subtasks.handle_meta_tool(
        orch, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=MagicMock())
    results = resp.result["subtasks"]
    assert all(r["status"] == "cancelled" for r in results)
    assert all("wall_clock" in r["detail"] for r in results)


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
    from orchestrator.delegation import DEFAULT_MAX_DELEGATION_DEPTH

    b = ChainBudget(turn_id="t")
    assert b.max_depth == DEFAULT_MAX_DELEGATION_DEPTH
    assert b.charge(DEFAULT_MAX_DELEGATION_DEPTH) is None
    assert b.charge(DEFAULT_MAX_DELEGATION_DEPTH + 1) == "depth_exceeded"


def test_turn_start_resets_budget_in_the_real_orchestrator():
    import inspect

    wrapper = inspect.getsource(Orchestrator.handle_chat_message)
    publication = inspect.getsource(Orchestrator._handle_chat_message_with_guidance)
    implementation = inspect.getsource(Orchestrator._handle_chat_message_impl)
    assert "self._handle_chat_message_with_guidance(" in wrapper
    assert "self._handle_chat_message_impl(" in publication
    assert "_existing_budget = self._chain_budgets.get(chat_id)" in implementation
    assert "_existing_budget.parent is None" in implementation
    assert "self._chain_budgets.pop(chat_id, None)" in implementation
