"""Real-PostgreSQL continuity regressions for orchestrator/work_continuity.py: a wake
delivery lost to node loss is recovered by the owner's recheck through the guarded wake
fence, while live sources, delivered wakes, stale revisions, and missing sources stay
honest without duplication or false success.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from astralplane.repositories.assignments import AssignmentEpisodeCompletion

from orchestrator.work_admission import OperationState, WorkAdmissionCoordinator
from orchestrator.work_continuity import WorkContinuityService
from orchestrator.work_controls import WorkControlRequest
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest

from tests.test_work_admission_api_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
)
from tests.test_work_completion_wake_postgres_088 import (
    admission_fences as admission_fences, complete_source as complete_source,
    key_of as key_of, paired as paired, subscribe as subscribe,
)
from tests.test_work_continuation_authority_088 import current
from tests.test_work_resume_postgres_088 import caller as caller

runtime = plane
pytestmark = pytest.mark.asyncio


def service_for(api):
    return WorkContinuityService(api.service.assignments)


def body(record, **changes):
    fields = {"expected_revision": record.state_version, "submission_id": str(uuid4())}
    fields.update(changes)
    return WorkControlRequest(**fields)


async def continuity_audits(api, owner):
    rows, _ = api.service.audit.list_for_user(owner)
    return [row for row in rows if row.action_type == "assignment_continuity"]


async def complete_without_wake(api, source):
    claim_fence, operation_fence = await admission_fences(api, source)
    if getattr(api.orch, "work_admission", None) is None:
        api.orch.work_admission = WorkAdmissionCoordinator.from_plane(plane_runtime=api.runtime)
    executor = SimpleNamespace(claim=SimpleNamespace(fence=claim_fence),
                               operation_fence=operation_fence)

    def complete(tx, repository):
        fresh = repository.get_operation(tx, owner_id=source.owner_id,
                                         assignment_id=source.assignment_id).assignment
        completion = AssignmentEpisodeCompletion(
            expected_state_version=fresh.state_version, checkpoint=fresh.checkpoint,
            completion_digest=digest(["continuity-fixture"]), phase="waiting",
            wake_reason="research_completed", completed=True, terminal_outcome="completed")
        result = repository.finish_episode(tx, fence=executor.claim.fence, completion=completion)
        api.orch.work_admission.terminalize(executor.operation_fence,
            state=OperationState.COMPLETED, terminal_code=None, safe_summary=None,
            retry_after_ms=None, transaction=tx)
        return result

    return await api.service.store.transaction(complete, bound_session_waits=True)


async def test_recheck_releases_a_wait_lost_to_node_loss(api):
    source, waiter = await paired(api)
    proposal, confirmed = await subscribe(api, waiter, source)
    assert confirmed["applied"] is True
    await complete_without_wake(api, source)
    stuck = current(api.runtime, waiter)
    assert stuck.phase == "awaiting_event" and stuck.lifecycle == "active"
    assert current(api.runtime, source).operation["terminal_outcome"] == "completed"

    request = body(stuck)
    result = await service_for(api).recheck(
        waiter.assignment_id, request, caller=await caller(api, api.fixture[2]))

    assert result["applied"] is True
    assert result["operation"]["id"] == stuck.assignment_id
    assert result["continuation"] is None
    awake = current(api.runtime, waiter)
    assert awake.phase == "waiting" and awake.state_version == stuck.state_version + 1
    assert awake.operation["control"]["wait"] is None
    audits = await continuity_audits(api, waiter.owner_id)
    assert len(audits) == 1
    assert audits[0].outputs_meta["submission_id"] == request.submission_id
    assert api.service.audit.verify_chain(waiter.owner_id) is None


async def test_recheck_reports_an_already_delivered_wake_without_mutation(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    await complete_source(api, source)
    awake = current(api.runtime, waiter)

    result = await service_for(api).recheck(
        waiter.assignment_id, body(awake), caller=await caller(api, api.fixture[2]))

    assert result["applied"] is False
    assert result["continuation"] is None
    assert current(api.runtime, waiter).state_version == awake.state_version
    assert not await continuity_audits(api, waiter.owner_id)


async def test_recheck_waits_honestly_while_the_source_is_live(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    waiting = current(api.runtime, waiter)

    result = await service_for(api).recheck(
        waiter.assignment_id, body(waiting), caller=await caller(api, api.fixture[2]))

    assert result["applied"] is False
    view = result["continuation"]
    assert view["source_operation_id"] == source.assignment_id
    assert view["source_disposition"] == "queued"
    assert view["source_terminal_outcome"] is None
    assert view["awaiting_event"] is True
    unchanged = current(api.runtime, waiter)
    assert unchanged.state_version == waiting.state_version
    assert unchanged.phase == "awaiting_event"
    assert not await continuity_audits(api, waiter.owner_id)


async def test_recheck_fences_a_stale_revision(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)

    with pytest.raises(AssignmentError, match="assignment_revision_conflict"):
        await service_for(api).recheck(
            waiter.assignment_id, body(waiter), caller=await caller(api, api.fixture[2]))

    assert current(api.runtime, waiter).phase == "awaiting_event"
    assert not await continuity_audits(api, waiter.owner_id)


async def test_recheck_reports_a_missing_source_without_waking(api):
    source, waiter = await paired(api)
    await api.service.store.call("set_owner_event_wait", owner_id=waiter.owner_id,
        assignment_id=waiter.assignment_id,
        expected_instruction_revision=waiter.instruction_revision,
        expected_control_epoch=waiter.control_epoch,
        expected_state_version=waiter.state_version, submission_id=str(uuid4()),
        submission_digest=digest("continuity-fixture-missing"),
        event_key="completion:" + str(uuid4()), source_revision=0)
    drifted = current(api.runtime, waiter)

    with pytest.raises(AssignmentError, match="work_completion_source_missing"):
        await service_for(api).recheck(
            waiter.assignment_id, body(drifted), caller=await caller(api, api.fixture[2]))

    assert current(api.runtime, waiter).phase == "awaiting_event"
    assert not await continuity_audits(api, waiter.owner_id)


async def test_recheck_refuses_a_non_control_body(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    waiting = current(api.runtime, waiter)

    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).recheck(
            waiter.assignment_id, object(), caller=await caller(api, api.fixture[2]))

    assert current(api.runtime, waiter).state_version == waiting.state_version


async def test_recheck_refuses_unsupported_operation_versions(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    waiting = current(api.runtime, waiter)
    with api.runtime.transaction() as tx:
        tx.execute("UPDATE persistent_assignment SET "
                   "data=jsonb_set(data,'{operation,version}','3'::jsonb) WHERE id=%s",
                   (waiter.assignment_id,))

    with pytest.raises(AssignmentError, match="assignment_version_unsupported"):
        await service_for(api).recheck(
            waiter.assignment_id, body(waiting), caller=await caller(api, api.fixture[2]))

    assert current(api.runtime, waiter).phase == "awaiting_event"


async def test_recheck_repeat_after_fire_is_an_honest_no_op(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    await complete_without_wake(api, source)
    stuck = current(api.runtime, waiter)
    request = body(stuck)
    first = await service_for(api).recheck(
        waiter.assignment_id, request, caller=await caller(api, api.fixture[2]))
    assert first["applied"] is True

    repeat = await service_for(api).recheck(
        waiter.assignment_id, request, caller=await caller(api, api.fixture[2]))

    assert repeat["applied"] is False
    assert current(api.runtime, waiter).state_version == stuck.state_version + 1
    assert len(await continuity_audits(api, waiter.owner_id)) == 1
