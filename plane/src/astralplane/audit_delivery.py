"""Durable audit-sink delivery through the transactional outbox: a sink call happens
outside the database transaction, so a delivery is only reported once its fenced
outbox acknowledgement commits.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from astralplane.contracts import (
    ClaimedOutboxEntry,
    CommandResultContract,
    OutboxEntry,
    OutboxStore,
    PlaneDatabase,
    Transaction,
)
from astralplane.errors import PlaneError, SQLContractError
from astralplane.repositories import (
    CANONICAL_JSON_MAX_BYTES,
    CANONICAL_JSON_MAX_DEPTH,
    CANONICAL_JSON_MAX_NODES,
)

AUDIT_OUTBOX_TOPIC = "audit.publish"
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "set_cookie",
        "access_token",
    }
)
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_client_secret",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_access_token",
)


@runtime_checkable
class AuditSink(Protocol):
    def publish(
        self,
        *,
        event_id: str,
        canonical_payload: bytes,
        idempotency_key: str,
    ) -> None: ...


class AuditDeliveryState(StrEnum):
    NO_WORK = "no_work"
    DELIVERED = "delivered"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class AuditDeliveryResult:
    state: AuditDeliveryState
    entry_id: str | None
    attempt: int | None
    retry_available_at: datetime | None = None
    error_code: str | None = None


def _safe_json_value(value: object, *, path: str = "$") -> object:
    active: set[int] = set()
    nodes = 0

    def visit(item: object, path: str, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if depth > CANONICAL_JSON_MAX_DEPTH:
            raise SQLContractError(
                "audit payload exceeds canonical JSON structural bounds",
                metadata={"path": path[:256], "depth": depth},
            )
        if nodes > CANONICAL_JSON_MAX_NODES:
            raise SQLContractError(
                "audit payload exceeds canonical JSON structural bounds",
                metadata={"path": path[:256], "nodes": nodes},
            )
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise SQLContractError(
                    "audit payload contains a cyclic reference",
                    metadata={"path": path[:256]},
                )
            active.add(identity)
            try:
                sanitized: dict[str, object] = {}
                for key, nested in item.items():
                    if not isinstance(key, str) or not key:
                        raise SQLContractError("audit payload keys must be non-empty strings")
                    if not key.isascii():
                        try:
                            key.encode("utf-8")
                        except UnicodeEncodeError as exc:
                            raise SQLContractError(
                                "audit payload contains unpaired surrogate code points",
                                metadata={"path": f"{path}.{key}"[:256]},
                            ) from exc
                    normalized_key = key.casefold().replace("-", "_")
                    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(
                        _SENSITIVE_SUFFIXES
                    ):
                        raise SQLContractError(
                            "audit payload contains a credential-bearing field",
                            metadata={"path": f"{path}.{key}"},
                        )
                    sanitized[key] = visit(nested, f"{path}.{key}", depth + 1)
            finally:
                active.discard(identity)
            return sanitized
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray, memoryview)):
            identity = id(item)
            if identity in active:
                raise SQLContractError(
                    "audit payload contains a cyclic reference",
                    metadata={"path": path[:256]},
                )
            active.add(identity)
            try:
                return [visit(nested, f"{path}[]", depth + 1) for nested in item]
            finally:
                active.discard(identity)
        if isinstance(item, str) and not item.isascii():
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise SQLContractError(
                    "audit payload contains unpaired surrogate code points",
                    metadata={"path": path[:256]},
                ) from exc
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        raise SQLContractError(
            "audit payload contains a value that is not canonical-JSON compatible",
            metadata={"path": path},
        )

    return visit(value, path, 0)


def canonical_audit_payload(event: Mapping[str, object]) -> bytes:
    if not isinstance(event, Mapping):
        raise SQLContractError("audit event must be a mapping")
    sanitized = _safe_json_value(event)
    try:
        encoded = json.dumps(
            sanitized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise SQLContractError("audit payload is not canonical-JSON compatible") from exc
    if len(encoded) > CANONICAL_JSON_MAX_BYTES:
        raise SQLContractError(
            "audit payload exceeds the canonical JSON byte bound",
            metadata={"maximum": CANONICAL_JSON_MAX_BYTES},
        )
    return encoded


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SQLContractError("audit delivery timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _require_applied(
    result: CommandResultContract,
    *,
    entry_id: str,
    operation: str,
) -> None:
    if result.rowcount != 1:
        raise PlaneError(
            "audit delivery state transition was not durably applied",
            code="audit_delivery_fence_conflict",
            metadata={"entry_id": entry_id, "operation": operation},
        )


class AuditOutboxDelivery:
    def __init__(
        self,
        *,
        database: PlaneDatabase,
        outbox: OutboxStore,
        sink: AuditSink,
        max_attempts: int = 8,
        base_retry_delay: timedelta = timedelta(seconds=5),
        max_retry_delay: timedelta = timedelta(hours=1),
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise SQLContractError("max_attempts must be a positive integer")
        if not isinstance(base_retry_delay, timedelta) or base_retry_delay <= timedelta(0):
            raise SQLContractError("base_retry_delay must be positive")
        if not isinstance(max_retry_delay, timedelta) or max_retry_delay < base_retry_delay:
            raise SQLContractError("max_retry_delay must be at least base_retry_delay")
        if not isinstance(sink, AuditSink):
            raise SQLContractError("sink must implement AuditSink")
        self._database = database
        self._outbox = outbox
        self._sink = sink
        self._max_attempts = max_attempts
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay

    def enqueue(
        self,
        transaction: Transaction,
        *,
        event_id: str,
        event: Mapping[str, object],
        available_at: datetime,
    ) -> CommandResultContract:
        if not isinstance(event_id, str) or not event_id or len(event_id) > 220:
            raise SQLContractError("event_id must be a non-empty bounded string")
        if any(ord(character) < 33 for character in event_id):
            raise SQLContractError("event_id must not contain whitespace or control characters")
        payload = canonical_audit_payload(event)
        entry = OutboxEntry(
            entry_id=f"audit:{event_id}",
            topic=AUDIT_OUTBOX_TOPIC,
            canonical_payload=payload,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            idempotency_key=f"audit:{event_id}",
            available_at=_aware_utc(available_at),
        )
        return self._outbox.enqueue(transaction, entry)

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedOutboxEntry, ...]:
        exact_now = _aware_utc(now)
        with self._database.transaction() as transaction:
            claimed = self._outbox.claim(
                transaction,
                worker_id=worker_id,
                topics=(AUDIT_OUTBOX_TOPIC,),
                now=exact_now,
                lease_duration=lease_duration,
                limit=limit,
            )
            # Validate before commit or a bad payload loops forever
            for item in claimed:
                self._validate_claim(item)
        return claimed

    def deliver(self, claim: ClaimedOutboxEntry, *, now: datetime) -> AuditDeliveryResult:
        self._validate_claim(claim)
        exact_now = _aware_utc(now)
        if exact_now >= claim.lease_expires_at:
            raise PlaneError(
                "audit outbox lease has expired",
                code="audit_lease_expired",
                metadata={"entry_id": claim.entry.entry_id},
            )
        try:
            self._sink.publish(
                event_id=claim.entry.entry_id.removeprefix("audit:"),
                canonical_payload=claim.entry.canonical_payload,
                idempotency_key=claim.entry.idempotency_key,
            )
        except Exception:
            return self._settle_failure(claim, now=exact_now)

        with self._database.transaction() as transaction:
            result = self._outbox.ack(
                transaction,
                entry_id=claim.entry.entry_id,
                worker_id=claim.worker_id,
                expected_version=claim.expected_version,
                now=exact_now,
            )
            _require_applied(result, entry_id=claim.entry.entry_id, operation="ack")
        return AuditDeliveryResult(
            state=AuditDeliveryState.DELIVERED,
            entry_id=claim.entry.entry_id,
            attempt=claim.attempt,
        )

    def deliver_one(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AuditDeliveryResult:
        claims = self.claim(
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
            limit=1,
        )
        if not claims:
            return AuditDeliveryResult(
                state=AuditDeliveryState.NO_WORK,
                entry_id=None,
                attempt=None,
            )
        return self.deliver(claims[0], now=now)

    def _settle_failure(
        self,
        claim: ClaimedOutboxEntry,
        *,
        now: datetime,
    ) -> AuditDeliveryResult:
        error_code = "audit_sink_unavailable"
        if claim.attempt >= self._max_attempts:
            with self._database.transaction() as transaction:
                result = self._outbox.dead_letter(
                    transaction,
                    entry_id=claim.entry.entry_id,
                    worker_id=claim.worker_id,
                    expected_version=claim.expected_version,
                    error_code=error_code,
                    now=now,
                )
                _require_applied(
                    result,
                    entry_id=claim.entry.entry_id,
                    operation="dead-letter",
                )
            return AuditDeliveryResult(
                state=AuditDeliveryState.DEAD_LETTERED,
                entry_id=claim.entry.entry_id,
                attempt=claim.attempt,
                error_code=error_code,
            )

        exponent = min(claim.attempt - 1, 30)
        multiplier = 2**exponent
        retry_delay = (
            self._max_retry_delay
            if self._base_retry_delay > self._max_retry_delay // multiplier
            else self._base_retry_delay * multiplier
        )
        if retry_delay > datetime.max.replace(tzinfo=UTC) - now:
            raise PlaneError(
                "audit retry time exceeds the supported datetime range",
                code="audit_retry_out_of_range",
                metadata={"entry_id": claim.entry.entry_id},
            )
        retry_available_at = now + retry_delay
        with self._database.transaction() as transaction:
            result = self._outbox.retry(
                transaction,
                entry_id=claim.entry.entry_id,
                worker_id=claim.worker_id,
                expected_version=claim.expected_version,
                available_at=retry_available_at,
                error_code=error_code,
                now=now,
            )
            _require_applied(result, entry_id=claim.entry.entry_id, operation="retry")
        return AuditDeliveryResult(
            state=AuditDeliveryState.RETRY_SCHEDULED,
            entry_id=claim.entry.entry_id,
            attempt=claim.attempt,
            retry_available_at=retry_available_at,
            error_code=error_code,
        )

    @staticmethod
    def _validate_claim(claim: ClaimedOutboxEntry) -> None:
        if not isinstance(claim, ClaimedOutboxEntry) or claim.entry.topic != AUDIT_OUTBOX_TOPIC:
            raise SQLContractError("claim is not an audit outbox entry")
        _aware_utc(claim.lease_expires_at)
        if (
            isinstance(claim.expected_version, bool)
            or not isinstance(claim.expected_version, int)
            or claim.expected_version < 1
            or isinstance(claim.attempt, bool)
            or not isinstance(claim.attempt, int)
            or claim.attempt < 1
        ):
            raise PlaneError("audit outbox lease metadata is invalid", code="audit_claim_invalid")
        if hashlib.sha256(claim.entry.canonical_payload).hexdigest() != claim.entry.payload_sha256:
            raise PlaneError("audit outbox payload digest mismatch", code="audit_payload_corrupt")
        try:
            decoded = json.loads(claim.entry.canonical_payload)
            canonical = canonical_audit_payload(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, SQLContractError) as exc:
            raise PlaneError(
                "audit outbox payload is not valid canonical JSON",
                code="audit_payload_corrupt",
            ) from exc
        if canonical != claim.entry.canonical_payload:
            raise PlaneError("audit outbox payload is not canonical", code="audit_payload_corrupt")


__all__ = (
    "AUDIT_OUTBOX_TOPIC",
    "AuditDeliveryResult",
    "AuditDeliveryState",
    "AuditOutboxDelivery",
    "AuditSink",
    "canonical_audit_payload",
)
