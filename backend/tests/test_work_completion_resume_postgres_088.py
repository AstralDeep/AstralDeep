"""Tests completion waits across pause and explicit human resume over real PostgreSQL.
Internal aliases and tracked-job waits reconcile only the guarded resumed target.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from astralplane.repositories.tracked_jobs import TrackedJobRecord
from orchestrator import remote_jobs, work_subscriptions
from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.work_continuations import WorkContinuationService, WorkJobWaitRequest, WorkOwnerWaitRequest
from orchestrator.work_controls import WorkControlAudit, WorkControlService, WorkDeleteRequest
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError
from tests.helpers.session_consent_088 import consent_from_store
from tests.test_work_completion_wake_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
    paired, subscribe, complete_source, observation_for,
)
from tests.test_work_continuation_authority_088 import control, current
from tests.test_work_resume_postgres_088 import caller, body, service_for

runtime = plane
pytestmark = pytest.mark.asyncio
KINDS = ("owner", "framework", "remote")


async def parked(api, kind, monkeypatch):
    if kind == "remote":
        target, = await paired(api, count=1)
        source = TrackedJobRecord(
            tracked_job_id=uuid4().hex, owner_id=target.owner_id, machine_id=uuid4().hex,
            scheduler_job_id="173", conversation_id=None, submit_marker=None,
            output_path=None, component_id=None, job_name="resume-job", state="running",
            exit_code=None, terminal=False, notify_on_finish=False, notified=False,
            fail_count=0, created_at=1000, last_polled_at=1000, finished_at=None)
        with api.runtime.transaction() as tx:
            api.runtime.repositories.tracked_jobs.create(tx, source)
        await WorkContinuationService(api.service.assignments).wait_job(
            target.assignment_id, WorkJobWaitRequest(
                expected_revision=target.state_version, submission_id=str(uuid4()),
                tracked_job_id=source.tracked_job_id, machine_id=source.machine_id,
                scheduler_job_id=source.scheduler_job_id), caller=await caller(api, api.fixture[2]))
    else:
        source, target = await paired(api)
        if kind == "owner":
            await subscribe(api, target, source)
        else:
            def clock():
                with api.runtime.transaction() as tx:
                    return tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
            monkeypatch.setattr("orchestrator.framework_credentials._now", clock)
            credentials = FrameworkCredentialService(plane_runtime=api.runtime, audit=api.service.audit)
            _, token = credentials.issue(owner_id=target.owner_id,
                caller=consent_from_store(api.fixture[0], target.owner_id, api.fixture[2]).observation,
                name="Paused completion read", scopes=("operations.control",),
                expires_in_seconds=3600, max_admissions=10)
            ops = FrameworkWorkOperations(assignments=api.service.assignments, credentials=credentials)
            await ops.subscribe(credentials.resolve_bearer(token),
                source_operation_id=source.assignment_id, target_operation_id=target.assignment_id,
                submission_id=str(uuid4()), expected_revision=target.state_version)
    return source, control(api.runtime, current(api.runtime, target))


async def finish(api, kind, source):
    if kind == "remote":
        db = SimpleNamespace(plane_runtime=api.runtime, plane_repositories=api.runtime.repositories)
        remote_jobs._apply_and_wake(db, remote_jobs._record_to_dict(source),
                                   state="COMPLETED", exit_code="0:0", outcome_name="completed")
    else:
        await complete_source(api, source)


def wakes(api, kind):
    action = "remote_job.completion_wake" if kind == "remote" else "work.completion_wake"
    rows, _ = api.service.audit.list_for_user(api.fixture[1])
    return [row for row in rows if row.action_type == action]


@pytest.mark.parametrize("kind", KINDS)
async def test_completion_while_paused_waits_for_explicit_human_resume(api, monkeypatch, kind):
    source, target = await parked(api, kind, monkeypatch)
    await finish(api, kind, source)
    held = current(api.runtime, target)
    assert held.lifecycle == "paused" and held.phase == "awaiting_event"
    assert held.next_wake_at is None and wakes(api, kind) == []
    due = await api.service.store.transaction(lambda tx, repo:
        repo.discover_due_operations_for_administration(tx, limit=20))
    assert target.assignment_id not in [record.assignment_id for record in due]
    request = body(held)
    resumed = await service_for(api).resume(held.assignment_id, request,
                                          caller=await caller(api, api.fixture[2]))
    after = current(api.runtime, target)
    assert resumed["applied"] is True and resumed["operation"]["phase"] == "waiting"
    assert after.lifecycle == "active" and after.phase == "waiting"
    assert after.operation["control"]["wait"] is None and after.next_wake_at is not None
    assert after.control_epoch == held.control_epoch + 1
    assert after.wake_generation == held.wake_generation + 1
    assert after.definition == held.definition and after.usage == held.usage
    assert len(wakes(api, kind)) == 1 and api.service.audit.verify_chain(target.owner_id) is None
    replay = await service_for(api).resume(held.assignment_id, request,
                                         caller=await caller(api, api.fixture[2]))
    assert replay["applied"] is False and current(api.runtime, target) == after
    assert len(wakes(api, kind)) == 1
    authority = await observation_for(api, after)
    claim = await api.service.store.transaction(lambda tx, repo:
        repo.claim_operation_for_administration(tx, owner_id=after.owner_id,
            assignment_id=after.assignment_id, expected_state_version=after.state_version,
            worker_id="resume-completion-claim", authority=authority, lease_seconds=30))
    assert claim.assignment.assignment_id == after.assignment_id


@pytest.mark.parametrize("kind", KINDS)
async def test_human_resume_preserves_wait_for_a_still_pending_source(api, monkeypatch, kind):
    _, target = await parked(api, kind, monkeypatch)
    result = await service_for(api).resume(target.assignment_id, body(target),
                                         caller=await caller(api, api.fixture[2]))
    after = current(api.runtime, target)
    assert result["applied"] is True and after.lifecycle == "active"
    assert after.phase == "awaiting_event" and after.next_wake_at is None
    assert after.operation["control"]["wait"] == target.operation["control"]["wait"]
    assert wakes(api, kind) == []


@pytest.mark.parametrize("kind", KINDS)
async def test_wake_audit_failure_rolls_back_human_resume_and_all_wake_state(api, monkeypatch, kind):
    source, target = await parked(api, kind, monkeypatch)
    await finish(api, kind, source)
    before = current(api.runtime, target)
    audits, _ = api.service.audit.list_for_user(target.owner_id)

    def refuse(*args, **kwargs):
        raise AssignmentError("work_control_unavailable", 503)

    if kind == "remote":
        monkeypatch.setattr(remote_jobs, "_audit_completion_wakes", refuse)
    else:
        monkeypatch.setattr(WorkControlAudit, "append_completion_wake", refuse)
    with pytest.raises(AssignmentError):
        await service_for(api).resume(target.assignment_id, body(before),
                                     caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, target) == before
    after_audits, _ = api.service.audit.list_for_user(target.owner_id)
    assert [row for row in after_audits if row.action_type.startswith(("assignment_", "work.", "remote_job."))] == [
        row for row in audits if row.action_type.startswith(("assignment_", "work.", "remote_job."))]
    assert api.service.audit.verify_chain(target.owner_id) is None


@pytest.mark.parametrize("kind", KINDS)
async def test_failed_canonical_release_cannot_report_successful_resume(api, monkeypatch, kind):
    source, target = await parked(api, kind, monkeypatch)
    await finish(api, kind, source)
    before = current(api.runtime, target)
    if kind == "remote":
        monkeypatch.setattr(remote_jobs, "_try_wake", lambda *args, **kwargs: None)
    else:
        monkeypatch.setattr(work_subscriptions, "_release", lambda *args, **kwargs: False)
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await service_for(api).resume(target.assignment_id, body(before),
                                     caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, target) == before


@pytest.mark.parametrize("kind", KINDS)
async def test_missing_source_refuses_resume_without_inventing_completion(api, monkeypatch, kind):
    source, target = await parked(api, kind, monkeypatch)
    await finish(api, kind, source)
    if kind == "remote":
        with api.runtime.transaction() as tx:
            assert api.runtime.repositories.tracked_jobs.delete_owner(tx, owner_id=target.owner_id) == 1
    else:
        finished = current(api.runtime, source)
        selected = await caller(api, api.fixture[2])
        await WorkControlService(api.service.assignments).delete(
            target.owner_id, selected.context.claims, source.assignment_id,
            WorkDeleteRequest(expected_revision=finished.state_version), caller=selected)
    before = current(api.runtime, target)
    with pytest.raises(AssignmentError, match="work_completion_source_missing"):
        await service_for(api).resume(target.assignment_id, body(before),
                                     caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, target) == before and wakes(api, kind) == []


async def test_unconfirmed_remote_terminal_record_cannot_release_a_resumed_target(api, monkeypatch):
    source, target = await parked(api, "remote", monkeypatch)
    with api.runtime.transaction() as tx:
        api.runtime.repositories.tracked_jobs.apply_poll(
            tx, owner_id=target.owner_id, tracked_job_id=source.tracked_job_id,
            expected_fail_count=0, expected_last_polled_at=1000, state="UNKNOWN",
            exit_code=None, terminal=True, fail_count=0, polled_at=1001)
    before = current(api.runtime, target)
    with pytest.raises(AssignmentError, match="work_job_completion_unconfirmed"):
        await service_for(api).resume(target.assignment_id, body(before),
                                     caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, target) == before and wakes(api, "remote") == []


async def test_resume_does_not_release_an_unrelated_explicit_owner_wait(api):
    target, = await paired(api, count=1)
    await WorkContinuationService(api.service.assignments).wait(
        target.assignment_id, WorkOwnerWaitRequest(
            expected_revision=target.state_version, submission_id=str(uuid4()),
            owner_event_id=str(uuid4()), owner_revision=7),
        caller=await caller(api, api.fixture[2]))
    held = control(api.runtime, current(api.runtime, target))
    result = await service_for(api).resume(held.assignment_id, body(held),
                                         caller=await caller(api, api.fixture[2]))
    after = current(api.runtime, target)
    assert result["applied"] is True and after.lifecycle == "active"
    assert after.phase == "awaiting_event" and after.next_wake_at is None
    assert after.operation["control"]["wait"] == held.operation["control"]["wait"]
