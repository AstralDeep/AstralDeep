"""Tests for persistent_agents/research_episode.py against real Postgres: the
source/model boundary holds across a transition, an old model proof cannot finish or
project a resumed epoch, and a new epoch cannot escape an unknown liability.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.session_authority import SessionAuthorityUnavailable
from orchestrator.work_admission import OperationState
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import ActionExecutor
from persistent_agents.models import AssignmentError
from persistent_agents.research_episode import run_research_episode
from persistent_agents.runner import _episode_lease
from persistent_agents.runtime_values import thaw
from persistent_agents.tests.test_research_episode_postgres_088 import (
    actions, admission, attach_runner, control, current,
    fixture as fixture, gate_orchestrator as gate_orchestrator,
    operation as operation, plane as plane, research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000, "model_calls": 2}], indirect=True)]


async def fresh_executor(op, runner):
    record = await current(op)
    authority = await runner._operation_authority(record)
    claim = await runner.store.operation_lifecycle_transaction(authority=authority,
        callback=lambda tx, repo, value: repo.claim_operation_for_administration(
            tx, owner_id=value.owner_id, assignment_id=value.assignment_id,
            expected_state_version=value.state_version, worker_id="synthetic-restarted-episode",
            authority=authority.observation, lease_seconds=60))
    fence = await runner._admit(claim)
    executor = ActionExecutor(runner, claim, fence, object(), operation_sessions=op.sessions)
    executor.operation_authority_lock = _episode_lease(executor).lock
    await runner.store.operation_lifecycle_transaction(authority=authority, fence=claim.fence,
        callback=lambda tx, repo, value: repo.bind_operation(tx, fence=claim.fence, binding=executor.binding))
    op.executor = executor
    return executor


@pytest.mark.parametrize("transition", ["pause_resume", "recovered_restart"])
async def test_source_model_boundary_after_transition(research, monkeypatch, transition):
    op = research
    runner = attach_runner(op, run_research_episode)
    initial = await current(op)
    old_executor = op.executor
    select = old_executor.research_selection
    boundary = []

    async def stop_before_model(*args, **kwargs):
        ledger = await actions(op)
        source = next(a for a in ledger if a.action_id != op.source_action.action_id)
        assert source.state == "succeeded" and len(source.attempts) == 1
        assert op.model_calls == []
        boundary.append(source)
        if transition == "pause_resume":
            await control(op, "pause")
            return await select(*args, **kwargs)
        raise InterruptedError("synthetic process exit before model")

    monkeypatch.setattr(old_executor, "research_selection", stop_before_model)
    with pytest.raises((SessionAuthorityUnavailable, AssignmentError, DispatchDenied, InterruptedError)):
        await run_research_episode(old_executor)
    assert len(boundary) == 1 and len(op.physical) == 2 and op.model_calls == []
    source_before = boundary[0]
    if transition == "pause_resume":
        await asyncio.to_thread(op.executor.orch.work_admission.terminalize,
            old_executor.operation_fence, state=OperationState.CANCELLED,
            terminal_code="synthetic_owner_pause", safe_summary=None, retry_after_ms=None)
        await control(op, "resume")
        assert (await current(op)).control_epoch == initial.control_epoch + 2
    else:
        expiry = datetime.now(UTC) - timedelta(seconds=1)
        with op.runtime.transaction() as tx:
            tx.execute("UPDATE persistent_assignment SET lease_expires_at=%s, "
                "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
                (expiry, expiry.isoformat(), initial.assignment_id))
        recovered = await runner._recover_operations()
        assert recovered.reclaimed_assignment_ids == (initial.assignment_id,)
        assert (await admission(op)).state == OperationState.FAILED
        waiting = await current(op)
        assert waiting.control_epoch == initial.control_epoch
        delay = (waiting.next_wake_at - datetime.now(UTC)).total_seconds()
        assert 3 <= delay <= 5
        await asyncio.sleep(max(0, delay) + .02)

    runner = attach_runner(op, run_research_episode)
    executor = await fresh_executor(op, runner)
    error = None
    final = None
    try:
        result = await run_research_episode(executor)
        final = await runner._finish_operation(executor, result)
    except (DispatchDenied, AssignmentError, SessionAuthorityUnavailable) as caught:
        error = str(caught)
        authority = await runner._operation_authority(await current(op))
        await runner._hold_operation(executor, authority)
        final = await current(op)

    source_after = next(a for a in await actions(op) if a.action_id == source_before.action_id)
    assert source_after == source_before
    assert error is None
    assert len(op.model_calls) == 1 and final.lifecycle == "completed"
    assert "research_result" in final.checkpoint
    assert (await admission(op)).state == OperationState.COMPLETED
    ledger = await actions(op)
    model = next(a for a in ledger if a.intent.request["kind"] == "model")
    used_source = next(a for a in ledger if a.action_id ==
                      final.checkpoint["research_result"]["source"]["action_id"])
    assert model.control_epoch == used_source.control_epoch == final.control_epoch
    if transition == "pause_resume":
        assert used_source.action_id != source_before.action_id
        assert used_source.intent.action_key != source_before.intent.action_key
        assert len(op.physical) == 3
    else:
        assert used_source == source_before
        assert len(op.physical) == 2
    assert final.usage["spent"]["tool_calls"] == len(op.physical)
    assert final.usage["spent"]["model_calls"] == 1
    assert final.usage["spent"]["tokens"] == 120
    assert all(value == 0 for value in final.usage["outstanding"].values())
    from tests.test_work_result_postgres_088 import project
    op.completed = final
    assert (await project(op))["content"] == thaw(final.checkpoint)["research_result"]


async def test_old_model_proof_cannot_finish_or_project_a_resumed_epoch(research):
    from tests.test_work_result_postgres_088 import _mutate_record, project, unavailable

    op = research
    runner = attach_runner(op, run_research_episode)
    old_executor = op.executor
    old_result = await run_research_episode(old_executor)
    old_ledger = await actions(op)
    assert len(op.model_calls) == 1
    await control(op, "pause")
    await asyncio.to_thread(op.executor.orch.work_admission.terminalize,
        old_executor.operation_fence, state=OperationState.CANCELLED,
        terminal_code="synthetic_owner_pause", safe_summary=None, retry_after_ms=None)
    await control(op, "resume")
    executor = await fresh_executor(op, runner)
    with pytest.raises((DispatchDenied, AssignmentError, SessionAuthorityUnavailable)):
        await runner._finish_operation(executor, old_result)
    assert "research_result" not in (await current(op)).checkpoint
    assert len(op.model_calls) == 1
    result = await run_research_episode(executor)
    op.completed = await runner._finish_operation(executor, result)
    assert op.completed.lifecycle == "completed"
    assert len(op.physical) == 3 and len(op.model_calls) == 2
    assert op.completed.usage["spent"]["tool_calls"] == 3
    assert op.completed.usage["spent"]["model_calls"] == 2
    assert op.completed.usage["spent"]["tokens"] == 240
    new_ledger = await actions(op)
    assert all(action in new_ledger for action in old_ledger)
    assert result.research.model_action_id != old_result.research.model_action_id
    assert result.research.private.source_action_id != old_result.research.private.source_action_id
    assert (await project(op))["available"] is True
    def stale(data):
        data["checkpoint"] = thaw(old_result.completion.checkpoint)
        data["operation"]["result_reference"] = old_result.research.model_action_id
    await _mutate_record(op, stale)
    unavailable(await project(op))
    assert len(op.model_calls) == 2 and len(op.physical) == 3


async def test_new_epoch_cannot_escape_an_unknown_issued_model_liability(research):
    op = research
    runner = attach_runner(op, run_research_episode)
    old_executor = op.executor
    op.model_status = 503
    with pytest.raises(DispatchDenied, match="assignment_action_uncertain"):
        await run_research_episode(old_executor)
    held = await current(op)
    unknown = next(a for a in await actions(op) if a.intent.request["kind"] == "model")
    assert unknown.state == "uncertain" and unknown.ever_started
    assert held.usage["outstanding"]["model_calls"] == 1
    assert held.usage["outstanding"]["tokens"] > 0
    await control(op, "pause")
    await asyncio.to_thread(op.executor.orch.work_admission.terminalize,
        old_executor.operation_fence, state=OperationState.CANCELLED,
        terminal_code="synthetic_owner_pause", safe_summary=None, retry_after_ms=None)
    await control(op, "resume")
    current_held = await current(op)
    assert current_held.control_epoch == held.control_epoch + 2
    assert current_held.phase == "reconciliation"
    with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
        await fresh_executor(op, runner)
    assert len(op.physical) == 2 and len(op.model_calls) == 1
    assert unknown in await actions(op)
    assert (await current(op)).usage == held.usage
    assert "research_result" not in (await current(op)).checkpoint
