"""Manual-owner wake through real IAM, Plane receipts, config and atomic audit.

Only external JWKS/refresh replies are synthetic. The supervised fixture never
dispatches, and no wake test sends a provider/tool request or activates a route.
"""

import asyncio
import importlib
import threading
import time
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from astralplane.repositories.assignment_models import (
    AssignmentActionIntent, AssignmentOperationBinding, AssignmentResourceAmount,
)
from astralplane.repositories.history import SessionExecutionObservation
from astralplane.repositories.work_admission import (
    AdmissionClass, AdmissionClassConfig, OperationOwner, OperationRequest, OwnerScope,
    WorkAdmissionRepository,
)

from orchestrator.work_submit import FixedResearchPreflight
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest, thaw
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
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


def module():
    return importlib.import_module("orchestrator.work_wake")


def service_for(api):
    return module().WorkWakeService(api.service.assignments)


async def waiting(api, *, issued=False):
    accepted = await api.service.submit(await context(api.fixture, api.runtime),
                                        research_command(api.service))
    record = accepted.record
    if issued:
        await issue(api, record)
        record = current(api.runtime, record)
    owner_event_id = str(uuid4())
    await api.service.store.call("set_owner_event_wait", owner_id=record.owner_id,
        assignment_id=record.assignment_id, expected_instruction_revision=record.instruction_revision,
        expected_control_epoch=record.control_epoch, expected_state_version=record.state_version,
        submission_id=str(uuid4()), submission_digest=digest("fixture-owner-wait"),
        event_key="owner:" + owner_event_id, source_revision=0)
    record = current(api.runtime, record)
    return record, module().WorkOwnerWakeRequest(expected_revision=record.state_version,
        submission_id=str(uuid4()), owner_event_id=owner_event_id, owner_revision=1)


def wake_audits(api):
    return [row for row in api.service.audit.list_for_user(api.fixture[1])[0]
            if row.action_type == "assignment_wake"]


def unchanged(api, record):
    assert current(api.runtime, record) == record
    assert wake_audits(api) == []


@pytest.mark.parametrize("same_session", [True, False])
async def test_wake_uses_original_a_and_captured_b_without_dispatch(api, same_session):
    record, body = await waiting(api)
    sid = api.fixture[2] if same_session else uuid4().hex
    if not same_session:
        api.fixture[0].create(sid, user_id=record.owner_id, access_token=api.fixture[3](),
            refresh_token="synthetic-independent-caller", hard_max_seconds=3600)
    try:
        selected = await caller(api, sid)
        before_b = selected.require_session().credential
        before_refresh = len(api.fixture[-1])
        reply = await service_for(api).wake(record.assignment_id, body, caller=selected)
        after = current(api.runtime, record)
        assert reply["applied"] is True and reply["operation"]["phase"] == "waiting"
        assert after.wake_generation == record.wake_generation + 1
        assert after.control_epoch == record.control_epoch
        assert after.operation["authority"] == record.operation["authority"]
        assert after.definition == record.definition and after.usage == record.usage
        assert after.checkpoint == record.checkpoint
        with api.runtime.transaction() as tx:
            assert tx.fetch_one("SELECT lease_expires_at FROM persistent_assignment WHERE id=%s",
                                (record.assignment_id,))["lease_expires_at"] is None
        assert after.operation["control"]["wait"] is None
        assert after.operation["control"]["watermarks"]["owner:" + body.owner_event_id] == 1
        assert len(api.fixture[-1]) == before_refresh + 1
        fresh_b = api.fixture[0].capture_execution_reference(
            owner_id=record.owner_id, session_id=sid).state.credential
        assert (fresh_b != before_b) is same_session
        assert await api.service.store.call("list_actions", owner_id=record.owner_id,
                                            assignment_id=record.assignment_id) == ()
        assert len(wake_audits(api)) == 1
        assert api.service.audit.verify_chain(record.owner_id) is None
    finally:
        if not same_session:
            api.fixture[0].delete(sid)


async def test_replay_after_control_bypasses_every_new_continuation_check(api, monkeypatch):
    record, body = await waiting(api)
    wake = service_for(api)
    await wake.wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    cancelled = control(api.runtime, current(api.runtime, record), "stop")
    api.fixture[0].delete(api.fixture[2])
    api.orch.persistent_assignment_runner = None
    api.orch._llm_store = None
    before_refresh = len(api.fixture[-1])

    def forbidden(*args, **kwargs):
        raise AssertionError("accepted wake attempted new continuation")

    monkeypatch.setattr(module(), "refresh_operation_control_authority", forbidden)
    monkeypatch.setattr(api.runtime.repositories.assignments, "assert_operation_continuation_clear", forbidden)
    reply = await wake.wake(record.assignment_id,
        body.model_copy(update={"expected_revision": cancelled.state_version + 100}),
        caller=await caller(api, None))
    assert reply["applied"] is False and reply["operation"]["disposition"] == "cancelled"
    assert current(api.runtime, record) == cancelled
    assert len(wake_audits(api)) == 1 and len(api.fixture[-1]) == before_refresh


@pytest.mark.parametrize("change", ["bare_bearer", "runner", "revision", "event_key", "watermark", "scope"])
async def test_new_wake_refuses_before_original_refresh(api, change):
    record, body = await waiting(api)
    selected = await caller(api, None if change == "bare_bearer" else api.fixture[2])
    if change == "runner":
        api.runner._stopping = True
    elif change == "revision":
        body = body.model_copy(update={"expected_revision": record.state_version + 1})
    elif change == "event_key":
        body = body.model_copy(update={"owner_event_id": str(uuid4())})
    elif change == "watermark":
        body = body.model_copy(update={"owner_revision": 0})
    elif change == "scope":
        api.orch.tool_permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:search"})
        api.orch.tool_permissions.set_agent_scopes(record.owner_id, "web-research-1", {"tools:search": True})
    before_refresh = len(api.fixture[-1])
    with pytest.raises(AssignmentError):
        await service_for(api).wake(record.assignment_id, body, caller=selected)
    unchanged(api, record)
    assert len(api.fixture[-1]) == before_refresh


@pytest.mark.parametrize("field,value", [("expected_revision", True), ("submission_id", "invalid"),
    ("owner_event_id", "provider:key"), ("owner_revision", True), ("owner_revision", 2**53)])
async def test_private_model_mutations_are_revalidated_before_await(api, field, value):
    record, body = await waiting(api)
    selected = await caller(api, api.fixture[2])
    before_refresh = len(api.fixture[-1])
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service_for(api).wake(record.assignment_id, body.model_copy(update={field: value}), caller=selected)
    unchanged(api, record)
    assert len(api.fixture[-1]) == before_refresh


@pytest.mark.parametrize("kind", ["caller", "body", "identity"])
async def test_private_argument_types_and_absent_identity_are_closed(api, kind):
    record, body = await waiting(api)
    selected = await caller(api, api.fixture[2])
    with pytest.raises(AssignmentError) as failure:
        await service_for(api).wake(str(uuid4()) if kind == "identity" else record.assignment_id,
            body.model_dump() if kind == "body" else body, caller=None if kind == "caller" else selected)
    assert failure.value.status_code == {"caller": 401, "body": 422, "identity": 404}[kind]
    unchanged(api, record)


async def test_body_snapshot_and_immutable_event_digest_survive_await(api, monkeypatch):
    record, body = await waiting(api)
    original = body.model_dump()
    before_prepare = FixedResearchPreflight.prepare

    async def prepare(self, **kwargs):
        result = await before_prepare(self, **kwargs)
        body.submission_id = str(uuid4())
        body.owner_event_id = str(uuid4())
        body.owner_revision = 100
        return result

    monkeypatch.setattr(FixedResearchPreflight, "prepare", prepare)
    await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    after = current(api.runtime, record)
    expected = digest({"api_version": 1, "operation_id": record.assignment_id, "command": "wake",
                       **{key: value for key, value in original.items() if key != "expected_revision"}})
    assert after.operation["control"]["wake_receipts"] == {
        original["submission_id"]: digest(["owner:" + original["owner_event_id"], 1, expected])}
    assert after.operation["control"]["watermarks"] == {"owner:" + original["owner_event_id"]: 1}


@pytest.mark.parametrize("change", ["control", "config", "key", "caller"])
async def test_changed_original_selection_or_prerequisite_rolls_back(api, monkeypatch, change):
    record, body = await waiting(api)
    before_prepare = FixedResearchPreflight.prepare
    expected = [record]

    async def prepare(self, **kwargs):
        result = await before_prepare(self, **kwargs)
        if change == "control":
            expected[0] = control(api.runtime, record)
        elif change == "config":
            with api.runtime.transaction() as tx:
                api.runtime.repositories.encrypted_llm_config.delete_user(tx, owner_id=record.owner_id)
        elif change == "key":
            monkeypatch.setenv("AUDIT_HMAC_SECRET_PREFLIGHT_TEST", "synthetic-retired-key-" + "z" * 40)
        else:
            replace_session_record(api.runtime, get_session_record(api.runtime, api.fixture[2]))
        return result

    monkeypatch.setattr(FixedResearchPreflight, "prepare", prepare)
    with pytest.raises(AssignmentError):
        await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    unchanged(api, expected[0])


async def test_missing_original_session_cannot_be_replaced_by_independent_caller(api):
    record, body = await waiting(api)
    sid = uuid4().hex
    api.fixture[0].create(sid, user_id=record.owner_id, access_token=api.fixture[3](),
        refresh_token="synthetic-other-caller", hard_max_seconds=3600)
    try:
        selected = await caller(api, sid)
        api.fixture[0].delete(api.fixture[2])
        with pytest.raises(AssignmentError, match="work_authority_unavailable"):
            await service_for(api).wake(record.assignment_id, body, caller=selected)
        unchanged(api, record)
    finally:
        api.fixture[0].delete(sid)


async def test_final_receipt_recheck_precedes_stale_selected_record_cas(api, monkeypatch):
    record, body = await waiting(api)
    before_prepare = FixedResearchPreflight.prepare
    external = []

    async def prepare(self, **kwargs):
        result = await before_prepare(self, **kwargs)
        # Another already-authorized owner event commit wins during the await.
        # This public-repository fixture proves receipt ordering, not host auth.
        signature = digest({"api_version": 1, "operation_id": record.assignment_id, "command": "wake",
                            **body.model_dump(exclude={"expected_revision"})})
        with api.runtime.transaction() as tx:
            accepted = api.runtime.repositories.assignments.accept_wake(tx,
                owner_id=record.owner_id, assignment_id=record.assignment_id,
                expected_instruction_revision=record.instruction_revision,
                expected_control_epoch=record.control_epoch, expected_state_version=record.state_version,
                event_id=body.submission_id, event_key="owner:" + body.owner_event_id,
                source_revision=body.owner_revision, event_digest=signature)
            service_for(api).audit.append(tx, owner_id=record.owner_id, command="wake", record=accepted.assignment)
        external.append(current(api.runtime, record))
        return result

    monkeypatch.setattr(FixedResearchPreflight, "prepare", prepare)
    reply = await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    assert reply["applied"] is False and current(api.runtime, record) == external[0]
    assert len(wake_audits(api)) == 1


@pytest.mark.parametrize("change", ["key", "runner", "failure"])
async def test_audit_and_post_policy_failures_roll_back_wake_receipt(api, monkeypatch, change):
    record, body = await waiting(api)
    if change == "failure":
        def fail(*args, **kwargs):
            raise RuntimeError("synthetic private audit failure")
        monkeypatch.setattr(api.service.audit, "insert_in_transaction", fail)
    else:
        before_policy = FixedResearchPreflight.assert_policy
        def policy(*args, **kwargs):
            before_policy(*args, **kwargs)
            if change == "runner":
                api.runner._stopping = True
            else:
                monkeypatch.setenv("AUDIT_HMAC_SECRET_PREFLIGHT_TEST", "synthetic-new-key-" + "z" * 40)
        monkeypatch.setattr(FixedResearchPreflight, "assert_policy", staticmethod(policy))
    with pytest.raises(AssignmentError):
        await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    unchanged(api, record)


async def test_lost_commit_ack_preserves_receipt_and_never_reaudits(api, monkeypatch):
    record, body = await waiting(api)
    original = api.service.store.transaction
    lost = []
    async def transaction(callback, **kwargs):
        result = await original(callback, **kwargs)
        if isinstance(result, dict) and result.get("applied") is True and not lost:
            lost.append(True)
            raise AssignmentError("assignment_transaction_unavailable", 503)
        return result
    monkeypatch.setattr(api.service.store, "transaction", transaction)
    with pytest.raises(AssignmentError, match="assignment_transaction_unavailable"):
        await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    after = current(api.runtime, record)
    assert after.phase == "waiting" and lost == [True]
    result = await service_for(api).wake(record.assignment_id, body, caller=await caller(api, None))
    assert result["applied"] is False and current(api.runtime, record) == after
    assert len(wake_audits(api)) == 1


async def test_cancellation_before_acceptance_cannot_later_wake(api, monkeypatch):
    record, body = await waiting(api)
    entered = asyncio.Event()
    async def prepare(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(FixedResearchPreflight, "prepare", prepare)
    task = asyncio.create_task(service_for(api).wake(record.assignment_id, body,
        caller=await caller(api, api.fixture[2])))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unchanged(api, record)


async def test_repeated_cancellation_during_commit_preserves_one_replayable_wake(api, monkeypatch):
    record, body = await waiting(api)
    entered, release = threading.Event(), threading.Event()
    original = api.service.audit.insert_in_transaction
    appended = []
    def audit(*args, **kwargs):
        result = original(*args, **kwargs)
        appended.append(True)
        entered.set()
        assert release.wait(5), "test did not release its owned audit boundary"
        return result
    monkeypatch.setattr(api.service.audit, "insert_in_transaction", audit)
    task = asyncio.create_task(service_for(api).wake(record.assignment_id, body,
        caller=await caller(api, api.fixture[2])))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        # AsyncPlaneRuntime deliberately shields the one real transaction.
        # Cancellation makes its acknowledgment unknown; it must not spawn a
        # second mutation or erase the receipt when that worker commits.
        end = time.monotonic() + 5
        while api.service.store.async_runtime.snapshot().active and time.monotonic() < end:
            await asyncio.sleep(0.01)
        assert api.service.store.async_runtime.snapshot().active == 0
    assert appended == [True] and current(api.runtime, record).phase == "waiting"
    replay = await service_for(api).wake(record.assignment_id, body, caller=await caller(api, None))
    assert replay["applied"] is False and appended == [True] and len(wake_audits(api)) == 1


async def test_policy_revocation_committed_during_actual_config_wait_denies_wake(api, monkeypatch):
    record, body = await waiting(api)
    config = api.runtime.repositories.encrypted_llm_config
    original = config.get_user_for_update
    locked, requesting = threading.Event(), threading.Event()
    shared = {}
    def get_user(tx, *, owner_id):
        shared["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
        requesting.set()
        return original(tx, owner_id=owner_id)
    monkeypatch.setattr(config, "get_user_for_update", get_user)
    def writer():
        with api.runtime.transaction() as tx:
            original(tx, owner_id=record.owner_id)
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            locked.set()
            assert requesting.wait(5)
            end = time.monotonic() + 0.08
            while time.monotonic() < end:
                if tx.fetch_one("SELECT %s = ANY(pg_blocking_pids(%s)) AS waiting",
                                (blocker, shared["pid"]))["waiting"]:
                    shared["observed_wait"] = True
                    break
                time.sleep(0.001)
            assert shared.get("observed_wait"), "actual config lock wait not observed"
            api.runtime.repositories.tool_policy_state.set_scopes(tx, owner_id=record.owner_id,
                agent_id="web-research-1", scopes={"tools:read": False}, updated_at=int(time.time() * 1000))
    worker = asyncio.create_task(asyncio.to_thread(writer))
    try:
        assert await asyncio.to_thread(locked.wait, 5)
        with pytest.raises(AssignmentError, match="assignment_scope_revoked"):
            await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    finally:
        await worker
    assert shared["observed_wait"]
    unchanged(api, record)


async def issue(api, record):
    """Mint an actual permit without a physical effect, then retain its liability."""
    work = WorkAdmissionRepository()
    configs = (
        AdmissionClassConfig(AdmissionClass.GLOBAL, None, 10, 0, 0, "wake-fixture"),
        AdmissionClassConfig(AdmissionClass.BACKGROUND, AdmissionClass.GLOBAL, 10, 0, 0, "wake-fixture"),
    )
    await api.service.store.transaction(lambda tx, _: work.configure(tx, configs))
    work.bind_configs(configs)
    def start(tx, repository):
        state = api.runtime.repositories.history.sessions.get_execution_state(
            tx, owner_id=record.owner_id, session_id=api.fixture[2])
        observation = SessionExecutionObservation(state.credential, state.observed_at,
                                                  state.observed_at + timedelta(seconds=15))
        claim = repository.claim_operation_for_administration(tx, owner_id=record.owner_id,
            assignment_id=record.assignment_id, expected_state_version=record.state_version,
            worker_id="wake-fixture", authority=observation, lease_seconds=30)
        admitted = work.submit(tx, OperationRequest(operation_kind="assignment_episode",
            admission_class=AdmissionClass.BACKGROUND,
            owner=OperationOwner(OwnerScope.USER, record.owner_id, None), submission_id=uuid4(),
            idempotency_namespace=None, idempotency_key=None, normalized_input_digest=None,
            chat_id=None, parent_operation_id=None, connection_generation=None, request_generation=None),
            now=None, retention=timedelta(days=1), slot_lease=timedelta(minutes=1))
        selected = work.claim_operation(tx, AdmissionClass.BACKGROUND, admitted.operation_id,
            now=None, retention=timedelta(days=1), slot_lease=timedelta(minutes=1))
        binding = AssignmentOperationBinding(str(selected.fence.operation_id),
            selected.fence.execution_generation, str(selected.fence.execution_lease_token))
        repository.bind_operation(tx, fence=claim.fence, binding=binding)
        source = thaw(record.definition.source)
        request = {"kind": "tool", **{key: source[key] for key in ("agent_id", "tool_name", "arguments")}}
        action = repository.put_action_for_execution(tx, fence=claim.fence, binding=binding,
            authority=observation, intent=AssignmentActionIntent("wake-fixture-source", request,
                digest(request), AssignmentResourceAmount(tool_calls=1, elapsed_ms=100),
                digest("permission"), digest("precondition"), boundary="unreplayable"))
        attempt = str(uuid4())
        repository.reserve_action_for_execution(tx, fence=claim.fence, binding=binding, authority=observation,
            action_id=action.action_id, attempt_id=attempt, expected_request_digest=action.intent.request_digest,
            maximum=action.intent.maximum)
        repository.start_action_for_execution(tx, fence=claim.fence, binding=binding, authority=observation,
            action_id=action.action_id, attempt_id=attempt, expected_request_digest=action.intent.request_digest,
            current_permission_digest=action.intent.permission_digest,
            current_precondition_digest=action.intent.precondition_digest)
    await api.service.store.transaction(start)


@pytest.mark.parametrize("stale_phase", [False, True])
async def test_genuine_issued_liability_denies_wake_before_config_lock(api, monkeypatch, stale_phase):
    record, body = await waiting(api, issued=True)
    assert record.usage["outstanding"]["tool_calls"] == 1
    if stale_phase:
        # Restore a stale controller phase only. The actual issued permit,
        # outstanding usage and held event remain untouched. Wake must inspect
        # those facts instead of trusting an executable-looking phase label.
        with api.runtime.transaction() as tx:
            tx.execute("UPDATE persistent_assignment SET "
                       "data=jsonb_set(data,'{phase}','\"awaiting_event\"'::jsonb) WHERE id=%s",
                       (record.assignment_id,))
        record = current(api.runtime, record)
    def forbidden(*args, **kwargs):
        raise AssertionError("liability guard did not precede config lock")
    monkeypatch.setattr(api.runtime.repositories.encrypted_llm_config, "get_user_for_update", forbidden)
    with pytest.raises(AssignmentError, match=("assignment_action_uncertain" if stale_phase else "assignment_not_waiting")):
        await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    unchanged(api, record)
    actions = await api.service.store.call("list_actions", owner_id=record.owner_id,
                                           assignment_id=record.assignment_id)
    assert len(actions) == 1 and actions[0].ever_started


@pytest.mark.parametrize("stage", ["prepare_type", "prepare_owner", "prepare_replayed", "clear", "accept"])
async def test_malformed_repository_contract_is_closed_and_rolls_back(api, monkeypatch, stage):
    record, body = await waiting(api)
    repository = api.runtime.repositories.assignments
    method = "prepare_wake" if stage.startswith("prepare") else (
        "assert_operation_continuation_clear" if stage == "clear" else "accept_wake")
    original = getattr(repository, method)
    def malformed(*args, **kwargs):
        result = original(*args, **kwargs)
        if stage in {"prepare_type", "accept"}:
            return {}
        if stage == "prepare_replayed":
            return replace(result, replayed=1)
        if stage == "clear":
            return replace(result, owner_id="foreign-owner")
        return replace(result, assignment=replace(result.assignment, owner_id="foreign-owner"))
    monkeypatch.setattr(repository, method, malformed)
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    unchanged(api, record)


async def test_changed_consent_scope_during_policy_await_cannot_adopt_new_scope(api, monkeypatch):
    record, body = await waiting(api)
    async def policy(*args, **kwargs):
        return {"web-research-1:fetch_page": "tools:search"}
    monkeypatch.setattr(api.service.assignments, "_definition_policy", policy)
    with pytest.raises(AssignmentError, match="assignment_scope_changed"):
        await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    unchanged(api, record)


@pytest.mark.parametrize("change", ["owner_revision", "owner_event_id"])
async def test_changed_content_under_accepted_event_id_is_conflict(api, change):
    record, body = await waiting(api)
    await service_for(api).wake(record.assignment_id, body, caller=await caller(api, api.fixture[2]))
    after = current(api.runtime, record)
    body = body.model_copy(update={change: 2 if change == "owner_revision" else str(uuid4())})
    with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
        await service_for(api).wake(record.assignment_id, body, caller=await caller(api, None))
    assert current(api.runtime, record) == after and len(wake_audits(api)) == 1
