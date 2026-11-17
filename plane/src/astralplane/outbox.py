"""PostgreSQL-backed transactional outbox storage: persists, leases, and fences delivery
work under caller-owned transactions. Topic registration and handler execution stay
with product code; used by astralplane.api and authority/repository.py.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Final

from astralplane.contracts import (
    ClaimedOutboxEntry,
    CommandResultContract,
    OutboxEntry,
    ReclaimedOutboxEntry,
    Record,
    Transaction,
)
from astralplane.errors import PlaneError, SQLContractError

_MAX_PAYLOAD_BYTES: Final = 1_048_576
_MAX_LEASE_DURATION: Final = timedelta(hours=24)
_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,254}$")
_SAFE_TOPIC: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_ERROR_CODE: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

_ENQUEUE_SQL: Final = """
INSERT INTO astralplane_outbox (
    entry_id,
    topic,
    canonical_payload,
    payload_sha256,
    idempotency_key,
    available_at,
    status,
    attempt_count,
    lease_owner,
    lease_expires_at,
    version,
    last_error_code
)
VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, NULL, NULL, 0, NULL)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING entry_id
""".strip()

_READ_IDEMPOTENT_SQL: Final = """
SELECT
    entry_id,
    topic,
    canonical_payload,
    payload_sha256,
    idempotency_key,
    available_at
FROM astralplane_outbox
WHERE idempotency_key = %s
""".strip()

_CLAIM_SQL: Final = """
WITH candidates AS (
    SELECT entry_id
    FROM astralplane_outbox
    WHERE status IN ('pending', 'retry')
      AND topic = ANY(%s)
      AND available_at <= %s
    ORDER BY available_at, entry_id
    FOR UPDATE SKIP LOCKED
    LIMIT %s
)
UPDATE astralplane_outbox AS outbox
SET status = 'claimed',
    lease_owner = %s,
    lease_expires_at = %s,
    attempt_count = outbox.attempt_count + 1,
    version = outbox.version + 1,
    last_error_code = NULL,
    updated_at = %s
FROM candidates
WHERE outbox.entry_id = candidates.entry_id
RETURNING
    outbox.entry_id,
    outbox.topic,
    outbox.canonical_payload,
    outbox.payload_sha256,
    outbox.idempotency_key,
    outbox.available_at,
    outbox.lease_owner,
    outbox.lease_expires_at,
    outbox.version,
    outbox.attempt_count
""".strip()

_ACK_SQL: Final = """
UPDATE astralplane_outbox
SET status = 'succeeded',
    lease_owner = NULL,
    lease_expires_at = NULL,
    version = version + 1,
    last_error_code = NULL,
    updated_at = %s
WHERE entry_id = %s
  AND status = 'claimed'
  AND lease_owner = %s
  AND version = %s
  AND lease_expires_at > %s
""".strip()

_RETRY_SQL: Final = """
UPDATE astralplane_outbox
SET status = 'retry',
    available_at = %s,
    lease_owner = NULL,
    lease_expires_at = NULL,
    version = version + 1,
    last_error_code = %s,
    updated_at = %s
WHERE entry_id = %s
  AND status = 'claimed'
  AND lease_owner = %s
  AND version = %s
  AND lease_expires_at > %s
""".strip()

_DEAD_LETTER_SQL: Final = """
UPDATE astralplane_outbox
SET status = 'dead_letter',
    lease_owner = NULL,
    lease_expires_at = NULL,
    version = version + 1,
    last_error_code = %s,
    updated_at = %s
WHERE entry_id = %s
  AND status = 'claimed'
  AND lease_owner = %s
  AND version = %s
  AND lease_expires_at > %s
""".strip()

_READ_MALFORMED_CLAIM_SQL: Final = """
SELECT entry_id
FROM astralplane_outbox
WHERE status = 'claimed'
  AND (lease_owner IS NULL OR lease_expires_at IS NULL)
ORDER BY entry_id
FOR UPDATE
LIMIT 1
""".strip()

_RECLAIM_SQL: Final = """
WITH expired AS (
    SELECT entry_id, lease_owner
    FROM astralplane_outbox
    WHERE status = 'claimed'
      AND lease_expires_at <= %s
    ORDER BY lease_expires_at, entry_id
    FOR UPDATE SKIP LOCKED
    LIMIT %s
)
UPDATE astralplane_outbox AS outbox
SET status = 'retry',
    available_at = %s,
    lease_owner = NULL,
    lease_expires_at = NULL,
    version = outbox.version + 1,
    last_error_code = 'lease_expired',
    updated_at = %s
FROM expired
WHERE outbox.entry_id = expired.entry_id
RETURNING
    outbox.entry_id,
    expired.lease_owner AS previous_worker_id,
    outbox.version,
    outbox.available_at
""".strip()


def _bounded_text(value: str, *, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SQLContractError(f"{name} is not a valid bounded identifier")
    return value


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SQLContractError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise SQLContractError("limit must be an integer between 1 and 1000")
    return limit


def _record_text(record: Record, field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise PlaneError(
            "outbox returned an invalid text field",
            code="outbox_record_invalid",
            metadata={"field": field},
        )
    return value


def _record_bytes(record: Record, field: str) -> bytes:
    value = record.get(field)
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise PlaneError(
            "outbox returned an invalid binary field",
            code="outbox_record_invalid",
            metadata={"field": field},
        )
    return bytes(value)


def _record_int(record: Record, field: str) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlaneError(
            "outbox returned an invalid integer field",
            code="outbox_record_invalid",
            metadata={"field": field},
        )
    return value


def _record_datetime(record: Record, field: str) -> datetime:
    value = record.get(field)
    try:
        return _utc(value, name=field)  # type: ignore[arg-type]
    except SQLContractError as exc:
        raise PlaneError(
            "outbox returned an invalid timestamp field",
            code="outbox_record_invalid",
            metadata={"field": field},
        ) from exc


def _record_bounded_text(
    record: Record,
    field: str,
    *,
    pattern: re.Pattern[str],
) -> str:
    try:
        return _bounded_text(_record_text(record, field), name=field, pattern=pattern)
    except SQLContractError as exc:
        raise PlaneError(
            "outbox returned an invalid bounded identifier",
            code="outbox_record_invalid",
            metadata={"field": field},
        ) from exc


def _validate_entry(entry: OutboxEntry) -> OutboxEntry:
    if not isinstance(entry, OutboxEntry):
        raise SQLContractError("entry must be an OutboxEntry")
    entry_id = _bounded_text(entry.entry_id, name="entry_id", pattern=_SAFE_ID)
    topic = _bounded_text(entry.topic, name="topic", pattern=_SAFE_TOPIC)
    idempotency_key = _bounded_text(
        entry.idempotency_key,
        name="idempotency_key",
        pattern=_SAFE_ID,
    )
    payload = bytes(entry.canonical_payload)
    if not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise SQLContractError("canonical_payload must contain between 1 and 1048576 bytes")
    payload_sha256 = entry.payload_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise SQLContractError("payload_sha256 must be a lowercase SHA-256 digest")
    if hashlib.sha256(payload).hexdigest() != payload_sha256:
        raise SQLContractError("canonical_payload does not match payload_sha256")
    return OutboxEntry(
        entry_id=entry_id,
        topic=topic,
        canonical_payload=payload,
        payload_sha256=payload_sha256,
        idempotency_key=idempotency_key,
        available_at=_utc(entry.available_at, name="available_at"),
    )


def _entry_from_record(record: Record) -> OutboxEntry:
    return _validate_entry(
        OutboxEntry(
            entry_id=_record_text(record, "entry_id"),
            topic=_record_text(record, "topic"),
            canonical_payload=_record_bytes(record, "canonical_payload"),
            payload_sha256=_record_text(record, "payload_sha256"),
            idempotency_key=_record_text(record, "idempotency_key"),
            available_at=_record_datetime(record, "available_at"),
        )
    )


def _require_single_update(
    result: CommandResultContract,
    *,
    operation: str,
    entry_id: str,
) -> CommandResultContract:
    if result.rowcount != 1:
        raise PlaneError(
            f"outbox {operation} version fence rejected the operation",
            code="outbox_fence_conflict",
            metadata={"entry_id": entry_id, "operation": operation},
        )
    return result


class PostgresOutboxStore:
    def enqueue(
        self,
        transaction: Transaction,
        entry: OutboxEntry,
    ) -> CommandResultContract:
        exact = _validate_entry(entry)
        result = transaction.execute(
            _ENQUEUE_SQL,
            (
                exact.entry_id,
                exact.topic,
                exact.canonical_payload,
                exact.payload_sha256,
                exact.idempotency_key,
                exact.available_at,
            ),
        )
        if result.rowcount == 1:
            return result
        if result.rowcount != 0:
            raise PlaneError(
                "outbox enqueue returned an invalid row count", code="outbox_write_invalid"
            )

        existing = transaction.fetch_one(_READ_IDEMPOTENT_SQL, (exact.idempotency_key,))
        if existing is None or _entry_from_record(existing) != exact:
            raise PlaneError(
                "outbox idempotency key already represents different work",
                code="outbox_idempotency_conflict",
                metadata={"entry_id": exact.entry_id},
            )
        return result

    def claim(
        self,
        transaction: Transaction,
        *,
        worker_id: str,
        topics: tuple[str, ...],
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedOutboxEntry, ...]:
        exact_worker_id = _bounded_text(worker_id, name="worker_id", pattern=_SAFE_ID)
        if not isinstance(topics, tuple) or not topics:
            raise SQLContractError("topics must be a non-empty tuple")
        exact_topics = tuple(
            _bounded_text(topic, name="topic", pattern=_SAFE_TOPIC) for topic in topics
        )
        if len(set(exact_topics)) != len(exact_topics):
            raise SQLContractError("topics must not contain duplicates")
        exact_now = _utc(now, name="now")
        if not isinstance(lease_duration, timedelta) or not (
            timedelta(0) < lease_duration <= _MAX_LEASE_DURATION
        ):
            raise SQLContractError("lease_duration must be positive and no greater than 24 hours")
        lease_expires_at = exact_now + lease_duration
        records = transaction.fetch_all(
            _CLAIM_SQL,
            (
                list(exact_topics),
                exact_now,
                _positive_limit(limit),
                exact_worker_id,
                lease_expires_at,
                exact_now,
            ),
        )
        claimed = tuple(
            ClaimedOutboxEntry(
                entry=_entry_from_record(record),
                worker_id=_record_text(record, "lease_owner"),
                lease_expires_at=_record_datetime(record, "lease_expires_at"),
                expected_version=_record_int(record, "version"),
                attempt=_record_int(record, "attempt_count"),
            )
            for record in records
        )
        if any(item.worker_id != exact_worker_id for item in claimed):
            raise PlaneError(
                "outbox claim returned a different worker", code="outbox_record_invalid"
            )
        return tuple(
            sorted(claimed, key=lambda item: (item.entry.available_at, item.entry.entry_id))
        )

    def ack(
        self,
        transaction: Transaction,
        *,
        entry_id: str,
        worker_id: str,
        expected_version: int,
        now: datetime,
    ) -> CommandResultContract:
        exact_entry_id, exact_worker_id, exact_version = self._fence_inputs(
            entry_id, worker_id, expected_version
        )
        exact_now = _utc(now, name="now")
        result = transaction.execute(
            _ACK_SQL,
            (exact_now, exact_entry_id, exact_worker_id, exact_version, exact_now),
        )
        return _require_single_update(result, operation="ack", entry_id=exact_entry_id)

    def retry(
        self,
        transaction: Transaction,
        *,
        entry_id: str,
        worker_id: str,
        expected_version: int,
        available_at: datetime,
        error_code: str,
        now: datetime,
    ) -> CommandResultContract:
        exact_entry_id, exact_worker_id, exact_version = self._fence_inputs(
            entry_id, worker_id, expected_version
        )
        exact_error = _bounded_text(error_code, name="error_code", pattern=_SAFE_ERROR_CODE)
        exact_now = _utc(now, name="now")
        result = transaction.execute(
            _RETRY_SQL,
            (
                _utc(available_at, name="available_at"),
                exact_error,
                exact_now,
                exact_entry_id,
                exact_worker_id,
                exact_version,
                exact_now,
            ),
        )
        return _require_single_update(result, operation="retry", entry_id=exact_entry_id)

    def dead_letter(
        self,
        transaction: Transaction,
        *,
        entry_id: str,
        worker_id: str,
        expected_version: int,
        error_code: str,
        now: datetime,
    ) -> CommandResultContract:
        exact_entry_id, exact_worker_id, exact_version = self._fence_inputs(
            entry_id, worker_id, expected_version
        )
        exact_error = _bounded_text(error_code, name="error_code", pattern=_SAFE_ERROR_CODE)
        exact_now = _utc(now, name="now")
        result = transaction.execute(
            _DEAD_LETTER_SQL,
            (
                exact_error,
                exact_now,
                exact_entry_id,
                exact_worker_id,
                exact_version,
                exact_now,
            ),
        )
        return _require_single_update(result, operation="dead-letter", entry_id=exact_entry_id)

    def reclaim_expired(
        self,
        transaction: Transaction,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ReclaimedOutboxEntry, ...]:
        exact_now = _utc(now, name="now")
        malformed = transaction.fetch_one(_READ_MALFORMED_CLAIM_SQL)
        if malformed is not None:
            raise PlaneError(
                "outbox contains a malformed claimed lease",
                code="outbox_lease_invalid",
            )
        records = transaction.fetch_all(
            _RECLAIM_SQL,
            (exact_now, _positive_limit(limit), exact_now, exact_now),
        )
        reclaimed_items: list[ReclaimedOutboxEntry] = []
        for record in records:
            entry_id = _record_bounded_text(record, "entry_id", pattern=_SAFE_ID)
            previous_worker_id = _record_bounded_text(
                record,
                "previous_worker_id",
                pattern=_SAFE_ID,
            )
            expected_version = _record_int(record, "version")
            if expected_version < 1:
                raise PlaneError(
                    "outbox returned an invalid reclaimed version",
                    code="outbox_record_invalid",
                    metadata={"field": "version"},
                )
            reclaimed_items.append(
                ReclaimedOutboxEntry(
                    entry_id=entry_id,
                    previous_worker_id=previous_worker_id,
                    expected_version=expected_version,
                    available_at=_record_datetime(record, "available_at"),
                )
            )
        reclaimed = tuple(reclaimed_items)
        return tuple(sorted(reclaimed, key=lambda item: (item.available_at, item.entry_id)))

    @staticmethod
    def _fence_inputs(entry_id: str, worker_id: str, expected_version: int) -> tuple[str, str, int]:
        exact_entry_id = _bounded_text(entry_id, name="entry_id", pattern=_SAFE_ID)
        exact_worker_id = _bounded_text(worker_id, name="worker_id", pattern=_SAFE_ID)
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise SQLContractError("expected_version must be a positive integer")
        return exact_entry_id, exact_worker_id, expected_version


__all__ = ("PostgresOutboxStore",)
