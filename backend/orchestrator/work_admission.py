"""Durable admission, operation lifecycle, and execution fencing.

``WorkAdmissionCoordinator`` is the sole product operation-state authority.
Production construction injects ``PlaneWorkAdmissionRepository`` bound to the
one application-scoped Plane runtime and repository catalog; the coordinator
never constructs storage or silently falls back to process memory.
``InMemoryWorkAdmissionRepository`` exists only as an explicitly named
deterministic test dependency.

The public projections in this module deliberately exclude authenticated owner
identifiers, idempotency material, and execution fences.  Internal operation
records remain available only to trusted workers through fenced or explicitly
administrative repository surfaces.
"""

from __future__ import annotations

import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Callable, ContextManager, Iterator, Protocol, Sequence

from astralplane.repositories import (
    RepositoryConflictError as PlaneRepositoryConflictError,
)
from astralplane.repositories import work_admission as plane_admission


_OPERATION_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATES: frozenset["OperationState"]


class AdmissionClass(str, Enum):
    GLOBAL = "global"
    INTERACTIVE = "interactive"
    VOICE_INTERACTIVE = "voice_interactive"
    MCP = "mcp"
    BACKGROUND = "background"
    SCHEDULED = "scheduled"
    MAINTENANCE = "maintenance"
    SYSTEM = "system"


VOICE_INTERACTIVE_PER_USER_ACTIVE_LIMIT = 2
_VOICE_CAPACITY_RETRY_AFTER_MS = 1_000


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


class WorkAdmissionConflictError(RuntimeError):
    """Raised when an immutable work-admission binding conflicts."""


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
        if self.class_name is AdmissionClass.VOICE_INTERACTIVE and (
            self.queue_limit != 0 or self.max_wait_ms not in {None, 0}
        ):
            raise AdmissionConfigurationError(
                "voice_interactive must not queue or wait for capacity"
            )
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
        if (
            self.admission_class is AdmissionClass.VOICE_INTERACTIVE
            and self.owner.owner_scope is not OwnerScope.USER
        ):
            raise ValueError("voice_interactive requires authenticated user ownership")
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

    def get_operation_for_administration(
        self,
        operation_id: uuid.UUID,
        *,
        for_update: bool = False,
        transaction: Any | None = None,
    ) -> OperationRecord | None: ...

    def bind_request_generation(
        self,
        fence: ExecutionFence,
        request_generation: uuid.UUID,
        *,
        transaction: Any | None = None,
    ) -> OperationRecord: ...

    def bind_chat(
        self, fence: ExecutionFence, chat_id: str, *, now: datetime | None
    ) -> OperationRecord: ...

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

    def oldest_purge_eligible_due_at(
        self,
        *,
        now: datetime | None,
        transaction: Any | None = None,
    ) -> datetime | None: ...

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
        clock: Callable[[], datetime] | None = None,
        operation_retention: timedelta = timedelta(hours=24),
        slot_lease: timedelta = timedelta(seconds=30),
        _configure_repository: bool = True,
    ) -> None:
        if repository is None:
            raise ValueError("inject an explicit work-admission repository")
        if operation_retention <= timedelta(0):
            raise ValueError("operation_retention must be positive")
        if slot_lease <= timedelta(0):
            raise ValueError("slot_lease must be positive")
        configs = tuple(admission_classes)
        _validate_admission_graph(configs)
        self._repository = repository
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
    def from_plane(
        cls,
        *,
        plane_runtime: Any,
        plane_repositories: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        operation_retention: timedelta = timedelta(hours=24),
        slot_lease: timedelta = timedelta(seconds=30),
    ) -> WorkAdmissionCoordinator:
        """Bind the effective Plane-owned graph without rewriting its rows."""

        repository = PlaneWorkAdmissionRepository(
            plane_runtime=plane_runtime,
            plane_repositories=plane_repositories,
        )
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

    def current_time(self) -> datetime:
        """Return the coordinator's normalized clock for product telemetry."""

        current = self._now()
        return datetime.now(UTC) if current is None else current

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
        fenced and terminalized in the same caller-owned Plane transaction as its
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

        Trusted subsystems that must decide a second durable guard while the
        operation lock is held may pass their Plane transaction and set
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

    def bind_chat(self, fence: ExecutionFence, chat_id: str) -> OperationRecord:
        """Bind the conversation a fenced turn just created onto its operation.

        Only the ``None -> chat`` transition is legal: an operation admitted
        before its conversation existed (the first message of a new chat has
        no chat_id at ingress) adopts the chat its turn created, durably, so
        every downstream publication fence keeps strict identity semantics.
        Re-binding the same chat is an idempotent no-op; a different existing
        binding is a cross-conversation conflict and refuses.
        """
        return self._repository.bind_chat(fence, chat_id, now=self._now())

    def renew_execution_lease(self, fence: ExecutionFence) -> SlotLeaseRenewal:
        return self._repository.renew_execution_lease(
            fence, now=self._now(), slot_lease=self._slot_lease
        )

    def expire_execution_leases(self) -> tuple[OperationRecord, ...]:
        return self._repository.expire_execution_leases(
            now=self._now(), retention=self._operation_retention
        )

    def oldest_purge_eligible_due_at(
        self, *, transaction: Any | None = None
    ) -> datetime | None:
        """Return the oldest operation/submission row currently purgeable."""

        return self._repository.oldest_purge_eligible_due_at(
            now=self._now(), transaction=transaction
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

        Plane-owned effects executed with the yielded transaction are
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
    voice_config = by_name.get(AdmissionClass.VOICE_INTERACTIVE)
    if (
        voice_config is not None
        and voice_config.parent_class_name is not AdmissionClass.INTERACTIVE
    ):
        raise AdmissionConfigurationError(
            "voice_interactive must be a child of interactive"
        )
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


def load_admission_class_configs(
    *, plane_runtime: Any, plane_repositories: Any | None = None
) -> tuple[AdmissionClassConfig, ...]:
    """Read the complete effective graph through Plane's typed repository."""

    return PlaneWorkAdmissionRepository(
        plane_runtime=plane_runtime,
        plane_repositories=plane_repositories,
    ).load_existing_configs()


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
            if request.admission_class is AdmissionClass.VOICE_INTERACTIVE:
                running_for_user = sum(
                    operation.admission_class is AdmissionClass.VOICE_INTERACTIVE
                    and operation.owner_scope is OwnerScope.USER
                    and operation.owner_user_id == request.owner.owner_user_id
                    and operation.state is OperationState.RUNNING
                    for operation in self._operations.values()
                )
                if running_for_user >= VOICE_INTERACTIVE_PER_USER_ACTIVE_LIMIT:
                    refusal = self._record_submission(
                        request,
                        now=current_time,
                        retention=retention,
                        refusal_code="capacity_exceeded",
                        retryable=True,
                        retry_after_ms=_VOICE_CAPACITY_RETRY_AFTER_MS,
                    )
                    return self._refused(refusal)
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

    def get_operation_for_administration(
        self,
        operation_id: uuid.UUID,
        *,
        for_update: bool = False,
        transaction: Any | None = None,
    ) -> OperationRecord | None:
        del transaction
        operation_id = _require_uuid(operation_id, "operation_id")
        if not isinstance(for_update, bool):
            raise ValueError("for_update must be boolean")
        with self._lock:
            return self._operations.get(operation_id)

    def bind_request_generation(
        self,
        fence: ExecutionFence,
        request_generation: uuid.UUID,
        *,
        transaction: Any | None = None,
    ) -> OperationRecord:
        del transaction
        request_generation = _require_uuid(
            request_generation, "request_generation"
        )
        with self._lock:
            record = self._operations.get(fence.operation_id)
            if record is None or not self._fence_matches(record, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            if record.request_generation is not None:
                if record.request_generation == request_generation:
                    return record
                raise WorkAdmissionConflictError(
                    "operation is bound to a different request generation"
                )
            updated = replace(
                record,
                request_generation=request_generation,
                state_revision=record.state_revision + 1,
                updated_at=datetime.now(UTC),
            )
            self._operations[record.operation_id] = updated
            return updated

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
        del transaction  # External transactions are not meaningful to this test double.
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
        del transaction  # External transactions are not meaningful to this test double.
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

    def bind_chat(
        self,
        fence: ExecutionFence,
        chat_id: str,
        *,
        now: datetime | None,
    ) -> OperationRecord:
        current_time = self._now(now)
        with self._lock:
            record = self._operations.get(fence.operation_id)
            if record is None or not self._fence_matches(record, fence):
                raise StaleExecutionFenceError("execution fence is stale")
            if record.chat_id is not None:
                if str(record.chat_id) == str(chat_id):
                    return record
                raise ValueError("operation is bound to a different conversation")
            updated = replace(
                record,
                chat_id=str(chat_id),
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

    def oldest_purge_eligible_due_at(
        self,
        *,
        now: datetime | None,
        transaction: Any | None = None,
    ) -> datetime | None:
        del transaction
        current_time = self._now(now)
        with self._lock:
            due_times: list[datetime] = []
            for submission in self._submissions.values():
                if submission.purge_after >= current_time:
                    continue
                operation = (
                    self._operations.get(submission.operation_id)
                    if submission.operation_id is not None
                    else None
                )
                if submission.accepted and operation is not None and (
                    operation.state not in _TERMINAL_STATES
                    or operation.purge_after is None
                    or operation.purge_after >= current_time
                ):
                    continue
                due_times.append(submission.purge_after)
            accepted_operation_ids = {
                submission.operation_id
                for submission in self._submissions.values()
                if submission.accepted and submission.operation_id is not None
            }
            due_times.extend(
                operation.purge_after
                for operation in self._operations.values()
                if operation.state in _TERMINAL_STATES
                and operation.purge_after is not None
                and operation.purge_after < current_time
                and operation.operation_id not in accepted_operation_ids
            )
            return min(due_times) if due_times else None

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


def _to_plane_class(value: AdmissionClass) -> plane_admission.AdmissionClass:
    return plane_admission.AdmissionClass(value.value)


def _from_plane_class(value: plane_admission.AdmissionClass) -> AdmissionClass:
    return AdmissionClass(value.value)


def _to_plane_state(value: OperationState) -> plane_admission.OperationState:
    return plane_admission.OperationState(value.value)


def _from_plane_state(value: plane_admission.OperationState) -> OperationState:
    return OperationState(value.value)


def _to_plane_owner(owner: OperationOwner) -> plane_admission.OperationOwner:
    return plane_admission.OperationOwner(
        owner_scope=plane_admission.OwnerScope(owner.owner_scope.value),
        owner_user_id=owner.owner_user_id,
        connection_scope_id=owner.connection_scope_id,
    )


def _to_plane_config(
    config: AdmissionClassConfig,
) -> plane_admission.AdmissionClassConfig:
    return plane_admission.AdmissionClassConfig(
        class_name=_to_plane_class(config.class_name),
        parent_class_name=(
            None
            if config.parent_class_name is None
            else _to_plane_class(config.parent_class_name)
        ),
        active_limit=config.active_limit,
        queue_limit=config.queue_limit,
        max_wait_ms=config.max_wait_ms,
        config_revision=config.config_revision,
    )


def _from_plane_config(
    config: plane_admission.AdmissionClassConfig,
) -> AdmissionClassConfig:
    return AdmissionClassConfig(
        class_name=_from_plane_class(config.class_name),
        parent_class_name=(
            None
            if config.parent_class_name is None
            else _from_plane_class(config.parent_class_name)
        ),
        active_limit=config.active_limit,
        queue_limit=config.queue_limit,
        max_wait_ms=config.max_wait_ms,
        config_revision=config.config_revision,
    )


def _to_plane_request(request: OperationRequest) -> plane_admission.OperationRequest:
    return plane_admission.OperationRequest(
        operation_kind=request.operation_kind,
        admission_class=_to_plane_class(request.admission_class),
        owner=_to_plane_owner(request.owner),
        submission_id=request.submission_id,
        idempotency_namespace=request.idempotency_namespace,
        idempotency_key=request.idempotency_key,
        normalized_input_digest=request.normalized_input_digest,
        chat_id=request.chat_id,
        parent_operation_id=request.parent_operation_id,
        connection_generation=request.connection_generation,
        request_generation=request.request_generation,
    )


def _to_plane_fence(fence: ExecutionFence) -> plane_admission.ExecutionFence:
    return plane_admission.ExecutionFence(
        operation_id=fence.operation_id,
        execution_generation=fence.execution_generation,
        execution_lease_token=fence.execution_lease_token,
    )


def _from_plane_fence(fence: plane_admission.ExecutionFence) -> ExecutionFence:
    return ExecutionFence(
        operation_id=fence.operation_id,
        execution_generation=fence.execution_generation,
        execution_lease_token=fence.execution_lease_token,
    )


def _from_plane_record(
    record: plane_admission.OperationRecord,
) -> OperationRecord:
    return OperationRecord(
        operation_id=record.operation_id,
        operation_kind=record.operation_kind,
        admission_class=_from_plane_class(record.admission_class),
        owner_scope=OwnerScope(record.owner_scope.value),
        owner_user_id=record.owner_user_id,
        connection_scope_id=record.connection_scope_id,
        idempotency_namespace=record.idempotency_namespace,
        idempotency_key=record.idempotency_key,
        normalized_input_digest=record.normalized_input_digest,
        chat_id=record.chat_id,
        parent_operation_id=record.parent_operation_id,
        connection_generation=record.connection_generation,
        request_generation=record.request_generation,
        state=_from_plane_state(record.state),
        phase_code=record.phase_code,
        terminal_code=record.terminal_code,
        safe_summary=record.safe_summary,
        retry_after_ms=record.retry_after_ms,
        execution_generation=record.execution_generation,
        execution_lease_token=record.execution_lease_token,
        state_revision=record.state_revision,
        accepted_at=record.accepted_at,
        updated_at=record.updated_at,
        queue_deadline_at=record.queue_deadline_at,
        started_at=record.started_at,
        terminal_at=record.terminal_at,
        cancel_requested_at=record.cancel_requested_at,
        purge_after=record.purge_after,
    )


def _from_plane_projection(
    projection: plane_admission.SafeOperationProjection,
) -> SafeOperationProjection:
    return SafeOperationProjection(
        operation_id=projection.operation_id,
        operation_kind=projection.operation_kind,
        admission_class=_from_plane_class(projection.admission_class),
        owner_scope=OwnerScope(projection.owner_scope.value),
        chat_id=projection.chat_id,
        parent_operation_id=projection.parent_operation_id,
        connection_generation=projection.connection_generation,
        request_generation=projection.request_generation,
        state=_from_plane_state(projection.state),
        phase_code=projection.phase_code,
        terminal_code=projection.terminal_code,
        safe_summary=projection.safe_summary,
        retry_after_ms=projection.retry_after_ms,
        state_revision=projection.state_revision,
        accepted_at=projection.accepted_at,
        queue_deadline_at=projection.queue_deadline_at,
        started_at=projection.started_at,
        terminal_at=projection.terminal_at,
        updated_at=projection.updated_at,
        purge_after=projection.purge_after,
    )


@contextmanager
def _translate_plane_errors() -> Iterator[None]:
    try:
        yield
    except plane_admission.StaleWorkExecutionFenceError as exc:
        raise StaleExecutionFenceError(str(exc)) from exc
    except plane_admission.WorkAdmissionNotFoundError as exc:
        raise OperationNotFoundError(str(exc)) from exc
    except plane_admission.WorkAdmissionConfigurationError as exc:
        raise AdmissionConfigurationError(str(exc)) from exc
    except plane_admission.WorkAdmissionIntegrityError as exc:
        raise RuntimeError(str(exc)) from exc
    except PlaneRepositoryConflictError as exc:
        raise WorkAdmissionConflictError(str(exc)) from exc
    except ValueError as exc:
        # Plane validation errors intentionally remain ordinary domain
        # ``ValueError`` instances at Deep's established public boundary.
        raise ValueError(str(exc)) from exc


class PlaneWorkAdmissionRepository:
    """Deep domain adapter over Plane's caller-owned transaction repository."""

    def __init__(
        self,
        *,
        plane_runtime: Any,
        plane_repositories: Any | None = None,
    ) -> None:
        if plane_runtime is None or not callable(
            getattr(plane_runtime, "transaction", None)
        ):
            raise TypeError("an initialized Plane runtime is required")
        catalog = plane_repositories or getattr(plane_runtime, "repositories", None)
        repository = getattr(catalog, "work_admission", None)
        if repository is None:
            raise TypeError("Plane repository catalog is missing work_admission")
        self._runtime = plane_runtime
        self._plane_repository = repository
        self._configuration_lock = threading.RLock()
        self._configs: dict[AdmissionClass, AdmissionClassConfig] = {}

    @contextmanager
    def _transaction(self, transaction: Any | None = None) -> Iterator[Any]:
        if transaction is not None:
            yield transaction
            return
        with self._runtime.transaction() as owned_transaction:
            yield owned_transaction

    def _invoke(
        self,
        operation: Callable[..., Any],
        /,
        *args: object,
        transaction: Any | None = None,
        **kwargs: object,
    ) -> Any:
        with self._transaction(transaction) as active_transaction:
            with _translate_plane_errors():
                return operation(active_transaction, *args, **kwargs)

    def load_existing_configs(self) -> tuple[AdmissionClassConfig, ...]:
        plane_configs = tuple(
            self._invoke(self._plane_repository.load_existing_configs)
        )
        with _translate_plane_errors():
            self._plane_repository.bind_configs(plane_configs)
        configs = tuple(_from_plane_config(config) for config in plane_configs)
        with self._configuration_lock:
            self._configs = {config.class_name: config for config in configs}
        return configs

    def configure(self, admission_classes: Sequence[AdmissionClassConfig]) -> None:
        configs = tuple(admission_classes)
        plane_configs = tuple(_to_plane_config(config) for config in configs)
        self._invoke(
            self._plane_repository.configure,
            plane_configs,
        )
        with _translate_plane_errors():
            self._plane_repository.bind_configs(plane_configs)
        with self._configuration_lock:
            self._configs = {config.class_name: config for config in configs}

    def submit(
        self,
        request: OperationRequest,
        *,
        now: datetime | None,
        retention: timedelta,
        slot_lease: timedelta,
    ) -> AdmissionResult:
        result = self._invoke(
            self._plane_repository.submit,
            _to_plane_request(request),
            now=now,
            retention=retention,
            slot_lease=slot_lease,
        )
        if isinstance(result, plane_admission.AcceptedAdmission):
            return AcceptedAdmission(
                accepted=result.accepted,
                operation_id=result.operation_id,
                state=_from_plane_state(result.state),
                state_revision=result.state_revision,
                queue_position=result.queue_position,
                queue_deadline_at=result.queue_deadline_at,
            )
        return RefusedAdmission(
            accepted=result.accepted,
            code=result.code,
            retryable=result.retryable,
            retry_after_ms=result.retry_after_ms,
        )

    @staticmethod
    def _claim(
        claim: plane_admission.OperationClaim | None,
    ) -> OperationClaim | None:
        if claim is None:
            return None
        return OperationClaim(
            operation=_from_plane_record(claim.operation),
            fence=_from_plane_fence(claim.fence),
        )

    def claim_next(
        self,
        class_name: AdmissionClass,
        *,
        now: datetime | None,
        slot_lease: timedelta,
        retention: timedelta,
    ) -> OperationClaim | None:
        return self._claim(
            self._invoke(
                self._plane_repository.claim_next,
                _to_plane_class(class_name),
                now=now,
                slot_lease=slot_lease,
                retention=retention,
            )
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
        return self._claim(
            self._invoke(
                self._plane_repository.claim_operation,
                _to_plane_class(class_name),
                operation_id,
                now=now,
                slot_lease=slot_lease,
                retention=retention,
            )
        )

    def inspect_admission_class(
        self, class_name: AdmissionClass, *, now: datetime | None
    ) -> AdmissionClassStatus:
        status = self._invoke(
            self._plane_repository.inspect_admission_class,
            _to_plane_class(class_name),
            now=now,
        )
        return AdmissionClassStatus(
            class_name=_from_plane_class(status.class_name),
            parent_class_name=(
                None
                if status.parent_class_name is None
                else _from_plane_class(status.parent_class_name)
            ),
            active_limit=status.active_limit,
            queue_limit=status.queue_limit,
            max_wait_ms=status.max_wait_ms,
            active_count=status.active_count,
            queued_count=status.queued_count,
            oldest_queued_at=status.oldest_queued_at,
            oldest_running_at=status.oldest_running_at,
        )

    def query_operation(
        self, owner: OperationOwner, operation_id: uuid.UUID
    ) -> SafeOperationProjection:
        return _from_plane_projection(
            self._invoke(
                self._plane_repository.query_operation,
                _to_plane_owner(owner),
                operation_id,
            )
        )

    def get_operation_for_administration(
        self,
        operation_id: uuid.UUID,
        *,
        for_update: bool = False,
        transaction: Any | None = None,
    ) -> OperationRecord | None:
        record = self._invoke(
            self._plane_repository.get_operation_for_administration,
            operation_id=operation_id,
            for_update=for_update,
            transaction=transaction,
        )
        return None if record is None else _from_plane_record(record)

    def bind_request_generation(
        self,
        fence: ExecutionFence,
        request_generation: uuid.UUID,
        *,
        transaction: Any | None = None,
    ) -> OperationRecord:
        return _from_plane_record(
            self._invoke(
                self._plane_repository.bind_request_generation,
                fence=_to_plane_fence(fence),
                request_generation=request_generation,
                transaction=transaction,
            )
        )

    def bind_chat(
        self, fence: ExecutionFence, chat_id: str, *, now: datetime | None
    ) -> OperationRecord:
        return _from_plane_record(
            self._invoke(
                self._plane_repository.bind_chat,
                _to_plane_fence(fence),
                chat_id,
                now=now,
            )
        )

    def reconcile_submission(
        self, owner: OperationOwner, submission_id: uuid.UUID
    ) -> SubmissionResult:
        result = self._invoke(
            self._plane_repository.reconcile_submission,
            _to_plane_owner(owner),
            submission_id,
        )
        if isinstance(result, plane_admission.AcceptedSubmission):
            return AcceptedSubmission(
                accepted=result.accepted,
                operation=_from_plane_projection(result.operation),
            )
        return RefusedAdmission(
            accepted=result.accepted,
            code=result.code,
            retryable=result.retryable,
            retry_after_ms=result.retry_after_ms,
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
        return _from_plane_record(
            self._invoke(
                self._plane_repository.cancel,
                _to_plane_owner(owner),
                operation_id,
                terminal_code,
                now=now,
                retention=retention,
                request_running=request_running,
                transaction=transaction,
            )
        )

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
        record = self._invoke(
            self._plane_repository.terminalize_unselected,
            operation_id,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
            now=now,
            retention=retention,
        )
        return None if record is None else _from_plane_record(record)

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
        return _from_plane_record(
            self._invoke(
                self._plane_repository.terminalize,
                _to_plane_fence(fence),
                state=_to_plane_state(state),
                terminal_code=terminal_code,
                safe_summary=safe_summary,
                retry_after_ms=retry_after_ms,
                now=now,
                retention=retention,
                transaction=transaction,
            )
        )

    def expire_queued(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        return tuple(
            _from_plane_record(record)
            for record in self._invoke(
                self._plane_repository.expire_queued,
                now=now,
                retention=retention,
            )
        )

    def assert_current_execution(
        self, fence: ExecutionFence, *, transaction: Any | None = None
    ) -> OperationRecord:
        return _from_plane_record(
            self._invoke(
                self._plane_repository.assert_current_execution,
                _to_plane_fence(fence),
                transaction=transaction,
            )
        )

    def reselect_execution(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> ExecutionFence:
        return _from_plane_fence(
            self._invoke(
                self._plane_repository.reselect_execution,
                _to_plane_fence(fence),
                now=now,
                slot_lease=slot_lease,
            )
        )

    def update_phase(
        self,
        fence: ExecutionFence,
        phase_code: str,
        *,
        now: datetime | None,
    ) -> OperationRecord:
        return _from_plane_record(
            self._invoke(
                self._plane_repository.update_phase,
                _to_plane_fence(fence),
                phase_code,
                now=now,
            )
        )

    def renew_execution_lease(
        self,
        fence: ExecutionFence,
        *,
        now: datetime | None,
        slot_lease: timedelta,
    ) -> SlotLeaseRenewal:
        renewal = self._invoke(
            self._plane_repository.renew_execution_lease,
            _to_plane_fence(fence),
            now=now,
            slot_lease=slot_lease,
        )
        return SlotLeaseRenewal(
            operation_id=renewal.operation_id,
            execution_generation=renewal.execution_generation,
            lease_expires_at=renewal.lease_expires_at,
        )

    def expire_execution_leases(
        self, *, now: datetime | None, retention: timedelta
    ) -> tuple[OperationRecord, ...]:
        return tuple(
            _from_plane_record(record)
            for record in self._invoke(
                self._plane_repository.expire_execution_leases,
                now=now,
                retention=retention,
            )
        )

    def oldest_purge_eligible_due_at(
        self,
        *,
        now: datetime | None,
        transaction: Any | None = None,
    ) -> datetime | None:
        return self._invoke(
            self._plane_repository.oldest_purge_eligible_due_at,
            now=now,
            transaction=transaction,
        )

    def purge_expired(
        self,
        *,
        now: datetime | None,
        limit: int,
        fence: ExecutionFence | None = None,
    ) -> PurgeResult:
        result = self._invoke(
            self._plane_repository.purge_expired,
            now=now,
            limit=limit,
            fence=None if fence is None else _to_plane_fence(fence),
        )
        return PurgeResult(
            operations=result.operations,
            submissions=result.submissions,
        )

    @contextmanager
    def fenced_transaction(self, fence: ExecutionFence) -> Iterator[Any]:
        with self._runtime.transaction() as transaction:
            with _translate_plane_errors():
                self._plane_repository.assert_current_execution(
                    transaction, _to_plane_fence(fence)
                )
            yield transaction
