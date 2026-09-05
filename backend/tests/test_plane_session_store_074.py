"""Focused Plane-boundary tests for durable web sessions and revocations."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from astralplane.repositories.history import SessionRecord
from astralplane.repositories.revocations import RevocationQueueRecord
from orchestrator.session_store import SessionStoreError, WebSessionStore


class _Context:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.transaction_count = 0

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        yield object()

    def call(self, operation, /, **kwargs):
        with self.transaction() as transaction:
            return operation(transaction, **kwargs)


class _Sessions:
    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}

    def put(self, _transaction, record: SessionRecord) -> SessionRecord:
        previous = self.records.get(record.session_id)
        if previous is not None and previous != record:
            raise AssertionError("test repository rejected changed replay")
        self.records[record.session_id] = record
        return record

    def get_by_session_id_for_administration(self, _query, *, session_id):
        return self.records.get(session_id)

    def get_latest_live_for_owner(self, _query, *, owner_id, observed_at):
        live = [
            record
            for record in self.records.values()
            if record.owner_id == owner_id and record.hard_expires_at > observed_at
        ]
        return max(live, key=lambda record: record.last_refresh_at, default=None)

    def compare_and_set_refresh(
        self,
        _transaction,
        record,
        *,
        expected_last_refresh_at,
    ):
        current = self.records[record.session_id]
        assert current.owner_id == record.owner_id
        assert current.last_refresh_at == expected_last_refresh_at
        self.records[record.session_id] = record
        return record

    def mark_resumed(
        self,
        _transaction,
        *,
        owner_id,
        session_id,
        expected_resumed,
        resumed,
    ):
        current = self.records[session_id]
        assert current.owner_id == owner_id
        assert current.resumed is expected_resumed
        updated = replace(current, resumed=resumed)
        self.records[session_id] = updated
        return updated

    def delete(self, _transaction, *, owner_id, session_id):
        current = self.records.get(session_id)
        if current is None or current.owner_id != owner_id:
            return False
        del self.records[session_id]
        return True

    def delete_and_return(self, transaction, *, owner_id, session_id):
        current = self.records.get(session_id)
        return current if self.delete(transaction, owner_id=owner_id, session_id=session_id) else None

    def delete_owner(self, _transaction, *, owner_id):
        keys = [key for key, value in self.records.items() if value.owner_id == owner_id]
        for key in keys:
            del self.records[key]
        return len(keys)

    def delete_expired_for_administration(self, _transaction, *, observed_at):
        keys = [
            key
            for key, value in self.records.items()
            if value.hard_expires_at <= observed_at
        ]
        for key in keys:
            del self.records[key]
        return len(keys)


class _Revocations:
    def __init__(self) -> None:
        self.records: dict[int, RevocationQueueRecord] = {}
        self.sequence = 0

    def enqueue(self, _transaction, **values):
        self.sequence += 1
        record = RevocationQueueRecord(
            queue_id=self.sequence,
            owner_id=values["owner_id"],
            refresh_token_ciphertext=values["refresh_token_ciphertext"],
            client_id=values["client_id"],
            enqueued_at=values["enqueued_at"],
            attempts=0,
        )
        self.records[record.queue_id] = record
        return record

    def pending_for_administration(self, _query, *, limit):
        return tuple(self.records[key] for key in sorted(self.records))[:limit]

    def resolve(self, _transaction, *, owner_id, queue_id):
        current = self.records.get(queue_id)
        if current is None or current.owner_id != owner_id:
            return False
        del self.records[queue_id]
        return True

    def bump_attempt(
        self,
        _transaction,
        *,
        owner_id,
        queue_id,
        expected_attempts,
    ):
        current = self.records[queue_id]
        assert current.owner_id == owner_id
        assert current.attempts == expected_attempts
        updated = replace(current, attempts=current.attempts + 1)
        self.records[queue_id] = updated
        return updated


def _store(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    sessions = _Sessions()
    revocations = _Revocations()
    store = WebSessionStore(
        session_context=_Context(sessions),
        revocation_context=_Context(revocations),
    )
    return store, sessions, revocations


def test_session_lifecycle_uses_typed_plane_contracts(monkeypatch) -> None:
    store, sessions, _revocations = _store(monkeypatch)

    created = store.create(
        "sid-a",
        user_id="alice",
        access_token="access-a",
        refresh_token="refresh-a",
        hard_max_seconds=3600,
    )
    durable = sessions.records["sid-a"]
    assert durable.access_token_ciphertext != "access-a"
    assert store.get("sid-a") == created
    assert store.latest_refresh_token_for("alice") == "refresh-a"

    anchor = created["interactive_anchor"]
    store.update_tokens(
        "sid-a",
        access_token="access-b",
        refresh_token="refresh-b",
    )
    refreshed = store.get("sid-a")
    assert refreshed is not None
    assert refreshed["access_token"] == "access-b"
    assert refreshed["interactive_anchor"] == anchor
    assert refreshed["last_refresh_at"] > created["last_refresh_at"]

    store.mark_resumed("sid-a")
    store.mark_resumed("sid-a")  # exact replay is harmless
    assert store.get("sid-a")["resumed"] is True
    assert store.delete("sid-a")["refresh_token"] == "refresh-b"
    assert store.delete("sid-a") is None


def test_owner_and_expiry_operations_remain_scoped(monkeypatch) -> None:
    store, sessions, _revocations = _store(monkeypatch)
    store.create(
        "sid-a",
        user_id="alice",
        access_token="a",
        refresh_token="ra",
        hard_max_seconds=3600,
    )
    store.create(
        "sid-b",
        user_id="alice",
        access_token="b",
        refresh_token="rb",
        hard_max_seconds=3600,
    )
    store.create(
        "sid-c",
        user_id="bob",
        access_token="c",
        refresh_token="rc",
        hard_max_seconds=3600,
    )
    assert store.delete_for_user("alice") == 2
    assert set(sessions.records) == {"sid-c"}

    sessions.records["sid-c"] = replace(sessions.records["sid-c"], hard_expires_at=0)
    store._cache.clear()
    assert store.get("sid-c") is None
    assert store.pop_death_reason("sid-c") == "hard_cap"
    assert store.purge_expired() == 0


def test_revocation_mutations_reuse_owner_and_attempt_fences(monkeypatch) -> None:
    store, _sessions, revocations = _store(monkeypatch)
    store.enqueue_revocation("alice", "refresh-a", client_id="web")
    store.enqueue_revocation("alice", "")

    pending = store.pending_revocations()
    assert len(pending) == 1
    assert pending[0]["refresh_token"] == "refresh-a"
    assert revocations.records[1].refresh_token_ciphertext != "refresh-a"

    store.bump_revocation_attempt(1)
    assert store.pending_revocations()[0]["attempts"] == 1
    store.resolve_revocation(1)
    assert store.pending_revocations() == []
    with pytest.raises(SessionStoreError, match="owner fence"):
        store.resolve_revocation(1)


def test_async_facade_keeps_database_work_off_event_loop(monkeypatch) -> None:
    store, _sessions, _revocations = _store(monkeypatch)

    async def exercise() -> None:
        await store.acreate(
            "sid-async",
            user_id="alice",
            access_token="a",
            refresh_token="r",
            hard_max_seconds=3600,
        )
        assert (await store.aget("sid-async"))["user_id"] == "alice"
        await store.aupdate_tokens(
            "sid-async",
            access_token="b",
            refresh_token="rr",
        )
        await store.amark_resumed("sid-async")
        await store.aenqueue_revocation("alice", "rr", "web")
        item = (await store.apending_revocations())[0]
        await store.abump_revocation_attempt(item["id"])
        await store.aresolve_revocation(item["id"])
        assert await store.adelete_for_user("alice") == 1
        assert await store.apurge_expired() == 0

    asyncio.run(exercise())


def test_store_requires_runtime_or_explicit_repository_contexts(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="initialized Plane runtime"):
        WebSessionStore()
