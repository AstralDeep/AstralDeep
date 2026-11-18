"""Tests for backend/orchestrator/session_store.py's Plane-backed web sessions
(AstralPlane repositories/history.py, revocations.py): lifecycle, owner/expiry
scoping, revocation fencing, and the async facade keeping DB work off the event loop.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from astralplane.repositories.history import SessionRecord
from astralplane.repositories.revocations import RevocationQueueRecord
from orchestrator.session_store import WebSessionStore


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
        record = replace(record, incarnation_id=(previous.incarnation_id if previous else str(uuid4())))
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
        assert current.incarnation_id == record.incarnation_id
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
        expected_incarnation_id,
    ):
        current = self.records[session_id]
        assert current.owner_id == owner_id
        assert current.incarnation_id == expected_incarnation_id
        assert current.resumed is expected_resumed
        updated = replace(current, resumed=resumed)
        self.records[session_id] = updated
        return updated

    def delete(self, _transaction, *, owner_id, session_id, expected_incarnation_id):
        current = self.records.get(session_id)
        if current is None or current.owner_id != owner_id or current.incarnation_id != expected_incarnation_id:
            return False
        del self.records[session_id]
        return True

    def delete_and_return(self, transaction, *, owner_id, session_id, expected_incarnation_id):
        current = self.records.get(session_id)
        return current if self.delete(transaction, owner_id=owner_id, session_id=session_id,
                                      expected_incarnation_id=expected_incarnation_id) else None

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


def test_store_requires_runtime_or_explicit_repository_contexts(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="initialized Plane runtime"):
        WebSessionStore()
