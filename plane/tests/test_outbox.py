"""Tests for src/astralplane/outbox.py: transactional enqueue, SKIP LOCKED claim
ordering, lease expiry/redelivery, and fenced ack/retry/dead-letter transitions.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.contracts import OutboxEntry
from astralplane.errors import PlaneError, SQLContractError
from astralplane.outbox import PostgresOutboxStore

NOW = datetime(2026, 8, 13, 20, tzinfo=UTC)


@dataclass(frozen=True)
class FakeResult:
    rowcount: int
    status_message: str | None = None
    returned_records: tuple[Mapping[str, Any], ...] = ()


class MemoryOutboxTransaction:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.override_result: FakeResult | None = None

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> FakeResult:
        self.statements.append(statement)
        if self.override_result is not None:
            result, self.override_result = self.override_result, None
            return result
        if statement.startswith("INSERT INTO astralplane_outbox"):
            entry_id, topic, payload, digest, idempotency_key, available_at = parameters
            if any(row["idempotency_key"] == idempotency_key for row in self.rows.values()):
                return FakeResult(0, "INSERT 0 0")
            self.rows[str(entry_id)] = {
                "entry_id": entry_id,
                "topic": topic,
                "canonical_payload": payload,
                "payload_sha256": digest,
                "idempotency_key": idempotency_key,
                "available_at": available_at,
                "status": "pending",
                "attempt_count": 0,
                "lease_owner": None,
                "lease_expires_at": None,
                "version": 0,
                "last_error_code": None,
                "updated_at": available_at,
            }
            return FakeResult(1, "INSERT 0 1", ({"entry_id": entry_id},))
        if "SET status = 'succeeded'" in statement:
            updated_at, entry_id, worker_id, version, now = parameters
            row = self.rows.get(str(entry_id))
            if not self._fence_matches(row, worker_id, version, now):
                return FakeResult(0, "UPDATE 0")
            row.update(
                status="succeeded",
                lease_owner=None,
                lease_expires_at=None,
                version=row["version"] + 1,
                last_error_code=None,
                updated_at=updated_at,
            )
            return FakeResult(1, "UPDATE 1")
        if "SET status = 'retry'" in statement and "WITH expired" not in statement:
            available_at, error_code, updated_at, entry_id, worker_id, version, now = parameters
            row = self.rows.get(str(entry_id))
            if not self._fence_matches(row, worker_id, version, now):
                return FakeResult(0, "UPDATE 0")
            row.update(
                status="retry",
                available_at=available_at,
                lease_owner=None,
                lease_expires_at=None,
                version=row["version"] + 1,
                last_error_code=error_code,
                updated_at=updated_at,
            )
            return FakeResult(1, "UPDATE 1")
        if "SET status = 'dead_letter'" in statement:
            error_code, updated_at, entry_id, worker_id, version, now = parameters
            row = self.rows.get(str(entry_id))
            if not self._fence_matches(row, worker_id, version, now):
                return FakeResult(0, "UPDATE 0")
            row.update(
                status="dead_letter",
                lease_owner=None,
                lease_expires_at=None,
                version=row["version"] + 1,
                last_error_code=error_code,
                updated_at=updated_at,
            )
            return FakeResult(1, "UPDATE 1")
        raise AssertionError(f"unexpected execute statement: {statement}")

    def fetch_one(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> Mapping[str, Any] | None:
        self.statements.append(statement)
        if "malformed claimed lease" not in statement and "lease_owner IS NULL" in statement:
            return next(
                (
                    {"entry_id": row["entry_id"]}
                    for row in self.rows.values()
                    if row["status"] == "claimed"
                    and (row["lease_owner"] is None or row["lease_expires_at"] is None)
                ),
                None,
            )
        assert "WHERE idempotency_key = %s" in statement
        return next(
            (
                copy.deepcopy(row)
                for row in self.rows.values()
                if row["idempotency_key"] == parameters[0]
            ),
            None,
        )

    def fetch_all(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> tuple[Mapping[str, Any], ...]:
        self.statements.append(statement)
        if statement.startswith("WITH candidates"):
            topics, now, limit, worker_id, lease_expires_at, updated_at = parameters
            candidates = sorted(
                (
                    row
                    for row in self.rows.values()
                    if row["status"] in {"pending", "retry"}
                    and row["topic"] in topics
                    and row["available_at"] <= now
                ),
                key=lambda row: (row["available_at"], row["entry_id"]),
            )[: int(limit)]
            records = []
            for row in candidates:
                row.update(
                    status="claimed",
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=row["attempt_count"] + 1,
                    version=row["version"] + 1,
                    last_error_code=None,
                    updated_at=updated_at,
                )
                records.append(copy.deepcopy(row))
            # PostgreSQL UPDATE RETURNING is unordered
            return tuple(reversed(records))
        if statement.startswith("WITH expired"):
            now, limit, available_at, updated_at = parameters
            candidates = sorted(
                (
                    row
                    for row in self.rows.values()
                    if row["status"] == "claimed" and row["lease_expires_at"] <= now
                ),
                key=lambda row: (row["lease_expires_at"], row["entry_id"]),
            )[: int(limit)]
            records = []
            for row in candidates:
                previous_worker = row["lease_owner"]
                row.update(
                    status="retry",
                    available_at=available_at,
                    lease_owner=None,
                    lease_expires_at=None,
                    version=row["version"] + 1,
                    last_error_code="lease_expired",
                    updated_at=updated_at,
                )
                records.append(
                    {
                        "entry_id": row["entry_id"],
                        "previous_worker_id": previous_worker,
                        "version": row["version"],
                        "available_at": row["available_at"],
                    }
                )
            return tuple(reversed(records))
        raise AssertionError(f"unexpected fetch_all statement: {statement}")

    @staticmethod
    def _fence_matches(
        row: dict[str, Any] | None,
        worker_id: object,
        version: object,
        now: object,
    ) -> bool:
        return bool(
            row
            and row["status"] == "claimed"
            and row["lease_owner"] == worker_id
            and row["version"] == version
            and isinstance(row["lease_expires_at"], datetime)
            and isinstance(now, datetime)
            and row["lease_expires_at"] > now
        )


class MemoryOutboxDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.transactions: list[MemoryOutboxTransaction] = []

    @contextmanager
    def transaction(self, **_: object) -> Iterator[MemoryOutboxTransaction]:
        working = copy.deepcopy(self.rows)
        transaction = MemoryOutboxTransaction(working)
        self.transactions.append(transaction)
        yield transaction
        self.rows = working


def entry(
    entry_id: str,
    *,
    topic: str = "audit.publish",
    available_at: datetime = NOW,
    payload: bytes | None = None,
    idempotency_key: str | None = None,
) -> OutboxEntry:
    exact_payload = payload or f'{{"entry":"{entry_id}"}}'.encode()
    return OutboxEntry(
        entry_id=entry_id,
        topic=topic,
        canonical_payload=exact_payload,
        payload_sha256=hashlib.sha256(exact_payload).hexdigest(),
        idempotency_key=idempotency_key or f"operation:{entry_id}",
        available_at=available_at,
    )


def test_enqueue_commits_or_rolls_back_with_the_authoritative_transaction() -> None:
    database = MemoryOutboxDatabase()
    store = PostgresOutboxStore()

    with database.transaction() as transaction:
        result = store.enqueue(transaction, entry("committed"))
    assert result.rowcount == 1
    assert set(database.rows) == {"committed"}

    with (
        pytest.raises(RuntimeError, match="authoritative write failed"),
        database.transaction() as transaction,
    ):
        store.enqueue(transaction, entry("rolled-back"))
        raise RuntimeError("authoritative write failed")
    assert set(database.rows) == {"committed"}


def test_idempotent_replay_is_a_noop_but_changed_semantics_fail_closed() -> None:
    store = PostgresOutboxStore()
    rows: dict[str, dict[str, Any]] = {}
    transaction = MemoryOutboxTransaction(rows)
    original = entry("one")

    assert store.enqueue(transaction, original).rowcount == 1
    assert store.enqueue(transaction, original).rowcount == 0
    with pytest.raises(PlaneError) as raised:
        store.enqueue(transaction, entry("two", idempotency_key=original.idempotency_key))
    assert raised.value.code == "outbox_idempotency_conflict"
    assert set(rows) == {"one"}


def test_claim_orders_available_work_and_uses_skip_locked_postgresql() -> None:
    store = PostgresOutboxStore()
    transaction = MemoryOutboxTransaction({})
    for item in (
        entry("later", available_at=NOW + timedelta(seconds=1)),
        entry("b"),
        entry("a"),
        entry("other", topic="purge.execute"),
    ):
        store.enqueue(transaction, item)

    claims = store.claim(
        transaction,
        worker_id="worker-1",
        topics=("audit.publish",),
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        limit=2,
    )
    assert [claim.entry.entry_id for claim in claims] == ["a", "b"]
    assert all(claim.worker_id == "worker-1" for claim in claims)
    assert all(claim.expected_version == 1 and claim.attempt == 1 for claim in claims)
    claim_statement = next(
        statement for statement in transaction.statements if "candidates" in statement
    )
    assert "FOR UPDATE SKIP LOCKED" in claim_statement
    assert "ORDER BY available_at, entry_id" in claim_statement


def test_worker_crash_requires_expiry_reclaim_before_redelivery() -> None:
    store = PostgresOutboxStore()
    transaction = MemoryOutboxTransaction({})
    store.enqueue(transaction, entry("crash"))
    first = store.claim(
        transaction,
        worker_id="worker-1",
        topics=("audit.publish",),
        now=NOW,
        lease_duration=timedelta(seconds=30),
        limit=1,
    )[0]
    assert not store.claim(
        transaction,
        worker_id="worker-2",
        topics=("audit.publish",),
        now=NOW + timedelta(seconds=29),
        lease_duration=timedelta(seconds=30),
        limit=1,
    )
    assert not store.reclaim_expired(transaction, now=NOW + timedelta(seconds=29), limit=1)

    reclaimed = store.reclaim_expired(transaction, now=NOW + timedelta(seconds=30), limit=1)
    assert reclaimed[0].entry_id == "crash"
    assert reclaimed[0].previous_worker_id == "worker-1"
    assert reclaimed[0].expected_version == first.expected_version + 1
    second = store.claim(
        transaction,
        worker_id="worker-2",
        topics=("audit.publish",),
        now=NOW + timedelta(seconds=30),
        lease_duration=timedelta(seconds=10),
        limit=1,
    )[0]
    assert second.worker_id == "worker-2"
    assert second.attempt == 2
    assert second.expected_version == 3


def test_ack_retry_and_dead_letter_are_worker_and_version_fenced() -> None:
    store = PostgresOutboxStore()
    transaction = MemoryOutboxTransaction({})
    for item_id in ("ack", "retry", "dead"):
        store.enqueue(transaction, entry(item_id))
    claims = {
        claim.entry.entry_id: claim
        for claim in store.claim(
            transaction,
            worker_id="worker-1",
            topics=("audit.publish",),
            now=NOW,
            lease_duration=timedelta(minutes=1),
            limit=3,
        )
    }

    claim = claims["ack"]
    with pytest.raises(PlaneError, match="version fence"):
        store.ack(
            transaction,
            entry_id="ack",
            worker_id="impostor",
            expected_version=claim.expected_version,
            now=NOW,
        )
    assert (
        store.ack(
            transaction,
            entry_id="ack",
            worker_id=claim.worker_id,
            expected_version=claim.expected_version,
            now=NOW,
        ).rowcount
        == 1
    )
    with pytest.raises(PlaneError):
        store.ack(
            transaction,
            entry_id="ack",
            worker_id=claim.worker_id,
            expected_version=claim.expected_version,
            now=NOW,
        )

    retry = claims["retry"]
    available_at = NOW + timedelta(minutes=5)
    store.retry(
        transaction,
        entry_id="retry",
        worker_id=retry.worker_id,
        expected_version=retry.expected_version,
        available_at=available_at,
        error_code="sink_unavailable",
        now=NOW,
    )
    assert transaction.rows["retry"]["status"] == "retry"
    assert transaction.rows["retry"]["available_at"] == available_at
    assert transaction.rows["retry"]["last_error_code"] == "sink_unavailable"

    dead = claims["dead"]
    store.dead_letter(
        transaction,
        entry_id="dead",
        worker_id=dead.worker_id,
        expected_version=dead.expected_version,
        error_code="attempts_exhausted",
        now=NOW,
    )
    assert transaction.rows["dead"]["status"] == "dead_letter"
    assert "secret details" not in repr(transaction.rows["dead"])


@pytest.mark.parametrize("operation", ["ack", "retry", "dead_letter"])
def test_settlement_rejects_an_expired_lease_at_the_exact_boundary(operation: str) -> None:
    store = PostgresOutboxStore()
    transaction = MemoryOutboxTransaction({})
    store.enqueue(transaction, entry(operation))
    claim = store.claim(
        transaction,
        worker_id="worker-1",
        topics=("audit.publish",),
        now=NOW,
        lease_duration=timedelta(seconds=30),
        limit=1,
    )[0]
    arguments: dict[str, object] = {
        "entry_id": operation,
        "worker_id": claim.worker_id,
        "expected_version": claim.expected_version,
        "now": claim.lease_expires_at,
    }
    if operation == "retry":
        arguments.update(
            available_at=claim.lease_expires_at + timedelta(seconds=1),
            error_code="retryable",
        )
    elif operation == "dead_letter":
        arguments["error_code"] = "terminal"

    with pytest.raises(PlaneError) as raised:
        getattr(store, operation)(transaction, **arguments)
    assert raised.value.code == "outbox_fence_conflict"
    assert transaction.rows[operation]["status"] == "claimed"


def test_reclaim_rejects_malformed_claimed_lease_before_mutation() -> None:
    store = PostgresOutboxStore()
    transaction = MemoryOutboxTransaction({})
    store.enqueue(transaction, entry("malformed"))
    store.claim(
        transaction,
        worker_id="worker-1",
        topics=("audit.publish",),
        now=NOW,
        lease_duration=timedelta(seconds=1),
        limit=1,
    )
    transaction.rows["malformed"]["lease_owner"] = None

    with pytest.raises(PlaneError) as raised:
        store.reclaim_expired(transaction, now=NOW + timedelta(seconds=1), limit=1)
    assert raised.value.code == "outbox_lease_invalid"
    assert transaction.rows["malformed"]["status"] == "claimed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("previous_worker_id", "bad worker"),
        ("version", 0),
        ("available_at", "later"),
    ],
)
def test_reclaim_rejects_corrupt_returned_records(field: str, value: object) -> None:
    class CorruptReclaimTransaction(MemoryOutboxTransaction):
        def fetch_all(
            self, statement: str, parameters: tuple[object, ...] = ()
        ) -> tuple[Mapping[str, Any], ...]:
            records = super().fetch_all(statement, parameters)
            if not statement.startswith("WITH expired"):
                return records
            corrupted = dict(records[0])
            corrupted[field] = value
            return (corrupted,)

    store = PostgresOutboxStore()
    transaction = CorruptReclaimTransaction({})
    store.enqueue(transaction, entry("corrupt-reclaim"))
    store.claim(
        transaction,
        worker_id="worker-1",
        topics=("audit.publish",),
        now=NOW,
        lease_duration=timedelta(seconds=1),
        limit=1,
    )

    with pytest.raises(PlaneError) as raised:
        store.reclaim_expired(transaction, now=NOW + timedelta(seconds=1), limit=1)
    assert raised.value.code == "outbox_record_invalid"


def test_invalid_entry_digest_is_rejected() -> None:
    original = entry("bad")
    invalid = OutboxEntry(
        entry_id=original.entry_id,
        topic=original.topic,
        canonical_payload=original.canonical_payload,
        payload_sha256="0" * 64,
        idempotency_key=original.idempotency_key,
        available_at=original.available_at,
    )
    with pytest.raises(SQLContractError, match="does not"):
        PostgresOutboxStore().enqueue(MemoryOutboxTransaction({}), invalid)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"worker_id": "bad worker"},
        {"topics": ()},
        {"topics": ("audit.publish", "audit.publish")},
        {"now": datetime(2026, 8, 13)},
        {"lease_duration": timedelta(0)},
        {"lease_duration": timedelta(days=2)},
        {"limit": 0},
        {"limit": True},
    ],
)
def test_claim_rejects_unbounded_or_ambiguous_inputs(kwargs: dict[str, object]) -> None:
    exact: dict[str, object] = {
        "worker_id": "worker-1",
        "topics": ("audit.publish",),
        "now": NOW,
        "lease_duration": timedelta(seconds=30),
        "limit": 1,
    }
    exact.update(kwargs)
    with pytest.raises(SQLContractError):
        PostgresOutboxStore().claim(MemoryOutboxTransaction({}), **exact)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "method_name, kwargs",
    [
        ("ack", {"expected_version": 0}),
        ("retry", {"error_code": "contains secret details"}),
        ("dead_letter", {"worker_id": "bad worker"}),
    ],
)
def test_fenced_transitions_validate_inputs(method_name: str, kwargs: dict[str, object]) -> None:
    store = PostgresOutboxStore()
    arguments: dict[str, object] = {
        "entry_id": "entry-1",
        "worker_id": "worker-1",
        "expected_version": 1,
        "now": NOW,
    }
    if method_name == "retry":
        arguments.update(available_at=NOW, error_code="retryable")
    elif method_name == "dead_letter":
        arguments["error_code"] = "failed"
    arguments.update(kwargs)
    with pytest.raises(SQLContractError):
        getattr(store, method_name)(MemoryOutboxTransaction({}), **arguments)


def test_invalid_database_records_and_row_counts_fail_closed() -> None:
    store = PostgresOutboxStore()
    transaction = MemoryOutboxTransaction({})
    transaction.override_result = FakeResult(-1)
    with pytest.raises(PlaneError) as raised:
        store.enqueue(transaction, entry("invalid-count"))
    assert raised.value.code == "outbox_write_invalid"

    transaction = MemoryOutboxTransaction({})
    store.enqueue(transaction, entry("invalid-record"))
    transaction.rows["invalid-record"]["payload_sha256"] = 7
    with pytest.raises(PlaneError) as raised:
        store.enqueue(transaction, entry("invalid-record"))
    assert raised.value.code == "outbox_record_invalid"


@pytest.mark.parametrize(
    "field, value",
    [
        ("canonical_payload", 7),
        ("version", "one"),
        ("lease_expires_at", "tomorrow"),
        ("lease_owner", "other-worker"),
    ],
)
def test_claim_rejects_corrupt_or_cross_worker_database_records(field: str, value: object) -> None:
    class CorruptClaimTransaction(MemoryOutboxTransaction):
        def fetch_all(
            self, statement: str, parameters: tuple[object, ...] = ()
        ) -> tuple[Mapping[str, Any], ...]:
            records = super().fetch_all(statement, parameters)
            corrupted = dict(records[0])
            corrupted[field] = value
            return (corrupted,)

    store = PostgresOutboxStore()
    transaction = CorruptClaimTransaction({})
    store.enqueue(transaction, entry("corrupt"))
    with pytest.raises(PlaneError) as raised:
        store.claim(
            transaction,
            worker_id="worker-1",
            topics=("audit.publish",),
            now=NOW,
            lease_duration=timedelta(seconds=30),
            limit=1,
        )
    assert raised.value.code == "outbox_record_invalid"


@pytest.mark.parametrize(
    "invalid",
    [
        "not-an-entry",
        OutboxEntry("empty", "audit.publish", b"", hashlib.sha256(b"").hexdigest(), "op", NOW),
        OutboxEntry("digest", "audit.publish", b"{}", "not-a-digest", "op", NOW),
    ],
)
def test_enqueue_rejects_invalid_entry_shapes(invalid: object) -> None:
    with pytest.raises(SQLContractError):
        PostgresOutboxStore().enqueue(MemoryOutboxTransaction({}), invalid)  # type: ignore[arg-type]
