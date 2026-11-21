"""Host-neutral durable authority-binding values; AstralPlane persists these fences but
leaves which agents are governed and which lifecycle transitions are permitted to the
composition host.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, Self

from astralplane.domain import require_identifier, require_utc
from astralplane.errors import DomainValidationError

_LETS_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PENDING_ID_DOMAIN: Final = b"astralplane.authority.pending/v1\0"
_PENDING_ID_PREFIX: Final = "pending:"
PendingAuthorityField = Literal["warden", "lease", "lineage", "subject"]


class AuthorityPopulation(StrEnum):
    SERVER_DYNAMIC = "server_dynamic"
    BYO_USER = "byo_user"


class AuthorityBindingState(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    QUIESCENT = "quiescent"
    CLOSING = "closing"
    CLOSED = "closed"
    REVOKING = "revoking"
    REVOKED = "revoked"
    RECONCILING = "reconciling"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            AuthorityBindingState.CLOSED,
            AuthorityBindingState.REVOKED,
            AuthorityBindingState.EXPIRED,
        }


def _positive_integer(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise DomainValidationError(f"{field} must be a positive integer")
    return value


def _non_negative_integer(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise DomainValidationError(f"{field} must be a non-negative integer")
    return value


def _lets_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _LETS_DIGEST.fullmatch(value) is None:
        raise DomainValidationError(f"{field} must be a canonical LETS SHA-256 digest")
    return value


def _canonical_capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise DomainValidationError("capabilities must be a nonempty canonical tuple")
    if any(not isinstance(item, str) for item in value):
        raise DomainValidationError("capabilities must contain canonical identifiers")
    for capability in value:
        require_identifier(capability, field="capability")
    if tuple(sorted(value)) != value or len(value) != len(set(value)):
        raise DomainValidationError("capabilities must be sorted and unique")
    return value


def pending_authority_identity(
    binding_id: str,
    *,
    field: PendingAuthorityField,
) -> str:
    canonical_binding_id = require_identifier(binding_id, field="binding id")
    if not isinstance(field, str) or field not in (
        "warden",
        "lease",
        "lineage",
        "subject",
    ):
        raise DomainValidationError("pending authority field is not supported")
    digest = hashlib.sha256(
        _PENDING_ID_DOMAIN + canonical_binding_id.encode("ascii")
    ).hexdigest()[:32]
    return f"{_PENDING_ID_PREFIX}{field}:{digest}"


@dataclass(frozen=True, slots=True)
class AgentAuthorityBinding:
    binding_id: str
    owner_id: str
    agent_id: str
    runtime_id: str
    runtime_generation: int
    population: AuthorityPopulation
    tenant_id: str
    envelope_id: str
    warden_id: str
    lease_id: str
    lineage_id: str
    subject_id: str
    policy_digest: str
    machine_digest: str
    config_epoch: int
    capabilities: tuple[str, ...]
    lease_sequence: int
    lease_expires_at_ns: int
    state: AuthorityBindingState
    created_at: datetime
    updated_at: datetime
    version: int

    @classmethod
    def provisioning_intent(
        cls,
        *,
        binding_id: str,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
        population: AuthorityPopulation,
        tenant_id: str,
        envelope_id: str,
        policy_digest: str,
        machine_digest: str,
        config_epoch: int,
        capabilities: tuple[str, ...],
        created_at: datetime,
    ) -> Self:
        return cls(
            binding_id=binding_id,
            owner_id=owner_id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            runtime_generation=runtime_generation,
            population=population,
            tenant_id=tenant_id,
            envelope_id=envelope_id,
            warden_id=pending_authority_identity(binding_id, field="warden"),
            lease_id=pending_authority_identity(binding_id, field="lease"),
            lineage_id=pending_authority_identity(binding_id, field="lineage"),
            subject_id=pending_authority_identity(binding_id, field="subject"),
            policy_digest=policy_digest,
            machine_digest=machine_digest,
            config_epoch=config_epoch,
            capabilities=capabilities,
            lease_sequence=0,
            lease_expires_at_ns=0,
            state=AuthorityBindingState.PROVISIONING,
            created_at=created_at,
            updated_at=created_at,
            version=0,
        )

    def __post_init__(self) -> None:
        for field, value in (
            ("binding id", self.binding_id),
            ("owner id", self.owner_id),
            ("agent id", self.agent_id),
            ("runtime id", self.runtime_id),
            ("tenant id", self.tenant_id),
            ("envelope id", self.envelope_id),
            ("warden id", self.warden_id),
            ("lease id", self.lease_id),
            ("lineage id", self.lineage_id),
            ("subject id", self.subject_id),
        ):
            require_identifier(value, field=field)
        if not isinstance(self.population, AuthorityPopulation):
            raise DomainValidationError("population must be an approved authority population")
        if not isinstance(self.state, AuthorityBindingState):
            raise DomainValidationError("state must be an authority binding state")

        _positive_integer(self.runtime_generation, field="runtime generation")
        _lets_digest(self.policy_digest, field="policy digest")
        _lets_digest(self.machine_digest, field="machine digest")
        _positive_integer(self.config_epoch, field="config epoch")
        object.__setattr__(
            self,
            "capabilities",
            _canonical_capabilities(self.capabilities),
        )
        _non_negative_integer(self.lease_sequence, field="lease sequence")
        _non_negative_integer(self.lease_expires_at_ns, field="lease expiry")
        _non_negative_integer(self.version, field="version")

        remote_identities = {
            "warden": self.warden_id,
            "lease": self.lease_id,
            "lineage": self.lineage_id,
            "subject": self.subject_id,
        }
        expected_pending = {
            "warden": pending_authority_identity(self.binding_id, field="warden"),
            "lease": pending_authority_identity(self.binding_id, field="lease"),
            "lineage": pending_authority_identity(self.binding_id, field="lineage"),
            "subject": pending_authority_identity(self.binding_id, field="subject"),
        }
        has_pending_identity = any(
            value.startswith(_PENDING_ID_PREFIX) for value in remote_identities.values()
        )
        retains_pending_intent = self.state in {
            AuthorityBindingState.PROVISIONING,
            AuthorityBindingState.CLOSED,
        }
        if retains_pending_intent and has_pending_identity:
            if remote_identities != expected_pending:
                raise DomainValidationError(
                    "pending binding requires deterministic pending remote identities"
                )
            if self.lease_sequence != 0 or self.lease_expires_at_ns != 0:
                raise DomainValidationError(
                    "pending binding cannot carry issued lease metadata"
                )
        elif self.state is AuthorityBindingState.PROVISIONING:
            raise DomainValidationError(
                "pending binding requires deterministic pending remote identities"
            )
        else:
            if has_pending_identity:
                raise DomainValidationError(
                    "issued binding cannot carry a pending remote identity"
                )
            if self.lease_expires_at_ns == 0:
                raise DomainValidationError(
                    "issued binding lease expiry must be positive"
                )

        created_at = require_utc(self.created_at, field="created at")
        updated_at = require_utc(self.updated_at, field="updated at")
        if updated_at < created_at:
            raise DomainValidationError("updated at cannot precede created at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @property
    def owner_agent_key(self) -> tuple[str, str, AuthorityPopulation]:
        return (self.owner_id, self.agent_id, self.population)

    @property
    def runtime_generation_key(self) -> tuple[str, str, str, int]:
        return (
            self.owner_id,
            self.agent_id,
            self.runtime_id,
            self.runtime_generation,
        )


__all__ = (
    "AgentAuthorityBinding",
    "AuthorityBindingState",
    "AuthorityPopulation",
    "PendingAuthorityField",
    "pending_authority_identity",
)
