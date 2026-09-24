"""Host-neutral structural ports the orchestrator uses for presentation and
durable-state access without importing implementations; records carry identity but
never prove authorization, consumed by projection_controllers.py.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ContextManager, Protocol, TypeAlias, runtime_checkable


BoundaryScalar: TypeAlias = str | int | float | bool | None
BoundaryValue: TypeAlias = (
    BoundaryScalar | tuple["BoundaryValue", ...] | Mapping[str, "BoundaryValue"]
)
BoundaryMapping: TypeAlias = Mapping[str, BoundaryValue]

_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_MAX_BOUNDARY_DEPTH = 32
_MAX_BOUNDARY_ITEMS = 10_000
_MAX_BOUNDARY_STRING = 1_048_576


def _empty_mapping() -> BoundaryMapping:
    return MappingProxyType({})


def _require_symbol(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        raise ValueError(f"{name} must be a bounded protocol symbol")


def _require_identifier(value: str, name: str, *, maximum: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or _CONTROL_CHARACTER.search(value)
    ):
        raise ValueError(f"{name} must be a bounded non-blank identifier")


def _require_optional_revision(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a non-negative integer when supplied")


def _require_revision(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _freeze_value(
    value: object,
    path: str,
    *,
    depth: int = 0,
    seen: frozenset[int] = frozenset(),
    budget: list[int] | None = None,
) -> BoundaryValue:
    if budget is None:
        budget = [_MAX_BOUNDARY_ITEMS]
    if depth > _MAX_BOUNDARY_DEPTH:
        raise ValueError(f"{path} exceeds the boundary nesting limit")
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError(f"{path} exceeds the boundary item limit")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        if len(value) > _MAX_BOUNDARY_STRING:
            raise ValueError(f"{path} exceeds the boundary string limit")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite host-neutral values")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{path} must not contain a reference cycle")
        nested_seen = seen | {identity}
        keys = tuple(value)
        if not all(type(key) is str for key in keys):
            raise ValueError(f"{path} keys must be strings")
        frozen: dict[str, BoundaryValue] = {}
        for key in sorted(keys):
            frozen[key] = _freeze_value(
                value[key],
                f"{path}.{key}",
                depth=depth + 1,
                seen=nested_seen,
                budget=budget,
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{path} must not contain a reference cycle")
        nested_seen = seen | {identity}
        return tuple(
            _freeze_value(
                item,
                f"{path}[{index}]",
                depth=depth + 1,
                seen=nested_seen,
                budget=budget,
            )
            for index, item in enumerate(value)
        )
    raise ValueError(f"{path} must contain only host-neutral JSON-like values")


def _freeze_mapping(value: object, name: str) -> BoundaryMapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a host-neutral mapping")
    frozen = _freeze_value(value, name)
    if not isinstance(frozen, Mapping):  # pragma: no cover
        raise ValueError(f"{name} must be a host-neutral mapping")
    return frozen


@dataclass(frozen=True, slots=True)
class PresentationContext:
    owner_id: str
    actor_id: str
    correlation_id: str
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.owner_id, "owner_id")
        _require_identifier(self.actor_id, "actor_id")
        _require_identifier(self.correlation_id, "correlation_id")
        if self.tenant_id is not None:
            _require_identifier(self.tenant_id, "tenant_id")


@dataclass(frozen=True, slots=True)
class PresentationQuery:
    surface: str
    operation: str
    context: PresentationContext
    arguments: BoundaryMapping = field(default_factory=_empty_mapping)

    def __post_init__(self) -> None:
        _require_symbol(self.surface, "surface")
        _require_symbol(self.operation, "operation")
        if not isinstance(self.context, PresentationContext):
            raise ValueError("context must be a PresentationContext")
        object.__setattr__(
            self,
            "arguments",
            _freeze_mapping(self.arguments, "arguments"),
        )


@dataclass(frozen=True, slots=True)
class PresentationView:
    surface: str
    revision: int | None = None
    model: BoundaryMapping = field(default_factory=_empty_mapping)

    def __post_init__(self) -> None:
        _require_symbol(self.surface, "surface")
        _require_optional_revision(self.revision, "revision")
        object.__setattr__(self, "model", _freeze_mapping(self.model, "model"))


@dataclass(frozen=True, slots=True)
class PresentationCommand:
    surface: str
    action: str
    context: PresentationContext
    arguments: BoundaryMapping = field(default_factory=_empty_mapping)
    expected_revision: int | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _require_symbol(self.surface, "surface")
        _require_symbol(self.action, "action")
        if not isinstance(self.context, PresentationContext):
            raise ValueError("context must be a PresentationContext")
        _require_optional_revision(self.expected_revision, "expected_revision")
        if self.idempotency_key is not None:
            _require_identifier(self.idempotency_key, "idempotency_key")
        object.__setattr__(
            self,
            "arguments",
            _freeze_mapping(self.arguments, "arguments"),
        )


@dataclass(frozen=True, slots=True)
class PresentationCommandResult:
    accepted: bool
    code: str
    revision: int | None = None
    model: BoundaryMapping = field(default_factory=_empty_mapping)

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise ValueError("accepted must be a boolean")
        _require_symbol(self.code, "code")
        _require_optional_revision(self.revision, "revision")
        object.__setattr__(self, "model", _freeze_mapping(self.model, "model"))


@runtime_checkable
class PresentationQueryPort(Protocol):
    def query(self, query: PresentationQuery, /) -> PresentationView: ...


@runtime_checkable
class PresentationCommandPort(Protocol):
    def execute(self, command: PresentationCommand, /) -> PresentationCommandResult: ...


@dataclass(frozen=True, slots=True)
class DurableOwner:
    namespace: str
    owner_id: str

    def __post_init__(self) -> None:
        _require_symbol(self.namespace, "namespace")
        _require_identifier(self.owner_id, "owner_id")


@dataclass(frozen=True, slots=True)
class DurableStateQuery:
    domain: str
    operation: str
    owner: DurableOwner
    arguments: BoundaryMapping = field(default_factory=_empty_mapping)
    limit: int | None = None
    cursor: str | None = None

    def __post_init__(self) -> None:
        _require_symbol(self.domain, "domain")
        _require_symbol(self.operation, "operation")
        if not isinstance(self.owner, DurableOwner):
            raise ValueError("owner must be a DurableOwner")
        if self.limit is not None and (
            type(self.limit) is not int or not 1 <= self.limit <= 10_000
        ):
            raise ValueError("limit must be between 1 and 10000 when supplied")
        if self.cursor is not None:
            _require_identifier(self.cursor, "cursor", maximum=2048)
        object.__setattr__(
            self,
            "arguments",
            _freeze_mapping(self.arguments, "arguments"),
        )


@dataclass(frozen=True, slots=True)
class DurableStateRecord:
    domain: str
    record_id: str
    owner: DurableOwner
    revision: int
    values: BoundaryMapping = field(default_factory=_empty_mapping)

    def __post_init__(self) -> None:
        _require_symbol(self.domain, "domain")
        _require_identifier(self.record_id, "record_id")
        if not isinstance(self.owner, DurableOwner):
            raise ValueError("owner must be a DurableOwner")
        _require_revision(self.revision, "revision")
        object.__setattr__(self, "values", _freeze_mapping(self.values, "values"))


@dataclass(frozen=True, slots=True)
class DurableStatePage:
    records: tuple[DurableStateRecord, ...] = ()
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        records = tuple(self.records)
        if not all(isinstance(record, DurableStateRecord) for record in records):
            raise ValueError("records must contain only DurableStateRecord values")
        if self.next_cursor is not None:
            _require_identifier(self.next_cursor, "next_cursor", maximum=2048)
        object.__setattr__(self, "records", records)


@dataclass(frozen=True, slots=True)
class DurableStateCommand:
    domain: str
    operation: str
    owner: DurableOwner
    values: BoundaryMapping = field(default_factory=_empty_mapping)
    expected_revision: int | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _require_symbol(self.domain, "domain")
        _require_symbol(self.operation, "operation")
        if not isinstance(self.owner, DurableOwner):
            raise ValueError("owner must be a DurableOwner")
        _require_optional_revision(self.expected_revision, "expected_revision")
        if self.idempotency_key is not None:
            _require_identifier(self.idempotency_key, "idempotency_key")
        object.__setattr__(self, "values", _freeze_mapping(self.values, "values"))


@dataclass(frozen=True, slots=True)
class DurableStateCommandResult:
    applied: bool
    code: str
    revision: int | None = None
    record: DurableStateRecord | None = None

    def __post_init__(self) -> None:
        if type(self.applied) is not bool:
            raise ValueError("applied must be a boolean")
        _require_symbol(self.code, "code")
        _require_optional_revision(self.revision, "revision")
        if self.record is not None and not isinstance(self.record, DurableStateRecord):
            raise ValueError("record must be a DurableStateRecord when supplied")


@runtime_checkable
class DurableStateTransaction(Protocol):
    def fetch_one(self, query: DurableStateQuery, /) -> DurableStateRecord | None: ...

    def fetch_page(self, query: DurableStateQuery, /) -> DurableStatePage: ...

    def execute(self, command: DurableStateCommand, /) -> DurableStateCommandResult: ...


@runtime_checkable
class DurableStateService(Protocol):
    def transaction(
        self, *, read_only: bool = False
    ) -> ContextManager[DurableStateTransaction]: ...


__all__ = [
    "BoundaryMapping",
    "BoundaryScalar",
    "BoundaryValue",
    "DurableOwner",
    "DurableStateCommand",
    "DurableStateCommandResult",
    "DurableStatePage",
    "DurableStateQuery",
    "DurableStateRecord",
    "DurableStateService",
    "DurableStateTransaction",
    "PresentationCommand",
    "PresentationCommandPort",
    "PresentationCommandResult",
    "PresentationContext",
    "PresentationQuery",
    "PresentationQueryPort",
    "PresentationView",
]
