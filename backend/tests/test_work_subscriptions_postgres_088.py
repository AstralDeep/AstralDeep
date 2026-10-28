"""Tests for orchestrator/work_subscriptions.py against real Plane: framework-authorized
subscribe/list/cancel of completion subscriptions, authority/ownership/duplicate refusals,
and the dispatcher that wakes parked targets through the real runner completion, framework
stop, and owner stop/delete paths with audited, redrivable fires.
"""

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentDefinition, AssignmentEpisodeCompletion, AssignmentOperationAuthority,
    AssignmentOperationSpec,
)
from astralplane.repositories.history import SessionExecutionObservation
from cryptography.fernet import Fernet

from audit.repository import AuditRepository
from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.work_admission import WorkAdmissionCoordinator
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_controls import (
    WorkControlRequest, WorkControlService, WorkDeleteRequest,
)
from orchestrator.work_operations import FrameworkWorkOperations
from orchestrator.work_subscriptions import (
    WorkSubscriptionService, _redrive_pending, completion_event_key, completion_event_source,
    fire_completions, redrive_pending, schedule_redrive,
)
from persistent_agents.config import RunnerConfig
from persistent_agents.execution import ActionExecutor
from persistent_agents.models import AssignmentError
from persistent_agents.runner import AssignmentRunner, OneShotEpisodeResult, OneShotLifecycle
from persistent_agents.runtime_values import digest
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from personalization.phi_gate import PHIGate
from tests.helpers.session_consent_088 import consent_from_store
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import signing_key as signing_key

runtime = plane

_ALL_SCOPES = ("operations.submit", "operations.read", "operations.control", "artifacts.read")
_LIMITS = {"max_retries": 1, "max_concurrent_tasks": 1, "max_depth": 1, "max_tasks": 2,
           "model_calls": 2, "tool_calls": 2, "tokens": 100, "elapsed_ms": 1000}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-work-subscriptions-audit-key")


@pytest.fixture(autouse=True)
def _clean_redrive():
    _redrive_pending.clear()
    yield
    _redrive_pending.clear()


@pytest.fixture
def audit(runtime):
    return AuditRepository(plane_runtime=runtime)


@pytest.fixture
def credentials(runtime, audit):
    return FrameworkCredentialService(plane_runtime=runtime, audit=audit)


@pytest.fixture
def work(runtime, fixture, audit, credentials, monkeypatch):
    def credential_clock():
        with runtime.transaction() as tx:
            return tx.fetch_one("SELECT clock_timestamp() AS now")["now"]

    monkeypatch.setattr("orchestrator.framework_credentials._now", credential_clock)
    _, owner, sid, _, _ = fixture
    orch = SimpleNamespace(history=SimpleNamespace(get_chat=lambda *args, **kwargs: None),
                           audit_repo=audit)
    service = AssignmentService(orch, AssignmentStore(plane_runtime=runtime), enabled=True,
                                phi_gate=PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))
    ops = FrameworkWorkOperations(assignments=service, credentials=credentials)
    return SimpleNamespace(runtime=runtime, store=fixture[0], owner=owner, sid=sid, audit=audit,
                           credentials=credentials, service=service, ops=ops, orch=orch)


def _issue(work, *, scopes=_ALL_SCOPES):
    _view, token = work.credentials.issue(
        owner_id=work.owner,
        caller=consent_from_store(work.store, work.owner, work.sid).observation,
        name="SDK key", scopes=scopes, expires_in_seconds=3600, max_admissions=1000)
    caller = work.credentials.resolve_bearer(token)
    assert caller is not None
    return caller


def _operation(work, *, owner=None, sid=None, limits=None, deadline_seconds=3600):
    store = work.store
    owner = owner or work.owner
    sid = sid or work.sid
    state = store.capture_execution_reference(owner_id=owner, session_id=sid).state
    hard_expiry = datetime.fromtimestamp(state.credential.hard_expires_at, timezone.utc)
    observation = SessionExecutionObservation(
        credential=state.credential, started_at=state.observed_at,
        valid_until=min(state.observed_at + timedelta(seconds=15), hard_expiry))
    identity = str(uuid4())
    with work.runtime.transaction() as tx:
        work.runtime.repositories.history.sessions.bound_request_execution_waits(tx)
        return work.runtime.repositories.assignments.create_operation(
            tx, owner_id=owner, assignment_id=identity, origin_namespace="fixture",
            caller_key=identity, command_digest=digest(identity), authority=observation,
            definition=AssignmentDefinition(
                name="Synthetic completion work", instructions="Read one public source.",
                source={}, allowed_tools=(), consented_scopes=(), offline_grant_id=None,
                limits=_LIMITS if limits is None else limits),
            operation=AssignmentOperationSpec(
                "chat", AssignmentOperationAuthority(
                    owner, "interactive", "session_incarnation", state.credential.incarnation_id,
                    min(state.observed_at + timedelta(seconds=3600), hard_expiry)),
                state.observed_at + timedelta(seconds=deadline_seconds), "none"))


async def _read(work, record_or_id, owner=None):
    identity = record_or_id if isinstance(record_or_id, str) else record_or_id.assignment_id
    return await work.service.store.call("get_operation", owner_id=owner or work.owner,
                                         assignment_id=identity)


async def _subscribe(work, caller, source, target, *, submission_id=None, expected_revision=1):
    return await work.ops.subscribe(
        caller, source_operation_id=_identity_of(source),
        target_operation_id=_identity_of(target),
        submission_id=submission_id or str(uuid4()), expected_revision=expected_revision)


def _identity_of(record_or_id):
    return record_or_id if isinstance(record_or_id, str) else record_or_id.assignment_id


async def _audits(work, owner, action_type):
    return [row for row in work.audit.list_for_user(owner)[0]
            if row.action_type == action_type]


@pytest.mark.asyncio
async def test_subscribe_parks_target_lists_and_replays_duplicates(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    result = await _subscribe(work, caller, source, target)
    assert result["applied"] is True
    assert result["subscription"] == {"source_operation_id": source.assignment_id,
                                      "target_operation_id": target.assignment_id}
    read = await _read(work, target)
    key = completion_event_key(source.assignment_id)
    assert read.assignment.phase == "awaiting_event"
    assert read.assignment.operation["control"]["wait"] == {"event_key": key,
                                                            "source_revision": 0}
    assert read.assignment.operation["control"]["watermarks"][key] == 0

    replay = await _subscribe(work, caller, source, target)
    assert replay["applied"] is False

    listed = await work.ops.subscriptions(caller, limit=50)
    assert listed["next_after_id"] is None
    assert len(listed["subscriptions"]) == 1
    entry = listed["subscriptions"][0]
    assert entry["source_operation_id"] == source.assignment_id
    assert entry["target_operation_id"] == target.assignment_id
    assert entry["target_disposition"] == "awaiting_event"
    assert entry["target_phase"] == "awaiting_event"
    assert entry["source_disposition"] == "queued"
    assert entry["source_missing"] is False

    empty = await work.ops.subscriptions(caller, limit=50, after_id=target.assignment_id)
    assert empty["subscriptions"] == []

    audits = await _audits(work, work.owner, "assignment_subscribe")
    assert len(audits) == 1
    assert audits[0].outputs_meta["source_operation_id"] == source.assignment_id
    assert audits[0].outputs_meta["target_operation_id"] == target.assignment_id


@pytest.mark.asyncio
async def test_same_submission_id_replays_and_conflicts_on_divergent_content(work):
    caller = _issue(work)
    source, target, other = _operation(work), _operation(work), _operation(work)
    submission = str(uuid4())
    first = await _subscribe(work, caller, source, target, submission_id=submission)
    assert first["applied"] is True
    replay = await _subscribe(work, caller, source, target, submission_id=submission)
    assert replay["applied"] is False
    with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
        await _subscribe(work, caller, other, target, submission_id=submission)


@pytest.mark.asyncio
async def test_second_source_conflicts_while_a_subscription_is_armed(work):
    caller = _issue(work)
    source, target, rival = _operation(work), _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    with pytest.raises(AssignmentError, match="assignment_event_key_conflict"):
        await _subscribe(work, caller, rival, target)


@pytest.mark.asyncio
async def test_subscribe_requires_owned_source_and_target(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    other_owner, other_sid = str(uuid4()), uuid4().hex
    work.store.create(other_sid, user_id=other_owner, access_token="synthetic-other",
                      refresh_token="synthetic-other-refresh", hard_max_seconds=3600)
    try:
        foreign = _operation(work, owner=other_owner, sid=other_sid)
        with pytest.raises(AssignmentError, match="work_not_found"):
            await _subscribe(work, caller, foreign, target)
        with pytest.raises(AssignmentError, match="work_not_found"):
            await _subscribe(work, caller, source, foreign)
        with pytest.raises(AssignmentError, match="work_not_found"):
            await work.ops.subscribe(caller, source_operation_id=str(uuid4()),
                                     target_operation_id=target.assignment_id,
                                     submission_id=str(uuid4()), expected_revision=1)
    finally:
        work.store.delete(other_sid)


@pytest.mark.asyncio
async def test_subscribe_refuses_self_target_and_terminal_source(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await _subscribe(work, caller, target, target)
    await work.ops.cancel(caller, operation_id=source.assignment_id,
                          submission_id=str(uuid4()),
                          expected_revision=(await _read(work, source)).assignment.state_version)
    with pytest.raises(AssignmentError, match="assignment_not_waiting"):
        await _subscribe(work, caller, source, target)


@pytest.mark.asyncio
async def test_subscribe_refuses_framework_profile_targets(work):
    caller = _issue(work)
    source = _operation(work)
    framework_target = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                             name="Framework target", instructions="B")
    read = await _read(work, framework_target["id"])
    assert read.assignment.operation["authority"]["origin"] == "framework"
    with pytest.raises(AssignmentError, match="framework_control_unavailable"):
        await work.ops.subscribe(caller, source_operation_id=source.assignment_id,
                                 target_operation_id=framework_target["id"],
                                 submission_id=str(uuid4()), expected_revision=1)


@pytest.mark.asyncio
async def test_subscribe_requires_control_scope(work):
    caller = _issue(work, scopes=("operations.read",))
    source, target = _operation(work), _operation(work)
    with pytest.raises(AssignmentError, match="framework_scope_required"):
        await _subscribe(work, caller, source, target)
    listed = await work.ops.subscriptions(caller, limit=50)
    assert listed["subscriptions"] == []


@pytest.mark.asyncio
async def test_subscribe_refuses_expired_and_revoked_credentials(work):
    expired_caller = _issue(work)
    revoked_caller = _issue(work)
    source, target = _operation(work), _operation(work)
    with work.runtime.transaction() as tx:
        tx.execute("UPDATE framework_credential SET created_at = clock_timestamp() - interval '2 seconds', "
                   "expires_at = clock_timestamp() - interval '1 second' "
                   "WHERE id=%s", (expired_caller.credential_id,))
    with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
        await _subscribe(work, expired_caller, source, target)
    work.credentials.revoke(owner_id=work.owner, credential_id=revoked_caller.credential_id)
    with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
        await _subscribe(work, revoked_caller, source, target)


@pytest.mark.asyncio
async def test_completion_after_registered_wait_wakes_the_target(work):
    caller, control_caller = _issue(work), _issue(work)
    source = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                   name="Completion control", instructions="Read public data.")
    target = _operation(work)
    assert (await _subscribe(work, caller, source["id"], target))["applied"] is True
    cancelled = await work.ops.cancel(control_caller, operation_id=source["id"],
                                      expected_revision=source["revision"], submission_id=str(uuid4()))
    assert cancelled["applied"] is True
    assert (await _read(work, source["id"])).assignment.operation["terminal_outcome"] == "cancelled"
    current = (await _read(work, target)).assignment
    assert current.phase == "waiting"
    assert current.operation["control"]["wait"] is None
    assert current.operation["control"]["watermarks"][completion_event_key(source["id"])] >= 1
    assert len(await _audits(work, work.owner, "work.completion_wake")) == 1


@pytest.mark.asyncio
async def test_completion_between_source_read_and_wait_registration_is_not_lost(work, monkeypatch):
    import threading

    from orchestrator import work_subscriptions

    caller, control_caller = _issue(work), _issue(work)
    source = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                   name="Concurrent completion", instructions="Read public data.")
    source_id = source["id"]
    target = _operation(work)
    observed, release = threading.Event(), threading.Event()
    backend_ids = {}
    original_read = work_subscriptions._owned_operation

    def hold_source(tx, repository, owner, identity, **options):
        result = original_read(tx, repository, owner, identity, **options)
        if identity == source_id and not observed.is_set():
            assert result.assignment.operation.get("terminal_outcome") is None
            backend_ids["subscriber"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            observed.set()
            assert release.wait(timeout=10), "source-read barrier did not settle"
        return result

    def subscriber_blocks_completion():
        with work.runtime.transaction() as tx:
            blocked = tx.fetch_all(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                "AND %s = ANY(pg_blocking_pids(pid))", (backend_ids["subscriber"],))
            if blocked:
                assert len(blocked) == 1
                backend_ids["completion"] = blocked[0]["pid"]
                return True
            return False

    monkeypatch.setattr(work_subscriptions, "_owned_operation", hold_source)
    subscription = asyncio.create_task(_subscribe(work, caller, source_id, target))
    completion = None
    try:
        assert await asyncio.to_thread(observed.wait, 10), "source read was not reached"
        completion = asyncio.create_task(work.ops.cancel(control_caller, operation_id=source_id,
            expected_revision=source["revision"], submission_id=str(uuid4())))
        async with asyncio.timeout(10):
            while not completion.done():
                if await asyncio.to_thread(subscriber_blocks_completion):
                    break
                await asyncio.sleep(0)
        assert "completion" in backend_ids, "completion never waited on the subscriber transaction"
        assert backend_ids["subscriber"] != backend_ids["completion"]
        release.set()
        subscribed, cancelled = await asyncio.gather(subscription, completion, return_exceptions=True)
        assert isinstance(cancelled, dict) and cancelled["applied"] is True
        assert (await _read(work, source_id)).assignment.operation["terminal_outcome"] == "cancelled"
        current = (await _read(work, target)).assignment
        assert current.operation["control"]["wait"] is None
        if isinstance(subscribed, AssignmentError):
            assert (subscribed.code, subscribed.status_code) == ("assignment_not_waiting", 409)
            assert current.phase == "queued"
        else:
            assert isinstance(subscribed, dict) and subscribed["applied"] is True
            assert current.phase == "waiting"
            assert current.operation["control"]["watermarks"][completion_event_key(source_id)] >= 1
            assert len(await _audits(work, work.owner, "work.completion_wake")) == 1
    finally:
        release.set()
        async with asyncio.timeout(10):
            await asyncio.gather(subscription, *([completion] if completion else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_releases_the_wait_and_repeat_cancel_is_not_found(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    armed = (await _read(work, target)).assignment
    cancelled = await work.ops.unsubscribe(
        caller, source_operation_id=source.assignment_id,
        target_operation_id=target.assignment_id,
        submission_id=str(uuid4()), expected_revision=armed.state_version)
    assert cancelled["applied"] is True
    read = await _read(work, target)
    key = completion_event_key(source.assignment_id)
    assert read.assignment.phase == "waiting" and read.assignment.lifecycle == "active"
    assert read.assignment.operation["control"]["wait"] is None
    assert read.assignment.operation["control"]["watermarks"][key] == 1
    with pytest.raises(AssignmentError, match="work_not_found"):
        await work.ops.unsubscribe(caller, source_operation_id=source.assignment_id,
                                   target_operation_id=target.assignment_id,
                                   submission_id=str(uuid4()),
                                   expected_revision=read.assignment.state_version)
    listed = await work.ops.subscriptions(caller, limit=50)
    assert listed["subscriptions"] == []
    rearmed = await _subscribe(work, caller, source, target,
                               expected_revision=read.assignment.state_version)
    assert rearmed["applied"] is True
    audits = await _audits(work, work.owner, "assignment_unsubscribe")
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_list_reports_missing_sources_and_pages_by_target_id(work):
    caller = _issue(work)
    source, target, second = _operation(work), _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    await _subscribe(work, caller, source, second)
    with work.runtime.transaction() as tx:
        tx.execute("DELETE FROM persistent_assignment WHERE id=%s AND owner_user_id=%s",
                   (source.assignment_id, work.owner))
    listed = await work.ops.subscriptions(caller, limit=1)
    assert len(listed["subscriptions"]) == 1
    assert listed["next_after_id"] is not None
    assert listed["subscriptions"][0]["source_missing"] is True
    assert listed["subscriptions"][0]["source_disposition"] is None
    remainder = await work.ops.subscriptions(caller, limit=10,
                                             after_id=listed["next_after_id"])
    assert len(remainder["subscriptions"]) == 1
    assert remainder["next_after_id"] is None
    assert remainder["subscriptions"][0]["source_missing"] is True


async def _runner(work):
    coordinator = await asyncio.to_thread(WorkAdmissionCoordinator.from_plane,
                                          plane_runtime=work.runtime,
                                          slot_lease=timedelta(seconds=21))
    work.orch.work_admission = coordinator
    work.orch._unbind_machine_turn = lambda _: None
    handler = AsyncMock(side_effect=AssertionError("no episode runs in subscription fixtures"))
    return AssignmentRunner(work.orch, work.service,
                            config=RunnerConfig(concurrency=2, lease_seconds=30),
                            one_shot=OneShotLifecycle(work.store, handler))


async def _claim_source(work, runner, record):
    authority = await runner._operation_authority(record)
    claim = await work.service.store.operation_lifecycle_transaction(
        authority=authority,
        callback=lambda tx, repo, current: repo.claim_operation_for_administration(
            tx, owner_id=current.owner_id, assignment_id=current.assignment_id,
            expected_state_version=current.state_version, worker_id=runner.worker_id,
            lease_seconds=30, authority=authority.observation))
    operation_fence = await runner._admit(claim)
    executor = ActionExecutor(runner, claim, operation_fence, object())
    authority = await runner._operation_authority(record)

    def bind(tx, repo, current):
        repo.bind_operation(tx, fence=claim.fence, binding=executor.binding)
        return repo.assert_current_assignment_execution(
            tx, fence=claim.fence, binding=executor.binding, authority=authority.observation)

    await work.service.store.operation_lifecycle_transaction(authority=authority,
                                                             fence=claim.fence, callback=bind)
    return executor


def _terminal_completion(record, label):
    return AssignmentEpisodeCompletion(
        expected_state_version=record.state_version, checkpoint=record.checkpoint,
        completion_digest=digest(["subscription-fire", label, record.assignment_id]),
        completed=True, terminal_outcome="completed", next_wake_at=None,
        wake_reason="assignment_finished")


@pytest.mark.asyncio
async def test_runner_completion_fires_the_subscription_and_wakes_the_target(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    target_before = (await _read(work, target)).assignment
    await _subscribe(work, caller, source, target)
    runner = await _runner(work)
    try:
        executor = await _claim_source(work, runner, source)
        before = (await _read(work, source)).assignment
        await runner._finish_operation(executor, OneShotEpisodeResult(
            before, _terminal_completion(before, "runner")))
    finally:
        await runner.stop()
    key = completion_event_key(source.assignment_id)
    woken = (await _read(work, target)).assignment
    assert woken.phase == "waiting" and woken.lifecycle == "active"
    assert woken.operation["control"]["wait"] is None
    assert woken.operation["control"]["watermarks"][key] >= 1
    assert woken.wake_generation == target_before.wake_generation + 1
    finished = await _read(work, source)
    assert finished.assignment.lifecycle == "completed"
    fires = await _audits(work, work.owner, "work.completion_wake")
    assert len(fires) == 1
    assert fires[0].outputs_meta["source_operation_id"] == source.assignment_id
    assert fires[0].outputs_meta["target_operation_id"] == target.assignment_id
    assert fires[0].outputs_meta["outcome"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["retryable", "exhausted", "deadline", "completed_default"])
async def test_runner_uses_persisted_terminal_outcome_for_subscription_wakes(work, mode):
    caller = _issue(work)
    source = _operation(work, limits={**_LIMITS, "max_retries": 0 if mode == "exhausted" else 1},
                        deadline_seconds=1800 if mode == "deadline" else 3600)
    target = _operation(work)
    await _subscribe(work, caller, source, target)
    parked = (await _read(work, target)).assignment
    runner = await _runner(work)
    try:
        executor = await _claim_source(work, runner, source)
        if mode in {"exhausted", "retryable"}:
            authority = await runner._operation_authority(executor.record)
            result = await runner._hold_operation(executor, authority)
        else:
            before = (await _read(work, source)).assignment
            completion = replace(_terminal_completion(before, mode), terminal_outcome=None)
            if mode == "deadline":
                completion = replace(completion, completed=False, phase="failed",
                    wake_reason="assignment_failed", safe_error_code="assignment_failed",
                    next_wake_at=datetime.fromisoformat(source.operation["deadline_at"])
                    + timedelta(seconds=1))
            result = await runner._finish_operation(executor, OneShotEpisodeResult(before, completion))
    finally:
        await runner.stop()
    finished = (await _read(work, source)).assignment
    after = (await _read(work, target)).assignment
    wakes = await _audits(work, work.owner, "work.completion_wake")
    assert result == finished
    if mode == "retryable":
        assert finished.lifecycle == "active" and finished.phase == "failed"
        assert finished.next_wake_at is not None
        assert finished.operation.get("terminal_outcome") is None
        assert after == parked and wakes == []
    else:
        expected = "completed" if mode == "completed_default" else "failed"
        assert finished.lifecycle == "completed"
        assert finished.operation["terminal_outcome"] == expected
        assert finished.next_wake_at is None
        assert after.phase == "waiting" and after.operation["control"]["wait"] is None
        assert after.wake_generation == parked.wake_generation + 1
        assert after.operation["control"]["watermarks"][completion_event_key(source.assignment_id)] >= finished.state_version
        assert len(wakes) == 1 and wakes[0].outputs_meta["outcome"] == expected
        assert wakes[0].outputs_meta["event_revision"] == finished.state_version
    assert work.audit.verify_chain(work.owner) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wake", "audit"])
async def test_derived_runner_failure_rolls_back_with_wake_and_recovers_once(work, monkeypatch, failure):
    caller = _issue(work)
    source = _operation(work, limits={**_LIMITS, "max_retries": 0})
    target = _operation(work)
    await _subscribe(work, caller, source, target)
    parked = (await _read(work, target)).assignment
    runner = await _runner(work)

    def refuse(*args, **kwargs):
        raise AssignmentError("work_control_unavailable", 503)

    try:
        executor = await _claim_source(work, runner, source)
        authority = await runner._operation_authority(executor.record)
        before = (await _read(work, source)).assignment
        with monkeypatch.context() as failing:
            failing.setattr(work.service.store.repository if failure == "wake" else work.audit,
                            "accept_wake" if failure == "wake" else "insert_in_transaction", refuse)
            with pytest.raises(AssignmentError, match="work_control_unavailable"):
                await runner._hold_operation(executor, authority)
        assert (await _read(work, source)).assignment == before
        assert (await _read(work, target)).assignment == parked
        assert await _audits(work, work.owner, "work.completion_wake") == []
        result = await runner._hold_operation(executor, authority)
        assert result.lifecycle == "completed" and result.operation["terminal_outcome"] == "failed"
        assert (await _read(work, source)).assignment == result
        resumed = (await _read(work, target)).assignment
        assert resumed.phase == "waiting" and resumed.operation["control"]["wait"] is None
        assert resumed.wake_generation == parked.wake_generation + 1
        wakes = await _audits(work, work.owner, "work.completion_wake")
        assert len(wakes) == 1 and wakes[0].outputs_meta["outcome"] == "failed"
        assert await runner._hold_operation(executor, authority) is None
        assert len(await _audits(work, work.owner, "work.completion_wake")) == 1
        assert work.audit.verify_chain(work.owner) is None
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_uncertain_wake_rolls_back_runner_completion_and_retry_recovers(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    runner = await _runner(work)
    repository = work.service.store.repository
    original_accept = repository.accept_wake

    def refusing_accept(tx, **kwargs):
        raise AssignmentError("assignment_action_uncertain", 409)

    try:
        repository.accept_wake = refusing_accept
        executor = await _claim_source(work, runner, source)
        before = (await _read(work, source)).assignment
        outcome = OneShotEpisodeResult(before, _terminal_completion(before, "skip"))
        with pytest.raises(AssignmentError, match="assignment_action_uncertain"):
            await runner._finish_operation(executor, outcome)
        assert (await _read(work, source)).assignment.lifecycle == "active"
        assert (await _read(work, target)).assignment.phase == "awaiting_event"
        assert await _audits(work, work.owner, "work.completion_wake") == []
        assert _redrive_pending == {}
        repository.accept_wake = original_accept
        await runner._finish_operation(executor, outcome)
        assert (await _read(work, target)).assignment.phase == "waiting"
        assert len(await _audits(work, work.owner, "work.completion_wake")) == 1
    finally:
        repository.accept_wake = original_accept
        await runner.stop()


@pytest.mark.asyncio
async def test_redrive_drops_exhausted_and_ignores_malformed_entries(work):
    source, _target = _operation(work), _operation(work)
    schedule_redrive(work.owner, source.assignment_id)
    for _ in range(3):
        assert await redrive_pending(work.service) == 0
        assert _redrive_pending[(work.owner, source.assignment_id)] >= 1
    await redrive_pending(work.service)
    assert (work.owner, source.assignment_id) not in _redrive_pending
    schedule_redrive(work.owner, "not-a-uuid")
    assert ("not-a-uuid" in {source_id for _, source_id in _redrive_pending}) is False


@pytest.mark.asyncio
async def test_framework_stop_fires_subscriptions(work):
    caller = _issue(work)
    target = _operation(work)
    framework_source = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                             name="Framework source", instructions="B")
    target_before = (await _read(work, target)).assignment
    await work.ops.subscribe(caller, source_operation_id=framework_source["id"],
                             target_operation_id=target.assignment_id,
                             submission_id=str(uuid4()),
                             expected_revision=target_before.state_version)
    cancelled = await work.ops.cancel(caller, operation_id=framework_source["id"],
                                      submission_id=str(uuid4()),
                                      expected_revision=framework_source["revision"])
    assert cancelled["applied"] is True
    key = completion_event_key(framework_source["id"])
    woken = (await _read(work, target)).assignment
    assert woken.phase == "waiting" and woken.operation["control"]["wait"] is None
    assert woken.operation["control"]["watermarks"][key] >= 1
    fires = await _audits(work, work.owner, "work.completion_wake")
    assert len(fires) == 1 and fires[0].outputs_meta["outcome"] == "cancelled"


@pytest.fixture
def owner_caller(work, monkeypatch):
    claims = {"sub": work.owner, "realm_access": {"roles": ["user"]}}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    context = SimpleNamespace(owner_id=work.owner, claims=claims)
    authority = WorkCallerAuthority(context, None, SimpleNamespace(assignments=work.service),
                                    "fixture.access.token", time.monotonic() + 15, expiry)
    monkeypatch.setattr(WorkCallerAuthority, "_assert_local",
                        lambda caller, assignments: work.service._owner(
                            caller.context.owner_id, caller.context.claims))
    monkeypatch.setattr(WorkCallerAuthority, "assert_current",
                        lambda caller, tx, *, assignments: work.service._owner(
                            caller.context.owner_id, caller.context.claims))
    monkeypatch.setattr(WorkCallerAuthority, "verify_delivery", AsyncMock())
    return authority


@pytest.mark.asyncio
async def test_owner_stop_and_delete_fire_subscriptions(work, owner_caller):
    caller = _issue(work)
    source, target, deleted = _operation(work), _operation(work), _operation(work)
    for record in (target, deleted):
        await _subscribe(work, caller, source, record)
    controls = WorkControlService(work.service)
    stopped = await controls.control(
        work.owner, owner_caller.context.claims, source.assignment_id, "cancel",
        WorkControlRequest(
            expected_revision=(await _read(work, source)).assignment.state_version,
            submission_id=str(uuid4())),
        caller=owner_caller)
    assert stopped["applied"] is True
    key = completion_event_key(source.assignment_id)
    woken = (await _read(work, target)).assignment
    assert woken.phase == "waiting" and woken.operation["control"]["wait"] is None
    assert woken.operation["control"]["watermarks"][key] >= 1

    await WorkControlService(work.service).delete(
        work.owner, owner_caller.context.claims, source.assignment_id,
        WorkDeleteRequest(
            expected_revision=(await _read(work, source)).assignment.state_version),
        caller=owner_caller)
    gone = (await _read(work, deleted)).assignment
    assert gone.phase == "waiting" and gone.operation["control"]["wait"] is None


@pytest.mark.asyncio
async def test_subscription_wait_survives_a_store_restart(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    reopened = AssignmentStore(plane_runtime=work.runtime)
    try:
        read = await reopened.call("get_operation", owner_id=work.owner,
                                   assignment_id=target.assignment_id)
        key = completion_event_key(source.assignment_id)
        assert read.assignment.operation["control"]["wait"]["event_key"] == key
        service = WorkSubscriptionService(
            SimpleNamespace(store=reopened, orch=work.orch), work.credentials)
        assert service.store is reopened
        listed = await work.ops.subscriptions(caller, limit=50)
        assert len(listed["subscriptions"]) == 1
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_event_key_helpers_reject_malformed_ids():
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        completion_event_key("not-a-uuid")
    identifier = str(uuid4())
    assert completion_event_source(f"operation:{identifier}:completed") == identifier
    assert completion_event_source(f"owner:{identifier}") is None
    assert completion_event_source("operation:not-a-uuid:completed") is None
    assert completion_event_source(None) is None


@pytest.mark.asyncio
async def test_stale_caller_without_a_credential_row_is_refused(work):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    with work.runtime.transaction() as tx:
        tx.execute("DELETE FROM framework_credential WHERE id=%s", (caller.credential_id,))
    with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
        await _subscribe(work, caller, source, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_read", "after_observation"])
async def test_revoked_caller_cannot_read_completion_subscriptions(work, monkeypatch, when):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    def revoke():
        work.credentials.revoke(owner_id=work.owner, credential_id=caller.credential_id)

    if when == "before_read":
        revoke()
    else:
        original = work.credentials.fresh_observation

        def revoke_after_observation(value):
            observation = original(value)
            revoke()
            return observation

        monkeypatch.setattr(work.credentials, "fresh_observation", revoke_after_observation)
    with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
        await work.ops.subscriptions(caller)


@pytest.mark.asyncio
async def test_subscription_list_bounds_and_mutation_shape_guards(work, monkeypatch):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    for limit in (0, 101, "50", True):
        with pytest.raises(AssignmentError, match="work_control_invalid"):
            await work.ops.subscriptions(caller, limit=limit)
    repository = work.service.store.repository
    monkeypatch.setattr(repository, "set_owner_event_wait",
                        lambda tx, **kwargs: SimpleNamespace(applied=True, assignment=None))
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await _subscribe(work, caller, source, target)


@pytest.mark.asyncio
async def test_cancel_shape_guard_and_release_fallbacks(work, monkeypatch):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    armed = (await _read(work, target)).assignment
    repository = work.service.store.repository
    monkeypatch.setattr(repository, "accept_wake",
                        lambda tx, **kwargs: SimpleNamespace(applied=True, assignment=None))
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await work.ops.unsubscribe(caller, source_operation_id=source.assignment_id,
                                   target_operation_id=target.assignment_id,
                                   submission_id=str(uuid4()),
                                   expected_revision=armed.state_version)


@pytest.mark.asyncio
async def test_fire_guards_skip_invalid_requests(work):
    source_id = str(uuid4())
    assert fire_completions(work.service, None, None, owner_id=work.owner,
                            source_id=source_id, source_revision=1, outcome="weird") == 0
    assert fire_completions(work.service, None, None, owner_id=work.owner,
                            source_id="junk", source_revision=1, outcome="completed") == 0
    assert fire_completions(work.service, None, None, owner_id=work.owner,
                            source_id=source_id, source_revision="1",
                            outcome="completed") == 0


@pytest.mark.asyncio
async def test_fire_scan_failures_abort_the_completion_transaction(work, monkeypatch):
    repository = work.service.store.repository

    def refusing_list(tx, **kwargs):
        raise AssignmentError("work_repository_unavailable", 503)

    def broken_list(tx, **kwargs):
        raise RuntimeError("synthetic scan failure")

    def run_fire():
        return fire_completions(work.service, None, repository, owner_id=work.owner,
                                source_id=str(uuid4()), source_revision=1,
                                outcome="completed")

    monkeypatch.setattr(repository, "list_owner_event_waits", refusing_list)
    with pytest.raises(AssignmentError, match="work_repository_unavailable"):
        run_fire()
    monkeypatch.setattr(repository, "list_owner_event_waits", broken_list)
    with pytest.raises(RuntimeError, match="synthetic scan failure"):
        run_fire()


@pytest.mark.asyncio
async def test_release_fallbacks_skip_missing_or_released_waiters(work, monkeypatch):
    caller = _issue(work)
    source, target = _operation(work), _operation(work)
    await _subscribe(work, caller, source, target)
    repository = work.service.store.repository
    real_get = repository.get_operation

    def missing_get(tx, **kwargs):
        return None

    def released_get(tx, **kwargs):
        from dataclasses import replace as dataclass_replace

        read = real_get(tx, **kwargs)
        record = dataclass_replace(read.assignment,
                                   operation={**read.assignment.operation,
                                              "control": {"version": 1, "wait": None,
                                                          "watermarks": {},
                                                          "wake_receipts": {}}})
        return SimpleNamespace(assignment=record, disposition=read.disposition,
                               continuation_supported=read.continuation_supported)

    def fire(tx):
        return fire_completions(work.service, tx, repository, owner_id=work.owner,
                                source_id=source.assignment_id, source_revision=1,
                                outcome="completed")

    async def raw_phase():
        repository = work.runtime.repositories.assignments

        def read():
            with work.runtime.transaction() as tx:
                return type(repository).get_operation(
                    repository, tx, owner_id=work.owner, assignment_id=target.assignment_id)

        return (await asyncio.to_thread(read)).assignment.phase

    monkeypatch.setattr(repository, "get_operation", missing_get)
    assert await work.service.store.transaction(lambda tx, repo: fire(tx)) == 0
    assert await raw_phase() == "awaiting_event"

    monkeypatch.setattr(repository, "get_operation", released_get)
    assert await work.service.store.transaction(lambda tx, repo: fire(tx)) == 0
    assert await raw_phase() == "awaiting_event"
    _redrive_pending.clear()


@pytest.mark.asyncio
async def test_redrive_failure_paths_consume_attempts(work, monkeypatch):
    source, _target = _operation(work), _operation(work)
    schedule_redrive(work.owner, source.assignment_id)
    store = work.service.store

    async def refusing_transaction(*args, **kwargs):
        raise AssignmentError("work_repository_unavailable", 503)

    async def broken_transaction(*args, **kwargs):
        raise RuntimeError("synthetic redrive failure")

    monkeypatch.setattr(store, "transaction", refusing_transaction)
    assert await redrive_pending(work.service) == 0
    assert _redrive_pending[(work.owner, source.assignment_id)] == 1
    monkeypatch.setattr(store, "transaction", broken_transaction)
    assert await redrive_pending(work.service) == 0
    assert _redrive_pending[(work.owner, source.assignment_id)] == 2


@pytest.mark.asyncio
async def test_redrive_registry_bounds_and_type_guards(work):
    schedule_redrive(123, str(uuid4()))
    assert len(_redrive_pending) == 0
    source = str(uuid4())
    for index in range(256):
        _redrive_pending[(f"owner-{index}", str(uuid4()))] = 0
    schedule_redrive(work.owner, source)
    assert len(_redrive_pending) == 256
    assert (work.owner, source) in _redrive_pending
    _redrive_pending.clear()


@pytest.mark.asyncio
async def test_subscription_audit_metadata_is_bounded(work):
    from orchestrator.work_control_audit import WorkControlAudit

    record = (await _read(work, _operation(work))).assignment
    audit = WorkControlAudit(work.service)

    def run(append):
        def callback(tx, _repository):
            append(tx)
            return True

        return work.service.store.transaction(callback)

    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await run(lambda tx: audit.append(
            tx, owner_id=work.owner, command="wait", record=record,
            submission_id=str(uuid4()), action_id=str(uuid4())))
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await run(lambda tx: audit.append(
            tx, owner_id=work.owner, command="pause", record=record,
            submission_id=str(uuid4())))
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await run(lambda tx: audit.append(
            tx, owner_id=work.owner, command="subscribe", record=record,
            submission_id=str(uuid4()), action_id=str(uuid4()),
            source_id=str(uuid4()), target_id=str(uuid4())))
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await run(lambda tx: audit.append_completion_wake(
            tx, owner_id=work.owner, record=record, source_id="junk",
            revision=1, outcome="completed"))
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await run(lambda tx: audit.append_completion_wake(
            tx, owner_id=work.owner, record=record, source_id=str(uuid4()),
            revision=0, outcome="completed"))


@pytest.mark.asyncio
async def test_transient_wake_failure_rolls_back_completion_and_retry_after_restart(work, monkeypatch):
    caller = _issue(work)
    source = await work.ops.submit(caller, idempotency_key=str(uuid4()),
                                   name="Atomic completion", instructions="Read public data.")
    target = _operation(work)
    await _subscribe(work, caller, source["id"], target)
    repository = work.service.store.repository
    original_accept = repository.accept_wake

    def broken_wake(tx, **options):
        raise RuntimeError("synthetic transient wake failure")

    monkeypatch.setattr(repository, "accept_wake", broken_wake)
    with pytest.raises(AssignmentError, match="assignment_transaction_unavailable"):
        await work.ops.cancel(caller, operation_id=source["id"],
                              expected_revision=source["revision"], submission_id=str(uuid4()))
    unfinished = (await _read(work, source["id"])).assignment
    assert unfinished.lifecycle == "active"
    assert unfinished.operation.get("terminal_outcome") is None
    assert (await _read(work, target)).assignment.phase == "awaiting_event"
    assert await _audits(work, work.owner, "work.completion_wake") == []
    _redrive_pending.clear()
    monkeypatch.setattr(repository, "accept_wake", original_accept)
    reopened = AssignmentStore(plane_runtime=work.runtime)
    try:
        restarted = FrameworkWorkOperations(
            assignments=AssignmentService(work.orch, reopened, enabled=True,
                                          phi_gate=work.service.phi_gate),
            credentials=work.credentials)
        await restarted.cancel(caller, operation_id=source["id"],
                               expected_revision=source["revision"], submission_id=str(uuid4()))
    finally:
        reopened.close()
    assert (await _read(work, target)).assignment.phase == "waiting"
    assert len(await _audits(work, work.owner, "work.completion_wake")) == 1


@pytest.mark.asyncio
async def test_reciprocal_subscriptions_serialize_without_source_owner_lock_inversion(work, monkeypatch):
    import threading

    first_caller, second_caller = _issue(work), _issue(work)
    first, second = _operation(work), _operation(work)
    barrier = threading.Barrier(2)
    original_transaction = work.service.store.transaction

    async def synchronized_transaction(callback, **options):
        def start(tx, repository):
            assert barrier.wait(timeout=10) in (0, 1)
            return callback(tx, repository)
        return await original_transaction(start, **options)

    monkeypatch.setattr(work.service.store, "transaction", synchronized_transaction)
    async with asyncio.timeout(15):
        results = await asyncio.gather(
            _subscribe(work, first_caller, first, second),
            _subscribe(work, second_caller, second, first), return_exceptions=True)
    assert all(isinstance(result, dict) and result["applied"] is True for result in results)
    monkeypatch.setattr(work.service.store, "transaction", original_transaction)
    for source, target in ((first, second), (second, first)):
        wait = (await _read(work, target)).assignment.operation["control"]["wait"]
        assert wait["event_key"] == completion_event_key(source.assignment_id)
