"""T031 (056-delegated-agent-chaining): planner decomposition into sub-tasks.

``delegate_subtasks`` spawns 2-5 bounded, isolated sub-tasks concurrently, each
in a fresh context, restricted to the tools the PARENT turn offered, holding a
slice of the turn's chain budget, and returning a bounded, provenance-tagged
digest — never a raw transcript (FR-020).
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import subtasks  # noqa: E402
from orchestrator.chain_authority import ChainBudget  # noqa: E402
from shared.feature_flags import flags  # noqa: E402


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch, user_skills_disabled):
    # ``user_skills_disabled``: feature 088 makes ``subtasks.handle_meta_tool``
    # require the parent turn's captured guidance origin, which these fake
    # orchestrators never hold, so every sub-task would be refused
    # ``guidance_read_unavailable`` before the decomposition seam under test
    # ran. The guidance inheritance itself is pinned over the real Plane by
    # ``test_skill_turn_handoffs_088.py``.
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)


@pytest.fixture
def orch():
    import itertools
    counter = itertools.count()
    o = MagicMock()
    o.history.create_chat = MagicMock(
        side_effect=lambda user_id=None, **k: f"sub-chat-{next(counter)}")
    o.ui_sessions = {}
    o._safe_send = AsyncMock()
    o._chain_budgets = {}
    o.turns = []

    async def _turn(vws, message, chat_id, **kw):
        o.turns.append({"message": message, "chat_id": chat_id, **kw})
        await vws.send_json({"type": "chat_message",
                             "payload": {"text": f"result for: {message}",
                                         "agent_id": "web-research-1"}})

    o.handle_chat_message = _turn

    from orchestrator.orchestrator import Orchestrator
    import types
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)
    return o


SPECS = [
    {"title": "Program A", "instruction": "audit program A"},
    {"title": "Program B", "instruction": "audit program B"},
    {"title": "Program C", "instruction": "audit program C"},
]


async def _run(orch, specs=None, **args):
    return await subtasks.handle_meta_tool(
        orch, "delegate_subtasks",
        {"subtasks": specs if specs is not None else SPECS, **args},
        user_id="u1", chat_id="c1", websocket=MagicMock())


@pytest.mark.asyncio
async def test_spawns_isolated_subtasks_and_returns_digests(orch):
    resp = await _run(orch)
    results = resp.result["subtasks"]
    assert len(results) == 3
    assert all(r["status"] == "ok" for r in results)
    # Each ran in its OWN fresh chat — never the parent's.
    chats = {t["chat_id"] for t in orch.turns}
    assert len(chats) == 3
    assert "c1" not in chats
    # Each digest is bounded and provenance-tagged with the agent that acted.
    for r in results:
        assert r["digest"] and len(r["digest"]) <= subtasks.DIGEST_CAP
        assert r["agents"] == ["web-research-1"]
    # The parent gets a synthesis instruction, not raw transcripts.
    assert "Synthesize" in resp.result["note"]


@pytest.mark.asyncio
async def test_subtasks_run_concurrently(orch):
    order = []

    async def _slow(vws, message, chat_id, **kw):
        order.append(("start", message))
        await asyncio.sleep(0.05)
        order.append(("end", message))
        await vws.send_json({"type": "chat_message", "payload": {"text": "ok"}})

    orch.handle_chat_message = _slow
    await _run(orch)
    # All three started before any finished ⇒ genuinely concurrent.
    assert [o[0] for o in order[:3]] == ["start", "start", "start"]


@pytest.mark.asyncio
async def test_subtask_tools_never_exceed_parent_tools(orch):
    await _run(orch, _parent_tools=["web_search", "summarize_text"])
    for t in orch.turns:
        assert t["selected_tools"] == ["web_search", "summarize_text"]


@pytest.mark.asyncio
async def test_subtask_inherits_parent_session_claims(orch):
    ws = MagicMock()
    orch.ui_sessions[ws] = {"sub": "u1", "_raw_token": "session-token"}
    bound = []

    async def _turn(vws, message, chat_id, **kw):
        bound.append(dict(orch.ui_sessions.get(vws) or {}))
        await vws.send_json({"type": "chat_message", "payload": {"text": "ok"}})

    orch.handle_chat_message = _turn
    await subtasks.handle_meta_tool(
        orch, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=ws)
    # Same human principal + authority: the sub-turn's dispatches can mint
    # delegated tokens exactly as the parent's do.
    assert all(b.get("sub") == "u1" and b.get("_raw_token") == "session-token"
               for b in bound)
    # ...and the binding is released when the sub-task ends (no socket leak).
    assert list(orch.ui_sessions) == [ws]


@pytest.mark.asyncio
@pytest.mark.parametrize("specs", [
    [{"title": "only one", "instruction": "x"}],                       # < MIN
    [{"title": f"t{i}", "instruction": "x"} for i in range(6)],        # > MAX
    "not-a-list",
])
async def test_fan_out_is_bounded(orch, specs):
    resp = await _run(orch, specs=specs)
    assert resp.error is not None
    assert not orch.turns  # nothing spawned


@pytest.mark.asyncio
async def test_failed_subtask_is_reported_honestly(orch):
    async def _boom(vws, message, chat_id, **kw):
        if "B" in message:
            raise RuntimeError("agent exploded")
        await vws.send_json({"type": "chat_message", "payload": {"text": "fine"}})

    orch.handle_chat_message = _boom
    resp = await _run(orch)
    results = {r["subtask"]: r for r in resp.result["subtasks"]}
    assert results["Program B"]["status"] == "failed"
    assert "exploded" in results["Program B"]["detail"]
    assert results["Program A"]["status"] == "ok"  # the others still deliver


@pytest.mark.asyncio
async def test_meta_tool_not_injected_when_flag_off(monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", False)
    assert subtasks.should_inject(None) is False


def test_meta_tool_not_injected_for_draft_self_tests():
    assert subtasks.should_inject("draft-1") is False


@pytest.mark.asyncio
async def test_budget_slices_debit_the_turn_budget(orch):
    orch._chain_budgets["c1"] = ChainBudget(turn_id="t", chat_id="c1",
                                            max_hops=10, wall_clock_s=999)
    await _run(orch)
    # Three sub-tasks charged the turn's global budget.
    assert orch._chain_budgets["c1"].spent_hops == 3
