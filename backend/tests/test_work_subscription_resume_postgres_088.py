"""Tests subscription completion during pause through guarded owner resume.
Real PostgreSQL cases preserve atomic wake/audit and current source authority.
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError
from tests.helpers.plane_template import plane_template as plane_template
from tests.helpers.session_consent_088 import consent_from_store
from tests.test_work_resume_postgres_088 import (
    api as api,
    body,
    caller,
    context,
    current,
    fixture as fixture,
    plane as plane,
    research_command,
    research_service as research_service,
    service as service,
    service_for,
    signing_key as signing_key,
    source_service as source_service,
)

runtime = plane


async def paused_subscription(api, monkeypatch, *, finish=True):
    credentials = FrameworkCredentialService(plane_runtime=api.runtime, audit=api.service.audit)

    def credential_clock():
        with api.runtime.transaction() as tx:
            return tx.fetch_one("SELECT clock_timestamp() AS now")["now"]

    monkeypatch.setattr("orchestrator.framework_credentials._now", credential_clock)
    owner, sid = api.fixture[1], api.fixture[2]
    _, token = credentials.issue(
        owner_id=owner,
        caller=consent_from_store(api.fixture[0], owner, sid).observation,
        name="independent synthetic completion control",
        scopes=("operations.control", "operations.read"),
        expires_in_seconds=3600,
        max_admissions=10,
    )
    framework_caller = credentials.resolve_bearer(token)
    operations = FrameworkWorkOperations(assignments=api.service.assignments, credentials=credentials)
    source = (await api.service.submit(await context(api.fixture, api.runtime), research_command(api.service))).record
    target = (await api.service.submit(await context(api.fixture, api.runtime), research_command(api.service))).record
    await operations.subscribe(framework_caller, source_operation_id=source.assignment_id,
        target_operation_id=target.assignment_id, submission_id=str(uuid4()), expected_revision=target.state_version)
    parked = current(api.runtime, target)
    paused = await operations.pause(framework_caller, target.assignment_id,
        submission_id=str(uuid4()), expected_revision=parked.state_version)
    assert paused["applied"] is True
    if finish:
        await operations.cancel(framework_caller, source.assignment_id,
            submission_id=str(uuid4()), expected_revision=source.state_version)
    terminal = current(api.runtime, source)
    if finish:
        assert terminal.operation["terminal_outcome"] == "cancelled"
    waiting = current(api.runtime, target)
    assert waiting.lifecycle == "paused" and waiting.phase == "awaiting_event"
    assert waiting.operation["control"]["wait"] is not None
    return source, target, terminal, waiting


@pytest.mark.asyncio
async def test_normal_owner_resume_reconciles_completion_that_happened_while_target_paused(api, monkeypatch):
    source, target, terminal, waiting = await paused_subscription(api, monkeypatch)
    owner, sid = api.fixture[1], api.fixture[2]
    selected = await caller(api, sid)
    result = await service_for(api).resume(target.assignment_id, body(waiting), caller=selected)
    assert result["applied"] is True
    resumed = current(api.runtime, target)
    assert resumed.lifecycle == "active"
    assert resumed.phase == "waiting", (
        "source terminal completion was lost while target paused; normal owner resume retained an unobservable event wait",
        resumed.phase,
        resumed.next_wake_at,
    )
    assert resumed.next_wake_at is not None
    assert resumed.operation["control"]["wait"] is None
    key = "operation:" + source.assignment_id + ":completed"
    assert resumed.operation["control"]["watermarks"][key] >= terminal.state_version
    rows, _ = api.service.audit.list_for_user(owner)
    assert sum(row.action_type == "work.completion_wake" for row in rows) == 1
    assert api.service.audit.verify_chain(owner) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["source_owner", "source_id", "source_unsupported", "source_missing", "source_shape", "wake", "wake_audit"])
async def test_resume_reconciliation_failures_rollback_and_same_submission_retries_once(api, monkeypatch, failure):
    source, target, _terminal, waiting = await paused_subscription(api, monkeypatch)
    owner, sid = api.fixture[1], api.fixture[2]
    repository = api.runtime.repositories.assignments
    original_read = repository.get_operation
    original_accept = repository.accept_wake
    original_audit = api.service.audit.insert_in_transaction

    def failed_read(tx, **options):
        result = original_read(tx, **options)
        if options.get("assignment_id") != source.assignment_id:
            return result
        if failure == "source_owner":
            return replace(result, assignment=replace(result.assignment, owner_id="independent-foreign-owner"))
        if failure == "source_id":
            return replace(result, assignment=replace(result.assignment, assignment_id=str(uuid4())))
        if failure == "source_unsupported":
            return replace(result, continuation_supported=False)
        if failure == "source_missing":
            return None
        if failure == "source_shape":
            return {"assignment": result.assignment}
        return result

    def failed_wake(*args, **kwargs):
        raise RuntimeError("independent controlled wake refusal")

    def failed_audit(event, **kwargs):
        if event.action_type == "work.completion_wake":
            raise RuntimeError("independent controlled wake audit refusal")
        return original_audit(event, **kwargs)

    if failure.startswith("source_"):
        monkeypatch.setattr(repository, "get_operation", failed_read)
    elif failure == "wake":
        monkeypatch.setattr(repository, "accept_wake", failed_wake)
    else:
        monkeypatch.setattr(api.service.audit, "insert_in_transaction", failed_audit)
    request_body = body(waiting)
    with pytest.raises(AssignmentError):
        await service_for(api).resume(target.assignment_id, request_body, caller=await caller(api, sid))
    monkeypatch.setattr(repository, "get_operation", original_read)
    monkeypatch.setattr(repository, "accept_wake", original_accept)
    monkeypatch.setattr(api.service.audit, "insert_in_transaction", original_audit)
    assert current(api.runtime, target) == waiting
    rows, _ = api.service.audit.list_for_user(owner)
    assert sum(row.action_type == "assignment_resume" for row in rows) == 0
    assert sum(row.action_type == "work.completion_wake" for row in rows) == 0
    result = await service_for(api).resume(target.assignment_id, request_body, caller=await caller(api, sid))
    assert result["applied"] is True
    resumed = current(api.runtime, target)
    assert resumed.phase == "waiting" and resumed.operation["control"]["wait"] is None
    rows, _ = api.service.audit.list_for_user(owner)
    assert sum(row.action_type == "assignment_resume" for row in rows) == 1
    assert sum(row.action_type == "work.completion_wake" for row in rows) == 1
    replay = await service_for(api).resume(target.assignment_id, request_body, caller=await caller(api, None))
    assert replay["applied"] is False
    assert current(api.runtime, target) == resumed
    assert api.service.audit.verify_chain(owner) is None


@pytest.mark.asyncio
async def test_owner_resume_preserves_wait_for_pending_subscription_source(api, monkeypatch):
    _source, target, _terminal, waiting = await paused_subscription(api, monkeypatch, finish=False)
    result = await service_for(api).resume(target.assignment_id, body(waiting),
                                         caller=await caller(api, api.fixture[2]))
    resumed = current(api.runtime, target)
    assert result["applied"] is True and result["operation"]["phase"] == "awaiting_event"
    assert resumed.lifecycle == "active" and resumed.next_wake_at is None
    assert resumed.operation["control"]["wait"] == waiting.operation["control"]["wait"]
    rows, _ = api.service.audit.list_for_user(target.owner_id)
    assert not any(row.action_type == "work.completion_wake" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["owner:synthetic-event", "remote-job:synthetic-event", "completion:synthetic-event"])
async def test_resume_does_not_resolve_other_event_namespaces(api, key):
    from persistent_agents.runtime_values import digest
    from tests.test_work_continuation_authority_088 import control

    target = (await api.service.submit(await context(api.fixture, api.runtime),
                                      research_command(api.service))).record
    with api.runtime.transaction() as tx:
        api.runtime.repositories.assignments.set_owner_event_wait(
            tx, owner_id=target.owner_id, assignment_id=target.assignment_id,
            expected_state_version=target.state_version,
            expected_instruction_revision=target.instruction_revision,
            expected_control_epoch=target.control_epoch, submission_id=str(uuid4()),
            submission_digest=digest(["synthetic-unrelated-wait", key]), event_key=key,
            source_revision=7)
    waiting = control(api.runtime, current(api.runtime, target))
    result = await service_for(api).resume(target.assignment_id, body(waiting),
                                         caller=await caller(api, api.fixture[2]))
    resumed = current(api.runtime, target)
    assert result["applied"] is True and resumed.lifecycle == "active"
    assert resumed.phase == "awaiting_event" and resumed.next_wake_at is None
    assert resumed.operation["control"]["wait"] == waiting.operation["control"]["wait"]
    rows, _ = api.service.audit.list_for_user(target.owner_id)
    assert not any(row.action_type == "work.completion_wake" for row in rows)


@pytest.mark.asyncio
async def test_owner_resume_uses_fenced_completed_source_revision(api, monkeypatch):
    from types import SimpleNamespace

    from tests.test_work_subscriptions_postgres_088 import (
        _claim_source, _runner, _terminal_completion,
    )

    source, target, _terminal, waiting = await paused_subscription(api, monkeypatch, finish=False)
    work = SimpleNamespace(runtime=api.runtime, store=api.fixture[0], owner=source.owner_id,
                           service=api.service.assignments, orch=api.orch)
    runner = await _runner(work)
    try:
        executor = await _claim_source(work, runner, source)
        def complete(tx, repository):
            before = repository.get_operation(
                tx, owner_id=source.owner_id, assignment_id=source.assignment_id).assignment
            return runner._complete_operation(tx, repository, executor, before,
                _terminal_completion(before, "paused-source-completed"))

        await api.service.store.transaction(complete, bound_session_waits=True)
    finally:
        await runner.stop()
    terminal = current(api.runtime, source)
    assert terminal.lifecycle == "completed" and terminal.operation["terminal_outcome"] == "completed"
    assert current(api.runtime, target) == waiting
    result = await service_for(api).resume(target.assignment_id, body(waiting),
                                         caller=await caller(api, api.fixture[2]))
    resumed = current(api.runtime, target)
    assert result["applied"] is True and result["operation"]["phase"] == "waiting"
    assert resumed.operation["control"]["wait"] is None and resumed.next_wake_at is not None
    key = "operation:" + source.assignment_id + ":completed"
    assert resumed.operation["control"]["watermarks"][key] >= terminal.state_version
    rows, _ = api.service.audit.list_for_user(target.owner_id)
    wakes = [row for row in rows if row.action_type == "work.completion_wake"]
    assert len(wakes) == 1 and wakes[0].outputs_meta["outcome"] == "completed"
    assert api.service.audit.verify_chain(target.owner_id) is None
