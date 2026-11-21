"""Tests for orchestrator/task_state.py's coordinator-backed Re-Act task: the legacy
Task dataclass stays a coordinator projection, operation states are validated against
work_admission.py, and sync surfaces make zero coordinator calls.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.task_state import (
    Task,
    TaskManager,
    TaskManagerNotBoundError,
    TaskState,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationNotFoundError,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)


@dataclass
class _Clock:
    now: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _coordinator(
    clock: _Clock,
    *,
    active_limit: int = 4,
    queue_limit: int = 4,
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=queue_limit,
                max_wait_ms=5_000 if queue_limit else None,
                config_revision="test-060",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )


def _admit(coordinator, *, chat_id="chat-1", user_id="user-1"):
    owner = OperationOwner(OwnerScope.USER, user_id, None)
    admitted = coordinator.submit(
        OperationRequest(
            operation_kind="react_task",
            admission_class=AdmissionClass.INTERACTIVE,
            owner=owner,
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=chat_id,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        )
    )
    assert admitted.accepted
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    operation = coordinator.query_operation(
        owner=owner, operation_id=admitted.operation_id
    )
    return owner, operation, claim


def test_synthetic_task_keeps_legacy_dto_and_transition_behavior() -> None:
    task = Task("synthetic", "chat", "user", message="hello")

    assert task.to_dict()["state"] == "pending"
    assert task.to_dict()["message"] == "hello"
    task.transition(TaskState.RUNNING, turn_count=1)
    assert task.state is TaskState.RUNNING
    assert task.turn_count == 1


def test_task_remains_a_legacy_dataclass_and_replace_detaches_authority() -> None:
    standalone = Task("synthetic", "chat", "user", message="hello")
    assert dataclasses.is_dataclass(standalone)
    assert dataclasses.asdict(standalone) == {
        "task_id": "synthetic",
        "chat_id": "chat",
        "user_id": "user",
        "state": TaskState.PENDING,
        "created_at": standalone.created_at,
        "updated_at": standalone.updated_at,
        "turn_count": 0,
        "max_turns": 10,
        "tool_calls_made": [],
        "current_tool": None,
        "error": None,
        "message": "hello",
    }
    twin = dataclasses.replace(standalone)
    assert twin == standalone
    assert "Task(task_id='synthetic'" in repr(standalone)

    coordinator = _coordinator(_Clock())
    owner, operation, claim = _admit(coordinator)
    managed = TaskManager(coordinator).create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )
    detached = dataclasses.replace(managed)
    assert detached._operation is None
    detached.transition(TaskState.CANCELLED)
    assert detached.state is TaskState.CANCELLED
    for field_name, value in (
        ("task_id", "forged"),
        ("chat_id", "forged"),
        ("user_id", "forged"),
        ("state", TaskState.COMPLETED),
        ("created_at", 0.0),
        ("updated_at", 0.0),
    ):
        with pytest.raises(AttributeError, match="read-only"):
            setattr(managed, field_name, value)
    assert managed.to_dict()["state"] == "running"


def test_create_requires_an_authoritative_operation_and_uses_full_uuid() -> None:
    clock = _Clock()
    coordinator = _coordinator(clock)
    manager = TaskManager(coordinator)
    owner, operation, claim = _admit(coordinator)

    with pytest.raises(TaskManagerNotBoundError, match="admitted operation"):
        manager.create_task("chat-1", "user-1")

    task = manager.create_task(
        "chat-1",
        "user-1",
        "hello",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )

    assert task.task_id == str(operation.operation_id)
    assert len(task.task_id) == 36
    assert task.state is TaskState.RUNNING
    assert manager.get_task(task.task_id) is task
    assert manager.get_active_task("chat-1") is task
    assert manager.get_chat_tasks("chat-1") == [task]
    assert task.created_at == operation.accepted_at.timestamp()
    assert task.updated_at == operation.updated_at.timestamp()


def test_running_phase_and_terminal_state_are_coordinator_projections() -> None:
    clock = _Clock()
    coordinator = _coordinator(clock)
    manager = TaskManager(coordinator)
    owner, operation, claim = _admit(coordinator)
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )

    with pytest.raises(TaskManagerNotBoundError, match="operation returned"):
        task.transition(TaskState.AWAITING_TOOL)

    awaiting = coordinator.update_phase(claim.fence, "awaiting_tool")
    task.transition(
        TaskState.AWAITING_TOOL,
        operation=awaiting,
        current_tool="search",
        turn_count=1,
    )
    assert task.state is TaskState.AWAITING_TOOL
    assert task.current_tool == "search"
    assert task.turn_count == 1

    running = coordinator.update_phase(claim.fence, "running")
    task.transition(TaskState.RUNNING, operation=running, current_tool=None)
    assert task.state is TaskState.RUNNING

    completed = coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    task.transition(TaskState.COMPLETED, operation=completed, turn_count=2)
    assert task.state is TaskState.COMPLETED
    assert manager.get_active_task("chat-1") is None
    assert manager.get_chat_tasks("chat-1") == [task]

    duplicate = coordinator.terminalize(
        claim.fence,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary="Task failed",
        retry_after_ms=None,
    )
    assert duplicate.state is OperationState.COMPLETED
    manager.apply_operation(duplicate)
    assert task.state is TaskState.COMPLETED


def test_retryable_projection_and_projection_validation() -> None:
    clock = _Clock()
    coordinator = _coordinator(clock)
    manager = TaskManager(coordinator)
    owner, operation, claim = _admit(coordinator)
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )
    retryable = coordinator.terminalize(
        claim.fence,
        state=OperationState.RETRYABLE,
        terminal_code="operation_failed",
        safe_summary="Retryable",
        retry_after_ms=100,
    )

    with pytest.raises(ValueError, match="does not match"):
        task.transition(TaskState.FAILED, operation=retryable)
    task.transition(TaskState.RETRYABLE, operation=retryable, error="try again")
    assert task.state is TaskState.RETRYABLE
    assert task.error == "try again"

    stale = dataclasses.replace(retryable, state_revision=0)
    with pytest.raises(RuntimeError, match="backwards"):
        manager.apply_operation(stale)


def test_pending_mappings_and_terminal_overwrite_are_strict() -> None:
    clock = _Clock()
    coordinator = _coordinator(clock)
    manager = TaskManager(coordinator)
    owner, operation, claim = _admit(coordinator)
    pending = coordinator.update_phase(claim.fence, "pending")
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=pending,
        owner=owner,
        execution_fence=claim.fence,
    )
    assert task.state is TaskState.PENDING

    queued = dataclasses.replace(
        pending,
        state=OperationState.QUEUED,
        phase_code=None,
        state_revision=pending.state_revision + 1,
    )
    task.transition(TaskState.PENDING, operation=queued)
    assert task.state is TaskState.PENDING

    completed = dataclasses.replace(
        queued,
        state=OperationState.COMPLETED,
        terminal_at=clock.now,
        purge_after=clock.now + timedelta(hours=24),
        state_revision=queued.state_revision + 1,
    )
    task.transition(TaskState.COMPLETED, operation=completed)
    overwritten = dataclasses.replace(
        completed,
        state=OperationState.FAILED,
        state_revision=completed.state_revision + 1,
    )
    with pytest.raises(RuntimeError, match="cannot be overwritten"):
        manager.apply_operation(overwritten)


def test_sync_projection_surfaces_make_zero_coordinator_calls() -> None:
    clock = _Clock()
    authority = _coordinator(clock)
    owner, operation, claim = _admit(authority)

    class NoSyncCalls:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected coordinator call: {name}")

    manager = TaskManager(NoSyncCalls())
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )
    assert manager.get_task(task.task_id) is task
    assert manager.get_active_task("chat-1") is task
    assert manager.get_chat_tasks("chat-1") == [task]

    awaiting = authority.update_phase(claim.fence, "awaiting_tool")
    task.transition(TaskState.AWAITING_TOOL, operation=awaiting)
    assert task.state is TaskState.AWAITING_TOOL


def test_projection_validation_binding_and_cache_edges() -> None:
    clock = _Clock()
    coordinator = _coordinator(clock)
    replacement = _coordinator(_Clock())
    owner, operation, claim = _admit(coordinator)
    manager = TaskManager()
    manager.bind(coordinator=coordinator)
    manager.bind(coordinator=coordinator)
    with pytest.raises(RuntimeError, match="cannot replace"):
        manager.bind(coordinator=replacement)

    with pytest.raises(ValueError, match="chat_id"):
        manager.create_task(
            "other-chat",
            "user-1",
            operation=operation,
            owner=owner,
        )
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
    )
    with pytest.raises(RuntimeError, match="cannot replace"):
        manager.bind(coordinator=replacement)
    with pytest.raises(OperationNotFoundError, match="projection"):
        manager.apply_operation(
            dataclasses.replace(operation, operation_id=uuid.uuid4())
        )
    with pytest.raises(RuntimeError, match="identity changed"):
        task._apply_operation(dataclasses.replace(operation, operation_id=uuid.uuid4()))

    manager.apply_operation(operation, execution_fence=claim.fence)
    assert task._execution_fence == claim.fence
    manager._tasks.pop(task.task_id)
    assert manager.get_active_task("chat-1") is None


def test_task_fences_are_validated_against_running_operation() -> None:
    coordinator = _coordinator(_Clock())
    owner, _, claim = _admit(coordinator)
    wrong_identity = dataclasses.replace(claim.fence, operation_id=uuid.uuid4())
    stale_token = dataclasses.replace(claim.fence, execution_lease_token=uuid.uuid4())

    for fence, message in (
        (wrong_identity, "operation identity"),
        (stale_token, "fence is stale"),
    ):
        with pytest.raises(RuntimeError, match=message):
            TaskManager(coordinator).create_task(
                "chat-1",
                "user-1",
                operation=claim.operation,
                owner=owner,
                execution_fence=fence,
            )

    manager = TaskManager(coordinator)
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=claim.operation,
        owner=owner,
        execution_fence=claim.fence,
    )
    replacement = coordinator.reselect_execution(claim.fence)
    replacement_operation = coordinator.assert_current_execution(replacement)
    manager.apply_operation(replacement_operation, execution_fence=replacement)
    assert task._execution_fence == replacement


def test_replacement_requires_prior_coordinator_terminal() -> None:
    clock = _Clock()
    coordinator = _coordinator(clock)
    manager = TaskManager(coordinator)
    owner, operation, claim = _admit(coordinator)
    manager.create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )
    owner2, operation2, claim2 = _admit(coordinator)

    with pytest.raises(TaskManagerNotBoundError, match="existing operation"):
        manager.create_task(
            "chat-1",
            "user-1",
            operation=operation2,
            owner=owner2,
            execution_fence=claim2.fence,
        )


@pytest.mark.asyncio
async def test_refresh_and_purge_delegate_to_coordinator_retention() -> None:
    clock = _Clock()
    coordinator = _coordinator(clock)
    manager = TaskManager(coordinator)
    owner, operation, claim = _admit(coordinator)
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )
    coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )

    refreshed = await manager.refresh_task(task.task_id)
    assert refreshed is task
    assert refreshed.state is TaskState.COMPLETED

    clock.now += timedelta(hours=25)
    result = await manager.purge_expired()
    assert result.operations == 1
    assert manager.get_task(task.task_id) is None


@pytest.mark.asyncio
async def test_refresh_and_cache_prune_preserve_current_running_fence() -> None:
    coordinator = _coordinator(_Clock())
    manager = TaskManager(coordinator)
    owner, operation, claim = _admit(coordinator)
    task = manager.create_task(
        "chat-1",
        "user-1",
        operation=operation,
        owner=owner,
        execution_fence=claim.fence,
    )

    assert await manager.refresh_task(task.task_id) is task
    assert task._execution_fence == claim.fence
    assert await manager.prune_missing() == 0
    assert task._execution_fence == claim.fence


@pytest.mark.asyncio
async def test_sync_cleanup_refuses_to_block_running_event_loop() -> None:
    manager = TaskManager(_coordinator(_Clock()))

    with pytest.raises(TaskManagerNotBoundError, match="await purge_expired"):
        manager.cleanup_old_tasks(max_age_seconds=1)


@pytest.mark.asyncio
async def test_unbound_async_queries_fail_closed_and_unknown_refresh_is_none() -> None:
    with pytest.raises(TaskManagerNotBoundError, match="bind an explicit"):
        await TaskManager().refresh_task(str(uuid.uuid4()))
    assert (
        await TaskManager(_coordinator(_Clock())).refresh_task(str(uuid.uuid4()))
        is None
    )


def test_sync_cleanup_delegates_to_async_coordinator_purge() -> None:
    result = TaskManager(_coordinator(_Clock())).cleanup_old_tasks()
    assert result.operations == 0
    assert result.submissions == 0
