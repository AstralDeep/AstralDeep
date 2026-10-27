"""Checks connection-bound explicit decision receipts without replaying effects.
Concurrent duplicates and cancelled handlers retain one canonical, bounded outcome.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import approval_receipts as receipts

REQUEST = "12345678-1234-4234-9234-123456789abc"


def runtime():
    ws = object()
    context = SimpleNamespace(websocket=ws, registered=True, closing=False, connection_generation="connection")
    sent = []
    async def send(socket, raw):
        sent.append(json.loads(raw))
        return True
    return SimpleNamespace(ui_sessions={ws: {"sub": "owner", "_project_workspaces_v1": True}},
        _connection_contexts={id(ws): context}, _ws_active_chat={id(ws): "chat"}, _safe_send=send), ws, sent


@pytest.mark.asyncio
async def test_duplicate_and_conflicting_decisions_replay_first_receipt_without_effect():
    orch, ws, sent = runtime()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler():
        calls.append(1)
        entered.set()
        await release.wait()
        receipts.note(orch, REQUEST, "accepted", "Approval accepted.")
    payload = {"hitl_request_id": REQUEST, "decision": "approve"}
    first = asyncio.create_task(receipts.run(orch, ws, "owner", payload, "hitl_request_id", handler))
    await entered.wait()
    second = asyncio.create_task(receipts.run(orch, ws, "owner", {**payload, "decision": "decline"}, "hitl_request_id", handler))
    release.set()
    await asyncio.gather(first, second)
    assert calls == [1]
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[0]["approval_decision"]["state"] == "accepted"
    assert sent[0]["approval_decision"]["decision"] == "approve"
    assert sent[0]["chat_id"] == "chat" and sent[0]["connection_generation"] == "connection"


@pytest.mark.asyncio
async def test_refusal_is_sticky_and_stale_connection_never_receives_receipt():
    orch, ws, sent = runtime()
    async def refused():
        receipts.note(orch, REQUEST, "expired", "This confirmation expired.")
    payload = {"hitl_request_id": REQUEST, "decision": "approve"}
    await receipts.run(orch, ws, "owner", payload, "hitl_request_id", refused)
    async def forbidden():
        pytest.fail("a definitive refusal cannot replay an effect")
    await receipts.run(orch, ws, "owner", payload, "hitl_request_id", forbidden)
    assert all(frame["approval_decision"]["state"] == "expired" for frame in sent)
    async def replaced():
        orch._connection_contexts.pop(id(ws))
        receipts.note(orch, REQUEST, "accepted", "Accepted")
    receipts.drop_connection(orch, ws)
    await receipts.run(orch, ws, "owner", payload, "hitl_request_id", replaced)
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_cancelled_post_acceptance_handler_reports_uncertain_and_propagates_cancel():
    orch, ws, sent = runtime()
    async def cancelled():
        receipts.note(orch, REQUEST, "accepted", "Accepted")
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await receipts.run(orch, ws, "owner", {"hitl_request_id": REQUEST, "decision": "approve"}, "hitl_request_id", cancelled)
    assert sent[0]["approval_decision"]["state"] == "uncertain"


@pytest.mark.asyncio
async def test_legacy_handler_has_no_receipt_and_cleanup_retires_connection_entries():
    orch, ws, sent = runtime()
    orch.ui_sessions[ws]["_project_workspaces_v1"] = False
    called = []
    async def handler():
        called.append(True)
    await receipts.run(orch, ws, "owner", {}, "hitl_request_id", handler)
    assert called == [True] and sent == []
    receipts.drop_connection(orch, ws)

@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_failed_handler_preserves_acceptance_boundary(accepted, failure):
    orch, ws, sent = runtime()
    async def handler():
        if accepted:
            receipts.note(orch, REQUEST, "accepted", "Accepted")
        raise failure()
    with pytest.raises(failure):
        await receipts.run(orch, ws, "owner", {"hitl_request_id": REQUEST, "decision": "approve"}, "hitl_request_id", handler)
    expected = "uncertain" if accepted else "cancelled" if failure is asyncio.CancelledError else "error"
    assert sent[0]["approval_decision"]["state"] == expected


@pytest.mark.asyncio
async def test_receipts_bound_storage_and_expire_finished_entries(monkeypatch):
    orch, ws, sent = runtime()
    monkeypatch.setattr(receipts, "MAXIMUM", 1)
    now = [10.0]
    monkeypatch.setattr(receipts.time, "monotonic", lambda: now[0])
    calls = []
    async def handler():
        calls.append(True)
    payload = {"hitl_request_id": REQUEST, "decision": "approve"}
    await receipts.run(orch, ws, "owner", payload, "hitl_request_id", handler)
    another = {**payload, "hitl_request_id": "a" * 32}
    await receipts.run(orch, ws, "owner", another, "hitl_request_id", handler)
    assert sent[-1]["approval_decision"]["state"] == "error" and len(calls) == 1
    now[0] += receipts.TTL + 1
    await receipts.run(orch, ws, "owner", another, "hitl_request_id", handler)
    assert len(calls) == 2 and len(orch._approval_decision_receipts) == 1


@pytest.mark.asyncio
async def test_origin_chat_and_first_decision_survive_navigation():
    orch, ws, sent = runtime()
    async def handler():
        receipts.bind_origin(orch, REQUEST, "origin-chat")
        receipts.note(orch, REQUEST, "accepted", "Accepted")
    payload = {"hitl_request_id": REQUEST, "decision": "approve"}
    await receipts.run(orch, ws, "owner", payload, "hitl_request_id", handler)
    orch._ws_active_chat[id(ws)] = "another-chat"
    async def forbidden():
        pytest.fail("navigation must not replay a consumed decision")
    await receipts.run(orch, ws, "owner", payload, "hitl_request_id", forbidden)
    assert len(sent) == 2 and sent[0] == sent[1] and sent[0]["chat_id"] == "origin-chat"
