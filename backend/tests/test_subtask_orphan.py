"""T033 (056-delegated-agent-chaining): orphaned sub-tasks are cancelled,
audited, and their partial output DISCARDED (FR-023).

If the parent turn ends, its socket goes away, or the budget is exhausted,
in-flight sub-tasks must not keep running and their partial results must never
be silently attached to a later turn.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import audit.recorder as audit_recorder  # noqa: E402
from orchestrator import subtasks  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
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
def captured(monkeypatch):
    rec = MagicMock()
    rec.record = AsyncMock()
    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: rec)
    return rec


def _orch(turn):
    o = MagicMock()
    o.history.create_chat = MagicMock(side_effect=lambda user_id=None, **k: "sub-chat")
    o.ui_sessions = {}
    o._safe_send = AsyncMock()
    o._chain_budgets = {}
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)
    o.handle_chat_message = turn
    return o


SPECS = [{"title": "A", "instruction": "do A"}, {"title": "B", "instruction": "do B"}]


@pytest.mark.asyncio
async def test_parent_cancellation_cancels_subtasks(captured):
    started = asyncio.Event()
    finished = []

    async def _slow(vws, message, chat_id, **kw):
        started.set()
        try:
            await asyncio.sleep(30)  # would outlive the parent
            finished.append(message)
        finally:
            # Whatever it produced before cancellation must be discarded.
            await vws.send_json({"type": "chat_message",
                                 "payload": {"text": "partial work"}})

    o = _orch(_slow)
    parent = asyncio.create_task(subtasks.handle_meta_tool(
        o, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=MagicMock()))
    await asyncio.wait_for(started.wait(), timeout=2)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent

    assert not finished, "sub-tasks must not outlive their parent turn"
    rows = [c.args[0] for c in captured.record.await_args_list]
    kinds = {r.action_type for r in rows}
    assert "delegation.subtask.orphaned" in kinds
    assert "delegation.subtask.cancelled" in kinds
    # The orphan record is an interrupted outcome, not a success.
    orphan = next(r for r in rows if r.action_type == "delegation.subtask.orphaned")
    assert orphan.outcome == "interrupted"


@pytest.mark.asyncio
async def test_cancelled_subtask_partials_are_discarded(captured):
    """A cancelled sub-task's captured outputs are cleared, so nothing can be
    attached to a later turn."""
    seen_task = {}

    async def _slow(vws, message, chat_id, **kw):
        seen_task["task"] = vws.task
        await vws.send_json({"type": "chat_message",
                             "payload": {"text": "half an answer"}})
        await asyncio.sleep(30)

    o = _orch(_slow)
    parent = asyncio.create_task(subtasks.handle_meta_tool(
        o, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=MagicMock()))
    await asyncio.sleep(0.1)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    await asyncio.sleep(0.05)
    assert seen_task["task"].outputs == [], "partial output must be discarded"


@pytest.mark.asyncio
async def test_timeout_discards_partials_and_reports_honestly(captured, monkeypatch):
    monkeypatch.setattr(subtasks, "SUBTASK_TIMEOUT_S", 0.05)

    async def _slow(vws, message, chat_id, **kw):
        await vws.send_json({"type": "chat_message", "payload": {"text": "partial"}})
        await asyncio.sleep(5)

    o = _orch(_slow)
    resp = await subtasks.handle_meta_tool(
        o, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=MagicMock())
    results = resp.result["subtasks"]
    assert all(r["status"] == "timeout" for r in results)
    assert all(r["digest"] == "" for r in results)  # no partial leaked upstream
    rows = [c.args[0] for c in captured.record.await_args_list]
    assert [r for r in rows if r.action_type == "delegation.subtask.timeout"]


@pytest.mark.asyncio
async def test_subtask_socket_binding_is_always_released(captured):
    async def _boom(vws, message, chat_id, **kw):
        raise RuntimeError("nope")

    ws = MagicMock()
    o = _orch(_boom)
    o.ui_sessions[ws] = {"sub": "u1"}
    await subtasks.handle_meta_tool(
        o, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=ws)
    # Only the parent socket remains — no leaked virtual sockets.
    assert list(o.ui_sessions) == [ws]
