"""Deep policy tests for Plane-owned durable streaming-blob purges."""

from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from astralplane import (
    PurgeAttemptResult,
    PurgeAttemptState,
    PurgeScheduleResult,
    PurgeTombstone,
)
from astralplane.errors import PlaneError
from astralplane.purge import storage_locator_sha256
from astralplane.repositories import RepositoryConflictError, RepositoryValidationError

from orchestrator.attachments.purge import (
    AttachmentPurgeCoordinator,
    AttachmentPurgeReadinessError,
    purge_coordinator_from_orchestrator,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _tombstone(*, owner_id: str, attachment_id: str, suffix: str = "") -> PurgeTombstone:
    key = attachment_id
    return PurgeTombstone(
        tombstone_id=f"purge-{attachment_id}{suffix}",
        owner_id=owner_id,
        object_kind="attachment",
        object_id=attachment_id,
        storage_key=key,
        storage_locator_sha256=storage_locator_sha256(owner_id=owner_id, key=key),
        requested_at=NOW,
        available_at=NOW,
    )


class _Runtime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.depth = 0
        self.commit_response_error: BaseException | None = None

    @contextmanager
    def transaction(self):
        self.events.append("transaction.enter")
        self.depth += 1
        try:
            yield object()
        finally:
            self.depth -= 1
            self.events.append("transaction.exit")
            if self.commit_response_error is not None:
                raise self.commit_response_error


class _SingleConnectionRuntime(_Runtime):
    """Tiny Plane-pool model that fails instead of hanging on lock inversion."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.connection_acquired = threading.Event()
        self._connection = threading.BoundedSemaphore(1)

    @contextmanager
    def transaction(self):
        if not self._connection.acquire(timeout=2):
            raise TimeoutError("single Plane connection remained unavailable")
        self.connection_acquired.set()
        self.events.append("transaction.enter")
        self.depth += 1
        try:
            yield object()
        finally:
            self.depth -= 1
            self.events.append("transaction.exit")
            self._connection.release()
            if self.commit_response_error is not None:
                raise self.commit_response_error


class _Repository:
    def __init__(self, runtime: _Runtime, events: list[str]) -> None:
        self.runtime = runtime
        self.events = events
        self.incomplete = False
        self.expired_schedules: tuple[PurgeScheduleResult, ...] = ()
        self.attachment_values: dict[str, object] | None = None
        self.owner_values: dict[str, object] | None = None
        self.abandon_values: dict[str, object] | None = None
        self.reconcile_limits: list[int] = []
        self.loaded_tombstone: PurgeTombstone | None = None
        self.load_values: dict[str, str] | None = None

    def load(self, _transaction, **values):
        assert self.runtime.depth == 1
        self.events.append("repository.load")
        self.load_values = values
        return self.loaded_tombstone

    def schedule_attachment_prefix(self, _transaction, **values):
        assert self.runtime.depth >= 1
        self.events.append("repository.schedule_attachment")
        self.attachment_values = values
        self.incomplete = True
        return PurgeScheduleResult(
            tombstone=_tombstone(
                owner_id=str(values["owner_id"]),
                attachment_id=str(values["attachment_id"]),
            ),
            tombstone_created=True,
            metadata_rows_soft_deleted=1,
        )

    def schedule_owner_namespace(self, _transaction, **values):
        assert self.runtime.depth >= 1
        self.events.append("repository.schedule_owner")
        self.owner_values = values
        self.incomplete = True
        owner_id = str(values["owner_id"])
        return PurgeScheduleResult(
            tombstone=_tombstone(
                owner_id=owner_id,
                attachment_id="owner-namespace",
            ),
            tombstone_created=True,
            metadata_rows_soft_deleted=0,
        )

    def abandon_pending_materialization(self, _transaction, **values):
        assert self.runtime.depth >= 1
        self.events.append("repository.abandon_pending")
        self.abandon_values = values
        self.incomplete = True
        return PurgeScheduleResult(
            tombstone=_tombstone(
                owner_id=str(values["owner_id"]),
                attachment_id=str(values["attachment_id"]),
            ),
            tombstone_created=True,
            metadata_rows_soft_deleted=1,
        )

    def schedule_expired_pending_materializations_for_administration(
        self,
        _transaction,
        *,
        limit,
    ):
        assert self.runtime.depth >= 1
        assert 1 <= limit <= 1000
        self.reconcile_limits.append(limit)
        self.events.append("repository.schedule_expired")
        if self.expired_schedules:
            self.incomplete = True
        return self.expired_schedules

    def has_incomplete_for_administration(self, _transaction) -> bool:
        assert self.runtime.depth == 1
        self.events.append("repository.has_incomplete")
        return self.incomplete


class _Executor:
    def __init__(
        self,
        runtime: _Runtime,
        repository: _Repository,
        events: list[str],
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.events = events
        self.execute_state = PurgeAttemptState.PURGED
        self.reconcile_results: tuple[PurgeAttemptResult, ...] = ()
        self.reconcile_incomplete = False
        self.reconciled = threading.Event()

    def execute(self, **values):
        assert self.runtime.depth == 0
        self.events.append("executor.execute")
        completed = self.execute_state is not PurgeAttemptState.FAILED
        self.repository.incomplete = not completed
        return PurgeAttemptResult(
            state=self.execute_state,
            tombstone_id=str(values["tombstone_id"]),
            attempt=1,
            error_code=None if completed else "blob_delete_failed",
        )

    def reconcile_ready_for_administration(self, **_values):
        assert self.runtime.depth == 0
        self.events.append("executor.reconcile")
        self.repository.incomplete = self.reconcile_incomplete
        self.reconciled.set()
        return self.reconcile_results


class _DrainingExecutor(_Executor):
    """Return a bounded prefix of durable ready work on each pass."""

    def __init__(self, runtime, repository, events, *, count: int) -> None:
        super().__init__(runtime, repository, events)
        self.remaining = [f"legacy-{index}" for index in range(count)]
        self.limits: list[int] = []
        self.drained = threading.Event()

    def reconcile_ready_for_administration(self, *, limit, **_values):
        assert self.runtime.depth == 0
        self.events.append("executor.reconcile")
        self.limits.append(limit)
        selected, self.remaining = self.remaining[:limit], self.remaining[limit:]
        self.repository.incomplete = bool(self.remaining)
        if not self.remaining:
            self.drained.set()
        return tuple(
            PurgeAttemptResult(
                state=PurgeAttemptState.PURGED,
                tombstone_id=tombstone_id,
                attempt=1,
                error_code=None,
            )
            for tombstone_id in selected
        )


def _coordinator():
    events: list[str] = []
    runtime = _Runtime(events)
    repository = _Repository(runtime, events)
    executor = _Executor(runtime, repository, events)
    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=runtime,
        purge_repository=repository,  # type: ignore[arg-type]
        blobs=object(),
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=10),
        reconcile_interval_seconds=1,
        reconcile_limit=7,
    )
    return coordinator, runtime, repository, executor, events


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"plane_runtime": None}, TypeError),
        ({"purge_repository": None}, TypeError),
        ({"blobs": None}, TypeError),
        ({"retry_delay": timedelta(0)}, ValueError),
        ({"reconcile_interval_seconds": 0}, ValueError),
        ({"reconcile_limit": True}, ValueError),
        ({"startup_reconcile_max_items": 0}, ValueError),
        ({"startup_reconcile_timeout_seconds": 0}, ValueError),
        ({"schedule_workers": 0}, ValueError),
    ],
)
def test_constructor_rejects_unsafe_recovery_configuration(overrides, error) -> None:
    coordinator, runtime, repository, executor, _events = _coordinator()
    del coordinator
    values = {
        "plane_runtime": runtime,
        "purge_repository": repository,
        "blobs": object(),
        "executor": executor,
        "clock": lambda: NOW,
        "retry_delay": timedelta(seconds=10),
        "reconcile_interval_seconds": 1,
        "reconcile_limit": 7,
        "startup_reconcile_max_items": 1000,
        "startup_reconcile_timeout_seconds": 30,
        "schedule_workers": 2,
    }
    values.update(overrides)

    with pytest.raises(error):
        AttachmentPurgeCoordinator(**values)


def test_attachment_intent_commits_before_physical_delete() -> None:
    coordinator, _runtime, repository, _executor, events = _coordinator()

    outcome = coordinator.schedule_attachment(
        owner_id="owner-1",
        attachment_id="attachment-1",
    )

    assert outcome.completed is True
    assert outcome.metadata_rows_soft_deleted == 1
    assert events == [
        "transaction.enter",
        "repository.schedule_attachment",
        "transaction.exit",
        "executor.execute",
        "transaction.enter",
        "repository.has_incomplete",
        "transaction.exit",
    ]
    assert repository.attachment_values == {
        "owner_id": "owner-1",
        "attachment_id": "attachment-1",
        "requested_at": NOW,
        "deleted_at": 1_786_708_800_000,
    }


def test_physical_failure_is_visible_until_bounded_reconciliation_proves_absence() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    executor.execute_state = PurgeAttemptState.FAILED

    outcome = coordinator.schedule_attachment(
        owner_id="owner-1",
        attachment_id="attachment-1",
    )

    assert outcome.completed is False
    assert coordinator.ready is False
    assert coordinator.readiness_code == "purge_reconciliation_incomplete"
    with pytest.raises(AttachmentPurgeReadinessError):
        coordinator.assert_ready()

    repository.incomplete = False
    executor.reconcile_incomplete = False
    assert coordinator.reconcile_once() == ()
    coordinator.assert_ready()


def test_failed_upload_abandonment_commits_before_physical_cleanup() -> None:
    coordinator, _runtime, repository, _executor, events = _coordinator()

    outcome = coordinator.abandon_pending_materialization(
        owner_id="owner-1",
        attachment_id="attachment-pending",
        lease_id="lease-1",
        expected_lease_version=3,
    )

    assert outcome.completed is True
    assert repository.abandon_values == {
        "owner_id": "owner-1",
        "attachment_id": "attachment-pending",
        "lease_id": "lease-1",
        "expected_lease_version": 3,
        "deleted_at": 1_786_708_800_000,
    }
    assert events == [
        "transaction.enter",
        "repository.abandon_pending",
        "transaction.exit",
        "executor.execute",
        "transaction.enter",
        "repository.has_incomplete",
        "transaction.exit",
    ]


@pytest.mark.asyncio
async def test_async_failed_upload_abandonment_is_schedule_only_and_wakes_recovery() -> None:
    coordinator, _runtime, repository, executor, events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    events.clear()
    coordinator._wake.clear()

    accepted = await coordinator.aabandon_pending_materialization(
        owner_id="owner-1",
        attachment_id="attachment-pending",
        lease_id="lease-1",
        expected_lease_version=3,
    )

    assert accepted.cleanup_id == "purge-attachment-pending"
    assert accepted.metadata_rows_soft_deleted == 1
    assert repository.abandon_values == {
        "owner_id": "owner-1",
        "attachment_id": "attachment-pending",
        "lease_id": "lease-1",
        "expected_lease_version": 3,
        "deleted_at": 1_786_708_800_000,
    }
    assert events == [
        "transaction.enter",
        "repository.abandon_pending",
        "transaction.exit",
    ]
    assert coordinator._wake.is_set()
    assert coordinator.ready is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_async_abandon_typed_rollback_restores_readiness_without_wake() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    coordinator._wake.clear()

    def _stale(_transaction, **_values):
        raise RepositoryConflictError("stale materialization lease")

    repository.abandon_pending_materialization = _stale
    with pytest.raises(RepositoryConflictError, match="stale materialization lease"):
        await coordinator.aabandon_pending_materialization(
            owner_id="owner-1",
            attachment_id="attachment-pending",
            lease_id="lease-stale",
            expected_lease_version=2,
        )

    coordinator.assert_ready()
    assert coordinator._wake.is_set() is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_async_abandon_commit_unknown_stays_red_and_wakes_recovery() -> None:
    coordinator, runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    coordinator._wake.clear()
    runtime.commit_response_error = ConnectionError("commit response lost")

    with pytest.raises(ConnectionError, match="commit response lost"):
        await coordinator.aabandon_pending_materialization(
            owner_id="owner-1",
            attachment_id="attachment-pending",
            lease_id="lease-1",
            expected_lease_version=3,
        )

    assert coordinator.ready is False
    assert coordinator._wake.is_set()
    runtime.commit_response_error = None
    await coordinator.close()


def test_expired_pending_intents_are_scheduled_before_purge_discovery() -> None:
    coordinator, _runtime, repository, executor, events = _coordinator()
    repository.expired_schedules = (
        PurgeScheduleResult(
            tombstone=_tombstone(
                owner_id="owner-expired",
                attachment_id="attachment-expired",
            ),
            tombstone_created=True,
            metadata_rows_soft_deleted=1,
        ),
    )
    executor.reconcile_incomplete = False

    assert coordinator.reconcile_once() == ()

    assert events == [
        "transaction.enter",
        "repository.schedule_expired",
        "transaction.exit",
        "executor.reconcile",
        "transaction.enter",
        "repository.has_incomplete",
        "transaction.exit",
    ]
    coordinator.assert_ready()


def test_executor_and_aggregate_failures_degrade_readiness() -> None:
    coordinator, runtime, repository, executor, _events = _coordinator()

    def _fail_execute(**_values):
        raise OSError("filesystem unavailable")

    executor.execute = _fail_execute
    with pytest.raises(OSError, match="filesystem unavailable"):
        coordinator.schedule_attachment(
            owner_id="owner-1",
            attachment_id="attachment-1",
        )
    assert coordinator.readiness_code == "purge_reconciliation_failed"

    def _fail_aggregate(_transaction):
        assert runtime.depth == 1
        raise RuntimeError("aggregate unavailable")

    repository.has_incomplete_for_administration = _fail_aggregate
    with pytest.raises(RuntimeError, match="aggregate unavailable"):
        coordinator.reconcile_once()
    assert coordinator.readiness_code == "purge_reconciliation_failed"


def test_commit_lost_response_cannot_leave_readiness_green() -> None:
    coordinator, runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    coordinator.assert_ready()

    runtime.commit_response_error = ConnectionError("commit response lost")
    with pytest.raises(ConnectionError, match="commit response lost"):
        coordinator.schedule_attachment(
            owner_id="owner-1",
            attachment_id="attachment-1",
        )

    assert repository.incomplete is True
    assert coordinator.ready is False
    assert coordinator.readiness_code == "purge_reconciliation_incomplete"


def test_missing_or_foreign_attachment_spam_cannot_pin_global_readiness_red() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()

    def _missing(_transaction, **_values):
        raise PlaneError("owner-scoped attachment not found", code="purge_object_not_found")

    repository.schedule_attachment_prefix = _missing
    for index in range(25):
        with pytest.raises(PlaneError) as raised:
            coordinator.schedule_attachment(
                owner_id="attacker",
                attachment_id=f"random-or-foreign-{index}",
            )
        assert raised.value.code == "purge_object_not_found"
        coordinator.assert_ready()

    assert coordinator.ready is True
    assert coordinator.readiness_code is None


@pytest.mark.asyncio
async def test_async_typed_rollback_does_not_thrash_reconciler_wake() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    coordinator._wake.clear()

    def _missing(_transaction, **_values):
        raise PlaneError("not found", code="purge_object_not_found")

    repository.schedule_attachment_prefix = _missing
    with pytest.raises(PlaneError) as raised:
        await coordinator.aschedule_attachment(
            owner_id="owner-1",
            attachment_id="missing",
        )
    assert raised.value.code == "purge_object_not_found"
    assert coordinator._wake.is_set() is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_async_commit_unknown_wakes_fail_closed_reconciliation() -> None:
    coordinator, runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    coordinator._wake.clear()
    runtime.commit_response_error = ConnectionError("commit response lost")

    with pytest.raises(ConnectionError, match="commit response lost"):
        await coordinator.aschedule_attachment(
            owner_id="owner-1",
            attachment_id="commit-unknown",
        )

    assert coordinator._wake.is_set()
    assert coordinator.ready is False
    runtime.commit_response_error = None
    await coordinator.close()


def test_clock_must_be_timezone_aware() -> None:
    coordinator, *_rest = _coordinator()
    coordinator._clock = lambda: datetime(2026, 8, 14, 12, 0)

    with pytest.raises(ValueError, match="aware datetime"):
        coordinator.reconcile_once()


def test_reconcile_rejects_invalid_override_and_can_fail_on_incomplete() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    with pytest.raises(ValueError, match="batch limit"):
        coordinator.reconcile_once(limit=8)

    repository.incomplete = True
    executor.reconcile_incomplete = True
    with pytest.raises(
        AttachmentPurgeReadinessError,
        match="purge_reconciliation_incomplete",
    ):
        coordinator.reconcile_once(fail_on_incomplete=True)


def test_startup_keeps_delayed_or_manual_work_unready_without_requiring_restart() -> None:
    coordinator, _runtime, repository, executor, events = _coordinator()
    repository.incomplete = True
    executor.reconcile_results = ()
    executor.reconcile_incomplete = True

    assert coordinator.reconcile_startup() == ()

    assert events[-3:] == [
        "transaction.enter",
        "repository.has_incomplete",
        "transaction.exit",
    ]
    assert coordinator.ready is False
    with pytest.raises(
        AttachmentPurgeReadinessError,
        match="purge_reconciliation_incomplete",
    ):
        coordinator.assert_ready()


def test_startup_drains_more_than_one_hundred_ready_legacy_tombstones() -> None:
    events: list[str] = []
    runtime = _Runtime(events)
    repository = _Repository(runtime, events)
    executor = _DrainingExecutor(runtime, repository, events, count=205)
    repository.incomplete = True
    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=runtime,
        purge_repository=repository,  # type: ignore[arg-type]
        blobs=object(),
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        reconcile_limit=100,
        startup_reconcile_max_items=1000,
        startup_reconcile_timeout_seconds=30,
    )

    results = coordinator.reconcile_startup()

    assert len(results) == 205
    assert executor.limits == [100, 100, 100]
    assert coordinator.ready is True


@pytest.mark.asyncio
async def test_over_budget_startup_stays_red_while_background_converges() -> None:
    events: list[str] = []
    runtime = _Runtime(events)
    repository = _Repository(runtime, events)
    executor = _DrainingExecutor(runtime, repository, events, count=205)
    repository.incomplete = True
    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=runtime,
        purge_repository=repository,  # type: ignore[arg-type]
        blobs=object(),
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        reconcile_interval_seconds=1,
        reconcile_limit=100,
        startup_reconcile_max_items=100,
        startup_reconcile_timeout_seconds=30,
    )

    assert len(coordinator.reconcile_startup()) == 100
    assert coordinator.ready is False
    coordinator._reconcile_interval_seconds = 0.01
    task = coordinator.start()
    await asyncio.wait_for(asyncio.to_thread(executor.drained.wait), timeout=2)
    for _ in range(100):
        if coordinator.ready:
            break
        await asyncio.sleep(0.01)
    await coordinator.stop()

    assert task.done()
    assert executor.limits == [100, 100, 100]
    assert coordinator.ready is True


def test_startup_time_budget_is_checked_between_bounded_batches() -> None:
    events: list[str] = []
    runtime = _Runtime(events)
    repository = _Repository(runtime, events)
    executor = _DrainingExecutor(runtime, repository, events, count=205)
    repository.incomplete = True
    monotonic_values = iter((0.0, 0.0, 2.0))
    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=runtime,
        purge_repository=repository,  # type: ignore[arg-type]
        blobs=object(),
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        reconcile_limit=100,
        startup_reconcile_max_items=1000,
        startup_reconcile_timeout_seconds=1,
        monotonic_clock=lambda: next(monotonic_values),
    )

    assert len(coordinator.reconcile_startup()) == 100
    assert executor.limits == [100]
    assert coordinator.ready is False


def test_startup_executor_exception_aborts_composition_and_stays_unready() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    repository.incomplete = True

    def _fail(**_values):
        raise OSError("purge storage unavailable")

    executor.reconcile_ready_for_administration = _fail
    with pytest.raises(OSError, match="storage unavailable"):
        coordinator.reconcile_startup()
    assert coordinator.ready is False
    assert coordinator.readiness_code == "purge_reconciliation_failed"


def test_owner_purge_is_an_unmounted_zero_metadata_service_boundary() -> None:
    coordinator, _runtime, repository, _executor, _events = _coordinator()

    outcome = coordinator.schedule_owner(owner_id="owner-with-orphan-blobs")

    assert outcome.completed is True
    assert outcome.metadata_rows_soft_deleted == 0
    assert repository.owner_values == {
        "owner_id": "owner-with-orphan-blobs",
        "requested_at": NOW,
        "deleted_at": 1_786_708_800_000,
    }


@pytest.mark.asyncio
async def test_reserved_owner_cleanup_acceptance_is_schedule_only() -> None:
    coordinator, _runtime, repository, executor, events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    events.clear()
    coordinator._wake.clear()

    accepted = await coordinator.aschedule_owner(owner_id="__verif__run_owner")

    assert accepted.cleanup_id == "purge-owner-namespace"
    assert accepted.metadata_rows_soft_deleted == 0
    assert events == [
        "transaction.enter",
        "repository.schedule_owner",
        "transaction.exit",
    ]
    assert "executor.execute" not in events
    assert coordinator._wake.is_set()
    assert coordinator.ready is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_owner_cleanup_status_is_owner_scoped_and_redacted() -> None:
    coordinator, _runtime, repository, _executor, events = _coordinator()
    repository.loaded_tombstone = _tombstone(
        owner_id="owner-1",
        attachment_id="owner-namespace",
    )

    observed = await coordinator.aowner_cleanup_status(
        owner_id="owner-1",
        cleanup_id="purge-owner-namespace",
    )

    assert observed is not None
    assert observed.cleanup_id == "purge-owner-namespace"
    assert observed.status == "pending"
    assert observed.attempt_count == 0
    assert repository.load_values == {
        "owner_id": "owner-1",
        "tombstone_id": "purge-owner-namespace",
    }
    assert events[-3:] == [
        "transaction.enter",
        "repository.load",
        "transaction.exit",
    ]
    await coordinator.close()


@pytest.mark.asyncio
async def test_owner_schedule_validation_rollback_restores_clean_readiness() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    coordinator._wake.clear()

    def _invalid(_transaction, **_values):
        raise RepositoryValidationError("owner is invalid")

    repository.schedule_owner_namespace = _invalid
    with pytest.raises(RepositoryValidationError, match="owner is invalid"):
        await coordinator.aschedule_owner(owner_id="invalid")

    coordinator.assert_ready()
    assert coordinator._wake.is_set() is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_periodic_reconciliation_uses_dedicated_lane_and_is_cancellable() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    executor.reconciled.clear()
    first = coordinator.start()
    second = coordinator.start()
    await asyncio.wait_for(asyncio.to_thread(executor.reconciled.wait), timeout=2)
    await coordinator.stop()
    await coordinator.stop()

    assert first is second
    assert first.done()
    assert coordinator.started is False

    executor.reconciled.clear()
    restarted = coordinator.start()
    await asyncio.wait_for(asyncio.to_thread(executor.reconciled.wait), timeout=2)
    await coordinator.close()
    assert restarted is not first
    assert restarted.done()


@pytest.mark.asyncio
async def test_many_delete_acceptances_cannot_starve_active_stage_default_worker() -> None:
    """Purge lock waiters never consume the executor needed by staged writes."""

    coordinator, _runtime, repository, executor, _events = _coordinator()
    reconcile_waiting_on_stage = threading.Event()
    release_stage = threading.Event()

    def _blocked_reconcile(**_values):
        reconcile_waiting_on_stage.set()
        assert release_stage.wait(timeout=5)
        return ()

    executor.reconcile_ready_for_administration = _blocked_reconcile
    coordinator.start()
    for _ in range(200):
        if reconcile_waiting_on_stage.is_set():
            break
        await asyncio.sleep(0.005)
    assert reconcile_waiting_on_stage.is_set()

    # Model the deliberately tiny default executor used by Plane's staged
    # writer.  The 32 delete schedules use the coordinator's dedicated lane,
    # so this worker remains available even while physical purge is blocked on
    # the active stage's owner lock.
    loop = asyncio.get_running_loop()
    upload_progress = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as default_pool:
        upload_write = loop.run_in_executor(default_pool, upload_progress.set)
        accepted = await asyncio.gather(
            *(
                coordinator.aschedule_attachment(
                    owner_id="owner-1",
                    attachment_id=f"attachment-{index}",
                )
                for index in range(32)
            )
        )
        await asyncio.wait_for(upload_write, timeout=1)

    assert upload_progress.is_set()
    assert len({item.cleanup_id for item in accepted}) == 32
    assert repository.incomplete is True
    assert "executor.execute" not in executor.events
    release_stage.set()
    await coordinator.close()


@pytest.mark.asyncio
async def test_reconcile_wake_during_active_pass_is_not_lost() -> None:
    coordinator, _runtime, repository, executor, _events = _coordinator()
    repository.incomplete = True
    calls = 0
    second_pass = threading.Event()
    loop = asyncio.get_running_loop()

    def _wake_during_first_pass(**_values):
        nonlocal calls
        calls += 1
        if calls == 1:
            loop.call_soon_threadsafe(coordinator._wake.set)
        else:
            second_pass.set()
        return ()

    executor.reconcile_ready_for_administration = _wake_during_first_pass
    coordinator.start()
    for _ in range(200):
        if second_pass.is_set():
            break
        await asyncio.sleep(0.005)
    await coordinator.close()

    assert second_pass.is_set()
    assert calls >= 2


@pytest.mark.asyncio
async def test_close_rejects_new_work_and_joins_an_admitted_schedule() -> None:
    coordinator, _runtime, repository, _executor, _events = _coordinator()
    entered = threading.Event()
    release = threading.Event()
    original = repository.schedule_attachment_prefix

    def _blocked_schedule(transaction, **values):
        entered.set()
        assert release.wait(timeout=5)
        return original(transaction, **values)

    repository.schedule_attachment_prefix = _blocked_schedule
    admitted = asyncio.create_task(
        coordinator.aschedule_attachment(
            owner_id="owner-1",
            attachment_id="attachment-admitted",
        )
    )
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert entered.is_set()

    closing = asyncio.create_task(coordinator.close())
    for _ in range(200):
        if coordinator._lifecycle_state == "closing":
            break
        await asyncio.sleep(0.005)
    assert coordinator._lifecycle_state == "closing"
    with pytest.raises(RuntimeError, match="closing"):
        await coordinator.aschedule_attachment(
            owner_id="owner-1",
            attachment_id="attachment-too-late",
        )
    assert closing.done() is False

    release.set()
    accepted = await admitted
    await closing

    assert accepted.cleanup_id == "purge-attachment-admitted"
    assert coordinator._lifecycle_state == "closed"
    with pytest.raises(RuntimeError, match="closing"):
        await coordinator.aschedule_owner(owner_id="after-close")
    with pytest.raises(RuntimeError, match="closing"):
        await coordinator.aabandon_pending_materialization(
            owner_id="owner-1",
            attachment_id="after-close",
            lease_id="lease-1",
            expected_lease_version=0,
        )
    for operation in (
        lambda: coordinator.schedule_attachment(
            owner_id="owner-1", attachment_id="after-close"
        ),
        lambda: coordinator.schedule_owner(owner_id="owner-1"),
        lambda: coordinator.abandon_pending_materialization(
            owner_id="owner-1",
            attachment_id="after-close",
            lease_id="lease-1",
            expected_lease_version=0,
        ),
        coordinator.reconcile_once,
    ):
        with pytest.raises(RuntimeError, match="closing"):
            operation()


@pytest.mark.asyncio
async def test_repeatedly_cancelled_close_joins_one_shared_close_task() -> None:
    coordinator, _runtime, repository, _executor, _events = _coordinator()
    entered = threading.Event()
    release = threading.Event()
    original = repository.schedule_attachment_prefix

    def _blocked_schedule(transaction, **values):
        entered.set()
        assert release.wait(timeout=5)
        return original(transaction, **values)

    repository.schedule_attachment_prefix = _blocked_schedule
    admitted = asyncio.create_task(
        coordinator.aschedule_attachment(
            owner_id="owner-1",
            attachment_id="attachment-admitted",
        )
    )
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert entered.is_set()

    first_caller = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    shared = coordinator._close_task
    assert shared is not None
    first_caller.cancel()
    await asyncio.sleep(0)
    first_caller.cancel()
    second_caller = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    assert coordinator._close_task is shared
    assert second_caller.done() is False

    release.set()
    await admitted
    with pytest.raises(asyncio.CancelledError):
        await first_caller
    await second_caller
    await coordinator.close()

    assert shared.done()
    assert coordinator._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_abort_and_close_are_idempotent_and_reject_restart() -> None:
    coordinator, *_rest = _coordinator()
    coordinator.abort()
    coordinator.abort()
    await coordinator.close()

    with pytest.raises(RuntimeError, match="closing"):
        coordinator.start()


def test_abort_rejects_started_or_active_coordinator() -> None:
    coordinator, *_rest = _coordinator()
    coordinator._task = SimpleNamespace()
    with pytest.raises(RuntimeError, match="requires async close"):
        coordinator.abort()
    coordinator._task = None
    coordinator._begin_operation()
    try:
        with pytest.raises(RuntimeError, match="active attachment purge"):
            coordinator.abort()
    finally:
        coordinator._end_operation()
        coordinator.abort()


def test_orchestrator_resolution_requires_the_application_composition() -> None:
    coordinator, *_rest = _coordinator()
    assert (
        purge_coordinator_from_orchestrator(
            SimpleNamespace(attachment_purge_coordinator=coordinator)
        )
        is coordinator
    )
    assert (
        purge_coordinator_from_orchestrator(
            SimpleNamespace(
                runtime_composition=SimpleNamespace(
                    plane=SimpleNamespace(attachment_purges=coordinator)
                )
            )
        )
        is coordinator
    )
    with pytest.raises(RuntimeError, match="not initialized"):
        purge_coordinator_from_orchestrator(object())


def test_stale_global_absence_proof_cannot_override_newly_scheduled_work() -> None:
    coordinator, _runtime, _repository, _executor, _events = _coordinator()
    initial_epoch = coordinator._readiness_epoch()

    outcome = coordinator.schedule_attachment(
        owner_id="owner-1",
        attachment_id="attachment-1",
    )
    assert outcome.completed is True
    coordinator._mark_incomplete("newly_committed_tombstone")

    coordinator._set_ready_if_unchanged(initial_epoch)

    assert coordinator.ready is False
    assert coordinator.readiness_code == "newly_committed_tombstone"


def test_live_probe_detects_tombstone_committed_by_another_coordinator() -> None:
    first, runtime, repository, first_executor, _events = _coordinator()
    second_executor = _Executor(runtime, repository, runtime.events)
    second = AttachmentPurgeCoordinator(
        plane_runtime=runtime,
        purge_repository=repository,  # type: ignore[arg-type]
        blobs=object(),
        executor=second_executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=10),
        reconcile_interval_seconds=1,
        reconcile_limit=7,
    )
    repository.incomplete = False
    first_executor.reconcile_incomplete = False
    second_executor.reconcile_incomplete = False
    first.reconcile_startup()
    second.reconcile_startup()
    first.assert_ready()

    second_executor.execute_state = PurgeAttemptState.FAILED
    outcome = second.schedule_attachment(
        owner_id="owner-1",
        attachment_id="attachment-1",
    )
    assert outcome.completed is False
    first.assert_ready()  # Process-local cache alone is deliberately stale.

    with runtime.transaction() as transaction:
        with pytest.raises(
            AttachmentPurgeReadinessError,
            match="purge_reconciliation_incomplete",
        ):
            first.assert_globally_ready(transaction)
    assert first.ready is False


def test_live_probe_rechecks_epoch_after_a_concurrent_schedule() -> None:
    coordinator, runtime, repository, executor, _events = _coordinator()
    repository.incomplete = False
    executor.reconcile_incomplete = False
    coordinator.reconcile_startup()
    probe_started = threading.Event()
    release_probe = threading.Event()

    def _stale_aggregate(_transaction):
        assert runtime.depth >= 1
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return False

    repository.has_incomplete_for_administration = _stale_aggregate

    def _failed_execute(**values):
        return PurgeAttemptResult(
            state=PurgeAttemptState.FAILED,
            tombstone_id=str(values["tombstone_id"]),
            attempt=1,
            error_code="blob_delete_failed",
        )

    executor.execute = _failed_execute

    def _probe() -> None:
        with runtime.transaction() as transaction:
            coordinator.assert_globally_ready(transaction)

    with ThreadPoolExecutor(max_workers=2) as workers:
        probe = workers.submit(_probe)
        assert probe_started.wait(timeout=2)
        scheduled = workers.submit(
            coordinator.schedule_attachment,
            owner_id="owner-1",
            attachment_id="attachment-1",
        )
        assert scheduled.result(timeout=2).completed is False
        release_probe.set()
        with pytest.raises(
            AttachmentPurgeReadinessError,
            match="purge_reconciliation_incomplete",
        ):
            probe.result(timeout=2)


def test_live_probe_and_schedule_do_not_invert_state_lock_and_tiny_pool() -> None:
    events: list[str] = []
    runtime = _SingleConnectionRuntime(events)
    repository = _Repository(runtime, events)
    executor = _Executor(runtime, repository, events)
    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=runtime,
        purge_repository=repository,  # type: ignore[arg-type]
        blobs=object(),
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=10),
        reconcile_interval_seconds=1,
        reconcile_limit=7,
    )
    repository.incomplete = False
    coordinator.reconcile_startup()
    coordinator.assert_ready()
    runtime.connection_acquired.clear()

    readiness_degraded = threading.Event()
    original_mark = coordinator._mark_incomplete_locked

    def _mark_and_signal(code: str) -> None:
        original_mark(code)
        readiness_degraded.set()

    coordinator._mark_incomplete_locked = _mark_and_signal  # type: ignore[method-assign]

    def _probe_with_only_connection() -> None:
        with runtime.transaction() as transaction:
            assert readiness_degraded.wait(timeout=2)
            with pytest.raises(
                AttachmentPurgeReadinessError,
                match="purge_reconciliation_incomplete",
            ):
                coordinator.assert_globally_ready(transaction)

    with ThreadPoolExecutor(max_workers=2) as workers:
        probe = workers.submit(_probe_with_only_connection)
        assert runtime.connection_acquired.wait(timeout=2)
        scheduled = workers.submit(
            coordinator.schedule_attachment,
            owner_id="owner-1",
            attachment_id="attachment-1",
        )
        probe.result(timeout=3)
        assert scheduled.result(timeout=3).completed is True

    coordinator.assert_ready()


def test_user_visible_delete_paths_cannot_bypass_the_durable_tombstone() -> None:
    from orchestrator.attachments import account_lifecycle, router
    from orchestrator.projection_surfaces import attachments as surface

    rest_delete = inspect.getsource(router.delete_attachment)
    chrome_delete = inspect.getsource(surface._schedule_delete)
    owner_delete = inspect.getsource(account_lifecycle.purge_user_attachments)

    assert "aschedule_attachment" in rest_delete
    assert "_delete_blob_prefix_verified" not in rest_delete
    assert "asoft_delete" not in rest_delete
    assert "aschedule_attachment" in chrome_delete
    assert "delete_prefix" not in chrome_delete
    assert "soft_delete" not in chrome_delete
    assert "schedule_owner" in owner_delete
    assert "delete_owner" not in owner_delete
    assert "soft_delete_all" not in owner_delete


def test_only_central_materializer_can_publish_or_mutate_attachment_blobs() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    central = backend_root / "orchestrator" / "attachments" / "materialization.py"
    purge = backend_root / "orchestrator" / "attachments" / "purge.py"
    legacy_store = backend_root / "orchestrator" / "attachments" / "store.py"
    assert central.is_file()
    assert not legacy_store.exists()

    publication_symbols = (
        "begin_pending_materialization(",
        "abegin_pending_materialization(",
        "open_pending_materialization_staging(",
        "aopen_pending_materialization_staging(",
        "publish_pending_materialization(",
        "apublish_pending_materialization(",
    )
    abandonment_symbols = (
        "abandon_pending_materialization(",
        "aabandon_pending_materialization(",
    )
    raw_blob_mutations = (
        ".write_chunks(",
        ".awrite_chunks(",
        ".delete_key(",
        ".delete_prefix(",
        ".delete_owner(",
        ".relocate(",
        "os.replace(",
        "shutil.move(",
    )
    violations: list[str] = []
    for path in backend_root.rglob("*.py"):
        if "tests" in path.parts or path == central:
            continue
        source = path.read_text(encoding="utf-8")
        for symbol in publication_symbols:
            if symbol in source:
                violations.append(f"{path.relative_to(backend_root)}:{symbol}")
        if path not in {central, purge}:
            for symbol in abandonment_symbols:
                if symbol in source:
                    violations.append(f"{path.relative_to(backend_root)}:{symbol}")
        attachment_coupled = (
            "attachments" in path.parts
            or path.name.startswith("attachment_")
            or path.name == "in_process.py"
        )
        if attachment_coupled:
            for symbol in raw_blob_mutations:
                if symbol in source:
                    violations.append(f"{path.relative_to(backend_root)}:{symbol}")

    assert violations == []
