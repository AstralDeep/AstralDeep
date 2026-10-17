"""Checks durable subscriber wakes after deadline and lease recovery on PostgreSQL.
Composes Work fixtures and the normal assignment runner across process restart.
"""

from datetime import datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace

import pytest

from persistent_agents.execution import ActionExecutor
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.models import AssignmentError
from orchestrator.work_subscriptions import _redrive_pending
from tests.helpers.database_clock import database_now, wait_for_database_time
from tests.helpers.plane_template import plane_template as plane_template
from tests.test_work_subscriptions_postgres_088 import (
    _LIMITS, _audits, _issue, _operation, _read, _runner, _subscribe,
    _env as _env, audit as audit, credentials as credentials, fixture as fixture,
    plane as plane, signing_key as signing_key, work as work,
)

runtime = plane


async def short_claim(work, runner, record):
    authority = await runner._operation_authority(record)
    claim = await work.service.store.operation_lifecycle_transaction(authority=authority,
        callback=lambda tx, repo, current: repo.claim_operation_for_administration(
            tx, owner_id=current.owner_id, assignment_id=current.assignment_id,
            expected_state_version=current.state_version, worker_id=runner.worker_id,
            lease_seconds=5, authority=authority.observation))
    operation_fence = await runner._admit(claim)
    executor = ActionExecutor(runner, claim, operation_fence, object())
    authority = await runner._operation_authority(record)

    def bind(tx, repo, current):
        repo.bind_operation(tx, fence=claim.fence, binding=executor.binding)
        return repo.assert_current_assignment_execution(tx, fence=claim.fence,
            binding=executor.binding, authority=authority.observation)

    await work.service.store.operation_lifecycle_transaction(authority=authority,
        fence=claim.fence, callback=bind)
    return executor


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unclaimed_deadline", "expired_lease_exhausted"])
async def test_recovery_terminal_failure_wakes_subscriber_durably(work, mode):
    caller = _issue(work)
    source = _operation(work, limits={**_LIMITS, "max_retries": 0},
        deadline_seconds=3 if mode == "unclaimed_deadline" else 3600)
    target = _operation(work)
    await _subscribe(work, caller, source, target)
    parked = (await _read(work, target)).assignment
    runner = await _runner(work)
    try:
        if mode == "unclaimed_deadline":
            await wait_for_database_time(work.runtime,
                datetime.fromisoformat(source.operation["deadline_at"]) + timedelta(milliseconds=1))
        else:
            executor = await short_claim(work, runner, source)
            await wait_for_database_time(work.runtime,
                executor.claim.lease_expires_at + timedelta(milliseconds=1))
        assert database_now(work.runtime) < datetime.fromisoformat(target.operation["deadline_at"])
        recovered = await runner._recover_operations()
    finally:
        await runner.stop()
    finished = (await _read(work, source)).assignment
    after = (await _read(work, target)).assignment
    wakes = await _audits(work, work.owner, "work.completion_wake")
    assert recovered.reclaimed_assignment_ids == (source.assignment_id,)
    assert finished.lifecycle == "completed" and finished.operation["terminal_outcome"] == "failed"
    expected_code = "assignment_deadline_exceeded" if mode == "unclaimed_deadline" else "assignment_retry_exhausted"
    assert finished.safe_error_code == expected_code and finished.next_wake_at is None
    assert _redrive_pending == {}
    reopened = AssignmentStore(plane_runtime=work.runtime)
    restarted_work = SimpleNamespace(runtime=work.runtime, store=work.store, orch=work.orch,
        service=AssignmentService(work.orch, reopened, enabled=True, phi_gate=work.service.phi_gate))
    restarted = await _runner(restarted_work)
    try:
        second = await restarted._recover_operations()
    finally:
        await restarted.stop()
        reopened.close()
    after_restart = (await _read(work, target)).assignment
    assert after.phase == "waiting" and after.operation["control"]["wait"] is None
    assert after.wake_generation == parked.wake_generation + 1
    assert len(wakes) == 1 and wakes[0].outputs_meta["outcome"] == "failed"
    assert after_restart == after and second.reclaimed_assignment_ids == ()
    assert work.audit.verify_chain(work.owner) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wake", "audit", "missing_records", "unmatched_record"])
async def test_recovery_rolls_back_source_and_wait_when_delivery_is_unavailable(work, monkeypatch, failure):
    caller = _issue(work)
    source = _operation(work, deadline_seconds=3)
    target = _operation(work)
    await _subscribe(work, caller, source, target)
    before = (await _read(work, source)).assignment
    parked = (await _read(work, target)).assignment
    runner = await _runner(work)
    repository = work.service.store.repository
    recover = repository.recover_expired_operations_for_administration

    def unavailable(*args, **kwargs):
        raise AssignmentError("work_control_unavailable", 503)

    def invalid_records(*args, **kwargs):
        recovered = recover(*args, **kwargs)
        return replace(recovered, terminal_assignments=None if failure == "missing_records" else (parked,))

    try:
        await wait_for_database_time(work.runtime,
            datetime.fromisoformat(source.operation["deadline_at"]) + timedelta(milliseconds=1))
        with monkeypatch.context() as fault:
            if failure == "wake":
                fault.setattr(repository, "accept_wake", unavailable)
            elif failure == "audit":
                fault.setattr(work.audit, "insert_in_transaction", unavailable)
            else:
                fault.setattr(repository, "recover_expired_operations_for_administration", invalid_records)
            with pytest.raises(AssignmentError, match="work_control_unavailable|assignment_transaction_unavailable"):
                await runner._recover_operations()
        assert (await _read(work, source)).assignment == before
        assert (await _read(work, target)).assignment == parked
        assert await _audits(work, work.owner, "work.completion_wake") == []
        assert _redrive_pending == {}
        recovered = await runner._recover_operations()
        assert recovered.reclaimed_assignment_ids == (source.assignment_id,)
        after = (await _read(work, target)).assignment
        assert after.wake_generation == parked.wake_generation + 1
        assert after.operation["control"]["wait"] is None
        assert len(await _audits(work, work.owner, "work.completion_wake")) == 1
        assert (await runner._recover_operations()).reclaimed_assignment_ids == ()
        assert work.audit.verify_chain(work.owner) is None
    finally:
        await runner.stop()
