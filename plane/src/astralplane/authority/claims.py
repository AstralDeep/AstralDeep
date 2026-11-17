"""Host-neutral receipt-claim and rollback-anchor values for external authority:
AstralPlane persists evidence, uniqueness keys, and the sequence fence, while
signature and policy decisions stay with the composition host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from astralplane.domain import require_identifier, require_sha256, require_utc
from astralplane.errors import DomainValidationError

EXECUTOR_ANCHOR_FORMAT = "LETS-EXECUTOR-AUTHORITY-ANCHOR/1"

_LETS_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SIGNED_64 = (1 << 63) - 1
_ZERO_SHA256 = "0" * 64


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > _MAX_SIGNED_64:
        raise DomainValidationError(
            f"{field} must be an integer from {minimum} through signed 64-bit maximum"
        )
    return value


def _lets_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _LETS_DIGEST.fullmatch(value) is None:
        raise DomainValidationError(f"{field} must be a canonical LETS SHA-256 digest")
    return value


def _optional_lets_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _lets_digest(value, field=field)


@dataclass(frozen=True, slots=True)
class ExternalAuthorityAnchorMetadata:
    anchor_format: str
    audience: str
    tenant_id: str
    envelope_id: str
    config_epoch: int
    executor_policy_sha256: str
    trust_registry_sha256: str
    schema_version: int
    database_instance_id: str
    claim_sequence: int
    claim_digest: str
    clock_floor_ns: int | None
    confirmed_at: datetime

    def __post_init__(self) -> None:
        if self.anchor_format != EXECUTOR_ANCHOR_FORMAT:
            raise DomainValidationError("unsupported external authority anchor format")
        for field, value in (
            ("anchor audience", self.audience),
            ("anchor tenant id", self.tenant_id),
            ("anchor envelope id", self.envelope_id),
        ):
            require_identifier(value, field=field)

        _integer(self.config_epoch, field="anchor config epoch", minimum=1)
        require_sha256(
            self.executor_policy_sha256,
            field="anchor executor policy digest",
        )
        require_sha256(
            self.trust_registry_sha256,
            field="anchor trust registry digest",
        )
        _integer(self.schema_version, field="anchor schema version", minimum=1)
        require_sha256(
            self.database_instance_id,
            field="anchor database instance id",
        )
        _integer(self.claim_sequence, field="anchor claim sequence")
        require_sha256(self.claim_digest, field="anchor claim digest")
        if self.claim_sequence == 0 and self.claim_digest != _ZERO_SHA256:
            raise DomainValidationError("an empty anchor claim chain must use the zero digest")
        if self.clock_floor_ns is not None:
            _integer(self.clock_floor_ns, field="anchor clock floor")
        object.__setattr__(
            self,
            "confirmed_at",
            require_utc(self.confirmed_at, field="anchor confirmed at"),
        )

    @property
    def stable_identity(self) -> tuple[str, str, str, int, str, str, int, str]:
        return (
            self.audience,
            self.tenant_id,
            self.envelope_id,
            self.config_epoch,
            self.executor_policy_sha256,
            self.trust_registry_sha256,
            self.schema_version,
            self.database_instance_id,
        )

    @property
    def head(self) -> tuple[int, str]:
        return (self.claim_sequence, self.claim_digest)


@dataclass(frozen=True, slots=True)
class ReceiptSequenceWatermark:
    warden_id: str
    lease_id: str
    audience: str
    last_sequence: int
    updated_at: datetime
    expires_at_ns: int
    version: int

    def __post_init__(self) -> None:
        for field, value in (
            ("watermark warden id", self.warden_id),
            ("watermark lease id", self.lease_id),
            ("watermark audience", self.audience),
        ):
            require_identifier(value, field=field)
        _integer(self.last_sequence, field="watermark sequence", minimum=1)
        _integer(self.expires_at_ns, field="watermark expiry", minimum=1)
        _integer(self.version, field="watermark version")
        object.__setattr__(
            self,
            "updated_at",
            require_utc(self.updated_at, field="watermark updated at"),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.warden_id, self.lease_id, self.audience)

    def require_advance(self, *, key: tuple[str, str, str], sequence: int) -> None:
        if key != self.key:
            raise DomainValidationError("receipt does not match the sequence watermark")
        candidate = _integer(sequence, field="receipt resulting sequence", minimum=1)
        if candidate <= self.last_sequence:
            raise DomainValidationError("receipt sequence must strictly advance the watermark")


@dataclass(frozen=True, slots=True)
class ReceiptClaim:
    receipt_id: str
    operation_id: str
    owner_id: str
    binding_id: str
    tenant_id: str
    envelope_id: str
    warden_id: str
    lease_id: str
    subject_id: str
    lineage_id: str
    policy_digest: str
    machine_digest: str
    config_epoch: int
    audience: str
    transition: str
    nonce: str
    resulting_sequence: int
    evidence_digest: str | None
    issued_at_ns: int
    expires_at_ns: int
    claimed_at: datetime
    canonical_digest: str
    authority_anchor: ExternalAuthorityAnchorMetadata

    def __post_init__(self) -> None:
        for field, value in (
            ("receipt id", self.receipt_id),
            ("operation id", self.operation_id),
            ("owner id", self.owner_id),
            ("binding id", self.binding_id),
            ("tenant id", self.tenant_id),
            ("envelope id", self.envelope_id),
            ("warden id", self.warden_id),
            ("lease id", self.lease_id),
            ("subject id", self.subject_id),
            ("lineage id", self.lineage_id),
            ("audience", self.audience),
            ("transition", self.transition),
            ("nonce", self.nonce),
        ):
            require_identifier(value, field=field)

        _lets_digest(self.policy_digest, field="policy digest")
        _lets_digest(self.machine_digest, field="machine digest")
        object.__setattr__(
            self,
            "evidence_digest",
            _optional_lets_digest(self.evidence_digest, field="evidence digest"),
        )
        _integer(self.config_epoch, field="config epoch", minimum=1)
        _integer(self.resulting_sequence, field="resulting sequence", minimum=1)
        issued_at_ns = _integer(self.issued_at_ns, field="issued at")
        expires_at_ns = _integer(self.expires_at_ns, field="expires at", minimum=1)
        if expires_at_ns <= issued_at_ns:
            raise DomainValidationError("receipt expiry must follow issuance")
        claimed_at = require_utc(self.claimed_at, field="claimed at")
        require_sha256(self.canonical_digest, field="canonical receipt digest")

        anchor = self.authority_anchor
        if not isinstance(anchor, ExternalAuthorityAnchorMetadata):
            raise DomainValidationError("authority anchor metadata is required")
        expected_anchor_identity = (
            self.audience,
            self.tenant_id,
            self.envelope_id,
            self.config_epoch,
        )
        actual_anchor_identity = (
            anchor.audience,
            anchor.tenant_id,
            anchor.envelope_id,
            anchor.config_epoch,
        )
        if actual_anchor_identity != expected_anchor_identity:
            raise DomainValidationError("authority anchor identity does not match the receipt")
        if anchor.claim_sequence < 1:
            raise DomainValidationError("claimed receipt requires a nonempty authority anchor head")
        if anchor.clock_floor_ns is None:
            raise DomainValidationError("claimed receipt requires an anchored clock floor")
        if anchor.confirmed_at < claimed_at:
            raise DomainValidationError("authority confirmation cannot precede the receipt claim")

        object.__setattr__(self, "claimed_at", claimed_at)

    @property
    def receipt_uniqueness_key(self) -> str:
        return self.receipt_id

    @property
    def nonce_uniqueness_key(self) -> tuple[str, str, str, str]:
        return (self.tenant_id, self.envelope_id, self.audience, self.nonce)

    @property
    def sequence_watermark_key(self) -> tuple[str, str, str]:
        return (self.warden_id, self.lease_id, self.audience)

    @property
    def owner_operation_key(self) -> tuple[str, str]:
        return (self.owner_id, self.operation_id)


__all__ = (
    "EXECUTOR_ANCHOR_FORMAT",
    "ExternalAuthorityAnchorMetadata",
    "ReceiptClaim",
    "ReceiptSequenceWatermark",
)
