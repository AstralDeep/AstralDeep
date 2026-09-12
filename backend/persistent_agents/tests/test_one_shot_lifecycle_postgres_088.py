"""Unregistered lifecycle contracts on actual Plane, session/JWT and admission.

These no-output completion fixtures qualify lifecycle mechanics only. They do not
claim a completed research answer or exercise a production ingress/runner.
External IAM replies are synthetic; every row belongs to a disposable schema.
"""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import AssignmentEpisodeCompletion
from orchestrator.work_admission import (
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from persistent_agents.config import RunnerConfig
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import ActionExecutor
from persistent_agents.models import AssignmentError
from persistent_agents.runner import (
    AssignmentRunner,
    OneShotEpisodeResult,
    OneShotLifecycle,
    _episode_lease,
)
from persistent_agents.runtime_values import digest
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.test_operation_session_authority_088 import create_operation
from tests.test_work_admission_repository import _classes
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import signing_key as signing_key
from tests.helpers.session_plane_runtime import (
    get_session_record,
    replace_session_record,
)


# This fixture supplies the real reader, normal governed dispatch, real audit and
# session/JWT stores. Only external IAM/delegation and reader transport are fake.
from persistent_agents.tests.test_operation_reader_postgres_088 import (
    operation as operation,
    REQUEST,
)
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator


runtime = plane


@pytest.fixture
async def lifecycle(runtime, fixture):
    record = await asyncio.to_thread(create_operation, fixture, runtime)
    store = AssignmentStore(plane_runtime=runtime)
    coordinator = await asyncio.to_thread(
        WorkAdmissionCoordinator.from_plane,
        plane_runtime=runtime,
        slot_lease=timedelta(seconds=21),
    )
    host = SimpleNamespace(
        work_admission=coordinator, _unbind_machine_turn=lambda _: None
    )
    service = AssignmentService(host, store, enabled=True, phi_gate=SimpleNamespace())
    handler = AsyncMock(
        side_effect=AssertionError("no handler is selected in repository fixtures")
    )
    runner = AssignmentRunner(
        host,
        service,
        config=RunnerConfig(concurrency=2, lease_seconds=30),
        one_shot=OneShotLifecycle(fixture[0], handler),
    )
    authority = await runner._operation_authority(record)
    claim = await store.operation_lifecycle_transaction(
        authority=authority,
        callback=lambda tx, repo, current: repo.claim_operation_for_administration(
            tx,
            owner_id=current.owner_id,
            assignment_id=current.assignment_id,
            expected_state_version=current.state_version,
            worker_id=runner.worker_id,
            lease_seconds=30,
            authority=authority.observation,
        ),
    )
    operation_fence = await runner._admit(claim)
    # Base executor is used only as a typed fence holder before reader integration.
    executor = ActionExecutor(runner, claim, operation_fence, object())
    authority = await runner._operation_authority(record)

    def bind(tx, repo, current):
        repo.bind_operation(tx, fence=claim.fence, binding=executor.binding)
        return repo.assert_current_assignment_execution(
            tx,
            fence=claim.fence,
            binding=executor.binding,
            authority=authority.observation,
        )

    await store.operation_lifecycle_transaction(
        authority=authority, fence=claim.fence, callback=bind
    )
    value = SimpleNamespace(
        runtime=runtime,
        fixture=fixture,
        store=store,
        runner=runner,
        executor=executor,
        record=record,
        coordinator=coordinator,
        handler=handler,
    )
    try:
        yield value
    finally:
        await runner.stop()
        store.close()


async def current(op):
    return (
        await op.store.call(
            "get_operation",
            owner_id=op.record.owner_id,
            assignment_id=op.record.assignment_id,
        )
    ).assignment


async def authority(op):
    return await op.runner._operation_authority(op.record)


def completion(record, **changes):
    values = dict(
        expected_state_version=record.state_version,
        checkpoint=record.checkpoint,
        completion_digest=digest(["repository-contract", str(uuid4())]),
        phase="waiting",
        wake_reason="explicit_yield",
        next_wake_at=datetime.now(UTC) + timedelta(seconds=20),
    )
    values.update(changes)
    return AssignmentEpisodeCompletion(**values)


async def admission(op):
    return await asyncio.to_thread(
        op.coordinator.query_operation,
        owner=OperationOwner(OwnerScope.USER, op.record.owner_id, None),
        operation_id=op.executor.operation_fence.operation_id,
    )


@pytest.mark.parametrize(
    "kind", ["yield", "failure", "terminal_failure", "terminal_empty", "event"]
)
async def test_explicit_completion_has_no_cadence_and_retires_both_leases(
    lifecycle, kind
):
    op = lifecycle
    before = await current(op)
    changes = {
        "yield": {},
        "failure": {
            "phase": "failed",
            "wake_reason": "assignment_failed",
            "next_wake_at": None,
            "safe_error_code": "assignment_failed",
        },
        "terminal_failure": {
            "completed": True,
            "terminal_outcome": "failed",
            "next_wake_at": None,
        },
        "terminal_empty": {"completed": True, "next_wake_at": None},
        "event": {
            "phase": "awaiting_event",
            "next_wake_at": None,
            "event_wait": {"event_key": "public_update", "source_revision": 1},
        },
    }[kind]
    result = await op.runner._finish_operation(
        op.executor, OneShotEpisodeResult(before, completion(before, **changes))
    )
    assert _episode_lease(op.executor).terminal
    assert (await admission(op)).state == OperationState.COMPLETED
    assert result.checkpoint == before.checkpoint and result.tasks == before.tasks
    if kind.startswith("terminal"):
        assert result.lifecycle == "completed" and result.next_wake_at is None
    elif kind == "failure":
        assert 3 <= (result.next_wake_at - datetime.now(UTC)).total_seconds() <= 5
    elif kind == "event":
        assert result.phase == "awaiting_event" and result.next_wake_at is None
    else:
        assert result.lifecycle == "active" and result.next_wake_at > datetime.now(UTC)
    with pytest.raises(AssignmentError):
        await op.store.call("assert_current_claim", fence=op.executor.claim.fence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "another-owner"),
        ("assignment_id", str(uuid4())),
        ("execution_profile", "persistent"),
        ("instruction_revision", 9),
        ("control_epoch", 9),
        ("checkpoint", {"stale": True}),
        ("phase", "failed"),
        ("wake_generation", 999),
        ("tasks", ({"id": "foreign"},)),
    ],
)
async def test_returned_meaningfully_stale_snapshot_cannot_publish(
    lifecycle, field, value
):
    op = lifecycle
    before = await current(op)
    altered = replace(before, **{field: value})
    with pytest.raises((DispatchDenied, AssignmentError)):
        await op.runner._finish_operation(
            op.executor, OneShotEpisodeResult(altered, completion(altered))
        )
    assert await current(op) == before
    assert (await admission(op)).state == OperationState.RUNNING
    assert not _episode_lease(op.executor).terminal


@pytest.mark.parametrize(
    "changes",
    [
        {"wake_reason": "cadence"},
        {"next_wake_at": None},
        {"expected_state_version": -1},
        {"completed": True, "terminal_outcome": "invented"},
        {"phase": "awaiting_event", "event_wait": None, "next_wake_at": None},
    ],
)
async def test_malformed_completion_leaves_both_fences_current(lifecycle, changes):
    op = lifecycle
    before = await current(op)
    with pytest.raises((DispatchDenied, AssignmentError)):
        await op.runner._finish_operation(
            op.executor, OneShotEpisodeResult(before, completion(before, **changes))
        )
    assert await current(op) == before
    assert (await admission(op)).state == OperationState.RUNNING


async def test_same_sid_replacement_blocks_terminalization_before_refresh(lifecycle):
    op = lifecycle
    before = await current(op)
    calls = len(op.fixture[-1])
    session = await asyncio.to_thread(get_session_record, op.runtime, op.fixture[2])
    await asyncio.to_thread(replace_session_record, op.runtime, session)
    with pytest.raises(Exception, match="session_authority_unavailable"):
        await op.runner._finish_operation(
            op.executor,
            OneShotEpisodeResult(
                before, completion(before, completed=True, next_wake_at=None)
            ),
        )
    assert len(op.fixture[-1]) == calls
    assert await current(op) == before
    assert (await admission(op)).state == OperationState.RUNNING


async def test_expiry_during_terminal_write_rolls_back_assignment_and_admission(
    lifecycle, monkeypatch
):
    op = lifecycle
    observed = await authority(op)
    observed = replace(
        observed,
        observation=replace(
            observed.observation,
            valid_until=datetime.now(UTC) + timedelta(milliseconds=90),
        ),
    )
    monkeypatch.setattr(
        op.runner, "_operation_authority", AsyncMock(return_value=observed)
    )
    original = op.coordinator.terminalize

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.12)
        return result

    monkeypatch.setattr(op.coordinator, "terminalize", slow)
    before = await current(op)
    with pytest.raises(AssignmentError):
        await op.runner._finish_operation(
            op.executor,
            OneShotEpisodeResult(
                before, completion(before, completed=True, next_wake_at=None)
            ),
        )
    assert await current(op) == before
    assert (await admission(op)).state == OperationState.RUNNING
    assert not _episode_lease(op.executor).terminal


async def test_renewal_transaction_uses_configured_admission_duration_and_rolls_back(
    lifecycle,
):
    op = lifecycle
    observed = await authority(op)
    before = await current(op)
    renewals = []

    def renew(tx, repo, current):
        repo.renew_claim(tx, fence=op.executor.claim.fence, lease_seconds=30)
        renewals.append(
            op.coordinator.renew_execution_lease(
                op.executor.operation_fence, transaction=tx
            )
        )
        raise RuntimeError("synthetic rollback")

    with pytest.raises(AssignmentError):
        await op.store.operation_lifecycle_transaction(
            authority=observed,
            fence=op.executor.claim.fence,
            binding=op.executor.binding,
            callback=renew,
        )
    assert (
        19 <= (renewals[0].lease_expires_at - datetime.now(UTC)).total_seconds() <= 21
    )
    assert await current(op) == before
    # A successful later call uses the same pool transaction and both exact fences.
    result = await op.store.operation_lifecycle_transaction(
        authority=observed,
        fence=op.executor.claim.fence,
        binding=op.executor.binding,
        callback=lambda tx, repo, current: op.coordinator.renew_execution_lease(
            op.executor.operation_fence, transaction=tx
        ),
    )
    assert result.operation_id == op.executor.operation_fence.operation_id


async def test_renewal_version_change_preserves_checkpoint_snapshot(lifecycle):
    op = lifecycle
    before = await current(op)
    observed = await authority(op)
    await op.store.operation_lifecycle_transaction(
        authority=observed,
        fence=op.executor.claim.fence,
        binding=op.executor.binding,
        callback=lambda tx, repo, current: repo.renew_claim(
            tx, fence=op.executor.claim.fence, lease_seconds=30
        ),
    )
    assert (await current(op)).state_version > before.state_version
    result = await op.runner._finish_operation(
        op.executor, OneShotEpisodeResult(before, completion(before))
    )
    assert result.checkpoint == before.checkpoint and result.usage == before.usage


async def test_async_lifecycle_callback_is_refused_without_committing(lifecycle):
    op = lifecycle
    observed = await authority(op)
    before = await current(op)

    async def wrong(*args):
        raise AssertionError("must not execute")

    with pytest.raises(
        AssignmentError, match="assignment_transaction_callback_invalid"
    ):
        await op.store.operation_lifecycle_transaction(
            authority=observed,
            fence=op.executor.claim.fence,
            binding=op.executor.binding,
            callback=wrong,
        )
    assert await current(op) == before


@pytest.mark.parametrize("change", ["runtime", "untyped", "binding_only"])
async def test_lifecycle_rejects_wrong_authority_contract(lifecycle, change):
    op = lifecycle
    observed = await authority(op)
    kwargs = {
        "authority": observed,
        "callback": lambda *_: pytest.fail("invalid callback ran"),
    }
    if change == "runtime":
        kwargs["authority"] = replace(observed, plane_runtime=object())
    elif change == "untyped":
        kwargs["authority"] = object()
    else:
        kwargs["binding"] = op.executor.binding
    with pytest.raises(AssignmentError):
        await op.store.operation_lifecycle_transaction(**kwargs)


def test_in_memory_renewal_refuses_foreign_transaction():
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(), repository=InMemoryWorkAdmissionRepository()
    )
    with pytest.raises(ValueError, match="external transaction"):
        coordinator.renew_execution_lease(object(), transaction=object())


async def test_terminal_acknowledgement_prevents_renewal_cancelling_completed_episode(
    lifecycle, monkeypatch
):
    op = lifecycle
    before = await current(op)
    lease = _episode_lease(op.executor)
    lease.terminal = True
    monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", AsyncMock())
    episode = asyncio.create_task(asyncio.Event().wait())
    try:
        await op.runner._renew_operation(op.executor, episode)
        assert not episode.cancelled() and not episode.done()
        assert await current(op) == before
    finally:
        episode.cancel()
        await asyncio.gather(episode, return_exceptions=True)


async def test_renewal_lost_session_cancels_episode_without_renewing(
    lifecycle, monkeypatch
):
    op = lifecycle
    before = await current(op)
    session = await asyncio.to_thread(get_session_record, op.runtime, op.fixture[2])
    await asyncio.to_thread(replace_session_record, op.runtime, session)
    monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", AsyncMock())
    episode = asyncio.create_task(asyncio.Event().wait())
    await op.runner._renew_operation(op.executor, episode)
    with pytest.raises(asyncio.CancelledError):
        await episode
    assert await current(op) == before


async def test_renewal_cancellation_propagates_without_cancelling_work(lifecycle):
    op = lifecycle
    episode = asyncio.create_task(asyncio.Event().wait())
    renewal = asyncio.create_task(op.runner._renew_operation(op.executor, episode))
    await asyncio.sleep(0)
    renewal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await renewal
    assert not episode.done()
    episode.cancel()
    await asyncio.gather(episode, return_exceptions=True)


async def test_original_current_hold_is_factual_failure_with_plane_retry(lifecycle):
    op = lifecycle
    observed = await authority(op)
    before = await current(op)
    result = await op.runner._hold_operation(op.executor, observed)
    assert result.checkpoint == before.checkpoint and result.phase == "failed"
    assert result.safe_error_code == "assignment_failed"
    assert 3 <= (result.next_wake_at - datetime.now(UTC)).total_seconds() <= 5
    assert (await admission(op)).state == OperationState.COMPLETED
    # A completion acknowledgement is final even if cleanup asks to hold again.
    assert await op.runner._hold_operation(op.executor, observed) is None


async def test_feature_disabled_after_refresh_refuses_guarded_completion(
    lifecycle, monkeypatch
):
    op = lifecycle
    observed = await authority(op)
    before = await current(op)
    op.runner.service.enabled = False
    monkeypatch.setattr(
        op.runner, "_operation_authority", AsyncMock(return_value=observed)
    )
    with pytest.raises(AssignmentError, match="assignment_feature_disabled"):
        await op.runner._finish_operation(
            op.executor, OneShotEpisodeResult(before, completion(before))
        )
    assert await current(op) == before


async def test_cancellation_while_session_row_locked_releases_worker_before_blocker(
    lifecycle,
):
    op = lifecycle
    observed = await authority(op)
    started = asyncio.Event()
    # Fixture-only lock injection, never product SQL or a live database.
    with op.runtime.transaction() as blocker:
        blocker.fetch_one(
            "SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (op.fixture[2],)
        )

        async def attempt():
            started.set()
            return await op.store.operation_lifecycle_transaction(
                authority=observed,
                fence=op.executor.claim.fence,
                binding=op.executor.binding,
                callback=lambda *_: pytest.fail("blocked callback ran"),
            )

        task = asyncio.create_task(attempt())
        await started.wait()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        deadline = asyncio.get_running_loop().time() + 2
        while (
            op.store.async_runtime._active
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        assert op.store.async_runtime._active == 0
        # SQL lock cap must release the owner lock despite the retained blocker.
        await op.store.transaction(
            lambda tx, repo: tx.fetch_one("SELECT 1 AS alive"), bound_session_waits=True
        )
    assert (await admission(op)).state == OperationState.RUNNING


async def test_recovery_retires_only_expired_exact_operation_binding(lifecycle):
    op = lifecycle
    before = await current(op)
    # Test-only crash clock perturbation; recovery itself uses public Plane APIs.
    with op.runtime.transaction() as tx:
        expiry = datetime.now(UTC) - timedelta(seconds=1)
        tx.execute(
            "UPDATE persistent_assignment SET lease_expires_at=%s, "
            "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
            (expiry, expiry.isoformat(), op.record.assignment_id),
        )
    recovered = await op.runner._recover_operations()
    assert recovered.reclaimed_assignment_ids == (op.record.assignment_id,)
    after = await current(op)
    assert after.checkpoint == before.checkpoint and after.phase == "failed"
    assert 3 <= (after.next_wake_at - datetime.now(UTC)).total_seconds() <= 5
    assert (await admission(op)).state == OperationState.FAILED
    assert (await op.runner._recover_operations()).reclaimed_assignment_ids == ()


async def test_discovery_continues_past_unavailable_original_session(
    lifecycle, monkeypatch
):
    op = lifecycle
    unavailable = await asyncio.to_thread(create_operation, op.fixture, op.runtime)
    available = await asyncio.to_thread(create_operation, op.fixture, op.runtime)
    original = op.runner._operation_authority
    seen = []

    async def resolve(record):
        seen.append(record.assignment_id)
        if record.assignment_id == unavailable.assignment_id:
            raise RuntimeError("synthetic unavailable session")
        return await original(record)

    monkeypatch.setattr(op.runner, "_operation_authority", resolve)
    # Prevent actual episode execution until the separately qualified reader is integrated.
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", lambda claim: started.append(claim))
    await op.runner._tick_operations()
    assert seen == [unavailable.assignment_id, available.assignment_id]
    assert [claim.assignment.assignment_id for claim in started] == [
        available.assignment_id
    ]
    await op.runner._tick_operations()
    assert op.runner._operation_cursor is None


async def test_discovery_obeys_shared_concurrency_and_shutdown(lifecycle, monkeypatch):
    op = lifecycle
    query = AsyncMock(side_effect=AssertionError("no database read allowed"))
    monkeypatch.setattr(op.store, "transaction", query)
    pending = asyncio.create_task(asyncio.Event().wait())
    op.runner._active = {("persistent", 1): pending, ("one-shot", 1): pending}
    await op.runner._tick_operations()
    op.runner._active.clear()
    op.runner._stopping = True
    await op.runner._tick_operations()
    query.assert_not_awaited()
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)


async def test_one_shot_claim_without_explicit_capability_never_uses_persistent_episode(
    lifecycle,
):
    op = lifecycle
    runner = AssignmentRunner(op.runner.orch, op.runner.service)
    runner.episode = AsyncMock(
        side_effect=AssertionError("persistent planner must not run")
    )
    with pytest.raises(DispatchDenied):
        await runner.run_claim(op.executor.claim)
    runner.episode.assert_not_awaited()


@pytest.mark.parametrize(
    "value", [object(), {"sessions": object(), "episode": lambda _: None}]
)
def test_untyped_capability_refused(value):
    with pytest.raises(TypeError):
        AssignmentRunner(object(), SimpleNamespace(store=object()), one_shot=value)


@pytest.mark.parametrize("sessions,handler", [(None, lambda _: None), (object(), None)])
def test_missing_capability_fields_refused(sessions, handler):
    with pytest.raises(ValueError):
        OneShotLifecycle(sessions, handler)


async def test_claim_does_not_adopt_state_changed_after_resolver(
    lifecycle, monkeypatch
):
    op = lifecycle
    candidate = await asyncio.to_thread(create_operation, op.fixture, op.runtime)
    observed = await op.runner._operation_authority(candidate)

    def competing_episode():
        repo = op.runtime.repositories.assignments
        with op.runtime.transaction() as tx:
            claim = repo.claim_operation_for_administration(
                tx,
                owner_id=candidate.owner_id,
                assignment_id=candidate.assignment_id,
                expected_state_version=candidate.state_version,
                worker_id="competing-worker",
                authority=observed.observation,
                lease_seconds=30,
            )
            return repo.finish_episode(
                tx,
                fence=claim.fence,
                completion=completion(claim.assignment, next_wake_at=datetime.now(UTC)),
            )

    newer = await asyncio.to_thread(competing_episode)
    assert newer.state_version > candidate.state_version
    monkeypatch.setattr(
        op.runner, "_operation_authority", AsyncMock(return_value=observed)
    )
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", lambda claim: started.append(claim))
    await op.runner._tick_operations()
    assert started == []
    actual = await op.store.call(
        "get_operation",
        owner_id=candidate.owner_id,
        assignment_id=candidate.assignment_id,
    )
    assert actual.assignment == newer


async def test_actual_renewal_uses_shorter_configured_lease_interval(
    lifecycle, monkeypatch
):
    op = lifecycle
    before = await current(op)
    intervals = []

    async def tick(delay):
        intervals.append(delay)
        if len(intervals) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", tick)
    episode = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(asyncio.CancelledError):
            await op.runner._renew_operation(op.executor, episode)
        assert intervals == [7, 7]
        assert (await current(op)).state_version > before.state_version
        assert (await admission(op)).state == OperationState.RUNNING
        assert not episode.done()
    finally:
        episode.cancel()
        await asyncio.gather(episode, return_exceptions=True)


async def test_recovery_never_retires_newer_admission_generation(lifecycle):
    op = lifecycle
    replacement = await asyncio.to_thread(
        op.coordinator.reselect_execution, op.executor.operation_fence
    )
    assert (
        replacement.execution_lease_token
        != op.executor.operation_fence.execution_lease_token
    )
    with op.runtime.transaction() as tx:
        expiry = datetime.now(UTC) - timedelta(seconds=1)
        tx.execute(
            "UPDATE persistent_assignment SET lease_expires_at=%s, "
            "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
            (expiry, expiry.isoformat(), op.record.assignment_id),
        )
    await op.runner._recover_operations()
    result = await asyncio.to_thread(
        op.coordinator.assert_current_execution, replacement
    )
    assert result.state == OperationState.RUNNING
    assert (await current(op)).phase == "failed"


async def test_capability_tick_alternates_first_pick_without_separate_concurrency(
    lifecycle, monkeypatch
):
    op = lifecycle
    events = []

    async def operations():
        events.append("one-shot")

    old_call = op.store.call

    async def call(method, **kwargs):
        if method == "claim_due_for_administration":
            events.append("persistent")
        return await old_call(method, **kwargs)

    monkeypatch.setattr(op.runner, "_tick_operations", operations)
    monkeypatch.setattr(op.store, "call", call)
    await op.runner.tick()
    await op.runner.tick()
    assert events == ["one-shot", "persistent", "persistent", "one-shot"]


async def test_claim_completed_during_shutdown_is_not_started(lifecycle, monkeypatch):
    op = lifecycle
    await asyncio.to_thread(create_operation, op.fixture, op.runtime)
    old = op.runner._operation_authority

    async def resolve(record):
        authority = await old(record)
        op.runner._stopping = True
        return authority

    monkeypatch.setattr(op.runner, "_operation_authority", resolve)
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", lambda claim: started.append(claim))
    await op.runner._tick_operations()
    assert started == [] and op.runner._stopping


async def test_mutated_operation_metadata_after_authority_refuses_callback(lifecycle):
    op = lifecycle
    observed = await authority(op)
    altered = replace(
        observed,
        record=replace(
            observed.record,
            operation={**observed.record.operation, "source_retention": "transient"},
        ),
    )
    with pytest.raises(AssignmentError, match="assignment_state_changed"):
        await op.store.operation_lifecycle_transaction(
            authority=altered,
            fence=op.executor.claim.fence,
            binding=op.executor.binding,
            callback=lambda *_: pytest.fail("mutated operation callback ran"),
        )


async def reader_runner(op, handler):
    executor = op.executor
    runner = AssignmentRunner(
        executor.orch,
        executor.service,
        config=RunnerConfig(concurrency=2),
        one_shot=OneShotLifecycle(op.sessions, handler),
    )
    snapshot = (
        await executor.store.call(
            "get_operation",
            owner_id=op.owner,
            assignment_id=executor.record.assignment_id,
        )
    ).assignment
    # The reader fixture initially owns an episode. Truthfully yield that empty
    # episode now, so tick must discover and claim it afresh through real Plane.
    await runner._finish_operation(
        executor,
        OneShotEpisodeResult(
            snapshot, completion(snapshot, next_wake_at=datetime.now(UTC))
        ),
    )
    return runner


async def test_tick_runs_actual_governed_reader_then_yields_without_claiming_research_complete(
    operation,
):
    op = operation
    handled = []

    async def handler(executor):
        result = await executor.action("public-source-observation", REQUEST)
        assert "Public release 088" in result["text"]
        actions = await executor.store.call(
            "list_actions",
            owner_id=op.owner,
            assignment_id=executor.record.assignment_id,
        )
        assert len(actions) == 1 and actions[0].state == "succeeded"
        snapshot = (
            await executor.store.call(
                "get_operation",
                owner_id=op.owner,
                assignment_id=executor.record.assignment_id,
            )
        ).assignment
        handled.append(executor)
        return OneShotEpisodeResult(
            snapshot,
            completion(
                snapshot,
                checkpoint={
                    "schema_version": 1,
                    "source_action_id": actions[0].action_id,
                    "step": "source_observed",
                },
            ),
        )

    runner = await reader_runner(op, handler)
    try:
        await runner.tick()
        await asyncio.wait_for(asyncio.gather(*runner._active.values()), timeout=15)
        await asyncio.sleep(0)
        snapshot = (
            await op.executor.store.call(
                "get_operation",
                owner_id=op.owner,
                assignment_id=op.executor.record.assignment_id,
            )
        ).assignment
        assert len(handled) == 1 and len(op.physical) == 1 and len(op.delegations) == 1
        assert snapshot.lifecycle == "active" and snapshot.phase == "waiting"
        assert snapshot.checkpoint["step"] == "source_observed"
        assert snapshot.next_wake_at > datetime.now(UTC)
        assert snapshot.usage["spent"]["tool_calls"] == 1
        assert snapshot.usage["spent"]["model_calls"] == 0
        assert _episode_lease(handled[0]).terminal and not runner._active
        assert not getattr(handled[0].websocket, "messages", [])
    finally:
        await runner.stop()


async def test_cancelled_actual_reader_settles_without_checkpoint_or_unbind_leak(
    operation,
):
    op = operation
    entered = asyncio.Event()

    async def held():
        entered.set()
        await asyncio.Event().wait()

    op.hooks.before = held

    async def handler(executor):
        await executor.action("cancelled-source-observation", REQUEST)
        pytest.fail("cancelled read must not complete handler")

    runner = await reader_runner(op, handler)
    try:
        await runner.tick()
        await asyncio.wait_for(entered.wait(), 15)
        tasks = tuple(runner._active.values())
        await runner.stop()
        assert all(task.cancelled() for task in tasks)
        snapshot = (
            await op.executor.store.call(
                "get_operation",
                owner_id=op.owner,
                assignment_id=op.executor.record.assignment_id,
            )
        ).assignment
        assert (
            snapshot.checkpoint == {"schema_version": 1}
            and snapshot.lifecycle == "active"
        )
        actions = await op.executor.store.call(
            "list_actions", owner_id=op.owner, assignment_id=snapshot.assignment_id
        )
        assert len(actions) == 1 and actions[0].state == "failed"
        assert snapshot.usage["spent"]["tool_calls"] == 1
        assert snapshot.usage["outstanding"]["tool_calls"] == 0
        assert op.physical == []
    finally:
        await runner.stop()


@pytest.mark.parametrize("invalid", [True, False])
async def test_handler_failure_or_malformed_result_is_closed_factual_retry(
    operation, invalid
):
    op = operation

    async def handler(executor):
        if invalid:
            return {"completed": True, "text": "must not publish"}
        raise RuntimeError("PRIVATE synthetic failure")

    runner = await reader_runner(op, handler)
    try:
        await runner.tick()
        await asyncio.wait_for(asyncio.gather(*runner._active.values()), 15)
        snapshot = (
            await op.executor.store.call(
                "get_operation",
                owner_id=op.owner,
                assignment_id=op.executor.record.assignment_id,
            )
        ).assignment
        assert (
            snapshot.phase == "failed"
            and snapshot.safe_error_code == "assignment_failed"
        )
        assert snapshot.lifecycle == "active" and snapshot.checkpoint == {
            "schema_version": 1
        }
        assert 3 <= (snapshot.next_wake_at - datetime.now(UTC)).total_seconds() <= 5
        assert op.physical == []
    finally:
        await runner.stop()


async def test_renewal_does_not_rotate_inside_actual_reader_delegation_window(
    operation, monkeypatch
):
    op = operation
    entered, release = asyncio.Event(), asyncio.Event()

    async def delegated():
        entered.set()
        await release.wait()

    op.hooks.delegated = delegated
    runner = AssignmentRunner(
        op.executor.orch,
        op.executor.service,
        one_shot=OneShotLifecycle(op.sessions, AsyncMock()),
    )
    op.executor.runner = runner
    op.executor.operation_authority_lock = _episode_lease(op.executor).lock
    task = asyncio.create_task(op.executor.action("coherent-delegation", REQUEST))
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if asyncio.current_task().get_name() == "test-renew-window":
            return await original_sleep(0.01)
        return await original_sleep(delay)

    renewal = None
    try:
        await asyncio.wait_for(entered.wait(), 15)
        before = len(op.refreshes)
        monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", sleep)
        renewal = asyncio.create_task(
            runner._renew_operation(op.executor, task), name="test-renew-window"
        )
        await original_sleep(0.15)
        # Fails before coordination: renewal replaces the snapshot already used
        # for actual delegation, so the following final gate cannot use it.
        after = len(op.refreshes)
        assert after == before
        release.set()
        result = await asyncio.wait_for(task, 15)
        assert "Public release 088" in result["text"] and len(op.physical) == 1
    finally:
        release.set()
        if renewal is not None:
            renewal.cancel()
        task.cancel()
        await asyncio.gather(
            task, *([renewal] if renewal else []), return_exceptions=True
        )


async def test_renewal_does_not_cancel_reader_own_refresh_claim(operation, monkeypatch):
    from orchestrator import web_auth

    op = operation
    entered, release = asyncio.Event(), asyncio.Event()
    original_exchange = web_auth._exchange_session_refresh

    async def exchange(refresh, prior):
        entered.set()
        await release.wait()
        return await original_exchange(refresh, prior)

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    runner = AssignmentRunner(
        op.executor.orch,
        op.executor.service,
        one_shot=OneShotLifecycle(op.sessions, AsyncMock()),
    )
    op.executor.runner = runner
    op.executor.operation_authority_lock = _episode_lease(op.executor).lock
    task = asyncio.create_task(op.executor.action("coherent-refresh", REQUEST))
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if asyncio.current_task().get_name() == "test-renew-refresh":
            return await original_sleep(0.01)
        return await original_sleep(delay)

    renewal = None
    try:
        await asyncio.wait_for(entered.wait(), 15)
        monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", sleep)
        renewal = asyncio.create_task(
            runner._renew_operation(op.executor, task), name="test-renew-refresh"
        )
        await original_sleep(0.15)
        assert not task.done()
        release.set()
        result = await asyncio.wait_for(task, 15)
        assert "Public release 088" in result["text"] and len(op.physical) == 1
    finally:
        release.set()
        if renewal is not None:
            renewal.cancel()
        task.cancel()
        await asyncio.gather(
            task, *([renewal] if renewal else []), return_exceptions=True
        )


async def test_long_physical_reader_renews_while_authority_window_is_released(
    operation,
):
    op = operation
    coordinator = await asyncio.to_thread(
        WorkAdmissionCoordinator.from_plane,
        plane_runtime=op.runtime,
        slot_lease=timedelta(seconds=1),
    )
    op.executor.orch.work_admission = coordinator
    entered, release = asyncio.Event(), asyncio.Event()
    selected = []

    async def held_physical():
        entered.set()
        await release.wait()

    op.hooks.before = held_physical

    async def handler(executor):
        selected.append(executor)
        result = await executor.action("long-public-read", REQUEST)
        assert "Public release 088" in result["text"]
        snapshot = (
            await executor.store.call(
                "get_operation",
                owner_id=op.owner,
                assignment_id=executor.record.assignment_id,
            )
        ).assignment
        return OneShotEpisodeResult(snapshot, completion(snapshot))

    runner = await reader_runner(op, handler)
    try:
        await runner.tick()
        await asyncio.wait_for(entered.wait(), 15)
        before = len(op.refreshes)
        await asyncio.sleep(1.5)  # Longer than the actual initial admission lease.
        after = len(op.refreshes)
        assert after > before
        # A renewal may currently own the window; the still-held physical call
        # must allow that renewal to finish and this independent waiter through.
        async with asyncio.timeout(2):
            async with _episode_lease(selected[0]).lock:
                current_admission = await asyncio.to_thread(
                    coordinator.assert_current_execution, selected[0].operation_fence
                )
                assert current_admission.state == OperationState.RUNNING
        release.set()
        await asyncio.wait_for(asyncio.gather(*runner._active.values()), 15)
        assert len(op.physical) == 1 and _episode_lease(selected[0]).terminal
        current_admission = await asyncio.to_thread(
            coordinator.query_operation,
            owner=OperationOwner(OwnerScope.USER, op.owner, None),
            operation_id=selected[0].operation_fence.operation_id,
        )
        assert current_admission.state == OperationState.COMPLETED
    finally:
        release.set()
        await runner.stop()


async def test_prepermit_cancellation_releases_window_and_unstarted_reservation(
    operation,
):
    op = operation
    entered, release = asyncio.Event(), asyncio.Event()
    selected = []

    async def delegated():
        entered.set()
        await release.wait()

    op.hooks.delegated = delegated

    async def handler(executor):
        selected.append(executor)
        await executor.action("cancel-before-permit", REQUEST)
        pytest.fail("cancelled prepermit action returned")

    runner = await reader_runner(op, handler)
    try:
        await runner.tick()
        await asyncio.wait_for(entered.wait(), 15)
        assert _episode_lease(selected[0]).lock.locked()
        await runner.stop()
        assert not _episode_lease(selected[0]).lock.locked()
        actions = await op.executor.store.call(
            "list_actions",
            owner_id=op.owner,
            assignment_id=op.executor.record.assignment_id,
        )
        assert len(actions) == 1 and actions[0].state == "failed_not_started"
        snapshot = (
            await op.executor.store.call(
                "get_operation",
                owner_id=op.owner,
                assignment_id=op.executor.record.assignment_id,
            )
        ).assignment
        assert snapshot.usage["outstanding"]["tool_calls"] == 0
        assert snapshot.usage["spent"] == {}
        assert op.physical == []
    finally:
        release.set()
        await runner.stop()


async def test_other_operation_rotation_before_settlement_suppresses_stale_output_once(
    operation, fixture
):
    from orchestrator.session_authority import refresh_operation_execution_authority

    op = operation
    other = await asyncio.to_thread(create_operation, fixture, op.runtime)
    op.executor.operation_authority_lock = _episode_lease(op.executor).lock
    original = op.executor.store.call_for_operation
    receipts = []

    async def conflicted(method, **kwargs):
        if method == "record_action_outcome":
            receipts.append(kwargs)
            await refresh_operation_execution_authority(
                owner_id=op.owner,
                assignment_id=other.assignment_id,
                sessions=op.sessions,
                plane_runtime=op.runtime,
            )
        return await original(method, **kwargs)

    op.executor.store.call_for_operation = conflicted
    with pytest.raises(DispatchDenied, match="assignment_result_unavailable"):
        await op.executor.action("concurrent-session-read", REQUEST)
    snapshot = (
        await op.executor.store.call(
            "get_operation",
            owner_id=op.owner,
            assignment_id=op.executor.record.assignment_id,
        )
    ).assignment
    assert snapshot.checkpoint == {"schema_version": 1}
    assert (
        snapshot.lifecycle == "active" and len(op.physical) == 1 and len(receipts) == 1
    )
    assert snapshot.usage["spent"]["tool_calls"] == 1
    assert snapshot.usage["outstanding"]["tool_calls"] == 0
    first = await original("record_action_outcome", **receipts[0])
    assert first.result["result_available"] is False
    assert first.result["result"] == {}
    after = (
        await op.executor.store.call(
            "get_operation",
            owner_id=op.owner,
            assignment_id=op.executor.record.assignment_id,
        )
    ).assignment
    assert after.usage == snapshot.usage
