"""Host-neutral identifiers and immutable values (OwnerContext, VersionedIdentity,
MutationReceipt) shared by every astralplane.authority module and repository.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from astralplane.errors import DomainValidationError

DomainScalar: TypeAlias = None | bool | int | float | str
DomainValue: TypeAlias = DomainScalar | tuple["DomainValue", ...] | Mapping[str, "DomainValue"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:@/-]{0,126}[A-Za-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_DEPTH = 12
_MAX_ITEMS = 4096
_MAX_TEXT = 16_384


def require_identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DomainValidationError(f"{field} must be a canonical bounded identifier")
    return value


def require_sha256(value: str, *, field: str = "digest") -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DomainValidationError(f"{field} must be lowercase SHA-256")
    return value


def require_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DomainValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def freeze_domain_value(
    value: object,
    *,
    field: str = "value",
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> DomainValue:
    if _depth > _MAX_DEPTH:
        raise DomainValidationError(f"{field} exceeds the maximum nesting depth")
    budget = [_MAX_ITEMS] if _budget is None else _budget
    budget[0] -= 1
    if budget[0] < 0:
        raise DomainValidationError(f"{field} exceeds the maximum item count")
    if value is None or type(value) in {bool, int}:
        return value  # type: ignore[return-value]
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise DomainValidationError(f"{field} contains a non-finite number")
        return value
    if type(value) is str:
        if len(value) > _MAX_TEXT or "\x00" in value:
            raise DomainValidationError(f"{field} contains invalid text")
        return value
    if type(value) in {list, tuple}:
        return tuple(
            freeze_domain_value(
                item,
                field=field,
                _depth=_depth + 1,
                _budget=budget,
            )
            for item in value
        )
    if type(value) is dict:
        frozen: dict[str, DomainValue] = {}
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 256 or "\x00" in key:
                raise DomainValidationError(f"{field} contains an invalid mapping key")
            if key in frozen:
                raise DomainValidationError(f"{field} contains a duplicate mapping key")
            frozen[key] = freeze_domain_value(
                item,
                field=field,
                _depth=_depth + 1,
                _budget=budget,
            )
        return MappingProxyType(frozen)
    if isinstance(value, (Mapping, Sequence)):
        raise DomainValidationError(f"{field} must use exact builtin containers")
    raise DomainValidationError(f"{field} contains an unsupported value type")


class MutationState(StrEnum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class OwnerContext:
    owner_id: str
    actor_id: str
    correlation_id: str
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.owner_id, field="owner id")
        require_identifier(self.actor_id, field="actor id")
        require_identifier(self.correlation_id, field="correlation id")
        if self.tenant_id is not None:
            require_identifier(self.tenant_id, field="tenant id")


@dataclass(frozen=True, slots=True)
class VersionedIdentity:
    owner_id: str
    record_id: str
    version: int

    def __post_init__(self) -> None:
        require_identifier(self.owner_id, field="owner id")
        require_identifier(self.record_id, field="record id")
        if type(self.version) is not int or self.version < 0:
            raise DomainValidationError("version must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    identity: VersionedIdentity
    state: MutationState
    operation_id: str
    committed_at: datetime
    metadata: Mapping[str, DomainValue]

    def __post_init__(self) -> None:
        require_identifier(self.operation_id, field="operation id")
        object.__setattr__(
            self,
            "committed_at",
            require_utc(self.committed_at, field="committed at"),
        )
        frozen = freeze_domain_value(dict(self.metadata), field="metadata")
        if not isinstance(frozen, Mapping):
            raise DomainValidationError("metadata must be a mapping")
        object.__setattr__(self, "metadata", frozen)


__all__ = (
    "DomainScalar",
    "DomainValue",
    "MutationReceipt",
    "MutationState",
    "OwnerContext",
    "VersionedIdentity",
    "freeze_domain_value",
    "require_identifier",
    "require_sha256",
    "require_utc",
)
