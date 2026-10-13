"""Checks stable audit identities through the public Plane-backed facade.
Identical retries preserve one authenticated row while conflicting and foreign
events cannot replace an owner's record.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from astralplane.errors import PlaneError
from orchestrator.context_usage import ContextUsage


def test_optional_identity_preserves_existing_fresh_insert_semantics(repo, make_event, unique_user):
    event = make_event(actor_user_id=unique_user, auth_principal=unique_user)
    assert event.event_id is None
    first, second = repo.insert(event), repo.insert(event)
    assert first.event_id != second.event_id


def test_stable_identity_replay_preserves_one_row_and_chain(repo, make_event, unique_user):
    identity = str(uuid4())
    event = make_event(actor_user_id=unique_user, auth_principal=unique_user, event_id=identity)
    first = repo.insert(event)
    assert first.event_id == identity and repo.insert(event) == first
    assert repo.list_for_user(unique_user)[0] == [first]
    assert repo.verify_chain(unique_user) is None


def test_stable_identity_conflicting_semantics_fail_closed(repo, make_event, unique_user):
    event = make_event(actor_user_id=unique_user, auth_principal=unique_user, event_id=str(uuid4()))
    first = repo.insert(event)
    conflicting = event.model_copy(update={"outputs_meta": {"charged": 2}})
    with pytest.raises(PlaneError) as caught:
        repo.insert(conflicting)
    assert caught.value.code == "audit_idempotency_conflict"
    assert repo.get_for_user(unique_user, first.event_id) == first
    assert len(repo.list_for_user(unique_user)[0]) == 1
    assert repo.verify_chain(unique_user) is None


def test_foreign_owner_cannot_read_or_replace_stable_event(repo, make_event, unique_user):
    event = make_event(actor_user_id=unique_user, auth_principal=unique_user, event_id=str(uuid4()))
    first = repo.insert(event)
    foreign_owner = unique_user + "-other"
    assert repo.get_for_user(foreign_owner, first.event_id) is None
    with pytest.raises(PlaneError) as caught:
        repo.insert(event.model_copy(update={"actor_user_id": foreign_owner, "auth_principal": foreign_owner}))
    assert caught.value.code == "audit_identity_unavailable"
    assert "duplicate key" not in str(caught.value) and first.event_id not in str(caught.value)
    assert repo.get_for_user(unique_user, first.event_id) == first
    assert repo.list_for_user(foreign_owner) == ([], None)


def test_transactional_stable_replay_uses_same_public_identity(repo, database, make_event, unique_user):
    event = make_event(actor_user_id=unique_user, auth_principal=unique_user, event_id=str(uuid4()))
    with database.transaction() as transaction:
        first = repo.insert_in_transaction(event, transaction=transaction, plane_runtime=database)
        assert repo.insert_in_transaction(event, transaction=transaction, plane_runtime=database) == first
    assert repo.insert(event) == first and repo.verify_chain(unique_user) is None


def test_stable_identity_replay_preserves_original_chain_key_after_rotation(repo, make_event, unique_user, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET_K1", "retained-prior-test-key")
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "k1")
    event = make_event(actor_user_id=unique_user, auth_principal=unique_user, event_id=str(uuid4()))
    first = repo.insert(event)
    monkeypatch.setenv("AUDIT_HMAC_SECRET_K2", "active-test-key")
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "k2")
    assert repo.insert(event) == first
    next_event = make_event(actor_user_id=unique_user, auth_principal=unique_user, event_id=str(uuid4()))
    assert repo.insert(next_event).event_id == next_event.event_id
    assert repo.verify_chain(unique_user) is None


async def test_cancelled_call_drains_durable_late_charge_before_recorder_shutdown(repo, unique_user):
    entered, finish = asyncio.Event(), asyncio.Event()
    closed = False

    async def record(event):
        assert not closed
        return await asyncio.to_thread(repo.insert, event)

    async def recover(owner, conversation):
        events, cursor = await asyncio.to_thread(repo.list_for_user, owner, limit=50, event_classes=["llm_call"])
        assert cursor is None
        return [event for event in events if event.conversation_id == conversation and event.action_type == "context.usage"]

    async def invoke():
        entered.set()
        await finish.wait()
        return {"usage": {"prompt_tokens": 8, "completion_tokens": 2, "cached_tokens": 0, "total_tokens": 10}}

    pricing = {"input_per_million": "2", "output_per_million": "4", "date": "2026-10-08", "currency": "USD"}
    ledger = ContextUsage(record, recover)
    task = asyncio.create_task(ledger.model_call(unique_user, "conversation", "summary", "model-a", invoke, pricing))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    await ledger.drain()
    total = await ledger.totals(unique_user, "conversation")
    closed = True
    restored = await ContextUsage(record, recover).totals(unique_user, "conversation")
    assert restored == total and restored["charged_cancelled"] == 1
    assert restored["known_cost_by_currency"] == {"USD": "0.000024"}
    assert restored["complete"] and repo.verify_chain(unique_user) is None


@pytest.mark.parametrize("value", ["", "not-a-uuid", 5, True, {}, "x" * 1024])
def test_untrusted_event_identity_is_rejected_before_plane(make_event, value):
    with pytest.raises(ValidationError):
        make_event(event_id=value)


def test_valid_uuid_identity_normalizes_to_one_canonical_string(make_event):
    identity = uuid4()
    assert make_event(event_id=str(identity).upper()).event_id == str(identity)
    assert make_event(event_id=UUID(str(identity))).event_id == str(identity)
