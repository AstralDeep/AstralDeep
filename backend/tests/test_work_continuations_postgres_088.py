"""Private owner holds and conservative settlements with real IAM and Plane."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from audit.repository import AuditRepository
from astralplane.repositories.assignment_models import AssignmentActionOutcome
from orchestrator.work_control_authority import authenticate_work_control_request
from orchestrator.work_controls import WorkControlRequest, WorkControlService
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest, thaw
from tests.helpers.work_control_caller import current_control_caller
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_request_session_authority_088 import request, signing_key as signing_key
from tests.test_work_controls_postgres_088 import issued_fixture
from tests import test_work_service_postgres_088 as read_fixtures
from persistent_agents.tests.test_engine_postgres import plane as plane

pytestmark = pytest.mark.asyncio
read_records = read_fixtures.records


@pytest.fixture
async def value(read_records, monkeypatch, signing_key):
    runtime, reads, identities, legacy = read_records
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-work-continuations-audit-key")
    audit = AuditRepository(plane_runtime=runtime)
    reads.assignments.orch.audit_repo = audit
    async with current_control_caller(reads.assignments, monkeypatch, signing_key) as caller:
        yield SimpleNamespace(runtime=runtime, reads=reads, identity=identities[0],
            foreign=identities[2], legacy=legacy, caller=caller, audit=audit)


def module():
    return importlib.import_module("orchestrator.work_continuations")


async def current(value):
    return (await value.reads.store.call("get_operation", owner_id="owner",
                                       assignment_id=value.identity)).assignment


def wait_body(value, **changes):
    fields = {"submission_id": str(uuid4()), "expected_revision": 1,
              "owner_event_id": str(uuid4()), "owner_revision": 0}
    fields.update(changes)
    return module().WorkOwnerWaitRequest(**fields)


async def test_owner_wait_holds_without_refresh_or_borrowing_a_worker(value):
    before = await current(value)
    request = wait_body(value)
    service = module().WorkContinuationService(value.reads.assignments)
    result = await service.wait(value.identity, request, caller=value.caller)
    after = await current(value)
    assert result["applied"] is True and result["operation"]["phase"] == "awaiting_event"
    assert after.lifecycle == before.lifecycle == "active"
    assert after.control_epoch == before.control_epoch + 1
    assert after.operation["authority"] == before.operation["authority"]
    assert after.operation["control"]["wait"] == {
        "event_key": "owner:" + request.owner_event_id, "source_revision": 0}
    assert after.next_wake_at is None
    assert after.definition == before.definition and after.usage == before.usage
    replay = await service.wait(value.identity, request, caller=value.caller)
    assert replay == {**result, "applied": False} and await current(value) == after
    events, _ = value.audit.list_for_user("owner")
    assert len(events) == 1 and events[0].action_type == "assignment_wait"
    assert value.audit.verify_chain("owner") is None


async def safe_control(value, command):
    record = await current(value)
    return await WorkControlService(value.reads.assignments).control("owner", value.caller.context.claims,
        value.identity, command, WorkControlRequest(expected_revision=record.state_version,
            submission_id=str(uuid4())), caller=value.caller)


async def uncertain(value):
    """A genuine DB permit and unknown synthetic effect; no physical call occurs."""
    action, attempt, permit = await issued_fixture(value.reads, value.identity)
    outcome = AssignmentActionOutcome("uncertain", digest("synthetic-unknown-outcome"), {})
    await value.reads.store.call("record_action_outcome", owner_id="owner", assignment_id=value.identity,
        action_id=action.action_id, attempt_id=attempt, dispatch_token=permit.dispatch_token,
        expected_request_digest=action.intent.request_digest, outcome=outcome)
    return action, outcome


async def reconcile_body(value, outcome, **changes):
    fields = {"submission_id": str(uuid4()), "expected_revision": (await current(value)).state_version,
              "prior_result_digest": outcome.result_digest, "decision": "confirmed_applied"}
    fields.update(changes)
    return module().WorkReconcileRequest(**fields)


async def historical_envelope(value):
    """Model a restored v1 envelope; preserve the genuinely issued action ledger.

    This compatibility fixture does not claim that the permit was minted by an
    old release. Plane separately qualifies its retained historical receipts.
    """
    def restore(tx, _repository):
        data = thaw(tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s",
                                 (value.identity,))["data"])
        data["operation"]["version"] = 1
        data["operation"]["authority"].update(reference_kind="session",
                                              reference_id="synthetic-historical-session")
        tx.execute("UPDATE persistent_assignment SET data=%s::jsonb WHERE id=%s",
                   (json.dumps(data), value.identity))
    await value.reads.store.transaction(restore)


@pytest.mark.parametrize("decision", ["confirmed_applied", "confirmed_not_applied"])
async def test_legacy_envelope_settles_real_liability_once_but_never_continues(value, decision):
    action, outcome = await uncertain(value)
    await historical_envelope(value)
    before = await current(value)
    body = await reconcile_body(value, outcome, decision=decision)
    service = module().WorkContinuationService(value.reads.assignments)
    reply = await service.reconcile(value.identity, action.action_id, body, caller=value.caller)
    after = await current(value)
    assert reply["applied"] is True and reply["operation"]["schema_supported"] is False
    assert after.operation == before.operation and after.lifecycle == before.lifecycle
    assert after.wake_generation == before.wake_generation and after.next_wake_at is None
    assert after.usage["spent"]["tool_calls"] == action.intent.maximum.tool_calls
    assert after.usage["outstanding"]["tool_calls"] == 0
    settled = await value.reads.store.call("get_action", owner_id="owner", assignment_id=value.identity,
                                         action_id=action.action_id)
    assert settled.result["result_available"] is False and settled.result["result"] == {}
    replay = await service.reconcile(value.identity, action.action_id,
        body.model_copy(update={"expected_revision": after.state_version + 100}), caller=value.caller)
    assert replay["applied"] is False and await current(value) == after
    with pytest.raises(AssignmentError):
        await service.wait(value.identity, wait_body(value, expected_revision=after.state_version),
                           caller=value.caller)
    assert await current(value) == after and len(value.audit.list_for_user("owner")[0]) == 1
    assert value.audit.verify_chain("owner") is None


@pytest.mark.parametrize("paused", [False, True])
async def test_owner_wait_preserves_started_liability_and_paused_lifecycle(value, paused):
    action, _, _ = await issued_fixture(value.reads, value.identity)
    if paused:
        await safe_control(value, "pause")
    before = await current(value)
    reply = await module().WorkContinuationService(value.reads.assignments).wait(value.identity,
        wait_body(value, expected_revision=before.state_version), caller=value.caller)
    after = await current(value)
    retained = await value.reads.store.call("get_action", owner_id="owner", assignment_id=value.identity,
                                          action_id=action.action_id)
    assert reply["applied"] is True and after.lifecycle == before.lifecycle
    assert after.next_wake_at is None and after.usage == before.usage
    assert retained.ever_started and retained.state in {"started", "uncertain"}
    assert after.operation["authority"] == before.operation["authority"]


async def test_wait_signature_does_not_adopt_new_revision_or_event(value):
    body = wait_body(value)
    service = module().WorkContinuationService(value.reads.assignments)
    await service.wait(value.identity, body, caller=value.caller)
    before = await current(value)
    for fields in ({"expected_revision": before.state_version}, {"owner_revision": 1},
                   {"owner_event_id": str(uuid4())}):
        with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
            await service.wait(value.identity, body.model_copy(update=fields), caller=value.caller)
    with pytest.raises(AssignmentError, match="assignment_revision_conflict"):
        await service.wait(value.identity, wait_body(value), caller=value.caller)
    assert await current(value) == before
    assert len(value.audit.list_for_user("owner")[0]) == 1


@pytest.mark.parametrize("decision", ["confirmed_applied", "confirmed_not_applied"])
@pytest.mark.parametrize("paused", [False, True])
async def test_reconcile_charges_maximum_once_without_output_or_continuation(value, decision, paused):
    action, outcome = await uncertain(value)
    if paused:
        await safe_control(value, "pause")
    before = await current(value)
    body = await reconcile_body(value, outcome, decision=decision)
    service = module().WorkContinuationService(value.reads.assignments)
    result = await service.reconcile(value.identity, action.action_id, body, caller=value.caller)
    after = await current(value)
    assert result["applied"] is True and set(result) == {"operation", "action_id", "applied"}
    assert after.lifecycle == before.lifecycle and after.next_wake_at is None
    assert after.usage["outstanding"]["tool_calls"] == 0
    assert after.usage["spent"]["tool_calls"] == action.intent.maximum.tool_calls
    assert after.usage["spent"]["elapsed_ms"] == action.intent.maximum.elapsed_ms
    assert after.wake_generation == before.wake_generation
    settled = await value.reads.store.call("get_action", owner_id="owner", assignment_id=value.identity,
                                         action_id=action.action_id)
    assert settled.result["result_available"] is False and settled.result["result"] == {}
    assert settled.result["evidence_reference"] == "work-owner-decision:v1:" + body.submission_id
    assert "prior_result_digest" not in str(result) and "synthetic" not in str(result)
    events, _ = value.audit.list_for_user("owner")
    event = next(e for e in events if e.action_type == "assignment_reconcile")
    assert event.outputs_meta == {"assignment_id": value.identity,
        "instruction_revision": before.instruction_revision, "control_epoch": before.control_epoch,
        "submission_id": body.submission_id, "action_id": action.action_id, "decision": decision}
    # Expected revision is a transient observation; the same immutable decision
    # replays even after a later safe control changes control/state counters.
    if not paused:
        await safe_control(value, "pause")
    before_replay = await current(value)
    replay = await service.reconcile(value.identity, action.action_id,
        body.model_copy(update={"expected_revision": before_replay.state_version + 100}), caller=value.caller)
    assert replay["applied"] is False and await current(value) == before_replay
    events, _ = value.audit.list_for_user("owner")
    assert sum(e.action_type == "assignment_reconcile" for e in events) == 1
    assert value.audit.verify_chain("owner") is None


@pytest.mark.parametrize("change", ["decision", "prior_result_digest", "submission_id"])
async def test_reconcile_exact_decision_conflict_never_charges_twice(value, change):
    action, outcome = await uncertain(value)
    service = module().WorkContinuationService(value.reads.assignments)
    body = await reconcile_body(value, outcome)
    await service.reconcile(value.identity, action.action_id, body, caller=value.caller)
    before = await current(value)
    changed = {"decision": "confirmed_not_applied", "prior_result_digest": "0" * 64,
               "submission_id": str(uuid4())}[change]
    with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
        await service.reconcile(value.identity, action.action_id,
            body.model_copy(update={change: changed}), caller=value.caller)
    assert await current(value) == before and len(value.audit.list_for_user("owner")[0]) == 1


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
async def test_bare_bearer_safe_decision_does_not_require_original_session(value, kind):
    if kind == "reconcile":
        action, outcome = await uncertain(value)
        body = await reconcile_body(value, outcome)
    record = await current(value)
    with value.runtime.transaction() as tx:
        repo = value.runtime.repositories.history.sessions
        original = repo.get_by_incarnation(tx, owner_id="owner",
            incarnation_id=record.operation["authority"]["reference_id"])
        repo.delete(tx, owner_id="owner", session_id=original.session_id,
                    expected_incarnation_id=original.incarnation_id)
    req = request(headers=[(b"authorization", ("Bearer " + value.caller._token).encode()),
                           (b"content-type", b"application/json")])
    req.scope["app"] = value.caller._binding.app
    caller = await authenticate_work_control_request(req, assignments=value.reads.assignments,
                                                      sessions=value.caller._binding.sessions)
    assert caller.caller is None
    service = module().WorkContinuationService(value.reads.assignments)
    if kind == "wait":
        result = await service.wait(value.identity, wait_body(value), caller=caller)
    else:
        result = await service.reconcile(value.identity, action.action_id, body, caller=caller)
    assert result["applied"] is True and (await current(value)).next_wake_at is None


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
@pytest.mark.parametrize("failure", ["audit_before", "audit_after", "final", "replacement", "expired"])
async def test_failed_decision_rolls_back_audit_state_and_receipt(value, monkeypatch, kind, failure):
    service = module().WorkContinuationService(value.reads.assignments)
    if kind == "reconcile":
        action, outcome = await uncertain(value)
        body = await reconcile_body(value, outcome)
    else:
        body = wait_body(value)
    before = await current(value)
    original = value.audit.insert_in_transaction
    audit_calls = []
    if failure == "expired":
        value.caller = replace(value.caller, _until=datetime.now(timezone.utc) + timedelta(seconds=.08))

    def audit(*args, **kwargs):
        audit_calls.append(True)
        if failure == "audit_before":
            raise RuntimeError("synthetic private failure")
        result = original(*args, **kwargs)
        if failure == "audit_after":
            raise RuntimeError("synthetic private failure")
        if failure == "replacement":
            value.reads.assignments.orch.audit_repo = object()
        if failure == "expired":
            # Cross only the narrowed private attempt lifetime after real audit.
            # Do not patch process clocks or alter production/SQL timeouts.
            time.sleep(.10)
        return result

    monkeypatch.setattr(value.audit, "insert_in_transaction", audit)
    if failure == "final":
        name = "set_owner_event_wait" if kind == "wait" else "reconcile_action"
        original_final = getattr(value.runtime.repositories.assignments, name)

        def after_final(*args, **kwargs):
            original_final(*args, **kwargs)
            raise ValueError("synthetic final failure after real mutation")

        monkeypatch.setattr(value.runtime.repositories.assignments, name, after_final)
    with pytest.raises(AssignmentError):
        if kind == "wait":
            await service.wait(value.identity, body, caller=value.caller)
        else:
            await service.reconcile(value.identity, action.action_id, body, caller=value.caller)
    assert audit_calls and await current(value) == before and value.audit.list_for_user("owner")[0] == []


async def bare_caller(value):
    req = request(headers=[(b"authorization", ("Bearer " + value.caller._token).encode()),
                           (b"content-type", b"application/json")])
    req.scope["app"] = value.caller._binding.app
    return await authenticate_work_control_request(req, assignments=value.reads.assignments,
                                                    sessions=value.caller._binding.sessions)


async def test_real_wait_race_rechecks_receipt_during_locked_preparation(value, monkeypatch):
    service = module().WorkContinuationService(value.reads.assignments)
    body = wait_body(value)
    left, right = await bare_caller(value), await bare_caller(value)
    barrier = threading.Barrier(2)
    repository = value.runtime.repositories.assignments
    original = repository.get_submission_receipt

    def miss(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result is None
        barrier.wait(timeout=3)
        return result

    monkeypatch.setattr(repository, "get_submission_receipt", miss)
    result = await asyncio.gather(service.wait(value.identity, body, caller=left),
                                  service.wait(value.identity, body, caller=right))
    assert sorted(item["applied"] for item in result) == [False, True]
    assert len(value.audit.list_for_user("owner")[0]) == 1


async def test_concurrent_exact_reconciliation_settles_once(value):
    action, outcome = await uncertain(value)
    body = await reconcile_body(value, outcome)
    service = module().WorkContinuationService(value.reads.assignments)
    result = await asyncio.gather(*(service.reconcile(value.identity, action.action_id,
        body, caller=value.caller) for _ in range(2)))
    assert sorted(item["applied"] for item in result) == [False, True]
    after = await current(value)
    assert after.usage["spent"]["tool_calls"] == 1
    assert len(value.audit.list_for_user("owner")[0]) == 1


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
async def test_lost_commit_ack_has_only_exact_replay_no_second_audit(value, monkeypatch, kind):
    service = module().WorkContinuationService(value.reads.assignments)
    if kind == "wait":
        body = wait_body(value)
        args = (value.identity, body)
    else:
        action, outcome = await uncertain(value)
        body = await reconcile_body(value, outcome)
        args = (value.identity, action.action_id, body)
    original = value.reads.store.transaction

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise AssignmentError("assignment_transaction_unavailable", 503)

    with monkeypatch.context() as patch:
        patch.setattr(value.reads.store, "transaction", lost_ack)
        with pytest.raises(AssignmentError):
            await getattr(service, kind)(*args, caller=value.caller)
    after = await current(value)
    assert len(value.audit.list_for_user("owner")[0]) == 1
    result = await getattr(service, kind)(*args, caller=value.caller)
    assert result["applied"] is False and await current(value) == after
    assert len(value.audit.list_for_user("owner")[0]) == 1


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
async def test_replaced_caller_refuses_before_any_owner_decision(value, kind):
    if kind == "reconcile":
        action, outcome = await uncertain(value)
        args = (value.identity, action.action_id, await reconcile_body(value, outcome))
    else:
        args = (value.identity, wait_body(value))
    before = await current(value)
    session_id = value.caller.caller.credential.session_id
    replace_session_record(value.runtime, get_session_record(value.runtime, session_id))
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await getattr(module().WorkContinuationService(value.reads.assignments), kind)(
            *args, caller=value.caller)
    assert await current(value) == before and value.audit.list_for_user("owner")[0] == []


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
async def test_foreign_legacy_absent_and_bad_identifiers_remain_private_404(value, kind):
    service = module().WorkContinuationService(value.reads.assignments)
    if kind == "reconcile":
        action, outcome = await uncertain(value)
        body = await reconcile_body(value, outcome)
    before = await current(value)
    for identity in (value.foreign, value.legacy, str(uuid4()), "invalid"):
        with pytest.raises(AssignmentError) as failure:
            if kind == "wait":
                await service.wait(identity, wait_body(value), caller=value.caller)
            else:
                await service.reconcile(identity, action.action_id, body, caller=value.caller)
        assert (failure.value.code, failure.value.status_code) == ("work_not_found", 404)
    assert await current(value) == before and value.audit.list_for_user("owner")[0] == []


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
async def test_malformed_repository_preparation_and_final_result_roll_back(value, monkeypatch, kind):
    if kind == "reconcile":
        action, outcome = await uncertain(value)
        args = (value.identity, action.action_id, await reconcile_body(value, outcome))
        methods = ("prepare_action_reconciliation", "reconcile_action")
    else:
        args = (value.identity, wait_body(value))
        methods = ("prepare_owner_event_wait", "set_owner_event_wait")
    before = await current(value)
    service = module().WorkContinuationService(value.reads.assignments)
    repository = value.runtime.repositories.assignments
    for name in methods:
        original = getattr(repository, name)

        def malformed(*args, **kwargs):
            original(*args, **kwargs)
            return None

        with monkeypatch.context() as patch:
            patch.setattr(repository, name, malformed)
            with pytest.raises(AssignmentError, match="work_control_unavailable"):
                await getattr(service, kind)(*args, caller=value.caller)
        assert await current(value) == before and value.audit.list_for_user("owner")[0] == []


@pytest.mark.parametrize("kind", ["wait", "reconcile"])
async def test_no_caller_or_invalid_private_model_never_reaches_decision(value, kind):
    service = module().WorkContinuationService(value.reads.assignments)
    if kind == "reconcile":
        action, outcome = await uncertain(value)
        body = await reconcile_body(value, outcome)
        prefix = (value.identity, action.action_id)
    else:
        body = wait_body(value)
        prefix = (value.identity,)
    before = await current(value)
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        await getattr(service, kind)(*prefix, body, caller=None)
    for malformed in (body.model_dump(), body.model_copy(update={"expected_revision": True})):
        with pytest.raises(AssignmentError, match="work_control_invalid"):
            await getattr(service, kind)(*prefix, malformed, caller=value.caller)
    assert await current(value) == before and value.audit.list_for_user("owner")[0] == []


@pytest.mark.parametrize("command,changes", [
    ("wait", {"submission_id": None}), ("wait", {"action_id": str(uuid4())}),
    ("wait", {"decision": "confirmed_applied"}),
    ("reconcile", {"submission_id": "private"}),
    ("reconcile", {"action_id": None}), ("reconcile", {"decision": "refunded"}),
    ("reconcile", {"decision": {"private": "body"}}),
    ("pause", {"submission_id": str(uuid4())}),
])
async def test_audit_new_fields_are_closed_identifiers_only(value, command, changes):
    fields = {"submission_id": str(uuid4())}
    if command == "reconcile":
        fields.update(action_id=str(uuid4()), decision="confirmed_applied")
    fields.update(changes)
    record = await current(value)
    service = module().WorkContinuationService(value.reads.assignments)
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await value.reads.store.transaction(lambda tx, _: service.audit.append(tx,
            owner_id="owner", command=command, record=record, **fields), bound_session_waits=True)
    assert value.audit.list_for_user("owner")[0] == []
