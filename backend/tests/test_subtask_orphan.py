"""Tests for orchestrator/subtasks.py orphan handling: sub-tasks are cancelled and
audited when the parent turn ends, disconnects, or exhausts its budget, and their
partial output is discarded rather than attached later.
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

import audit.recorder as audit_recorder  # noqa: E402
from orchestrator import subtasks  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from shared.feature_flags import flags  # noqa: E402


@asynccontextmanager
async def _authenticated_operation(orch, websocket, *, owner_id="u1"):
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
    from orchestrator.work_admission import (
        AdmissionClass, ExecutionFence, OperationOwner, OperationRecord,
        OperationState, OwnerScope,
    )

    orch.ui_sessions[websocket] = {"sub": owner_id}
    orch._llm_context_user_id = types.MethodType(
        Orchestrator._llm_context_user_id, orch)
    now = datetime.now(timezone.utc)
    operation_id, lease_token = uuid.uuid4(), uuid.uuid4()
    operation = OperationRecord(
        operation_id=operation_id, operation_kind="interactive_turn",
        admission_class=AdmissionClass.INTERACTIVE, owner_scope=OwnerScope.USER,
        owner_user_id=owner_id, connection_scope_id=None,
        idempotency_namespace=None, idempotency_key=None,
        normalized_input_digest=None, chat_id="c1", parent_operation_id=None,
        connection_generation=None, request_generation=None,
        state=OperationState.RUNNING, phase_code=None, terminal_code=None,
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
        yield
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch, user_skills_disabled):
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
            await asyncio.sleep(30)
            finished.append(message)
        finally:
            await vws.send_json({"type": "chat_message",
                                 "payload": {"text": "partial work"}})

    o = _orch(_slow)
    websocket = MagicMock()
    async with _authenticated_operation(o, websocket):
        parent = asyncio.create_task(subtasks.handle_meta_tool(
            o, "delegate_subtasks", {"subtasks": SPECS},
            user_id="u1", chat_id="c1", websocket=websocket))
    await asyncio.wait_for(started.wait(), timeout=2)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent

    assert not finished, "sub-tasks must not outlive their parent turn"
    rows = [c.args[0] for c in captured.record.await_args_list]
    kinds = {r.action_type for r in rows}
    assert "delegation.subtask.orphaned" in kinds
    assert "delegation.subtask.cancelled" in kinds
    orphan = next(r for r in rows if r.action_type == "delegation.subtask.orphaned")
    assert orphan.outcome == "interrupted"


@pytest.mark.asyncio
async def test_cancelled_subtask_partials_are_discarded(captured):
    seen_task = {}

    async def _slow(vws, message, chat_id, **kw):
        seen_task["task"] = vws.task
        await vws.send_json({"type": "chat_message",
                             "payload": {"text": "half an answer"}})
        await asyncio.sleep(30)

    o = _orch(_slow)
    websocket = MagicMock()
    async with _authenticated_operation(o, websocket):
        parent = asyncio.create_task(subtasks.handle_meta_tool(
            o, "delegate_subtasks", {"subtasks": SPECS},
            user_id="u1", chat_id="c1", websocket=websocket))
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
    websocket = MagicMock()
    async with _authenticated_operation(o, websocket):
        resp = await subtasks.handle_meta_tool(
            o, "delegate_subtasks", {"subtasks": SPECS},
            user_id="u1", chat_id="c1", websocket=websocket)
    results = resp.result["subtasks"]
    assert all(r["status"] == "timeout" for r in results)
    assert all(r["digest"] == "" for r in results)
    rows = [c.args[0] for c in captured.record.await_args_list]
    assert [r for r in rows if r.action_type == "delegation.subtask.timeout"]


@pytest.mark.asyncio
async def test_subtask_socket_binding_is_always_released(captured):
    async def _boom(vws, message, chat_id, **kw):
        raise RuntimeError("nope")

    ws = MagicMock()
    o = _orch(_boom)
    o.ui_sessions[ws] = {"sub": "u1"}
    async with _authenticated_operation(o, ws):
        await subtasks.handle_meta_tool(
            o, "delegate_subtasks", {"subtasks": SPECS},
            user_id="u1", chat_id="c1", websocket=ws)
    assert list(o.ui_sessions) == [ws]
