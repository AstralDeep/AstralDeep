"""T052 — operation measurements over the real Plane ledger and PostgreSQL.

The measurement read joins three durable facts: the assignment's own window, the
action ledger (one entry per LOGICAL task, one attempt per PHYSICAL claim) and
the activity sequence that splits the window. Nothing is estimated here, so an
operation whose claims were never measured must read as unmeasured rather than
as zero, and a row written before per-claim accounting must not be credited with
an attempt that was never recorded. Only the explicit future-version fixture
uses SQL, to reproduce storage written by a newer Plane.
"""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from astralplane.repositories.assignment_models import (
    AssignmentActionIntent,
    AssignmentActionOutcome,
    AssignmentActivityRecord,
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
from fastapi import FastAPI

from orchestrator.work_api import work_router
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest
from persistent_agents.tests.test_engine_postgres import plane as plane  # noqa: F401
from tests.test_work_service_postgres_088 import records as records  # noqa: F401

OWNER = {"sub": "owner"}


async def claimed(service, identity, *, owner="owner"):
    """Claim one operation through the real session/admission guards.

    The stored incarnation and database-clock observation are genuine; this
    fixture refreshes no external IAM and starts no host runner. It returns the
    live claim so a test can append activity and issue claims under that fence.
    """
    sessions = service.store.plane_runtime.repositories.history.sessions
    work = WorkAdmissionRepository()
    configs = (
        AdmissionClassConfig(AdmissionClass.GLOBAL, None, 10, 0, 0, "work-measure-fixture"),
        AdmissionClassConfig(AdmissionClass.BACKGROUND, AdmissionClass.GLOBAL,
                             10, 0, 0, "work-measure-fixture"),
    )
    await service.store.transaction(lambda tx, _: work.configure(tx, configs))
    work.bind_configs(configs)

    def claim(tx, repo):
        record = repo.get_operation(tx, owner_id=owner, assignment_id=identity).assignment
        incarnation = record.operation["authority"]["reference_id"]
        session = sessions.get_by_incarnation(tx, owner_id=owner, incarnation_id=incarnation)
        state = sessions.get_execution_state(tx, owner_id=owner, session_id=session.session_id)
        authority = SessionExecutionObservation(
            state.credential, state.observed_at, state.observed_at + timedelta(seconds=15))
        held = repo.claim_operation_for_administration(
            tx, owner_id=owner, assignment_id=identity,
            expected_state_version=record.state_version, worker_id="work-measure-fixture",
            lease_seconds=60, authority=authority)
        accepted = work.submit(tx, OperationRequest(
            operation_kind="assignment_episode", admission_class=AdmissionClass.BACKGROUND,
            owner=OperationOwner(OwnerScope.USER, owner, None), submission_id=uuid4(),
            idempotency_namespace=None, idempotency_key=None, normalized_input_digest=None,
            chat_id=None, parent_operation_id=None, connection_generation=None,
            request_generation=None), now=None, retention=timedelta(days=1),
            slot_lease=timedelta(minutes=5))
        selected = work.claim_operation(
            tx, AdmissionClass.BACKGROUND, accepted.operation_id, now=None,
            retention=timedelta(days=1), slot_lease=timedelta(minutes=5))
        binding = AssignmentOperationBinding(
            str(selected.fence.operation_id), selected.fence.execution_generation,
            str(selected.fence.execution_lease_token))
        repo.bind_operation(tx, fence=held.fence, binding=binding)
        return held.fence, binding, authority

    return await service.store.transaction(claim)


async def issue_task(service, identity, fence, binding, authority, *, key="measured-effect"):
    """Declare one logical task (an action) with no claim recorded yet."""
    request = {"kind": "tool", "agent_id": "fixture", "tool_name": key, "arguments": {}}

    def put(tx, repo):
        return repo.put_action_for_execution(
            tx, fence=fence, binding=binding, authority=authority,
            intent=AssignmentActionIntent(
                key, request, digest(request),
                AssignmentResourceAmount(tool_calls=1, elapsed_ms=100),
                digest("permission"), digest("precondition"), boundary="read_only"))

    return await service.store.transaction(put)


async def claim_task(service, identity, fence, binding, authority, action, *, outcome, elapsed):
    """Record one PHYSICAL claim; ``outcome`` None leaves it started/unmeasured."""
    attempt = str(uuid4())

    def start(tx, repo):
        repo.reserve_action_for_execution(
            tx, fence=fence, binding=binding, authority=authority, action_id=action.action_id,
            attempt_id=attempt, expected_request_digest=action.intent.request_digest,
            maximum=action.intent.maximum)
        return repo.start_action_for_execution(
            tx, fence=fence, authority=authority, action_id=action.action_id,
            attempt_id=attempt, expected_request_digest=action.intent.request_digest,
            current_permission_digest=digest("permission"),
            current_precondition_digest=digest("precondition"), binding=binding)

    permit = await service.store.transaction(start)
    if outcome is None:
        return attempt
    await service.store.call(
        "record_action_outcome", owner_id="owner", assignment_id=identity,
        action_id=action.action_id, attempt_id=attempt, dispatch_token=permit.dispatch_token,
        expected_request_digest=action.intent.request_digest,
        outcome=AssignmentActionOutcome(
            outcome, digest({}), {},
            actual=None if elapsed is None
            else AssignmentResourceAmount(tool_calls=1, elapsed_ms=elapsed)))
    return attempt


@pytest.mark.asyncio
async def test_one_logical_task_reports_every_physical_claim_separately(records):
    _, service, ids, _ = records
    identity = ids[0]
    fence, binding, authority = await claimed(service, identity)
    action = await issue_task(service, identity, fence, binding, authority)
    # A declared task with no attempt yet is one logical task and zero claims.
    declared = await service.measurements("owner", OWNER, identity)
    assert declared["task_count"] == 1 and declared["claim_count"] == 0
    assert declared["tasks"][0]["claims"] == 0 and declared["tasks"][0]["observed_ms"] is None
    await claim_task(service, identity, fence, binding, authority, action,
                     outcome="failed", elapsed=7)
    await claim_task(service, identity, fence, binding, authority, action,
                     outcome="succeeded", elapsed=11)
    measured = await service.measurements("owner", OWNER, identity)
    # One task, two claims: the retry is physical and never collapses the task.
    assert measured["task_count"] == 1 and measured["claim_count"] == 2
    assert measured["measured_claim_count"] == 2 and measured["observed_ms"] == 18
    assert measured["tasks"][0]["id"] == action.action_id
    assert measured["tasks"][0]["claims"] == 2 and measured["tasks"][0]["observed_ms"] == 18
    assert measured["tasks"][0]["incomplete"] is False and measured["incomplete"] is False
    # The window is still open, so the elapsed union is not yet a fact.
    assert measured["cutoff"] is True and measured["elapsed_ms"] is None
    assert measured["intervals"][-1]["open"] is True
    assert measured["intervals"][-1]["end_at"] is None


@pytest.mark.asyncio
async def test_an_unmeasured_claim_is_incomplete_and_never_counted_as_zero(records):
    _, service, ids, _ = records
    identity = ids[0]
    fence, binding, authority = await claimed(service, identity)
    action = await issue_task(service, identity, fence, binding, authority)
    await claim_task(service, identity, fence, binding, authority, action,
                     outcome=None, elapsed=None)
    started = await service.measurements("owner", OWNER, identity)
    assert started["claim_count"] == 1 and started["measured_claim_count"] == 0
    # An in-flight claim has no observation: absent, never a measured zero.
    assert started["observed_ms"] is None and started["tasks"][0]["observed_ms"] is None
    assert started["tasks"][0]["incomplete"] is True and started["incomplete"] is True


@pytest.mark.asyncio
async def test_operation_without_a_ledger_gets_no_synthesized_attempt(records):
    _, service, ids, _ = records
    result = await service.measurements("owner", OWNER, ids[0])
    assert result["task_count"] == 0 and result["claim_count"] == 0
    assert result["measured_claim_count"] == 0 and result["tasks"] == []
    # A queued operation that never recorded a claim is not credited with one.
    assert result["observed_ms"] is None and result["elapsed_ms"] is None
    assert result["disposition"] == "queued"
    assert result["incomplete"] is False and result["cutoff"] is True
    assert result["intervals"] == [{"sequence": None, "start_at": result["intervals"][0]["start_at"],
                                   "end_at": None, "duration_ms": None, "open": True}]


@pytest.mark.asyncio
async def test_activity_sequence_splits_the_window_into_measured_intervals(records):
    _, service, ids, _ = records
    identity = ids[0]
    fence, _binding, _authority = await claimed(service, identity)
    for index in (1, 2):
        await service.store.transaction(lambda tx, repo, index=index: repo.append_activity(
            tx, fence=fence, activity=AssignmentActivityRecord(
                f"measure:{index}", "attention", "Ongoing agent needs attention",
                "fixture", notification_state="none")))
    result = await service.measurements("owner", OWNER, identity)
    assert [item["sequence"] for item in result["intervals"]] == [1, 2, None]
    closed = result["intervals"][:2]
    assert all(item["open"] is False and item["end_at"] is not None for item in closed)
    assert all(item["duration_ms"] >= 0 for item in closed)
    # Each interval starts where the previous one ended; the last stays open.
    assert closed[0]["end_at"] == closed[1]["start_at"]
    assert result["intervals"][-1]["start_at"] == closed[1]["end_at"]
    assert result["intervals"][-1]["open"] is True and result["cutoff"] is True
    # Activity titles and summaries are not a measurement and are not disclosed.
    assert "attention" not in str(result) and "Ongoing agent" not in str(result)


@pytest.mark.asyncio
async def test_terminal_operation_closes_the_window_and_reports_elapsed(records):
    _, service, ids, _ = records
    identity = ids[0]
    current = await service.get("owner", OWNER, identity)
    await service.store.transaction(lambda tx, repo: repo.apply_control(
        tx, owner_id="owner", assignment_id=identity,
        expected_state_version=current["revision"],
        expected_instruction_revision=current["instruction_revision"],
        expected_control_epoch=current["control_epoch"], submission_id=str(uuid4()),
        submission_digest=digest("measure-stop"), control="stop"))
    result = await service.measurements("owner", OWNER, identity)
    assert result["disposition"] == "cancelled" and result["cutoff"] is False
    assert type(result["elapsed_ms"]) is int and result["elapsed_ms"] >= 0
    assert result["intervals"][-1]["open"] is False
    assert result["intervals"][-1]["end_at"] is not None


@pytest.mark.asyncio
async def test_future_operation_reports_no_measurement_instead_of_guessing(records):
    _, service, ids, _ = records
    def future_record(tx, repo):
        # Fixture-only forward-version simulation in the isolated schema.
        tx.execute(
            "UPDATE persistent_assignment SET data=jsonb_set(data, '{operation}', %s::jsonb) "
            "WHERE id=%s",
            ('{"version":3,"kind":{"private":"opaque"},"deadline_at":"private",'
             '"authority":"private"}', ids[0]))
    await service.store.transaction(future_record)
    result = await service.measurements("owner", OWNER, ids[0])
    assert result["task_count"] is None and result["claim_count"] is None
    assert result["observed_ms"] is None and result["elapsed_ms"] is None
    assert result["tasks"] == [] and result["intervals"] == []
    assert result["incomplete"] is True and result["cutoff"] is True
    assert "private" not in str(result)


@pytest.mark.asyncio
async def test_foreign_legacy_and_unknown_identities_share_one_refusal(records):
    _, service, ids, legacy = records
    for identity in (ids[2], legacy, str(uuid4()), "not-an-id"):
        with pytest.raises(AssignmentError) as failure:
            await service.measurements("owner", OWNER, identity)
        assert (failure.value.code, failure.value.status_code) == ("work_not_found", 404)
    assert (await service.measurements("other", {"sub": "other"}, ids[2]))["id"] == ids[2]


@pytest.mark.asyncio
async def test_http_measurements_route_is_owner_scoped_and_never_cached(records, monkeypatch):
    from tests.test_work_api_088 import override_read_auth
    _, service, ids, legacy = records
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(persistent_assignments=service.assignments)
    app.include_router(work_router, prefix="/api")
    override_read_auth(app, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.get(f"/api/work/v1/operations/{ids[0]}/measurements")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()["measurements"]
        assert body["id"] == ids[0] and body["claim_count"] == 0
        for identity in (ids[2], legacy, str(uuid4())):
            refused = await client.get(f"/api/work/v1/operations/{identity}/measurements")
            assert refused.status_code == 404
            assert refused.json() == {"error": "work_not_found"}
            assert refused.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_plane_charge_basis_and_money_disclosure_are_forwarded_verbatim(records):
    _, service, ids, _ = records
    def annotate(tx, repo):
        # Reproduce Plane's additive usage annotations without a schema change:
        # an explicit unknown price, a known currency and a per-dimension basis.
        tx.execute(
            "UPDATE persistent_assignment SET data=jsonb_set(data, '{usage}', "
            "data->'usage' || %s::jsonb) WHERE id=%s",
            ('{"money_status":"unknown","currency":"USD","spent":{"tokens":0,'
             '"spend_micro_units":0},"basis":{"tokens":"observed",'
             '"spend_micro_units":"uncertain","elapsed_ms":null}}', ids[0]))
    await service.store.transaction(annotate)
    usage = (await service.get("owner", OWNER, ids[0]))["usage"]
    assert usage["money_status"] == "unknown" and usage["currency"] == "USD"
    # A reported zero and an uncertain zero must not read alike.
    assert usage["spent"]["spend_micro_units"] == 0
    assert usage["basis"] == {"tokens": "observed", "spend_micro_units": "uncertain",
                              "elapsed_ms": None}


@pytest.mark.asyncio
async def test_absent_and_null_usage_disclosures_are_never_coerced(records):
    _, service, ids, _ = records
    baseline = (await service.get("owner", OWNER, ids[0]))["usage"]
    # Plane writes money_status today; currency and basis are absent, and an
    # absent key must stay absent rather than becoming a default value.
    assert baseline["money_status"] == "unknown"
    assert "currency" not in baseline and "basis" not in baseline

    def nulls(tx, repo):
        tx.execute(
            "UPDATE persistent_assignment SET data=jsonb_set(data, '{usage}', "
            "data->'usage' || %s::jsonb) WHERE id=%s",
            ('{"money_status":null,"currency":null,"basis":null}', ids[0]))
    await service.store.transaction(nulls)
    usage = (await service.get("owner", OWNER, ids[0]))["usage"]
    assert usage["money_status"] is None and usage["currency"] is None
    assert usage["basis"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("annotation", [
    '{"money_status":"free"}', '{"currency":""}', '{"currency":123}',
    '{"basis":{"tokens":"guessed"}}', '{"basis":"observed"}',
])
async def test_unknown_usage_disclosure_is_refused_rather_than_reinterpreted(records, annotation):
    _, service, ids, _ = records
    await service.store.transaction(lambda tx, repo: tx.execute(
        "UPDATE persistent_assignment SET data=jsonb_set(data, '{usage}', "
        "data->'usage' || %s::jsonb) WHERE id=%s", (annotation, ids[0])))
    with pytest.raises(AssignmentError) as failure:
        await service.get("owner", OWNER, ids[0])
    assert (failure.value.code, failure.value.status_code) == ("work_read_unavailable", 503)
