"""Production acceptance coverage for feature-060 task compatibility wiring.

These tests focus on the integration seams that are easy to regress while the
legacy background-task and Re-Act DTOs are projected over one durable operation
authority.  PostgreSQL itself is covered by the repository integration suite;
this file verifies that production construction selects that repository and
that asyncio call sites never create a second authority or block the loop.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from astralplane import create_work_admission_repository
from orchestrator.async_tasks import (
    BackgroundTask,
    BackgroundTaskManager,
    RetentionSweepResult,
    TaskStatus,
)
from orchestrator.orchestrator import Orchestrator
from orchestrator.task_state import TaskManager, TaskState
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationNotFoundError,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    PlaneWorkAdmissionRepository,
    PurgeResult,
    WorkAdmissionCoordinator,
    load_admission_class_configs,
)
from shared.feature_flags import flags


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _call_supplies_shared_authority(call: ast.Call) -> bool:
    if any(_dotted_name(argument) == "self.work_admission" for argument in call.args):
        return True
    return any(
        keyword.arg == "coordinator"
        and _dotted_name(keyword.value) == "self.work_admission"
        for keyword in call.keywords
    )


def test_production_constructor_loads_one_plane_authority_for_both_managers() -> None:
    """The production object graph must not create process-local authorities."""

    tree = ast.parse(textwrap.dedent(inspect.getsource(Orchestrator.__init__)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    coordinator_calls = [
        call
        for call in calls
        if _dotted_name(call.func)
        in {"WorkAdmissionCoordinator", "WorkAdmissionCoordinator.from_plane"}
    ]

    assert len(coordinator_calls) == 1
    coordinator_call = coordinator_calls[0]
    constructor_arguments = {
        keyword.arg: _dotted_name(keyword.value)
        for keyword in coordinator_call.keywords
    }
    assert constructor_arguments["plane_runtime"] == (
        "self.runtime_composition.plane.runtime"
    )
    assert constructor_arguments["plane_repositories"] == (
        "self.runtime_composition.plane.repositories"
    )
    assert "database" not in constructor_arguments

    assigned_names = {
        _dotted_name(target)
        for assignment in ast.walk(tree)
        if isinstance(assignment, (ast.Assign, ast.AnnAssign))
        and assignment.value is coordinator_call
        for target in (
            assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        )
    }
    assert assigned_names == {"self.work_admission"}

    assert _dotted_name(coordinator_call.func) == "WorkAdmissionCoordinator.from_plane"

    manager_bindings = {"background": False, "task": False}
    for call in calls:
        function_name = _dotted_name(call.func)
        if not _call_supplies_shared_authority(call):
            continue
        if function_name.endswith("BackgroundTaskManager") or function_name == (
            "self.async_task_manager.bind"
        ):
            manager_bindings["background"] = True
        if function_name.endswith("TaskManager") or function_name == (
            "self.task_manager.bind"
        ):
            manager_bindings["task"] = True
    assert manager_bindings == {"background": True, "task": True}

    factory_tree = ast.parse(
        textwrap.dedent(inspect.getsource(WorkAdmissionCoordinator.from_plane))
    )
    factory_calls = [
        node for node in ast.walk(factory_tree) if isinstance(node, ast.Call)
    ]
    assert sum(
        _dotted_name(call.func).split(".")[-1] == "PlaneWorkAdmissionRepository"
        for call in factory_calls
    ) == 1
    assert sum(
        _dotted_name(call.func).split(".")[-1] == "load_existing_configs"
        for call in factory_calls
    ) == 1
    constructor_call = next(
        call for call in factory_calls if _dotted_name(call.func) == "cls"
    )
    assert any(
        keyword.arg == "_configure_repository"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in constructor_call.keywords
    ), "loading persisted operator values must not UPSERT them during startup"


def test_production_binds_runtime_observability_and_bounded_shutdown_drain() -> None:
    constructor = ast.parse(
        textwrap.dedent(inspect.getsource(Orchestrator.__init__))
    )
    bind_calls = [
        node
        for node in ast.walk(constructor)
        if isinstance(node, ast.Call)
        and _dotted_name(node.func) == "self.async_task_manager.bind"
    ]
    assert len(bind_calls) == 1
    assert any(
        keyword.arg == "observability"
        and _dotted_name(keyword.value) == "self.runtime_observability"
        for keyword in bind_calls[0].keywords
    )
    assert any(
        keyword.arg == "plane_runtime"
        and _dotted_name(keyword.value)
        == "self.runtime_composition.plane.runtime"
        for keyword in bind_calls[0].keywords
    )
    assert any(
        keyword.arg == "plane_repositories"
        and _dotted_name(keyword.value)
        == "self.runtime_composition.plane.repositories"
        for keyword in bind_calls[0].keywords
    )
    assert all(keyword.arg != "db" for keyword in bind_calls[0].keywords)

    shutdown = ast.parse(
        textwrap.dedent(
            inspect.getsource(Orchestrator._close_started_services_once)
        )
    )
    drain_calls = [
        node
        for node in ast.walk(shutdown)
        if isinstance(node, ast.Call)
        and _dotted_name(node.func) == "self.async_task_manager.drain"
    ]
    assert len(drain_calls) == 1
    timeout = next(
        keyword.value
        for keyword in drain_calls[0].keywords
        if keyword.arg == "timeout_seconds"
    )
    assert isinstance(timeout, ast.Constant)
    assert timeout.value == 5.0


class _ConfigTransaction:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> SimpleNamespace:
        self.calls.append((statement, parameters))
        return SimpleNamespace(
            rowcount=len(self.rows),
            status_message="SELECT",
            returned_records=tuple(self.rows),
        )

    def fetch_one(self, statement: str, parameters=()):
        raise AssertionError("repository must use detached command results")

    def fetch_all(self, statement: str, parameters=()):
        raise AssertionError("repository must use detached command results")

    @contextmanager
    def savepoint(self, name: str):
        del name
        yield self


class _ConfigPlaneRuntime:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.transactions: list[_ConfigTransaction] = []
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def transaction(self):
        transaction = _ConfigTransaction(self.rows)
        self.transactions.append(transaction)
        try:
            yield transaction
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


class _ConfigurationCaptureRepository:
    def __init__(self) -> None:
        self.configured: tuple[AdmissionClassConfig, ...] | None = None

    def configure(self, configs) -> None:
        self.configured = tuple(configs)


def test_all_effective_rows_and_operator_retention_are_loaded_read_only() -> None:
    rows = [
        {
            "class_name": "global",
            "parent_class_name": None,
            "active_limit": 37,
            "queue_limit": 0,
            "max_wait_ms": 0,
            "config_revision": "operator-2026-07",
        },
        {
            "class_name": "interactive",
            "parent_class_name": "global",
            "active_limit": 29,
            "queue_limit": 41,
            "max_wait_ms": 2_500,
            "config_revision": "operator-2026-07",
        },
        {
            "class_name": "voice_interactive",
            "parent_class_name": "interactive",
            "active_limit": 10,
            "queue_limit": 0,
            "max_wait_ms": 0,
            "config_revision": "operator-2026-07",
        },
        {
            "class_name": "mcp",
            "parent_class_name": "global",
            "active_limit": 8,
            "queue_limit": 32,
            "max_wait_ms": 5_000,
            "config_revision": "operator-2026-07",
        },
        {
            "class_name": "background",
            "parent_class_name": "global",
            "active_limit": 7,
            "queue_limit": 19,
            "max_wait_ms": 12_345,
            "config_revision": "operator-2026-07",
        },
        {
            "class_name": "scheduled",
            "parent_class_name": "global",
            "active_limit": 4,
            "queue_limit": 11,
            "max_wait_ms": 22_222,
            "config_revision": "operator-2026-07",
        },
        {
            "class_name": "maintenance",
            "parent_class_name": "global",
            "active_limit": 3,
            "queue_limit": 13,
            "max_wait_ms": 33_333,
            "config_revision": "operator-2026-07",
        },
        {
            "class_name": "system",
            "parent_class_name": "global",
            "active_limit": 6,
            "queue_limit": 17,
            "max_wait_ms": 44_444,
            "config_revision": "operator-2026-07",
        },
    ]
    runtime = _ConfigPlaneRuntime(rows)
    catalog = SimpleNamespace(work_admission=create_work_admission_repository())

    configs = load_admission_class_configs(
        plane_runtime=runtime,
        plane_repositories=catalog,
    )

    assert {config.class_name for config in configs} == set(AdmissionClass)
    effective = {config.class_name: config for config in configs}
    assert effective[AdmissionClass.GLOBAL].max_wait_ms is None
    assert effective[AdmissionClass.BACKGROUND].active_limit == 7
    assert effective[AdmissionClass.BACKGROUND].queue_limit == 19
    assert effective[AdmissionClass.BACKGROUND].max_wait_ms == 12_345
    assert effective[AdmissionClass.VOICE_INTERACTIVE].parent_class_name is (
        AdmissionClass.INTERACTIVE
    )
    assert effective[AdmissionClass.VOICE_INTERACTIVE].queue_limit == 0
    assert effective[AdmissionClass.VOICE_INTERACTIVE].max_wait_ms is None
    assert {config.config_revision for config in configs} == {"operator-2026-07"}
    assert len(runtime.transactions) == 1
    assert runtime.commits == 1
    assert runtime.rollbacks == 0
    assert len(runtime.transactions[0].calls) == 1
    statement, parameters = runtime.transactions[0].calls[0]
    assert parameters == ()
    assert "operation_admission_class" in statement
    assert "FOR SHARE" in statement
    assert not {"INSERT", "UPDATE", "DELETE", "UPSERT"} & set(
        statement.upper().split()
    )

    capture = _ConfigurationCaptureRepository()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=configs,
        repository=capture,
        operation_retention=timedelta(hours=31),
    )
    assert capture.configured == configs
    assert coordinator.operation_retention == timedelta(hours=31)


def test_from_plane_binds_operator_snapshot_without_upsert() -> None:
    rows = [
        {
            "class_name": member.value,
            "parent_class_name": (
                None
                if member is AdmissionClass.GLOBAL
                else (
                    "interactive"
                    if member is AdmissionClass.VOICE_INTERACTIVE
                    else "global"
                )
            ),
            "active_limit": 47 if member is AdmissionClass.GLOBAL else 8,
            "queue_limit": (
                0
                if member
                in {AdmissionClass.GLOBAL, AdmissionClass.VOICE_INTERACTIVE}
                else 23
            ),
            "max_wait_ms": (
                0
                if member
                in {AdmissionClass.GLOBAL, AdmissionClass.VOICE_INTERACTIVE}
                else 9_876
            ),
            "config_revision": "operator-live",
        }
        for member in AdmissionClass
    ]
    runtime = _ConfigPlaneRuntime(rows)
    catalog = SimpleNamespace(work_admission=create_work_admission_repository())

    coordinator = WorkAdmissionCoordinator.from_plane(
        plane_runtime=runtime,
        plane_repositories=catalog,
        operation_retention=timedelta(hours=27),
    )

    assert isinstance(coordinator._repository, PlaneWorkAdmissionRepository)
    assert coordinator.operation_retention == timedelta(hours=27)
    assert coordinator._repository._configs[AdmissionClass.GLOBAL].active_limit == 47
    assert coordinator._repository._configs[AdmissionClass.BACKGROUND].queue_limit == 23
    assert (
        coordinator._repository._configs[
            AdmissionClass.VOICE_INTERACTIVE
        ].parent_class_name
        is AdmissionClass.INTERACTIVE
    )
    assert (
        coordinator._repository._configs[AdmissionClass.VOICE_INTERACTIVE].queue_limit
        == 0
    )
    assert len(runtime.transactions) == 1
    assert runtime.commits == 1
    assert runtime.rollbacks == 0
    assert len(runtime.transactions[0].calls) == 1
    statement, parameters = runtime.transactions[0].calls[0]
    assert parameters == ()
    normalized_query = " ".join(statement.split()).upper()
    assert normalized_query.startswith("SELECT ")
    assert " FOR SHARE" in normalized_query
    assert not {"INSERT", "UPDATE", "DELETE", "UPSERT"} & set(
        normalized_query.split()
    )


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class _ThreadRecordingAuthority:
    def __init__(self, delegate: WorkAdmissionCoordinator) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, int, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str):
        attribute = getattr(self.delegate, name)
        if not callable(attribute):
            return attribute

        def recorded(*args, **kwargs):
            self.calls.append((name, threading.get_ident(), args, kwargs))
            return attribute(*args, **kwargs)

        return recorded


def _background_authority() -> _ThreadRecordingAuthority:
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=None,
                active_limit=5,
                queue_limit=10,
                max_wait_ms=30_000,
                config_revision="test-t013",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=_Clock(),
    )
    return _ThreadRecordingAuthority(coordinator)


def _maintenance_coordinator(
    clock: _Clock,
    *,
    retention: timedelta = timedelta(hours=1),
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=20,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="test-t013",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=5,
                queue_limit=10,
                max_wait_ms=30_000,
                config_revision="test-t013",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.MAINTENANCE,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=2,
                queue_limit=10,
                max_wait_ms=30_000,
                config_revision="test-t013",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        operation_retention=retention,
    )


def _seed_expiring_background_operation(
    coordinator: WorkAdmissionCoordinator,
) -> tuple[OperationOwner, uuid.UUID]:
    owner = OperationOwner(OwnerScope.USER, "retention-owner", None)
    request = OperationRequest(
        operation_kind="retention_fixture",
        admission_class=AdmissionClass.BACKGROUND,
        owner=owner,
        submission_id=uuid.uuid4(),
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
        chat_id="retention-chat",
        parent_operation_id=None,
        connection_generation=None,
        request_generation=None,
    )
    admitted = coordinator.submit(request)
    assert admitted.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.BACKGROUND, admitted.operation_id
    )
    assert claim is not None
    coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Fixture completed",
        retry_after_ms=None,
    )
    return owner, admitted.operation_id


@pytest.mark.asyncio
async def test_background_react_projection_reuses_operation_fence_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(flags._flags, "task_state_machine", True)
    event_loop_thread = threading.get_ident()
    authority = _background_authority()
    background = BackgroundTaskManager(coordinator=authority)
    react = TaskManager(coordinator=authority)
    observed: dict[str, object] = {}

    async def managed_turn(virtual_socket) -> None:
        background_task = virtual_socket.task
        projected = await react.admit_task(
            background_task.chat_id,
            background_task.user_id,
            "managed turn",
            operation=background_task._operation,
            owner=background_task._owner,
            execution_fence=background_task._execution_fence,
        )
        observed["task"] = projected
        observed["operation"] = background_task._operation
        observed["fence"] = background_task._execution_fence
        assert projected.task_id == background_task.task_id
        assert projected._operation is background_task._operation
        assert projected._execution_fence is background_task._execution_fence
        await react.transition_task(projected, TaskState.AWAITING_TOOL)
        await react.transition_task(projected, TaskState.RUNNING)

    background_task = await background.submit(
        "chat-t013",
        "user-t013",
        managed_turn,
        kind="async_chat",
    )
    assert background_task.asyncio_task is not None
    await background_task.asyncio_task

    call_names = [name for name, *_ in authority.calls]
    assert call_names.count("submit") == 1
    assert call_names.count("claim_operation") == 1
    submit_call = next(call for call in authority.calls if call[0] == "submit")
    request = submit_call[2][0]
    assert request.admission_class is AdmissionClass.BACKGROUND
    assert observed["operation"].operation_id == observed["fence"].operation_id
    assert background_task.status is TaskStatus.COMPLETED
    assert all(thread_id != event_loop_thread for _, thread_id, _, _ in authority.calls)


def test_handle_chat_message_reuses_managed_socket_authority_at_real_callsite() -> None:
    """The simulated manager seam above must also be wired into production."""

    wrapper_source = textwrap.dedent(
        inspect.getsource(Orchestrator.handle_chat_message)
    )
    wrapper_tree = ast.parse(wrapper_source)
    wrapper = next(
        node for node in wrapper_tree.body if isinstance(node, ast.AsyncFunctionDef)
    )
    assert "operation_context" in {
        argument.arg for argument in wrapper.args.args
    }
    delegates = [
        call
        for call in ast.walk(wrapper)
        if isinstance(call, ast.Call)
        and _dotted_name(call.func) == "self._handle_chat_message_impl"
    ]
    assert len(delegates) == 1
    forwarded = {
        keyword.arg: _dotted_name(keyword.value)
        for keyword in delegates[0].keywords
    }
    assert forwarded["operation_context"] == "operation_context"

    # Conversation publication now wraps the Re-Act implementation. Inspect
    # the delegated production callsite as well as proving the wrapper carries
    # the exact managed authority through that boundary.
    source = textwrap.dedent(
        inspect.getsource(Orchestrator._handle_chat_message_impl)
    )
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef))
    assert "operation_context" in {argument.arg for argument in function.args.args}

    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    assert not any(
        _dotted_name(call.func) == "self.task_manager.create_task" for call in calls
    )
    admissions = [
        call
        for call in calls
        if _dotted_name(call.func) == "self.task_manager.admit_task"
    ]
    assert len(admissions) == 1
    keywords = {
        keyword.arg: _dotted_name(keyword.value) for keyword in admissions[0].keywords
    }
    assert keywords["operation"] == "authority_operation"
    assert keywords["owner"] == "authority_owner"
    assert keywords["execution_fence"] == "authority_fence"
    assert "getattr(websocket, \"task\", None)" in source

    transition_calls = [
        call
        for call in calls
        if _dotted_name(call.func) == "self.task_manager.transition_task"
    ]
    awaited_call_ids = {
        id(node.value)
        for node in ast.walk(function)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }
    assert transition_calls
    assert all(id(call) in awaited_call_ids for call in transition_calls)


@pytest.mark.asyncio
async def test_retryable_background_rows_replay_as_terminal_completion() -> None:
    completed_at = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)

    class EmptyTaskManager:
        async def list_for_user(self, user_id):
            assert user_id == "user-t013"
            return []

    replay_requests: list[str] = []
    notified: list[tuple[str, tuple[str, ...]]] = []
    sent: list[dict[str, object]] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.async_task_manager = EmptyTaskManager()

    def background_tasks_for_replay(user_id: str):
        replay_requests.append(user_id)
        return (
            SimpleNamespace(
                task_id="retryable-task",
                conversation_id="chat-t013",
                status=TaskStatus.RETRYABLE,
                summary="Try again",
                completed_at=completed_at,
            ),
        )

    def mark_background_tasks_notified(
        user_id: str,
        task_ids: tuple[str, ...],
    ) -> None:
        notified.append((user_id, task_ids))

    orchestrator._background_tasks_for_replay = background_tasks_for_replay
    orchestrator._mark_background_tasks_notified = mark_background_tasks_notified

    async def safe_send(_websocket, payload):
        sent.append(json.loads(payload))
        return True

    orchestrator._safe_send = safe_send
    await orchestrator._replay_user_tasks(object(), "user-t013")

    assert replay_requests == ["user-t013"]
    assert sent == [
        {
            "type": "task_completed",
            "payload": {
                "task_id": "retryable-task",
                "chat_id": "chat-t013",
                "status": "retryable",
                "completed_at": completed_at.isoformat(),
                "summary": "Try again",
                "replay": True,
            },
        }
    ]
    assert notified == [("user-t013", ("retryable-task",))]


@pytest.mark.asyncio
async def test_failed_replay_delivery_remains_unnotified_then_marks_once() -> None:
    completed_at = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)

    class EmptyTaskManager:
        async def list_for_user(self, _user_id):
            return []

    notification_state = {"notified": False, "update_count": 0}
    delivery_outcomes = iter((False, True))
    attempts: list[dict[str, object]] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.async_task_manager = EmptyTaskManager()

    def background_tasks_for_replay(user_id: str):
        assert user_id == "user-t013"
        if notification_state["notified"]:
            return ()
        return (
            SimpleNamespace(
                task_id="retryable-task",
                conversation_id="chat-t013",
                status=TaskStatus.RETRYABLE,
                summary="Try again",
                completed_at=completed_at,
            ),
        )

    def mark_background_tasks_notified(
        user_id: str,
        task_ids: tuple[str, ...],
    ) -> None:
        assert user_id == "user-t013"
        assert task_ids == ("retryable-task",)
        notification_state["update_count"] += 1
        notification_state["notified"] = True

    orchestrator._background_tasks_for_replay = background_tasks_for_replay
    orchestrator._mark_background_tasks_notified = mark_background_tasks_notified

    async def safe_send(_websocket, payload):
        attempts.append(json.loads(payload))
        return next(delivery_outcomes)

    orchestrator._safe_send = safe_send
    websocket = object()

    await orchestrator._replay_user_tasks(websocket, "user-t013")
    assert notification_state == {"notified": False, "update_count": 0}
    await orchestrator._replay_user_tasks(websocket, "user-t013")
    assert notification_state == {"notified": True, "update_count": 1}
    await orchestrator._replay_user_tasks(websocket, "user-t013")
    assert notification_state["update_count"] == 1
    assert [attempt["payload"]["status"] for attempt in attempts] == [
        "retryable",
        "retryable",
    ]


@pytest.mark.asyncio
async def test_watch_task_immediately_delivers_retryable_terminal() -> None:
    websocket = object()
    watched = BackgroundTask(
        task_id="retryable-task",
        chat_id="chat-t013",
        user_id="user-t013",
        status=TaskStatus.RETRYABLE,
    )

    class WatchManager:
        async def get(self, task_id):
            assert task_id == watched.task_id
            return watched

    sent: list[dict[str, object]] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.ui_sessions = {websocket: {"sub": "user-t013"}}
    orchestrator.async_task_manager = WatchManager()
    orchestrator._chat_recorders = {}

    async def safe_send(_websocket, payload):
        sent.append(json.loads(payload))
        return True

    orchestrator._safe_send = safe_send
    await orchestrator.handle_ui_message(
        websocket,
        json.dumps(
            {
                "type": "ui_event",
                "action": "watch_task",
                "payload": {"task_id": watched.task_id},
            }
        ),
    )
    await asyncio.sleep(0)

    assert watched.watchers == []
    assert sent == [
        {
            "type": "task_completed",
            "payload": {
                "task_id": watched.task_id,
                "chat_id": watched.chat_id,
                "status": "retryable",
            },
        }
    ]


@pytest.mark.asyncio
async def test_maintenance_sweep_is_admitted_fenced_bounded_and_off_loop() -> None:
    clock = _Clock()
    coordinator = _maintenance_coordinator(clock)
    expired_owner, expired_operation_id = _seed_expiring_background_operation(
        coordinator
    )
    clock.advance(timedelta(hours=1, seconds=1))
    authority = _ThreadRecordingAuthority(coordinator)
    manager = BackgroundTaskManager(authority)
    event_loop_thread = threading.get_ident()

    result = await manager.run_retention_sweep_once(limit=1, max_batches=3)

    assert result == RetentionSweepResult(
        operations=1,
        submissions=1,
        compatibility_rows=0,
        batches=2,
    )
    with pytest.raises(OperationNotFoundError):
        coordinator.query_operation(
            owner=expired_owner,
            operation_id=expired_operation_id,
        )

    submissions = [call for call in authority.calls if call[0] == "submit"]
    assert len(submissions) == 1
    maintenance_request = submissions[0][2][0]
    assert maintenance_request.admission_class is AdmissionClass.MAINTENANCE
    assert maintenance_request.owner.owner_scope is OwnerScope.MAINTENANCE
    claims = [call for call in authority.calls if call[0] == "claim_operation"]
    assert len(claims) == 1
    assert claims[0][2][0] is AdmissionClass.MAINTENANCE

    purge_calls = [call for call in authority.calls if call[0] == "purge_expired"]
    assert len(purge_calls) == 2
    assert all(call[3]["limit"] == 1 for call in purge_calls)
    purge_fences = [call[3]["fence"] for call in purge_calls]
    assert all(fence.operation_id == claims[0][2][1] for fence in purge_fences)
    assert all(
        thread_id != event_loop_thread for _, thread_id, _, _ in authority.calls
    )

    maintenance_projection = coordinator.query_operation(
        owner=maintenance_request.owner,
        operation_id=claims[0][2][1],
    )
    assert maintenance_projection.state is OperationState.COMPLETED


class _CleanupBackgroundRepository:
    def __init__(self, deleted_rows: int) -> None:
        self.deleted_rows = deleted_rows
        self.projection_records = []
        self.oldest_calls: list[tuple[object, datetime]] = []
        self.purge_calls: list[tuple[object, datetime, int]] = []

    def apply_operation_projection(self, transaction, record):
        self.projection_records.append((transaction, record))
        return record

    def oldest_overdue_for_administration(self, transaction, *, cutoff_at):
        self.oldest_calls.append((transaction, cutoff_at))
        return None

    def purge_overdue_for_administration(
        self,
        transaction,
        *,
        cutoff_at,
        limit,
    ):
        self.purge_calls.append((transaction, cutoff_at, limit))
        return tuple(
            f"expired-{index}"
            for index in range(min(self.deleted_rows, limit))
        )


class _FakePlaneRuntime:
    @contextmanager
    def transaction(self, *, isolation=None):
        del isolation
        yield object()


class _FencedCursorAuthority(_ThreadRecordingAuthority):
    def __init__(
        self, delegate: WorkAdmissionCoordinator
    ) -> None:
        super().__init__(delegate)
        self.transactions: list[object] = []

    @contextmanager
    def fenced_transaction(self, fence):
        self.calls.append(
            ("fenced_transaction", threading.get_ident(), (fence,), {})
        )
        with self.delegate.fenced_transaction(fence):
            transaction = object()
            self.transactions.append(transaction)
            yield transaction


class _SaturatedPurgeAuthority(_ThreadRecordingAuthority):
    def purge_expired(self, *, limit: int, fence):
        self.calls.append(
            (
                "purge_expired",
                threading.get_ident(),
                (),
                {"limit": limit, "fence": fence},
            )
        )
        self.delegate.assert_current_execution(fence)
        return PurgeResult(operations=limit, submissions=0)


@pytest.mark.asyncio
async def test_restart_sweep_bulk_cleans_fk_null_rows_under_same_fence() -> None:
    clock = _Clock()
    coordinator = _maintenance_coordinator(
        clock, retention=timedelta(hours=24)
    )
    repository = _CleanupBackgroundRepository(deleted_rows=2)
    authority = _FencedCursorAuthority(coordinator)
    manager = BackgroundTaskManager(authority)
    manager.bind(
        plane_runtime=_FakePlaneRuntime(),
        background_repository=repository,
    )
    assert manager._tasks == {}, "restart cleanup must not depend on process cache"

    result = await manager.run_retention_sweep_once(
        limit=7,
        max_batches=2,
        compatibility_limit=3,
    )

    assert result == RetentionSweepResult(
        operations=0,
        submissions=0,
        compatibility_rows=2,
        batches=1,
    )
    purge_call = next(call for call in authority.calls if call[0] == "purge_expired")
    transaction_call = next(
        call for call in authority.calls if call[0] == "fenced_transaction"
    )
    assert purge_call[3]["fence"] == transaction_call[2][0]
    assert len(repository.oldest_calls) == 1
    assert len(repository.purge_calls) == 1
    purge_transaction, cutoff_at, purge_limit = repository.purge_calls[0]
    assert purge_transaction is authority.transactions[-1]
    assert cutoff_at == clock.current - timedelta(hours=24)
    assert purge_limit == 3


@pytest.mark.asyncio
async def test_custom_batch_ceiling_reports_backlog_for_prompt_retry() -> None:
    clock = _Clock()
    coordinator = _maintenance_coordinator(clock)
    authority = _SaturatedPurgeAuthority(coordinator)
    manager = BackgroundTaskManager(authority)

    result = await manager.run_retention_sweep_once(
        limit=3,
        max_batches=2,
        compatibility_limit=5,
    )

    assert result == RetentionSweepResult(
        operations=6,
        submissions=0,
        compatibility_rows=0,
        batches=2,
        backlog=True,
    )
    assert len(
        [call for call in authority.calls if call[0] == "purge_expired"]
    ) == 2


@pytest.mark.asyncio
async def test_retention_loop_runs_immediately_periodically_and_is_awaited_on_stop() -> None:
    manager = BackgroundTaskManager(coordinator=object())
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()
    call_times: list[float] = []
    loop = asyncio.get_running_loop()

    async def sweep_once():
        call_times.append(loop.time())
        if len(call_times) == 1:
            first_started.set()
            return RetentionSweepResult(0, 0, 0, 1)
        second_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager.run_retention_sweep_once = sweep_once
    started_at = loop.time()
    task = manager.start_retention_sweep(
        interval_seconds=0.03,
        retry_seconds=0.01,
    )
    try:
        await asyncio.wait_for(first_started.wait(), timeout=0.2)
        await asyncio.wait_for(second_started.wait(), timeout=0.5)
        assert call_times[0] - started_at < 0.1
        assert call_times[1] - call_times[0] >= 0.015
    finally:
        await manager.stop_retention_sweep()

    assert cancelled.is_set()
    assert task.done()
    assert manager._retention_task is None
    assert manager._retention_stop is None


@pytest.mark.asyncio
async def test_retention_failure_uses_prompt_retry_then_shutdown_cancels_worker() -> None:
    manager = BackgroundTaskManager(coordinator=object())
    retry_started = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()
    call_times: list[float] = []
    loop = asyncio.get_running_loop()

    async def sweep_once():
        call_times.append(loop.time())
        if len(call_times) == 1:
            raise RuntimeError("transient retention failure")
        retry_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager.run_retention_sweep_once = sweep_once
    task = manager.start_retention_sweep(
        interval_seconds=0.3,
        retry_seconds=0.01,
    )
    try:
        await asyncio.wait_for(retry_started.wait(), timeout=0.2)
        assert call_times[1] - call_times[0] < 0.15
    finally:
        await manager.stop_retention_sweep()

    assert cancelled.is_set()
    assert task.done()


@pytest.mark.asyncio
async def test_batch_limit_backlog_uses_prompt_retry_not_hourly_interval() -> None:
    manager = BackgroundTaskManager(coordinator=object())
    retry_started = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()
    callback_count = 0
    call_times: list[float] = []
    loop = asyncio.get_running_loop()

    async def sweep_once():
        call_times.append(loop.time())
        if len(call_times) == 1:
            return RetentionSweepResult(
                operations=6,
                submissions=0,
                compatibility_rows=0,
                batches=2,
                backlog=True,
            )
        retry_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def on_sweep() -> None:
        nonlocal callback_count
        callback_count += 1

    manager.run_retention_sweep_once = sweep_once
    task = manager.start_retention_sweep(
        interval_seconds=0.3,
        retry_seconds=0.01,
        on_sweep=on_sweep,
    )
    try:
        await asyncio.wait_for(retry_started.wait(), timeout=0.2)
        assert call_times[1] - call_times[0] < 0.15
    finally:
        await manager.stop_retention_sweep()

    assert callback_count == 1
    assert cancelled.is_set()
    assert task.done()
