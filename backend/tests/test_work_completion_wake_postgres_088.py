"""Tests for owner completion continuations (backend/orchestrator/work_completion_wake.py)
against guarded session authority and real Plane: bounded proposal/confirmation, registration-completion
serialization, completion-transaction wakes through the ordinary dispatch boundary,
duplicate dedup, truthful blocks, revocation, and redrive.
"""

import asyncio
import importlib
import threading
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignment_models import AssignmentOperationBinding
from astralplane.repositories.assignments import AssignmentEpisodeCompletion
from astralplane.repositories.history import SessionExecutionObservation
from astralplane.repositories.work_admission import (
    AdmissionClass, AdmissionClassConfig, OperationOwner, OperationRequest, OwnerScope,
)

from orchestrator.work_admission import WorkAdmissionCoordinator
from orchestrator.work_controls import WorkControlRequest, WorkControlService
from orchestrator.work_service import WorkService
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest
from tests.helpers.database_waits import observe_lock_wait
from tests.test_work_admission_api_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
)
from tests.test_work_continuation_authority_088 import control, current
from tests.test_work_research_preflight_postgres_088 import research_command
from tests.test_work_resume_postgres_088 import caller
from tests.test_work_submit_postgres_088 import context

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_redrive_state():
    module()._redrive_pending.clear()
    yield
    module()._redrive_pending.clear()


def module():
    return importlib.import_module("orchestrator.work_completion_wake")


def service_for(api):
    return module().WorkCompletionWakeService(api.service.assignments)


def proposal_body(record, source_id, **changes):
    body = {"expected_revision": record.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source_id}
    body.update(changes)
    return module().WorkContinuationProposalRequest(**body)


def confirm_body(record, source_id, proposal, **changes):
    body = {"expected_revision": record.state_version, "submission_id": str(uuid4()),
            "source_operation_id": source_id, "proposal_id": proposal["proposal_id"],
            "proposal_digest": proposal["proposal_digest"]}
    body.update(changes)
    return module().WorkContinuationConfirmRequest(**body)


def revoke_body(record, **changes):
    body = {"expected_revision": record.state_version, "submission_id": str(uuid4())}
    body.update(changes)
    return WorkControlRequest(**body)


async def propose_for(api, waiter, source, *, sid=None):
    return await service_for(api).propose(
        waiter.assignment_id, proposal_body(waiter, source.assignment_id),
        caller=await caller(api, sid or api.fixture[2]))


async def subscribe(api, waiter, source):
    proposal = await propose_for(api, waiter, source)
    confirmed = await service_for(api).confirm(
        waiter.assignment_id, confirm_body(waiter, source.assignment_id, proposal),
        caller=await caller(api, api.fixture[2]))
    return proposal, confirmed


async def paired(api, count=2):
    records = []
    for _ in range(count):
        records.append((await api.service.submit(await context(api.fixture, api.runtime),
                                                 research_command(api.service))).record)
    return records


def continuation_audits(api):
    rows, _ = api.service.audit.list_for_user(api.fixture[1])
    return [row for row in rows if row.action_type == "work.completion_wake"]


def command_audits(api, command):
    rows, _ = api.service.audit.list_for_user(api.fixture[1])
    return [row for row in rows if row.action_type == "assignment_" + command]


def wait_of(runtime, record):
    from collections.abc import Mapping

    value = current(runtime, record)
    operation = value.operation if isinstance(value.operation, Mapping) else {}
    control = operation.get("control") if isinstance(operation.get("control"), Mapping) else {}
    wait = control.get("wait") if isinstance(control.get("wait"), Mapping) else None
    return dict(wait) if wait else None, value


def key_of(source):
    return "completion:" + source.assignment_id


def observation_for(api, record, seconds=15):
    def read(tx):
        state = api.runtime.repositories.history.sessions.get_execution_state(
            tx, owner_id=record.owner_id, session_id=api.fixture[2])
        return SessionExecutionObservation(state.credential, state.observed_at,
                                           state.observed_at + timedelta(seconds=seconds))
    return api.service.store.transaction(lambda tx, _: read(tx))


async def admission_fences(api, record):
    from astralplane.repositories.work_admission import WorkAdmissionRepository

    repository = WorkAdmissionRepository()
    configs = (
        AdmissionClassConfig(AdmissionClass.GLOBAL, None, 10, 0, 0, "completion-fixture"),
        AdmissionClassConfig(AdmissionClass.BACKGROUND, AdmissionClass.GLOBAL, 10, 0, 0,
                             "completion-fixture"),
    )
    await api.service.store.transaction(lambda tx, _: repository.configure(tx, configs))
    repository.bind_configs(configs)

    def start(tx, assignment_repository):
        state = api.runtime.repositories.history.sessions.get_execution_state(
            tx, owner_id=record.owner_id, session_id=api.fixture[2])
        authority = SessionExecutionObservation(state.credential, state.observed_at,
                                                state.observed_at + timedelta(seconds=15))
        claim = assignment_repository.claim_operation_for_administration(
            tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
            expected_state_version=record.state_version, worker_id="completion-fixture",
            authority=authority, lease_seconds=30)
        admitted = repository.submit(
            tx, OperationRequest(operation_kind="assignment_episode",
                admission_class=AdmissionClass.BACKGROUND,
                owner=OperationOwner(OwnerScope.USER, record.owner_id, None),
                submission_id=uuid4(), idempotency_namespace=None, idempotency_key=None,
                normalized_input_digest=None, chat_id=None, parent_operation_id=None,
                connection_generation=None, request_generation=None),
            now=None, retention=timedelta(days=1), slot_lease=timedelta(minutes=1))
        selected = repository.claim_operation(tx, AdmissionClass.BACKGROUND,
                                              admitted.operation_id, now=None,
                                              retention=timedelta(days=1),
                                              slot_lease=timedelta(minutes=1))
        binding = AssignmentOperationBinding(str(selected.fence.operation_id),
            selected.fence.execution_generation, str(selected.fence.execution_lease_token))
        assignment_repository.bind_operation(tx, fence=claim.fence, binding=binding)
        return claim.fence, selected.fence

    return await api.service.store.transaction(start)


async def complete_source(api, source, *, fences=None):
    claim_fence, operation_fence = fences or await admission_fences(api, source)
    if getattr(api.orch, "work_admission", None) is None:
        api.orch.work_admission = WorkAdmissionCoordinator.from_plane(plane_runtime=api.runtime)
    executor = SimpleNamespace(claim=SimpleNamespace(fence=claim_fence),
                               operation_fence=operation_fence)
    completion_digest = digest(["completion-fixture"])

    def complete(tx, repository):
        fresh = repository.get_operation(tx, owner_id=source.owner_id,
                                         assignment_id=source.assignment_id).assignment
        completion = AssignmentEpisodeCompletion(
            expected_state_version=fresh.state_version, checkpoint=fresh.checkpoint,
            completion_digest=completion_digest, phase="waiting",
            wake_reason="research_completed", completed=True, terminal_outcome="completed")
        return api.runner._complete_operation(tx, repository, executor, fresh, completion)

    return await api.service.store.transaction(complete, bound_session_waits=True)


async def fire_again(api, source, revision, outcome="completed"):
    return await api.service.store.transaction(lambda tx, repo:
        module().fire_completions(api.service.assignments, tx, repo,
            owner_id=source.owner_id, source_id=source.assignment_id,
            source_revision=revision, outcome=outcome))


async def test_confirm_registers_durable_subscription_over_qualified_records(api):
    source, waiter = await paired(api)
    proposal, confirmed = await subscribe(api, waiter, source)
    assert confirmed["applied"] is True
    assert confirmed["continuation"] == {"source_operation_id": source.assignment_id,
                                         "event_revision": source.state_version}
    assert proposal["proposal_digest"] == module()._proposal_digest(
        waiter.assignment_id, source.assignment_id)
    assert proposal["expires_at"] > proposal["expires_at"] - module()._PROPOSAL_TTL_SECONDS
    wait, after = wait_of(api.runtime, waiter)
    assert after.phase == "awaiting_event" and after.lifecycle == "active"
    assert wait == {"event_key": key_of(source), "source_revision": source.state_version}
    assert after.control_epoch == waiter.control_epoch + 1
    assert after.operation["authority"] == waiter.operation["authority"]
    assert after.definition == waiter.definition and after.checkpoint == waiter.checkpoint
    assert current(api.runtime, source).state_version == source.state_version
    subscribed = command_audits(api, "subscribe")
    assert len(subscribed) == 1
    assert subscribed[0].outputs_meta["source_operation_id"] == source.assignment_id
    assert subscribed[0].outputs_meta["target_operation_id"] == waiter.assignment_id
    assert api.service.audit.verify_chain(waiter.owner_id) is None


async def test_propose_refuses_foreign_self_terminal_and_ineligible_records(api):
    source, waiter = await paired(api)
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service_for(api).propose(waiter.assignment_id,
                                       proposal_body(waiter, str(uuid4())),
                                       caller=await caller(api, api.fixture[2]))
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service_for(api).propose(str(uuid4()),
                                       proposal_body(waiter, source.assignment_id),
                                       caller=await caller(api, api.fixture[2]))
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).propose(waiter.assignment_id,
                                       proposal_body(waiter, waiter.assignment_id),
                                       caller=await caller(api, api.fixture[2]))
    control(api.runtime, source, "stop")
    with pytest.raises(AssignmentError, match="work_continuation_source_terminal"):
        await service_for(api).propose(waiter.assignment_id,
                                       proposal_body(waiter, source.assignment_id),
                                       caller=await caller(api, api.fixture[2]))
    _, cancelled_waiter = await paired(api)
    control(api.runtime, cancelled_waiter, "stop")
    with pytest.raises(AssignmentError, match="assignment_not_waiting"):
        await service_for(api).propose(cancelled_waiter.assignment_id,
                                       proposal_body(cancelled_waiter, waiter.assignment_id),
                                       caller=await caller(api, api.fixture[2]))


async def test_propose_refuses_waiter_waiting_on_a_different_event(api):
    source, waiter = await paired(api)
    await api.service.store.call("set_owner_event_wait", owner_id=waiter.owner_id,
        assignment_id=waiter.assignment_id,
        expected_instruction_revision=waiter.instruction_revision,
        expected_control_epoch=waiter.control_epoch,
        expected_state_version=waiter.state_version, submission_id=str(uuid4()),
        submission_digest=digest("fixture-other-wait"), event_key="owner:" + str(uuid4()),
        source_revision=0)
    with pytest.raises(AssignmentError, match="assignment_event_key_conflict"):
        await propose_for(api, waiter, source)
    assert wait_of(api.runtime, waiter)[1].phase == "awaiting_event"


async def framework_operation(api, monkeypatch):
    from audit.repository import AuditRepository
    from orchestrator.framework_credentials import FrameworkCredentialService
    from orchestrator.work_operations import FrameworkWorkOperations
    from tests.helpers.session_consent_088 import consent_from_store

    def credential_clock():
        with api.runtime.transaction() as tx:
            return tx.fetch_one("SELECT clock_timestamp() AS now")["now"]

    monkeypatch.setattr("orchestrator.framework_credentials._now", credential_clock)
    credentials = FrameworkCredentialService(plane_runtime=api.runtime,
                                             audit=AuditRepository(plane_runtime=api.runtime))
    consent = consent_from_store(api.fixture[0], api.fixture[1], api.fixture[2])
    _view, token = credentials.issue(owner_id=api.fixture[1], caller=consent.observation,
                                     name="Continuation fixture",
                                     scopes=("operations.submit",), expires_in_seconds=3600,
                                     max_admissions=100)
    operations = FrameworkWorkOperations(assignments=api.service.assignments,
                                         credentials=credentials)
    return await operations.submit(credentials.resolve_bearer(token),
                                   idempotency_key=str(uuid4()), name="Framework waiter",
                                   instructions="Standalone framework work.")


async def test_propose_and_confirm_refuse_unsupported_waiter_versions(api, monkeypatch):
    source, waiter = await paired(api)
    with api.runtime.transaction() as tx:
        tx.execute("UPDATE persistent_assignment SET "
                   "data=jsonb_set(data,'{operation,version}','3'::jsonb) WHERE id=%s",
                   (waiter.assignment_id,))
    with pytest.raises(AssignmentError, match="assignment_version_unsupported"):
        await propose_for(api, waiter, source)
    framework = await framework_operation(api, monkeypatch)
    record = current(api.runtime, SimpleNamespace(owner_id=api.fixture[1],
                                                  assignment_id=framework["id"]))
    with pytest.raises(AssignmentError, match="work_continuation_waiter_unsupported"):
        await service_for(api).propose(framework["id"],
                                       proposal_body(record, source.assignment_id),
                                       caller=await caller(api, api.fixture[2]))
    source_id = source.assignment_id
    import time as time_module
    module()._proposals(api.orch).setdefault(api.fixture[1], {})[uuid4().hex] = {
        "waiter": framework["id"], "source": source_id, "created_at": time_module.time()}
    proposal = {"proposal_id": next(iter(module()._proposals(api.orch)[api.fixture[1]])),
                "proposal_digest": module()._proposal_digest(framework["id"], source_id)}
    with pytest.raises(AssignmentError, match="work_continuation_waiter_unsupported"):
        await service_for(api).confirm(framework["id"],
                                       confirm_body(record, source_id, proposal),
                                       caller=await caller(api, api.fixture[2]))
    assert wait_of(api.runtime, SimpleNamespace(owner_id=api.fixture[1],
                                                assignment_id=framework["id"]))[0] is None


async def test_confirm_requires_a_live_bound_proposal(api):
    source, waiter = await paired(api)
    with pytest.raises(AssignmentError, match="work_proposal_expired"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, source.assignment_id, {"proposal_id": uuid4().hex,
                                           "proposal_digest": "0" * 64}),
            caller=await caller(api, api.fixture[2]))
    proposal = await propose_for(api, waiter, source)
    with pytest.raises(AssignmentError, match="assignment_proposal_changed"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, source.assignment_id, proposal, proposal_digest="1" * 64),
            caller=await caller(api, api.fixture[2]))
    with pytest.raises(AssignmentError, match="assignment_proposal_changed"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, str(uuid4()), proposal), caller=await caller(api, api.fixture[2]))
    bucket = module()._proposals(api.orch)[waiter.owner_id]
    foreign_key = uuid4().hex
    bucket[foreign_key] = {"waiter": str(uuid4()), "source": source.assignment_id,
                           "created_at": __import__("time").time()}
    stale_waiter = {"proposal_id": foreign_key, "proposal_digest": module()._proposal_digest(
        waiter.assignment_id, source.assignment_id)}
    with pytest.raises(AssignmentError, match="assignment_proposal_changed"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, source.assignment_id, stale_waiter),
            caller=await caller(api, api.fixture[2]))
    bucket.pop(foreign_key)
    bucket[proposal["proposal_id"]]["created_at"] -= module()._PROPOSAL_TTL_SECONDS + 1
    with pytest.raises(AssignmentError, match="work_proposal_expired"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, source.assignment_id, proposal), caller=await caller(api, api.fixture[2]))
    assert proposal["proposal_id"] not in bucket
    unchanged = current(api.runtime, waiter)
    assert unchanged.phase == waiter.phase and unchanged.control_epoch == waiter.control_epoch
    assert command_audits(api, "subscribe") == []


async def test_confirm_refuses_stale_revision_and_terminal_source(api):
    source, waiter = await paired(api)
    proposal = await propose_for(api, waiter, source)
    control(api.runtime, waiter, "pause")
    with pytest.raises(AssignmentError, match="assignment_revision_conflict"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, source.assignment_id, proposal), caller=await caller(api, api.fixture[2]))
    resumed = current(api.runtime, waiter)
    control(api.runtime, resumed, "resume")
    resumed = current(api.runtime, resumed)
    proposal = await propose_for(api, resumed, source)
    control(api.runtime, source, "stop")
    with pytest.raises(AssignmentError, match="work_continuation_source_terminal"):
        await service_for(api).confirm(resumed.assignment_id, confirm_body(
            resumed, source.assignment_id, proposal), caller=await caller(api, api.fixture[2]))
    assert wait_of(api.runtime, resumed)[0] is None
    assert command_audits(api, "subscribe") == []


async def test_confirm_is_idempotent_and_replays_without_new_continuation(api, monkeypatch):
    source, waiter = await paired(api)
    proposal = await propose_for(api, waiter, source)
    original = confirm_body(waiter, source.assignment_id, proposal)
    confirmed = await service_for(api).confirm(waiter.assignment_id, original,
                                               caller=await caller(api, api.fixture[2]))
    assert confirmed["applied"] is True
    second_proposal = await propose_for(api, waiter, source)
    second = await service_for(api).confirm(waiter.assignment_id, confirm_body(
        waiter, source.assignment_id, second_proposal), caller=await caller(api, api.fixture[2]))
    assert second["applied"] is False and second["continuation"] == {
        "source_operation_id": source.assignment_id, "event_revision": source.state_version,
        "awaiting_event": True, "source_disposition": "queued", "source_missing": False,
        "source_terminal_outcome": None}
    assert len(command_audits(api, "subscribe")) == 1

    def forbidden(*args, **kwargs):
        raise AssertionError("replayed confirm attempted a new continuation")

    monkeypatch.setattr(api.runtime.repositories.assignments, "set_owner_event_wait", forbidden)
    stopped = control(api.runtime, current(api.runtime, waiter), "stop")
    api.fixture[0].delete(api.fixture[2])
    replay = await service_for(api).confirm(waiter.assignment_id, confirm_body(
        stopped, source.assignment_id, proposal, expected_revision=stopped.state_version + 50,
        submission_id=original.submission_id), caller=await caller(api, None))
    assert replay["applied"] is False
    assert current(api.runtime, waiter) == stopped
    assert len(command_audits(api, "subscribe")) == 1


async def test_registration_serializes_against_completion_race(api, monkeypatch):
    from tests.helpers.database_waits import unbounded_request_execution_waits

    unbounded_request_execution_waits(monkeypatch)
    source, waiter = await paired(api)
    proposal = await propose_for(api, waiter, source)
    selected = await caller(api, api.fixture[2])
    repository = api.runtime.repositories.assignments
    original_get = repository.get_operation
    locked, requesting, finished = threading.Event(), threading.Event(), threading.Event()
    shared = {}

    def locked_get(query, *, owner_id, assignment_id, lock=False):
        if lock:
            shared["pid"] = query.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            requesting.set()
        return original_get(query, owner_id=owner_id, assignment_id=assignment_id, lock=lock)

    repository.get_operation = locked_get
    try:
        def holder():
            with api.runtime.transaction() as tx:
                tx.execute("SELECT id FROM persistent_assignment WHERE id=%s FOR UPDATE",
                           (source.assignment_id,))
                blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
                locked.set()
                assert requesting.wait(5), "confirm did not reach the locked source read"
                assert observe_lock_wait(tx, blocker=blocker, waiter=shared["pid"],
                                         finished=finished)
                repository.apply_control(
                    tx, owner_id=source.owner_id, assignment_id=source.assignment_id,
                    expected_instruction_revision=source.instruction_revision,
                    expected_control_epoch=source.control_epoch,
                    expected_state_version=source.state_version,
                    submission_id=str(uuid4()), submission_digest=digest("race-stop"),
                    control="stop")

        worker = asyncio.create_task(asyncio.to_thread(holder))
        try:
            assert await asyncio.to_thread(locked.wait, 5)
            with pytest.raises(AssignmentError, match="work_continuation_source_terminal"):
                await service_for(api).confirm(waiter.assignment_id, confirm_body(
                    waiter, source.assignment_id, proposal), caller=selected)
        finally:
            finished.set()
            await worker
    finally:
        repository.get_operation = original_get
    stopped = current(api.runtime, source)
    assert stopped.lifecycle == "stopped"
    assert current(api.runtime, waiter).phase == waiter.phase
    assert wait_of(api.runtime, waiter)[0] is None
    assert command_audits(api, "subscribe") == []


async def test_completion_through_the_runner_fires_the_durable_wake(api):
    source, waiter = await paired(api)
    def _inc():
        with api.runtime.transaction() as tx:
            state = api.runtime.repositories.history.sessions.get_execution_state(
                tx, owner_id=api.fixture[1], session_id=api.fixture[2])
            return state.credential.incarnation_id
    print("INC-BEFORE", _inc(), source.operation["authority"]["reference_id"], flush=True)
    _, confirmed = await subscribe(api, waiter, source)
    print("INC-AFTER", _inc(), flush=True)
    assert confirmed["applied"] is True
    before_generation = current(api.runtime, waiter).wake_generation
    finished = await complete_source(api, source)
    assert finished.lifecycle == "completed"
    assert finished.operation["terminal_outcome"] == "completed"
    wait, after = wait_of(api.runtime, waiter)
    assert wait is None and after.phase == "waiting"
    assert after.wake_generation == before_generation + 1
    assert after.next_wake_at is not None
    control_state = after.operation["control"]
    assert control_state["watermarks"][key_of(source)] == finished.state_version
    event_id = f"completion:{source.assignment_id}:{waiter.assignment_id}:{finished.state_version}"
    assert event_id in control_state["wake_receipts"]
    wakes = continuation_audits(api)
    assert len(wakes) == 1
    assert wakes[0].outputs_meta["outcome"] == "completed"
    assert wakes[0].outputs_meta["source_operation_id"] == source.assignment_id
    assert wakes[0].outputs_meta["event_revision"] == finished.state_version
    assert api.service.audit.verify_chain(waiter.owner_id) is None


async def test_woken_waiter_is_claimable_through_ordinary_dispatch(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    await complete_source(api, source)
    due = await api.service.store.transaction(lambda tx, repo:
        repo.discover_due_operations_for_administration(tx, limit=20))
    assert [record.assignment_id for record in due] == [waiter.assignment_id]
    authority = await observation_for(api, waiter)
    claimed = await api.service.store.transaction(lambda tx, repo:
        repo.claim_operation_for_administration(tx, owner_id=waiter.owner_id,
            assignment_id=waiter.assignment_id,
            expected_state_version=current(api.runtime, waiter).state_version,
            worker_id="dispatch-fixture", authority=authority, lease_seconds=30))
    assert claimed.assignment.assignment_id == waiter.assignment_id


async def test_duplicate_completion_events_deduplicate(api):
    source, first, second = await paired(api, count=3)
    await subscribe(api, first, source)
    await subscribe(api, second, source)
    finished = await complete_source(api, source)
    after_first = current(api.runtime, first)
    assert current(api.runtime, second).phase == "waiting"
    assert len(continuation_audits(api)) == 2
    fired = await fire_again(api, source, finished.state_version)
    assert fired == 0
    assert current(api.runtime, first) == after_first
    assert current(api.runtime, second).phase == "waiting"
    assert len(continuation_audits(api)) == 2


async def test_cancelled_source_fires_wakes_through_the_owner_control(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    before = current(api.runtime, waiter).wake_generation
    selected = await caller(api, api.fixture[2])
    reply = await WorkControlService(api.service.assignments).control(
        waiter.owner_id, selected.context.claims, source.assignment_id, "cancel",
        revoke_body(source), caller=selected)
    assert reply["applied"] is True
    wait, after = wait_of(api.runtime, waiter)
    assert wait is None and after.phase == "waiting"
    assert after.wake_generation == before + 1
    wakes = continuation_audits(api)
    assert len(wakes) == 1 and wakes[0].outputs_meta["outcome"] == "cancelled"


async def test_expired_waiter_stays_blocked_with_the_truthful_reason(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    with api.runtime.transaction() as tx:
        tx.execute("UPDATE persistent_assignment SET data=jsonb_set("
                   "data,'{operation,deadline_at}',to_jsonb('2020-01-01T00:00:00+00:00'"
                   "::timestamptz)) WHERE id=%s", (waiter.assignment_id,))
    finished = await complete_source(api, source)
    assert finished.lifecycle == "completed"
    after = current(api.runtime, waiter)
    assert after.phase == "awaiting_event" and after.operation["control"]["wait"] is not None
    assert continuation_audits(api) == []
    assert module()._redrive_pending == {}


async def test_uncertain_waiter_rolls_back_completion_and_retry_recovers(api, monkeypatch):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    repository = api.runtime.repositories.assignments
    original_accept = repository.accept_wake

    def uncertain(*args, **kwargs):
        raise RepositoryConflictError("assignment_action_uncertain",
                                      code="assignment_action_uncertain")

    monkeypatch.setattr(repository, "accept_wake", uncertain)
    fences = await admission_fences(api, source)
    with pytest.raises(AssignmentError, match="assignment_action_uncertain"):
        await complete_source(api, source, fences=fences)
    assert current(api.runtime, source).lifecycle == "active"
    assert current(api.runtime, waiter).phase == "awaiting_event"
    assert continuation_audits(api) == []
    assert module()._redrive_pending == {}
    monkeypatch.setattr(repository, "accept_wake", original_accept)
    await complete_source(api, source, fences=fences)
    after = current(api.runtime, waiter)
    assert after.phase == "waiting" and len(continuation_audits(api)) == 1


async def test_redrive_attempts_are_bounded_and_drop_exhausted_entries(api, monkeypatch):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    repository = api.runtime.repositories.assignments

    def uncertain(*args, **kwargs):
        raise RepositoryConflictError("assignment_action_uncertain",
                                      code="assignment_action_uncertain")

    monkeypatch.setattr(repository, "accept_wake", uncertain)
    with pytest.raises(AssignmentError, match="assignment_action_uncertain"):
        await complete_source(api, source)
    module().schedule_redrive(waiter.owner_id, source.assignment_id)
    for _ in range(module()._REDRIVE_ATTEMPTS + 1):
        assert await module().redrive_pending(api.service.assignments) == 0
    assert module()._redrive_pending == {}
    assert current(api.runtime, waiter).phase == "awaiting_event"


async def test_redrive_skips_missing_sources_and_counts_attempts(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    module().schedule_redrive(waiter.owner_id, str(uuid4()))
    module().schedule_redrive(waiter.owner_id, source.assignment_id)
    finished = await complete_source(api, source)
    for _ in range(module()._REDRIVE_ATTEMPTS):
        await module().redrive_pending(api.service.assignments)
    missing = [key for key in module()._redrive_pending if key[1] != source.assignment_id]
    assert module()._redrive_pending[missing[0]] == module()._REDRIVE_ATTEMPTS
    module()._redrive_pending.clear()
    assert finished.lifecycle == "completed"


async def test_revoke_releases_the_waiter_to_ordinary_dispatch(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    subscribed = current(api.runtime, waiter)
    wait_revision = subscribed.operation["control"]["wait"]["source_revision"]
    revoked = await service_for(api).revoke(
        waiter.assignment_id, source.assignment_id, revoke_body(subscribed),
        caller=await caller(api, api.fixture[2]))
    assert revoked["applied"] is True and revoked["continuation"] is None
    wait, after = wait_of(api.runtime, waiter)
    assert wait is None and after.phase == "waiting" and after.next_wake_at is not None
    assert after.operation["control"]["watermarks"][key_of(source)] == wait_revision + 1
    assert len(command_audits(api, "unsubscribe")) == 1
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service_for(api).revoke(waiter.assignment_id, source.assignment_id,
                                      revoke_body(after), caller=await caller(api, api.fixture[2]))
    assert len(command_audits(api, "unsubscribe")) == 1
    assert api.service.audit.verify_chain(waiter.owner_id) is None


async def test_revoke_refuses_unsubscribed_and_paused_waiters(api):
    source, waiter, other = await paired(api, count=3)
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service_for(api).revoke(waiter.assignment_id, source.assignment_id,
                                      revoke_body(waiter), caller=await caller(api, api.fixture[2]))
    await subscribe(api, waiter, source)
    control(api.runtime, current(api.runtime, waiter), "pause")
    paused = current(api.runtime, waiter)
    with pytest.raises(AssignmentError, match="assignment_not_waiting"):
        await service_for(api).revoke(waiter.assignment_id, source.assignment_id,
                                      revoke_body(paused), caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, waiter).operation["control"]["wait"] is not None
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service_for(api).revoke(other.assignment_id, str(uuid4()),
                                      revoke_body(other), caller=await caller(api, api.fixture[2]))


async def test_private_argument_types_and_absent_caller_are_closed(api):
    source, waiter = await paired(api)
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await service_for(api).propose(waiter.assignment_id,
                                       proposal_body(waiter, source.assignment_id), caller=None)
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).propose(waiter.assignment_id,
                                       proposal_body(waiter, source.assignment_id).model_dump(),
                                       caller=await caller(api, api.fixture[2]))
    proposal = await propose_for(api, waiter, source)
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).confirm(waiter.assignment_id,
                                       confirm_body(waiter, source.assignment_id, proposal).model_dump(),
                                       caller=await caller(api, api.fixture[2]))
    assert wait_of(api.runtime, waiter)[0] is None


async def test_proposals_are_bounded_per_owner(api):
    source, waiter = await paired(api)
    service = service_for(api)
    first = await service.propose(waiter.assignment_id,
                                  proposal_body(waiter, source.assignment_id),
                                  caller=await caller(api, api.fixture[2]))
    for _ in range(module()._PROPOSALS_PER_OWNER):
        await service.propose(waiter.assignment_id,
                              proposal_body(waiter, source.assignment_id),
                              caller=await caller(api, api.fixture[2]))
    bucket = module()._proposals(api.orch)[waiter.owner_id]
    assert len(bucket) == module()._PROPOSALS_PER_OWNER
    assert first["proposal_id"] not in bucket

async def test_malformed_repository_contracts_abort_completion(api, monkeypatch):
    repository = api.runtime.repositories.assignments
    original_accept = repository.accept_wake
    stage = {"current": "shape"}

    def malformed_accept(*args, **kwargs):
        if stage["current"] == "shape":
            return {}
        if stage["current"] == "deep_error":
            raise AssignmentError("assignment_action_uncertain", 409)
        raise RuntimeError("synthetic release failure")

    monkeypatch.setattr(repository, "accept_wake", malformed_accept)
    for stage_name in ("shape", "deep_error", "runtime"):
        stage["current"] = stage_name
        source, waiter = await paired(api)
        await subscribe(api, waiter, source)
        with pytest.raises(AssignmentError):
            await complete_source(api, source)
        assert current(api.runtime, source).lifecycle == "active"
        assert current(api.runtime, waiter).phase == "awaiting_event"
        assert continuation_audits(api) == []
        assert module()._redrive_pending == {}
    monkeypatch.setattr(repository, "accept_wake", original_accept)
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    finished = await complete_source(api, source)
    assert finished.lifecycle == "completed"
    assert current(api.runtime, waiter).phase == "waiting"
    assert len(continuation_audits(api)) == 1


async def test_waiter_missing_between_scan_and_release_is_rechecked(api, monkeypatch):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    repository = api.runtime.repositories.assignments
    original_get = repository.get_operation
    reads = {"count": 0}

    def mutating_get(query, *, owner_id, assignment_id, lock=False):
        result = original_get(query, owner_id=owner_id, assignment_id=assignment_id, lock=lock)
        if (not lock and assignment_id == waiter.assignment_id and result is not None
                and result.assignment.phase == "awaiting_event"):
            reads["count"] += 1
            if reads["count"] == 1:
                return None
        return result

    monkeypatch.setattr(repository, "get_operation", mutating_get)
    finished = await complete_source(api, source)
    assert finished.lifecycle == "completed"
    assert reads["count"] == 1
    monkeypatch.setattr(repository, "get_operation", original_get)
    assert current(api.runtime, waiter).phase == "awaiting_event"
    assert continuation_audits(api) == []
    fired = await fire_again(api, source, finished.state_version)
    assert fired == 1
    assert current(api.runtime, waiter).phase == "waiting"


async def test_waiter_state_change_between_scan_and_release_is_rechecked(api, monkeypatch):
    from astralplane.repositories.assignment_models import AssignmentOperationRead
    from dataclasses import replace as dataclass_replace

    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    repository = api.runtime.repositories.assignments
    original_get = repository.get_operation

    def mutating_get(query, *, owner_id, assignment_id, lock=False):
        result = original_get(query, owner_id=owner_id, assignment_id=assignment_id, lock=lock)
        if (not lock and assignment_id == waiter.assignment_id and result is not None
                and result.assignment.phase == "awaiting_event"):
            moved = dataclass_replace(result.assignment, phase="waiting")
            return AssignmentOperationRead(moved, result.disposition,
                                           result.continuation_supported,
                                           result.result_reference, result.terminal_outcome)
        return result

    original_accept = repository.accept_wake
    calls = []

    def spy_accept(*args, **kwargs):
        calls.append(kwargs.get("event_id"))
        return original_accept(*args, **kwargs)

    monkeypatch.setattr(repository, "accept_wake", spy_accept)
    monkeypatch.setattr(repository, "get_operation", mutating_get)
    finished = await complete_source(api, source)
    assert finished.lifecycle == "completed"
    assert calls == []
    monkeypatch.setattr(repository, "get_operation", original_get)
    monkeypatch.setattr(repository, "accept_wake", original_accept)
    after = current(api.runtime, waiter)
    assert after.phase == "awaiting_event"
    assert after.wake_generation == waiter.wake_generation
    assert continuation_audits(api) == []


async def test_scan_failures_roll_back_completion(api, monkeypatch):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    repository = api.runtime.repositories.assignments
    original_scan = repository.list_owner_event_waits

    for failure in (AssignmentError("work_repository_unavailable", 503),
                    RuntimeError("synthetic scan failure")):
        def failing_scan(tx, *, owner_id, limit, after_id, _failure=failure, **event_filters):
            raise _failure

        monkeypatch.setattr(repository, "list_owner_event_waits", original_scan)
        stage_source, stage_waiter = await paired(api)
        await subscribe(api, stage_waiter, stage_source)
        monkeypatch.setattr(repository, "list_owner_event_waits", failing_scan)
        with pytest.raises(AssignmentError):
            await complete_source(api, stage_source)
        assert current(api.runtime, stage_source).lifecycle == "active"
        assert current(api.runtime, stage_waiter).phase == "awaiting_event"
        assert module()._redrive_pending == {}
    monkeypatch.setattr(repository, "list_owner_event_waits", original_scan)


async def test_confirm_rechecks_source_existence_and_support(api):
    source, waiter = await paired(api)
    proposal = await propose_for(api, waiter, source)
    with api.runtime.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment WHERE id=%s", (source.assignment_id,))
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, source.assignment_id, proposal), caller=await caller(api, api.fixture[2]))
    second, other_waiter = await paired(api)
    proposal = await propose_for(api, other_waiter, second)
    with api.runtime.transaction() as tx:
        tx.execute("UPDATE persistent_assignment SET "
                   "data=jsonb_set(data,'{operation,version}','3'::jsonb) WHERE id=%s",
                   (second.assignment_id,))
    with pytest.raises(AssignmentError, match="assignment_version_unsupported"):
        await service_for(api).confirm(other_waiter.assignment_id, confirm_body(
            other_waiter, second.assignment_id, proposal), caller=await caller(api, api.fixture[2]))


async def test_confirm_refuses_waiter_waiting_on_a_different_event(api):
    source, waiter = await paired(api)
    await api.service.store.call("set_owner_event_wait", owner_id=waiter.owner_id,
        assignment_id=waiter.assignment_id,
        expected_instruction_revision=waiter.instruction_revision,
        expected_control_epoch=waiter.control_epoch,
        expected_state_version=waiter.state_version, submission_id=str(uuid4()),
        submission_digest=digest("fixture-owner-wait"), event_key="owner:" + str(uuid4()),
        source_revision=0)
    import time as time_module
    module()._proposals(api.orch).setdefault(waiter.owner_id, {})[uuid4().hex] = {
        "waiter": waiter.assignment_id, "source": source.assignment_id,
        "created_at": time_module.time()}
    proposal = {"proposal_id": next(iter(module()._proposals(api.orch)[waiter.owner_id])),
                "proposal_digest": module()._proposal_digest(waiter.assignment_id,
                                                             source.assignment_id)}
    with pytest.raises(AssignmentError, match="assignment_event_key_conflict"):
        await service_for(api).confirm(waiter.assignment_id, confirm_body(
            waiter, source.assignment_id, proposal), caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, waiter).phase == "awaiting_event"


async def test_private_mutation_and_type_guards_close_revoke_and_propose(api):
    source, waiter = await paired(api)
    proposal = await propose_for(api, waiter, source)
    body = confirm_body(waiter, source.assignment_id, proposal)
    body.expected_revision = -5
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).confirm(waiter.assignment_id, body,
                                       caller=await caller(api, api.fixture[2]))
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await service_for(api).revoke(waiter.assignment_id, source.assignment_id,
                                      revoke_body(waiter), caller=None)
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).revoke(waiter.assignment_id, source.assignment_id,
                                      revoke_body(waiter).model_dump(),
                                      caller=await caller(api, api.fixture[2]))


async def test_audit_helpers_refuse_wrong_command_shapes(api):
    from orchestrator.work_control_audit import WorkControlAudit

    source, _waiter = await paired(api)
    record = current(api.runtime, source)
    audit = WorkControlAudit(api.service.assignments)
    with api.runtime.transaction() as tx:
        with pytest.raises(AssignmentError, match="work_control_unavailable"):
            audit.append(tx, owner_id=record.owner_id, command="subscribe",
                                     record=record, submission_id=str(uuid4()),
                                     source_id=str(uuid4()), target_id=record.assignment_id,
                                     decision="approve")
        with pytest.raises(AssignmentError, match="work_control_unavailable"):
            audit.append_completion_wake(tx, owner_id=record.owner_id,
                                         record=record, source_id=str(uuid4()),
                                         revision=0, outcome="completed")
        with pytest.raises(AssignmentError, match="work_control_unavailable"):
            audit.append_completion_wake(tx, owner_id=record.owner_id,
                                         record=record, source_id=str(uuid4()),
                                         revision=1, outcome="running")


async def test_inspection_reports_a_deleted_source(api):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    from orchestrator.work_service import WorkService
    view = await WorkService(api.service.assignments).continuations(
        waiter.owner_id, {"sub": waiter.owner_id}, waiter.assignment_id)
    assert view["continuation"]["source_missing"] is False
    with api.runtime.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment WHERE id=%s", (source.assignment_id,))
    view = await WorkService(api.service.assignments).continuations(
        waiter.owner_id, {"sub": waiter.owner_id}, waiter.assignment_id)
    assert view["continuation"]["source_missing"] is True
    assert view["continuation"]["source_disposition"] is None

async def test_propose_refuses_an_unsupported_source_and_confirm_guards(api, monkeypatch):
    source, waiter = await paired(api)
    with api.runtime.transaction() as tx:
        tx.execute("UPDATE persistent_assignment SET "
                   "data=jsonb_set(data,'{operation,version}','3'::jsonb) WHERE id=%s",
                   (source.assignment_id,))
    with pytest.raises(AssignmentError, match="assignment_version_unsupported"):
        await propose_for(api, waiter, source)
    live, other_waiter = await paired(api)
    proposal = await propose_for(api, other_waiter, live)
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await service_for(api).confirm(other_waiter.assignment_id, confirm_body(
            other_waiter, live.assignment_id, proposal), caller=None)
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).confirm(other_waiter.assignment_id, confirm_body(
            other_waiter, other_waiter.assignment_id, proposal),
            caller=await caller(api, api.fixture[2]))
    repository = api.runtime.repositories.assignments
    original_wait = repository.set_owner_event_wait
    monkeypatch.setattr(repository, "set_owner_event_wait", lambda *args, **kwargs: {})
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await service_for(api).confirm(other_waiter.assignment_id, confirm_body(
            other_waiter, live.assignment_id, proposal), caller=await caller(api, api.fixture[2]))
    monkeypatch.setattr(repository, "set_owner_event_wait", original_wait)
    assert wait_of(api.runtime, other_waiter)[0] is None

    await subscribe(api, other_waiter, live)
    subscribed = current(api.runtime, other_waiter)
    original_accept = repository.accept_wake
    monkeypatch.setattr(repository, "accept_wake", lambda *args, **kwargs: {})
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await service_for(api).revoke(other_waiter.assignment_id, live.assignment_id,
                                      revoke_body(subscribed),
                                      caller=await caller(api, api.fixture[2]))
    monkeypatch.setattr(repository, "accept_wake", original_accept)
    assert current(api.runtime, other_waiter).phase == "awaiting_event"


async def test_fire_pages_across_scan_boundaries(api, monkeypatch):
    source, waiter, extra = await paired(api, count=3)
    await subscribe(api, waiter, source)
    await subscribe(api, extra, source)
    module()._SCAN_PAGE = 1
    try:
        finished = await complete_source(api, source)
        assert finished.lifecycle == "completed"
        assert current(api.runtime, waiter).phase == "waiting"
        assert current(api.runtime, extra).phase == "waiting"
        assert len(continuation_audits(api)) == 2
    finally:
        module()._SCAN_PAGE = 100


async def test_redrive_counts_attempts_for_nonterminal_sources(api):
    source, waiter = await paired(api)
    module().schedule_redrive(waiter.owner_id, source.assignment_id)
    for _ in range(module()._REDRIVE_ATTEMPTS):
        await module().redrive_pending(api.service.assignments)
    assert module()._redrive_pending == {(waiter.owner_id, source.assignment_id):
                                         module()._REDRIVE_ATTEMPTS}
    await module().redrive_pending(api.service.assignments)
    assert module()._redrive_pending == {}

async def test_continuation_read_defends_against_malformed_source_states(api, monkeypatch):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    service = WorkService(api.service.assignments)
    repository = api.runtime.repositories.assignments
    original_get = repository.get_operation

    def refusing_get(tx, *, owner_id, assignment_id, lock=False):
        raise AssignmentError("work_repository_unavailable", 503)

    monkeypatch.setattr(repository, "get_operation", refusing_get)
    with pytest.raises(AssignmentError, match="work_repository_unavailable"):
        await service.continuations(waiter.owner_id, {"sub": waiter.owner_id},
                                    waiter.assignment_id)
    monkeypatch.setattr(repository, "get_operation", original_get)

    class NoGetRepository:
        pass

    monkeypatch.setattr(service.store, "repository", NoGetRepository())
    with pytest.raises(AssignmentError, match="work_repository_unavailable"):
        await service.continuations(waiter.owner_id, {"sub": waiter.owner_id},
                                    waiter.assignment_id)
    monkeypatch.setattr(service.store, "repository", repository)

async def test_continuation_read_tolerates_unreadable_sources(api, monkeypatch):
    source, waiter = await paired(api)
    await subscribe(api, waiter, source)
    service = WorkService(api.service.assignments)
    repository = api.runtime.repositories.assignments
    original_get = repository.get_operation
    reads = {"count": 0}

    def failing_source_get(tx, *, owner_id, assignment_id, lock=False):
        if assignment_id == source.assignment_id:
            reads["count"] += 1
            raise ValueError("synthetic unreadable source")
        return original_get(tx, owner_id=owner_id, assignment_id=assignment_id, lock=lock)

    monkeypatch.setattr(repository, "get_operation", failing_source_get)
    view = await service.continuations(waiter.owner_id, {"sub": waiter.owner_id},
                                       waiter.assignment_id)
    assert view["continuation"]["source_operation_id"] == source.assignment_id
    assert reads["count"] == 1
    monkeypatch.setattr(repository, "get_operation", original_get)

    def broken_view(tx, repo):
        raise ValueError("synthetic projection failure")

    monkeypatch.setattr("orchestrator.work_completion_wake.continuation_view",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            ValueError("synthetic projection failure")))
    with pytest.raises(AssignmentError, match="work_read_unavailable"):
        await service.continuations(waiter.owner_id, {"sub": waiter.owner_id},
                                    waiter.assignment_id)
    monkeypatch.setattr(repository, "get_operation", original_get)


async def test_completion_wake_audit_refuses_unreadable_ids(api):
    from orchestrator.work_control_audit import WorkControlAudit

    source, _waiter = await paired(api)
    record = current(api.runtime, source)
    audit = WorkControlAudit(api.service.assignments)
    with api.runtime.transaction() as tx:
        with pytest.raises(AssignmentError, match="work_control_unavailable"):
            audit.append_completion_wake(tx, owner_id=record.owner_id, record=record,
                                         source_id="not-a-uuid", revision=1,
                                         outcome="completed")


async def test_owner_confirmation_refuses_relevant_fanout_capacity_without_consuming_a_wait(api, monkeypatch):
    from orchestrator import work_subscriptions

    monkeypatch.setattr(work_subscriptions, "_MAX_EVENT_WAITERS", 1, raising=False)
    source, first = await paired(api)
    _, refused = await paired(api)
    await subscribe(api, first, source)
    proposal = await propose_for(api, refused, source)
    with pytest.raises(AssignmentError, match="work_completion_capacity_exhausted"):
        await service_for(api).confirm(refused.assignment_id, confirm_body(
            refused, source.assignment_id, proposal), caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, refused).operation.get("control", {}).get("wait") is None
