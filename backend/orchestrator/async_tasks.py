"""Compatibility views for operation-backed asynchronous chat work.

``WorkAdmissionCoordinator`` is the sole identity, admission, lifecycle, and
retention authority.  This module keeps the legacy background-task DTO,
captured-output, watcher, and completion-fan surfaces while projecting their
status from the coordinator.  A process-local dispatcher retains only the
callable needed to execute a durably accepted operation; FIFO selection,
capacity, cancellation, execution fencing, and retention remain coordinator
decisions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from astralplane import AsyncPlaneRuntime
from astralplane.repositories.background_tasks import (
    BackgroundTaskRecord as PlaneBackgroundTaskRecord,
)
from astralplane.repositories.background_tasks import (
    BackgroundTaskStatus as PlaneBackgroundTaskStatus,
)
from orchestrator.work_admission import (
    AdmissionClass,
    ExecutionFence,
    OperationNotFoundError,
    OperationOwner,
    OperationRecord,
    OperationRequest,
    OperationState,
    OwnerScope,
    PurgeResult,
    SafeOperationProjection,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from shared.feature_flags import flags

logger = logging.getLogger(__name__)

_SAFE_OBSERVABILITY_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYABLE = "retryable"


_STATUS_BY_OPERATION_STATE = {
    OperationState.QUEUED: TaskStatus.QUEUED,
    OperationState.RUNNING: TaskStatus.RUNNING,
    OperationState.COMPLETED: TaskStatus.COMPLETED,
    OperationState.FAILED: TaskStatus.FAILED,
    OperationState.CANCELLED: TaskStatus.CANCELLED,
    OperationState.RETRYABLE: TaskStatus.RETRYABLE,
}
_TERMINAL_OPERATION_STATES = frozenset(
    {
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RETRYABLE,
    }
)


class BackgroundTaskManagerNotBoundError(RuntimeError):
    """Raised when lifecycle work is attempted without a coordinator."""


class BackgroundTaskAdmissionError(RuntimeError):
    """Legacy exception projection for an explicit admission refusal."""

    def __init__(
        self, code: str, *, retryable: bool, retry_after_ms: int | None
    ) -> None:
        super().__init__(f"background task admission refused: {code}")
        self.code = code
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms


@dataclass(frozen=True)
class RetentionSweepResult:
    """Bounded work performed by one maintenance-admitted sweep."""

    operations: int
    submissions: int
    compatibility_rows: int
    batches: int
    backlog: bool = False


OperationProjection = OperationRecord | SafeOperationProjection
_FENCE_UNSET = object()
_SAFE_OPERATION_PROJECTION_FIELDS = tuple(
    projection_field.name for projection_field in fields(SafeOperationProjection)
)


@dataclass(frozen=True)
class _BackgroundExecution:
    """Process-local callable paired with one durable accepted operation."""

    coro_factory: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _validate_execution_fence(
    operation: OperationProjection, fence: ExecutionFence | None
) -> None:
    if fence is None:
        return
    if fence.operation_id != operation.operation_id:
        raise RuntimeError("background task fence operation identity changed")
    if (
        isinstance(operation, OperationRecord)
        and operation.state is OperationState.RUNNING
    ):
        if (
            fence.execution_generation != operation.execution_generation
            or fence.execution_lease_token != operation.execution_lease_token
        ):
            raise RuntimeError("background task execution fence is stale")


@dataclass
class BackgroundTask:
    """Legacy task DTO backed by one durable operation when manager-created.

    Direct construction remains supported for ``VirtualWebSocket`` adapters
    used by internal call sites and tests; arbitrary synthetic identifiers are
    therefore valid here.  Only ``BackgroundTaskManager.submit`` creates a
    managed task, and managed identifiers are full operation UUIDs.
    """

    task_id: str
    chat_id: str
    user_id: str
    status: TaskStatus = TaskStatus.QUEUED
    kind: str = "async_chat"
    title: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    asyncio_task: asyncio.Task | None = None
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    watchers: List[Any] = field(default_factory=list)

    _MANAGED_FIELDS = frozenset(
        {"task_id", "chat_id", "user_id", "status", "created_at", "completed_at"}
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            name in self._MANAGED_FIELDS
            and getattr(self, "_operation", None) is not None
        ):
            raise AttributeError(f"managed background task field {name!r} is read-only")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        self.status = TaskStatus(self.status)
        self._owner: OperationOwner | None = None
        self._operation: OperationProjection | None = None
        self._execution_fence: ExecutionFence | None = None
        self._notification_sent = False
        self._lease_task: asyncio.Task | None = None
        self._lease_loss_reason: str | None = None
        self._virtual_websocket: VirtualWebSocket | None = None
        self._cancellation_terminal_code: str | None = None
        self._cancellation_safe_summary: str | None = None
        self._terminal_observed = False

    def _attach_authority(
        self,
        *,
        owner: OperationOwner,
        operation: OperationProjection,
        execution_fence: ExecutionFence | None,
    ) -> None:
        if self._operation is not None or self._owner is not None:
            raise RuntimeError("background task authority is already attached")
        if execution_fence is not None:
            _validate_execution_fence(operation, execution_fence)
        self._apply_operation(operation, execution_fence=execution_fence)
        self._owner = owner

    @property
    def operation_execution_generation(self) -> int | None:
        if self._execution_fence is not None:
            return self._execution_fence.execution_generation
        if isinstance(self._operation, OperationRecord):
            generation = self._operation.execution_generation
            return generation if generation > 0 else None
        return None

    def _canonical_status(self) -> TaskStatus:
        if self._operation is None:
            return self.status
        return _STATUS_BY_OPERATION_STATE[self._operation.state]

    def _apply_operation(
        self,
        operation: OperationProjection,
        *,
        execution_fence: ExecutionFence | None | object = _FENCE_UNSET,
    ) -> None:
        if str(operation.operation_id) != self.task_id:
            raise RuntimeError("background task operation identity changed")
        if (
            self._operation is not None
            and operation.state_revision < self._operation.state_revision
        ):
            raise RuntimeError("background task operation projection moved backwards")
        if (
            self._operation is not None
            and self._operation.state in _TERMINAL_OPERATION_STATES
            and operation.state is not self._operation.state
        ):
            raise RuntimeError(
                "background task terminal projection cannot be overwritten"
            )
        if execution_fence is not _FENCE_UNSET:
            if execution_fence is not None and not isinstance(
                execution_fence, ExecutionFence
            ):
                raise TypeError("execution_fence must be an ExecutionFence")
            _validate_execution_fence(operation, execution_fence)
        elif (
            self._execution_fence is not None
            and isinstance(operation, OperationRecord)
            and operation.state is OperationState.RUNNING
            and (
                self._execution_fence.execution_generation
                != operation.execution_generation
                or self._execution_fence.execution_lease_token
                != operation.execution_lease_token
            )
        ):
            # Owner-safe refresh/cancel records may reveal that another worker
            # was explicitly reselected.  Never synthesize its secret fence;
            # discard only our stale one and wait for an explicit new fence.
            self._execution_fence = None
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(
            self, "status", _STATUS_BY_OPERATION_STATE[operation.state]
        )
        object.__setattr__(self, "created_at", operation.accepted_at)
        object.__setattr__(self, "completed_at", operation.terminal_at)
        if operation.state in _TERMINAL_OPERATION_STATES:
            # The durable terminal transition already revoked this token. Keep
            # no stale private capability in the compatibility projection.
            self._execution_fence = None
        elif execution_fence is not _FENCE_UNSET:
            self._execution_fence = execution_fence

    def to_dict(self) -> Dict[str, Any]:
        status = self._canonical_status()
        created_at = (
            self._operation.accepted_at
            if self._operation is not None
            else self.created_at
        )
        completed_at = (
            self._operation.terminal_at
            if self._operation is not None
            else self.completed_at
        )
        return {
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "status": status.value,
            "created_at": created_at.isoformat(),
            "completed_at": completed_at.isoformat() if completed_at else None,
        }


class VirtualWebSocket:
    """Capture messages produced by a background turn."""

    def __init__(self, task: BackgroundTask):
        self.task = task
        self._closed = False

    def _can_publish(self) -> bool:
        if self._closed:
            return False
        operation = self.task._operation
        if operation is None:
            return True
        return (
            operation.state is OperationState.RUNNING
            and self.task._execution_fence is not None
        )

    async def send_text(self, data: str):
        if not self._can_publish():
            return
        try:
            self.task.outputs.append(json.loads(data))
        except json.JSONDecodeError:
            self.task.outputs.append({"type": "raw", "data": data})

    async def send_json(self, data: Any, mode: str = "text"):
        if not self._can_publish():
            return
        if isinstance(data, dict):
            self.task.outputs.append(data)
        elif isinstance(data, str):
            await self.send_text(data)

    async def receive_text(self) -> str:
        return ""

    async def receive_json(self, mode: str = "text"):
        return {}

    async def close(self, code: int = 1000):
        self._closed = True

    @property
    def client(self):
        return ("background", self.task.task_id)

    def __repr__(self):
        return f"VirtualWebSocket(task={self.task.task_id})"


class DurableUserTurnWebSocket:
    """Memory-only execution socket for one already-admitted user turn.

    The object deliberately has a distinct identity from the UI transport so
    the orchestrator can bind a private copy of verified claims and the raw
    delegation token for the exact task lifetime. Conversation candidate
    frames remain private until atomic publication; only a fully correlated
    pre-acceptance voice rejection may be forwarded directly. The durable
    operation and committed conversation are therefore independent of the
    originating socket surviving, without turning this adapter into a second
    authority or content store.
    """

    _FORWARDED_FRAME_TYPES = frozenset({"voice_submission_rejected"})

    def __init__(self, origin: Any, *, user_id: str) -> None:
        if origin is None:
            raise ValueError("origin is required")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id is required")
        self._origin = origin
        self.llm_context_user_id: str | None = user_id.strip()
        self._closed = False

    async def send_text(self, data: str):
        if self._closed:
            return None
        try:
            frame = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(frame, dict)
            or frame.get("type") not in self._FORWARDED_FRAME_TYPES
        ):
            return None
        sender = getattr(self._origin, "send_text", None)
        if callable(sender):
            return await sender(data)
        sender = getattr(self._origin, "send", None)
        if callable(sender):
            return await sender(data)
        return None

    async def send_json(self, data: Any, mode: str = "text"):
        del mode
        if isinstance(data, dict):
            return await self.send_text(
                json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            )
        if isinstance(data, str):
            return await self.send_text(data)
        return None

    async def receive_text(self) -> str:
        return ""

    async def receive_json(self, mode: str = "text"):
        del mode
        return {}

    async def close(self, code: int = 1000):
        del code
        self.scrub()

    def scrub(self) -> None:
        """Release task-local references without closing the real client."""

        self._closed = True
        self.llm_context_user_id = None
        self._origin = None

    @property
    def client(self):
        origin = self._origin
        return getattr(origin, "client", ("durable-user-turn", 0))

    def __repr__(self) -> str:
        return "DurableUserTurnWebSocket(redacted=True)"


class BackgroundTaskManager:
    """Legacy async-task surface projected over an injected coordinator."""

    MAX_CONCURRENT_TASKS = 5
    _DISPATCH_POLL_SECONDS = 0.25
    _LEASE_RENEWAL_DIVISOR = 4
    _OBSERVABILITY_STALL_SECONDS = 0.25

    def __init__(
        self,
        coordinator: WorkAdmissionCoordinator | None = None,
        *,
        dispatch_poll_seconds: float = _DISPATCH_POLL_SECONDS,
    ) -> None:
        if dispatch_poll_seconds <= 0:
            raise ValueError("dispatch poll interval must be positive")
        self._coordinator = coordinator
        self._tasks: Dict[str, BackgroundTask] = {}
        self._pending_executions: Dict[str, _BackgroundExecution] = {}
        self._lock = asyncio.Lock()
        self._dispatch_poll_seconds = dispatch_poll_seconds
        self._dispatcher_task: asyncio.Task | None = None
        self._dispatcher_wakeup: asyncio.Event | None = None
        self._plane_runtime = None
        self._async_plane_runtime: AsyncPlaneRuntime | None = None
        self._background_tasks = None
        self._on_complete = None
        self._observability = None
        self._admission_observer_task: asyncio.Task | None = None
        self._admission_observation_requested = False
        self._compatibility_write_tasks: set[asyncio.Task] = set()
        self._draining = False
        self._drain_lock = asyncio.Lock()
        self._retention_task: asyncio.Task | None = None
        self._retention_stop: asyncio.Event | None = None

    def bind(
        self,
        *,
        coordinator=None,
        plane_runtime=None,
        plane_repositories=None,
        background_repository=None,
        on_complete=None,
        observability=None,
    ):
        """Additively bind operation authority and Plane-owned projections."""

        if coordinator is not None:
            if self._coordinator is not None and coordinator is not self._coordinator:
                raise RuntimeError("cannot replace the bound coordinator")
            self._coordinator = coordinator
        if plane_repositories is not None:
            catalog_repository = getattr(
                plane_repositories,
                "background_tasks",
                None,
            )
            if catalog_repository is None:
                raise ValueError(
                    "plane_repositories must expose background_tasks"
                )
            if (
                background_repository is not None
                and background_repository is not catalog_repository
            ):
                raise RuntimeError(
                    "background repository differs from the Plane catalog"
                )
            background_repository = catalog_repository
        if (plane_runtime is None) != (background_repository is None):
            raise ValueError(
                "plane_runtime and background repository must be bound together"
            )
        if plane_runtime is not None:
            if (
                self._plane_runtime is not None
                and plane_runtime is not self._plane_runtime
            ):
                raise RuntimeError("cannot replace the bound Plane runtime")
            if (
                self._background_tasks is not None
                and background_repository is not self._background_tasks
            ):
                raise RuntimeError(
                    "cannot replace the bound background-task repository"
                )
            if not callable(getattr(plane_runtime, "transaction", None)):
                raise TypeError("plane_runtime must expose transaction()")
            required_repository_methods = (
                "apply_operation_projection",
                "oldest_overdue_for_administration",
                "purge_overdue_for_administration",
            )
            if any(
                not callable(getattr(background_repository, method, None))
                for method in required_repository_methods
            ):
                raise TypeError(
                    "background repository does not expose the required Plane contract"
                )
            if self._plane_runtime is None:
                self._plane_runtime = plane_runtime
                self._background_tasks = background_repository
                self._async_plane_runtime = AsyncPlaneRuntime(
                    plane_runtime,
                    maximum_concurrency=2,
                )
        if on_complete is not None:
            self._on_complete = on_complete
        if observability is not None:
            if (
                self._observability is not None
                and observability is not self._observability
            ):
                raise RuntimeError("cannot replace the bound observability collector")
            self._observability = observability

    @staticmethod
    def _safe_observability_token(value: str | None, *, fallback: str) -> str:
        if isinstance(value, str) and _SAFE_OBSERVABILITY_TOKEN.fullmatch(value):
            return value
        return fallback

    @classmethod
    def _operation_kind(cls, bg_task: BackgroundTask | None = None) -> str:
        return cls._safe_observability_token(
            bg_task.kind if bg_task is not None else None,
            fallback="background_chat",
        )

    def _record_operation_observation(
        self,
        event: str,
        *,
        operation_kind: str,
        result_code: str | None = None,
        phase: str | None = None,
    ) -> None:
        observability = self._observability
        if observability is None:
            return
        safe_kind = self._safe_observability_token(
            operation_kind,
            fallback="background_chat",
        )
        safe_result = (
            self._safe_observability_token(result_code, fallback="unknown")
            if result_code is not None
            else None
        )
        safe_phase = (
            self._safe_observability_token(phase, fallback="unknown")
            if phase is not None
            else None
        )
        try:
            observability.record_operation(
                event,
                operation_kind=safe_kind,
                result_code=safe_result,
                phase=safe_phase,
            )
        except Exception:
            logger.debug("background operation telemetry failed", exc_info=True)

    async def _observe_admission(self) -> None:
        """Request one detached, coalesced effective-admission refresh."""

        observability = self._observability
        if observability is None or self._draining:
            return
        self._admission_observation_requested = True
        task = self._admission_observer_task
        if task is not None and not task.done():
            return
        self._admission_observer_task = asyncio.create_task(
            self._run_admission_observer(),
            name="background-admission-observer",
        )

    async def _run_admission_observer(self) -> None:
        """Refresh gauges off-loop without ever delaying lifecycle callers."""

        current_task = asyncio.current_task()
        try:
            while self._admission_observation_requested and not self._draining:
                self._admission_observation_requested = False
                observability = self._observability
                if observability is None:
                    return

                def _refresh() -> None:
                    status = self._require_coordinator().inspect_admission_class(
                        AdmissionClass.BACKGROUND
                    )
                    observability.observe_admission(
                        status,
                        operation_kind="background_chat",
                    )

                refresh = asyncio.create_task(
                    asyncio.to_thread(_refresh),
                    name="background-admission-inspection",
                )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(refresh),
                        timeout=self._OBSERVABILITY_STALL_SECONDS,
                    )
                except TimeoutError:
                    # Keep one coalescing owner until the already-running call
                    # returns. This bounds thread-pool pressure without making
                    # accepted/refused/terminal lifecycle paths await telemetry.
                    logger.warning("background admission telemetry is stalled")
                    try:
                        await refresh
                    except asyncio.CancelledError:
                        refresh.cancel()
                        await asyncio.gather(refresh, return_exceptions=True)
                        raise
                    except Exception:
                        logger.debug(
                            "background admission telemetry failed",
                            exc_info=True,
                        )
                except asyncio.CancelledError:
                    refresh.cancel()
                    await asyncio.gather(refresh, return_exceptions=True)
                    raise
                except Exception:
                    logger.debug(
                        "background admission telemetry failed",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("background admission telemetry failed", exc_info=True)
        finally:
            if self._admission_observer_task is current_task:
                self._admission_observer_task = None
            if self._draining:
                self._admission_observation_requested = False

    async def _observe_terminal(self, bg_task: BackgroundTask) -> None:
        operation = bg_task._operation
        if (
            self._observability is None
            or operation is None
            or operation.state not in _TERMINAL_OPERATION_STATES
            or bg_task._terminal_observed
        ):
            return
        bg_task._terminal_observed = True
        operation_kind = self._operation_kind(bg_task)
        result_code = operation.terminal_code or operation.state.value
        if operation.terminal_code == "queue_wait_expired":
            self._record_operation_observation(
                "queue_expired",
                operation_kind=operation_kind,
                result_code="queue_wait_expired",
            )
        self._record_operation_observation(
            operation.state.value,
            operation_kind=operation_kind,
            result_code=result_code,
        )
        self._record_operation_observation(
            "terminal",
            operation_kind=operation_kind,
            result_code=operation.state.value,
        )
        await self._observe_admission()

    async def _refuse_service_draining(self, operation_kind: str) -> None:
        self._record_operation_observation(
            "refused",
            operation_kind=operation_kind,
            result_code="service_draining",
        )
        await self._observe_admission()
        raise self._service_draining_error()

    @staticmethod
    def _service_draining_error() -> BackgroundTaskAdmissionError:
        return BackgroundTaskAdmissionError(
            "service_draining",
            retryable=True,
            retry_after_ms=1000,
        )

    def _require_coordinator(self) -> WorkAdmissionCoordinator:
        if self._coordinator is None:
            raise BackgroundTaskManagerNotBoundError(
                "bind an explicit WorkAdmissionCoordinator before submitting work"
            )
        return self._coordinator

    def _lease_renewal_seconds(self) -> float:
        coordinator = self._require_coordinator()
        lease_seconds = coordinator.slot_lease.total_seconds()
        if lease_seconds <= 0:  # pragma: no cover - coordinator validates this
            raise RuntimeError("execution slot lease must be positive")
        # Renew before the contractual one-third boundary to leave scheduling
        # and database latency margin rather than treating the bound as a
        # best-effort sleep duration.
        return lease_seconds / self._LEASE_RENEWAL_DIVISOR

    def _wake_dispatcher(self) -> None:
        if self._dispatcher_wakeup is not None:
            self._dispatcher_wakeup.set()

    def _ensure_dispatcher_locked(self) -> None:
        if not self._pending_executions:
            return
        if self._dispatcher_task is not None and not self._dispatcher_task.done():
            self._wake_dispatcher()
            return
        self._dispatcher_wakeup = asyncio.Event()
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_pending(), name="background-operation-dispatcher"
        )

    @staticmethod
    def _projection_record(
        bg_task: BackgroundTask,
        *,
        summary: str | None = None,
        notified: bool = False,
    ) -> PlaneBackgroundTaskRecord:
        """Build the exact Plane projection for one managed operation."""

        if bg_task._operation is None:
            raise RuntimeError("background projection requires operation authority")
        return PlaneBackgroundTaskRecord(
            task_id=bg_task.task_id,
            owner_id=bg_task.user_id,
            conversation_id=bg_task.chat_id,
            kind=bg_task.kind,
            status=PlaneBackgroundTaskStatus(
                bg_task._canonical_status().value
            ),
            title=bg_task.title,
            summary=summary,
            created_at=bg_task.created_at,
            completed_at=bg_task.completed_at,
            notified=notified,
            operation_id=bg_task.task_id,
            operation_execution_generation=(
                bg_task.operation_execution_generation
            ),
        )

    def _project(self, record: PlaneBackgroundTaskRecord) -> None:
        """Best-effort typed projection over the shared Plane runtime."""

        if (
            self._draining
            or self._async_plane_runtime is None
            or self._background_tasks is None
            or not flags.is_enabled("bg_continuity")
        ):
            return

        runtime = self._async_plane_runtime
        repository = self._background_tasks

        async def _write():
            try:
                await runtime.run_in_transaction(
                    lambda transaction: repository.apply_operation_projection(
                        transaction,
                        record,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "background task Plane projection failed",
                    exc_info=True,
                )

        task = asyncio.create_task(
            _write(),
            name="background-plane-projection",
        )
        self._compatibility_write_tasks.add(task)
        task.add_done_callback(self._compatibility_write_done)

    def _compatibility_write_done(self, task: asyncio.Task) -> None:
        self._compatibility_write_tasks.discard(task)
        self._consume_drain_helper(task)

    async def submit(
        self,
        chat_id: str,
        user_id: str,
        coro_factory,
        *args,
        kind: str = "async_chat",
        title: str = "",
        connection_generation: uuid.UUID | None = None,
        request_generation: uuid.UUID | None = None,
        **kwargs,
    ) -> BackgroundTask:
        """Durably admit one background operation and project its task DTO."""

        coordinator = self._require_coordinator()
        operation_kind = self._safe_observability_token(
            kind,
            fallback="background_chat",
        )
        if self._draining:
            await self._refuse_service_draining(operation_kind)
        owner = OperationOwner(
            owner_scope=OwnerScope.USER,
            owner_user_id=user_id,
            connection_scope_id=None,
        )
        request = OperationRequest(
            operation_kind=kind,
            admission_class=AdmissionClass.BACKGROUND,
            owner=owner,
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=chat_id,
            parent_operation_id=None,
            connection_generation=connection_generation,
            request_generation=request_generation,
        )

        accepted_while_draining = False
        async with self._lock:
            if self._draining:
                await self._refuse_service_draining(operation_kind)
            admitted = await asyncio.to_thread(coordinator.submit, request)
            if not admitted.accepted:
                self._record_operation_observation(
                    "refused",
                    operation_kind=operation_kind,
                    result_code=admitted.code,
                )
                await self._observe_admission()
                raise BackgroundTaskAdmissionError(
                    admitted.code,
                    retryable=admitted.retryable,
                    retry_after_ms=admitted.retry_after_ms,
                )
            if self._draining:
                operation = await asyncio.to_thread(
                    coordinator.cancel,
                    owner=owner,
                    operation_id=admitted.operation_id,
                    terminal_code="service_draining",
                )
                accepted_while_draining = True
            else:
                operation = await asyncio.to_thread(
                    coordinator.query_operation,
                    owner=owner,
                    operation_id=admitted.operation_id,
                )
                if self._draining:
                    operation = await asyncio.to_thread(
                        coordinator.cancel,
                        owner=owner,
                        operation_id=admitted.operation_id,
                        terminal_code="service_draining",
                    )
                    accepted_while_draining = True
            bg_task = BackgroundTask(
                task_id=str(admitted.operation_id),
                chat_id=chat_id,
                user_id=user_id,
                kind=kind,
                title=title,
            )
            bg_task._attach_authority(
                owner=owner,
                operation=operation,
                execution_fence=None,
            )
            self._tasks[bg_task.task_id] = bg_task
            if not accepted_while_draining:
                self._pending_executions[bg_task.task_id] = _BackgroundExecution(
                    coro_factory=coro_factory,
                    args=tuple(args),
                    kwargs=dict(kwargs),
                )

            if not accepted_while_draining and admitted.state is OperationState.RUNNING:
                try:
                    claim = await asyncio.to_thread(
                        coordinator.claim_operation,
                        AdmissionClass.BACKGROUND,
                        admitted.operation_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    claim = None
                    logger.exception(
                        "background operation %s initial handoff will retry",
                        bg_task.task_id,
                    )
                if self._draining:
                    self._pending_executions.pop(bg_task.task_id, None)
                    if claim is not None:
                        operation = await asyncio.to_thread(
                            coordinator.terminalize,
                            claim.fence,
                            state=OperationState.CANCELLED,
                            terminal_code="service_draining",
                            safe_summary="Service draining",
                            retry_after_ms=None,
                        )
                    else:
                        operation = await asyncio.to_thread(
                            coordinator.cancel,
                            owner=owner,
                            operation_id=admitted.operation_id,
                            terminal_code="service_draining",
                        )
                    bg_task._apply_operation(operation, execution_fence=None)
                    accepted_while_draining = True
                elif claim is not None:
                    self._start_claimed_task_locked(bg_task, claim)
                else:
                    logger.warning(
                        "background operation %s was accepted but not selected locally",
                        bg_task.task_id,
                    )
            if not accepted_while_draining:
                self._ensure_dispatcher_locked()

        self._record_operation_observation(
            "accepted",
            operation_kind=operation_kind,
        )
        await self._observe_admission()
        if accepted_while_draining:
            await self._observe_terminal(bg_task)

        self._project(self._projection_record(bg_task))
        return bg_task

    def _start_claimed_task_locked(self, bg_task: BackgroundTask, claim: Any) -> bool:
        """Start exactly one locally retained callable for an exact claim."""

        descriptor = self._pending_executions.get(bg_task.task_id)
        if descriptor is None:
            return False
        if bg_task.asyncio_task is not None and not bg_task.asyncio_task.done():
            return False
        if str(claim.operation.operation_id) != bg_task.task_id:
            raise RuntimeError("background dispatcher claimed the wrong operation")
        bg_task._apply_operation(claim.operation, execution_fence=claim.fence)
        bg_task._lease_loss_reason = None
        self._pending_executions.pop(bg_task.task_id, None)
        vws = VirtualWebSocket(bg_task)
        bg_task._virtual_websocket = vws
        bg_task.asyncio_task = asyncio.create_task(
            self._run_task(
                bg_task,
                vws,
                descriptor.coro_factory,
                *descriptor.args,
                **descriptor.kwargs,
            ),
            name=f"background-operation-{bg_task.task_id}",
        )
        return True

    async def _dispatch_pending(self) -> None:
        """Claim retained operations in durable FIFO order until none remain."""

        coordinator = self._require_coordinator()
        current_task = asyncio.current_task()
        try:
            while True:
                notify: list[BackgroundTask] = []
                made_progress = False
                wakeup: asyncio.Event | None = None
                async with self._lock:
                    pending = sorted(
                        (
                            self._tasks[task_id]
                            for task_id in self._pending_executions
                            if task_id in self._tasks
                        ),
                        key=lambda task: (
                            task.created_at,
                            uuid.UUID(task.task_id).int,
                        ),
                    )
                    if not pending:
                        return

                    for bg_task in pending:
                        try:
                            claim = await asyncio.to_thread(
                                coordinator.claim_operation,
                                AdmissionClass.BACKGROUND,
                                uuid.UUID(bg_task.task_id),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception(
                                "background operation %s could not be claimed",
                                bg_task.task_id,
                            )
                            break

                        if claim is not None:
                            made_progress = (
                                self._start_claimed_task_locked(bg_task, claim)
                                or made_progress
                            )
                            continue

                        if bg_task._owner is None:
                            self._pending_executions.pop(bg_task.task_id, None)
                            continue
                        try:
                            operation = await asyncio.to_thread(
                                coordinator.query_operation,
                                owner=bg_task._owner,
                                operation_id=uuid.UUID(bg_task.task_id),
                            )
                        except OperationNotFoundError:
                            self._pending_executions.pop(bg_task.task_id, None)
                            self._tasks.pop(bg_task.task_id, None)
                            continue
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception(
                                "background operation %s could not be refreshed",
                                bg_task.task_id,
                            )
                            break
                        operation = self._reconcile_operation_projection(
                            bg_task,
                            operation,
                        )
                        if operation.state in _TERMINAL_OPERATION_STATES:
                            self._pending_executions.pop(bg_task.task_id, None)
                            notify.append(bg_task)
                            continue
                        if operation.state is OperationState.QUEUED:
                            # Exact claims preserve global FIFO.  If the first
                            # local queued candidate cannot claim, no later
                            # queued candidate can either; avoid an O(queue)
                            # database poll while capacity is saturated.
                            break

                    if self._pending_executions and not made_progress:
                        wakeup = self._dispatcher_wakeup
                        if wakeup is not None:
                            wakeup.clear()

                for bg_task in notify:
                    await self._observe_terminal(bg_task)
                    await self._notify_watchers(bg_task)
                if made_progress:
                    await asyncio.sleep(0)
                    continue
                if wakeup is None:
                    return
                try:
                    await asyncio.wait_for(
                        wakeup.wait(), timeout=self._dispatch_poll_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            async with self._lock:
                if self._dispatcher_task is current_task:
                    self._dispatcher_task = None
                    self._dispatcher_wakeup = None

    async def _terminalize(
        self,
        bg_task: BackgroundTask,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None,
        retry_after_ms: int | None = None,
    ) -> OperationProjection | None:
        coordinator = self._require_coordinator()
        fence = bg_task._execution_fence
        if fence is None:
            return None
        try:
            operation = await asyncio.to_thread(
                coordinator.terminalize,
                fence,
                state=state,
                terminal_code=terminal_code,
                safe_summary=safe_summary,
                retry_after_ms=retry_after_ms,
            )
        except StaleExecutionFenceError:
            bg_task._execution_fence = None
            if bg_task._owner is None:
                return None
            operation = await asyncio.to_thread(
                coordinator.query_operation,
                owner=bg_task._owner,
                operation_id=fence.operation_id,
            )
        operation = self._reconcile_operation_projection(bg_task, operation)
        await self._observe_terminal(bg_task)
        return operation

    async def _handle_execution_lease_loss(
        self,
        bg_task: BackgroundTask,
        vws: VirtualWebSocket,
        *,
        stale: bool,
        worker: asyncio.Task | None,
    ) -> None:
        """Revoke local output/effect authority and stop a lost execution."""

        coordinator = self._require_coordinator()
        fence = bg_task._execution_fence
        bg_task._lease_loss_reason = (
            "execution_lease_lost" if stale else "execution_lease_renewal_failed"
        )
        await vws.close()

        operation: OperationProjection | None = None
        if stale:
            bg_task._execution_fence = None
        if stale and bg_task._owner is not None and fence is not None:
            try:
                operation = await asyncio.to_thread(
                    coordinator.query_operation,
                    owner=bg_task._owner,
                    operation_id=fence.operation_id,
                )
            except OperationNotFoundError:
                operation = None
            except Exception:
                logger.exception(
                    "background operation %s could not refresh after lease loss",
                    bg_task.task_id,
                )
        if operation is not None:
            operation = self._reconcile_operation_projection(bg_task, operation)
            if operation.state in _TERMINAL_OPERATION_STATES:
                await self._observe_terminal(bg_task)
                await self._notify_watchers(bg_task)

        if worker is not None and worker is not asyncio.current_task():
            if not worker.done():
                worker.cancel()

    async def _renew_execution_lease_loop(
        self,
        bg_task: BackgroundTask,
        vws: VirtualWebSocket,
        worker: asyncio.Task,
    ) -> None:
        coordinator = self._require_coordinator()
        interval = self._lease_renewal_seconds()
        loop = asyncio.get_running_loop()
        next_renewal = loop.time() + interval
        while True:
            await asyncio.sleep(max(0.0, next_renewal - loop.time()))
            next_renewal += interval
            fence = bg_task._execution_fence
            if fence is None:
                return
            try:
                await asyncio.to_thread(coordinator.renew_execution_lease, fence)
            except asyncio.CancelledError:
                raise
            except StaleExecutionFenceError:
                logger.warning(
                    "background operation %s lost its execution lease",
                    bg_task.task_id,
                )
                await self._handle_execution_lease_loss(
                    bg_task,
                    vws,
                    stale=True,
                    worker=worker,
                )
                return
            except Exception:
                logger.exception(
                    "background operation %s could not renew its execution lease",
                    bg_task.task_id,
                )
                await self._handle_execution_lease_loss(
                    bg_task,
                    vws,
                    stale=False,
                    worker=worker,
                )
                return

    async def _run_task(
        self,
        bg_task: BackgroundTask,
        vws: VirtualWebSocket,
        coro_factory,
        *args,
        **kwargs,
    ) -> None:
        terminal_state: OperationState | None = OperationState.COMPLETED
        terminal_code = None
        retry_after_ms = None
        try:
            fence = bg_task._execution_fence
            if fence is not None:
                try:
                    await asyncio.to_thread(
                        self._require_coordinator().renew_execution_lease,
                        fence,
                    )
                except StaleExecutionFenceError:
                    await self._handle_execution_lease_loss(
                        bg_task,
                        vws,
                        stale=True,
                        worker=None,
                    )
                    terminal_state = None
                    return
                except Exception:
                    logger.exception(
                        "background operation %s could not establish its execution lease",
                        bg_task.task_id,
                    )
                    await self._handle_execution_lease_loss(
                        bg_task,
                        vws,
                        stale=False,
                        worker=None,
                    )
                    terminal_state = OperationState.RETRYABLE
                    terminal_code = "execution_lease_renewal_failed"
                    retry_after_ms = 1000
                    return
                worker = asyncio.current_task()
                if worker is None:  # pragma: no cover - coroutine always runs in a task
                    raise RuntimeError("background work requires an asyncio task")
                bg_task._lease_task = asyncio.create_task(
                    self._renew_execution_lease_loop(bg_task, vws, worker),
                    name=f"background-lease-{bg_task.task_id}",
                )
            logger.info(
                "Background task %s started (chat=%s user=%s)",
                bg_task.task_id,
                bg_task.chat_id,
                bg_task.user_id,
            )
            await coro_factory(vws, *args, **kwargs)
        except asyncio.CancelledError:
            if bg_task._lease_loss_reason == "execution_lease_lost":
                terminal_state = None
            elif bg_task._lease_loss_reason == "execution_lease_renewal_failed":
                terminal_state = OperationState.RETRYABLE
                terminal_code = "execution_lease_renewal_failed"
                retry_after_ms = 1000
            else:
                terminal_state = OperationState.CANCELLED
                terminal_code = (
                    bg_task._cancellation_terminal_code or "cancelled_by_user"
                )
        except Exception as exc:
            terminal_state = OperationState.FAILED
            terminal_code = "operation_failed"
            bg_task.errors.append(str(exc))
            logger.error(
                "Background task %s failed: %s",
                bg_task.task_id,
                exc,
                exc_info=True,
            )
        finally:
            lease_task = bg_task._lease_task
            if lease_task is not None and lease_task is not asyncio.current_task():
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)
            bg_task._lease_task = None
            await vws.close()
            if bg_task._virtual_websocket is vws:
                bg_task._virtual_websocket = None

            if terminal_state is OperationState.FAILED:
                safe_summary = "Background task failed"
            elif terminal_state is OperationState.CANCELLED:
                safe_summary = bg_task._cancellation_safe_summary or "Cancelled"
            elif terminal_state is OperationState.RETRYABLE:
                safe_summary = "Execution lease renewal failed"
            else:
                safe_summary = "Completed"
            operation = None
            if terminal_state is not None:
                try:
                    operation = await self._terminalize(
                        bg_task,
                        state=terminal_state,
                        terminal_code=terminal_code,
                        safe_summary=safe_summary,
                        retry_after_ms=retry_after_ms,
                    )
                except Exception:
                    logger.exception(
                        "background operation %s could not be terminalized",
                        bg_task.task_id,
                    )
            if operation is not None and operation.state in _TERMINAL_OPERATION_STATES:
                await self._notify_watchers(bg_task)
            self._wake_dispatcher()

    async def _notify_watchers(self, bg_task: BackgroundTask) -> None:
        if bg_task._notification_sent:
            return
        bg_task._notification_sent = True
        notification = {
            "type": "task_completed",
            "payload": {
                "task_id": bg_task.task_id,
                "chat_id": bg_task.chat_id,
                "status": bg_task._canonical_status().value,
                "completed_at": (
                    bg_task._operation.terminal_at.isoformat()
                    if bg_task._operation is not None
                    and bg_task._operation.terminal_at is not None
                    else None
                ),
            },
        }
        if bg_task._canonical_status() is TaskStatus.COMPLETED:
            summary = self._summary_from_outputs(bg_task)
        elif bg_task._operation is not None:
            summary = bg_task._operation.safe_summary or "Background task failed"
        else:  # pragma: no cover - managed notifications require an operation
            summary = "Background task failed"
        fanned = 0
        if self._on_complete is not None and flags.is_enabled("bg_continuity"):
            notification["payload"]["summary"] = summary
            try:
                fanned = int(await self._on_complete(bg_task, notification) or 0)
            except Exception:
                logger.debug(
                    "completion fan failed for task %s",
                    bg_task.task_id,
                    exc_info=True,
                )
        for ws in bg_task.watchers:
            try:
                await ws.send_text(json.dumps(notification))
            except Exception:
                logger.debug(
                    "Failed to notify watcher for task %s",
                    bg_task.task_id,
                    exc_info=True,
                )
        bg_task.watchers.clear()
        self._project(
            self._projection_record(
                bg_task,
                summary=summary,
                notified=fanned > 0,
            )
        )

    @staticmethod
    def _summary_from_outputs(bg_task: BackgroundTask) -> str:
        summary = ""
        for out in bg_task.outputs:
            if not isinstance(out, dict):
                continue
            if out.get("type") == "ui_render" and out.get("target") == "chat":
                for comp in out.get("components") or []:
                    content = comp.get("content") if isinstance(comp, dict) else None
                    if isinstance(content, str) and content.strip():
                        summary = content.strip()
                        break
                continue
            text = out.get("text") or out.get("message")
            if not text and isinstance(out.get("payload"), dict):
                text = out["payload"].get("text") or out["payload"].get("message")
            if isinstance(text, str) and text.strip():
                summary = text.strip()
        if not summary and bg_task.errors:
            summary = str(bg_task.errors[-1])
        return " ".join(summary.split())[:200]

    @staticmethod
    def _reconcile_operation_projection(
        bg_task: BackgroundTask,
        candidate: OperationProjection,
    ) -> OperationProjection:
        """Keep the newest projection when coordinator calls finish out of order.

        Durable cancellation and terminalization serialize at the coordinator,
        but their results return to the event loop independently. A worker can
        therefore apply a newer terminal record while an earlier cancellation
        response is still in flight. The DTO's strict monotonic guard remains
        valuable for direct callers; manager-owned reconciliation discards only
        an authority-issued response whose revision is already known to be stale.
        """

        if str(candidate.operation_id) != bg_task.task_id:
            raise RuntimeError("background task operation identity changed")
        current = bg_task._operation
        if current is None:
            bg_task._apply_operation(candidate)
            return candidate
        if candidate.state_revision < current.state_revision:
            return current
        if candidate.state_revision == current.state_revision:
            if type(candidate) is type(current):
                equivalent = candidate == current
            else:
                equivalent = all(
                    getattr(candidate, field_name) == getattr(current, field_name)
                    for field_name in _SAFE_OPERATION_PROJECTION_FIELDS
                )
            if not equivalent:
                raise RuntimeError(
                    "background task operation projection changed without "
                    "advancing its revision"
                )
            # Owner-safe and full records are different views of the same
            # authority. Do not downgrade, upgrade, or otherwise swap the
            # already-applied representation at an unchanged revision.
            return current
        bg_task._apply_operation(candidate)
        return candidate

    async def _refresh(self, bg_task: BackgroundTask) -> bool:
        coordinator = self._require_coordinator()
        if bg_task._owner is None:
            return True
        try:
            operation = await asyncio.to_thread(
                coordinator.query_operation,
                owner=bg_task._owner,
                operation_id=uuid.UUID(bg_task.task_id),
            )
        except OperationNotFoundError:
            return False
        self._reconcile_operation_projection(bg_task, operation)
        await self._observe_terminal(bg_task)
        return True

    async def _cancel_and_cleanup(
        self,
        bg_task: BackgroundTask,
        *,
        owner: OperationOwner,
        coordinator: WorkAdmissionCoordinator,
    ) -> bool:
        """Commit cancellation and settle its process-local execution."""

        task_id = bg_task.task_id
        worker: asyncio.Task | None
        websocket: VirtualWebSocket | None

        try:
            operation = await asyncio.to_thread(
                coordinator.cancel,
                owner=owner,
                operation_id=uuid.UUID(bg_task.task_id),
                terminal_code="cancelled_by_user",
            )
        except OperationNotFoundError:
            async with self._lock:
                if self._tasks.get(task_id) is bg_task:
                    self._pending_executions.pop(task_id, None)
                    self._tasks.pop(task_id, None)
            return False

        response_requested_cancellation = (
            operation.state is OperationState.RUNNING
            and operation.cancel_requested_at is not None
        )
        operation = self._reconcile_operation_projection(bg_task, operation)
        async with self._lock:
            # Terminalization may have advanced the local authority while this
            # coroutine waited to reacquire the state lock.
            operation = self._reconcile_operation_projection(bg_task, operation)
            cancellation_effective = (
                operation.state is OperationState.CANCELLED
                or (
                    operation.state is OperationState.RUNNING
                    and (
                        response_requested_cancellation
                        or getattr(operation, "cancel_requested_at", None)
                        is not None
                    )
                )
            )
            if (
                cancellation_effective
                or operation.state in _TERMINAL_OPERATION_STATES
            ):
                self._pending_executions.pop(task_id, None)
            worker = bg_task.asyncio_task
            websocket = bg_task._virtual_websocket

        await self._observe_terminal(bg_task)

        if cancellation_effective and websocket is not None:
            await websocket.close()

        if (
            cancellation_effective
            and operation.state is OperationState.RUNNING
            and bg_task._execution_fence is not None
            and (worker is None or worker.done())
        ):
            terminal = await self._terminalize(
                bg_task,
                state=OperationState.CANCELLED,
                terminal_code="cancelled_by_user",
                safe_summary="Cancelled",
            )
            if terminal is not None:
                operation = terminal

        if cancellation_effective and worker is not None and not worker.done():
            worker.cancel()
            if worker is not asyncio.current_task():
                await asyncio.gather(worker, return_exceptions=True)

        # Cancellation before a newly created asyncio task's first scheduling
        # does not enter that coroutine's ``finally`` block.  Only after the
        # wrapper is fully joined may this fallback clear the durable slot;
        # otherwise a replacement could start while old user cleanup runs.
        if cancellation_effective and (worker is None or worker.done()):
            if await self._refresh(bg_task):
                if (
                    bg_task._operation is not None
                    and bg_task._operation.state is OperationState.RUNNING
                    and bg_task._execution_fence is not None
                ):
                    await self._terminalize(
                        bg_task,
                        state=OperationState.CANCELLED,
                        terminal_code="cancelled_by_user",
                        safe_summary="Cancelled",
                    )
        notify = (
            bg_task._operation is not None
            and bg_task._operation.state in _TERMINAL_OPERATION_STATES
        )
        self._wake_dispatcher()
        if notify:
            await self._notify_watchers(bg_task)
        final_operation = bg_task._operation
        if final_operation is None:
            return False
        if final_operation.state in _TERMINAL_OPERATION_STATES:
            return final_operation.state is OperationState.CANCELLED
        return cancellation_effective

    async def cancel(self, task_id: str) -> bool:
        coordinator = self._require_coordinator()
        async with self._lock:
            bg_task = self._tasks.get(task_id)
            if bg_task is None or bg_task._owner is None:
                return False
            owner = bg_task._owner

        # Coordinator calls may block on PostgreSQL row/class locks. Never
        # retain the process-local state lock across that I/O: worker
        # terminalization and unrelated submissions must remain able to make
        # progress while this request is in flight.
        if not await self._refresh(bg_task):
            async with self._lock:
                if self._tasks.get(task_id) is bg_task:
                    self._pending_executions.pop(task_id, None)
                    self._tasks.pop(task_id, None)
            return False
        if (
            bg_task._operation is None
            or bg_task._operation.state in _TERMINAL_OPERATION_STATES
        ):
            return False

        cancellation_flow = asyncio.create_task(
            self._cancel_and_cleanup(
                bg_task,
                owner=owner,
                coordinator=coordinator,
            ),
            name=f"background-cancellation-{task_id}",
        )
        try:
            return await asyncio.shield(cancellation_flow)
        except asyncio.CancelledError as caller_cancellation:
            # ``asyncio.to_thread`` cannot stop its worker thread. Once the
            # coordinator call may have committed, join the shielded flow so
            # its response is reconciled and local execution is closed before
            # honoring cancellation of this caller. Repeated cancellation of
            # the caller must not strand the committed cleanup either.
            completion = asyncio.get_running_loop().create_future()

            def mark_completed(_task: asyncio.Task[bool]) -> None:
                if not completion.done():
                    completion.set_result(None)

            cancellation_flow.add_done_callback(mark_completed)
            while not cancellation_flow.done():
                try:
                    # Wait only for completion here. Awaiting the flow itself
                    # could surface its ordinary exception from this loop and
                    # bypass the stable caller-cancellation precedence below.
                    await asyncio.shield(completion)
                except asyncio.CancelledError:
                    continue
            cancellation_flow.remove_done_callback(mark_completed)
            try:
                cancellation_flow.result()
            except asyncio.CancelledError:
                pass
            except Exception as cleanup_error:
                raise caller_cancellation from cleanup_error
            raise

    @staticmethod
    def _consume_drain_helper(task: asyncio.Task) -> None:
        """Retrieve helper failures without extending the bounded drain."""

        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            return

    async def _request_service_cancellation(
        self,
        bg_task: BackgroundTask,
    ) -> None:
        if bg_task._owner is None or bg_task._operation is None:
            return
        if bg_task._operation.state in _TERMINAL_OPERATION_STATES:
            await self._observe_terminal(bg_task)
            return
        try:
            operation = await asyncio.to_thread(
                self._require_coordinator().cancel,
                owner=bg_task._owner,
                operation_id=uuid.UUID(bg_task.task_id),
                terminal_code="service_draining",
            )
        except OperationNotFoundError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "background operation %s shutdown cancellation failed",
                bg_task.task_id,
            )
            return
        self._reconcile_operation_projection(bg_task, operation)
        await self._observe_terminal(bg_task)

    async def _force_service_terminal(
        self,
        bg_task: BackgroundTask,
    ) -> None:
        operation = bg_task._operation
        if operation is None:
            return
        if operation.state in _TERMINAL_OPERATION_STATES:
            bg_task._execution_fence = None
            await self._observe_terminal(bg_task)
            return
        if bg_task._execution_fence is not None:
            try:
                terminal = await self._terminalize(
                    bg_task,
                    state=OperationState.CANCELLED,
                    terminal_code="service_draining",
                    safe_summary="Service draining",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "background operation %s shutdown terminalization failed",
                    bg_task.task_id,
                )
                return
            if terminal is not None and terminal.state in _TERMINAL_OPERATION_STATES:
                bg_task._execution_fence = None
            return
        await self._request_service_cancellation(bg_task)
        if (
            bg_task._operation is not None
            and bg_task._operation.state in _TERMINAL_OPERATION_STATES
        ):
            bg_task._execution_fence = None

    async def drain(self, *, timeout_seconds: float = 5.0) -> int:
        """Boundedly fence and settle all locally retained background work.

        The manager remains permanently draining after this call: subsequent
        submissions receive the explicit retryable ``service_draining``
        refusal.  The returned integer is the count of cancellation-resistant
        user coroutines still executing locally after their durable operation
        and captured-output authority have both been revoked.
        """

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("drain timeout must be finite and positive")
        self._require_coordinator()
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + float(timeout_seconds)
        graceful_deadline = started_at + (float(timeout_seconds) * 0.7)

        # Publish the irreversible service state before any lock acquisition.
        # A submitter already waiting on coordinator I/O will observe this flag
        # when that I/O returns and settle its just-accepted pre-handoff record.
        self._draining = True
        drain_lock_acquired = False
        state_lock_acquired = False
        background_tasks: tuple[BackgroundTask, ...] = ()
        dispatcher: asyncio.Task | None = None
        retention: asyncio.Task | None = None
        admission_observer: asyncio.Task | None = None
        workers: set[asyncio.Task] = set()
        lease_tasks: set[asyncio.Task] = set()
        compatibility_writes: set[asyncio.Task] = set()
        infrastructure_tasks: set[asyncio.Task] = set()
        cancellation_helpers: set[asyncio.Task] = set()
        force_helpers: set[asyncio.Task] = set()
        try:
            if self._drain_lock.locked():
                remaining = max(0.0, deadline - loop.time())
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(
                    self._drain_lock.acquire(),
                    timeout=remaining,
                )
            else:
                await self._drain_lock.acquire()
            drain_lock_acquired = True

            if self._lock.locked():
                remaining = max(0.0, graceful_deadline - loop.time())
                if remaining > 0:
                    try:
                        await asyncio.wait_for(
                            self._lock.acquire(),
                            timeout=remaining,
                        )
                        state_lock_acquired = True
                    except TimeoutError:
                        pass
            else:
                await self._lock.acquire()
                state_lock_acquired = True

            # This snapshot is event-loop atomic even if a coordinator call is
            # holding the logical state lock in another suspended coroutine.
            # No new submitter may pass its draining checks, and every awaited
            # submit/claim path rechecks the flag before starting user code.
            self._pending_executions.clear()
            self._wake_dispatcher()
            background_tasks = tuple(self._tasks.values())
            dispatcher = self._dispatcher_task
            retention = self._retention_task
            admission_observer = self._admission_observer_task
            self._admission_observation_requested = False
            compatibility_writes = {
                task
                for task in self._compatibility_write_tasks
                if not task.done()
            }
            if self._retention_stop is not None:
                self._retention_stop.set()
            for bg_task in background_tasks:
                bg_task._cancellation_terminal_code = "service_draining"
                bg_task._cancellation_safe_summary = "Service draining"
                if bg_task._virtual_websocket is not None:
                    await bg_task._virtual_websocket.close()
            if state_lock_acquired:
                self._lock.release()
                state_lock_acquired = False

            workers = {
                bg_task.asyncio_task
                for bg_task in background_tasks
                if bg_task.asyncio_task is not None
                and not bg_task.asyncio_task.done()
            }
            lease_tasks = {
                bg_task._lease_task
                for bg_task in background_tasks
                if bg_task._lease_task is not None
                and not bg_task._lease_task.done()
            }
            infrastructure_tasks = {
                task
                for task in (
                    dispatcher,
                    retention,
                    admission_observer,
                    *lease_tasks,
                    *compatibility_writes,
                )
                if task is not None and not task.done()
            }
            for task in (*infrastructure_tasks, *workers):
                task.cancel()

            cancellation_helpers = {
                asyncio.create_task(
                    self._request_service_cancellation(bg_task),
                    name=f"background-drain-cancel-{bg_task.task_id}",
                )
                for bg_task in background_tasks
                if bg_task._operation is not None
                and bg_task._operation.state not in _TERMINAL_OPERATION_STATES
            }
            graceful_tasks = {
                *infrastructure_tasks,
                *workers,
                *cancellation_helpers,
            }
            if graceful_tasks:
                _, pending = await asyncio.wait(
                    graceful_tasks,
                    timeout=max(0.0, graceful_deadline - loop.time()),
                )
                for helper in pending.intersection(cancellation_helpers):
                    helper.add_done_callback(self._consume_drain_helper)
                    helper.cancel()

            force_helpers = {
                asyncio.create_task(
                    self._force_service_terminal(bg_task),
                    name=f"background-drain-terminal-{bg_task.task_id}",
                )
                for bg_task in background_tasks
                if bg_task._operation is not None
                and bg_task._operation.state not in _TERMINAL_OPERATION_STATES
            }
            if force_helpers:
                _, pending_force = await asyncio.wait(
                    force_helpers,
                    timeout=max(0.0, deadline - loop.time()),
                )
                for helper in pending_force:
                    helper.add_done_callback(self._consume_drain_helper)
                    helper.cancel()
        except TimeoutError:
            # A concurrent drain owns settlement. This caller still obeys its
            # own deadline and reports the currently visible local remainder.
            background_tasks = tuple(self._tasks.values())
            workers = {
                bg_task.asyncio_task
                for bg_task in background_tasks
                if bg_task.asyncio_task is not None
                and not bg_task.asyncio_task.done()
            }
        finally:
            if state_lock_acquired:
                self._lock.release()
            for task in (*infrastructure_tasks, *workers):
                if not task.done():
                    task.add_done_callback(self._consume_drain_helper)
                    task.cancel()
            for helper in (*cancellation_helpers, *force_helpers):
                if not helper.done():
                    helper.add_done_callback(self._consume_drain_helper)
                    helper.cancel()
            for bg_task in background_tasks:
                # No local worker retains publication authority after the
                # bounded deadline, including when its final database CAS is
                # still unavailable. The durable lease may recover elsewhere,
                # but this process can emit nothing late.
                bg_task._execution_fence = None
                if bg_task._lease_task in lease_tasks:
                    bg_task._lease_task = None
                bg_task.watchers.clear()
            if self._dispatcher_task is dispatcher:
                self._dispatcher_task = None
                self._dispatcher_wakeup = None
            if self._retention_task is retention:
                self._retention_task = None
                self._retention_stop = None
            if self._admission_observer_task is admission_observer:
                self._admission_observer_task = None
            self._admission_observation_requested = False
            self._compatibility_write_tasks.difference_update(
                task for task in compatibility_writes if task.done()
            )
            if self._async_plane_runtime is not None:
                self._async_plane_runtime.close()
            if drain_lock_acquired:
                self._drain_lock.release()

        remainder = sum(not worker.done() for worker in workers)
        self._record_operation_observation(
            "drain",
            operation_kind="background_chat",
            result_code=("complete" if remainder == 0 else "fenced_remainder"),
            phase="shutdown",
        )
        return remainder

    async def get(self, task_id: str) -> Optional[BackgroundTask]:
        bg_task = self._tasks.get(task_id)
        if bg_task is None:
            return None
        if not await self._refresh(bg_task):
            self._pending_executions.pop(task_id, None)
            self._tasks.pop(task_id, None)
            return None
        return bg_task

    async def list_for_user(
        self, user_id: str, limit: int = 20
    ) -> List[BackgroundTask]:
        user_tasks = []
        for task_id, task in tuple(self._tasks.items()):
            if task.user_id != user_id:
                continue
            if await self._refresh(task):
                user_tasks.append(task)
            else:
                self._pending_executions.pop(task_id, None)
                self._tasks.pop(task_id, None)
        user_tasks.sort(key=lambda task: task.created_at, reverse=True)
        return user_tasks[:limit]

    async def get_active_for_chat(self, chat_id: str) -> Optional[BackgroundTask]:
        for task_id, task in tuple(self._tasks.items()):
            if task.chat_id != chat_id:
                continue
            if not await self._refresh(task):
                self._pending_executions.pop(task_id, None)
                self._tasks.pop(task_id, None)
                continue
            if (
                task._operation is not None
                and task._operation.state
                in {OperationState.QUEUED, OperationState.RUNNING}
            ):
                return task
        return None

    async def prune_missing(self) -> int:
        """Drop cache entries whose durable operation was already purged."""

        removed = 0
        for task_id, task in tuple(self._tasks.items()):
            if not await self._refresh(task):
                self._pending_executions.pop(task_id, None)
                self._tasks.pop(task_id, None)
                removed += 1
        return removed

    def _oldest_retention_backlog_lag_seconds(
        self,
        fence: ExecutionFence,
    ) -> float:
        """Return the age of the oldest row currently eligible for purge."""

        coordinator = self._require_coordinator()
        current_time = coordinator.current_time()
        cutoff_at = current_time - coordinator.operation_retention
        due_times: list[datetime] = []
        with coordinator.fenced_transaction(fence) as transaction:
            operation_due_at = coordinator.oldest_purge_eligible_due_at(
                transaction=transaction,
            )
            if operation_due_at is not None:
                due_times.append(operation_due_at)
            if self._background_tasks is not None:
                retained_at = (
                    self._background_tasks.oldest_overdue_for_administration(
                        transaction,
                        cutoff_at=cutoff_at,
                    )
                )
                if retained_at is not None:
                    due_times.append(
                        retained_at + coordinator.operation_retention
                    )
        if not due_times:
            return 0.0
        return max(0.0, (current_time - min(due_times)).total_seconds())

    def _fenced_compatibility_cleanup(
        self,
        fence: ExecutionFence,
        *,
        limit: int,
    ) -> int:
        """Purge retained FK-null rows in the exact maintenance transaction."""

        coordinator = self._require_coordinator()
        repository = self._background_tasks
        if repository is None:
            return 0
        cutoff_at = coordinator.current_time() - coordinator.operation_retention
        with coordinator.fenced_transaction(fence) as transaction:
            purged = repository.purge_overdue_for_administration(
                transaction,
                cutoff_at=cutoff_at,
                limit=limit,
            )
            if not isinstance(purged, tuple):
                raise TypeError(
                    "background retention repository returned an invalid result"
                )
            if len(purged) > limit:
                raise RuntimeError(
                    "background retention repository exceeded the requested limit"
                )
            if len(set(purged)) != len(purged):
                raise RuntimeError(
                    "background retention repository returned duplicate identities"
                )
            if any(not isinstance(task_id, str) or not task_id for task_id in purged):
                raise RuntimeError(
                    "background retention repository returned an invalid identity"
                )
            if not purged:
                return 0
            return len(purged)

    async def run_retention_sweep_once(
        self,
        *,
        limit: int = 100,
        max_batches: int = 10,
        compatibility_limit: int = 1000,
    ) -> RetentionSweepResult | None:
        """Run one bounded, maintenance-admitted retention operation.

        ``None`` means maintenance capacity was unavailable.  The caller may
        retry sooner, while interactive/background capacity and latency remain
        governed by their independent child-class limits.
        """

        if limit <= 0 or max_batches <= 0 or compatibility_limit <= 0:
            raise ValueError("retention sweep bounds must be positive")
        if self._draining:
            self._record_operation_observation(
                "refused",
                operation_kind="operation_retention_sweep",
                result_code="service_draining",
            )
            raise self._service_draining_error()
        coordinator = self._require_coordinator()
        owner = OperationOwner(
            owner_scope=OwnerScope.MAINTENANCE,
            owner_user_id=None,
            connection_scope_id=None,
        )
        request = OperationRequest(
            operation_kind="operation_retention_sweep",
            admission_class=AdmissionClass.MAINTENANCE,
            owner=owner,
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        )
        admitted = await asyncio.to_thread(coordinator.submit, request)
        if not admitted.accepted:
            return None
        claim = await asyncio.to_thread(
            coordinator.claim_operation,
            AdmissionClass.MAINTENANCE,
            admitted.operation_id,
        )
        if claim is None:
            await asyncio.to_thread(
                coordinator.cancel,
                owner=owner,
                operation_id=admitted.operation_id,
                terminal_code="maintenance_capacity_unavailable",
            )
            return None

        operations = 0
        submissions = 0
        batches = 0
        compatibility_rows = 0
        saturated = False
        retention_lag_seconds = 0.0
        try:
            try:
                retention_lag_seconds = await asyncio.to_thread(
                    self._oldest_retention_backlog_lag_seconds,
                    claim.fence,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "retention backlog telemetry failed",
                    exc_info=True,
                )
            for _ in range(max_batches):
                await asyncio.to_thread(
                    coordinator.renew_execution_lease,
                    claim.fence,
                )
                result = await asyncio.to_thread(
                    coordinator.purge_expired,
                    limit=limit,
                    fence=claim.fence,
                )
                operations += result.operations
                submissions += result.submissions
                batches += 1
                saturated = (
                    result.operations >= limit or result.submissions >= limit
                )
                if not saturated:
                    break
            await asyncio.to_thread(
                coordinator.renew_execution_lease,
                claim.fence,
            )
            compatibility_rows = await asyncio.to_thread(
                self._fenced_compatibility_cleanup,
                claim.fence,
                limit=compatibility_limit,
            )
            await asyncio.to_thread(
                coordinator.terminalize,
                claim.fence,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary="Retention sweep completed",
                retry_after_ms=None,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(
                    coordinator.terminalize,
                    claim.fence,
                    state=OperationState.CANCELLED,
                    terminal_code="retention_sweep_cancelled",
                    safe_summary="Retention sweep cancelled",
                    retry_after_ms=None,
                )
            except Exception:
                logger.exception("retention sweep operation could not be terminalized")
            raise
        except Exception:
            try:
                await asyncio.to_thread(
                    coordinator.terminalize,
                    claim.fence,
                    state=OperationState.RETRYABLE,
                    terminal_code="retention_sweep_failed",
                    safe_summary="Retention sweep retryable",
                    retry_after_ms=60_000,
                )
            except Exception:
                logger.exception("retention sweep operation could not be terminalized")
            raise
        await self.prune_missing()
        sweep_result = RetentionSweepResult(
            operations=operations,
            submissions=submissions,
            compatibility_rows=compatibility_rows,
            batches=batches,
            backlog=saturated or compatibility_rows >= compatibility_limit,
        )
        observability = self._observability
        if observability is not None:
            try:
                observability.observe_retention(
                    purged_count=(
                        operations + submissions + compatibility_rows
                    ),
                    lag_seconds=retention_lag_seconds,
                )
            except Exception:
                logger.debug("retention sweep telemetry failed", exc_info=True)
        return sweep_result

    def start_retention_sweep(
        self,
        *,
        interval_seconds: float = 3300,
        retry_seconds: float = 60,
        on_sweep=None,
    ) -> asyncio.Task:
        """Start the immediate and periodic production retention loop."""

        if not 0 < interval_seconds <= 3300:
            raise ValueError("retention interval must be in (0, 3300] seconds")
        if not 0 < retry_seconds <= interval_seconds:
            raise ValueError("retention retry must be in (0, interval] seconds")
        if self._draining:
            self._record_operation_observation(
                "refused",
                operation_kind="operation_retention_sweep",
                result_code="service_draining",
            )
            raise self._service_draining_error()
        if self._retention_task is not None and not self._retention_task.done():
            return self._retention_task
        self._retention_stop = asyncio.Event()

        async def _loop() -> None:
            delay = 0.0
            while self._retention_stop is not None:
                if delay:
                    try:
                        await asyncio.wait_for(
                            self._retention_stop.wait(), timeout=delay
                        )
                        return
                    except TimeoutError:
                        pass
                try:
                    result = await self.run_retention_sweep_once()
                    if result is not None and on_sweep is not None:
                        callback_result = on_sweep()
                        if asyncio.iscoroutine(callback_result):
                            await callback_result
                    delay = (
                        interval_seconds
                        if result is not None
                        and not result.backlog
                        else retry_seconds
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("operation retention sweep failed")
                    delay = retry_seconds

        self._retention_task = asyncio.create_task(
            _loop(), name="operation-retention-sweep"
        )
        return self._retention_task

    async def stop_retention_sweep(self) -> None:
        """Cancel and await the tracked retention task during shutdown."""

        task = self._retention_task
        if task is None:
            return
        if self._retention_stop is not None:
            self._retention_stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._retention_task = None
        self._retention_stop = None

    async def purge_expired(self, *, limit: int = 100) -> PurgeResult:
        """Delegate retention to the coordinator, then prune missing DTOs."""

        coordinator = self._require_coordinator()
        result = await asyncio.to_thread(coordinator.purge_expired, limit=limit)
        await self.prune_missing()
        return result
