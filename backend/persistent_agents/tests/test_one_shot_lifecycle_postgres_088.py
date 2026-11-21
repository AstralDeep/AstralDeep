"""Verifies retirement and subscription delivery in the assignment recovery worker."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from orchestrator.work_admission import InMemoryWorkAdmissionRepository, WorkAdmissionCoordinator, StaleExecutionFenceError
from persistent_agents import runner as runtime
from tests.test_work_admission_repository import _classes


def test_in_memory_renewal_refuses_foreign_transaction():
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(), repository=InMemoryWorkAdmissionRepository()
    )
    with pytest.raises(ValueError, match="external transaction"):
        coordinator.renew_execution_lease(object(), transaction=object())


async def test_worker_retries_after_failure_and_stops_cleanly(caplog):
    runner = runtime.AssignmentRunner(
        SimpleNamespace(), SimpleNamespace(store=None), config=runtime.RunnerConfig(tick_seconds=0),
    )
    retry = asyncio.Event()
    attempts = 0

    async def tick():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("failure detail must remain private")
        retry.set()

    runner.tick = tick
    runner.start()
    with pytest.raises(RuntimeError, match="already started"):
        runner.start()
    await retry.wait()
    runner.notify()
    await runner.stop()
    assert attempts >= 2 and "persistent_assignment_tick_failed" in caplog.text
    assert "failure detail" not in caplog.text


@pytest.mark.parametrize("stale", [False, True])
async def test_recovery_never_runs_an_episode_and_delivers_terminal_results(monkeypatch, stale):
    binding = {"operation_id": str(uuid4()), "execution_generation": 1, "execution_lease_token": str(uuid4())}
    record = SimpleNamespace(owner_id="a", assignment_id=str(uuid4()), state_version=4,
                             execution_profile="one_shot", lifecycle="completed",
                             operation={"terminal_outcome": "failed"})
    held = SimpleNamespace(execution_profile="one_shot", lifecycle="active")
    repo = Mock()
    repo.recover_expired_for_administration.return_value = SimpleNamespace(operation_bindings=(), terminal_assignments=())
    repo.recover_expired_operations_for_administration.return_value = SimpleNamespace(operation_bindings=(binding,),
        terminal_assignments=(record,))
    repo.retire_unleased_assignments.return_value = (held, record)
    tx = object()
    async def transaction(callback, **kwargs):
        assert kwargs == {"bound_session_waits": True}
        return callback(tx, repo)
    service = SimpleNamespace(store=SimpleNamespace(transaction=transaction))
    orch = SimpleNamespace(work_admission=Mock())
    if stale:
        orch.work_admission.terminalize.side_effect = StaleExecutionFenceError("expired")
    runner = runtime.AssignmentRunner(orch, service)
    fire = Mock()
    redrive = AsyncMock()
    monkeypatch.setattr(runtime, "fire_completions", fire)
    monkeypatch.setattr(runtime, "redrive_pending", redrive)
    await runner.tick()
    orch.work_admission.terminalize.assert_called_once()
    repo.retire_unleased_assignments.assert_called_once_with(tx, reason="turn_engine_retired", limit=100)
    assert len(fire.call_args_list) == 2
    for call in fire.call_args_list:
        assert call.args == (service, tx, repo)
        assert call.kwargs == dict(owner_id="a", source_id=record.assignment_id, source_revision=4, outcome="failed")
    redrive.assert_awaited_once_with(service)
