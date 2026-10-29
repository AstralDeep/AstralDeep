"""Ordinary durable admission preserves original replay before current rollout/config assertions.
New work uses the same transaction's owner, preference, config and normal admission guards.
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from orchestrator.harness.production_profiles import HarnessSelectionError
from orchestrator.harness.selection_service import HarnessSelectionService
from orchestrator.work_admission import (
    AdmissionClass, OperationOwner, OperationRequest, OwnerScope, WorkAdmissionCoordinator,
)
from persistent_agents.tests.test_engine_postgres import plane as plane


def request():
    return OperationRequest("connection_frame", AdmissionClass.INTERACTIVE,
        OperationOwner(OwnerScope.CONNECTION, None, uuid4()), uuid4(),
        None, None, None, None, None, uuid4(), uuid4())


def test_submission_replay_ignores_new_selection_but_new_work_keeps_current_fence(plane):
    service = HarnessSelectionService(plane_runtime=plane)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=plane)
    prepared = service.prepare_sync("owner-a")
    selected_request = request()
    original = coordinator.submit_with_harness(selected_request, service=service,
        prepared=prepared, owner_id="owner-a")
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
    replay = coordinator.submit_with_harness(selected_request, service=service,
        prepared=prepared, owner_id="owner-a")
    assert replay.operation_id == original.operation_id
    with pytest.raises(HarnessSelectionError, match="harness_selection_conflict"):
        coordinator.submit_with_harness(replace(selected_request, submission_id=uuid4()),
            service=service, prepared=prepared, owner_id="owner-a")
    with plane.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 1


def test_idempotency_conflict_remains_normal_outcome_without_reselecting(plane):
    service = HarnessSelectionService(plane_runtime=plane)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=plane)
    prepared = service.prepare_sync("owner-a")
    selected_request = replace(request(), idempotency_namespace="ordinary", idempotency_key="same",
                               normalized_input_digest="c" * 64)
    original = coordinator.submit_with_harness(selected_request, service=service,
        prepared=prepared, owner_id="owner-a")
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
    conflicting = replace(selected_request, submission_id=uuid4(), normalized_input_digest="d" * 64)
    refused = coordinator.submit_with_harness(conflicting, service=service,
        prepared=prepared, owner_id="owner-a")
    assert refused.accepted is False and refused.code == "idempotency_conflict"
    same = coordinator.submit_with_harness(replace(selected_request, submission_id=uuid4()),
        service=service, prepared=prepared, owner_id="owner-a")
    assert same.operation_id == original.operation_id


def test_owner_mismatch_cannot_bind_another_prepared_capture(plane):
    service = HarnessSelectionService(plane_runtime=plane)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=plane)
    prepared = service.prepare_sync("owner-a")
    with pytest.raises(HarnessSelectionError, match="harness_owner_invalid"):
        coordinator.submit_with_harness(request(), service=service, prepared=prepared,
                                        owner_id="other")


def host(plane):
    from orchestrator.orchestrator import Orchestrator, ConnectionContext
    import json
    orch = Orchestrator.__new__(Orchestrator)
    from llm_config.types import LLMUnavailable
    orch._LLMUnavailable = LLMUnavailable
    socket = object()
    context = ConnectionContext(socket, uuid4(), 0, connection_generation=uuid4(), registered=True)
    orch._connection_contexts = {id(socket): context}
    orch.ui_sessions = {socket: {"sub": "owner-a"}}
    orch.work_admission = WorkAdmissionCoordinator.from_plane(plane_runtime=plane)
    orch.harness_selection = HarnessSelectionService(plane_runtime=plane)
    parsed = {"type": "ui_event", "action": "chat_message", "submission_id": str(uuid4()),
        "request_generation": str(uuid4()), "connection_generation": str(context.connection_generation),
        "session_id": str(uuid4()), "payload": {"message": "bounded"}}
    frame = orch._connection_frame(context, json.dumps(parsed), parsed)
    assert frame is not None
    return orch, context, frame


def test_actual_connection_admission_denies_selected_flag_off(plane):
    orch, context, frame = host(plane)
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
    result = orch._submit_connection_batch(context, [frame])[0][2]
    assert isinstance(result, HarnessSelectionError) and result.code == "harness_selection_disabled"
    with plane.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 0


def test_owner_change_during_capture_cannot_admit_the_previous_owner(plane, monkeypatch):
    orch, context, frame = host(plane)
    original = orch.harness_selection.prepare_sync
    def replaced(owner, *, transaction=None):
        prepared = original(owner, transaction=transaction)
        orch.ui_sessions[context.websocket] = {"sub": "other"}
        return prepared
    monkeypatch.setattr(orch.harness_selection, "prepare_sync", replaced)
    result = orch._submit_connection_batch(context, [frame])[0][2]
    assert isinstance(result, HarnessSelectionError) and result.code == "harness_owner_invalid"
    with plane.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 0


def test_assignment_capture_is_checked_before_grant_or_assignment_mutation(plane):
    from types import SimpleNamespace
    from persistent_agents.service import AssignmentService
    from persistent_agents.store import AssignmentStore
    from unittest.mock import Mock
    harness = HarnessSelectionService(plane_runtime=plane)
    prepared = harness.prepare_sync("owner-a")
    grants = SimpleNamespace(capture_in_transaction=Mock(), assert_current_capture=Mock())
    service = AssignmentService(SimpleNamespace(harness_selection=harness, offline_grants=grants),
                                AssignmentStore(plane_runtime=plane), enabled=True)
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=None, qualification_digest=None)
    with pytest.raises(HarnessSelectionError, match="harness_selection_conflict"):
        with plane.transaction() as tx:
            service._consented_mutation(tx, service.store.repository, "create_assignment", "create",
                SimpleNamespace(grant_id="unused"), {"owner_id": "owner-a", "assignment_id": str(uuid4()),
                    "submission_id": str(uuid4()), "submission_digest": "a" * 64}, harness=prepared)
    grants.capture_in_transaction.assert_not_called()


def test_runtime_capture_checks_current_owner_and_execution_incarnation(plane):
    from orchestrator.work_admission import StaleExecutionFenceError
    harness = HarnessSelectionService(plane_runtime=plane)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=plane)
    selected_request = request()
    admitted = coordinator.submit_with_harness(selected_request, service=harness,
        prepared=harness.prepare_sync("owner-a"), owner_id="owner-a")
    claim = coordinator.claim_operation(AdmissionClass.INTERACTIVE, admitted.operation_id)
    assert coordinator.harness_capture(owner=selected_request.owner, fence=claim.fence,
        service=harness, owner_id="owner-a") is None
    with pytest.raises(StaleExecutionFenceError):
        coordinator.harness_capture(owner=selected_request.owner,
            fence=replace(claim.fence, execution_lease_token=uuid4()), service=harness, owner_id="owner-a")
    with pytest.raises(Exception):
        coordinator.harness_capture(owner=OperationOwner(OwnerScope.CONNECTION, None, uuid4()),
            fence=claim.fence, service=harness, owner_id="owner-a")


@pytest.mark.asyncio
async def test_actual_model_path_denies_unavailable_captured_artifact_before_provider_resolution(plane):
    from astralplane.repositories.harness_capture import HarnessCapture
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT
    from orchestrator.work_admission import _to_plane_request
    from unittest.mock import AsyncMock
    orch, context, _ = host(plane)
    selected_request = request()
    selected_request = replace(selected_request, owner=OperationOwner(OwnerScope.CONNECTION, None,
                                                                     context.connection_scope_id))
    with plane.transaction() as tx:
        selection = plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
        captured = HarnessCapture("owner-a", 1, selection.identity, "b" * 64,
                                   "local-page-selection/v1", "synthetic", "c" * 64)
        converted = replace(_to_plane_request(selected_request), harness_selection=selection,
                            harness_capture=captured)
        admitted = plane.repositories.work_admission.submit(tx, converted, now=None,
            retention=orch.work_admission.operation_retention, slot_lease=orch.work_admission.slot_lease)
    claim = orch.work_admission.claim_operation(AdmissionClass.INTERACTIVE, admitted.operation_id)
    orch._resolve_llm_client_for = AsyncMock(side_effect=AssertionError("provider resolution invoked"))
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": claim.operation,
        "owner": selected_request.owner, "execution_fence": claim.fence})
    try:
        with pytest.raises(HarnessSelectionError, match="harness_qualification_missing"):
            await orch._call_llm(context.websocket, [{"role": "user", "content": "bounded"}])
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    orch._resolve_llm_client_for.assert_not_called()


def test_only_server_retry_observation_inherits_default_after_rollout_change(plane):
    from orchestrator.work_admission import OperationState
    harness = HarnessSelectionService(plane_runtime=plane)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=plane)
    original_request = replace(request(), owner=OperationOwner(OwnerScope.USER, "owner-a", None),
        idempotency_namespace="agent_runtime_delivery", idempotency_key="original",
        normalized_input_digest="a" * 64)
    admitted = coordinator.submit_with_harness(original_request, service=harness,
        prepared=harness.prepare_sync("owner-a"), owner_id="owner-a")
    claim = coordinator.claim_operation(AdmissionClass.INTERACTIVE, admitted.operation_id)
    coordinator.terminalize(claim.fence, state=OperationState.RETRYABLE,
                            terminal_code="host_lost", safe_summary="Unavailable", retry_after_ms=0)
    observed = coordinator.observe_harness_retry(owner=original_request.owner,
        operation_id=admitted.operation_id, service=harness, owner_id="owner-a")
    child = replace(original_request, submission_id=uuid4(), parent_operation_id=admitted.operation_id,
                    idempotency_key="retry:" + str(admitted.operation_id))
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
    accepted = coordinator.submit_harness_retry(child, observation=observed, service=harness,
                                                owner_id="owner-a")
    assert accepted.accepted and accepted.operation_id != admitted.operation_id
    replay = coordinator.submit_harness_retry(replace(child, submission_id=uuid4()),
        observation=observed, service=harness, owner_id="owner-a")
    assert replay.operation_id == accepted.operation_id
    with pytest.raises(HarnessSelectionError, match="harness_selection_disabled"):
        harness.prepare_sync("owner-a")
    with pytest.raises(HarnessSelectionError):
        coordinator.submit_harness_retry(replace(child, submission_id=uuid4(), idempotency_key="unrelated"),
            observation=observed, service=harness, owner_id="owner-a")


def test_actual_connection_submission_replay_does_not_reselect_after_flag_off(plane):
    orch, context, frame = host(plane)
    original = orch._submit_connection_batch(context, [frame])[0][2]
    assert original.accepted
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
    replay = orch._submit_connection_batch(context, [frame])[0][2]
    assert replay.operation_id == original.operation_id


@pytest.mark.asyncio
async def test_actual_persistent_episode_rejects_detached_capture_not_in_its_parent(plane, monkeypatch):
    from persistent_agents.tests.test_engine_postgres import _engine
    from astralplane.repositories.harness_capture import HarnessCapture
    from orchestrator.work_admission import WorkAdmissionConflictError
    engine = _engine(plane, monkeypatch, seed_prior=False)
    host, runner, store, _ = next(engine)
    try:
        [claim] = await store.call("claim_due_for_administration", worker_id=runner.worker_id, lease_seconds=5)
        captured = HarnessCapture("owner", 1, HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            "b" * 64, "local-page-selection/v1", "synthetic", "c" * 64)
        with pytest.raises(WorkAdmissionConflictError):
            await runner._admit(replace(claim, assignment=replace(claim.assignment, harness_capture=captured)))
        with plane.transaction() as tx:
            assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 0
    finally:
        next(engine, None)


@pytest.mark.asyncio
async def test_real_bounded_server_retry_producer_preserves_the_original_default(plane):
    from orchestrator.work_admission import OperationState
    orch, _, _ = host(plane)
    kwargs = dict(owner_user_id="owner-a", operation_kind="agent_runtime_delivery",
        idempotency_namespace="agent_runtime_delivery", idempotency_key="same-delivery",
        normalized_identity={"kind": "synthetic-delivery"}, admission_class=AdmissionClass.INTERACTIVE,
        retry_terminal=True)
    owner, original = await orch._claim_personal_agent_operation(**kwargs)
    orch.work_admission.terminalize(original.fence, state=OperationState.RETRYABLE,
        terminal_code="host_lost", safe_summary="Unavailable", retry_after_ms=0)
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
            expected_revision=0, identity=HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
            qualification_digest="b" * 64)
    child_owner, child = await orch._claim_personal_agent_operation(**kwargs)
    assert child_owner == owner and child.operation.parent_operation_id == original.operation.operation_id
    assert child.operation.operation_id != original.operation.operation_id
    with plane.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 2


@pytest.mark.parametrize("change", ["unregistered", "closing", "generation", "context", "claims", "origin"])
def test_current_connection_incarnation_is_required_at_admission(plane, change):
    orch, context, frame = host(plane)
    if change == "unregistered":
        context.registered = False
    elif change == "closing":
        context.closing = True
    elif change == "generation":
        context.connection_generation = None
    elif change == "context":
        orch._connection_contexts.clear()
    elif change == "claims":
        orch.ui_sessions.clear()
    else:
        frame.authenticated_user_id = "other"
    result = orch._submit_connection_batch(context, [frame])[0][2]
    assert isinstance(result, HarnessSelectionError) and result.code == "harness_owner_invalid"


def test_verified_opaque_subject_is_not_confused_with_missing_owner_fallback(plane):
    orch, context, frame = host(plane)
    orch.ui_sessions[context.websocket] = {"sub": "legacy"}
    import json
    frame = orch._connection_frame(context, json.dumps(frame.parsed), frame.parsed)
    admitted = orch._submit_connection_batch(context, [frame])[0][2]
    assert admitted.accepted
    missing = replace(frame, submission_id=uuid4())
    orch.ui_sessions[context.websocket] = {}
    denied = orch._submit_connection_batch(context, [missing])[0][2]
    assert isinstance(denied, HarnessSelectionError) and denied.code == "harness_owner_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["fresh_a", "other", "removed", "unchanged"])
async def test_actual_enqueued_frame_keeps_original_registration_before_preference_capture(plane, replacement):
    import asyncio
    import json
    orch, context, _ = host(plane)
    parsed = {"type": "ui_event", "action": "chat_message", "submission_id": str(uuid4()),
        "request_generation": str(uuid4()), "connection_generation": str(context.connection_generation),
        "session_id": str(uuid4()), "payload": {"message": "bounded"}}
    pending = asyncio.Event()
    blocker = asyncio.create_task(pending.wait())
    context.admission_task = blocker
    try:
        await orch._enqueue_connection_frame(context, json.dumps(parsed), parsed, human_lookup_eligible=False)
        [queued] = context.ingress
        with plane.transaction() as tx:
            plane.repositories.preferences.harness.compare_and_set(tx, owner_id="owner-a",
                expected_revision=0, identity=None, qualification_digest=None)
        if replacement == "fresh_a":
            orch.ui_sessions[context.websocket] = {"sub": "owner-a"}
            context.connection_generation = uuid4()
        elif replacement == "other":
            orch.ui_sessions[context.websocket] = {"sub": "other"}
        elif replacement == "removed":
            orch.ui_sessions.pop(context.websocket)
        result = orch._submit_connection_batch(context, [queued])[0][2]
        if replacement == "unchanged":
            assert result.accepted
        else:
            assert isinstance(result, HarnessSelectionError) and result.code == "harness_owner_invalid"
            with plane.transaction() as tx:
                assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 0
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
        for queued in context.ingress:
            queued.close_work_read()
