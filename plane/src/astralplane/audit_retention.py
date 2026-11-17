"""Authenticated audit-chain retention anchors (HMAC-SHA256 by default) and atomic
prefix pruning, persisted through repositories/audit.py's AuditRetentionRepository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from astralplane.contracts import Record, Transaction
from astralplane.errors import PlaneError
from astralplane.repositories.audit import (
    GENESIS_DIGEST,
    AuditAuthenticator,
    AuditRepository,
    ChainVerification,
    verify_records,
)


class AnchorAuthenticator(Protocol):
    def sign(self, key_id: str, payload: bytes) -> bytes: ...

    def verify(self, key_id: str, payload: bytes, authentication: bytes) -> bool: ...


@dataclass(frozen=True, slots=True)
class HMACAnchorAuthenticator:
    key_resolver: Callable[[str], bytes]

    def sign(self, key_id: str, payload: bytes) -> bytes:
        key = bytes(self.key_resolver(key_id))
        if len(key) < 32:
            raise ValueError("audit anchor HMAC keys must contain at least 32 bytes")
        return hmac.new(key, payload, hashlib.sha256).digest()

    def verify(self, key_id: str, payload: bytes, authentication: bytes) -> bool:
        return hmac.compare_digest(self.sign(key_id, payload), authentication)


@dataclass(frozen=True, slots=True)
class AuditRetentionAnchor:
    anchor_id: str
    chain_id: str
    first_retained_sequence: int
    previous_entry_digest: bytes
    retention_policy_digest: bytes
    created_at: datetime
    key_id: str
    authentication: bytes

    def __post_init__(self) -> None:
        for name, value in (
            ("anchor_id", self.anchor_id),
            ("chain_id", self.chain_id),
            ("key_id", self.key_id),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"{name} must be a bounded non-empty string")
        if self.first_retained_sequence <= 1:
            raise ValueError("retention anchors begin after genesis")
        _digest("previous_entry_digest", self.previous_entry_digest)
        _digest("retention_policy_digest", self.retention_policy_digest)
        _aware("created_at", self.created_at)
        if not isinstance(self.authentication, bytes) or not self.authentication:
            raise ValueError("authentication must contain bytes")


@dataclass(frozen=True, slots=True)
class RetentionPruneResult:
    anchor: AuditRetentionAnchor
    deleted_events: int


class AuditRetentionError(PlaneError):
    default_code = "audit_retention_failed"


class AuditRetentionRepository:
    def load_anchor(
        self,
        transaction: Transaction,
        *,
        chain_id: str,
        first_retained_sequence: int,
    ) -> AuditRetentionAnchor | None:
        row = transaction.fetch_one(
            """
            SELECT * FROM audit_retention_anchor
            WHERE owner_or_chain = %s AND first_retained_sequence = %s
            """,
            (chain_id, first_retained_sequence),
        )
        return None if row is None else _anchor(row)

    def prune_prefix(
        self,
        transaction: Transaction,
        *,
        chain_id: str,
        first_retained_sequence: int,
        anchor_id: str,
        policy_digest: bytes,
        created_at: datetime,
        key_id: str,
        authenticator: AnchorAuthenticator,
    ) -> RetentionPruneResult:
        if first_retained_sequence <= 1:
            raise ValueError("first_retained_sequence must be greater than one")
        _digest("policy_digest", policy_digest)
        _aware("created_at", created_at)
        transaction.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtext(%s)) AS locked",
            (f"audit_events:{chain_id}",),
        )
        boundary = transaction.fetch_one(
            """
            SELECT chain_sequence, prev_hash FROM audit_events
            WHERE actor_user_id = %s AND chain_sequence = %s
            FOR UPDATE
            """,
            (chain_id, first_retained_sequence),
        )
        if boundary is None:
            raise AuditRetentionError(
                "the requested first retained audit event does not exist",
                code="audit_retention_boundary_missing",
                metadata={"chain_id": chain_id},
            )
        previous = transaction.fetch_one(
            """
            SELECT entry_hash FROM audit_events
            WHERE actor_user_id = %s AND chain_sequence = %s
            FOR UPDATE
            """,
            (chain_id, first_retained_sequence - 1),
        )
        if previous is None:
            existing = self.load_anchor(
                transaction,
                chain_id=chain_id,
                first_retained_sequence=first_retained_sequence,
            )
            if (
                existing is None
                or existing.previous_entry_digest != _bytes(boundary["prev_hash"])
                or existing.retention_policy_digest != policy_digest
                or not verify_anchor(existing, authenticator)
            ):
                raise AuditRetentionError(
                    "a pruned audit prefix has no valid matching anchor",
                    code="audit_retention_anchor_missing",
                    metadata={"chain_id": chain_id},
                )
            return RetentionPruneResult(anchor=existing, deleted_events=0)

        previous_digest = _bytes(previous["entry_hash"])
        if _bytes(boundary["prev_hash"]) != previous_digest:
            raise AuditRetentionError(
                "audit retention boundary does not join the preceding event",
                code="audit_retention_boundary_tampered",
                metadata={"chain_id": chain_id},
            )
        candidate = build_anchor(
            anchor_id=anchor_id,
            chain_id=chain_id,
            first_retained_sequence=first_retained_sequence,
            previous_entry_digest=previous_digest,
            retention_policy_digest=policy_digest,
            created_at=created_at,
            key_id=key_id,
            authenticator=authenticator,
        )
        inserted = transaction.fetch_one(
            """
            INSERT INTO audit_retention_anchor (
                anchor_id, owner_or_chain, first_retained_sequence,
                previous_entry_digest, retention_policy_digest, created_at,
                key_id, signature_or_mac
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (owner_or_chain, first_retained_sequence) DO NOTHING
            RETURNING *
            """,
            (
                candidate.anchor_id,
                candidate.chain_id,
                candidate.first_retained_sequence,
                candidate.previous_entry_digest,
                candidate.retention_policy_digest,
                candidate.created_at,
                candidate.key_id,
                candidate.authentication,
            ),
        )
        durable = (
            _anchor(inserted)
            if inserted is not None
            else self.load_anchor(
                transaction,
                chain_id=chain_id,
                first_retained_sequence=first_retained_sequence,
            )
        )
        if durable != candidate or durable is None or not verify_anchor(durable, authenticator):
            raise AuditRetentionError(
                "audit retention anchor identity conflicts with durable state",
                code="audit_retention_anchor_conflict",
                metadata={"chain_id": chain_id},
            )

        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        deleted = transaction.fetch_all(
            """
            DELETE FROM audit_events
            WHERE actor_user_id = %s AND chain_sequence < %s
            RETURNING event_id
            """,
            (chain_id, first_retained_sequence),
        )
        return RetentionPruneResult(anchor=durable, deleted_events=len(deleted))

    def verify_retained_chain(
        self,
        transaction: Transaction,
        *,
        chain_id: str,
        audit_repository: AuditRepository,
        authenticate_event: AuditAuthenticator,
        authenticate_anchor: AnchorAuthenticator,
    ) -> ChainVerification:
        records = audit_repository.load_chain(transaction, chain_id=chain_id)
        if not records:
            return ChainVerification(True, None, None, 0, GENESIS_DIGEST)
        first = records[0]
        if first.sequence == 1:
            return verify_records(
                records,
                chain_id=chain_id,
                authenticate=authenticate_event,
            )
        anchor = self.load_anchor(
            transaction,
            chain_id=chain_id,
            first_retained_sequence=first.sequence,
        )
        if anchor is None:
            return _invalid(first.event.event_id, "missing_retention_anchor", first.sequence)
        if not verify_anchor(anchor, authenticate_anchor):
            return _invalid(first.event.event_id, "invalid_retention_anchor", first.sequence)
        if (
            anchor.chain_id != chain_id
            or anchor.previous_entry_digest != first.previous_digest
            or anchor.first_retained_sequence != first.sequence
        ):
            return _invalid(first.event.event_id, "retention_anchor_mismatch", first.sequence)
        return verify_records(
            records,
            chain_id=chain_id,
            authenticate=authenticate_event,
            start_sequence=first.sequence,
            expected_previous_digest=anchor.previous_entry_digest,
        )


def build_anchor(
    *,
    anchor_id: str,
    chain_id: str,
    first_retained_sequence: int,
    previous_entry_digest: bytes,
    retention_policy_digest: bytes,
    created_at: datetime,
    key_id: str,
    authenticator: AnchorAuthenticator,
) -> AuditRetentionAnchor:
    unsigned = AuditRetentionAnchor(
        anchor_id=anchor_id,
        chain_id=chain_id,
        first_retained_sequence=first_retained_sequence,
        previous_entry_digest=previous_entry_digest,
        retention_policy_digest=retention_policy_digest,
        created_at=created_at,
        key_id=key_id,
        authentication=b"pending",
    )
    authentication = bytes(authenticator.sign(key_id, canonical_anchor_bytes(unsigned)))
    return AuditRetentionAnchor(
        anchor_id=anchor_id,
        chain_id=chain_id,
        first_retained_sequence=first_retained_sequence,
        previous_entry_digest=previous_entry_digest,
        retention_policy_digest=retention_policy_digest,
        created_at=created_at,
        key_id=key_id,
        authentication=authentication,
    )


def verify_anchor(anchor: AuditRetentionAnchor, authenticator: AnchorAuthenticator) -> bool:
    try:
        return bool(
            authenticator.verify(
                anchor.key_id,
                canonical_anchor_bytes(anchor),
                anchor.authentication,
            )
        )
    except Exception:
        return False


def canonical_anchor_bytes(anchor: AuditRetentionAnchor) -> bytes:
    value = {
        "type": "astral.audit-retention-anchor/v1",
        "anchor_id": anchor.anchor_id,
        "owner_or_chain": anchor.chain_id,
        "first_retained_sequence": anchor.first_retained_sequence,
        "previous_entry_digest": anchor.previous_entry_digest.hex(),
        "retention_policy_digest": anchor.retention_policy_digest.hex(),
        "created_at": anchor.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "key_id": anchor.key_id,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _anchor(row: Record) -> AuditRetentionAnchor:
    return AuditRetentionAnchor(
        anchor_id=str(row["anchor_id"]),
        chain_id=str(row["owner_or_chain"]),
        first_retained_sequence=int(row["first_retained_sequence"]),
        previous_entry_digest=_bytes(row["previous_entry_digest"]),
        retention_policy_digest=_bytes(row["retention_policy_digest"]),
        created_at=row["created_at"],
        key_id=str(row["key_id"]),
        authentication=_bytes_unbounded(row["signature_or_mac"]),
    )


def _invalid(event_id: str, reason: str, first_sequence: int) -> ChainVerification:
    return ChainVerification(
        valid=False,
        first_invalid_event_id=event_id,
        reason=reason,
        last_sequence=first_sequence - 1,
        last_digest=GENESIS_DIGEST,
    )


def _bytes(value: object) -> bytes:
    result = _bytes_unbounded(value)
    _digest("audit digest", result)
    return result


def _bytes_unbounded(value: object) -> bytes:
    try:
        return bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("audit authentication value is not byte-compatible") from exc


def _digest(name: str, value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must contain 32 bytes")


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = (
    "AnchorAuthenticator",
    "AuditRetentionAnchor",
    "AuditRetentionError",
    "AuditRetentionRepository",
    "HMACAnchorAuthenticator",
    "RetentionPruneResult",
    "build_anchor",
    "canonical_anchor_bytes",
    "verify_anchor",
)
