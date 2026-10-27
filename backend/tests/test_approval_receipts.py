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

@pytest.mark.asyncio
async def test_bypass_withdraws_only_owning_computer_reviews_before_delivery(monkeypatch):
    from orchestrator import hitl_confirmation as hitl, remote_confirmation as remote
    orch, ws, sent = runtime()
    host = SimpleNamespace(host_id="mac", owner_sub="owner")
    authority = SimpleNamespace(owner="owner", host=host, websocket=ws, chat="chat", controller_context=orch._connection_contexts[id(ws)], connection_generation="connection")
    other = SimpleNamespace(owner="owner", host=SimpleNamespace(host_id="other"), websocket=ws, chat="chat")
    call = SimpleNamespace(owner="owner", computer_authority=authority, request_id=REQUEST, websocket=ws, chat="chat", review_connection=orch._connection_contexts[id(ws)], review_connection_generation="connection")
    orch._hitl_pending_calls = {REQUEST: call, "noncomputer": SimpleNamespace(computer_authority=None)}
    orch._computer_approval_authorities = {"ab" * 16: (authority, 999999), "cd" * 16: (other, 999999)}
    finished = []
    async def card(*args, **kwargs):
        assert REQUEST not in orch._hitl_pending_calls
        assert "ab" * 16 not in orch._computer_approval_authorities
        if len(args) == 5:
            assert kwargs == {"notify": False}
        finished.append(args)
    monkeypatch.setattr(hitl, "_finish_card", card)
    monkeypatch.setattr(remote, "_replace_card", card)
    await receipts.withdraw_computer_reviews(orch, "owner", "mac")
    assert list(orch._hitl_pending_calls) == ["noncomputer"]
    assert list(orch._computer_approval_authorities) == ["cd" * 16]
    assert len(finished) == 2
    assert len(sent) == 2
    assert all(item["approval_decision"]["decision"] == "withdraw" and
               item["approval_decision"]["state"] == "cancelled" for item in sent)
    async def forbidden():
        pytest.fail("withdrawn request cannot be replayed")
    await receipts.run(orch, ws, "owner", {"hitl_request_id": REQUEST, "decision": "approve"},
                       "hitl_request_id", forbidden)
    assert sent[-1] == sent[0]


@pytest.mark.asyncio
async def test_bypass_withdrawal_preserves_inflight_canonical_receipt(monkeypatch):
    from orchestrator import hitl_confirmation as hitl
    orch, ws, sent = runtime()
    authority = SimpleNamespace(owner="owner", host=SimpleNamespace(host_id="mac"), websocket=ws, chat="chat", controller_context=orch._connection_contexts[id(ws)], connection_generation="connection")
    orch._hitl_pending_calls = {REQUEST: SimpleNamespace(computer_authority=authority, owner="owner",
        request_id=REQUEST, websocket=ws, chat="chat", review_connection=orch._connection_contexts[id(ws)], review_connection_generation="connection")}
    async def card(*args, **kwargs):
        pass
    monkeypatch.setattr(hitl, "_finish_card", card)
    entered, release = asyncio.Event(), asyncio.Event()
    async def accepted():
        receipts.note(orch, REQUEST, "accepted", "Accepted")
        entered.set()
        await release.wait()
    task = asyncio.create_task(receipts.run(orch, ws, "owner", {"hitl_request_id": REQUEST, "decision": "approve"},
                                           "hitl_request_id", accepted))
    await entered.wait()
    await receipts.withdraw_computer_reviews(orch, "owner", "mac")
    assert sent == []
    release.set()
    await task
    assert sent[0]["approval_decision"]["state"] == "accepted"


@pytest.mark.asyncio
async def test_withdrawal_does_not_rebind_old_card_to_new_connection_generation(monkeypatch):
    from orchestrator import hitl_confirmation as hitl
    orch, ws, sent = runtime()
    connection = orch._connection_contexts[id(ws)]
    authority = SimpleNamespace(owner="owner", host=SimpleNamespace(host_id="mac"), websocket=ws, chat="chat")
    orch._hitl_pending_calls = {REQUEST: SimpleNamespace(computer_authority=authority, owner="owner",
        request_id=REQUEST, websocket=ws, chat="chat", review_connection=connection,
        review_connection_generation="connection")}
    async def card(*args, **kwargs):
        pass
    monkeypatch.setattr(hitl, "_finish_card", card)
    connection.connection_generation = "new-connection"
    await receipts.withdraw_computer_reviews(orch, "owner", "mac")
    assert not orch._hitl_pending_calls and not sent
