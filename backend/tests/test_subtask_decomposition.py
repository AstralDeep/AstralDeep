"""Tests for orchestrator/subtasks.py's delegate_subtasks: 2-5 bounded, isolated
sub-tasks run concurrently in fresh contexts restricted to the parent's tools, charge
the turn's chain budget, and return provenance-tagged digests.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import subtasks  # noqa: E402
from orchestrator.chain_authority import ChainBudget  # noqa: E402
from shared.feature_flags import flags  # noqa: E402


@asynccontextmanager
async def _authenticated_operation(orch, websocket, *, owner_id="u1", state=None):
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.work_admission import (
        AdmissionClass, ExecutionFence, OperationOwner, OperationRecord,
        OperationState, OwnerScope,
    )

    orch.ui_sessions.setdefault(websocket, {"sub": owner_id})
    orch._llm_context_user_id = types.MethodType(
        Orchestrator._llm_context_user_id, orch)
    now = datetime.now(timezone.utc)
    operation_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    operation = OperationRecord(
        operation_id=operation_id, operation_kind="interactive_turn",
        admission_class=AdmissionClass.INTERACTIVE, owner_scope=OwnerScope.USER,
        owner_user_id=owner_id, connection_scope_id=None,
        idempotency_namespace=None, idempotency_key=None,
        normalized_input_digest=None, chat_id="c1", parent_operation_id=None,
        connection_generation=None, request_generation=None,
        state=state or OperationState.RUNNING, phase_code=None, terminal_code=None,
        safe_summary=None, retry_after_ms=None, execution_generation=1,
        execution_lease_token=lease_token, state_revision=1, accepted_at=now,
        updated_at=now, queue_deadline_at=None, started_at=now, terminal_at=None,
        cancel_requested_at=None, purge_after=None,
    )
    owner = OperationOwner(OwnerScope.USER, owner_id, None)
    fence = ExecutionFence(operation_id, 1, lease_token)
    token = _CONNECTION_OPERATION_CONTEXT.set({
        "owner": owner, "operation": operation, "execution_fence": fence,
    })
    try:
        yield operation
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch, user_skills_disabled):
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
    websocket = MagicMock()
    async with _authenticated_operation(orch, websocket):
        return await subtasks.handle_meta_tool(
            orch, "delegate_subtasks",
            {"subtasks": specs if specs is not None else SPECS, **args},
            user_id="u1", chat_id="c1", websocket=websocket)


@pytest.mark.asyncio
async def test_spawns_isolated_subtasks_and_returns_digests(orch):
    owner_ids = []

    async def _turn(vws, message, chat_id, **kw):
        owner_ids.append(vws.llm_context_user_id)
        orch.turns.append({"message": message, "chat_id": chat_id, **kw})
        await vws.send_json({"type": "chat_message",
                             "payload": {"text": f"result for: {message}",
                                         "agent_id": "web-research-1"}})

    orch.handle_chat_message = _turn
    resp = await _run(orch)
    results = resp.result["subtasks"]
    assert owner_ids == ["u1"] * 3
    assert len(results) == 3
    assert all(r["status"] == "ok" for r in results)
    chats = {t["chat_id"] for t in orch.turns}
    assert len(chats) == 3
    assert "c1" not in chats
    for r in results:
        assert r["digest"] and len(r["digest"]) <= subtasks.DIGEST_CAP
        assert r["agents"] == ["web-research-1"]
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
    async with _authenticated_operation(orch, ws):
        await subtasks.handle_meta_tool(
            orch, "delegate_subtasks", {"subtasks": SPECS},
            user_id="u1", chat_id="c1", websocket=ws)
    assert all(b.get("sub") == "u1" and b.get("_raw_token") == "session-token"
               for b in bound)
    assert list(orch.ui_sessions) == [ws]


@pytest.mark.asyncio
@pytest.mark.parametrize("specs", [
    [{"title": "only one", "instruction": "x"}],
    [{"title": f"t{i}", "instruction": "x"} for i in range(6)],
    "not-a-list",
])
async def test_fan_out_is_bounded(orch, specs):
    resp = await _run(orch, specs=specs)
    assert resp.error is not None
    assert not orch.turns


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
    assert results["Program A"]["status"] == "ok"


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
    assert orch._chain_budgets["c1"].spent_hops == 3


@pytest.mark.asyncio
async def test_subtask_delegation_rejects_missing_or_cross_owner_admission(orch):
    websocket = MagicMock()
    orch.ui_sessions[websocket] = {"sub": "u2"}
    from orchestrator.orchestrator import Orchestrator
    orch._llm_context_user_id = types.MethodType(
        Orchestrator._llm_context_user_id, orch)

    # A session identity without a live admission context is not sufficient.
    response = await subtasks.handle_meta_tool(
        orch, "delegate_subtasks", {"subtasks": SPECS},
        user_id="u1", chat_id="c1", websocket=websocket)
    assert response.error is not None
    assert not orch.turns


@pytest.mark.asyncio
async def test_subtask_delegation_rejects_cancelled_parent_operation(orch):
    from orchestrator.work_admission import OperationState
    websocket = MagicMock()
    async with _authenticated_operation(
            orch, websocket, state=OperationState.CANCELLED):
        response = await subtasks.handle_meta_tool(
            orch, "delegate_subtasks", {"subtasks": SPECS},
            user_id="u1", chat_id="c1", websocket=websocket)
    assert response.error is not None
    assert not orch.turns
