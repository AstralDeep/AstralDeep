"""Neutral public contracts (Transaction, Repository, OutboxStore,
ReconciliationCoordinator, ProductReconciler, ...) that every astralplane module and
its many AstralDeep callers implement or depend on.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, TypeAlias, runtime_checkable

Statement: TypeAlias = str
PositionalParameters: TypeAlias = tuple[object, ...]
NamedParameters: TypeAlias = Mapping[str, object]
Parameters: TypeAlias = PositionalParameters | NamedParameters
Record: TypeAlias = Mapping[str, Any]


class IsolationLevel(StrEnum):
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


@runtime_checkable
class CommandResultContract(Protocol):
    @property
    def rowcount(self) -> int: ...

    @property
    def status_message(self) -> str | None: ...

    @property
    def returned_records(self) -> tuple[Record, ...]: ...


@runtime_checkable
class QueryExecutor(Protocol):
    def execute(
        self, statement: Statement, parameters: Parameters = ()
    ) -> CommandResultContract: ...

    def fetch_one(self, statement: Statement, parameters: Parameters = ()) -> Record | None: ...

    def fetch_all(
        self, statement: Statement, parameters: Parameters = ()
    ) -> tuple[Record, ...]: ...


@runtime_checkable
class Transaction(QueryExecutor, Protocol):
    def savepoint(self, name: str) -> AbstractContextManager[Transaction]: ...


@runtime_checkable
class PlaneDatabase(Protocol):
    def transaction(
        self, *, isolation: IsolationLevel | None = None
    ) -> AbstractContextManager[Transaction]: ...


@runtime_checkable
class SchemaMigration(Protocol):
    name: str
    source_revisions: tuple[str | None, ...]
    target_revision: str
    checksum: str

    def apply(self, transaction: Transaction) -> None: ...


@runtime_checkable
class Repository(Protocol):
    def health(self, transaction: Transaction) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    entry_id: str
    topic: str
    canonical_payload: bytes
    payload_sha256: str
    idempotency_key: str
    available_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEntry:
    entry: OutboxEntry
    worker_id: str
    lease_expires_at: datetime
    expected_version: int
    attempt: int


@dataclass(frozen=True, slots=True)
class ReclaimedOutboxEntry:
    entry_id: str
    previous_worker_id: str
    expected_version: int
    available_at: datetime


@runtime_checkable
class OutboxStore(Protocol):
    def enqueue(
        self,
        transaction: Transaction,
        entry: OutboxEntry,
    ) -> CommandResultContract: ...

    def claim(
        self,
        transaction: Transaction,
        *,
        worker_id: str,
        topics: tuple[str, ...],
        now: datetime,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[ClaimedOutboxEntry, ...]: ...

    def ack(
        self,
        transaction: Transaction,
        *,
        entry_id: str,
        worker_id: str,
        expected_version: int,
        now: datetime,
    ) -> CommandResultContract: ...

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
    ) -> CommandResultContract: ...

    def dead_letter(
        self,
        transaction: Transaction,
        *,
        entry_id: str,
        worker_id: str,
        expected_version: int,
        error_code: str,
        now: datetime,
    ) -> CommandResultContract: ...

    def reclaim_expired(
        self,
        transaction: Transaction,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ReclaimedOutboxEntry, ...]: ...


@runtime_checkable
class LifecycleStore(Protocol):
    def compare_and_set(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        operation_id: str,
        expected_version: int,
        state: str,
    ) -> CommandResultContract: ...


@runtime_checkable
class RecoveryInspector(Protocol):
    def inspect(self, transaction: Transaction) -> Mapping[str, object]: ...

    def verify(self, transaction: Transaction) -> Mapping[str, object]: ...


class ReconciliationMarkerState(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReconciliationHookIdentity:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ReconciliationMarker:
    schema_revision: str
    plan_digest: str
    hook: ReconciliationHookIdentity
    state: ReconciliationMarkerState
    attempt: int
    result_digest: str | None = None
    error_type: str | None = None


@runtime_checkable
class ReconciliationSession(Protocol):
    def get_marker(self, hook: ReconciliationHookIdentity) -> ReconciliationMarker | None: ...

    def mark_started(self, hook: ReconciliationHookIdentity) -> ReconciliationMarker: ...

    def mark_completed(
        self,
        hook: ReconciliationHookIdentity,
        *,
        result_digest: str,
    ) -> ReconciliationMarker: ...

    def mark_failed(
        self,
        hook: ReconciliationHookIdentity,
        *,
        error_type: str,
    ) -> ReconciliationMarker: ...


@runtime_checkable
class ReconciliationCoordinator(Protocol):
    def coordinate(
        self,
        *,
        advisory_lock: tuple[int, int],
        schema_revision: str,
        plan_digest: str,
    ) -> AbstractContextManager[ReconciliationSession]: ...


@runtime_checkable
class ProductReconciler(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def reconcile(self, context: Mapping[str, object]) -> Mapping[str, object] | None: ...


MigrationCallable: TypeAlias = Callable[[Transaction], None]
ReconcilerFactory: TypeAlias = Callable[[], Iterator[ProductReconciler]]

__all__ = (
    "ClaimedOutboxEntry",
    "CommandResultContract",
    "IsolationLevel",
    "LifecycleStore",
    "MigrationCallable",
    "NamedParameters",
    "OutboxEntry",
    "OutboxStore",
    "Parameters",
    "PlaneDatabase",
    "PositionalParameters",
    "ProductReconciler",
    "QueryExecutor",
    "ReclaimedOutboxEntry",
    "ReconcilerFactory",
    "ReconciliationCoordinator",
    "ReconciliationHookIdentity",
    "ReconciliationMarker",
    "ReconciliationMarkerState",
    "ReconciliationSession",
    "Record",
    "RecoveryInspector",
    "Repository",
    "SchemaMigration",
    "Statement",
    "Transaction",
)
