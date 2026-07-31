"""Durable admission, operation lifecycle, and execution fencing.

``WorkAdmissionCoordinator`` is the sole operation-state authority.  Product
construction must inject either ``PostgresWorkAdmissionRepository`` or an
existing :class:`shared.database.Database`; the coordinator never silently
falls back to process memory.  ``InMemoryWorkAdmissionRepository`` exists only
as an explicitly named deterministic test dependency.

The public projections in this module deliberately exclude authenticated owner
identifiers, idempotency material, and execution fences.  Internal operation
records remain available only to trusted workers that already hold a fence.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol, Sequence


_OPERATION_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATES: frozenset["OperationState"]


class AdmissionClass(str, Enum):
    GLOBAL = "global"
    INTERACTIVE = "interactive"
    MCP = "mcp"
    BACKGROUND = "background"
    SCHEDULED = "scheduled"
    MAINTENANCE = "maintenance"
    SYSTEM = "system"


class OwnerScope(str, Enum):
    CONNECTION = "connection"
    USER = "user"
    SCHEDULE = "schedule"
    MAINTENANCE = "maintenance"
    SYSTEM = "system"


class OperationState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYABLE = "retryable"


_TERMINAL_STATES = frozenset(
    {
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RETRYABLE,
    }
)


class OperationNotFoundError(LookupError):
    """Raised identically for absent and non-owner-visible records."""


class StaleExecutionFenceError(RuntimeError):
    """Raised when a worker no longer owns the selected execution."""


class AdmissionConfigurationError(ValueError):
    """Raised for an invalid admission-class graph or limit."""


@dataclass(frozen=True)
class AdmissionClassConfig:
    class_name: AdmissionClass
    parent_class_name: AdmissionClass | None
    active_limit: int
    queue_limit: int
    max_wait_ms: int | None
    config_revision: str

    def __post_init__(self) -> None:
        if self.active_limit <= 0:
            raise AdmissionConfigurationError("active_limit must be positive")
        if self.queue_limit < 0:
            raise AdmissionConfigurationError("queue_limit cannot be negative")
        if self.queue_limit > 0 and (self.max_wait_ms is None or self.max_wait_ms <= 0):
            raise AdmissionConfigurationError(
                "a finite positive max_wait_ms is required for a non-empty queue"
            )
        if self.max_wait_ms is not None and self.max_wait_ms < 0:
            raise AdmissionConfigurationError("max_wait_ms cannot be negative")
        if not (1 <= len(self.config_revision) <= 128):
            raise AdmissionConfigurationError(
                "config_revision must be 1..128 characters"
            )
        if self.parent_class_name is self.class_name:
            raise AdmissionConfigurationError("an admission class cannot parent itself")


@dataclass(frozen=True)
class OperationOwner:
    owner_scope: OwnerScope
    owner_user_id: str | None
    connection_scope_id: uuid.UUID | None

    def __post_init__(self) -> None:
        if self.owner_scope in {OwnerScope.USER, OwnerScope.SCHEDULE}:
            if not self.owner_user_id:
                raise ValueError("user and schedule ownership require owner_user_id")
        elif self.owner_user_id is not None:
            raise ValueError("owner_user_id is invalid for this owner scope")
        if self.owner_scope is OwnerScope.CONNECTION:
            if not isinstance(self.connection_scope_id, uuid.UUID):
                raise ValueError(
                    "connection ownership requires a UUID connection_scope_id"
                )
        elif self.connection_scope_id is not None and not isinstance(
            self.connection_scope_id, uuid.UUID
        ):
            raise ValueError("connection_scope_id must be a UUID when supplied")


@dataclass(frozen=True)
class OperationRequest:
    operation_kind: str
    admission_class: AdmissionClass
    owner: OperationOwner
    submission_id: uuid.UUID
    idempotency_namespace: str | None
    idempotency_key: str | None
    normalized_input_digest: str | None
    chat_id: str | None
    parent_operation_id: uuid.UUID | None
    connection_generation: uuid.UUID | None
    request_generation: uuid.UUID | None

    def __post_init__(self) -> None:
        if not _OPERATION_KIND_RE.fullmatch(self.operation_kind):
            raise ValueError("operation_kind must be bounded snake_case")
        if self.admission_class is AdmissionClass.GLOBAL:
            raise ValueError("global is a parent capacity class, not a work class")
        if not isinstance(self.submission_id, uuid.UUID):
            raise ValueError("submission_id must be a UUID")
        for name, value in (
            ("parent_operation_id", self.parent_operation_id),
            ("connection_generation", self.connection_generation),
            ("request_generation", self.request_generation),
        ):
            if value is not None and not isinstance(value, uuid.UUID):
                raise ValueError(f"{name} must be a UUID when supplied")
        identity = (
            self.idempotency_namespace,
            self.idempotency_key,
            self.normalized_input_digest,
        )
        if any(value is not None for value in identity):
            if any(value is None for value in identity):
                raise ValueError(
                    "idempotency namespace, key, and digest are all-or-none"
                )
            if not (1 <= len(self.idempotency_namespace or "") <= 128):
                raise ValueError("idempotency_namespace must be 1..128 characters")
            if not (1 <= len(self.idempotency_key or "") <= 256):
                raise ValueError("idempotency_key must be 1..256 characters")
            if not _SHA256_RE.fullmatch(self.normalized_input_digest or ""):
                raise ValueError("normalized_input_digest must be lowercase SHA-256")


@dataclass(frozen=True)
class ExecutionFence:
    operation_id: uuid.UUID
    execution_generation: int
    execution_lease_token: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, uuid.UUID):
            raise ValueError("operation_id must be a UUID")
        if self.execution_generation <= 0:
            raise ValueError("execution_generation must be positive")
        if not isinstance(self.execution_lease_token, uuid.UUID):
            raise ValueError("execution_lease_token must be a UUID")


@dataclass(frozen=True)
class OperationRecord:
    operation_id: uuid.UUID
    operation_kind: str
    admission_class: AdmissionClass
    owner_scope: OwnerScope
    owner_user_id: str | None
    connection_scope_id: uuid.UUID | None
    idempotency_namespace: str | None
    idempotency_key: str | None
    normalized_input_digest: str | None
    chat_id: str | None
    parent_operation_id: uuid.UUID | None
    connection_generation: uuid.UUID | None
    request_generation: uuid.UUID | None
    state: OperationState
    phase_code: str | None
    terminal_code: str | None
    safe_summary: str | None
    retry_after_ms: int | None
    execution_generation: int
    execution_lease_token: uuid.UUID | None
    state_revision: int
    accepted_at: datetime
    updated_at: datetime
    queue_deadline_at: datetime | None
    started_at: datetime | None
    terminal_at: datetime | None
    cancel_requested_at: datetime | None
    purge_after: datetime | None


@dataclass(frozen=True)
class SafeOperationProjection:
    operation_id: uuid.UUID
    operation_kind: str
    admission_class: AdmissionClass
    owner_scope: OwnerScope
    chat_id: str | None
    parent_operation_id: uuid.UUID | None
    connection_generation: uuid.UUID | None
    request_generation: uuid.UUID | None
    state: OperationState
    phase_code: str | None
    terminal_code: str | None
    safe_summary: str | None
    retry_after_ms: int | None
    state_revision: int
    accepted_at: datetime
    queue_deadline_at: datetime | None
    started_at: datetime | None
    terminal_at: datetime | None
    updated_at: datetime
    purge_after: datetime | None


@dataclass(frozen=True)
class AcceptedAdmission:
    accepted: bool
    operation_id: uuid.UUID
    state: OperationState
    state_revision: int
    queue_position: int | None
    queue_deadline_at: datetime | None


@dataclass(frozen=True)
class RefusedAdmission:
    accepted: bool
    code: str
    retryable: bool
    retry_after_ms: int | None


AdmissionResult = AcceptedAdmission | RefusedAdmission


@dataclass(frozen=True)
class AcceptedSubmission:
    accepted: bool
    operation: SafeOperationProjection


SubmissionResult = AcceptedSubmission | RefusedAdmission


@dataclass(frozen=True)
class OperationClaim:
    operation: OperationRecord
    fence: ExecutionFence


@dataclass(frozen=True)
class AdmissionClassStatus:
    class_name: AdmissionClass
    parent_class_name: AdmissionClass | None
    active_limit: int
    queue_limit: int
    max_wait_ms: int | None
    active_count: int
    queued_count: int
    oldest_queued_at: datetime | None
    oldest_running_at: datetime | None


@dataclass(frozen=True)
class PurgeResult:
    operations: int
    submissions: int


@dataclass(frozen=True)
class SlotLeaseRenewal:
    operation_id: uuid.UUID
    execution_generation: int
    lease_expires_at: datetime


class WorkAdmissionRepository(Protocol):
    """Transactional persistence contract used by the coordinator."""

    def configure(self, admission_classes: Sequence[AdmissionClassConfig]) -> None: ...

    def submit(
        self,
        request: OperationRequest,
        *,
        now: datetime | None,
        retention: timedelta,
        slot_lease: timedelta,
    ) -> AdmissionResult: ...

    def claim_next(
        self,
        class_name: AdmissionClass,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None: ...

    def claim_operation(
        self,
        class_name: AdmissionClass,
        operation_id: uuid.UUID,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None: ...

    def inspect_admission_class(
        self, class_name: AdmissionClass, *, now: datetime | None
    ) -> AdmissionClassStatus: ...

    def query_operation(
        self, owner: OperationOwner, operation_id: uuid.UUID
    ) -> SafeOperationProjection: ...

    def reconcile_submission(
        self, owner: OperationOwner, submission_id: uuid.UUID
    ) -> SubmissionResult: ...

    def cancel(
        self,
        owner: OperationOwner,
        operation_id: uuid.UUID,
        terminal_code: str,
        *,
        now: datetime | None,
        retention: timedelta,
        request_running: bool = True,
        transaction: Any | None = None,
    ) -> OperationRecord: ...

    def terminalize_unselected(
        self,
        operation_id: uuid.UUID,
        *,
        terminal_code: str,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
    ) -> OperationRecord | None: ...

    def terminalize(
        self,
        fence: ExecutionFence,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
        transaction: Any | None = None,
    ) -> OperationRecord: ...

    def expire_queued(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]: ...

    def assert_current_execution(
        self, fence: ExecutionFence, *, transaction: Any | None = None
    ) -> OperationRecord: ...

    def reselect_execution(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> ExecutionFence: ...

    def update_phase(
        self,
        fence: ExecutionFence,
        phase_code: str,
        *,
        now: datetime | None,
    ) -> OperationRecord: ...

    def renew_execution_lease(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> SlotLeaseRenewal: ...

    def expire_execution_leases(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]: ...

    def purge_expired(
        self,
        *,
        now: datetime | None,
        limit: int,
        fence: ExecutionFence | None = None,
    ) -> PurgeResult: ...

    def fenced_transaction(self, fence: ExecutionFence) -> ContextManager[Any]: ...


class WorkAdmissionCoordinator:
    """Validated public façade over one explicitly injected repository."""

    def __init__(
        self,
        *,
        admission_classes: Sequence[AdmissionClassConfig],
        repository: WorkAdmissionRepository | None = None,
        database: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        operation_retention: timedelta = timedelta(hours=24),
        slot_lease: timedelta = timedelta(seconds=30),
        _configure_repository: bool = True,
    ) -> None:
        if (repository is None) == (database is None):
            raise ValueError("inject exactly one work-admission repository or Database")
        if operation_retention <= timedelta(0):
            raise ValueError("operation_retention must be positive")
        if slot_lease <= timedelta(0):
            raise ValueError("slot_lease must be positive")
        configs = tuple(admission_classes)
        _validate_admission_graph(configs)
        self._repository: WorkAdmissionRepository = (
            repository
            if repository is not None
            else PostgresWorkAdmissionRepository(database)
        )
        self._clock = clock
        self._operation_retention = operation_retention
        self._slot_lease = slot_lease
        if _configure_repository:
            self._repository.configure(configs)
        else:
            bound_configs = getattr(self._repository, "_configs", None)
            if not isinstance(bound_configs, dict) or bound_configs != {
                config.class_name: config for config in configs
            }:
                raise ValueError(
                    "read-only coordinator construction requires an atomically "
                    "bound repository"
                )

    @classmethod
    def from_database(
        cls,
        *,
        database: Any,
        clock: Callable[[], datetime] | None = None,
        operation_retention: timedelta = timedelta(hours=24),
        slot_lease: timedelta = timedelta(seconds=30),
    ) -> WorkAdmissionCoordinator:
        """Bind the effective PostgreSQL graph without rewriting its rows."""

        repository = PostgresWorkAdmissionRepository(database)
        configs = repository.load_existing_configs()
        return cls(
            admission_classes=configs,
            repository=repository,
            clock=clock,
            operation_retention=operation_retention,
            slot_lease=slot_lease,
            _configure_repository=False,
        )

    def _now(self) -> datetime | None:
        if self._clock is None:
            return None
        return _normalize_datetime(self._clock())

    def submit(self, request: OperationRequest) -> AdmissionResult:
        return self._repository.submit(
            request,
            now=self._now(),
            retention=self._operation_retention,
            slot_lease=self._slot_lease,
        )

    def claim_next(self, class_name: AdmissionClass) -> OperationClaim | None:
        return self._repository.claim_next(
            class_name,
            now=self._now(),
            slot_lease=self._slot_lease,
            retention=self._operation_retention,
        )

    def claim_operation(
        self,
        class_name: AdmissionClass,
        operation_id: uuid.UUID,
    ) -> OperationClaim | None:
        """Claim exactly ``operation_id`` without consuming another handoff.

        A queued operation is claimable only when it is the class's FIFO head.
        A running operation is returned only while its one-time preselection
        marker is intact.  This is the origin-local handoff used immediately
        after ``submit``; normal workers should continue to use ``claim_next``.
        """

        return self._repository.claim_operation(
            class_name,
            _require_uuid(operation_id, "operation_id"),
            now=self._now(),
            slot_lease=self._slot_lease,
            retention=self._operation_retention,
        )

    @property
    def operation_retention(self) -> timedelta:
        """Configured terminal retention, exposed for compatibility cleanup."""

        return self._operation_retention

    @property
    def repository(self) -> WorkAdmissionRepository:
        """The injected durable repository shared by trusted state machines.

        Runtime subsystems use this narrow exposure so one operation can be
        fenced and terminalized in the same PostgreSQL transaction as its
        domain effect. Callers must not replace or reconfigure the repository.
        """

        return self._repository

    @property
    def slot_lease(self) -> timedelta:
        """Configured execution-slot lease used to bound worker renewals."""

        return self._slot_lease

    def inspect_admission_class(
        self, class_name: AdmissionClass
    ) -> AdmissionClassStatus:
        return self._repository.inspect_admission_class(class_name, now=self._now())

    def query_operation(
        self, *, owner: OperationOwner, operation_id: uuid.UUID
    ) -> SafeOperationProjection:
        return self._repository.query_operation(
            owner, _require_uuid(operation_id, "operation_id")
        )

    def reconcile_submission(
        self, *, owner: OperationOwner, submission_id: uuid.UUID
    ) -> SubmissionResult:
        return self._repository.reconcile_submission(
            owner, _require_uuid(submission_id, "submission_id")
        )

    def cancel(
        self,
        *,
        owner: OperationOwner,
        operation_id: uuid.UUID,
        terminal_code: str,
        request_running: bool = True,
        transaction: Any | None = None,
    ) -> OperationRecord:
        """Cancel queued/preselected work, or request cancellation after handoff.

        Trusted subsystems that must decide a second database guard while the
        operation lock is held may pass their PostgreSQL cursor and set
        ``request_running=False``.  Existing callers retain the normal
        cooperative-cancellation behavior by default.
        """

        _validate_safe_code(terminal_code, "terminal_code")
        return self._repository.cancel(
            owner,
            _require_uuid(operation_id, "operation_id"),
            terminal_code,
            now=self._now(),
            retention=self._operation_retention,
            request_running=request_running,
            transaction=transaction,
        )

    def terminalize_unselected(
        self,
        operation_id: uuid.UUID,
        *,
        terminal_code: str,
        safe_summary: str | None,
        retry_after_ms: int | None,
    ) -> OperationRecord | None:
        """Settle exact accepted work only before its worker handoff.

        Queued work and a running operation whose one-time preselection marker
        is still intact transition to ``RETRYABLE``. Missing operations and
        executions already handed to or reselected by a worker are left
        untouched. Replays return the first terminal record unchanged.
        """

        operation_id = _require_uuid(operation_id, "operation_id")
        _validate_safe_code(terminal_code, "terminal_code")
        _validate_safe_summary(safe_summary)
        _validate_retry_after(OperationState.RETRYABLE, retry_after_ms)
        return self._repository.terminalize_unselected(
            operation_id,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
            now=self._now(),
            retention=self._operation_retention,
        )

    def terminalize(
        self,
        fence: ExecutionFence,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
        transaction: Any | None = None,
    ) -> OperationRecord:
        if state not in _TERMINAL_STATES:
            raise ValueError("terminalize requires a terminal state")
        if state is not OperationState.COMPLETED and terminal_code is None:
            raise ValueError("non-completed terminal states require terminal_code")
        if terminal_code is not None:
            _validate_safe_code(terminal_code, "terminal_code")
        _validate_safe_summary(safe_summary)
        _validate_retry_after(state, retry_after_ms)
        return self._repository.terminalize(
            fence,
            state=state,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
            now=self._now(),
            retention=self._operation_retention,
            transaction=transaction,
        )

    def expire_queued(self) -> tuple[OperationRecord, ...]:
        return self._repository.expire_queued(
            now=self._now(), retention=self._operation_retention
        )

    def assert_current_execution(
        self, fence: ExecutionFence, *, transaction: Any | None = None
    ) -> OperationRecord:
        return self._repository.assert_current_execution(fence, transaction=transaction)

    def reselect_execution(self, fence: ExecutionFence) -> ExecutionFence:
        return self._repository.reselect_execution(
            fence, now=self._now(), slot_lease=self._slot_lease
        )

    def update_phase(self, fence: ExecutionFence, phase_code: str) -> OperationRecord:
        _validate_safe_code(phase_code, "phase_code")
        return self._repository.update_phase(fence, phase_code, now=self._now())

    def renew_execution_lease(self, fence: ExecutionFence) -> SlotLeaseRenewal:
        return self._repository.renew_execution_lease(
            fence, now=self._now(), slot_lease=self._slot_lease
        )

    def expire_execution_leases(self) -> tuple[OperationRecord, ...]:
        return self._repository.expire_execution_leases(
            now=self._now(), retention=self._operation_retention
        )

    def purge_expired(
        self, *, limit: int = 100, fence: ExecutionFence | None = None
    ) -> PurgeResult:
        if limit <= 0 or limit > 10_000:
            raise ValueError("purge limit must be between 1 and 10000")
        return self._repository.purge_expired(
            now=self._now(), limit=limit, fence=fence
        )

    def fenced_transaction(self, fence: ExecutionFence) -> ContextManager[Any]:
        """Return a transaction that locks and validates ``fence``.

        Database-owned effects executed with the yielded PostgreSQL cursor are
        committed atomically with the fence check.  The explicitly injected
        in-memory test repository yields a sentinel while holding its lock.
        """

        return self._repository.fenced_transaction(fence)


def _validate_admission_graph(configs: Sequence[AdmissionClassConfig]) -> None:
    if not configs:
        raise AdmissionConfigurationError("at least one admission class is required")
    by_name = {config.class_name: config for config in configs}
    if len(by_name) != len(configs):
        raise AdmissionConfigurationError("admission class names must be unique")
    for config in configs:
        if (
            config.parent_class_name is not None
            and config.parent_class_name not in by_name
        ):
            raise AdmissionConfigurationError(
                f"missing parent admission class {config.parent_class_name.value}"
            )
        seen: set[AdmissionClass] = set()
        current: AdmissionClass | None = config.class_name
        while current is not None:
            if current in seen:
                raise AdmissionConfigurationError(
                    "admission class graph contains a cycle"
                )
            seen.add(current)
            current = by_name[current].parent_class_name


def _admission_configs_from_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[AdmissionClassConfig, ...]:
    configs = tuple(
        AdmissionClassConfig(
            class_name=AdmissionClass(str(row["class_name"])),
            parent_class_name=(
                AdmissionClass(str(row["parent_class_name"]))
                if row["parent_class_name"] is not None
                else None
            ),
            active_limit=int(row["active_limit"]),
            queue_limit=int(row["queue_limit"]),
            max_wait_ms=(
                int(row["max_wait_ms"]) if int(row["max_wait_ms"]) > 0 else None
            ),
            config_revision=str(row["config_revision"]),
        )
        for row in rows
    )
    configured = {config.class_name for config in configs}
    required = set(AdmissionClass)
    if configured != required:
        missing = sorted(member.value for member in required - configured)
        unexpected = sorted(member.value for member in configured - required)
        raise AdmissionConfigurationError(
            "production admission config must contain every class "
            f"(missing={missing}, unexpected={unexpected})"
        )
    _validate_admission_graph(configs)
    return configs


def load_admission_class_configs(database: Any) -> tuple[AdmissionClassConfig, ...]:
    """Read and validate the complete effective PostgreSQL class graph."""

    rows = database.fetch_all(
        "SELECT class_name, parent_class_name, active_limit, queue_limit, "
        "max_wait_ms, config_revision FROM operation_admission_class "
        "ORDER BY CASE WHEN parent_class_name IS NULL THEN 0 ELSE 1 END, "
        "class_name"
    )
    return _admission_configs_from_rows(rows)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("coordination timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _require_uuid(value: uuid.UUID, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ValueError(f"{name} must be a UUID")
    return value


def _validate_safe_code(value: str, name: str) -> None:
    if not _SAFE_CODE_RE.fullmatch(value):
        raise ValueError(f"{name} must be bounded snake_case")


def _validate_safe_summary(value: str | None) -> None:
    if value is not None and len(value) > 512:
        raise ValueError("safe_summary cannot exceed 512 characters")


def _validate_retry_after(state: OperationState, retry_after_ms: int | None) -> None:
    if retry_after_ms is not None and retry_after_ms < 0:
        raise ValueError("retry_after_ms cannot be negative")
    if retry_after_ms is not None and state is not OperationState.RETRYABLE:
        raise ValueError("retry_after_ms is valid only for retryable outcomes")


def _owner_partition(owner: OperationOwner) -> tuple[str, str]:
    if owner.owner_scope is OwnerScope.CONNECTION:
        return owner.owner_scope.value, str(owner.connection_scope_id)
    if owner.owner_scope in {OwnerScope.USER, OwnerScope.SCHEDULE}:
        return owner.owner_scope.value, owner.owner_user_id or ""
    return owner.owner_scope.value, ""


def _safe_projection(record: OperationRecord) -> SafeOperationProjection:
    return SafeOperationProjection(
        operation_id=record.operation_id,
        operation_kind=record.operation_kind,
        admission_class=record.admission_class,
        owner_scope=record.owner_scope,
        chat_id=record.chat_id,
        parent_operation_id=record.parent_operation_id,
        connection_generation=record.connection_generation,
        request_generation=record.request_generation,
        state=record.state,
        phase_code=record.phase_code,
        terminal_code=record.terminal_code,
        safe_summary=record.safe_summary,
        retry_after_ms=record.retry_after_ms,
        state_revision=record.state_revision,
        accepted_at=record.accepted_at,
        queue_deadline_at=record.queue_deadline_at,
        started_at=record.started_at,
        terminal_at=record.terminal_at,
        updated_at=record.updated_at,
        purge_after=record.purge_after,
    )


@dataclass(frozen=True)
class _SubmissionRecord:
    submission_result_id: uuid.UUID
    submission_id: uuid.UUID
    owner: OperationOwner
    accepted: bool
    operation_id: uuid.UUID | None
    refusal_code: str | None
    retryable: bool
    retry_after_ms: int | None
    observed_at: datetime
    purge_after: datetime


@dataclass(frozen=True)
class _SlotRecord:
    class_name: AdmissionClass
    slot_number: int
    operation_id: uuid.UUID | None = None
    lease_token: uuid.UUID | None = None
    claim_generation: int = 0
    lease_expires_at: datetime | None = None


class InMemoryWorkAdmissionRepository:
    """Explicit deterministic test repository.

    This class is intentionally never selected by ``WorkAdmissionCoordinator``.
    Tests must name and inject it, making accidental production use visible in
    construction and code review.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._configs: dict[AdmissionClass, AdmissionClassConfig] = {}
        self._operations: dict[uuid.UUID, OperationRecord] = {}
        self._submissions: dict[tuple[str, str, uuid.UUID], _SubmissionRecord] = {}
        self._idempotency: dict[tuple[str, str, str, str], uuid.UUID] = {}
        self._slots: dict[AdmissionClass, list[_SlotRecord]] = {}

    def configure(self, admission_classes: Sequence[AdmissionClassConfig]) -> None:
        configs = tuple(admission_classes)
        _validate_admission_graph(configs)
        with self._lock:
            self._configs = {config.class_name: config for config in configs}
            for config in configs:
                slots = self._slots.setdefault(config.class_name, [])
                for slot_number in range(len(slots) + 1, config.active_limit + 1):
                    slots.append(_SlotRecord(config.class_name, slot_number))
                self._slots[config.class_name] = [
                    slot
                    for slot in slots
                    if slot.slot_number <= config.active_limit
                    or slot.operation_id is not None
                ]

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        if now is None:
            raise ValueError("the in-memory test repository requires an injected clock")
        return _normalize_datetime(now)

    def _chain(self, class_name: AdmissionClass) -> tuple[AdmissionClass, ...]:
        if class_name not in self._configs:
            raise AdmissionConfigurationError(
                f"unknown admission class {class_name.value}"
            )
        chain: list[AdmissionClass] = []
        current: AdmissionClass | None = class_name
        while current is not None:
            chain.append(current)
            current = self._configs[current].parent_class_name
        return tuple(reversed(chain))

    @staticmethod
    def _submission_key(
        owner: OperationOwner, submission_id: uuid.UUID
    ) -> tuple[str, str, uuid.UUID]:
        scope, partition = _owner_partition(owner)
        return scope, partition, submission_id

    @staticmethod
    def _idempotency_key(request: OperationRequest) -> tuple[str, str, str, str] | None:
        if request.idempotency_namespace is None:
            return None
        scope, partition = _owner_partition(request.owner)
        return (
            scope,
            partition,
            request.idempotency_namespace,
            request.idempotency_key or "",
        )

    def _owner_matches(self, record: OperationRecord, owner: OperationOwner) -> bool:
        return _owner_partition(owner) == _owner_partition(
            OperationOwner(
                owner_scope=record.owner_scope,
                owner_user_id=record.owner_user_id,
                connection_scope_id=record.connection_scope_id,
            )
        )

    def _queue_position(self, record: OperationRecord) -> int | None:
        if record.state is not OperationState.QUEUED:
            return None
        queued = sorted(
            (
                candidate
                for candidate in self._operations.values()
                if candidate.admission_class is record.admission_class
                and candidate.state is OperationState.QUEUED
            ),
            key=lambda candidate: (candidate.accepted_at, candidate.operation_id.int),
        )
        return queued.index(record) + 1

    def _accepted(self, record: OperationRecord) -> AcceptedAdmission:
        return AcceptedAdmission(
            accepted=True,
            operation_id=record.operation_id,
            state=record.state,
            state_revision=record.state_revision,
            queue_position=self._queue_position(record),
            queue_deadline_at=record.queue_deadline_at,
        )

    @staticmethod
    def _refused(submission: _SubmissionRecord) -> RefusedAdmission:
        return RefusedAdmission(
            accepted=False,
            code=submission.refusal_code or "capacity_exceeded",
            retryable=submission.retryable,
            retry_after_ms=submission.retry_after_ms,
        )

    def _submission_result(self, submission: _SubmissionRecord) -> SubmissionResult:
        if not submission.accepted:
            return self._refused(submission)
        operation = self._operations.get(submission.operation_id)
        if operation is None:
            raise OperationNotFoundError("operation submission not found")
        return AcceptedSubmission(accepted=True, operation=_safe_projection(operation))

    def _record_submission(
        self,
        request: OperationRequest,
        *,
        now: datetime,
        retention: timedelta,
        operation_id: uuid.UUID | None = None,
        refusal_code: str | None = None,
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ) -> _SubmissionRecord:
        result = _SubmissionRecord(
            submission_result_id=uuid.uuid4(),
            submission_id=request.submission_id,
            owner=request.owner,
            accepted=operation_id is not None,
            operation_id=operation_id,
            refusal_code=refusal_code,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            observed_at=now,
            purge_after=now + retention,
        )
        self._submissions[
            self._submission_key(request.owner, request.submission_id)
        ] = result
        return result

    def _free_headroom(self, class_name: AdmissionClass) -> int:
        available = []
        for name in self._chain(class_name):
            config = self._configs[name]
            available.append(
                sum(
                    1
                    for slot in self._slots[name]
                    if slot.slot_number <= config.active_limit
                    and slot.operation_id is None
                )
            )
        return min(available)

    def _claim_free_slots_locked(
        self,
        class_name: AdmissionClass,
        operation_id: uuid.UUID,
        *,
        lease_token: uuid.UUID | None,
        lease_expires_at: datetime,
    ) -> bool:
        if self._free_headroom(class_name) <= 0:
            return False
        for name in self._chain(class_name):
            config = self._configs[name]
            slots = self._slots[name]
            for index, slot in enumerate(slots):
                if (
                    slot.slot_number <= config.active_limit
                    and slot.operation_id is None
                ):
                    slots[index] = replace(
                        slot,
                        operation_id=operation_id,
                        lease_token=lease_token or uuid.uuid4(),
                        claim_generation=slot.claim_generation + 1,
                        lease_expires_at=lease_expires_at,
                    )
                    break
            else:  # pragma: no cover - guarded by one process-wide lock
                raise RuntimeError("admission slot claim lost atomicity")
        return True

    def _is_preselected_locked(self, record: OperationRecord) -> bool:
        if (
            record.state is not OperationState.RUNNING
            or record.execution_lease_token is None
        ):
            return False
        owned_slots = [
            slot
            for slots in self._slots.values()
            for slot in slots
            if slot.operation_id == record.operation_id
        ]
        return len(owned_slots) == len(self._chain(record.admission_class)) and all(
            slot.lease_token == record.execution_lease_token for slot in owned_slots
        )

    def submit(
        self,
        request: OperationRequest,
        *,
        now: datetime | None,
        retention: timedelta,
        slot_lease: timedelta,
    ) -> AdmissionResult:
        current_time = self._now(now)
        with self._lock:
            submission = self._submissions.get(
                self._submission_key(request.owner, request.submission_id)
            )
            if submission is not None:
                if submission.accepted:
                    operation = self._operations.get(submission.operation_id)
                    if operation is None:
                        raise OperationNotFoundError("operation submission not found")
                    return self._accepted(operation)
                return self._refused(submission)

            identity_key = self._idempotency_key(request)
            if identity_key is not None and identity_key in self._idempotency:
                operation = self._operations[self._idempotency[identity_key]]
                if (
                    operation.operation_kind != request.operation_kind
                    or operation.admission_class is not request.admission_class
                    or operation.normalized_input_digest
                    != request.normalized_input_digest
                ):
                    refusal = self._record_submission(
                        request,
                        now=current_time,
                        retention=retention,
                        refusal_code="idempotency_conflict",
                        retryable=False,
                    )
                    return self._refused(refusal)
                self._record_submission(
                    request,
                    now=current_time,
                    retention=retention,
                    operation_id=operation.operation_id,
                )
                return self._accepted(operation)

            config = self._configs.get(request.admission_class)
            if config is None:
                raise AdmissionConfigurationError(
                    f"unknown admission class {request.admission_class.value}"
                )
            self._expire_queued_locked(current_time, retention)
            self._expire_execution_leases_locked(current_time, retention)
            queued_count = sum(
                operation.admission_class is request.admission_class
                and operation.state is OperationState.QUEUED
                for operation in self._operations.values()
            )
            has_active_headroom = self._free_headroom(request.admission_class) > 0
            if not has_active_headroom and queued_count >= config.queue_limit:
                retry_after_ms = max(1, min(config.max_wait_ms or 1_000, 60_000))
                refusal = self._record_submission(
                    request,
                    now=current_time,
                    retention=retention,
                    refusal_code="capacity_exceeded",
                    retryable=True,
                    retry_after_ms=retry_after_ms,
                )
                return self._refused(refusal)

            operation_id = uuid.uuid4()
            execution_token = uuid.uuid4() if has_active_headroom else None
            if not has_active_headroom and (
                config.max_wait_ms is None or config.max_wait_ms <= 0
            ):
                raise AdmissionConfigurationError(
                    f"work class {config.class_name.value} requires finite queue wait"
                )
            record = OperationRecord(
                operation_id=operation_id,
                operation_kind=request.operation_kind,
                admission_class=request.admission_class,
                owner_scope=request.owner.owner_scope,
                owner_user_id=request.owner.owner_user_id,
                connection_scope_id=request.owner.connection_scope_id,
                idempotency_namespace=request.idempotency_namespace,
                idempotency_key=request.idempotency_key,
                normalized_input_digest=request.normalized_input_digest,
                chat_id=request.chat_id,
                parent_operation_id=request.parent_operation_id,
                connection_generation=request.connection_generation,
                request_generation=request.request_generation,
                state=(
                    OperationState.RUNNING
                    if has_active_headroom
                    else OperationState.QUEUED
                ),
                phase_code=None,
                terminal_code=None,
                safe_summary=None,
                retry_after_ms=None,
                execution_generation=1 if has_active_headroom else 0,
                execution_lease_token=execution_token,
                state_revision=1 if has_active_headroom else 0,
                accepted_at=current_time,
                updated_at=current_time,
                queue_deadline_at=(
                    None
                    if has_active_headroom
                    else current_time + timedelta(milliseconds=config.max_wait_ms or 0)
                ),
                started_at=current_time if has_active_headroom else None,
                terminal_at=None,
                cancel_requested_at=None,
                purge_after=None,
            )
            self._operations[operation_id] = record
            if has_active_headroom:
                if execution_token is None:  # pragma: no cover - branch invariant
                    raise RuntimeError("preselected execution is missing its token")
                if not self._claim_free_slots_locked(
                    request.admission_class,
                    operation_id,
                    lease_token=execution_token,
                    lease_expires_at=current_time + slot_lease,
                ):
                    raise RuntimeError("preselected operation lost active capacity")
            if identity_key is not None:
                self._idempotency[identity_key] = operation_id
            self._record_submission(
                request,
                now=current_time,
                retention=retention,
                operation_id=operation_id,
            )
            return self._accepted(record)

    def _expire_queued_locked(
        self, current_time: datetime, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        expired = []
        for operation_id, record in list(self._operations.items()):
            if (
                record.state is OperationState.QUEUED
                and record.queue_deadline_at is not None
                and record.queue_deadline_at <= current_time
            ):
                terminal = replace(
                    record,
                    state=OperationState.RETRYABLE,
                    terminal_code="queue_wait_expired",
                    safe_summary="Queue wait expired",
                    retry_after_ms=1_000,
                    state_revision=record.state_revision + 1,
                    updated_at=current_time,
                    terminal_at=current_time,
                    purge_after=current_time + retention,
                )
                self._operations[operation_id] = terminal
                expired.append(terminal)
        return tuple(expired)

    def claim_next(
        self,
        class_name: AdmissionClass,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None:
        current_time = self._now(now)
        with self._lock:
            self._chain(class_name)
            self._expire_execution_leases_locked(current_time, retention)
            self._expire_queued_locked(current_time, retention)
            preselected = sorted(
                (
                    record
                    for record in self._operations.values()
                    if record.admission_class is class_name
                    and self._is_preselected_locked(record)
                ),
                key=lambda record: (record.accepted_at, record.operation_id.int),
            )
            if preselected:
                record = preselected[0]
                lease_expires_at = current_time + slot_lease
                for slots in self._slots.values():
                    for index, slot in enumerate(slots):
                        if slot.operation_id == record.operation_id:
                            slots[index] = replace(
                                slot,
                                lease_token=uuid.uuid4(),
                                claim_generation=slot.claim_generation + 1,
                                lease_expires_at=lease_expires_at,
                            )
                return OperationClaim(
                    operation=record,
                    fence=ExecutionFence(
                        operation_id=record.operation_id,
                        execution_generation=record.execution_generation,
                        execution_lease_token=record.execution_lease_token,
                    ),
                )
            candidates = sorted(
                (
                    record
                    for record in self._operations.values()
                    if record.admission_class is class_name
                    and record.state is OperationState.QUEUED
                ),
                key=lambda record: (record.accepted_at, record.operation_id.int),
            )
            if not candidates or self._free_headroom(class_name) <= 0:
                return None
            record = candidates[0]
            slot_expiry = current_time + slot_lease
            if not self._claim_free_slots_locked(
                class_name,
                record.operation_id,
                lease_token=None,
                lease_expires_at=slot_expiry,
            ):  # pragma: no cover - checked immediately above under the lock
                raise RuntimeError("admission slot claim lost atomicity")
            execution_token = uuid.uuid4()
            running = replace(
                record,
                state=OperationState.RUNNING,
                execution_generation=record.execution_generation + 1,
                execution_lease_token=execution_token,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
                started_at=record.started_at or current_time,
            )
            self._operations[record.operation_id] = running
            fence = ExecutionFence(
                operation_id=record.operation_id,
                execution_generation=running.execution_generation,
                execution_lease_token=execution_token,
            )
            return OperationClaim(operation=running, fence=fence)

    def claim_operation(
        self,
        class_name: AdmissionClass,
        operation_id: uuid.UUID,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None:
        """Consume only the named operation's origin-local handoff marker."""

        current_time = self._now(now)
        with self._lock:
            self._chain(class_name)
            self._expire_execution_leases_locked(current_time, retention)
            self._expire_queued_locked(current_time, retention)
            record = self._operations.get(operation_id)
            if record is None or record.admission_class is not class_name:
                return None

            if record.state is OperationState.RUNNING:
                if record.cancel_requested_at is not None or not self._is_preselected_locked(
                    record
                ):
                    return None
                lease_expires_at = current_time + slot_lease
                rotated = 0
                for slots in self._slots.values():
                    for index, slot in enumerate(slots):
                        if slot.operation_id == record.operation_id:
                            slots[index] = replace(
                                slot,
                                lease_token=uuid.uuid4(),
                                claim_generation=slot.claim_generation + 1,
                                lease_expires_at=lease_expires_at,
                            )
                            rotated += 1
                if rotated != len(self._chain(record.admission_class)):
                    raise RuntimeError("preselected handoff marker is incomplete")
                marker_token = record.execution_lease_token
                if marker_token is None:  # pragma: no cover - guarded above
                    raise RuntimeError("preselected execution is missing its token")
                return OperationClaim(
                    operation=record,
                    fence=ExecutionFence(
                        operation_id=record.operation_id,
                        execution_generation=record.execution_generation,
                        execution_lease_token=marker_token,
                    ),
                )

            if record.state is not OperationState.QUEUED:
                return None
            candidates = sorted(
                (
                    candidate
                    for candidate in self._operations.values()
                    if candidate.admission_class is class_name
                    and candidate.state is OperationState.QUEUED
                ),
                key=lambda candidate: (candidate.accepted_at, candidate.operation_id.int),
            )
            if (
                not candidates
                or candidates[0].operation_id != operation_id
                or self._free_headroom(class_name) <= 0
            ):
                return None
            lease_expires_at = current_time + slot_lease
            if not self._claim_free_slots_locked(
                class_name,
                operation_id,
                lease_token=None,
                lease_expires_at=lease_expires_at,
            ):  # pragma: no cover - checked immediately above under the lock
                raise RuntimeError("admission slot claim lost atomicity")
            execution_token = uuid.uuid4()
            running = replace(
                record,
                state=OperationState.RUNNING,
                execution_generation=record.execution_generation + 1,
                execution_lease_token=execution_token,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
                started_at=record.started_at or current_time,
            )
            self._operations[operation_id] = running
            return OperationClaim(
                operation=running,
                fence=ExecutionFence(
                    operation_id=operation_id,
                    execution_generation=running.execution_generation,
                    execution_lease_token=execution_token,
                ),
            )

    def inspect_admission_class(
        self, class_name: AdmissionClass, *, now: datetime | None
    ) -> AdmissionClassStatus:
        self._now(now)
        with self._lock:
            config = self._configs.get(class_name)
            if config is None:
                raise AdmissionConfigurationError(
                    f"unknown admission class {class_name.value}"
                )
            active = [
                operation
                for operation in self._operations.values()
                if operation.state is OperationState.RUNNING
                and any(
                    slot.operation_id == operation.operation_id
                    for slot in self._slots[class_name]
                )
            ]
            queued = [
                operation
                for operation in self._operations.values()
                if operation.admission_class is class_name
                and operation.state is OperationState.QUEUED
            ]
            return AdmissionClassStatus(
                class_name=class_name,
                parent_class_name=config.parent_class_name,
                active_limit=config.active_limit,
                queue_limit=config.queue_limit,
                max_wait_ms=config.max_wait_ms,
                active_count=len(active),
                queued_count=len(queued),
                oldest_queued_at=min(
                    (operation.accepted_at for operation in queued), default=None
                ),
                oldest_running_at=min(
                    (
                        operation.started_at
                        for operation in active
                        if operation.started_at
                    ),
                    default=None,
                ),
            )

    def query_operation(
        self, owner: OperationOwner, operation_id: uuid.UUID
    ) -> SafeOperationProjection:
        with self._lock:
            record = self._operations.get(operation_id)
            if record is None or not self._owner_matches(record, owner):
                raise OperationNotFoundError("operation not found")
            return _safe_projection(record)

    def reconcile_submission(
        self, owner: OperationOwner, submission_id: uuid.UUID
    ) -> SubmissionResult:
        with self._lock:
            submission = self._submissions.get(
                self._submission_key(owner, submission_id)
            )
            if submission is None:
                raise OperationNotFoundError("operation submission not found")
            return self._submission_result(submission)

    def _release_slots_locked(self, operation_id: uuid.UUID) -> None:
        for class_name, slots in self._slots.items():
            for index, slot in enumerate(slots):
                if slot.operation_id == operation_id:
                    slots[index] = replace(
                        slot,
                        operation_id=None,
                        lease_token=None,
                        claim_generation=slot.claim_generation + 1,
                        lease_expires_at=None,
                    )
            config = self._configs[class_name]
            self._slots[class_name] = [
                slot
                for slot in slots
                if slot.slot_number <= config.active_limit
                or slot.operation_id is not None
            ]

    def cancel(
        self,
        owner: OperationOwner,
        operation_id: uuid.UUID,
        terminal_code: str,
        *,
        now: datetime | None,
        retention: timedelta,
        request_running: bool = True,
        transaction: Any | None = None,
    ) -> OperationRecord:
        del transaction  # PostgreSQL cursors are not meaningful to this test double.
        current_time = self._now(now)
        with self._lock:
            record = self._operations.get(operation_id)
            if record is None or not self._owner_matches(record, owner):
                raise OperationNotFoundError("operation not found")
            if (
                record.state in _TERMINAL_STATES
                or record.cancel_requested_at is not None
            ):
                return record
            if record.state is OperationState.QUEUED or self._is_preselected_locked(
                record
            ):
                cancelled = replace(
                    record,
                    state=OperationState.CANCELLED,
                    terminal_code=terminal_code,
                    safe_summary="Cancelled",
                    state_revision=record.state_revision + 1,
                    updated_at=current_time,
                    cancel_requested_at=current_time,
                    terminal_at=current_time,
                    purge_after=current_time + retention,
                    execution_lease_token=None,
                )
                self._operations[operation_id] = cancelled
                self._release_slots_locked(operation_id)
                return cancelled
            if not request_running:
                return record
            requested = replace(
                record,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
                cancel_requested_at=current_time,
            )
            self._operations[operation_id] = requested
            return requested

    def terminalize_unselected(
        self,
        operation_id: uuid.UUID,
        *,
        terminal_code: str,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
    ) -> OperationRecord | None:
        current_time = self._now(now)
        with self._lock:
            record = self._operations.get(operation_id)
            if record is None:
                return None
            if record.state in _TERMINAL_STATES:
                return record
            if record.state is not OperationState.QUEUED and not (
                record.cancel_requested_at is None
                and self._is_preselected_locked(record)
            ):
                return None
            terminal = replace(
                record,
                state=OperationState.RETRYABLE,
                terminal_code=terminal_code,
                safe_summary=safe_summary,
                retry_after_ms=retry_after_ms,
                execution_lease_token=None,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
                terminal_at=current_time,
                purge_after=current_time + retention,
            )
            self._operations[operation_id] = terminal
            self._release_slots_locked(operation_id)
            return terminal

    @staticmethod
    def _fence_matches(record: OperationRecord, fence: ExecutionFence) -> bool:
        return (
            record.state is OperationState.RUNNING
            and record.execution_generation == fence.execution_generation
            and record.execution_lease_token == fence.execution_lease_token
        )

    def terminalize(
        self,
        fence: ExecutionFence,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
        transaction: Any | None = None,
    ) -> OperationRecord:
        del transaction  # PostgreSQL cursors are not meaningful to this test double.
        current_time = self._now(now)
        with self._lock:
            record = self._operations.get(fence.operation_id)
            if record is None:
                raise StaleExecutionFenceError("execution fence is stale")
            if record.state in _TERMINAL_STATES:
                return record
            if not self._fence_matches(record, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            terminal = replace(
                record,
                state=state,
                terminal_code=terminal_code,
                safe_summary=safe_summary,
                retry_after_ms=retry_after_ms,
                execution_lease_token=None,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
                terminal_at=current_time,
                purge_after=current_time + retention,
            )
            self._operations[record.operation_id] = terminal
            self._release_slots_locked(record.operation_id)
            return terminal

    def expire_queued(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        current_time = self._now(now)
        with self._lock:
            return self._expire_queued_locked(current_time, retention)

    def assert_current_execution(
        self, fence: ExecutionFence, *, transaction: Any | None = None
    ) -> OperationRecord:
        del transaction
        with self._lock:
            record = self._operations.get(fence.operation_id)
            if record is None or not self._fence_matches(record, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            return record

    def reselect_execution(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> ExecutionFence:
        current_time = self._now(now)
        with self._lock:
            record = self._operations.get(fence.operation_id)
            if record is None or not self._fence_matches(record, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            token = uuid.uuid4()
            selected = replace(
                record,
                execution_generation=record.execution_generation + 1,
                execution_lease_token=token,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
            )
            self._operations[record.operation_id] = selected
            lease_expires_at = current_time + slot_lease
            for slots in self._slots.values():
                for index, slot in enumerate(slots):
                    if slot.operation_id == record.operation_id:
                        slots[index] = replace(
                            slot,
                            lease_token=uuid.uuid4(),
                            claim_generation=slot.claim_generation + 1,
                            lease_expires_at=lease_expires_at,
                        )
            return ExecutionFence(
                record.operation_id, selected.execution_generation, token
            )

    def update_phase(
        self,
        fence: ExecutionFence,
        phase_code: str,
        *,
        now: datetime | None,
    ) -> OperationRecord:
        current_time = self._now(now)
        with self._lock:
            record = self._operations.get(fence.operation_id)
            if record is None or not self._fence_matches(record, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            if record.phase_code == phase_code:
                return record
            updated = replace(
                record,
                phase_code=phase_code,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
            )
            self._operations[record.operation_id] = updated
            return updated

    def renew_execution_lease(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> SlotLeaseRenewal:
        current_time = self._now(now)
        with self._lock:
            record = self._operations.get(fence.operation_id)
            if record is None or not self._fence_matches(record, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            expires = current_time + slot_lease
            found = False
            for slots in self._slots.values():
                for index, slot in enumerate(slots):
                    if slot.operation_id == record.operation_id:
                        found = True
                        slots[index] = replace(
                            slot,
                            lease_token=uuid.uuid4(),
                            claim_generation=slot.claim_generation + 1,
                            lease_expires_at=expires,
                        )
            if not found:
                raise StaleExecutionFenceError("execution capacity lease is missing")
            return SlotLeaseRenewal(
                operation_id=record.operation_id,
                execution_generation=record.execution_generation,
                lease_expires_at=expires,
            )

    def expire_execution_leases(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        current_time = self._now(now)
        with self._lock:
            return self._expire_execution_leases_locked(current_time, retention)

    def _expire_execution_leases_locked(
        self, current_time: datetime, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        expired_ids = {
            slot.operation_id
            for slots in self._slots.values()
            for slot in slots
            if slot.operation_id is not None
            and slot.lease_expires_at is not None
            and slot.lease_expires_at <= current_time
        }
        expired = []
        for operation_id in sorted(expired_ids, key=lambda value: value.int):
            record = self._operations.get(operation_id)
            if record is None or record.state is not OperationState.RUNNING:
                self._release_slots_locked(operation_id)
                continue
            terminal = replace(
                record,
                state=OperationState.RETRYABLE,
                terminal_code="execution_lease_expired",
                safe_summary="Execution lease expired",
                retry_after_ms=1_000,
                execution_lease_token=None,
                state_revision=record.state_revision + 1,
                updated_at=current_time,
                terminal_at=current_time,
                purge_after=current_time + retention,
            )
            self._operations[operation_id] = terminal
            self._release_slots_locked(operation_id)
            expired.append(terminal)
        return tuple(expired)

    def purge_expired(
        self,
        *,
        now: datetime | None,
        limit: int,
        fence: ExecutionFence | None = None,
    ) -> PurgeResult:
        current_time = self._now(now)
        with self._lock:
            if fence is not None:
                record = self._operations.get(fence.operation_id)
                if record is None or not self._fence_matches(record, fence):
                    raise StaleExecutionFenceError("execution fence is stale")
            submission_keys = []
            for key, submission in sorted(
                self._submissions.items(), key=lambda item: item[1].observed_at
            ):
                if (
                    len(submission_keys) >= limit
                    or submission.purge_after >= current_time
                ):
                    continue
                operation = (
                    self._operations.get(submission.operation_id)
                    if submission.operation_id is not None
                    else None
                )
                if submission.accepted and (
                    operation is not None
                    and (
                        operation.state not in _TERMINAL_STATES
                        or operation.purge_after is None
                        or operation.purge_after >= current_time
                    )
                ):
                    continue
                submission_keys.append(key)
            for key in submission_keys:
                self._submissions.pop(key, None)

            operation_ids = []
            for operation in sorted(
                self._operations.values(),
                key=lambda record: (
                    record.purge_after or datetime.max.replace(tzinfo=UTC)
                ),
            ):
                if len(operation_ids) >= limit:
                    break
                if (
                    operation.state not in _TERMINAL_STATES
                    or operation.purge_after is None
                    or operation.purge_after >= current_time
                ):
                    continue
                if any(
                    submission.accepted
                    and submission.operation_id == operation.operation_id
                    for submission in self._submissions.values()
                ):
                    continue
                operation_ids.append(operation.operation_id)
            for operation_id in operation_ids:
                operation = self._operations.pop(operation_id)
                if operation.idempotency_namespace is not None:
                    key = (
                        *_owner_partition(
                            OperationOwner(
                                operation.owner_scope,
                                operation.owner_user_id,
                                operation.connection_scope_id,
                            )
                        ),
                        operation.idempotency_namespace,
                        operation.idempotency_key or "",
                    )
                    self._idempotency.pop(key, None)
            return PurgeResult(
                operations=len(operation_ids), submissions=len(submission_keys)
            )

    @contextmanager
    def fenced_transaction(self, fence: ExecutionFence) -> Iterator[object]:
        with self._lock:
            self.assert_current_execution(fence)
            yield self


class PostgresWorkAdmissionRepository:
    """PostgreSQL authority for admission, lifecycle, and commit fencing.

    Every multi-row transition uses one borrowed connection and one explicit
    transaction.  When no test clock is supplied, the transaction timestamp
    comes from PostgreSQL, so replicas cannot disagree because of host-clock
    skew.  Submission and idempotency identities also take stable advisory
    transaction locks before their unique rows are inspected.
    """

    def __init__(self, database: Any) -> None:
        if database is None or not callable(getattr(database, "_get_connection", None)):
            raise TypeError("database must provide _get_connection()")
        self._database = database
        self._configuration_lock = threading.RLock()
        self._configs: dict[AdmissionClass, AdmissionClassConfig] = {}

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._database._get_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

    @contextmanager
    def _transaction_or_cursor(self, transaction: Any | None) -> Iterator[Any]:
        """Reuse a trusted caller transaction without committing it here."""

        if transaction is not None:
            yield transaction
            return
        with self._transaction() as cursor:
            yield cursor

    def load_existing_configs(self) -> tuple[AdmissionClassConfig, ...]:
        """Atomically bind persisted class rows without an UPDATE/UPSERT."""

        with self._configuration_lock:
            with self._transaction() as cursor:
                cursor.execute(
                    """
                    SELECT class_name, parent_class_name, active_limit,
                           queue_limit, max_wait_ms, config_revision
                    FROM operation_admission_class
                    ORDER BY CASE WHEN parent_class_name IS NULL THEN 0 ELSE 1 END,
                             class_name
                    FOR SHARE
                    """
                )
                configs = _admission_configs_from_rows(cursor.fetchall())
                # Bind while shared row locks remain held. A concurrent
                # operator update can proceed only after this complete snapshot
                # is installed, and startup never writes the snapshot back.
                self._configs = {
                    config.class_name: config for config in configs
                }
                return configs

    @staticmethod
    def _current_time(cursor: Any, now: datetime | None) -> datetime:
        if now is not None:
            return _normalize_datetime(now)
        cursor.execute("SELECT CURRENT_TIMESTAMP AS current_time")
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL did not return its transaction timestamp")
        return _normalize_datetime(row["current_time"])

    @staticmethod
    def _advisory_identity(*parts: object) -> int:
        digest = hashlib.sha256()
        for part in parts:
            encoded = str(part).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return int.from_bytes(digest.digest()[:8], "big", signed=True)

    @classmethod
    def _lock_request_identities(cls, cursor: Any, request: OperationRequest) -> None:
        scope, partition = _owner_partition(request.owner)
        lock_ids = {
            cls._advisory_identity(
                "operation-submission", scope, partition, request.submission_id
            )
        }
        if request.idempotency_namespace is not None:
            lock_ids.add(
                cls._advisory_identity(
                    "operation-idempotency",
                    scope,
                    partition,
                    request.idempotency_namespace,
                    request.idempotency_key,
                )
            )
        for lock_id in sorted(lock_ids):
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))

    @staticmethod
    def _owner_clause(
        owner: OperationOwner, *, alias: str = ""
    ) -> tuple[str, tuple[Any, ...]]:
        prefix = f"{alias}." if alias else ""
        if owner.owner_scope is OwnerScope.CONNECTION:
            return (
                f"{prefix}owner_scope = %s AND {prefix}connection_scope_id = %s",
                (owner.owner_scope.value, str(owner.connection_scope_id)),
            )
        if owner.owner_scope in {OwnerScope.USER, OwnerScope.SCHEDULE}:
            return (
                f"{prefix}owner_scope = %s AND {prefix}owner_user_id = %s",
                (owner.owner_scope.value, owner.owner_user_id),
            )
        return f"{prefix}owner_scope = %s", (owner.owner_scope.value,)

    @staticmethod
    def _uuid(value: Any) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    @classmethod
    def _operation_from_row(cls, row: Mapping[str, Any]) -> OperationRecord:
        return OperationRecord(
            operation_id=cls._uuid(row["operation_id"]),  # type: ignore[arg-type]
            operation_kind=str(row["operation_kind"]),
            admission_class=AdmissionClass(row["admission_class"]),
            owner_scope=OwnerScope(row["owner_scope"]),
            owner_user_id=row["owner_user_id"],
            connection_scope_id=cls._uuid(row["connection_scope_id"]),
            idempotency_namespace=row["idempotency_namespace"],
            idempotency_key=row["idempotency_key"],
            normalized_input_digest=(
                str(row["normalized_input_digest"])
                if row["normalized_input_digest"] is not None
                else None
            ),
            chat_id=row["chat_id"],
            parent_operation_id=cls._uuid(row["parent_operation_id"]),
            connection_generation=cls._uuid(row["connection_generation"]),
            request_generation=cls._uuid(row["request_generation"]),
            state=OperationState(row["state"]),
            phase_code=row["phase_code"],
            terminal_code=row["terminal_code"],
            safe_summary=row["safe_summary"],
            retry_after_ms=row["retry_after_ms"],
            execution_generation=int(row["execution_generation"]),
            execution_lease_token=cls._uuid(row["execution_lease_token"]),
            state_revision=int(row["state_revision"]),
            accepted_at=_normalize_datetime(row["accepted_at"]),
            updated_at=_normalize_datetime(row["updated_at"]),
            queue_deadline_at=(
                _normalize_datetime(row["queue_deadline_at"])
                if row["queue_deadline_at"] is not None
                else None
            ),
            started_at=(
                _normalize_datetime(row["started_at"])
                if row["started_at"] is not None
                else None
            ),
            terminal_at=(
                _normalize_datetime(row["terminal_at"])
                if row["terminal_at"] is not None
                else None
            ),
            cancel_requested_at=(
                _normalize_datetime(row["cancel_requested_at"])
                if row["cancel_requested_at"] is not None
                else None
            ),
            purge_after=(
                _normalize_datetime(row["purge_after"])
                if row["purge_after"] is not None
                else None
            ),
        )

    @staticmethod
    def _configuration_order(
        configs: Sequence[AdmissionClassConfig],
    ) -> tuple[AdmissionClassConfig, ...]:
        by_name = {config.class_name: config for config in configs}
        ordered: list[AdmissionClassConfig] = []

        def visit(config: AdmissionClassConfig) -> None:
            parent = config.parent_class_name
            if parent is not None and by_name[parent] not in ordered:
                visit(by_name[parent])
            if config not in ordered:
                ordered.append(config)

        for config in configs:
            visit(config)
        return tuple(ordered)

    def configure(self, admission_classes: Sequence[AdmissionClassConfig]) -> None:
        configs = tuple(admission_classes)
        _validate_admission_graph(configs)
        ordered = self._configuration_order(configs)
        with self._configuration_lock:
            with self._transaction() as cursor:
                for config in ordered:
                    cursor.execute(
                        """
                    INSERT INTO operation_admission_class (
                        class_name, parent_class_name, active_limit, queue_limit,
                        max_wait_ms, config_revision, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (class_name) DO UPDATE SET
                        parent_class_name = EXCLUDED.parent_class_name,
                        active_limit = EXCLUDED.active_limit,
                        queue_limit = EXCLUDED.queue_limit,
                        max_wait_ms = EXCLUDED.max_wait_ms,
                        config_revision = EXCLUDED.config_revision,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                        (
                            config.class_name.value,
                            config.parent_class_name.value
                            if config.parent_class_name is not None
                            else None,
                            config.active_limit,
                            config.queue_limit,
                            config.max_wait_ms or 0,
                            config.config_revision,
                        ),
                    )
                    cursor.execute(
                        """
                    INSERT INTO operation_admission_slot (class_name, slot_number)
                    SELECT %s, generate_series(1, %s)
                    ON CONFLICT (class_name, slot_number) DO NOTHING
                    """,
                        (config.class_name.value, config.active_limit),
                    )
                    cursor.execute(
                        """
                    DELETE FROM operation_admission_slot
                    WHERE class_name = %s AND slot_number > %s
                      AND operation_id IS NULL
                    """,
                        (config.class_name.value, config.active_limit),
                    )
            self._configs = {config.class_name: config for config in configs}

    def _chain(self, class_name: AdmissionClass) -> tuple[AdmissionClass, ...]:
        with self._configuration_lock:
            if class_name not in self._configs:
                raise AdmissionConfigurationError(
                    f"unknown admission class {class_name.value}"
                )
            chain: list[AdmissionClass] = []
            current: AdmissionClass | None = class_name
            while current is not None:
                chain.append(current)
                current = self._configs[current].parent_class_name
        return tuple(reversed(chain))

    def _lock_class_chain(self, cursor: Any, class_name: AdmissionClass) -> None:
        for member in self._chain(class_name):
            cursor.execute(
                """
                SELECT class_name FROM operation_admission_class
                WHERE class_name = %s FOR UPDATE
                """,
                (member.value,),
            )
            if cursor.fetchone() is None:
                raise AdmissionConfigurationError(
                    f"unknown admission class {member.value}"
                )

    def _submission_row(
        self,
        cursor: Any,
        owner: OperationOwner,
        submission_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Mapping[str, Any] | None:
        owner_sql, owner_params = self._owner_clause(owner)
        cursor.execute(
            f"""
            SELECT * FROM operation_submission_result
            WHERE submission_id = %s AND {owner_sql}
            {"FOR UPDATE" if lock else ""}
            """,
            (str(submission_id), *owner_params),
        )
        return cursor.fetchone()

    @staticmethod
    def _refusal_from_row(row: Mapping[str, Any]) -> RefusedAdmission:
        return RefusedAdmission(
            accepted=False,
            code=str(row["refusal_code"]),
            retryable=bool(row["retryable"]),
            retry_after_ms=row["retry_after_ms"],
        )

    def _operation_row(
        self, cursor: Any, operation_id: uuid.UUID, *, lock: bool = False
    ) -> Mapping[str, Any] | None:
        cursor.execute(
            f"SELECT * FROM operation_record WHERE operation_id = %s "
            f"{'FOR UPDATE' if lock else ''}",
            (str(operation_id),),
        )
        return cursor.fetchone()

    def _queue_position(self, cursor: Any, operation: OperationRecord) -> int | None:
        if operation.state is not OperationState.QUEUED:
            return None
        cursor.execute(
            """
            SELECT COUNT(*) AS queue_position
            FROM operation_record
            WHERE admission_class = %s AND state = 'queued'
              AND (
                  accepted_at < %s
                  OR (accepted_at = %s AND operation_id <= %s)
              )
            """,
            (
                operation.admission_class.value,
                operation.accepted_at,
                operation.accepted_at,
                str(operation.operation_id),
            ),
        )
        row = cursor.fetchone()
        return int(row["queue_position"]) if row is not None else None

    def _accepted(self, cursor: Any, operation: OperationRecord) -> AcceptedAdmission:
        return AcceptedAdmission(
            accepted=True,
            operation_id=operation.operation_id,
            state=operation.state,
            state_revision=operation.state_revision,
            queue_position=self._queue_position(cursor, operation),
            queue_deadline_at=operation.queue_deadline_at,
        )

    @staticmethod
    def _insert_submission(
        cursor: Any,
        request: OperationRequest,
        *,
        current_time: datetime,
        retention: timedelta,
        operation_id: uuid.UUID | None = None,
        refusal_code: str | None = None,
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO operation_submission_result (
                submission_result_id, submission_id, owner_scope, owner_user_id,
                connection_scope_id, accepted, operation_id, refusal_code,
                retryable, retry_after_ms, observed_at, purge_after
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                str(request.submission_id),
                request.owner.owner_scope.value,
                request.owner.owner_user_id,
                str(request.owner.connection_scope_id)
                if request.owner.connection_scope_id is not None
                else None,
                operation_id is not None,
                str(operation_id) if operation_id is not None else None,
                refusal_code,
                retryable,
                retry_after_ms,
                current_time,
                current_time + retention,
            ),
        )

    def _existing_idempotent_operation(
        self, cursor: Any, request: OperationRequest
    ) -> Mapping[str, Any] | None:
        if request.idempotency_namespace is None:
            return None
        owner_sql, owner_params = self._owner_clause(request.owner)
        cursor.execute(
            f"""
            SELECT * FROM operation_record
            WHERE {owner_sql}
              AND idempotency_namespace = %s AND idempotency_key = %s
            FOR UPDATE
            """,
            (
                *owner_params,
                request.idempotency_namespace,
                request.idempotency_key,
            ),
        )
        return cursor.fetchone()

    def _select_free_slots(
        self, cursor: Any, class_name: AdmissionClass
    ) -> list[tuple[AdmissionClass, int]] | None:
        with self._configuration_lock:
            configs = dict(self._configs)
        selected: list[tuple[AdmissionClass, int]] = []
        for member in self._chain(class_name):
            cursor.execute(
                """
                SELECT slot_number FROM operation_admission_slot
                WHERE class_name = %s AND slot_number <= %s
                  AND operation_id IS NULL
                ORDER BY slot_number
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (member.value, configs[member].active_limit),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            selected.append((member, int(row["slot_number"])))
        return selected

    @staticmethod
    def _occupy_slots(
        cursor: Any,
        selected: Sequence[tuple[AdmissionClass, int]],
        *,
        operation_id: uuid.UUID,
        lease_token: uuid.UUID | None,
        lease_expires_at: datetime,
    ) -> None:
        for member, slot_number in selected:
            cursor.execute(
                """
                UPDATE operation_admission_slot
                SET operation_id = %s,
                    lease_token = %s,
                    claim_generation = claim_generation + 1,
                    lease_expires_at = %s
                WHERE class_name = %s AND slot_number = %s
                  AND operation_id IS NULL
                """,
                (
                    str(operation_id),
                    str(lease_token or uuid.uuid4()),
                    lease_expires_at,
                    member.value,
                    slot_number,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("admission slot claim lost atomicity")

    def submit(
        self,
        request: OperationRequest,
        *,
        now: datetime | None,
        retention: timedelta,
        slot_lease: timedelta,
    ) -> AdmissionResult:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            self._lock_request_identities(cursor, request)
            existing_submission = self._submission_row(
                cursor, request.owner, request.submission_id, lock=True
            )
            if existing_submission is not None:
                if not existing_submission["accepted"]:
                    return self._refusal_from_row(existing_submission)
                operation_id = self._uuid(existing_submission["operation_id"])
                if operation_id is None:
                    raise OperationNotFoundError("operation submission not found")
                operation_row = self._operation_row(cursor, operation_id)
                if operation_row is None:
                    raise OperationNotFoundError("operation submission not found")
                return self._accepted(cursor, self._operation_from_row(operation_row))

            idempotent_row = self._existing_idempotent_operation(cursor, request)
            if idempotent_row is not None:
                operation = self._operation_from_row(idempotent_row)
                if (
                    operation.operation_kind != request.operation_kind
                    or operation.admission_class is not request.admission_class
                    or operation.normalized_input_digest
                    != request.normalized_input_digest
                ):
                    self._insert_submission(
                        cursor,
                        request,
                        current_time=current_time,
                        retention=retention,
                        refusal_code="idempotency_conflict",
                    )
                    return RefusedAdmission(False, "idempotency_conflict", False, None)
                self._insert_submission(
                    cursor,
                    request,
                    current_time=current_time,
                    retention=retention,
                    operation_id=operation.operation_id,
                )
                return self._accepted(cursor, operation)

            with self._configuration_lock:
                config = self._configs.get(request.admission_class)
            if config is None:
                raise AdmissionConfigurationError(
                    f"unknown admission class {request.admission_class.value}"
                )
            self._lock_class_chain(cursor, request.admission_class)
            self._expire_queued_locked(cursor, current_time, retention)
            self._expire_execution_leases_locked(cursor, current_time, retention)
            selected_slots = self._select_free_slots(cursor, request.admission_class)
            cursor.execute(
                """
                SELECT COUNT(*) AS queued_count FROM operation_record
                WHERE admission_class = %s AND state = 'queued'
                """,
                (request.admission_class.value,),
            )
            queue_row = cursor.fetchone()
            queued_count = int(queue_row["queued_count"]) if queue_row else 0
            if selected_slots is None and queued_count >= config.queue_limit:
                retry_after_ms = max(1, min(config.max_wait_ms or 1_000, 60_000))
                self._insert_submission(
                    cursor,
                    request,
                    current_time=current_time,
                    retention=retention,
                    refusal_code="capacity_exceeded",
                    retryable=True,
                    retry_after_ms=retry_after_ms,
                )
                return RefusedAdmission(
                    False, "capacity_exceeded", True, retry_after_ms
                )

            if selected_slots is None and (
                config.max_wait_ms is None or config.max_wait_ms <= 0
            ):
                raise AdmissionConfigurationError(
                    f"work class {config.class_name.value} requires finite queue wait"
                )
            operation_id = uuid.uuid4()
            execution_token = uuid.uuid4() if selected_slots is not None else None
            state = (
                OperationState.RUNNING
                if selected_slots is not None
                else OperationState.QUEUED
            )
            queue_deadline = (
                None
                if selected_slots is not None
                else current_time + timedelta(milliseconds=config.max_wait_ms or 0)
            )
            cursor.execute(
                """
                INSERT INTO operation_record (
                    operation_id, operation_kind, admission_class, owner_scope,
                    owner_user_id, connection_scope_id, idempotency_namespace,
                    idempotency_key, normalized_input_digest, chat_id,
                    parent_operation_id, connection_generation, request_generation,
                    state, execution_generation, execution_lease_token,
                    state_revision, accepted_at, updated_at, queue_deadline_at,
                    started_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    str(operation_id),
                    request.operation_kind,
                    request.admission_class.value,
                    request.owner.owner_scope.value,
                    request.owner.owner_user_id,
                    str(request.owner.connection_scope_id)
                    if request.owner.connection_scope_id is not None
                    else None,
                    request.idempotency_namespace,
                    request.idempotency_key,
                    request.normalized_input_digest,
                    request.chat_id,
                    str(request.parent_operation_id)
                    if request.parent_operation_id is not None
                    else None,
                    str(request.connection_generation)
                    if request.connection_generation is not None
                    else None,
                    str(request.request_generation)
                    if request.request_generation is not None
                    else None,
                    state.value,
                    1 if selected_slots is not None else 0,
                    str(execution_token) if execution_token is not None else None,
                    1 if selected_slots is not None else 0,
                    current_time,
                    current_time,
                    queue_deadline,
                    current_time if selected_slots is not None else None,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("accepted operation insert returned no record")
            if selected_slots is not None:
                if execution_token is None:  # pragma: no cover - branch invariant
                    raise RuntimeError("preselected execution is missing its token")
                self._occupy_slots(
                    cursor,
                    selected_slots,
                    operation_id=operation_id,
                    lease_token=execution_token,
                    lease_expires_at=current_time + slot_lease,
                )
            self._insert_submission(
                cursor,
                request,
                current_time=current_time,
                retention=retention,
                operation_id=operation_id,
            )
            return self._accepted(cursor, self._operation_from_row(row))

    @staticmethod
    def _expire_queued_locked(
        cursor: Any, current_time: datetime, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        cursor.execute(
            """
            UPDATE operation_record
            SET state = 'retryable',
                terminal_code = 'queue_wait_expired',
                safe_summary = 'Queue wait expired',
                retry_after_ms = 1000,
                state_revision = state_revision + 1,
                updated_at = %s,
                terminal_at = %s,
                purge_after = %s
            WHERE state = 'queued' AND queue_deadline_at <= %s
            RETURNING *
            """,
            (
                current_time,
                current_time,
                current_time + retention,
                current_time,
            ),
        )
        return tuple(
            PostgresWorkAdmissionRepository._operation_from_row(row)
            for row in cursor.fetchall()
        )

    def claim_next(
        self,
        class_name: AdmissionClass,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            self._lock_class_chain(cursor, class_name)
            self._expire_execution_leases_locked(cursor, current_time, retention)
            self._expire_queued_locked(cursor, current_time, retention)
            cursor.execute(
                """
                SELECT operation.*
                FROM operation_record AS operation
                JOIN operation_admission_slot AS marker
                  ON marker.operation_id = operation.operation_id
                 AND marker.class_name = operation.admission_class
                 AND marker.lease_token = operation.execution_lease_token
                WHERE operation.admission_class = %s
                  AND operation.state = 'running'
                  AND operation.cancel_requested_at IS NULL
                ORDER BY operation.accepted_at, operation.operation_id
                FOR UPDATE OF operation, marker SKIP LOCKED
                LIMIT 1
                """,
                (class_name.value,),
            )
            preselected_row = cursor.fetchone()
            if preselected_row is not None:
                operation = self._operation_from_row(preselected_row)
                marker_token = operation.execution_lease_token
                if marker_token is None:  # pragma: no cover - SQL predicate invariant
                    raise RuntimeError("preselected execution is missing its token")
                cursor.execute(
                    """
                    UPDATE operation_admission_slot
                    SET lease_token = %s,
                        claim_generation = claim_generation + 1,
                        lease_expires_at = %s
                    WHERE operation_id = %s AND lease_token = %s
                    """,
                    (
                        str(uuid.uuid4()),
                        current_time + slot_lease,
                        str(operation.operation_id),
                        str(marker_token),
                    ),
                )
                if cursor.rowcount != len(self._chain(operation.admission_class)):
                    raise RuntimeError("preselected handoff marker is incomplete")
                return OperationClaim(
                    operation=operation,
                    fence=ExecutionFence(
                        operation_id=operation.operation_id,
                        execution_generation=operation.execution_generation,
                        execution_lease_token=marker_token,
                    ),
                )
            cursor.execute(
                """
                SELECT * FROM operation_record
                WHERE admission_class = %s AND state = 'queued'
                ORDER BY accepted_at, operation_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (class_name.value,),
            )
            candidate = cursor.fetchone()
            if candidate is None:
                return None

            claimed_slots = self._select_free_slots(cursor, class_name)
            if claimed_slots is None:
                return None

            operation_id = self._uuid(candidate["operation_id"])
            if operation_id is None:
                raise RuntimeError("queued operation has no identity")
            lease_expires_at = current_time + slot_lease
            self._occupy_slots(
                cursor,
                claimed_slots,
                operation_id=operation_id,
                lease_token=None,
                lease_expires_at=lease_expires_at,
            )

            execution_token = uuid.uuid4()
            cursor.execute(
                """
                UPDATE operation_record
                SET state = 'running',
                    execution_generation = execution_generation + 1,
                    execution_lease_token = %s,
                    state_revision = state_revision + 1,
                    updated_at = %s,
                    started_at = COALESCE(started_at, %s)
                WHERE operation_id = %s AND state = 'queued'
                RETURNING *
                """,
                (
                    str(execution_token),
                    current_time,
                    current_time,
                    str(operation_id),
                ),
            )
            running_row = cursor.fetchone()
            if running_row is None:
                raise RuntimeError("operation selection lost atomicity")
            operation = self._operation_from_row(running_row)
            return OperationClaim(
                operation=operation,
                fence=ExecutionFence(
                    operation_id=operation.operation_id,
                    execution_generation=operation.execution_generation,
                    execution_lease_token=execution_token,
                ),
            )

    def claim_operation(
        self,
        class_name: AdmissionClass,
        operation_id: uuid.UUID,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None:
        """Claim one exact preselected/FIFO operation under the class lock."""

        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            self._lock_class_chain(cursor, class_name)
            self._expire_execution_leases_locked(cursor, current_time, retention)
            self._expire_queued_locked(cursor, current_time, retention)

            # A submit that found headroom has already selected durable slots.
            # The child-class slot still carries the operation token only until
            # one worker consumes this handoff.  Addressing it by ID prevents a
            # local submitter from accidentally taking an older operation.
            cursor.execute(
                """
                SELECT operation.*
                FROM operation_record AS operation
                JOIN operation_admission_slot AS marker
                  ON marker.operation_id = operation.operation_id
                 AND marker.class_name = operation.admission_class
                 AND marker.lease_token = operation.execution_lease_token
                WHERE operation.operation_id = %s
                  AND operation.admission_class = %s
                  AND operation.state = 'running'
                  AND operation.cancel_requested_at IS NULL
                FOR UPDATE OF operation, marker
                """,
                (str(operation_id), class_name.value),
            )
            preselected_row = cursor.fetchone()
            if preselected_row is not None:
                operation = self._operation_from_row(preselected_row)
                marker_token = operation.execution_lease_token
                if marker_token is None:  # pragma: no cover - SQL invariant
                    raise RuntimeError("preselected execution is missing its token")
                cursor.execute(
                    """
                    UPDATE operation_admission_slot
                    SET lease_token = %s,
                        claim_generation = claim_generation + 1,
                        lease_expires_at = %s
                    WHERE operation_id = %s AND lease_token = %s
                    """,
                    (
                        str(uuid.uuid4()),
                        current_time + slot_lease,
                        str(operation_id),
                        str(marker_token),
                    ),
                )
                if cursor.rowcount != len(self._chain(class_name)):
                    raise RuntimeError("preselected handoff marker is incomplete")
                return OperationClaim(
                    operation=operation,
                    fence=ExecutionFence(
                        operation_id=operation.operation_id,
                        execution_generation=operation.execution_generation,
                        execution_lease_token=marker_token,
                    ),
                )

            cursor.execute(
                """
                SELECT * FROM operation_record
                WHERE operation_id = %s AND admission_class = %s
                FOR UPDATE
                """,
                (str(operation_id), class_name.value),
            )
            candidate = cursor.fetchone()
            if candidate is None or candidate["state"] != OperationState.QUEUED.value:
                return None

            cursor.execute(
                """
                SELECT operation_id FROM operation_record
                WHERE admission_class = %s AND state = 'queued'
                ORDER BY accepted_at, operation_id
                LIMIT 1
                """,
                (class_name.value,),
            )
            head = cursor.fetchone()
            if head is None or str(head["operation_id"]) != str(operation_id):
                return None

            claimed_slots = self._select_free_slots(cursor, class_name)
            if claimed_slots is None:
                return None
            lease_expires_at = current_time + slot_lease
            self._occupy_slots(
                cursor,
                claimed_slots,
                operation_id=operation_id,
                lease_token=None,
                lease_expires_at=lease_expires_at,
            )
            execution_token = uuid.uuid4()
            cursor.execute(
                """
                UPDATE operation_record
                SET state = 'running',
                    execution_generation = execution_generation + 1,
                    execution_lease_token = %s,
                    state_revision = state_revision + 1,
                    updated_at = %s,
                    started_at = COALESCE(started_at, %s)
                WHERE operation_id = %s AND state = 'queued'
                RETURNING *
                """,
                (
                    str(execution_token),
                    current_time,
                    current_time,
                    str(operation_id),
                ),
            )
            running_row = cursor.fetchone()
            if running_row is None:
                raise RuntimeError("operation selection lost atomicity")
            operation = self._operation_from_row(running_row)
            return OperationClaim(
                operation=operation,
                fence=ExecutionFence(
                    operation_id=operation.operation_id,
                    execution_generation=operation.execution_generation,
                    execution_lease_token=execution_token,
                ),
            )

    def inspect_admission_class(
        self, class_name: AdmissionClass, *, now: datetime | None
    ) -> AdmissionClassStatus:
        with self._transaction() as cursor:
            self._current_time(cursor, now)
            cursor.execute(
                """
                SELECT class_name, parent_class_name, active_limit, queue_limit,
                       max_wait_ms
                FROM operation_admission_class WHERE class_name = %s
                """,
                (class_name.value,),
            )
            config = cursor.fetchone()
            if config is None:
                raise AdmissionConfigurationError(
                    f"unknown admission class {class_name.value}"
                )
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE s.operation_id IS NOT NULL) AS active_count,
                    MIN(o.started_at) AS oldest_running_at
                FROM operation_admission_slot AS s
                LEFT JOIN operation_record AS o ON o.operation_id = s.operation_id
                WHERE s.class_name = %s
                """,
                (class_name.value,),
            )
            active = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*) AS queued_count, MIN(accepted_at) AS oldest_queued_at
                FROM operation_record
                WHERE admission_class = %s AND state = 'queued'
                """,
                (class_name.value,),
            )
            queued = cursor.fetchone()
            max_wait = int(config["max_wait_ms"])
            return AdmissionClassStatus(
                class_name=AdmissionClass(config["class_name"]),
                parent_class_name=(
                    AdmissionClass(config["parent_class_name"])
                    if config["parent_class_name"] is not None
                    else None
                ),
                active_limit=int(config["active_limit"]),
                queue_limit=int(config["queue_limit"]),
                max_wait_ms=max_wait or None,
                active_count=int(active["active_count"]) if active else 0,
                queued_count=int(queued["queued_count"]) if queued else 0,
                oldest_queued_at=(
                    _normalize_datetime(queued["oldest_queued_at"])
                    if queued and queued["oldest_queued_at"] is not None
                    else None
                ),
                oldest_running_at=(
                    _normalize_datetime(active["oldest_running_at"])
                    if active and active["oldest_running_at"] is not None
                    else None
                ),
            )

    def query_operation(
        self, owner: OperationOwner, operation_id: uuid.UUID
    ) -> SafeOperationProjection:
        owner_sql, owner_params = self._owner_clause(owner)
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM operation_record
                WHERE operation_id = %s AND {owner_sql}
                """,
                (str(operation_id), *owner_params),
            )
            row = cursor.fetchone()
            if row is None:
                raise OperationNotFoundError("operation not found")
            return _safe_projection(self._operation_from_row(row))

    def reconcile_submission(
        self, owner: OperationOwner, submission_id: uuid.UUID
    ) -> SubmissionResult:
        with self._transaction() as cursor:
            row = self._submission_row(cursor, owner, submission_id)
            if row is None:
                raise OperationNotFoundError("operation submission not found")
            if not row["accepted"]:
                return self._refusal_from_row(row)
            operation_id = self._uuid(row["operation_id"])
            if operation_id is None:
                raise OperationNotFoundError("operation submission not found")
            operation = self._operation_row(cursor, operation_id)
            if operation is None:
                raise OperationNotFoundError("operation submission not found")
            return AcceptedSubmission(
                accepted=True,
                operation=_safe_projection(self._operation_from_row(operation)),
            )

    @staticmethod
    def _release_slots(cursor: Any, operation_id: uuid.UUID) -> None:
        cursor.execute(
            """
            UPDATE operation_admission_slot
            SET operation_id = NULL,
                lease_token = NULL,
                claim_generation = claim_generation + 1,
                lease_expires_at = NULL
            WHERE operation_id = %s
            """,
            (str(operation_id),),
        )
        cursor.execute(
            """
            DELETE FROM operation_admission_slot AS slot
            USING operation_admission_class AS config
            WHERE slot.class_name = config.class_name
              AND slot.slot_number > config.active_limit
              AND slot.operation_id IS NULL
            """
        )

    def cancel(
        self,
        owner: OperationOwner,
        operation_id: uuid.UUID,
        terminal_code: str,
        *,
        now: datetime | None,
        retention: timedelta,
        request_running: bool = True,
        transaction: Any | None = None,
    ) -> OperationRecord:
        owner_sql, owner_params = self._owner_clause(owner)
        with self._transaction_or_cursor(transaction) as cursor:
            current_time = self._current_time(cursor, now)
            cursor.execute(
                "SELECT admission_class FROM operation_record "
                "WHERE operation_id = %s",
                (str(operation_id),),
            )
            identity = cursor.fetchone()
            if identity is None:
                raise OperationNotFoundError("operation not found")
            self._lock_class_chain(
                cursor, AdmissionClass(str(identity["admission_class"]))
            )
            cursor.execute(
                f"""
                SELECT * FROM operation_record
                WHERE operation_id = %s AND {owner_sql}
                FOR UPDATE
                """,
                (str(operation_id), *owner_params),
            )
            row = cursor.fetchone()
            if row is None:
                raise OperationNotFoundError("operation not found")
            operation = self._operation_from_row(row)
            if (
                operation.state in _TERMINAL_STATES
                or operation.cancel_requested_at is not None
            ):
                return operation
            preselected = False
            if operation.state is OperationState.RUNNING:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS slot_count,
                           BOOL_AND(lease_token = %s) AS marker_matches
                    FROM operation_admission_slot
                    WHERE operation_id = %s
                    """,
                    (
                        str(operation.execution_lease_token),
                        str(operation.operation_id),
                    ),
                )
                marker = cursor.fetchone()
                preselected = bool(
                    marker
                    and int(marker["slot_count"])
                    == len(self._chain(operation.admission_class))
                    and marker["marker_matches"]
                )
            if operation.state is OperationState.QUEUED or preselected:
                cursor.execute(
                    """
                    UPDATE operation_record
                    SET state = 'cancelled', terminal_code = %s,
                        safe_summary = 'Cancelled',
                        state_revision = state_revision + 1,
                        updated_at = %s, cancel_requested_at = %s,
                        terminal_at = %s, purge_after = %s,
                        execution_lease_token = NULL
                    WHERE operation_id = %s
                      AND state IN ('queued', 'running')
                    RETURNING *
                    """,
                    (
                        terminal_code,
                        current_time,
                        current_time,
                        current_time,
                        current_time + retention,
                        str(operation_id),
                    ),
                )
                release_slots = True
            else:
                if not request_running:
                    return operation
                cursor.execute(
                    """
                    UPDATE operation_record
                    SET state_revision = state_revision + 1,
                        updated_at = %s, cancel_requested_at = %s
                    WHERE operation_id = %s AND state = 'running'
                    RETURNING *
                    """,
                    (current_time, current_time, str(operation_id)),
                )
                release_slots = False
            updated = cursor.fetchone()
            if updated is None:
                raise RuntimeError("operation cancellation lost atomicity")
            if release_slots:
                self._release_slots(cursor, operation_id)
            return self._operation_from_row(updated)

    def terminalize_unselected(
        self,
        operation_id: uuid.UUID,
        *,
        terminal_code: str,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
    ) -> OperationRecord | None:
        """Atomically settle exact work only while no worker owns its handoff."""

        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            cursor.execute(
                "SELECT admission_class FROM operation_record "
                "WHERE operation_id = %s",
                (str(operation_id),),
            )
            identity = cursor.fetchone()
            if identity is None:
                return None

            admission_class = AdmissionClass(identity["admission_class"])
            # Claim paths take these locks before the operation and slot rows.
            # Matching that order makes handoff-vs-recovery linearizable and
            # avoids inverting locks with an exact claim.
            self._lock_class_chain(cursor, admission_class)
            row = self._operation_row(cursor, operation_id, lock=True)
            if row is None:
                return None
            operation = self._operation_from_row(row)
            if operation.state in _TERMINAL_STATES:
                return operation

            if operation.state is OperationState.RUNNING:
                cursor.execute(
                    """
                    SELECT class_name, lease_token
                    FROM operation_admission_slot
                    WHERE operation_id = %s
                    ORDER BY class_name, slot_number
                    FOR UPDATE
                    """,
                    (str(operation_id),),
                )
                slots = tuple(cursor.fetchall())
                expected_classes = {
                    member.value for member in self._chain(admission_class)
                }
                preselected = (
                    operation.cancel_requested_at is None
                    and operation.execution_lease_token is not None
                    and len(slots) == len(expected_classes)
                    and {str(slot["class_name"]) for slot in slots}
                    == expected_classes
                    and all(
                        self._uuid(slot["lease_token"])
                        == operation.execution_lease_token
                        for slot in slots
                    )
                )
                if not preselected:
                    return None
            elif operation.state is not OperationState.QUEUED:
                return None

            execution_token = (
                str(operation.execution_lease_token)
                if operation.execution_lease_token is not None
                else None
            )
            cursor.execute(
                """
                UPDATE operation_record
                SET state = 'retryable', terminal_code = %s,
                    safe_summary = %s, retry_after_ms = %s,
                    execution_lease_token = NULL,
                    state_revision = state_revision + 1,
                    updated_at = %s, terminal_at = %s, purge_after = %s
                WHERE operation_id = %s AND state = %s
                  AND execution_generation = %s
                  AND execution_lease_token IS NOT DISTINCT FROM %s
                RETURNING *
                """,
                (
                    terminal_code,
                    safe_summary,
                    retry_after_ms,
                    current_time,
                    current_time,
                    current_time + retention,
                    str(operation_id),
                    operation.state.value,
                    operation.execution_generation,
                    execution_token,
                ),
            )
            terminal = cursor.fetchone()
            if terminal is None:  # pragma: no cover - row lock and CAS invariant
                raise RuntimeError("unselected terminalization lost atomicity")
            self._release_slots(cursor, operation_id)
            return self._operation_from_row(terminal)

    @staticmethod
    def _fence_matches(operation: OperationRecord, fence: ExecutionFence) -> bool:
        return (
            operation.state is OperationState.RUNNING
            and operation.execution_generation == fence.execution_generation
            and operation.execution_lease_token == fence.execution_lease_token
        )

    def terminalize(
        self,
        fence: ExecutionFence,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None,
        now: datetime | None,
        retention: timedelta,
        transaction: Any | None = None,
    ) -> OperationRecord:
        with self._transaction_or_cursor(transaction) as cursor:
            current_time = self._current_time(cursor, now)
            row = self._operation_row(cursor, fence.operation_id, lock=True)
            if row is None:
                raise StaleExecutionFenceError("execution fence is stale")
            operation = self._operation_from_row(row)
            if operation.state in _TERMINAL_STATES:
                return operation
            if not self._fence_matches(operation, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            cursor.execute(
                """
                UPDATE operation_record
                SET state = %s, terminal_code = %s, safe_summary = %s,
                    retry_after_ms = %s, execution_lease_token = NULL,
                    state_revision = state_revision + 1,
                    updated_at = %s, terminal_at = %s, purge_after = %s
                WHERE operation_id = %s AND state = 'running'
                  AND execution_generation = %s
                  AND execution_lease_token = %s
                RETURNING *
                """,
                (
                    state.value,
                    terminal_code,
                    safe_summary,
                    retry_after_ms,
                    current_time,
                    current_time,
                    current_time + retention,
                    str(fence.operation_id),
                    fence.execution_generation,
                    str(fence.execution_lease_token),
                ),
            )
            terminal = cursor.fetchone()
            if terminal is None:
                raise StaleExecutionFenceError("execution fence is stale")
            self._release_slots(cursor, fence.operation_id)
            return self._operation_from_row(terminal)

    def expire_queued(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            return self._expire_queued_locked(cursor, current_time, retention)

    def _assert_current_execution_cursor(
        self, cursor: Any, fence: ExecutionFence
    ) -> OperationRecord:
        cursor.execute(
            """
            SELECT * FROM operation_record
            WHERE operation_id = %s
            FOR UPDATE
            """,
            (str(fence.operation_id),),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleExecutionFenceError("execution fence is stale")
        operation = self._operation_from_row(row)
        if not self._fence_matches(operation, fence):
            raise StaleExecutionFenceError("execution fence is stale")
        return operation

    def assert_current_execution(
        self, fence: ExecutionFence, *, transaction: Any | None = None
    ) -> OperationRecord:
        if transaction is not None:
            return self._assert_current_execution_cursor(transaction, fence)
        with self._transaction() as cursor:
            return self._assert_current_execution_cursor(cursor, fence)

    def reselect_execution(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> ExecutionFence:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            operation = self._assert_current_execution_cursor(cursor, fence)
            execution_token = uuid.uuid4()
            cursor.execute(
                """
                UPDATE operation_record
                SET execution_generation = execution_generation + 1,
                    execution_lease_token = %s,
                    state_revision = state_revision + 1,
                    updated_at = %s
                WHERE operation_id = %s AND state = 'running'
                  AND execution_generation = %s
                  AND execution_lease_token = %s
                RETURNING execution_generation
                """,
                (
                    str(execution_token),
                    current_time,
                    str(fence.operation_id),
                    fence.execution_generation,
                    str(fence.execution_lease_token),
                ),
            )
            selected = cursor.fetchone()
            if selected is None:
                raise StaleExecutionFenceError("execution fence is stale")
            slot_token = uuid.uuid4()
            cursor.execute(
                """
                UPDATE operation_admission_slot
                SET lease_token = %s,
                    claim_generation = claim_generation + 1,
                    lease_expires_at = %s
                WHERE operation_id = %s
                """,
                (
                    str(slot_token),
                    current_time + slot_lease,
                    str(fence.operation_id),
                ),
            )
            expected_slots = len(self._chain(operation.admission_class))
            if cursor.rowcount != expected_slots:
                raise StaleExecutionFenceError("execution capacity lease is missing")
            return ExecutionFence(
                operation_id=fence.operation_id,
                execution_generation=int(selected["execution_generation"]),
                execution_lease_token=execution_token,
            )

    def update_phase(
        self,
        fence: ExecutionFence,
        phase_code: str,
        *,
        now: datetime | None,
    ) -> OperationRecord:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            operation = self._assert_current_execution_cursor(cursor, fence)
            if operation.phase_code == phase_code:
                return operation
            cursor.execute(
                """
                UPDATE operation_record
                SET phase_code = %s, state_revision = state_revision + 1,
                    updated_at = %s
                WHERE operation_id = %s AND state = 'running'
                  AND execution_generation = %s
                  AND execution_lease_token = %s
                RETURNING *
                """,
                (
                    phase_code,
                    current_time,
                    str(fence.operation_id),
                    fence.execution_generation,
                    str(fence.execution_lease_token),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise StaleExecutionFenceError("execution fence is stale")
            return self._operation_from_row(row)

    def renew_execution_lease(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> SlotLeaseRenewal:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            operation = self._assert_current_execution_cursor(cursor, fence)
            lease_expires_at = current_time + slot_lease
            slot_token = uuid.uuid4()
            cursor.execute(
                """
                UPDATE operation_admission_slot
                SET lease_token = %s,
                    claim_generation = claim_generation + 1,
                    lease_expires_at = %s
                WHERE operation_id = %s
                """,
                (str(slot_token), lease_expires_at, str(fence.operation_id)),
            )
            if cursor.rowcount != len(self._chain(operation.admission_class)):
                raise StaleExecutionFenceError("execution capacity lease is missing")
            return SlotLeaseRenewal(
                operation_id=fence.operation_id,
                execution_generation=fence.execution_generation,
                lease_expires_at=lease_expires_at,
            )

    def expire_execution_leases(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            return self._expire_execution_leases_locked(cursor, current_time, retention)

    def _expire_execution_leases_locked(
        self, cursor: Any, current_time: datetime, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        cursor.execute(
            """
                SELECT class_name, slot_number, operation_id
                FROM operation_admission_slot
                WHERE operation_id IS NOT NULL AND lease_expires_at <= %s
                ORDER BY lease_expires_at, class_name, slot_number
                """,
            (current_time,),
        )
        operation_ids = sorted(
            {
                operation_id
                for row in cursor.fetchall()
                if (operation_id := self._uuid(row["operation_id"])) is not None
            },
            key=lambda value: value.int,
        )
        terminal_records: list[OperationRecord] = []
        for operation_id in operation_ids:
            row = self._operation_row(cursor, operation_id, lock=True)
            if row is None:
                self._release_slots(cursor, operation_id)
                continue
            operation = self._operation_from_row(row)
            if operation.state is not OperationState.RUNNING:
                self._release_slots(cursor, operation_id)
                continue
            cursor.execute(
                """
                    SELECT 1 FROM operation_admission_slot
                    WHERE operation_id = %s
                    ORDER BY class_name, slot_number
                    FOR UPDATE
                    """,
                (str(operation_id),),
            )
            cursor.fetchall()
            cursor.execute(
                """
                    SELECT 1 FROM operation_admission_slot
                    WHERE operation_id = %s AND lease_expires_at <= %s
                    LIMIT 1
                    """,
                (str(operation_id), current_time),
            )
            if cursor.fetchone() is None:
                continue
            cursor.execute(
                """
                    UPDATE operation_record
                    SET state = 'retryable',
                        terminal_code = 'execution_lease_expired',
                        safe_summary = 'Execution lease expired',
                        retry_after_ms = 1000,
                        execution_lease_token = NULL,
                        state_revision = state_revision + 1,
                        updated_at = %s, terminal_at = %s, purge_after = %s
                    WHERE operation_id = %s AND state = 'running'
                    RETURNING *
                    """,
                (
                    current_time,
                    current_time,
                    current_time + retention,
                    str(operation_id),
                ),
            )
            terminal = cursor.fetchone()
            if terminal is None:
                raise RuntimeError("execution lease recovery lost atomicity")
            self._release_slots(cursor, operation_id)
            terminal_records.append(self._operation_from_row(terminal))
        return tuple(terminal_records)

    def purge_expired(
        self,
        *,
        now: datetime | None,
        limit: int,
        fence: ExecutionFence | None = None,
    ) -> PurgeResult:
        with self._transaction() as cursor:
            current_time = self._current_time(cursor, now)
            if fence is not None:
                self._assert_current_execution_cursor(cursor, fence)
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT submission.submission_result_id
                    FROM operation_submission_result AS submission
                    LEFT JOIN operation_record AS operation
                      ON operation.operation_id = submission.operation_id
                    WHERE submission.purge_after < %s
                      AND (
                          NOT submission.accepted
                          OR operation.operation_id IS NULL
                          OR (
                              operation.state IN (
                                  'completed', 'failed', 'cancelled', 'retryable'
                              )
                              AND operation.purge_after < %s
                          )
                      )
                    ORDER BY submission.purge_after, submission.submission_result_id
                    FOR UPDATE OF submission SKIP LOCKED
                    LIMIT %s
                )
                DELETE FROM operation_submission_result AS submission
                USING candidates
                WHERE submission.submission_result_id = candidates.submission_result_id
                RETURNING submission.submission_result_id
                """,
                (current_time, current_time, limit),
            )
            submissions = len(cursor.fetchall())
            cursor.execute(
                """
                SELECT operation_id
                FROM operation_record
                WHERE state IN ('completed', 'failed', 'cancelled', 'retryable')
                  AND purge_after < %s
                  AND NOT EXISTS (
                      SELECT 1 FROM operation_submission_result AS submission
                      WHERE submission.accepted
                        AND submission.operation_id = operation_record.operation_id
                  )
                ORDER BY purge_after, operation_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (current_time, limit),
            )
            candidate_ids = [str(row["operation_id"]) for row in cursor.fetchall()]
            if not candidate_ids:
                return PurgeResult(operations=0, submissions=submissions)
            # This second statement intentionally rechecks accepted
            # reconciliation rows after the candidate operation locks are
            # held. A concurrent idempotent replay either held the operation
            # lock (and was skipped above) or is now visible to this fresh
            # READ COMMITTED snapshot; cleanup cannot race it into a false
            # missing result.
            cursor.execute(
                """
                DELETE FROM operation_record AS operation
                WHERE operation.operation_id = ANY(%s::uuid[])
                  AND NOT EXISTS (
                      SELECT 1 FROM operation_submission_result AS submission
                      WHERE submission.accepted
                        AND submission.operation_id = operation.operation_id
                  )
                RETURNING operation.operation_id
                """,
                (candidate_ids,),
            )
            operations = len(cursor.fetchall())
            return PurgeResult(operations=operations, submissions=submissions)

    @contextmanager
    def fenced_transaction(self, fence: ExecutionFence) -> Iterator[Any]:
        with self._transaction() as cursor:
            self._assert_current_execution_cursor(cursor, fence)
            yield cursor
