"""020-async-queries: Tests for background task infrastructure."""

import asyncio
import dataclasses
import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from astralplane.repositories.background_tasks import BackgroundTaskStatus
from orchestrator.async_tasks import (
    BackgroundTaskAdmissionError,
    BackgroundTaskManager,
    BackgroundTask,
    VirtualWebSocket,
    TaskStatus,
)
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.work_admission import OperationState
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationNotFoundError,
    OperationOwner,
    OperationRecord,
    OperationRequest,
    OwnerScope,
    PurgeResult,
    SafeOperationProjection,
    WorkAdmissionCoordinator,
)
from shared.feature_flags import flags


# The manager only performs the Plane compatibility projection when
# bg_continuity is enabled; the flags-off SC-009 byte-equivalence run correctly
# performs no such write, so tests that assert on it skip cleanly there. Flags
# are read once at import, so this is stable for the whole process.
_REQUIRES_BG_CONTINUITY = pytest.mark.skipif(
    not flags.is_enabled("bg_continuity"),
    reason="legacy compatibility write requires FF_BG_CONTINUITY",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect(mgr):
    """Wait for all known tasks in the manager to finish."""
    tasks = [t.asyncio_task for t in mgr._tasks.values() if t.asyncio_task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_until(predicate, *, timeout=1.0):
    """Wait for an event-loop-owned predicate without hiding timeouts."""

    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not reached before the test deadline")
        await asyncio.sleep(0.005)


@dataclass
class _Clock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.current


class _RecordingObservability:
    def __init__(self):
        self.operation_events = []
        self.admission_statuses = []
        self.retention_observations = []

    def record_operation(
        self,
        event,
        *,
        operation_kind,
        result_code=None,
        phase=None,
    ):
        self.operation_events.append(
            (event, operation_kind, result_code, phase)
        )

    def observe_admission(self, status, *, operation_kind):
        self.admission_statuses.append((status, operation_kind))

    def observe_retention(self, *, purged_count, lag_seconds):
        self.retention_observations.append((purged_count, lag_seconds))


class _FailingObservability:
    def record_operation(self, *args, **kwargs):
        raise RuntimeError("collector unavailable")

    def observe_admission(self, *args, **kwargs):
        raise RuntimeError("collector unavailable")

    def observe_retention(self, *args, **kwargs):
        raise RuntimeError("collector unavailable")


class _FakePlaneRuntime:
    def __init__(self) -> None:
        self.transactions: list[object] = []

    @contextmanager
    def transaction(self, *, isolation=None):
        del isolation
        transaction = object()
        self.transactions.append(transaction)
        yield transaction


class _RecordingBackgroundRepository:
    def __init__(self) -> None:
        self.records = []
        self.transactions = []
        self.retained_at = None
        self.purge_ids: tuple[str, ...] = ()

    def apply_operation_projection(self, transaction, record):
        self.transactions.append(transaction)
        self.records.append(record)
        return record

    def oldest_overdue_for_administration(self, transaction, *, cutoff_at):
        self.transactions.append(transaction)
        self.cutoff_at = cutoff_at
        return self.retained_at

    def purge_overdue_for_administration(
        self,
        transaction,
        *,
        cutoff_at,
        limit,
    ):
        self.transactions.append(transaction)
        self.cutoff_at = cutoff_at
        return self.purge_ids[:limit]


def _bind_plane(manager, repository=None):
    runtime = _FakePlaneRuntime()
    repository = repository or _RecordingBackgroundRepository()
    manager.bind(
        plane_runtime=runtime,
        plane_repositories=SimpleNamespace(background_tasks=repository),
    )
    return runtime, repository


def _manager(
    *,
    active_limit=20,
    queue_limit=20,
    max_wait_ms=30_000,
    slot_lease=timedelta(seconds=30),
    clock=None,
    **manager_kwargs,
):
    clock = clock or _Clock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=queue_limit,
                max_wait_ms=max_wait_ms if queue_limit else None,
                config_revision="test-060",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        slot_lease=slot_lease,
    )
    return BackgroundTaskManager(coordinator=coordinator, **manager_kwargs)


def _maintenance_manager(*, active_limit=1, queue_limit=0, clock=None):
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.MAINTENANCE,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=queue_limit,
                max_wait_ms=30_000 if queue_limit else None,
                config_revision="test-060-maintenance",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock or _Clock(),
    )
    return BackgroundTaskManager(coordinator=coordinator)


def _claim_maintenance(coordinator):
    owner = OperationOwner(OwnerScope.MAINTENANCE, None, None)
    admitted = coordinator.submit(
        OperationRequest(
            operation_kind="retention_test",
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
    )
    assert admitted.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.MAINTENANCE,
        admitted.operation_id,
    )
    assert claim is not None
    return claim


# ---------------------------------------------------------------------------
# VirtualWebSocket Tests
# ---------------------------------------------------------------------------


class TestVirtualWebSocket:
    def test_send_text_json(self):
        task = BackgroundTask(task_id="t1", chat_id="c1", user_id="u1")
        vws = VirtualWebSocket(task)
        asyncio.run(vws.send_text('{"type": "test", "data": "hello"}'))
        assert task.outputs == [{"type": "test", "data": "hello"}]

    def test_send_text_raw(self):
        task = BackgroundTask(task_id="t1", chat_id="c1", user_id="u1")
        vws = VirtualWebSocket(task)
        asyncio.run(vws.send_text("plain text"))
        assert task.outputs == [{"type": "raw", "data": "plain text"}]

    def test_send_json_dict(self):
        task = BackgroundTask(task_id="t1", chat_id="c1", user_id="u1")
        vws = VirtualWebSocket(task)
        asyncio.run(vws.send_json({"type": "direct", "val": 42}))
        asyncio.run(vws.send_json(42))
        assert task.outputs == [{"type": "direct", "val": 42}]

    def test_no_capture_after_close(self):
        task = BackgroundTask(task_id="t1", chat_id="c1", user_id="u1")
        vws = VirtualWebSocket(task)
        asyncio.run(vws.close())
        asyncio.run(vws.send_json({"type": "after_close"}))
        asyncio.run(vws.send_text("after-close"))
        assert task.outputs == []

    def test_string_send_receive_and_legacy_socket_metadata(self):
        task = BackgroundTask(task_id="synthetic", chat_id="c1", user_id="u1")
        vws = VirtualWebSocket(task)

        asyncio.run(vws.send_json('{"type":"string"}'))

        assert task.outputs == [{"type": "string"}]
        assert asyncio.run(vws.receive_text()) == ""
        assert asyncio.run(vws.receive_json()) == {}
        assert vws.client == ("background", "synthetic")
        assert repr(vws) == "VirtualWebSocket(task=synthetic)"


# ---------------------------------------------------------------------------
# BackgroundTaskManager Tests
# ---------------------------------------------------------------------------


class TestBackgroundTaskManager:
    @pytest.mark.asyncio
    async def test_submit_creates_task(self):
        mgr = _manager()

        async def dummy(vws, **kw):
            vws.task.outputs.append({"type": "done"})

        t = await mgr.submit("c1", "u1", dummy)
        await _collect(mgr)
        assert t.chat_id == "c1"
        assert t.user_id == "u1"
        assert t.status == TaskStatus.COMPLETED
        assert str(uuid.UUID(t.task_id)) == t.task_id
        assert len(t.task_id) == 36
        assert t._operation.connection_generation is None
        assert t._operation.request_generation is None

    @pytest.mark.asyncio
    async def test_submit_persists_exact_wire_generations(self):
        mgr = _manager()
        connection_generation = uuid.uuid4()
        request_generation = uuid.uuid4()

        async def dummy(vws, **kw):
            pass

        task = await mgr.submit(
            "c1",
            "u1",
            dummy,
            connection_generation=connection_generation,
            request_generation=request_generation,
        )
        operation = task._operation

        assert operation is not None
        assert operation.connection_generation == connection_generation
        assert operation.request_generation == request_generation
        await _collect(mgr)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field_name", "malformed"),
        (
            ("connection_generation", "not-a-uuid"),
            ("request_generation", 42),
        ),
    )
    async def test_submit_rejects_malformed_generation_before_admission(
        self,
        monkeypatch,
        field_name,
        malformed,
    ):
        mgr = _manager()
        submission_called = False

        def unexpected_submit(request):
            nonlocal submission_called
            submission_called = True
            raise AssertionError("malformed generation reached admission")

        monkeypatch.setattr(mgr._coordinator, "submit", unexpected_submit)

        async def dummy(vws, **kw):
            pass

        with pytest.raises(ValueError, match=rf"{field_name} must be a UUID"):
            await mgr.submit("c1", "u1", dummy, **{field_name: malformed})

        assert submission_called is False

    @pytest.mark.asyncio
    async def test_task_runs_to_completion(self):
        mgr = _manager()

        async def dummy(vws, **kw):
            pass

        t = await mgr.submit("c1", "u1", dummy)
        await _collect(mgr)
        assert t.status == TaskStatus.COMPLETED
        assert t.completed_at is not None

    @pytest.mark.asyncio
    async def test_task_handles_exception(self):
        mgr = _manager()

        async def failing(vws, **kw):
            raise ValueError("test-explosion")

        t = await mgr.submit("c1", "u1", failing)
        await _collect(mgr)
        assert t.status == TaskStatus.FAILED
        assert "test-explosion" in t.errors[0]

    @pytest.mark.asyncio
    async def test_cancel_task(self):
        mgr = _manager()
        started = asyncio.Event()

        async def slow(vws, **kw):
            started.set()
            await asyncio.sleep(600)

        t = await mgr.submit("c1", "u1", slow)
        await started.wait()
        cancelled = await mgr.cancel(t.task_id)
        assert cancelled is True
        await asyncio.sleep(0.1)
        assert t.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_running_cancellation_joins_before_reusing_capacity(self):
        mgr = _manager(
            active_limit=1,
            queue_limit=1,
            slot_lease=timedelta(milliseconds=300),
            clock=lambda: datetime.now(UTC),
        )
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        queued_started = asyncio.Event()
        never = asyncio.Event()
        active = 0
        peak_active = 0
        cancel_request = None
        tasks = []

        async def running(vws):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            started.set()
            try:
                await never.wait()
            finally:
                cleanup_started.set()
                await vws.send_json({"type": "late_after_cancellation"})
                await cleanup_release.wait()
                active -= 1

        async def queued(vws):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            queued_started.set()
            active -= 1

        try:
            current = await mgr.submit("c0", "u1", running)
            successor = await mgr.submit("c1", "u1", queued)
            tasks.extend((current, successor))
            await asyncio.wait_for(started.wait(), timeout=1)

            cancel_request = asyncio.create_task(mgr.cancel(current.task_id))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            await asyncio.sleep(0.65)
            assert not queued_started.is_set()
            assert not cancel_request.done()

            cleanup_release.set()
            assert await asyncio.wait_for(cancel_request, timeout=1) is True
            await asyncio.wait_for(queued_started.wait(), timeout=1)
            assert peak_active == 1
            assert current.outputs == []
        finally:
            cleanup_release.set()
            if cancel_request is not None:
                await asyncio.gather(cancel_request, return_exceptions=True)
            for task in tasks:
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_caller_cancellation_joins_committed_cancel_and_worker_cleanup(
        self,
        monkeypatch,
    ):
        mgr = _manager(active_limit=1, queue_limit=0)
        started = asyncio.Event()
        worker_cleanup_finished = asyncio.Event()
        never = asyncio.Event()
        cancel_committed = threading.Event()
        cancel_response_release = threading.Event()
        cancel_request = None

        async def running(vws):
            started.set()
            try:
                await never.wait()
            finally:
                worker_cleanup_finished.set()

        task = await mgr.submit("c0", "u1", running)
        await asyncio.wait_for(started.wait(), timeout=1)
        coordinator = mgr._coordinator
        original_cancel = coordinator.cancel
        operation_id = uuid.UUID(task.task_id)

        def committed_then_delayed(*args, **kwargs):
            response = original_cancel(*args, **kwargs)
            if kwargs.get("operation_id") == operation_id:
                cancel_committed.set()
                assert cancel_response_release.wait(timeout=5)
            return response

        monkeypatch.setattr(coordinator, "cancel", committed_then_delayed)
        try:
            cancel_request = asyncio.create_task(mgr.cancel(task.task_id))
            await _wait_until(cancel_committed.is_set, timeout=1)

            assert cancel_request.cancel() is True
            await asyncio.sleep(0)
            assert not cancel_request.done()
            assert not worker_cleanup_finished.is_set()
            assert cancel_request.cancel() is True
            await asyncio.sleep(0)
            assert not cancel_request.done()

            cancel_response_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(cancel_request, timeout=1)

            assert worker_cleanup_finished.is_set()
            assert task.asyncio_task.done()
            assert task._virtual_websocket is None
            assert task.status is TaskStatus.CANCELLED
            durable = coordinator.query_operation(
                owner=task._owner,
                operation_id=operation_id,
            )
            assert durable.state is OperationState.CANCELLED
            assert (
                coordinator.inspect_admission_class(
                    AdmissionClass.BACKGROUND
                ).active_count
                == 0
            )
        finally:
            cancel_response_release.set()
            never.set()
            if cancel_request is not None:
                await asyncio.gather(cancel_request, return_exceptions=True)
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_double_caller_cancel_keeps_cleanup_failure_as_cause(
        self,
        monkeypatch,
    ):
        mgr = _manager(active_limit=1, queue_limit=0)
        started = asyncio.Event()
        worker_cleanup_finished = asyncio.Event()
        never = asyncio.Event()
        cancel_committed = threading.Event()
        cancel_response_release = threading.Event()
        flow_error_ready = asyncio.Event()
        flow_error_release = asyncio.Event()
        cancel_request = None

        async def running(vws):
            started.set()
            try:
                await never.wait()
            finally:
                worker_cleanup_finished.set()

        task = await mgr.submit("c0", "u1", running)
        await asyncio.wait_for(started.wait(), timeout=1)
        coordinator = mgr._coordinator
        original_cancel = coordinator.cancel
        operation_id = uuid.UUID(task.task_id)

        def committed_then_delayed(*args, **kwargs):
            response = original_cancel(*args, **kwargs)
            if kwargs.get("operation_id") == operation_id:
                cancel_committed.set()
                assert cancel_response_release.wait(timeout=5)
            return response

        original_cancellation_flow = mgr._cancel_and_cleanup

        async def cleanup_then_fail(*args, **kwargs):
            await original_cancellation_flow(*args, **kwargs)
            flow_error_ready.set()
            await flow_error_release.wait()
            raise RuntimeError("cleanup-after-committed-cancel")

        monkeypatch.setattr(coordinator, "cancel", committed_then_delayed)
        monkeypatch.setattr(mgr, "_cancel_and_cleanup", cleanup_then_fail)
        try:
            cancel_request = asyncio.create_task(mgr.cancel(task.task_id))
            await _wait_until(cancel_committed.is_set, timeout=1)

            assert cancel_request.cancel() is True
            await asyncio.sleep(0)
            assert not cancel_request.done()
            assert cancel_request.cancel() is True
            await asyncio.sleep(0)
            assert not cancel_request.done()

            cancel_response_release.set()
            await asyncio.wait_for(flow_error_ready.wait(), timeout=1)
            assert worker_cleanup_finished.is_set()
            assert not cancel_request.done()

            flow_error_release.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await asyncio.wait_for(cancel_request, timeout=1)

            assert isinstance(exc_info.value.__cause__, RuntimeError)
            assert str(exc_info.value.__cause__) == "cleanup-after-committed-cancel"
            assert task.asyncio_task.done()
            assert task._virtual_websocket is None
            assert task.status is TaskStatus.CANCELLED
            durable = coordinator.query_operation(
                owner=task._owner,
                operation_id=operation_id,
            )
            assert durable.state is OperationState.CANCELLED
        finally:
            cancel_response_release.set()
            flow_error_release.set()
            never.set()
            if cancel_request is not None:
                await asyncio.gather(cancel_request, return_exceptions=True)
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_dispatcher_ignores_stale_query_after_cancel_and_keeps_progressing(
        self,
        monkeypatch,
    ):
        clock = _Clock()
        mgr = _manager(active_limit=1, queue_limit=2, clock=clock)
        active_started = asyncio.Event()
        active_release = asyncio.Event()
        successor_started = asyncio.Event()
        cancel_entered = threading.Event()
        cancel_commit_release = threading.Event()
        stale_query_captured = threading.Event()
        stale_query_release = threading.Event()
        stale_queries = []
        cancel_request = None
        dispatcher = None

        # Retain queued callables without starting the normal dispatcher so the
        # cancellation can snapshot local authority before the forced query
        # interleaving below acquires the manager lock.
        monkeypatch.setattr(mgr, "_ensure_dispatcher_locked", lambda: None)

        async def active(vws):
            active_started.set()
            await active_release.wait()

        async def successor(vws):
            successor_started.set()

        current = await mgr.submit("c0", "u1", active)
        first_queued = await mgr.submit("c1", "u1", successor)
        clock.current += timedelta(milliseconds=1)
        second_queued = await mgr.submit("c2", "u1", successor)
        await asyncio.wait_for(active_started.wait(), timeout=1)

        coordinator = mgr._coordinator
        original_cancel = coordinator.cancel
        original_query = coordinator.query_operation
        first_id = uuid.UUID(first_queued.task_id)
        block_dispatch_query = threading.Event()

        def delayed_cancel(*args, **kwargs):
            if kwargs.get("operation_id") == first_id:
                cancel_entered.set()
                assert cancel_commit_release.wait(timeout=5)
            return original_cancel(*args, **kwargs)

        def stale_query(*args, **kwargs):
            response = original_query(*args, **kwargs)
            if (
                kwargs.get("operation_id") == first_id
                and block_dispatch_query.is_set()
                and not stale_query_captured.is_set()
            ):
                stale_queries.append(response)
                stale_query_captured.set()
                assert stale_query_release.wait(timeout=5)
            return response

        monkeypatch.setattr(coordinator, "cancel", delayed_cancel)
        monkeypatch.setattr(coordinator, "query_operation", stale_query)
        try:
            cancel_request = asyncio.create_task(mgr.cancel(first_queued.task_id))
            await _wait_until(cancel_entered.is_set, timeout=1)

            block_dispatch_query.set()
            mgr._dispatcher_wakeup = asyncio.Event()
            dispatcher = asyncio.create_task(
                mgr._dispatch_pending(),
                name="stale-query-dispatcher-test",
            )
            mgr._dispatcher_task = dispatcher
            await _wait_until(stale_query_captured.is_set, timeout=1)
            assert stale_queries[0].state is OperationState.QUEUED

            cancel_commit_release.set()
            await _wait_until(
                lambda: first_queued.status is TaskStatus.CANCELLED,
                timeout=1,
            )
            assert not dispatcher.done()

            stale_query_release.set()
            assert await asyncio.wait_for(cancel_request, timeout=1) is True
            assert first_queued.status is TaskStatus.CANCELLED

            active_release.set()
            await asyncio.wait_for(successor_started.wait(), timeout=1)
            await asyncio.wait_for(second_queued.asyncio_task, timeout=1)
            assert second_queued.status is TaskStatus.COMPLETED
            await asyncio.wait_for(dispatcher, timeout=1)
            assert dispatcher.exception() is None
        finally:
            cancel_commit_release.set()
            stale_query_release.set()
            active_release.set()
            if cancel_request is not None:
                await asyncio.gather(cancel_request, return_exceptions=True)
            if dispatcher is not None:
                await asyncio.gather(dispatcher, return_exceptions=True)
            for task in (current, first_queued, second_queued):
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response_order",
        ("terminal_response", "stale_response"),
    )
    async def test_completion_wins_inflight_cancellation_without_regression(
        self,
        monkeypatch,
        response_order,
    ):
        mgr = _manager(active_limit=1, queue_limit=1)
        started = asyncio.Event()
        finish = asyncio.Event()
        successor_started = asyncio.Event()
        cancel_entered = threading.Event()
        cancel_release = threading.Event()
        delayed_responses = []
        current = None
        successor = None
        cancel_request = None

        async def running(vws):
            started.set()
            await finish.wait()

        async def queued(vws):
            successor_started.set()

        try:
            current = await mgr.submit("c0", "u1", running)
            await asyncio.wait_for(started.wait(), timeout=1)
            coordinator = mgr._coordinator
            original_cancel = coordinator.cancel
            operation_id = uuid.UUID(current.task_id)

            def delayed_cancel(*args, **kwargs):
                if kwargs.get("operation_id") != operation_id:
                    return original_cancel(*args, **kwargs)
                if response_order == "terminal_response":
                    cancel_entered.set()
                    assert cancel_release.wait(timeout=5)
                    return original_cancel(*args, **kwargs)
                response = original_cancel(*args, **kwargs)
                delayed_responses.append(response)
                cancel_entered.set()
                assert cancel_release.wait(timeout=5)
                return response

            monkeypatch.setattr(coordinator, "cancel", delayed_cancel)
            cancel_request = asyncio.create_task(mgr.cancel(current.task_id))
            await _wait_until(cancel_entered.is_set, timeout=1)

            finish.set()
            await asyncio.wait_for(current.asyncio_task, timeout=1)
            assert current.status is TaskStatus.COMPLETED
            terminal_revision = current._operation.state_revision

            # The manager state lock must not be held by the blocked database
            # cancellation call. Completion released durable capacity, so an
            # unrelated successor can be admitted and run before it returns.
            successor = await asyncio.wait_for(
                mgr.submit("c1", "u1", queued),
                timeout=1,
            )
            await asyncio.wait_for(successor_started.wait(), timeout=1)
            await asyncio.wait_for(successor.asyncio_task, timeout=1)
            assert successor.status is TaskStatus.COMPLETED
            assert not cancel_request.done()

            if response_order == "stale_response":
                assert len(delayed_responses) == 1
                delayed = delayed_responses[0]
                assert delayed.state is OperationState.RUNNING
                assert delayed.cancel_requested_at is not None
                assert delayed.state_revision < terminal_revision

            cancel_release.set()
            assert await asyncio.wait_for(cancel_request, timeout=1) is False
            assert current.status is TaskStatus.COMPLETED
            assert current._operation.state is OperationState.COMPLETED
            assert current._operation.state_revision == terminal_revision
            durable = coordinator.query_operation(
                owner=current._owner,
                operation_id=operation_id,
            )
            assert durable.state is OperationState.COMPLETED
            assert durable.state_revision == terminal_revision
        finally:
            finish.set()
            cancel_release.set()
            if cancel_request is not None:
                await asyncio.gather(cancel_request, return_exceptions=True)
            for task in (current, successor):
                if task is not None and task.status in {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                }:
                    await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_stale_cancel_response_reconciles_newer_safe_projection(
        self,
        monkeypatch,
    ):
        mgr = _manager()
        started = asyncio.Event()
        hold = asyncio.Event()
        cancel_entered = threading.Event()
        cancel_release = threading.Event()
        delayed_responses = []

        async def running(vws):
            started.set()
            await hold.wait()

        task = await mgr.submit("c0", "u1", running)
        await asyncio.wait_for(started.wait(), timeout=1)
        coordinator = mgr._coordinator
        original_cancel = coordinator.cancel
        fence = task._execution_fence
        assert fence is not None

        def delayed_cancel(*args, **kwargs):
            response = original_cancel(*args, **kwargs)
            delayed_responses.append(response)
            cancel_entered.set()
            assert cancel_release.wait(timeout=5)
            return response

        monkeypatch.setattr(coordinator, "cancel", delayed_cancel)
        cancel_request = asyncio.create_task(mgr.cancel(task.task_id))
        try:
            await _wait_until(cancel_entered.is_set, timeout=1)
            updated = coordinator.update_phase(fence, "cancel_pending")
            assert await mgr._refresh(task) is True
            assert task._operation.state_revision == updated.state_revision
            assert not hasattr(task._operation, "cancel_requested_at")
            assert delayed_responses[0].state_revision < updated.state_revision

            cancel_release.set()
            assert await asyncio.wait_for(cancel_request, timeout=1) is True
            assert task.status is TaskStatus.CANCELLED
        finally:
            hold.set()
            cancel_release.set()
            await asyncio.gather(cancel_request, return_exceptions=True)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_same_revision_reconciliation_is_idempotent_only(self):
        mgr = _manager()
        hold = asyncio.Event()

        async def running(vws):
            await hold.wait()

        task = await mgr.submit("c0", "u1", running)
        full = task._operation
        assert isinstance(full, OperationRecord)
        safe = mgr._coordinator.query_operation(
            owner=task._owner,
            operation_id=uuid.UUID(task.task_id),
        )
        assert isinstance(safe, SafeOperationProjection)
        try:
            detached = BackgroundTask(task.task_id, "c0", "u1")
            assert mgr._reconcile_operation_projection(detached, safe) is safe
            assert detached._operation is safe
            with pytest.raises(RuntimeError, match="identity changed"):
                mgr._reconcile_operation_projection(
                    task,
                    replace(full, operation_id=uuid.uuid4()),
                )
            assert task._operation is full

            equivalent_full = replace(full)
            assert (
                mgr._reconcile_operation_projection(task, equivalent_full)
                is full
            )
            assert task._operation is full

            hidden_payload_swap = replace(
                full,
                cancel_requested_at=full.updated_at,
            )
            with pytest.raises(RuntimeError, match="without advancing"):
                mgr._reconcile_operation_projection(task, hidden_payload_swap)
            assert task._operation is full

            # A safe/full pair at the same revision represents the same
            # authority only when every field exposed by the safe contract is
            # identical. The existing richer projection is retained.
            assert mgr._reconcile_operation_projection(task, safe) is full
            assert task._operation is full
            for changed_safe in (
                replace(safe, state=OperationState.COMPLETED),
                replace(safe, safe_summary="forged-same-revision"),
            ):
                with pytest.raises(RuntimeError, match="without advancing"):
                    mgr._reconcile_operation_projection(task, changed_safe)
                assert task._operation is full

            # The inverse safe/full ordering is also idempotent-only and must
            # not upgrade the stored representation at an unchanged revision.
            task._apply_operation(safe)
            assert task._operation is safe
            assert mgr._reconcile_operation_projection(task, full) is safe
            assert task._operation is safe
            with pytest.raises(RuntimeError, match="without advancing"):
                mgr._reconcile_operation_projection(
                    task,
                    replace(full, phase_code="forged_same_revision"),
                )
            assert task._operation is safe
        finally:
            hold.set()
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_cancellation_before_wrapper_start_releases_capacity_once(self):
        mgr = _manager(active_limit=1, queue_limit=0)
        user_code_called = False

        async def must_not_run(vws):
            nonlocal user_code_called
            user_code_called = True

        task = await mgr.submit("c1", "u1", must_not_run)
        task.asyncio_task.cancel()

        assert await mgr.cancel(task.task_id) is True
        assert user_code_called is False
        assert task.status is TaskStatus.CANCELLED
        assert (
            mgr._coordinator.inspect_admission_class(
                AdmissionClass.BACKGROUND
            ).active_count
            == 0
        )

    @pytest.mark.asyncio
    async def test_list_for_user(self):
        mgr = _manager()

        async def dummy(vws, **kw):
            pass

        t1 = await mgr.submit("c1", "u1", dummy)
        t2 = await mgr.submit("c2", "u1", dummy)
        await mgr.submit("c3", "u2", dummy)
        await _collect(mgr)
        u1_tasks = await mgr.list_for_user("u1")
        assert len(u1_tasks) == 2
        ids = {t.task_id for t in u1_tasks}
        assert t1.task_id in ids
        assert t2.task_id in ids

    @pytest.mark.asyncio
    async def test_get_active_for_chat(self):
        mgr = _manager()
        started = asyncio.Event()

        async def slow(vws, **kw):
            started.set()
            await asyncio.sleep(600)

        t1 = await mgr.submit("c1", "u1", slow)
        await started.wait()
        active = await mgr.get_active_for_chat("c1")
        assert active is not None
        assert active.task_id == t1.task_id

    @pytest.mark.asyncio
    async def test_watchers_notified(self):
        mgr = _manager()
        notifications = []

        class FakeWS:
            # 055: watcher notification uses send_text (send_json would
            # double-encode over a real FastAPI socket).
            async def send_text(self, data):
                notifications.append(json.loads(data))

        async def dummy(vws, **kw):
            pass

        t = await mgr.submit("c1", "u1", dummy)
        t.watchers.append(FakeWS())
        await _collect(mgr)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "task_completed"
        assert notifications[0]["payload"]["task_id"] == t.task_id

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        """Cancelling a non-existent task returns False."""
        mgr = _manager()
        result = await mgr.cancel("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_prunes_operation_missing_after_initial_refresh(
        self,
        monkeypatch,
    ):
        mgr = _manager(active_limit=1, queue_limit=1)
        release = asyncio.Event()

        async def running(vws):
            await release.wait()

        running_task = await mgr.submit("c0", "u1", running)
        queued_task = await mgr.submit("c1", "u1", running)
        coordinator = mgr._coordinator
        original_cancel = coordinator.cancel
        operation_id = uuid.UUID(queued_task.task_id)

        def missing_after_refresh(*args, **kwargs):
            if kwargs.get("operation_id") == operation_id:
                raise OperationNotFoundError("operation was retained elsewhere")
            return original_cancel(*args, **kwargs)

        monkeypatch.setattr(coordinator, "cancel", missing_after_refresh)
        try:
            assert await mgr.cancel(queued_task.task_id) is False
            assert queued_task.task_id not in mgr._tasks
            assert queued_task.task_id not in mgr._pending_executions
        finally:
            original_cancel(
                owner=queued_task._owner,
                operation_id=operation_id,
                terminal_code="cancelled_by_user",
            )
            release.set()
            await _collect(mgr)
            if running_task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                await mgr.cancel(running_task.task_id)

    @pytest.mark.asyncio
    async def test_get_nonexistent(self):
        """Getting a non-existent task returns None."""
        mgr = _manager()
        result = await mgr.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_active_for_idle_chat(self):
        """get_active_for_chat on a chat with no tasks returns None."""
        mgr = _manager()
        result = await mgr.get_active_for_chat("idle-chat")
        assert result is None

    def test_background_task_to_dict(self):
        """BackgroundTask.to_dict produces expected shape."""
        t = BackgroundTask(task_id="t99", chat_id="c99", user_id="u99")
        d = t.to_dict()
        assert d["task_id"] == "t99"
        assert d["chat_id"] == "c99"
        assert d["user_id"] == "u99"
        assert d["status"] == "queued"
        assert d["completed_at"] is None

    def test_background_task_retains_legacy_dataclass_surface(self):
        task = BackgroundTask("synthetic", "c1", "u1", title="Report")
        assert dataclasses.is_dataclass(task)
        projection = dataclasses.asdict(task)
        assert projection["task_id"] == "synthetic"
        assert projection["status"] is TaskStatus.QUEUED
        assert "_operation" not in projection
        twin = dataclasses.replace(task)
        assert twin == task
        assert twin._operation is None
        assert "BackgroundTask(task_id='synthetic'" in repr(task)
        task.status = TaskStatus.RUNNING
        task.completed_at = datetime.now(UTC)
        assert task.status is TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_managed_background_authority_fields_are_read_only(self):
        mgr = _manager()
        hold = asyncio.Event()

        async def slow(vws):
            await hold.wait()

        task = await mgr.submit("c1", "u1", slow)
        detached = dataclasses.replace(task)
        assert detached._operation is None
        detached.status = TaskStatus.FAILED
        for field_name, value in (
            ("task_id", "forged"),
            ("chat_id", "forged"),
            ("user_id", "forged"),
            ("status", TaskStatus.COMPLETED),
            ("created_at", datetime.now(UTC)),
            ("completed_at", datetime.now(UTC)),
        ):
            with pytest.raises(AttributeError, match="read-only"):
                setattr(task, field_name, value)
        assert task.to_dict()["status"] == "running"
        hold.set()
        await _collect(mgr)

    @pytest.mark.asyncio
    async def test_managed_authority_rejects_duplicate_attach_and_invalid_fence_type(
        self,
    ):
        mgr = _manager()
        hold = asyncio.Event()

        async def slow(vws):
            await hold.wait()

        task = await mgr.submit("c1", "u1", slow)
        with pytest.raises(RuntimeError, match="already attached"):
            task._attach_authority(
                owner=task._owner,
                operation=task._operation,
                execution_fence=task._execution_fence,
            )
        with pytest.raises(TypeError, match="ExecutionFence"):
            task._apply_operation(task._operation, execution_fence=object())

        hold.set()
        await _collect(mgr)
        task._execution_fence = None
        assert task.operation_execution_generation == 1

    @pytest.mark.asyncio
    async def test_submit_without_explicit_coordinator_fails_closed(self):
        mgr = BackgroundTaskManager()

        async def dummy(vws):
            return None

        with pytest.raises(RuntimeError, match="WorkAdmissionCoordinator"):
            await mgr.submit("c1", "u1", dummy)

    def test_dispatch_poll_interval_must_be_positive(self):
        with pytest.raises(ValueError, match="poll interval"):
            BackgroundTaskManager(dispatch_poll_seconds=0)

    @pytest.mark.asyncio
    async def test_explicit_admission_refusal_preserves_safe_details(self):
        mgr = _manager(active_limit=1, queue_limit=0)
        hold = asyncio.Event()

        async def slow(vws):
            await hold.wait()

        first = await mgr.submit("c1", "u1", slow)
        with pytest.raises(BackgroundTaskAdmissionError) as caught:
            await mgr.submit("c2", "u1", slow)

        assert caught.value.code == "capacity_exceeded"
        assert caught.value.retryable is True
        assert caught.value.retry_after_ms is not None
        assert await mgr.cancel(first.task_id) is True

    @pytest.mark.asyncio
    async def test_background_ceiling_five_dispatches_accepted_queue_in_fifo_order(
        self,
    ):
        clock = _Clock()
        mgr = _manager(active_limit=5, queue_limit=2, clock=clock)
        releases = [asyncio.Event() for _ in range(7)]
        started = [asyncio.Event() for _ in range(7)]
        start_order = []
        active = 0
        peak_active = 0
        tasks = []

        async def bounded(vws, index):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            start_order.append(index)
            started[index].set()
            try:
                await releases[index].wait()
            finally:
                active -= 1

        try:
            for index in range(7):
                tasks.append(await mgr.submit(f"c{index}", "u1", bounded, index))
                clock.current += timedelta(microseconds=1)

            await asyncio.gather(
                *(asyncio.wait_for(event.wait(), timeout=1) for event in started[:5])
            )
            await asyncio.sleep(0.02)
            status = mgr._coordinator.inspect_admission_class(
                AdmissionClass.BACKGROUND
            )
            assert status.active_count == 5
            assert status.queued_count == 2
            assert start_order == [0, 1, 2, 3, 4]
            assert all(str(uuid.UUID(task.task_id)) == task.task_id for task in tasks)
            assert len({task.task_id for task in tasks}) == len(tasks)

            releases[0].set()
            await asyncio.wait_for(started[5].wait(), timeout=1)
            assert start_order == [0, 1, 2, 3, 4, 5]

            releases[1].set()
            await asyncio.wait_for(started[6].wait(), timeout=1)
            assert start_order == [0, 1, 2, 3, 4, 5, 6]
            assert peak_active == 5
        finally:
            for task in tasks:
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            for release in releases:
                release.set()
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_background_queue_is_finite_and_wait_expiry_never_runs_user_code(
        self,
    ):
        clock = _Clock()
        mgr = _manager(
            active_limit=1,
            queue_limit=2,
            max_wait_ms=100,
            clock=clock,
        )
        release = asyncio.Event()
        calls = []
        tasks = []

        async def work(vws, label):
            calls.append(label)
            if label == "running":
                await release.wait()

        try:
            running = await mgr.submit("c0", "u1", work, "running")
            tasks.append(running)
            queued_one = await mgr.submit("c1", "u1", work, "queued-one")
            queued_two = await mgr.submit("c2", "u1", work, "queued-two")
            tasks.extend((queued_one, queued_two))

            with pytest.raises(BackgroundTaskAdmissionError) as caught:
                await mgr.submit("c3", "u1", work, "refused")
            assert caught.value.code == "capacity_exceeded"
            assert caught.value.retryable is True

            clock.current += timedelta(milliseconds=101)
            await _wait_until(
                lambda: queued_one.status is TaskStatus.RETRYABLE
                and queued_two.status is TaskStatus.RETRYABLE
            )

            assert calls == ["running"]
            assert queued_one.operation_execution_generation is None
            assert queued_two.operation_execution_generation is None
            assert queued_one._operation.terminal_code == "queue_wait_expired"
            assert queued_two._operation.terminal_code == "queue_wait_expired"
        finally:
            for task in tasks:
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            release.set()
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_saturated_dispatcher_polls_fifo_head_not_every_queued_item(
        self, monkeypatch
    ):
        mgr = _manager(active_limit=1, queue_limit=20)
        release = asyncio.Event()
        tasks = []

        async def blocker(vws):
            await release.wait()

        async def queued(vws):
            raise AssertionError("saturated queued work must not start")

        try:
            tasks.append(await mgr.submit("c0", "u1", blocker))
            for index in range(20):
                tasks.append(await mgr.submit(f"c{index + 1}", "u1", queued))

            real_claim = mgr._coordinator.claim_operation
            claim_calls = 0

            def counted_claim(class_name, operation_id):
                nonlocal claim_calls
                claim_calls += 1
                return real_claim(class_name, operation_id)

            monkeypatch.setattr(mgr._coordinator, "claim_operation", counted_claim)
            await asyncio.sleep(0.6)

            assert claim_calls <= 4
            assert all(task.status is TaskStatus.QUEUED for task in tasks[1:])
        finally:
            for task in reversed(tasks):
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            release.set()
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_cancelled_queued_task_is_skipped_and_next_fifo_item_runs(self):
        mgr = _manager(active_limit=1, queue_limit=2)
        release = asyncio.Event()
        survivor_started = asyncio.Event()
        calls = []
        tasks = []

        async def work(vws, label):
            calls.append(label)
            if label == "running":
                await release.wait()
            elif label == "survivor":
                survivor_started.set()

        try:
            running = await mgr.submit("c0", "u1", work, "running")
            cancelled = await mgr.submit("c1", "u1", work, "cancelled")
            survivor = await mgr.submit("c2", "u1", work, "survivor")
            tasks.extend((running, cancelled, survivor))

            assert await mgr.cancel(cancelled.task_id) is True
            assert cancelled.status is TaskStatus.CANCELLED
            assert await mgr.cancel(cancelled.task_id) is False

            release.set()
            await asyncio.wait_for(survivor_started.wait(), timeout=1)
            await _wait_until(lambda: survivor.status is TaskStatus.COMPLETED)

            assert calls == ["running", "survivor"]
            assert cancelled.asyncio_task is None
            assert cancelled._operation.terminal_code == "cancelled_by_user"
        finally:
            release.set()
            for task in tasks:
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_dispatcher_retries_same_operation_after_transient_claim_failure(
        self, monkeypatch
    ):
        mgr = _manager(active_limit=1, queue_limit=1, dispatch_poll_seconds=0.01)
        release = asyncio.Event()
        queued_started = asyncio.Event()
        tasks = []

        async def blocker(vws):
            await release.wait()

        async def queued(vws):
            queued_started.set()

        try:
            tasks.append(await mgr.submit("c0", "u1", blocker))
            queued_task = await mgr.submit("c1", "u1", queued)
            tasks.append(queued_task)
            real_claim = mgr._coordinator.claim_operation
            failed_once = False

            def transient_claim(class_name, operation_id):
                nonlocal failed_once
                if operation_id == uuid.UUID(queued_task.task_id) and not failed_once:
                    failed_once = True
                    raise RuntimeError("transient claim failure")
                return real_claim(class_name, operation_id)

            monkeypatch.setattr(mgr._coordinator, "claim_operation", transient_claim)
            release.set()
            await asyncio.wait_for(queued_started.wait(), timeout=1)
            await _wait_until(lambda: queued_task.status is TaskStatus.COMPLETED)

            assert failed_once is True
            assert queued_task.operation_execution_generation == 1
        finally:
            release.set()
            for task in tasks:
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_submit_returns_accepted_identity_when_initial_handoff_retries(
        self, monkeypatch
    ):
        mgr = _manager(dispatch_poll_seconds=0.01)
        real_claim = mgr._coordinator.claim_operation
        claim_calls = 0

        def transient_initial_claim(class_name, operation_id):
            nonlocal claim_calls
            claim_calls += 1
            if claim_calls == 1:
                raise RuntimeError("transient handoff failure")
            return real_claim(class_name, operation_id)

        monkeypatch.setattr(
            mgr._coordinator,
            "claim_operation",
            transient_initial_claim,
        )

        async def done(vws):
            return None

        task = await mgr.submit("c1", "u1", done)
        await _wait_until(lambda: task.status is TaskStatus.COMPLETED)

        assert claim_calls >= 2
        assert str(uuid.UUID(task.task_id)) == task.task_id

    @pytest.mark.asyncio
    async def test_bind_is_additive_and_refuses_authority_replacement(self):
        manager = BackgroundTaskManager()
        bound = _manager()._coordinator
        replacement = _manager()._coordinator
        plane_runtime = _FakePlaneRuntime()
        background_repository = _RecordingBackgroundRepository()
        repositories = SimpleNamespace(
            background_tasks=background_repository
        )

        manager.bind(
            coordinator=bound,
            plane_runtime=plane_runtime,
            plane_repositories=repositories,
            on_complete=lambda *_: None,
        )
        manager.bind(
            coordinator=bound,
            plane_runtime=plane_runtime,
            plane_repositories=repositories,
        )
        assert manager._coordinator is bound
        assert manager._plane_runtime is plane_runtime
        assert manager._background_tasks is background_repository
        assert manager._on_complete is not None
        with pytest.raises(RuntimeError, match="cannot replace"):
            manager.bind(coordinator=replacement)
        with pytest.raises(RuntimeError, match="cannot replace"):
            manager.bind(
                plane_runtime=_FakePlaneRuntime(),
                background_repository=background_repository,
            )
        with pytest.raises(ValueError, match="must be bound together"):
            manager.bind(plane_runtime=plane_runtime)

        async def dummy(vws):
            return None

        submitted = await manager.submit("c1", "u1", dummy)
        assert submitted.asyncio_task is not None
        if flags.is_enabled("bg_continuity"):
            await _wait_until(lambda: bool(background_repository.records))
        else:
            assert background_repository.records == []
        with pytest.raises(RuntimeError, match="cannot replace"):
            manager.bind(coordinator=replacement)
        await _collect(manager)

    def test_bind_rejects_incomplete_or_conflicting_plane_contract(self):
        manager = BackgroundTaskManager()
        runtime = _FakePlaneRuntime()
        repository = _RecordingBackgroundRepository()

        with pytest.raises(ValueError, match="expose background_tasks"):
            manager.bind(
                plane_runtime=runtime,
                plane_repositories=SimpleNamespace(),
            )
        with pytest.raises(RuntimeError, match="differs from the Plane catalog"):
            manager.bind(
                plane_runtime=runtime,
                plane_repositories=SimpleNamespace(
                    background_tasks=repository
                ),
                background_repository=_RecordingBackgroundRepository(),
            )
        with pytest.raises(TypeError, match=r"transaction\(\)"):
            manager.bind(
                plane_runtime=object(),
                background_repository=repository,
            )
        with pytest.raises(TypeError, match="required Plane contract"):
            manager.bind(
                plane_runtime=runtime,
                background_repository=object(),
            )

        manager.bind(
            plane_runtime=runtime,
            background_repository=repository,
        )
        with pytest.raises(RuntimeError, match="cannot replace the bound background"):
            manager.bind(
                plane_runtime=runtime,
                background_repository=_RecordingBackgroundRepository(),
            )

    def test_projection_record_rejects_synthetic_task_without_authority(self):
        with pytest.raises(RuntimeError, match="requires operation authority"):
            BackgroundTaskManager._projection_record(
                BackgroundTask(task_id="synthetic", chat_id="c1", user_id="u1")
            )

    @pytest.mark.asyncio
    async def test_claim_mismatch_leaves_authoritative_projection_unexecuted(
        self, monkeypatch
    ):
        mgr = _manager()
        monkeypatch.setattr(
            mgr._coordinator,
            "claim_operation",
            lambda class_name, operation_id: None,
        )

        async def must_not_run(vws):
            raise AssertionError("unselected work executed")

        task = await mgr.submit("c1", "u1", must_not_run)

        assert task.status is TaskStatus.RUNNING
        assert task.asyncio_task is None
        assert await mgr.cancel(task.task_id) is True
        assert task.status is TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_submit_target_handoff_does_not_consume_older_preselection(self):
        mgr = _manager(active_limit=2)
        owner = OperationOwner(OwnerScope.USER, "u1", None)
        older = mgr._coordinator.submit(
            OperationRequest(
                operation_kind="async_chat",
                admission_class=AdmissionClass.BACKGROUND,
                owner=owner,
                submission_id=uuid.uuid4(),
                idempotency_namespace=None,
                idempotency_key=None,
                normalized_input_digest=None,
                chat_id="older-chat",
                parent_operation_id=None,
                connection_generation=None,
                request_generation=None,
            )
        )

        async def done(vws):
            return None

        local = await mgr.submit("local-chat", "u1", done)
        await _collect(mgr)
        assert local.task_id != str(older.operation_id)
        assert local.status is TaskStatus.COMPLETED

        older_claim = mgr._coordinator.claim_next(AdmissionClass.BACKGROUND)
        assert older_claim is not None
        assert older_claim.operation.operation_id == older.operation_id
        mgr._coordinator.terminalize(
            older_claim.fence,
            state=OperationState.CANCELLED,
            terminal_code="test_cleanup",
            safe_summary="Cancelled",
            retry_after_ms=None,
        )

    @pytest.mark.asyncio
    @_REQUIRES_BG_CONTINUITY
    async def test_completion_fan_and_plane_projection_keep_operation_fence(self):
        mgr = _manager()
        _, repository = _bind_plane(mgr)
        fanned = []

        async def fan(task, frame):
            fanned.append(frame)
            return 2

        class BrokenWatcher:
            async def send_text(self, data):
                raise RuntimeError("gone")

        mgr.bind(on_complete=fan)

        async def narrative(vws):
            await vws.send_json(
                {
                    "type": "ui_render",
                    "target": "chat",
                    "components": [{"content": "Report finished."}],
                }
            )

        task = await mgr.submit("c1", "u1", narrative, title="Report")
        task.watchers.append(BrokenWatcher())
        await _collect(mgr)
        await _wait_until(
            lambda: any(
                record.status is BackgroundTaskStatus.COMPLETED
                for record in repository.records
            )
        )

        assert fanned[0]["payload"]["summary"] == "Report finished."
        assert all(
            record.operation_id == task.task_id
            and record.owner_id == "u1"
            and record.conversation_id == "c1"
            for record in repository.records
        )
        terminal = next(
            record
            for record in repository.records
            if record.status is BackgroundTaskStatus.COMPLETED
        )
        assert terminal.summary == "Report finished."
        assert terminal.notified is True
        assert terminal.completed_at is not None
        assert task.operation_execution_generation == 1
        assert task._execution_fence is None
        before = len(fanned)
        await mgr._notify_watchers(task)
        assert len(fanned) == before

    @pytest.mark.asyncio
    @_REQUIRES_BG_CONTINUITY
    async def test_delayed_submit_write_cannot_regress_terminal_durable_status(self):
        mgr = _manager()
        initial_started = threading.Event()
        initial_release = threading.Event()
        terminal_written = threading.Event()

        class ReorderedRepository(_RecordingBackgroundRepository):
            def __init__(self):
                super().__init__()
                self.row = None
                self.lock = threading.Lock()

            def apply_operation_projection(self, transaction, record):
                if record.completed_at is None:
                    initial_started.set()
                    initial_release.wait(timeout=2)
                with self.lock:
                    if (
                        self.row is not None
                        and self.row.completed_at is not None
                        and record.completed_at is None
                    ):
                        raise RuntimeError("projection moved backwards")
                    self.row = record
                    if record.completed_at is not None:
                        terminal_written.set()
                return record

        repository = ReorderedRepository()
        _bind_plane(mgr, repository)
        finish = asyncio.Event()

        async def done(vws):
            await finish.wait()

        await mgr.submit("c1", "u1", done)
        await asyncio.wait_for(
            asyncio.to_thread(initial_started.wait),
            timeout=1,
        )
        finish.set()
        await _collect(mgr)
        await asyncio.wait_for(
            asyncio.to_thread(terminal_written.wait),
            timeout=1,
        )
        initial_release.set()
        await _wait_until(
            lambda: mgr._async_plane_runtime.snapshot().active == 0
        )

        assert repository.row.status is BackgroundTaskStatus.COMPLETED

    @pytest.mark.asyncio
    @_REQUIRES_BG_CONTINUITY
    async def test_failed_task_uses_generic_durable_and_notification_summary(self):
        mgr = _manager()
        frames = []

        async def fan(task, frame):
            frames.append(frame)
            raise RuntimeError("fan unavailable")

        mgr.bind(on_complete=fan)

        async def failing(vws):
            raise RuntimeError("provider-secret-detail")

        task = await mgr.submit("c1", "u1", failing)
        await _collect(mgr)

        assert task.status is TaskStatus.FAILED
        assert task._operation.safe_summary == "Background task failed"
        assert frames[0]["payload"]["summary"] == "Background task failed"
        assert task.errors == ["provider-secret-detail"]

    @pytest.mark.asyncio
    async def test_execution_lease_renews_before_start_and_within_one_third(
        self, monkeypatch
    ):
        lease = timedelta(milliseconds=90)
        mgr = _manager(slot_lease=lease)
        coordinator = mgr._coordinator
        assert coordinator.slot_lease == lease
        real_renew = coordinator.renew_execution_lease
        renewals = {}
        loop = asyncio.get_running_loop()

        def recording_renew(fence):
            renewals.setdefault(str(fence.operation_id), []).append(loop.time())
            return real_renew(fence)

        monkeypatch.setattr(coordinator, "renew_execution_lease", recording_renew)

        async def slow(vws):
            task_renewals = renewals.get(vws.task.task_id, [])
            assert task_renewals, "the execution lease must renew before user code starts"
            await asyncio.sleep(0.13)

        task = await mgr.submit("c1", "u1", slow)
        await _collect(mgr)

        task_renewals = renewals.get(task.task_id, [])
        assert task.status is TaskStatus.COMPLETED
        assert len(task_renewals) >= 4
        assert all(
            later - earlier <= 0.06
            for earlier, later in zip(task_renewals, task_renewals[1:])
        )

    @pytest.mark.asyncio
    async def test_stale_initial_lease_refuses_user_code_and_terminal_claim(
        self, monkeypatch
    ):
        mgr = _manager()
        coordinator = mgr._coordinator
        real_renew = coordinator.renew_execution_lease
        replacement_fences = []
        user_code_called = False

        def stale_initial_renew(fence):
            replacement_fences.append(coordinator.reselect_execution(fence))
            return real_renew(fence)

        monkeypatch.setattr(coordinator, "renew_execution_lease", stale_initial_renew)

        async def must_not_run(vws):
            nonlocal user_code_called
            user_code_called = True

        task = await mgr.submit("c1", "u1", must_not_run)
        await _collect(mgr)

        assert user_code_called is False
        assert task.outputs == []
        assert task._execution_fence is None
        assert task.status is TaskStatus.RUNNING
        assert len(replacement_fences) == 1

        coordinator.terminalize(
            replacement_fences[0],
            state=OperationState.RETRYABLE,
            terminal_code="execution_lease_lost",
            safe_summary="Execution lease lost",
            retry_after_ms=1000,
        )

    @pytest.mark.asyncio
    async def test_failed_initial_lease_is_retryable_without_entering_user_code(
        self, monkeypatch
    ):
        mgr = _manager()
        user_code_called = False

        def unavailable_renew(fence):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(
            mgr._coordinator,
            "renew_execution_lease",
            unavailable_renew,
        )

        async def must_not_run(vws):
            nonlocal user_code_called
            user_code_called = True

        task = await mgr.submit("c1", "u1", must_not_run)
        await _collect(mgr)

        assert user_code_called is False
        assert task.status is TaskStatus.RETRYABLE
        assert task._operation.terminal_code == "execution_lease_renewal_failed"
        assert task._operation.retry_after_ms == 1000
        assert task.outputs == []

    @pytest.mark.asyncio
    async def test_failed_periodic_lease_joins_cleanup_before_capacity_reuse(
        self, monkeypatch
    ):
        mgr = _manager(
            active_limit=1,
            queue_limit=1,
            slot_lease=timedelta(milliseconds=300),
        )
        coordinator = mgr._coordinator
        real_renew = coordinator.renew_execution_lease
        renew_count = 0
        running_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        queued_started = asyncio.Event()
        tasks = []

        def fail_periodic_renew(fence):
            nonlocal renew_count
            renew_count += 1
            if renew_count == 2:
                raise RuntimeError("database unavailable")
            return real_renew(fence)

        monkeypatch.setattr(coordinator, "renew_execution_lease", fail_periodic_renew)

        async def running(vws):
            await vws.send_json({"type": "before_renewal_failure"})
            running_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                await vws.send_json({"type": "late_after_renewal_failure"})

        async def queued(vws):
            queued_started.set()

        try:
            current = await mgr.submit("c0", "u1", running)
            tasks.append(current)
            await asyncio.wait_for(running_started.wait(), timeout=1)
            successor = await mgr.submit("c1", "u1", queued)
            tasks.append(successor)

            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            await asyncio.sleep(0.35)
            assert not queued_started.is_set()

            cleanup_release.set()
            await _wait_until(lambda: current.status is TaskStatus.RETRYABLE)
            await asyncio.wait_for(queued_started.wait(), timeout=1)
            await _wait_until(lambda: successor.status is TaskStatus.COMPLETED)

            assert current.outputs == [{"type": "before_renewal_failure"}]
            assert current._operation.terminal_code == "execution_lease_renewal_failed"
        finally:
            cleanup_release.set()
            for task in tasks:
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    await mgr.cancel(task.task_id)
            await _collect(mgr)

    @pytest.mark.asyncio
    async def test_execution_lease_cas_loss_refuses_late_output_and_stale_terminal(
        self, monkeypatch
    ):
        mgr = _manager(slot_lease=timedelta(milliseconds=90))
        coordinator = mgr._coordinator
        real_renew = coordinator.renew_execution_lease
        replacement_fences = []
        renew_count = 0
        task = None

        def lose_second_renewal(fence):
            nonlocal renew_count
            renew_count += 1
            if renew_count == 2:
                replacement_fences.append(coordinator.reselect_execution(fence))
            return real_renew(fence)

        monkeypatch.setattr(coordinator, "renew_execution_lease", lose_second_renewal)

        async def stale_worker(vws):
            await vws.send_json({"type": "before_lease_loss"})
            try:
                await asyncio.Event().wait()
            finally:
                # A worker may execute cleanup after cancellation, but it must
                # no longer be able to publish visible output.
                await vws.send_json({"type": "late_after_lease_loss"})

        try:
            task = await mgr.submit("c1", "u1", stale_worker)
            await _wait_until(
                lambda: task.asyncio_task is not None and task.asyncio_task.done()
            )

            assert renew_count == 2
            assert len(replacement_fences) == 1
            assert task.outputs == [{"type": "before_lease_loss"}]
            assert task._execution_fence is None
            current = coordinator.assert_current_execution(replacement_fences[0])
            assert current.state is OperationState.RUNNING
            assert current.cancel_requested_at is None
        finally:
            if replacement_fences:
                coordinator.terminalize(
                    replacement_fences[0],
                    state=OperationState.RETRYABLE,
                    terminal_code="execution_lease_lost",
                    safe_summary="Execution lease lost",
                    retry_after_ms=1000,
                )
            elif task is not None:
                await mgr.cancel(task.task_id)
            if task is not None and task.asyncio_task is not None:
                await asyncio.gather(task.asyncio_task, return_exceptions=True)

    def test_summary_projection_covers_supported_legacy_output_shapes(self):
        task = BackgroundTask("synthetic", "c1", "u1")
        task.outputs.extend(
            [
                "ignored",
                {"type": "ui_render", "target": "chat", "components": [None]},
                {
                    "type": "ui_render",
                    "target": "chat",
                    "components": [{"content": "  Canvas result  "}],
                },
                {"payload": {"message": "  Payload result  "}},
                {"text": "  Final result  "},
            ]
        )
        assert BackgroundTaskManager._summary_from_outputs(task) == "Final result"

        error_only = BackgroundTask("synthetic-2", "c1", "u1")
        error_only.errors.append("  local failure  ")
        assert (
            BackgroundTaskManager._summary_from_outputs(error_only) == "local failure"
        )

    @pytest.mark.asyncio
    async def test_stale_execution_refreshes_instead_of_inventing_terminal(self):
        mgr = _manager()
        hold = asyncio.Event()

        async def slow(vws):
            await hold.wait()

        task = await mgr.submit("c1", "u1", slow)
        old_fence = task._execution_fence
        replacement = mgr._coordinator.reselect_execution(old_fence)
        hold.set()
        await _collect(mgr)

        assert task.status is TaskStatus.RUNNING
        mgr._coordinator.terminalize(
            replacement,
            state=OperationState.RETRYABLE,
            terminal_code="operation_failed",
            safe_summary="Retryable",
            retry_after_ms=None,
        )
        assert (await mgr.get(task.task_id)).status is TaskStatus.RETRYABLE

    @pytest.mark.asyncio
    async def test_reselected_cancel_never_uses_or_adopts_a_stale_fence(self):
        mgr = _manager()
        hold = asyncio.Event()

        async def slow(vws):
            await hold.wait()

        task = await mgr.submit("c1", "u1", slow)
        stale_fence = task._execution_fence
        replacement = mgr._coordinator.reselect_execution(stale_fence)

        assert await mgr.cancel(task.task_id) is True
        await _collect(mgr)
        assert task._execution_fence is None
        current = mgr._coordinator.assert_current_execution(replacement)
        assert current.cancel_requested_at is not None
        assert current.state is OperationState.RUNNING

        task._apply_operation(current, execution_fence=replacement)
        assert await mgr.cancel(task.task_id) is True
        assert task.status is TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_delayed_running_projection_cannot_overwrite_terminal_cache(self):
        mgr = _manager()

        async def done(vws):
            return None

        task = await mgr.submit("c1", "u1", done)
        delayed_running = task._operation
        running_fence = task._execution_fence
        assert running_fence is not None
        await _collect(mgr)
        terminal = task._operation
        assert terminal.state is OperationState.COMPLETED

        with pytest.raises(RuntimeError, match="backwards"):
            task._apply_operation(delayed_running)
        assert task._operation is terminal
        assert task._execution_fence is None

        forged_running = replace(
            delayed_running,
            state_revision=terminal.state_revision + 1,
        )
        with pytest.raises(RuntimeError, match="cannot be overwritten"):
            task._apply_operation(forged_running)
        assert task.status is TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_terminalization_failure_does_not_fake_success(self, monkeypatch):
        mgr = _manager()
        task = BackgroundTask("synthetic", "c1", "u1")
        vws = VirtualWebSocket(task)

        async def explode(*args, **kwargs):
            raise RuntimeError("database unavailable")

        async def dummy(vws):
            return None

        monkeypatch.setattr(mgr, "_terminalize", explode)
        await mgr._run_task(task, vws, dummy)
        assert task.status is TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_purge_delegates_retention_and_prunes_compatibility_cache(self):
        clock = _Clock()
        mgr = _manager(clock=clock)

        async def dummy(vws):
            return None

        task = await mgr.submit("c1", "u1", dummy)
        await _collect(mgr)
        assert await mgr.get(task.task_id) is task

        clock.current += timedelta(hours=25)
        result = await mgr.purge_expired()

        assert result.operations == 1
        assert await mgr.get(task.task_id) is None

    @pytest.mark.asyncio
    async def test_terminal_is_queryable_at_24_hours_and_purged_by_hour_25(self):
        clock = _Clock()
        mgr = _manager(clock=clock)

        async def dummy(vws):
            return None

        task = await mgr.submit("c1", "u1", dummy)
        await _collect(mgr)
        owner = task._owner
        operation_id = uuid.UUID(task.task_id)

        clock.current += timedelta(hours=24)
        at_retention = mgr._coordinator.query_operation(
            owner=owner,
            operation_id=operation_id,
        )
        assert at_retention.state is OperationState.COMPLETED
        assert (await mgr.purge_expired()).operations == 0
        assert await mgr.get(task.task_id) is task

        clock.current += timedelta(hours=1)
        assert (await mgr.purge_expired()).operations == 1
        assert await mgr.get(task.task_id) is None

    @pytest.mark.asyncio
    async def test_retention_sweep_refusal_and_missing_claim_fail_closed(
        self, monkeypatch
    ):
        refused = _maintenance_manager()

        class Refusal:
            accepted = False

        monkeypatch.setattr(refused._coordinator, "submit", lambda request: Refusal())
        assert await refused.run_retention_sweep_once() is None

        unclaimed = _maintenance_manager()
        monkeypatch.setattr(
            unclaimed._coordinator,
            "claim_operation",
            lambda class_name, operation_id: None,
        )
        assert await unclaimed.run_retention_sweep_once() is None
        status = unclaimed._coordinator.inspect_admission_class(
            AdmissionClass.MAINTENANCE
        )
        assert status.active_count == 0

    @pytest.mark.asyncio
    async def test_retention_sweep_failure_is_retryable_and_releases_capacity(
        self, monkeypatch
    ):
        mgr = _maintenance_manager()

        def failed_purge(*, limit, fence):
            raise RuntimeError("purge failed")

        monkeypatch.setattr(mgr._coordinator, "purge_expired", failed_purge)
        with pytest.raises(RuntimeError, match="purge failed"):
            await mgr.run_retention_sweep_once()

        status = mgr._coordinator.inspect_admission_class(
            AdmissionClass.MAINTENANCE
        )
        assert status.active_count == 0

    @pytest.mark.asyncio
    async def test_retention_sweep_cancellation_terminalizes_its_operation(
        self, monkeypatch
    ):
        mgr = _maintenance_manager()
        purge_started = threading.Event()
        purge_release = threading.Event()

        def blocking_purge(*, limit, fence):
            purge_started.set()
            purge_release.wait(timeout=2)
            return PurgeResult(operations=0, submissions=0)

        monkeypatch.setattr(mgr._coordinator, "purge_expired", blocking_purge)
        sweep = asyncio.create_task(mgr.run_retention_sweep_once())
        await asyncio.wait_for(asyncio.to_thread(purge_started.wait), timeout=1)
        sweep.cancel()
        purge_release.set()
        with pytest.raises(asyncio.CancelledError):
            await sweep

        status = mgr._coordinator.inspect_admission_class(
            AdmissionClass.MAINTENANCE
        )
        assert status.active_count == 0

    @pytest.mark.asyncio
    async def test_retention_loop_validates_bounds_is_singleton_and_stops_empty(
        self, monkeypatch
    ):
        mgr = BackgroundTaskManager(coordinator=object())
        with pytest.raises(ValueError, match="retention sweep bounds"):
            await mgr.run_retention_sweep_once(limit=0)
        with pytest.raises(ValueError, match="retention interval"):
            mgr.start_retention_sweep(interval_seconds=0)
        with pytest.raises(ValueError, match="retention retry"):
            mgr.start_retention_sweep(interval_seconds=1, retry_seconds=2)
        await mgr.stop_retention_sweep()

        started = asyncio.Event()
        release = asyncio.Event()

        async def sweep_once():
            started.set()
            await release.wait()
            return None

        monkeypatch.setattr(mgr, "run_retention_sweep_once", sweep_once)
        first = mgr.start_retention_sweep(interval_seconds=1, retry_seconds=0.1)
        second = mgr.start_retention_sweep(interval_seconds=1, retry_seconds=0.1)
        assert second is first
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await mgr.stop_retention_sweep()
        assert first.done()

    @pytest.mark.asyncio
    async def test_purged_entries_are_pruned_from_cancel_list_and_active_queries(self):
        clock = _Clock()
        mgr = _manager(clock=clock)

        async def dummy(vws):
            return None

        task = await mgr.submit("c1", "u1", dummy)
        await _collect(mgr)
        clock.current += timedelta(hours=25)
        mgr._coordinator.purge_expired()

        assert await mgr.cancel(task.task_id) is False
        assert await mgr.list_for_user("u1") == []
        assert await mgr.get_active_for_chat("c1") is None

    @pytest.mark.asyncio
    async def test_standalone_projection_helpers_are_fail_closed(self):
        mgr = _manager()
        synthetic = BackgroundTask("synthetic", "c1", "u1")
        mgr._tasks[synthetic.task_id] = synthetic

        assert await mgr._refresh(synthetic) is True
        assert await mgr.cancel(synthetic.task_id) is False
        assert (
            await mgr._terminalize(
                synthetic,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary="Completed",
            )
            is None
        )

        managed = await mgr.submit("c2", "u1", lambda vws: asyncio.sleep(0))
        other = await mgr.submit("c3", "u1", lambda vws: asyncio.sleep(0))
        with pytest.raises(RuntimeError, match="identity changed"):
            managed._apply_operation(other._operation)
        await _collect(mgr)

    def test_managed_background_fences_must_match_operation(self):
        mgr = _manager()
        request_manager = mgr._coordinator
        owner = OperationOwner(OwnerScope.USER, "u1", None)
        admitted = request_manager.submit(
            OperationRequest(
                operation_kind="async_chat",
                admission_class=AdmissionClass.BACKGROUND,
                owner=owner,
                submission_id=uuid.uuid4(),
                idempotency_namespace=None,
                idempotency_key=None,
                normalized_input_digest=None,
                chat_id="c1",
                parent_operation_id=None,
                connection_generation=None,
                request_generation=None,
            )
        )
        claim = request_manager.claim_next(AdmissionClass.BACKGROUND)
        assert claim is not None
        wrong_fence = replace(claim.fence, execution_lease_token=uuid.uuid4())
        wrong_operation_fence = replace(claim.fence, operation_id=uuid.uuid4())

        task = BackgroundTask(str(admitted.operation_id), "c1", "u1")
        with pytest.raises(RuntimeError, match="operation identity changed"):
            task._attach_authority(
                owner=owner,
                operation=claim.operation,
                execution_fence=wrong_operation_fence,
            )
        with pytest.raises(RuntimeError, match="fence is stale"):
            task._attach_authority(
                owner=owner,
                operation=claim.operation,
                execution_fence=wrong_fence,
            )


class TestBackgroundTaskShutdownAndObservability:
    @pytest.mark.asyncio
    async def test_runtime_observability_collector_is_wired_without_payload_labels(
        self,
    ):
        clock = _Clock()
        mgr = _manager(clock=clock)
        observability = RuntimeObservability(
            clock=clock,
            deployment_instance="test_instance",
        )
        mgr.bind(observability=observability)

        async def done(vws):
            return None

        task = await mgr.submit("secret-chat", "secret-user", done)
        await _collect(mgr)
        assert task.status is TaskStatus.COMPLETED
        await _wait_until(lambda: mgr._admission_observer_task is None)

        samples = observability.snapshot()
        assert {
            "operation_accepted_total",
            "operation_completed_total",
            "operation_terminal_total",
            "operation_active_count",
            "operation_queued_count",
        }.issubset({sample.name for sample in samples})
        assert all(
            "secret-chat" not in sample.labels.values()
            and "secret-user" not in sample.labels.values()
            for sample in samples
        )

    @pytest.mark.asyncio
    async def test_observability_is_additive_and_drain_timeout_is_bounded(self):
        mgr = _manager()
        observability = _RecordingObservability()
        mgr.bind(observability=observability)
        mgr.bind(observability=observability)
        with pytest.raises(RuntimeError, match="cannot replace"):
            mgr.bind(observability=_RecordingObservability())
        for invalid in (0, -1, float("inf"), float("nan"), True, "5"):
            with pytest.raises(ValueError, match="finite and positive"):
                await mgr.drain(timeout_seconds=invalid)

    @pytest.mark.asyncio
    async def test_observability_failure_never_changes_background_outcome(
        self, monkeypatch
    ):
        mgr = _manager()
        mgr.bind(observability=_FailingObservability())

        async def done(vws):
            return None

        task = await mgr.submit("c1", "u1", done, kind="background_chat")
        await _collect(mgr)
        assert task.status is TaskStatus.COMPLETED

        maintenance = _maintenance_manager()
        maintenance.bind(observability=_FailingObservability())
        monkeypatch.setattr(
            maintenance._coordinator,
            "purge_expired",
            lambda *, limit, fence: PurgeResult(operations=0, submissions=0),
        )
        assert await maintenance.run_retention_sweep_once() is not None

    @pytest.mark.asyncio
    async def test_service_drain_stops_retention_worker_without_work(self, monkeypatch):
        mgr = _manager()
        started = asyncio.Event()

        async def waiting_sweep():
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(mgr, "run_retention_sweep_once", waiting_sweep)
        retention = mgr.start_retention_sweep(
            interval_seconds=1,
            retry_seconds=0.1,
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        assert await mgr.drain(timeout_seconds=0.5) == 0
        assert retention.done()
        assert mgr._retention_task is None
        assert mgr._retention_stop is None

    @pytest.mark.parametrize(
        ("stalled_method", "stall_after_call"),
        (
            ("submit", False),
            ("query_operation", False),
            ("claim_operation", False),
            ("claim_operation", True),
        ),
    )
    @pytest.mark.asyncio
    async def test_service_drain_is_bounded_while_submit_coordinator_stalls(
        self, monkeypatch, stalled_method, stall_after_call
    ):
        mgr = _manager()
        submit_entered = threading.Event()
        submit_release = threading.Event()
        original_call = getattr(mgr._coordinator, stalled_method)

        def stalled_call(*args, **kwargs):
            result = original_call(*args, **kwargs) if stall_after_call else None
            submit_entered.set()
            if not submit_release.wait(timeout=2):
                raise RuntimeError("test coordinator call was not released")
            if not stall_after_call:
                result = original_call(*args, **kwargs)
            return result

        monkeypatch.setattr(mgr._coordinator, stalled_method, stalled_call)

        async def should_not_run(vws):
            raise AssertionError("draining work must not enter user code")

        submit = asyncio.create_task(
            mgr.submit("c1", "u1", should_not_run, kind="background_chat")
        )
        await asyncio.wait_for(
            asyncio.to_thread(submit_entered.wait),
            timeout=1,
        )

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        try:
            remainder = await asyncio.wait_for(
                mgr.drain(timeout_seconds=0.05),
                timeout=0.25,
            )
        finally:
            submit_release.set()

        assert remainder == 0
        assert loop.time() - started_at < 0.25
        task = await asyncio.wait_for(submit, timeout=1)
        assert task.status is TaskStatus.CANCELLED
        assert task._operation.terminal_code == "service_draining"
        assert task.asyncio_task is None
        status = mgr._coordinator.inspect_admission_class(
            AdmissionClass.BACKGROUND
        )
        assert status.active_count == 0
        assert status.queued_count == 0

    @pytest.mark.asyncio
    async def test_service_drain_permanently_refuses_retention_restart(self):
        mgr = _manager()
        assert await mgr.drain(timeout_seconds=0.05) == 0

        with pytest.raises(BackgroundTaskAdmissionError) as start_refusal:
            mgr.start_retention_sweep(
                interval_seconds=1,
                retry_seconds=0.1,
            )
        assert start_refusal.value.code == "service_draining"
        assert start_refusal.value.retryable is True
        assert start_refusal.value.retry_after_ms is not None

        with pytest.raises(BackgroundTaskAdmissionError) as run_refusal:
            await mgr.run_retention_sweep_once()
        assert run_refusal.value.code == "service_draining"
        assert mgr._retention_task is None
        assert mgr._retention_stop is None

    @pytest.mark.parametrize("blocked_stage", ("inspect", "collector"))
    @pytest.mark.asyncio
    async def test_admission_observation_is_detached_coalesced_and_drained(
        self, monkeypatch, blocked_stage
    ):
        mgr = _manager()
        observability = _RecordingObservability()
        observation_entered = threading.Event()
        observation_release = threading.Event()
        calls = []

        if blocked_stage == "inspect":
            original_inspect = mgr._coordinator.inspect_admission_class

            def blocked_inspect(class_name):
                calls.append("inspect")
                observation_entered.set()
                observation_release.wait(timeout=0.4)
                return original_inspect(class_name)

            monkeypatch.setattr(
                mgr._coordinator,
                "inspect_admission_class",
                blocked_inspect,
            )
        else:
            original_observe = observability.observe_admission

            def blocked_observe(status, *, operation_kind):
                calls.append("collector")
                observation_entered.set()
                observation_release.wait(timeout=0.4)
                original_observe(status, operation_kind=operation_kind)

            monkeypatch.setattr(
                observability,
                "observe_admission",
                blocked_observe,
            )
        mgr.bind(observability=observability)

        async def done(vws):
            return None

        try:
            task = await asyncio.wait_for(
                mgr.submit("c1", "u1", done, kind="background_chat"),
                timeout=0.15,
            )
            await asyncio.wait_for(
                asyncio.to_thread(observation_entered.wait),
                timeout=1,
            )
            for _ in range(20):
                await mgr._observe_admission()
            await _collect(mgr)
            assert task.status is TaskStatus.COMPLETED
            assert calls == [blocked_stage]

            assert await asyncio.wait_for(
                mgr.drain(timeout_seconds=0.05),
                timeout=0.2,
            ) == 0
            assert (
                mgr._admission_observer_task is None
                or mgr._admission_observer_task.done()
            )
        finally:
            observation_release.set()
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    @_REQUIRES_BG_CONTINUITY
    async def test_service_drain_cancels_projection_awaiters_while_worker_settles(
        self,
    ):
        mgr = _manager()
        write_started = threading.Event()
        write_release = threading.Event()

        class BlockingRepository(_RecordingBackgroundRepository):
            def apply_operation_projection(self, transaction, record):
                write_started.set()
                write_release.wait(timeout=2)
                return record

        _bind_plane(mgr, BlockingRepository())

        async def running(vws):
            await asyncio.Event().wait()

        try:
            await mgr.submit("c1", "u1", running, kind="background_chat")
            await asyncio.wait_for(
                asyncio.to_thread(write_started.wait),
                timeout=1,
            )

            assert await mgr.drain(timeout_seconds=0.2) == 0
            assert not mgr._compatibility_write_tasks
            snapshot = mgr._async_plane_runtime.snapshot()
            assert snapshot.active == 1
            assert snapshot.closed is True
        finally:
            write_release.set()
            await _wait_until(
                lambda: mgr._async_plane_runtime.snapshot().active == 0
            )

    @pytest.mark.asyncio
    async def test_background_lifecycle_records_safe_events_and_admission(self):
        mgr = _manager(active_limit=1, queue_limit=0)
        observability = _RecordingObservability()
        mgr.bind(observability=observability)
        release = asyncio.Event()

        async def slow(vws):
            await release.wait()

        task = await mgr.submit("c1", "u1", slow, kind="background_chat")
        with pytest.raises(BackgroundTaskAdmissionError) as caught:
            await mgr.submit("c2", "u1", slow, kind="background_chat")

        assert caught.value.code == "capacity_exceeded"
        assert await mgr.cancel(task.task_id) is True

        assert (
            "accepted",
            "background_chat",
            None,
            None,
        ) in observability.operation_events
        assert (
            "refused",
            "background_chat",
            "capacity_exceeded",
            None,
        ) in observability.operation_events
        assert (
            "cancelled",
            "background_chat",
            "cancelled_by_user",
            None,
        ) in observability.operation_events
        assert (
            "terminal",
            "background_chat",
            "cancelled",
            None,
        ) in observability.operation_events
        await _wait_until(
            lambda: observability.admission_statuses
            and observability.admission_statuses[-1][0].active_count == 0
        )
        assert observability.admission_statuses
        assert all(
            operation_kind == "background_chat"
            for _, operation_kind in observability.admission_statuses
        )
        assert observability.admission_statuses[-1][0].active_count == 0

    @pytest.mark.asyncio
    async def test_queue_expiry_records_retryable_terminal_without_user_code(self):
        clock = _Clock()
        mgr = _manager(
            active_limit=1,
            queue_limit=1,
            max_wait_ms=50,
            clock=clock,
            dispatch_poll_seconds=0.01,
        )
        observability = _RecordingObservability()
        mgr.bind(observability=observability)
        release = asyncio.Event()
        queued_called = False

        async def blocker(vws):
            await release.wait()

        async def queued(vws):
            nonlocal queued_called
            queued_called = True

        running = await mgr.submit("c1", "u1", blocker, kind="background_chat")
        expired = await mgr.submit("c2", "u1", queued, kind="background_chat")
        clock.current += timedelta(milliseconds=51)
        await _wait_until(lambda: expired.status is TaskStatus.RETRYABLE)

        assert queued_called is False
        assert (
            "queue_expired",
            "background_chat",
            "queue_wait_expired",
            None,
        ) in observability.operation_events
        assert (
            "retryable",
            "background_chat",
            "queue_wait_expired",
            None,
        ) in observability.operation_events
        assert (
            "terminal",
            "background_chat",
            "retryable",
            None,
        ) in observability.operation_events

        release.set()
        await _collect(mgr)
        if running.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            await mgr.cancel(running.task_id)

    @pytest.mark.asyncio
    async def test_service_drain_cancels_queued_and_running_with_shutdown_code(self):
        mgr = _manager(
            active_limit=1,
            queue_limit=1,
            dispatch_poll_seconds=0.01,
        )
        observability = _RecordingObservability()
        mgr.bind(observability=observability)
        started = asyncio.Event()
        queued_called = False

        async def running(vws):
            started.set()
            await vws.send_json({"type": "before_shutdown"})
            try:
                await asyncio.Event().wait()
            finally:
                await vws.send_json({"type": "late_after_shutdown"})

        async def queued(vws):
            nonlocal queued_called
            queued_called = True

        active = await mgr.submit("c1", "u1", running, kind="background_chat")
        pending = await mgr.submit("c2", "u1", queued, kind="background_chat")
        await asyncio.wait_for(started.wait(), timeout=1)

        remainder = await asyncio.wait_for(
            mgr.drain(timeout_seconds=0.5),
            timeout=1,
        )

        assert remainder == 0
        assert queued_called is False
        assert active.status is TaskStatus.CANCELLED
        assert pending.status is TaskStatus.CANCELLED
        assert active._operation.terminal_code == "service_draining"
        assert pending._operation.terminal_code == "service_draining"
        assert active.outputs == [{"type": "before_shutdown"}]
        assert active._execution_fence is None
        assert active._lease_task is None
        assert active._virtual_websocket is None
        assert mgr._dispatcher_task is None or mgr._dispatcher_task.done()
        status = mgr._coordinator.inspect_admission_class(
            AdmissionClass.BACKGROUND
        )
        assert status.active_count == 0
        assert status.queued_count == 0

        with pytest.raises(BackgroundTaskAdmissionError) as caught:
            await mgr.submit("c3", "u1", queued, kind="background_chat")
        assert caught.value.code == "service_draining"
        assert caught.value.retryable is True
        assert caught.value.retry_after_ms is not None
        assert (
            "refused",
            "background_chat",
            "service_draining",
            None,
        ) in observability.operation_events

    @pytest.mark.asyncio
    async def test_service_drain_force_fences_cancellation_resistant_worker(self):
        mgr = _manager(active_limit=1, queue_limit=0)
        release = asyncio.Event()
        started = asyncio.Event()

        async def cancellation_resistant(vws):
            started.set()
            await vws.send_json({"type": "before_shutdown"})
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await vws.send_json({"type": "late_after_shutdown"})

        task = await mgr.submit(
            "c1",
            "u1",
            cancellation_resistant,
            kind="background_chat",
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        started_at = asyncio.get_running_loop().time()
        remainder = await asyncio.wait_for(
            mgr.drain(timeout_seconds=0.1),
            timeout=0.5,
        )
        elapsed = asyncio.get_running_loop().time() - started_at

        assert remainder == 1
        assert elapsed < 0.5
        assert task.status is TaskStatus.CANCELLED
        assert task._operation.terminal_code == "service_draining"
        assert task._execution_fence is None
        assert task._virtual_websocket is not None
        assert task._virtual_websocket._closed is True
        assert task.outputs == [{"type": "before_shutdown"}]

        release.set()
        await asyncio.wait_for(task.asyncio_task, timeout=1)
        assert task.status is TaskStatus.CANCELLED
        assert task._operation.terminal_code == "service_draining"

    @pytest.mark.asyncio
    async def test_retention_sweep_records_purge_throughput_and_lag(
        self, monkeypatch
    ):
        mgr = _maintenance_manager()
        observability = _RecordingObservability()
        mgr.bind(observability=observability)
        monkeypatch.setattr(
            mgr._coordinator,
            "purge_expired",
            lambda *, limit, fence: PurgeResult(operations=2, submissions=3),
        )

        result = await mgr.run_retention_sweep_once()

        assert result is not None
        assert result.operations == 2
        assert result.submissions == 3
        assert observability.retention_observations == [(5, 0.0)]

    @pytest.mark.asyncio
    async def test_retention_lag_is_oldest_overdue_purge_age(self):
        clock = _Clock()
        mgr = _maintenance_manager(clock=clock)
        coordinator = mgr._coordinator
        owner = OperationOwner(OwnerScope.MAINTENANCE, None, None)
        admitted = coordinator.submit(
            OperationRequest(
                operation_kind="expired_retention_fixture",
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
        )
        assert admitted.accepted
        claim = coordinator.claim_operation(
            AdmissionClass.MAINTENANCE,
            admitted.operation_id,
        )
        assert claim is not None
        coordinator.terminalize(
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Expired retention fixture",
            retry_after_ms=None,
        )
        clock.current += timedelta(hours=24, seconds=17)
        observability = _RecordingObservability()
        mgr.bind(observability=observability)
        mgr._retention_lag_seconds = 999.0

        result = await mgr.run_retention_sweep_once()

        assert result is not None
        assert result.operations == 1
        assert result.submissions == 1
        assert observability.retention_observations == [(2, 17.0)]

    def test_retention_lag_includes_plane_background_projection_on_same_fence(self):
        clock = _Clock()
        mgr = _maintenance_manager(clock=clock)
        repository = _RecordingBackgroundRepository()
        repository.retained_at = clock.current - timedelta(
            hours=24,
            seconds=13,
        )
        _bind_plane(mgr, repository)
        claim = _claim_maintenance(mgr._coordinator)

        lag = mgr._oldest_retention_backlog_lag_seconds(claim.fence)

        assert lag == 13.0
        assert repository.transactions == [mgr._coordinator.repository]
        assert repository.cutoff_at == clock.current - timedelta(hours=24)

    @pytest.mark.parametrize(
        ("result", "limit", "error", "message"),
        (
            ([], 1, TypeError, "invalid result"),
            (("a", "b"), 1, RuntimeError, "exceeded"),
            (("a", "a"), 2, RuntimeError, "duplicate"),
            (("",), 1, RuntimeError, "invalid identity"),
        ),
    )
    def test_fenced_plane_retention_rejects_invalid_repository_results(
        self,
        result,
        limit,
        error,
        message,
    ):
        clock = _Clock()
        mgr = _maintenance_manager(clock=clock)

        class InvalidResultRepository(_RecordingBackgroundRepository):
            def purge_overdue_for_administration(
                self,
                transaction,
                *,
                cutoff_at,
                limit,
            ):
                self.transactions.append(transaction)
                self.cutoff_at = cutoff_at
                return result

        repository = InvalidResultRepository()
        _bind_plane(mgr, repository)
        claim = _claim_maintenance(mgr._coordinator)

        with pytest.raises(error, match=message):
            mgr._fenced_compatibility_cleanup(
                claim.fence,
                limit=limit,
            )

        assert repository.transactions == [mgr._coordinator.repository]
        assert repository.cutoff_at == clock.current - timedelta(hours=24)

    def test_fenced_plane_retention_accepts_empty_bounded_result(self):
        mgr = _maintenance_manager()
        repository = _RecordingBackgroundRepository()
        _bind_plane(mgr, repository)
        claim = _claim_maintenance(mgr._coordinator)

        assert (
            mgr._fenced_compatibility_cleanup(claim.fence, limit=3)
            == 0
        )
