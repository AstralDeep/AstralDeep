"""Work controls use real Plane CAS/receipts and preserve unresolved effects."""
import asyncio
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentActionIntent,
    AssignmentActionOutcome,
    AssignmentOperationBinding,
    AssignmentResourceAmount,
)

from orchestrator.work_controls import WorkControlRequest, WorkControlService, WorkDeleteRequest
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest
from tests.test_work_service_postgres_088 import records as records
from persistent_agents.tests.test_engine_postgres import plane as plane

OWNER = {"sub": "owner"}


def command(revision, submission=None):
    return WorkControlRequest(expected_revision=revision, submission_id=submission or str(uuid4()))


@pytest.mark.asyncio
async def test_owner_controls_replay_stale_and_terminal_delete_contract(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    identity = ids[0]
    request = command(1)
    paused = await service.control("owner", OWNER, identity, "pause", request)
    assert paused["applied"] is True and paused["operation"]["disposition"] == "paused"
    duplicate = await service.control("owner", OWNER, identity, "pause", request)
    assert duplicate == {**paused, "applied": False}
    for changed in (command(1), command(paused["operation"]["revision"], request.submission_id)):
        with pytest.raises(AssignmentError) as error:
            await service.control("owner", OWNER, identity, "pause", changed)
        assert error.value.status_code == 409
    with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
        await service.control("owner", OWNER, identity, "cancel", request)
    with pytest.raises(AssignmentError, match="assignment_not_terminal"):
        await service.delete("owner", OWNER, identity, WorkDeleteRequest(expected_revision=paused["operation"]["revision"]))
    cancellation = command(paused["operation"]["revision"])
    cancelled = await service.control("owner", OWNER, identity, "cancel", cancellation)
    assert cancelled["operation"]["disposition"] == "cancelled"
    assert (await service.control("owner", OWNER, identity, "cancel", cancellation))["applied"] is False
    old_pause = await service.control("owner", OWNER, identity, "pause", request)
    assert old_pause == {**cancelled, "applied": False}
    with pytest.raises(AssignmentError, match="assignment_revision_conflict"):
        await service.delete("owner", OWNER, identity, WorkDeleteRequest(expected_revision=1))
    delete = WorkDeleteRequest(expected_revision=cancelled["operation"]["revision"])
    assert await service.delete("owner", OWNER, identity, delete) == {"id": identity, "deleted": True}
    with pytest.raises(AssignmentError, match="work_not_found") as missing:
        await service.delete("owner", OWNER, identity, delete)
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
                await service.delete("owner", OWNER, identity, WorkDeleteRequest(expected_revision=1))
            else:
                await service.control("owner", OWNER, identity, method, command(1))
        assert (failure.value.code, failure.value.status_code) == ("work_not_found", 404)
    assert (await reads.get("other", {"sub": "other"}, ids[2]))["revision"] == 1
    assert (await reads.get("owner", OWNER, ids[0]))["revision"] == 1


@pytest.mark.asyncio
async def test_concurrent_pause_cancel_has_one_linearized_control(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    results = await asyncio.gather(
        service.control("owner", OWNER, ids[0], "pause", command(1)),
        service.control("owner", OWNER, ids[0], "cancel", command(1)), return_exceptions=True)
    assert sum(isinstance(value, dict) and value["applied"] is True for value in results) == 1
    errors = [value for value in results if isinstance(value, AssignmentError)]
    assert len(errors) == 1 and errors[0].status_code == 409
    assert (await reads.get("owner", OWNER, ids[0]))["revision"] == 2


@pytest.mark.asyncio
async def test_concurrent_exact_retry_changes_control_only_once(records):
    _, reads, ids, _ = records
    service = WorkControlService(reads.assignments)
    request = command(1)
    results = await asyncio.gather(*(service.control("owner", OWNER, ids[0], "pause", request) for _ in range(2)))
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
                 '{"version":2,"private":"opaque authority and payload"}')
        tx.execute("UPDATE persistent_assignment SET data=jsonb_set(data,%s,%s::jsonb) WHERE id=%s",
                   (path, value, ids[0]))
    await reads.store.transaction(future)
    before = await reads.store.call("get_operation", owner_id="owner", assignment_id=ids[0])
    with pytest.raises(AssignmentError, match="assignment_version_unsupported"):
        await service.control("owner", OWNER, ids[0], "pause", command(1))
    request = command(1)
    cancelled = await service.control("owner", OWNER, ids[0], "cancel", request)
    assert cancelled["operation"]["disposition"] == "cancelled"
    assert cancelled["operation"]["schema_supported"] is False and "private" not in str(cancelled)
    after = await reads.store.call("get_operation", owner_id="owner", assignment_id=ids[0])
    assert after.assignment.operation == before.assignment.operation
    assert after.assignment.checkpoint == before.assignment.checkpoint
    assert (await service.control("owner", OWNER, ids[0], "cancel", request))["applied"] is False


async def issued_fixture(reads, identity):
    """Seed an issued synthetic liability through Plane; no external call occurs."""
    def issue(tx, repo):
        claim = next(row for row in repo.claim_operations_for_administration(
            tx, worker_id="work-control-fixture", lease_seconds=30) if row.assignment.assignment_id == identity)
        binding = AssignmentOperationBinding(str(uuid4()), 1, str(uuid4()))
        repo.bind_operation(tx, fence=claim.fence, binding=binding)
        request = {"kind": "tool", "agent_id": "fixture", "tool_name": "effect", "arguments": {}}
        action = repo.put_action(tx, fence=claim.fence, intent=AssignmentActionIntent(
            "synthetic-effect", request, digest(request), AssignmentResourceAmount(tool_calls=1, elapsed_ms=100),
            digest("permission"), digest("precondition"), boundary="unreplayable"))
        attempt = str(uuid4())
        repo.reserve_action(tx, fence=claim.fence, action_id=action.action_id, attempt_id=attempt,
            expected_request_digest=action.intent.request_digest, maximum=action.intent.maximum)
        permit = repo.start_action(tx, fence=claim.fence, action_id=action.action_id, attempt_id=attempt,
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
    cancelled = await service.control("owner", OWNER, ids[0], "cancel", command(revision))
    assert cancelled["operation"]["usage"]["outstanding"]["tool_calls"] == 1
    with pytest.raises(AssignmentError, match="assignment_action_uncertain"):
        await service.delete("owner", OWNER, ids[0], WorkDeleteRequest(expected_revision=cancelled["operation"]["revision"]))
    retained = await reads.store.call("get_action", owner_id="owner", assignment_id=ids[0], action_id=action.action_id)
    assert retained.state == "started" and retained.ever_started
    await reads.store.call("record_action_outcome", owner_id="owner", assignment_id=ids[0],
        action_id=action.action_id, attempt_id=attempt, dispatch_token=permit.dispatch_token,
        expected_request_digest=action.intent.request_digest,
        outcome=AssignmentActionOutcome("succeeded", digest({}), {}, actual=AssignmentResourceAmount(tool_calls=1, elapsed_ms=10)))
    settled = await reads.get("owner", OWNER, ids[0])
    assert settled["usage"]["spent"]["tool_calls"] == 1
    assert (await service.delete("owner", OWNER, ids[0], WorkDeleteRequest(expected_revision=settled["revision"])))["deleted"] is True


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
    cancelled = await service.control("owner", OWNER, ids[0], "cancel", command(before["revision"]))
    with pytest.raises(AssignmentError, match="assignment_action_uncertain"):
        await service.delete("owner", OWNER, ids[0], WorkDeleteRequest(expected_revision=cancelled["operation"]["revision"]))
    retirement = await reads.store.call("retire_operations_for_owner", owner_id="owner")
    assert ids[0] in retirement.retained_assignment_ids
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service.control("owner", OWNER, ids[0], "resume", command(cancelled["operation"]["revision"]))
