"""Work controls use real Plane CAS/receipts and preserve unresolved effects."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentActionIntent,
    AssignmentActionOutcome,
    AssignmentOperationBinding,
    AssignmentResourceAmount,
)
from astralplane.repositories.history import SessionExecutionObservation
from astralplane.repositories.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionRepository,
)

from tests.helpers.work_control_caller import current_control_caller
from tests.test_request_session_authority_088 import signing_key as signing_key

from audit.repository import AuditRepository
from orchestrator.work_controls import WorkControlRequest, WorkControlService, WorkDeleteRequest
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest
from tests import test_work_service_postgres_088 as read_fixtures
from persistent_agents.tests.test_engine_postgres import plane as plane

OWNER = {"sub": "owner"}
read_records = read_fixtures.records


@pytest.fixture
async def records(read_records, monkeypatch, signing_key):
    runtime, reads, _identities, _legacy = read_records
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-work-controls-contract-key")
    reads.assignments.orch.audit_repo = AuditRepository(plane_runtime=runtime)
    async with current_control_caller(reads.assignments, monkeypatch, signing_key) as caller:
        reads.control_caller = caller
        yield read_records


def command(revision, submission=None):
    return WorkControlRequest(expected_revision=revision, submission_id=submission or str(uuid4()))


@pytest.mark.asyncio
async def test_owner_controls_replay_stale_and_terminal_delete_contract(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    identity = ids[0]
    request = command(1)
    paused = await service.control("owner", reads.control_caller.context.claims, identity, "pause", request, caller=reads.control_caller)
    assert paused["applied"] is True and paused["operation"]["disposition"] == "paused"
    duplicate = await service.control("owner", reads.control_caller.context.claims, identity, "pause", request, caller=reads.control_caller)
    assert duplicate == {**paused, "applied": False}
    for changed in (command(1), command(paused["operation"]["revision"], request.submission_id)):
        with pytest.raises(AssignmentError) as error:
            await service.control("owner", reads.control_caller.context.claims, identity, "pause", changed, caller=reads.control_caller)
        assert error.value.status_code == 409
    with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
        await service.control("owner", reads.control_caller.context.claims, identity, "cancel", request, caller=reads.control_caller)
    with pytest.raises(AssignmentError, match="assignment_not_terminal"):
        await service.delete("owner", reads.control_caller.context.claims, identity, WorkDeleteRequest(expected_revision=paused["operation"]["revision"]), caller=reads.control_caller)
    cancellation = command(paused["operation"]["revision"])
    cancelled = await service.control("owner", reads.control_caller.context.claims, identity, "cancel", cancellation, caller=reads.control_caller)
    assert cancelled["operation"]["disposition"] == "cancelled"
    assert (await service.control("owner", reads.control_caller.context.claims, identity, "cancel", cancellation, caller=reads.control_caller))["applied"] is False
    old_pause = await service.control("owner", reads.control_caller.context.claims, identity, "pause", request, caller=reads.control_caller)
    assert old_pause == {**cancelled, "applied": False}
    with pytest.raises(AssignmentError, match="assignment_revision_conflict"):
        await service.delete("owner", reads.control_caller.context.claims, identity, WorkDeleteRequest(expected_revision=1), caller=reads.control_caller)
    delete = WorkDeleteRequest(expected_revision=cancelled["operation"]["revision"])
    assert await service.delete("owner", reads.control_caller.context.claims, identity, delete, caller=reads.control_caller) == {"id": identity, "deleted": True}
    with pytest.raises(AssignmentError, match="work_not_found") as missing:
        await service.delete("owner", reads.control_caller.context.claims, identity, delete, caller=reads.control_caller)
    assert missing.value.status_code == 404
    # Original submission identity survives deletion: retry cannot create a new effect.
    with pytest.raises(AssignmentError, match="assignment_operation_deleted"):
        await reads.store.call("get_operation_receipt", owner_id="owner", origin_namespace="web",
                               caller_key=identity, command_digest=digest(identity))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["pause", "cancel", "delete"])
async def test_foreign_legacy_absent_and_invalid_ids_share_404_without_mutation(records, method):
    _, reads, ids, legacy = records
    service = WorkControlService(reads.assignments)
    for identity in (ids[2], legacy, str(uuid4()), "not-an-id"):
        with pytest.raises(AssignmentError) as failure:
            if method == "delete":
                await service.delete("owner", reads.control_caller.context.claims, identity, WorkDeleteRequest(expected_revision=1), caller=reads.control_caller)
            else:
                await service.control("owner", reads.control_caller.context.claims, identity, method, command(1), caller=reads.control_caller)
        assert (failure.value.code, failure.value.status_code) == ("work_not_found", 404)
    assert (await reads.get("other", {"sub": "other"}, ids[2]))["revision"] == 1
    assert (await reads.get("owner", OWNER, ids[0]))["revision"] == 1


@pytest.mark.asyncio
async def test_concurrent_pause_cancel_has_one_linearized_control(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    results = await asyncio.gather(
        service.control("owner", reads.control_caller.context.claims, ids[0], "pause", command(1), caller=reads.control_caller),
        service.control("owner", reads.control_caller.context.claims, ids[0], "cancel", command(1), caller=reads.control_caller), return_exceptions=True)
    assert sum(isinstance(value, dict) and value["applied"] is True for value in results) == 1
    errors = [value for value in results if isinstance(value, AssignmentError)]
    assert len(errors) == 1 and errors[0].status_code == 409
    assert (await reads.get("owner", OWNER, ids[0]))["revision"] == 2


@pytest.mark.asyncio
async def test_concurrent_exact_retry_changes_control_only_once(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    request = command(1)
    results = await asyncio.gather(*(service.control("owner", reads.control_caller.context.claims, ids[0], "pause", request, caller=reads.control_caller) for _ in range(2)))
    assert sorted(value["applied"] for value in results) == [False, True]
    assert (await reads.get("owner", OWNER, ids[0]))["revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("member", ["operation", "operation_control", "checkpoint"])
async def test_unknown_operation_can_cancel_without_interpreting_or_rewriting_payload(records, member):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)

    def future(tx, repository):
        # Test-only future writer in this disposable schema, never application SQL.
        path = {"operation": ["operation"], "operation_control": ["operation", "control"],
                "checkpoint": ["checkpoint"]}[member]
        value = ('{"schema_version":2,"private":"opaque"}' if member == "checkpoint" else
                 '{"version":3,"private":"opaque authority and payload"}' if member == "operation" else
                 '{"version":2,"private":"opaque authority and payload"}')
        tx.execute("UPDATE persistent_assignment SET data=jsonb_set(data,%s,%s::jsonb) WHERE id=%s",
                   (path, value, ids[0]))
    await reads.store.transaction(future)
    before = await reads.store.call("get_operation", owner_id="owner", assignment_id=ids[0])
    with pytest.raises(AssignmentError, match="assignment_version_unsupported"):
        await service.control("owner", reads.control_caller.context.claims, ids[0], "pause", command(1), caller=reads.control_caller)
    request = command(1)
    cancelled = await service.control("owner", reads.control_caller.context.claims, ids[0], "cancel", request, caller=reads.control_caller)
    assert cancelled["operation"]["disposition"] == "cancelled"
    assert cancelled["operation"]["schema_supported"] is False and "private" not in str(cancelled)
    after = await reads.store.call("get_operation", owner_id="owner", assignment_id=ids[0])
    assert after.assignment.operation == before.assignment.operation
    assert after.assignment.checkpoint == before.assignment.checkpoint
    assert (await service.control("owner", reads.control_caller.context.claims, ids[0], "cancel", request, caller=reads.control_caller))["applied"] is False


async def issued_fixture(reads, identity):
    """Issue a synthetic liability through real session/admission/action guards.

    The stored incarnation and database-clock observation are genuine, but this
    fixture does not refresh or verify external IAM and enables no host runner.
    """
    sessions = reads.store.plane_runtime.repositories.history.sessions
    work = WorkAdmissionRepository()
    configs = (
        AdmissionClassConfig(AdmissionClass.GLOBAL, None, 10, 0, 0, "work-control-fixture"),
        AdmissionClassConfig(AdmissionClass.BACKGROUND, AdmissionClass.GLOBAL,
                             10, 0, 0, "work-control-fixture"),
    )
    await reads.store.transaction(lambda tx, _: work.configure(tx, configs))
    # Publish in-memory config only after the repository transaction commits.
    work.bind_configs(configs)

    def issue(tx, repo):
        record = repo.get_operation(tx, owner_id="owner", assignment_id=identity).assignment
        incarnation = record.operation["authority"]["reference_id"]
        session = sessions.get_by_incarnation(tx, owner_id="owner", incarnation_id=incarnation)
        assert session is not None
        state = sessions.get_execution_state(tx, owner_id="owner", session_id=session.session_id)
        assert state is not None and state.credential.incarnation_id == incarnation
        authority = SessionExecutionObservation(
            state.credential, state.observed_at, state.observed_at + timedelta(seconds=15))
        claim = repo.claim_operation_for_administration(
            tx, owner_id="owner", assignment_id=identity,
            expected_state_version=record.state_version, worker_id="work-control-fixture",
            lease_seconds=30, authority=authority)
        accepted = work.submit(tx, OperationRequest(
            operation_kind="assignment_episode", admission_class=AdmissionClass.BACKGROUND,
            owner=OperationOwner(OwnerScope.USER, "owner", None), submission_id=uuid4(),
            idempotency_namespace=None, idempotency_key=None, normalized_input_digest=None,
            chat_id=None, parent_operation_id=None, connection_generation=None,
            request_generation=None), now=None, retention=timedelta(days=1),
            slot_lease=timedelta(minutes=1))
        selected = work.claim_operation(
            tx, AdmissionClass.BACKGROUND, accepted.operation_id, now=None,
            retention=timedelta(days=1), slot_lease=timedelta(minutes=1))
        assert selected is not None
        binding = AssignmentOperationBinding(
            str(selected.fence.operation_id), selected.fence.execution_generation,
            str(selected.fence.execution_lease_token))
        repo.bind_operation(tx, fence=claim.fence, binding=binding)
        request = {"kind": "tool", "agent_id": "fixture", "tool_name": "effect", "arguments": {}}
        action = repo.put_action_for_execution(
            tx, fence=claim.fence, binding=binding, authority=authority, intent=AssignmentActionIntent(
            "synthetic-effect", request, digest(request), AssignmentResourceAmount(tool_calls=1, elapsed_ms=100),
            digest("permission"), digest("precondition"), boundary="unreplayable"))
        attempt = str(uuid4())
        repo.reserve_action_for_execution(
            tx, fence=claim.fence, binding=binding, authority=authority,
            action_id=action.action_id, attempt_id=attempt,
            expected_request_digest=action.intent.request_digest, maximum=action.intent.maximum)
        permit = repo.start_action_for_execution(
            tx, fence=claim.fence, authority=authority, action_id=action.action_id, attempt_id=attempt,
            expected_request_digest=action.intent.request_digest, current_permission_digest=digest("permission"),
            current_precondition_digest=digest("precondition"), binding=binding)
        return action, attempt, permit
    return await reads.store.transaction(issue)


@pytest.mark.asyncio
async def test_cancel_preserves_issued_effect_and_delete_requires_actual_settlement(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    action, attempt, permit = await issued_fixture(reads, ids[0])
    revision = (await reads.get("owner", OWNER, ids[0]))["revision"]
    cancelled = await service.control("owner", reads.control_caller.context.claims, ids[0], "cancel", command(revision), caller=reads.control_caller)
    assert cancelled["operation"]["usage"]["outstanding"]["tool_calls"] == 1
    with pytest.raises(AssignmentError, match="assignment_action_uncertain"):
        await service.delete("owner", reads.control_caller.context.claims, ids[0], WorkDeleteRequest(expected_revision=cancelled["operation"]["revision"]), caller=reads.control_caller)
    retained = await reads.store.call("get_action", owner_id="owner", assignment_id=ids[0], action_id=action.action_id)
    assert retained.state == "started" and retained.ever_started
    await reads.store.call("record_action_outcome", owner_id="owner", assignment_id=ids[0],
        action_id=action.action_id, attempt_id=attempt, dispatch_token=permit.dispatch_token,
        expected_request_digest=action.intent.request_digest,
        outcome=AssignmentActionOutcome("succeeded", digest({}), {}, actual=AssignmentResourceAmount(tool_calls=1, elapsed_ms=10)))
    settled = await reads.get("owner", OWNER, ids[0])
    assert settled["usage"]["spent"]["tool_calls"] == 1
    assert (await service.delete("owner", reads.control_caller.context.claims, ids[0], WorkDeleteRequest(expected_revision=settled["revision"]), caller=reads.control_caller))["deleted"] is True


@pytest.mark.asyncio
async def test_opaque_action_and_retired_owner_cannot_be_erased_or_resumed(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    action, _, _ = await issued_fixture(reads, ids[0])

    def future(tx, repo):
        tx.execute("UPDATE persistent_assignment_action SET data=jsonb_set(data,'{intent,transient_input}',%s::jsonb) WHERE id=%s",
                   ('{"version":2,"private":"opaque"}', action.action_id))
    await reads.store.transaction(future)
    before = await reads.get("owner", OWNER, ids[0])
    cancelled = await service.control("owner", reads.control_caller.context.claims, ids[0], "cancel", command(before["revision"]), caller=reads.control_caller)
    with pytest.raises(AssignmentError, match="assignment_action_uncertain"):
        await service.delete("owner", reads.control_caller.context.claims, ids[0], WorkDeleteRequest(expected_revision=cancelled["operation"]["revision"]), caller=reads.control_caller)
    retirement = await reads.store.call("retire_operations_for_owner", owner_id="owner")
    assert ids[0] in retirement.retained_assignment_ids
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service.control("owner", reads.control_caller.context.claims, ids[0], "resume", command(cancelled["operation"]["revision"]), caller=reads.control_caller)
