"""Durable, owner-scoped records for externally governed physical effects:
ProtectedEffectOperation binds one effect intent, permit checkpoint, and redacted
outcome to a fixed enforcement scope profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from astralplane.domain import require_identifier, require_sha256, require_utc
from astralplane.errors import DomainValidationError


class AstralToolScope(StrEnum):
    READ = "tools:read"
    WRITE = "tools:write"
    SEARCH = "tools:search"
    SYSTEM = "tools:system"
    FILES = "tools:files"
    EXECUTE = "tools:execute"


class ProtectedEffectStatus(StrEnum):
    CREATED = "created"
    ASTRAL_AUTHORIZED = "astral_authorized"
    LETS_PENDING = "lets_pending"
    RECEIPT_RECEIVED = "receipt_received"
    RECEIPT_CLAIMED = "receipt_claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED_CLOSED = "failed_closed"
    EFFECT_FAILED = "effect_failed"
    OUTCOME_UNCERTAIN = "outcome_uncertain"

    @property
    def terminal(self) -> bool:
        return self in {
            ProtectedEffectStatus.SUCCEEDED,
            ProtectedEffectStatus.DENIED,
            ProtectedEffectStatus.FAILED_CLOSED,
            ProtectedEffectStatus.EFFECT_FAILED,
        }


_RECEIPT_REQUIRED = frozenset(
    {
        ProtectedEffectStatus.RECEIPT_RECEIVED,
        ProtectedEffectStatus.RECEIPT_CLAIMED,
        ProtectedEffectStatus.EXECUTING,
        ProtectedEffectStatus.SUCCEEDED,
        ProtectedEffectStatus.EFFECT_FAILED,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    }
)
_RECEIPT_FORBIDDEN = frozenset(
    {
        ProtectedEffectStatus.CREATED,
        ProtectedEffectStatus.ASTRAL_AUTHORIZED,
        ProtectedEffectStatus.LETS_PENDING,
    }
)
_ERROR_REQUIRED = frozenset(
    {
        ProtectedEffectStatus.DENIED,
        ProtectedEffectStatus.FAILED_CLOSED,
        ProtectedEffectStatus.EFFECT_FAILED,
        ProtectedEffectStatus.OUTCOME_UNCERTAIN,
    }
)
_OUTCOME_REQUIRED = frozenset(
    {
        ProtectedEffectStatus.SUCCEEDED,
        ProtectedEffectStatus.EFFECT_FAILED,
    }
)


def _non_negative(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise DomainValidationError(f"{field} must be a non-negative integer")
    return value


def _effect_nonce(value: object) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 256:
        raise DomainValidationError("nonce must contain 16 to 256 characters")
    if value != value.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise DomainValidationError("nonce must be a canonical printable identifier")
    return value


def _optional_identifier(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return require_identifier(value, field=field)  # type: ignore[arg-type]


def _optional_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return require_sha256(value, field=field)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProtectedEffectOperation:
    operation_id: str
    owner_id: str
    agent_id: str
    binding_id: str
    tool_id: str
    astral_scope: AstralToolScope
    lets_capability: str
    lets_transition: str
    executor_audience: str
    nonce: str
    effect_digest: str
    expected_sequence: int
    audit_correlation_id: str
    status: ProtectedEffectStatus
    receipt_id: str | None
    receipt_digest: str | None
    effect_result_digest: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        for field, value in (
            ("operation id", self.operation_id),
            ("owner id", self.owner_id),
            ("agent id", self.agent_id),
            ("binding id", self.binding_id),
            ("tool id", self.tool_id),
            ("LETS capability", self.lets_capability),
            ("LETS transition", self.lets_transition),
            ("executor audience", self.executor_audience),
            ("audit correlation id", self.audit_correlation_id),
        ):
            require_identifier(value, field=field)
        if not isinstance(self.astral_scope, AstralToolScope):
            raise DomainValidationError("astral scope must be a fixed Astral tool scope")
        if not isinstance(self.status, ProtectedEffectStatus):
            raise DomainValidationError("status must be a protected effect status")

        object.__setattr__(self, "nonce", _effect_nonce(self.nonce))
        require_sha256(self.effect_digest, field="effect digest")
        _non_negative(self.expected_sequence, field="expected sequence")
        _non_negative(self.version, field="version")

        receipt_id = _optional_identifier(self.receipt_id, field="receipt id")
        receipt_digest = _optional_digest(self.receipt_digest, field="receipt digest")
        effect_result_digest = _optional_digest(
            self.effect_result_digest,
            field="effect result digest",
        )
        error_code = _optional_identifier(self.error_code, field="error code")

        has_receipt_id = receipt_id is not None
        has_receipt_digest = receipt_digest is not None
        if has_receipt_id != has_receipt_digest:
            raise DomainValidationError("receipt id and receipt digest must be recorded together")
        if self.status in _RECEIPT_REQUIRED and not has_receipt_id:
            raise DomainValidationError("status requires validated receipt metadata")
        if self.status in _RECEIPT_FORBIDDEN and has_receipt_id:
            raise DomainValidationError("status cannot carry receipt metadata")

        if self.status in _ERROR_REQUIRED and error_code is None:
            raise DomainValidationError("denied or failed effect requires an error code")
        if self.status not in _ERROR_REQUIRED and error_code is not None:
            raise DomainValidationError("non-error status cannot carry an error code")

        if self.status in _OUTCOME_REQUIRED and effect_result_digest is None:
            raise DomainValidationError("known effect outcome requires a result digest")
        if self.status not in _OUTCOME_REQUIRED and effect_result_digest is not None:
            raise DomainValidationError(
                "status without a known effect outcome cannot carry a result digest"
            )

        created_at = require_utc(self.created_at, field="created at")
        updated_at = require_utc(self.updated_at, field="updated at")
        if updated_at < created_at:
            raise DomainValidationError("updated at cannot precede created at")

        for field, value in (
            ("receipt_id", receipt_id),
            ("receipt_digest", receipt_digest),
            ("effect_result_digest", effect_result_digest),
            ("error_code", error_code),
            ("created_at", created_at),
            ("updated_at", updated_at),
        ):
            object.__setattr__(self, field, value)

    @property
    def owner_operation_key(self) -> tuple[str, str]:
        return (self.owner_id, self.operation_id)

    @property
    def audit_key(self) -> tuple[str, str, str]:
        return (self.owner_id, self.audit_correlation_id, self.operation_id)


__all__ = (
    "AstralToolScope",
    "ProtectedEffectOperation",
    "ProtectedEffectStatus",
)
