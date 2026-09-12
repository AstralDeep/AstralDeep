"""Supervisor lifecycle, admission, checkpoint fencing and bounded task joins."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from persistent_agents.config import RunnerConfig
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import ApprovalPending
from persistent_agents.models import CreateAssignmentRequest
from persistent_agents.runner import AssignmentRunner
from persistent_agents.runtime_values import digest, thaw
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_models import create_payload
from persistent_agents.tests.test_service import service as shared_service

service = shared_service


@pytest.fixture
async def supervisor(service, monkeypatch):
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    coordinator = SimpleNamespace(expire_execution_leases=Mock(), submit=Mock(return_value=SimpleNamespace(
        accepted=True, operation_id=uuid4())), claim_operation=Mock(return_value=SimpleNamespace(fence=object())),
        cancel=Mock(), renew_execution_lease=Mock(), assert_current_execution=Mock(), terminalize=Mock())
    service.orch.work_admission = coordinator
    service.orch._unbind_machine_turn = Mock()
    service.orch.ui_sessions = {}
    service.store.call = AsyncMock(return_value=record)
    service.store.repository = SimpleNamespace(finish_episode=Mock(return_value=record),
                                               assert_current_assignment_execution=Mock(return_value=record))
    async def transaction(callback):
        return callback("transaction", service.store.repository)
    service.store.transaction = AsyncMock(side_effect=transaction)
    service.store.current_execution_transaction = AssignmentStore.current_execution_transaction.__get__(service.store)
    runner = AssignmentRunner(service.orch, service, config=RunnerConfig(concurrency=2))
    claim = SimpleNamespace(assignment=record, fence=SimpleNamespace(claim_generation=1))
    executor = SimpleNamespace(record=record, claim=claim, operation_fence=object(), binding=object(),
        approved_action_id=None,
        refresh=AsyncMock(), action=AsyncMock(return_value={"text": "Release", "revision_digest": "a"*64}))
    executor.fork = lambda socket: executor
    monkeypatch.setattr("persistent_agents.runner.safe_text", AsyncMock())
    return SimpleNamespace(runner=runner, service=service, record=record, claim=claim,
                           executor=executor, coordinator=coordinator)


@pytest.mark.asyncio
async def test_supervisor_start_stop_and_tick_failure_remain_bounded(supervisor, caplog):
    runner = supervisor.runner
    attempts = 0
    async def tick():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("PRIVATE provider failure")
        runner._stopping = True
        runner.notify()
    runner.tick = AsyncMock(side_effect=tick)
    runner.notify()
    runner.start()
    with pytest.raises(RuntimeError, match="already started"):
        runner.start()
    await asyncio.wait_for(runner._loop, 1)
    assert attempts == 2 and "PRIVATE" not in caplog.text
    pending = asyncio.create_task(asyncio.Event().wait())
    runner._active["pending"] = pending
    await runner.stop()
    assert pending.cancelled()


@pytest.mark.asyncio
async def test_tick_recovers_first_and_does_not_oversubscribe(supervisor):
    runner = supervisor.runner
    claims = (supervisor.claim,)
    async def call(method, **kwargs):
        if method == "claim_due_for_administration":
            assert kwargs["limit"] == 2
            return claims
        return ()
    supervisor.service.store.call.side_effect = call
    runner.run_claim = AsyncMock()
    await runner.tick()
    await asyncio.gather(*runner._active.values())
    await asyncio.sleep(0)
    assert not runner._active
    runner.run_claim.assert_awaited_once_with(supervisor.claim)
    supervisor.coordinator.expire_execution_leases.assert_called_once()
    runner._active = {"one": Mock(), "two": Mock()}
    supervisor.service.store.call.reset_mock()
    await runner.tick()
    assert [call.args[0] for call in supervisor.service.store.call.call_args_list] == ["recover_expired_for_administration"]


def test_finished_callback_reports_failure_without_secret(supervisor, caplog):
    task = Mock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("PRIVATE")
    supervisor.runner._active["task"] = task
    supervisor.runner._finished("task", task)
    assert not supervisor.runner._active and "PRIVATE" not in caplog.text


@pytest.mark.asyncio
async def test_replacement_claim_keeps_both_generations_supervised_until_shutdown(supervisor):
    runner = supervisor.runner
    newer = SimpleNamespace(assignment=supervisor.record, fence=SimpleNamespace(claim_generation=2))
    claims = iter(((supervisor.claim,), (newer,)))
    async def call(method, **kwargs):
        return next(claims) if method == "claim_due_for_administration" else ()
    supervisor.service.store.call.side_effect = call
    entered = asyncio.Queue()
    cancelled = []
    async def episode(claim):
        entered.put_nowait(claim.fence.claim_generation)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(claim.fence.claim_generation)
    runner.run_claim = episode
    await runner.tick()
    assert await entered.get() == 1
    await runner.tick()
    assert await entered.get() == 2
    assert len(runner._active) == 2
    await runner.tick()  # Both generations count against the local limit.
    await runner.stop()
    assert sorted(cancelled) == [1, 2]
    assert not runner._active


def test_late_callback_cannot_remove_another_task(supervisor):
    old, current = Mock(), Mock()
    old.cancelled.return_value = True
    supervisor.runner._active["identity"] = current
    supervisor.runner._finished("identity", old)
    assert supervisor.runner._active["identity"] is current


@pytest.mark.asyncio
async def test_claim_returning_during_shutdown_never_starts(supervisor):
    runner = supervisor.runner
    async def call(method, **kwargs):
        if method == "claim_due_for_administration":
            runner._stopping = True
            return (supervisor.claim,)
        return ()
    supervisor.service.store.call.side_effect = call
    runner.run_claim = AsyncMock()
    await runner.tick()
    runner.run_claim.assert_not_called()
    assert not runner._active


@pytest.mark.asyncio
async def test_admission_denial_and_unclaimable_operation_are_cancelled(supervisor):
    runner, coordinator = supervisor.runner, supervisor.coordinator
    assert await runner._admit(supervisor.claim) is coordinator.claim_operation.return_value.fence
    coordinator.submit.return_value.accepted = False
    with pytest.raises(DispatchDenied):
        await runner._admit(supervisor.claim)
    coordinator.submit.return_value.accepted = True
    coordinator.claim_operation.return_value = None
    with pytest.raises(DispatchDenied):
        await runner._admit(supervisor.claim, interactive=True)
    coordinator.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_lost_lease_cancels_episode_before_another_action(supervisor, monkeypatch):
    monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", AsyncMock())
    supervisor.service.store.call.side_effect = DispatchDenied("assignment_stale")
    episode = Mock()
    await supervisor.runner._renew(supervisor.executor, episode)
    episode.cancel.assert_called_once()
    supervisor.service.store.call.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await supervisor.runner._renew(supervisor.executor, episode)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_lease_lost", [False, True])
async def test_active_renewal_checks_both_current_fences(supervisor, monkeypatch, operation_lease_lost):
    ticks = 0

    async def one_tick(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", one_tick)
    if operation_lease_lost:
        supervisor.coordinator.renew_execution_lease.side_effect = DispatchDenied("operation_stale")
    episode = Mock()
    if operation_lease_lost:
        await supervisor.runner._renew(supervisor.executor, episode)
        episode.cancel.assert_called_once()
    else:
        with pytest.raises(asyncio.CancelledError):
            await supervisor.runner._renew(supervisor.executor, episode)
        episode.cancel.assert_not_called()
    supervisor.service.store.call.assert_awaited_once_with(
        "renew_claim", fence=supervisor.claim.fence, lease_seconds=supervisor.runner.config.lease_seconds,
    )
    supervisor.coordinator.renew_execution_lease.assert_called_once_with(supervisor.executor.operation_fence)


@pytest.mark.asyncio
async def test_renewal_waits_for_terminal_commit_acknowledgement(supervisor, monkeypatch):
    committed = asyncio.Event()
    acknowledge = asyncio.Event()
    tick = asyncio.Event()
    tick_reached = asyncio.Event()

    async def transaction(callback):
        result = callback("transaction", supervisor.service.store.repository)
        committed.set()
        await acknowledge.wait()
        return result

    async def renewal_tick(_seconds):
        await tick.wait()
        tick_reached.set()

    supervisor.service.store.transaction.side_effect = transaction
    supervisor.service.store.call.side_effect = DispatchDenied("assignment_stale")
    monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", renewal_tick)
    episode = Mock()
    finish = asyncio.create_task(supervisor.runner._finish(supervisor.executor, supervisor.record))
    renewal = asyncio.create_task(supervisor.runner._renew(supervisor.executor, episode))
    try:
        await asyncio.wait_for(committed.wait(), 1)
        tick.set()
        await asyncio.wait_for(tick_reached.wait(), 1)
        supervisor.service.store.call.assert_not_awaited()
        episode.cancel.assert_not_called()
        acknowledge.set()
        assert await asyncio.wait_for(finish, 1) == supervisor.record
        await asyncio.wait_for(renewal, 1)
        supervisor.service.store.call.assert_not_awaited()
        supervisor.coordinator.renew_execution_lease.assert_not_called()
        episode.cancel.assert_not_called()
    finally:
        acknowledge.set()
        finish.cancel()
        renewal.cancel()
        await asyncio.gather(finish, renewal, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_terminal_commit_does_not_disable_lost_lease_cancellation(supervisor, monkeypatch):
    supervisor.service.store.transaction.side_effect = RuntimeError("transaction rolled back")
    with pytest.raises(RuntimeError, match="rolled back"):
        await supervisor.runner._finish(supervisor.executor, supervisor.record)
    monkeypatch.setattr("persistent_agents.runner.asyncio.sleep", AsyncMock())
    supervisor.service.store.call.side_effect = DispatchDenied("assignment_stale")
    episode = Mock()
    await supervisor.runner._renew(supervisor.executor, episode)
    episode.cancel.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, ApprovalPending("assignment_approval_required"),
                                     DispatchDenied("assignment_budget_exhausted"), RuntimeError("private")])
async def test_claim_cleans_up_virtual_session_and_releases_renewal(supervisor, monkeypatch, failure):
    monkeypatch.setattr("persistent_agents.runner.ActionExecutor", lambda *a, **kw: supervisor.executor)
    runner = supervisor.runner
    runner.episode = AsyncMock(side_effect=failure)
    runner._hold = AsyncMock()
    await runner.run_claim(supervisor.claim)
    supervisor.service.orch._unbind_machine_turn.assert_called_once()
    if failure:
        runner._hold.assert_awaited_once()
        assert "private" not in runner._hold.call_args.args[1]
    else:
        runner._hold.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_claim_propagates_without_publishing_stale_hold(supervisor, monkeypatch):
    monkeypatch.setattr("persistent_agents.runner.ActionExecutor", lambda *a, **kw: supervisor.executor)
    supervisor.runner.episode = AsyncMock(side_effect=asyncio.CancelledError)
    supervisor.runner._hold = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await supervisor.runner.run_claim(supervisor.claim)
    supervisor.runner._hold.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,phase", [("assignment_approval_required", "waiting_approval"),
    ("assignment_authorization_required", "waiting_authorization"), ("assignment_budget_exhausted", "budget_exhausted"),
    ("assignment_action_uncertain", "reconciliation"), ("private error", "failed")])
async def test_hold_has_actionable_safe_reason_and_no_automatic_sensitive_retry(supervisor, code, phase):
    supervisor.runner._finish = AsyncMock()
    await supervisor.runner._hold(supervisor.executor, code)
    kwargs = supervisor.runner._finish.call_args.kwargs
    assert kwargs["phase"] == phase
    assert kwargs["activity"].notification_state == "pending"
    assert "private" not in kwargs["reason"]


@pytest.mark.asyncio
async def test_finish_checkpoints_and_terminalizes_same_operation_in_one_transaction(supervisor):
    result = await supervisor.runner._finish(supervisor.executor, supervisor.record)
    assert result == supervisor.record
    finish = supervisor.service.store.repository.finish_episode.call_args
    assert finish.args == ("transaction",)
    assert finish.kwargs["completion"].expected_state_version == supervisor.record.state_version
    assert finish.kwargs["completion"].next_wake_at is not None
    assert supervisor.coordinator.terminalize.call_args.kwargs["transaction"] == "transaction"
    supervisor.coordinator.assert_current_execution.assert_not_called()
    guard = supervisor.service.store.repository.assert_current_assignment_execution
    assert guard.call_args.args == ("transaction",)
    assert guard.call_args.kwargs == {"fence": supervisor.claim.fence,
        "binding": supervisor.executor.binding, "action_id": None}
    supervisor.executor.refresh.assert_awaited_once()
    current = replace(supervisor.record, state_version=2)
    guard.return_value = current
    await supervisor.runner._finish(supervisor.executor, supervisor.record)
    assert supervisor.service.store.repository.finish_episode.call_args.kwargs["completion"].expected_state_version == 2
    guard.return_value = replace(current, checkpoint={"new": "payload"})
    with pytest.raises(DispatchDenied, match="state_changed"):
        await supervisor.runner._finish(supervisor.executor, supervisor.record)


@pytest.mark.asyncio
async def test_unchanged_source_skips_models_and_notifications(supervisor):
    record = replace(supervisor.record, checkpoint={"cursor": {"revision": "a"*64, "sequence": 1}})
    supervisor.executor.record = record
    async def call(method, **kwargs):
        return () if method == "list_events" else record
    supervisor.service.store.call.side_effect = call
    supervisor.runner._finish = AsyncMock()
    supervisor.runner._model = AsyncMock()
    await supervisor.runner.episode(supervisor.executor)
    supervisor.runner._model.assert_not_called()
    finish = supervisor.runner._finish.call_args.kwargs
    assert "last_checked_at" in finish["checkpoint"]
    assert "activity" not in finish


@pytest.mark.asyncio
@pytest.mark.parametrize("finding", ["Relevant release change", "UNCHANGED"])
async def test_episode_reads_source_then_durably_joins_tasks_once(supervisor, finding):
    record = supervisor.record
    source_event = None
    async def call(method, **kwargs):
        nonlocal record, source_event
        if method == "list_events":
            return ()
        if method == "record_source_batch":
            source_event = kwargs["batch"].events[0]
            record = replace(record, checkpoint={"cursor": thaw(kwargs["batch"].next_cursor)})
            return record, (source_event,)
        if method == "put_task_plan":
            record = replace(record, tasks=tuple(thaw(task) for task in kwargs["tasks"]))
        return record
    supervisor.service.store.call.side_effect = call
    async def task(executor, task, event, siblings):
        nonlocal record
        record = replace(record, tasks=tuple({**entry, "state": "completed", "bounded_result": "Analyzed",
            "result_digest": "d"*64} for entry in record.tasks))
    supervisor.runner._task = AsyncMock(side_effect=task)
    supervisor.runner._model = AsyncMock(side_effect=[
        {"text": '{"tasks":[{"id":"analysis","instruction":"Compare public releases","tools":[],"depends_on":[]}]}'},
        {"text": '{"kind":"result","text":"' + finding + '","completed":false}'},
    ])
    supervisor.runner._finish = AsyncMock()
    await supervisor.runner.episode(supervisor.executor)
    assert supervisor.runner._task.await_count == 1
    kwargs = supervisor.runner._finish.call_args.kwargs
    assert len(kwargs["receipts"]) == len(kwargs["incorporations"]) == 1
    assert kwargs["receipts"][0]["event_id"] == source_event.event_id
    if finding == "UNCHANGED":
        assert "last_finding" not in kwargs["checkpoint"]
    else:
        assert kwargs["checkpoint"]["last_finding"] == finding
    assert (kwargs["activity"] is None) == (finding == "UNCHANGED")


@pytest.mark.asyncio
async def test_child_failure_cancels_and_joins_siblings_before_parent_release(supervisor):
    event = SimpleNamespace(event_id=str(uuid4()), context={})
    plan_key = digest([supervisor.record.instruction_revision, event.event_id])
    tasks = tuple({"plan_key": plan_key, "task_id": str(uuid4()), "state": "pending", "depends_on": []} for _ in range(2))
    record = replace(supervisor.record, tasks=tasks, definition=replace(supervisor.record.definition,
        limits={**supervisor.record.definition.limits, "max_concurrent_tasks": 2}))
    supervisor.executor.record = record
    async def call(method, **kwargs):
        return (event,) if method == "list_events" else record
    supervisor.service.store.call.side_effect = call
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def task(executor, task, *args):
        if task["task_id"] == tasks[0]["task_id"]:
            await started.wait()
            raise ApprovalPending("assignment_approval_required")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    supervisor.runner._task = task
    with pytest.raises(ApprovalPending):
        await supervisor.runner.episode(supervisor.executor)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_task_uses_bounded_steps_and_persists_result_provenance(supervisor):
    task = {"task_id": str(uuid4()), "task_generation": 0, "instruction_revision": 1,
            "instruction": "Analyze", "allowed_tools": ["web-research-1:fetch_page"], "depends_on": []}
    event = SimpleNamespace(event_id=str(uuid4()), context={"text": "Public release evidence"})
    supervisor.runner._model = AsyncMock(side_effect=[
        {"text": '{"kind":"tool","tool":"web-research-1:fetch_page","arguments":{"url":"https://example.org"}}'},
        {"text": '{"kind":"result","text":"Supported finding"}'},
    ])
    await supervisor.runner._task(supervisor.executor, task, event, [])
    assert supervisor.service.store.call.call_args.args[0] == "complete_task"
    assert supervisor.service.store.call.call_args.kwargs["result"].provenance["event_id"] == event.event_id
    supervisor.runner._model = AsyncMock(return_value={"text": '{"kind":"tool","tool":"web-research-1:fetch_page","arguments":{}}'})
    with pytest.raises(DispatchDenied, match="step_limit"):
        await supervisor.runner._task(supervisor.executor, task, event, [])
    assert supervisor.runner._model.await_count == 4


@pytest.mark.asyncio
async def test_approved_executor_binds_attended_claim_and_retains_outcome(supervisor, monkeypatch):
    action = SimpleNamespace(owner_id="owner", assignment_id=supervisor.record.assignment_id, action_id=str(uuid4()),
                             intent=SimpleNamespace(request_digest="a"*64))
    supervisor.service._interaction = Mock(return_value={"sub": "owner"})
    supervisor.service.get = AsyncMock(return_value=supervisor.record)
    supervisor.executor.execute = AsyncMock()
    constructor = Mock(return_value=supervisor.executor)
    monkeypatch.setattr("persistent_agents.runner.ActionExecutor", constructor)
    async def call(method, **kwargs):
        if method == "claim_for_approved_action":
            assert kwargs["action_id"] == action.action_id
            assert kwargs["interactive_receipt_id"]
            return supervisor.claim
        return action if method == "get_action" else supervisor.record
    supervisor.service.store.call.side_effect = call
    supervisor.runner._finish = AsyncMock()
    assert await supervisor.runner.execute_approved(action, object(), remote_marker="remote") == action
    assert constructor.call_args.kwargs["approved_action_id"] == action.action_id
    assert supervisor.runner._finish.call_args.kwargs["reason"] == "approved_action_completed"
    supervisor.executor.execute.side_effect = DispatchDenied("assignment_refused")
    supervisor.runner._hold = AsyncMock()
    with pytest.raises(DispatchDenied):
        await supervisor.runner.execute_approved(action, object())
    supervisor.runner._hold.assert_awaited_once()


@pytest.mark.asyncio
async def test_hold_skips_failed_remote_refresh_but_still_requires_local_guard(supervisor):
    from persistent_agents.runner import _episode_lease

    supervisor.executor.refresh.side_effect = DispatchDenied("assignment_authorization_required")
    supervisor.runner._notify_activity = AsyncMock()
    guard = supervisor.service.store.repository.assert_current_assignment_execution
    guard.side_effect = DispatchDenied("assignment_authorization_unavailable")
    with pytest.raises(DispatchDenied, match="assignment_authorization_unavailable"):
        await supervisor.runner._hold(supervisor.executor, "assignment_authorization_required")
    supervisor.executor.refresh.assert_not_awaited()
    guard.assert_called_once()
    supervisor.service.store.repository.finish_episode.assert_not_called()
    supervisor.coordinator.terminalize.assert_not_called()
    supervisor.runner._notify_activity.assert_not_awaited()
    assert not _episode_lease(supervisor.executor).terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"phase": "waiting"}, {"checkpoint": {}}, {"receipts": ({"receipt": "new"},)},
    {"incorporations": ({"result": "new"},)}, {"completed": True},
])
async def test_authority_hold_cannot_publish_checkpoint_or_completion(supervisor, changes):
    kwargs = {"phase": "waiting_authorization", "authority_hold": True, **changes}
    with pytest.raises(DispatchDenied, match="assignment_hold_invalid"):
        await supervisor.runner._finish(supervisor.executor, supervisor.record, **kwargs)
    supervisor.executor.refresh.assert_not_awaited()
    supervisor.service.store.transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_finish_refreshes_before_guard_and_forwards_approved_action(supervisor):
    events = []

    async def refresh():
        events.append("remote")

    def guard(*args, **kwargs):
        assert events == ["remote"]
        assert kwargs["action_id"] == supervisor.executor.approved_action_id
        events.append("local")
        return supervisor.record

    supervisor.executor.approved_action_id = str(uuid4())
    supervisor.executor.refresh.side_effect = refresh
    supervisor.service.store.repository.assert_current_assignment_execution.side_effect = guard
    await supervisor.runner._finish(supervisor.executor, supervisor.record)
    assert events == ["remote", "local"]


@pytest.mark.asyncio
async def test_activity_notification_cas_paginates_and_never_duplicates(supervisor, caplog):
    def activity(sequence, state="pending"):
        return SimpleNamespace(activity_id=f"a-{sequence}", sequence=sequence,
            title="Release finding", summary="A relevant change", notification_state=state)
    first_page = tuple(activity(index, "sent") for index in range(1, 101))
    calls = []
    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "list_activity":
            return first_page if kwargs["after_sequence"] == 0 else (activity(101), activity(102))
        if method == "mark_activity_notified":
            return kwargs["activity_id"] == "a-101"
        raise AssertionError(method)
    supervisor.service.store.call.side_effect = call
    supervisor.service.orch.notify_user = AsyncMock()
    await supervisor.runner._notify_activity(supervisor.record)
    supervisor.service.orch.notify_user.assert_awaited_once()
    assert supervisor.service.orch.notify_user.call_args.args[1]["activity_id"] == "a-101"
    assert [kwargs["after_sequence"] for method, kwargs in calls if method == "list_activity"] == [0, 100]
    supervisor.service.orch.notify_user.side_effect = ConnectionError("PRIVATE")
    await supervisor.runner._notify_activity(supervisor.record)
    assert "PRIVATE" not in caplog.text


@pytest.mark.asyncio
async def test_activity_only_notifies_after_checkpoint_commit(supervisor):
    from astralplane.repositories.assignments import AssignmentActivityRecord
    supervisor.runner._notify_activity = AsyncMock()
    activity = AssignmentActivityRecord("key", "finding", "Title", "Finding")
    await supervisor.runner._finish(supervisor.executor, supervisor.record, activity=activity)
    supervisor.runner._notify_activity.assert_awaited_once_with(supervisor.record)
    supervisor.service.store.repository.finish_episode.side_effect = DispatchDenied("assignment_stale")
    supervisor.runner._notify_activity.reset_mock()
    with pytest.raises(DispatchDenied):
        await supervisor.runner._finish(supervisor.executor, supervisor.record, activity=activity)
    supervisor.runner._notify_activity.assert_not_called()


@pytest.mark.asyncio
async def test_model_request_stays_on_metered_action_seam(supervisor):
    supervisor.service.store.call.return_value = None
    await supervisor.runner._model(supervisor.executor, "plan", "Authority stays with owner", {"source": "untrusted"})
    request = supervisor.executor.action.call_args.args[1]
    assert request["kind"] == "model" and request["max_output_tokens"] == 4096
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][1]["role"] == "user"
