"""Tests for verification-turn admission (orchestrator/work_admission.py,
verification/drivers/fixture_admission.py) over real Plane: fencing, lease renewal,
capacity refusal, and terminal cleanup on cancellation or failure.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationRequest,
    OperationState,
    PlaneWorkAdmissionRepository,
    WorkAdmissionCoordinator,
)
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from verification.drivers.fixture_admission import (
    FixtureAdmissionError,
    admitted_registered_turn,
)


@pytest.fixture(scope="module")
def database():
    with isolated_plane_runtime("fixture_admission") as runtime:
        yield runtime


@pytest.fixture
def registered(database):
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(AdmissionClassConfig(
            AdmissionClass.INTERACTIVE, None, 1, 1, 5000, "fixture-admission-tests",
        ),),
        repository=PlaneWorkAdmissionRepository(
            plane_runtime=database, plane_repositories=database.repositories,
        ),
        slot_lease=timedelta(seconds=2),
    )
    websocket = object()
    connection = SimpleNamespace(
        registered=True, closing=False, connection_scope_id=uuid4(), connection_generation=uuid4(),
    )
    orch = SimpleNamespace(
        work_admission=coordinator, _connection_contexts={id(websocket): connection},
        ui_sessions={websocket: {"sub": "synthetic-owner"}},
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(
            runtime=database, repositories=database.repositories,
        )),
    )
    frame = {
        "type": "ui_event", "action": "chat_message", "chat_id": "fixture-conversation",
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "connection_generation": str(connection.connection_generation),
    }
    return orch, websocket, frame, object()


@pytest.mark.asyncio
async def test_normal_turn_has_real_fence_and_releases_capacity(registered):
    orch, websocket, frame, pending = registered
    async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as context:
        assert context["human_request"] is pending
        assert context["operation"].state is OperationState.RUNNING
        assert context["operation"].connection_scope_id == context["owner"].connection_scope_id
        assert orch.work_admission.assert_current_execution(context["execution_fence"]).operation_id == context["operation"].operation_id
    result = orch.work_admission.query_operation(
        owner=context["owner"], operation_id=context["operation"].operation_id,
    )
    assert result.state is OperationState.COMPLETED
    assert orch.work_admission.inspect_admission_class(AdmissionClass.INTERACTIVE).active_count == 0


@pytest.mark.asyncio
async def test_body_failure_and_task_cancellation_terminalize(registered):
    orch, websocket, frame, pending = registered
    with pytest.raises(ValueError, match="body failed"):
        async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as context:
            raise ValueError("body failed")
    assert orch.work_admission.query_operation(
        owner=context["owner"], operation_id=context["operation"].operation_id,
    ).state is OperationState.FAILED
    frame = {**frame, "submission_id": str(uuid4())}
    entered = asyncio.Event()
    captured = {}

    async def turn():
        async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as current:
            captured.update(current)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(turn())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert orch.work_admission.query_operation(
        owner=captured["owner"], operation_id=captured["operation"].operation_id,
    ).state is OperationState.CANCELLED
    assert orch.work_admission.inspect_admission_class(AdmissionClass.INTERACTIVE).active_count == 0


@pytest.mark.asyncio
async def test_queued_turn_is_settled_without_claiming_another_operation(registered):
    orch, websocket, frame, pending = registered
    second = {**frame, "submission_id": str(uuid4())}
    async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as first:
        with pytest.raises(FixtureAdmissionError, match="exact admission"):
            async with admitted_registered_turn(orch, websocket, frame=second, human_request=pending):
                pytest.fail("queued work must never run without a fence")
        from uuid import UUID
        reconciled = orch.work_admission.reconcile_submission(
            owner=first["owner"], submission_id=UUID(second["submission_id"]),
        )
        assert reconciled.operation.state is OperationState.RETRYABLE
        assert orch.work_admission.assert_current_execution(first["execution_fence"])


@pytest.mark.asyncio
async def test_claim_failure_settles_preselection(registered, monkeypatch):
    orch, websocket, frame, pending = registered

    def fail(*_args):
        raise RuntimeError("injected claim failure")

    monkeypatch.setattr(orch.work_admission, "claim_operation", fail)
    with pytest.raises(RuntimeError, match="claim failure"):
        async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending):
            pytest.fail("a failed claim must never yield")
    assert orch.work_admission.inspect_admission_class(AdmissionClass.INTERACTIVE).active_count == 0


@pytest.mark.asyncio
async def test_full_real_capacity_refuses_without_fabricating_a_claim(registered):
    orch, websocket, frame, pending = registered
    async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as first:
        queued = orch.work_admission.submit(OperationRequest(
            operation_kind="chat_message", admission_class=AdmissionClass.INTERACTIVE,
            owner=first["owner"], submission_id=uuid4(), idempotency_namespace=None,
            idempotency_key=None, normalized_input_digest=None, chat_id=frame["chat_id"],
            parent_operation_id=None, connection_generation=first["connection_generation"],
            request_generation=uuid4(),
        ))
        try:
            assert queued.state is OperationState.QUEUED
            with pytest.raises(FixtureAdmissionError, match="admission was refused"):
                async with admitted_registered_turn(
                    orch, websocket, frame={**frame, "submission_id": str(uuid4())},
                    human_request=pending,
                ):
                    pytest.fail("full capacity must not yield a manufactured execution")
        finally:
            orch.work_admission.cancel(
                owner=first["owner"], operation_id=queued.operation_id,
                terminal_code="fixture_queued_cleanup",
            )


@pytest.mark.asyncio
async def test_active_turn_renews_real_execution_lease(registered, monkeypatch):
    orch, websocket, frame, pending = registered
    renewed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = orch.work_admission.renew_execution_lease

    def renew(fence):
        result = original(fence)
        loop.call_soon_threadsafe(renewed.set)
        return result

    monkeypatch.setattr(orch.work_admission, "renew_execution_lease", renew)
    async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as context:
        await asyncio.wait_for(renewed.wait(), timeout=10)
        assert orch.work_admission.assert_current_execution(context["execution_fence"])


@pytest.mark.asyncio
async def test_renewal_failure_cancels_execution_and_records_failure(registered, monkeypatch):
    orch, websocket, frame, pending = registered
    cancellations_before = asyncio.current_task().cancelling()

    def fail(_fence):
        raise RuntimeError("injected renewal failure")

    monkeypatch.setattr(orch.work_admission, "renew_execution_lease", fail)
    with pytest.raises(FixtureAdmissionError, match="lease was lost"):
        async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as context:
            await asyncio.sleep(10)
    assert orch.work_admission.query_operation(
        owner=context["owner"], operation_id=context["operation"].operation_id,
    ).state is OperationState.FAILED
    assert asyncio.current_task().cancelling() == cancellations_before


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["coordinator", "graph", "registration", "generation", "frame", "uuid", "uuid1"])
async def test_unregistered_or_unbound_turn_is_refused(registered, mutation):
    orch, websocket, frame, pending = registered
    if mutation == "coordinator":
        orch.work_admission = object()
    elif mutation == "graph":
        orch.runtime_composition.plane.runtime = object()
    elif mutation == "registration":
        orch._connection_contexts[id(websocket)].registered = False
    elif mutation == "generation":
        frame["connection_generation"] = str(uuid4())
    elif mutation == "frame":
        frame["action"] = "other"
    elif mutation == "uuid":
        frame["request_generation"] = "bad"
    else:
        frame["request_generation"] = "00000000-0000-1000-8000-000000000000"
    with pytest.raises(FixtureAdmissionError):
        async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending):
            pytest.fail("invalid registered context must never be admitted")


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_method", ["submit", "claim_operation", "terminalize", "renew_execution_lease"])
async def test_blocking_calls_keep_loop_responsive_and_settle_through_cancellation(
    registered, monkeypatch, blocked_method,
):
    orch, websocket, frame, pending = registered
    coordinator = orch.work_admission
    original = getattr(coordinator, blocked_method)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    observed = {}

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=10):
            raise TimeoutError("test did not release worker thread")
        result = original(*args, **kwargs)
        observed["settled"] = True
        return result

    monkeypatch.setattr(coordinator, blocked_method, blocked)

    async def turn():
        async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending):
            if blocked_method == "renew_execution_lease":
                await asyncio.Event().wait()

    task = asyncio.create_task(turn())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert observed["settled"]
        assert coordinator.inspect_admission_class(AdmissionClass.INTERACTIVE).active_count == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
