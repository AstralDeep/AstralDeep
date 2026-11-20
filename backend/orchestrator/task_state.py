"""Legacy synchronous Task/TaskManager DTOs projected from durable operation state owned
by WorkAdmissionCoordinator, never performing Postgres work on the event loop itself.
Task IDs are operation UUIDs. Orchestrator uses these records for task lifecycle updates.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from orchestrator.work_admission import (
    ExecutionFence,
    OperationNotFoundError,
    OperationOwner,
    OperationRecord,
    OperationState,
    PurgeResult,
    SafeOperationProjection,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)

logger = logging.getLogger("Orchestrator.TaskState")


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_TOOL = "awaiting_tool"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYABLE = "retryable"


_TERMINAL_TASK_STATES = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.RETRYABLE,
    }
)
OperationProjection = OperationRecord | SafeOperationProjection
_FENCE_UNSET = object()


def _validate_execution_fence(
    operation: OperationProjection, fence: ExecutionFence | None
) -> None:
    if fence is None:
        return
    if fence.operation_id != operation.operation_id:
        raise RuntimeError("task fence operation identity changed")
    if (
        isinstance(operation, OperationRecord)
        and operation.state is OperationState.RUNNING
    ):
        if (
            fence.execution_generation != operation.execution_generation
            or fence.execution_lease_token != operation.execution_lease_token
        ):
            raise RuntimeError("task execution fence is stale")


class TaskManagerNotBoundError(RuntimeError):
    pass


def _project_task_state(operation: OperationProjection) -> TaskState:
    if operation.state is OperationState.QUEUED:
        return TaskState.PENDING
    if operation.state is OperationState.RUNNING:
        if operation.phase_code == TaskState.PENDING.value:
            return TaskState.PENDING
        if operation.phase_code == TaskState.AWAITING_TOOL.value:
            return TaskState.AWAITING_TOOL
        return TaskState.RUNNING
    return TaskState(operation.state.value)


@dataclass
class Task:
    task_id: str
    chat_id: str
    user_id: str
    state: TaskState = TaskState.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    turn_count: int = 0
    max_turns: int = 10
    tool_calls_made: List[str] = field(default_factory=list)
    current_tool: Optional[str] = None
    error: Optional[str] = None
    message: str = ""

    _MANAGED_FIELDS = frozenset(
        {"task_id", "chat_id", "user_id", "state", "created_at", "updated_at"}
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            name in self._MANAGED_FIELDS
            and getattr(self, "_operation", None) is not None
        ):
            raise AttributeError(f"managed task field {name!r} is read-only")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        self.state = TaskState(self.state)
        self._manager: TaskManager | None = None
        self._owner: OperationOwner | None = None
        self._operation: OperationProjection | None = None
        self._execution_fence: ExecutionFence | None = None

    def _attach_authority(
        self,
        *,
        manager: TaskManager,
        owner: OperationOwner,
        operation: OperationProjection,
        execution_fence: ExecutionFence | None,
    ) -> None:
        if self._operation is not None or self._manager is not None:
            raise RuntimeError("task authority is already attached")
        if execution_fence is not None:
            _validate_execution_fence(operation, execution_fence)
        self._apply_operation(operation, execution_fence=execution_fence)
        self._manager = manager
        self._owner = owner

    def _canonical_state(self) -> TaskState:
        if self._operation is None:
            return self.state
        return _project_task_state(self._operation)

    def _apply_operation(
        self,
        operation: OperationProjection,
        *,
        execution_fence: ExecutionFence | None | object = _FENCE_UNSET,
    ) -> None:
        if str(operation.operation_id) != self.task_id:
            raise RuntimeError("task operation identity changed")
        if (
            self._operation is not None
            and operation.state_revision < self._operation.state_revision
        ):
            raise RuntimeError("task operation projection moved backwards")
        if (
            self._operation is not None
            and self._canonical_state() in _TERMINAL_TASK_STATES
            and _project_task_state(operation) is not self._canonical_state()
        ):
            raise RuntimeError("task terminal projection cannot be overwritten")
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
            self._execution_fence = None
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "state", _project_task_state(operation))
        object.__setattr__(self, "created_at", operation.accepted_at.timestamp())
        object.__setattr__(self, "updated_at", operation.updated_at.timestamp())
        if execution_fence is not _FENCE_UNSET:
            self._execution_fence = execution_fence

    def transition(self, new_state: TaskState, **kwargs) -> None:
        if self._manager is None and self._operation is None:
            old_state = self.state
            self.state = TaskState(new_state)
            self.updated_at = time.time()
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)
            logger.debug(
                "Task %s: %s → %s",
                self.task_id,
                old_state.value,
                self.state.value,
            )
            return
        if self._manager is None:
            raise TaskManagerNotBoundError("managed task has no task manager")
        operation = kwargs.pop("operation", None)
        if operation is None:
            raise TaskManagerNotBoundError(
                "transition requires the operation returned by the coordinator"
            )
        self._manager._apply_transition(self, TaskState(new_state), operation, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        state = self._canonical_state()
        created_at = (
            self._operation.accepted_at.timestamp()
            if self._operation is not None
            else self.created_at
        )
        updated_at = (
            self._operation.updated_at.timestamp()
            if self._operation is not None
            else self.updated_at
        )
        return {
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "state": state.value,
            "created_at": created_at,
            "updated_at": updated_at,
            "turn_count": self.turn_count,
            "max_turns": self.max_turns,
            "tool_calls_made": self.tool_calls_made,
            "current_tool": self.current_tool,
            "error": self.error,
            "message": self.message,
            "elapsed_seconds": round(time.time() - created_at, 1),
        }


class TaskManager:
    def __init__(self, coordinator: WorkAdmissionCoordinator | None = None) -> None:
        self._coordinator = coordinator
        self._tasks: Dict[str, Task] = {}
        self._chat_tasks: Dict[str, str] = {}
        self._chat_locks: Dict[str, asyncio.Lock] = {}

    def bind(self, *, coordinator: WorkAdmissionCoordinator) -> None:
        if self._coordinator is not None and coordinator is not self._coordinator:
            raise RuntimeError("cannot replace the bound coordinator")
        self._coordinator = coordinator

    def _require_coordinator(self) -> WorkAdmissionCoordinator:
        if self._coordinator is None:
            raise TaskManagerNotBoundError(
                "bind an explicit WorkAdmissionCoordinator before querying retention"
            )
        return self._coordinator

    async def assert_current_execution(self, task: Task) -> Task:
        coordinator = self._require_coordinator()
        if task._execution_fence is None:
            raise TaskManagerNotBoundError("task has no current execution fence")
        try:
            operation = await asyncio.to_thread(
                coordinator.assert_current_execution,
                task._execution_fence,
            )
        except StaleExecutionFenceError:
            task._execution_fence = None
            await self.refresh_task(task.task_id)
            raise
        task._apply_operation(operation)
        return task

    def create_task(
        self,
        chat_id: str,
        user_id: str,
        message: str = "",
        *,
        operation: OperationProjection | None = None,
        owner: OperationOwner | None = None,
        execution_fence: ExecutionFence | None = None,
    ) -> Task:
        if operation is None or owner is None:
            raise TaskManagerNotBoundError(
                "create_task requires an admitted operation and authenticated owner"
            )
        if operation.chat_id != chat_id:
            raise ValueError("task chat_id must match the operation projection")
        existing = self.get_active_task(chat_id)
        if existing is not None and existing.task_id != str(operation.operation_id):
            raise TaskManagerNotBoundError(
                "terminalize the existing operation before projecting a replacement"
            )
        task_id = str(operation.operation_id)
        task = Task(
            task_id=task_id,
            chat_id=chat_id,
            user_id=user_id,
            message=message,
        )
        task._attach_authority(
            manager=self,
            owner=owner,
            operation=operation,
            execution_fence=execution_fence,
        )
        self._tasks[task_id] = task
        if task._canonical_state() not in _TERMINAL_TASK_STATES:
            self._chat_tasks[chat_id] = task_id
        logger.info("Projected task %s for chat %s", task_id, chat_id)
        return task

    def _apply_transition(
        self,
        task: Task,
        requested_state: TaskState,
        operation: OperationProjection,
        **metadata,
    ) -> None:
        projected_state = _project_task_state(operation)
        if projected_state is not requested_state:
            raise ValueError(
                "requested task state does not match coordinator operation projection"
            )
        task._apply_operation(operation)
        for key, value in metadata.items():
            if key in {
                "turn_count",
                "max_turns",
                "tool_calls_made",
                "current_tool",
                "error",
                "message",
            }:
                setattr(task, key, value)
        if task._canonical_state() in _TERMINAL_TASK_STATES:
            self._chat_tasks.pop(task.chat_id, None)
        else:
            self._chat_tasks[task.chat_id] = task.task_id
        logger.debug(
            "Task %s projected as %s",
            task.task_id,
            task._canonical_state().value,
        )

    def apply_operation(
        self,
        operation: OperationProjection,
        *,
        execution_fence: ExecutionFence | None | object = _FENCE_UNSET,
    ) -> Task:
        task = self._tasks.get(str(operation.operation_id))
        if task is None:
            raise OperationNotFoundError("task projection not found")
        if execution_fence is _FENCE_UNSET:
            task._apply_operation(operation)
        else:
            task._apply_operation(operation, execution_fence=execution_fence)
        if task._canonical_state() in _TERMINAL_TASK_STATES:
            self._chat_tasks.pop(task.chat_id, None)
        else:
            self._chat_tasks[task.chat_id] = task.task_id
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def get_active_task(self, chat_id: str) -> Optional[Task]:
        task_id = self._chat_tasks.get(chat_id)
        if task_id is None:
            return None
        task = self._tasks.get(task_id)
        if (
            task is not None
            and task._operation is not None
            and task._canonical_state() not in _TERMINAL_TASK_STATES
        ):
            return task
        self._chat_tasks.pop(chat_id, None)
        return None

    def get_chat_tasks(self, chat_id: str) -> List[Task]:
        return [task for task in self._tasks.values() if task.chat_id == chat_id]

    async def refresh_task(self, task_id: str) -> Optional[Task]:
        coordinator = self._require_coordinator()
        task = self._tasks.get(task_id)
        if task is None or task._owner is None:
            return None
        try:
            operation = await asyncio.to_thread(
                coordinator.query_operation,
                owner=task._owner,
                operation_id=uuid.UUID(task.task_id),
            )
        except OperationNotFoundError:
            self._tasks.pop(task_id, None)
            self._chat_tasks.pop(task.chat_id, None)
            return None
        return self.apply_operation(operation)

    async def prune_missing(self) -> int:
        removed = 0
        for task_id in tuple(self._tasks):
            if await self.refresh_task(task_id) is None:
                removed += 1
        return removed

    async def purge_expired(self, *, limit: int = 100) -> PurgeResult:
        coordinator = self._require_coordinator()
        result = await asyncio.to_thread(coordinator.purge_expired, limit=limit)
        await self.prune_missing()
        return result

    def cleanup_old_tasks(self, max_age_seconds: float = 3600):
        if max_age_seconds != 3600:
            logger.debug(
                "Ignoring legacy max_age_seconds=%s; operation retention is authoritative",
                max_age_seconds,
            )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.purge_expired())
        raise TaskManagerNotBoundError(
            "await purge_expired() from async code; cleanup cannot block the event loop"
        )
