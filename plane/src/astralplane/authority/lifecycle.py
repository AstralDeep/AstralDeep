"""Durable, idempotent authority-lifecycle operation values recording one owner-scoped
remote mutation and its retry/reconciliation fences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from astralplane.domain import require_identifier, require_sha256, require_utc
from astralplane.errors import DomainValidationError


class AuthorityLifecycleKind(StrEnum):
    PROVISION = "provision"
    SPAWN = "spawn"
    RENEW = "renew"
    QUIESCE = "quiesce"
    RESUME = "resume"
    CLOSE = "close"
    REVOKE = "revoke"
    RECONCILE = "reconcile"


class AuthorityLifecycleStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    RECONCILED = "reconciled"

    @property
    def terminal(self) -> bool:
        return self in {
            AuthorityLifecycleStatus.SUCCEEDED,
            AuthorityLifecycleStatus.FAILED,
            AuthorityLifecycleStatus.RECONCILED,
        }


def _non_negative(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise DomainValidationError(f"{field} must be a non-negative integer")
    return value


def _optional_sequence(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _non_negative(value, field=field)


def _optional_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return require_sha256(value, field=field)  # type: ignore[arg-type]


def _optional_time(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    return require_utc(value, field=field)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class AuthorityLifecycleOperation:
    operation_id: str
    owner_id: str
    binding_id: str
    kind: AuthorityLifecycleKind
    expected_binding_version: int
    expected_lease_sequence: int | None
    request_fingerprint: str
    status: AuthorityLifecycleStatus
    remote_request_id: str
    result_digest: str | None
    error_code: str | None
    attempt_count: int
    next_attempt_at: datetime | None
    last_attempt_at: datetime | None
    reconciled_at: datetime | None
    reconciliation_digest: str | None
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        for field, value in (
            ("operation id", self.operation_id),
            ("owner id", self.owner_id),
            ("binding id", self.binding_id),
            ("remote request id", self.remote_request_id),
        ):
            require_identifier(value, field=field)
        if self.remote_request_id != self.operation_id:
            raise DomainValidationError("remote request id must equal operation id")
        if not isinstance(self.kind, AuthorityLifecycleKind):
            raise DomainValidationError("kind must be an authority lifecycle kind")
        if not isinstance(self.status, AuthorityLifecycleStatus):
            raise DomainValidationError("status must be an authority lifecycle status")

        _non_negative(self.expected_binding_version, field="expected binding version")
        _optional_sequence(self.expected_lease_sequence, field="expected lease sequence")
        require_sha256(self.request_fingerprint, field="request fingerprint")
        _non_negative(self.attempt_count, field="attempt count")
        _non_negative(self.version, field="version")

        result_digest = _optional_digest(self.result_digest, field="result digest")
        if self.error_code is not None:
            require_identifier(self.error_code, field="error code")
        if result_digest is not None and self.error_code is not None:
            raise DomainValidationError("result digest and error code are mutually exclusive")

        next_attempt_at = _optional_time(self.next_attempt_at, field="next attempt at")
        last_attempt_at = _optional_time(self.last_attempt_at, field="last attempt at")
        reconciled_at = _optional_time(self.reconciled_at, field="reconciled at")
        reconciliation_digest = _optional_digest(
            self.reconciliation_digest,
            field="reconciliation digest",
        )
        created_at = require_utc(self.created_at, field="created at")
        updated_at = require_utc(self.updated_at, field="updated at")
        if updated_at < created_at:
            raise DomainValidationError("updated at cannot precede created at")
        for field, value in (
            ("next attempt at", next_attempt_at),
            ("last attempt at", last_attempt_at),
            ("reconciled at", reconciled_at),
        ):
            if value is not None and value < created_at:
                raise DomainValidationError(f"{field} cannot precede created at")

        if self.attempt_count == 0 and last_attempt_at is not None:
            raise DomainValidationError("unattempted operation cannot have a last attempt")
        if self.attempt_count > 0 and last_attempt_at is None:
            raise DomainValidationError("attempted operation requires a last attempt")
        if self.status is not AuthorityLifecycleStatus.PENDING and self.attempt_count == 0:
            raise DomainValidationError("started operation must record an attempt")
        if self.status is AuthorityLifecycleStatus.SUCCEEDED and result_digest is None:
            raise DomainValidationError("succeeded operation requires a result digest")
        if (
            self.status
            in {
                AuthorityLifecycleStatus.FAILED,
                AuthorityLifecycleStatus.UNCERTAIN,
            }
            and self.error_code is None
        ):
            raise DomainValidationError("failed or uncertain operation requires an error code")
        if self.status is AuthorityLifecycleStatus.RECONCILED:
            if reconciled_at is None or reconciliation_digest is None:
                raise DomainValidationError("reconciled operation requires reconciliation metadata")
            if result_digest is None and self.error_code is None:
                raise DomainValidationError(
                    "reconciled operation requires a result or error outcome"
                )
        elif reconciled_at is not None or reconciliation_digest is not None:
            raise DomainValidationError(
                "only reconciled operation may carry reconciliation metadata"
            )
        if self.status.terminal and next_attempt_at is not None:
            raise DomainValidationError("terminal operation cannot schedule another attempt")

        for field, value in (
            ("result_digest", result_digest),
            ("next_attempt_at", next_attempt_at),
            ("last_attempt_at", last_attempt_at),
            ("reconciled_at", reconciled_at),
            ("reconciliation_digest", reconciliation_digest),
            ("created_at", created_at),
            ("updated_at", updated_at),
        ):
            object.__setattr__(self, field, value)

    @property
    def idempotency_key(self) -> tuple[str, str, str]:
        return (self.owner_id, self.operation_id, self.request_fingerprint)


__all__ = (
    "AuthorityLifecycleKind",
    "AuthorityLifecycleOperation",
    "AuthorityLifecycleStatus",
)
