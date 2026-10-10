"""Exercise durable emergency stop transitions through current signed owner authority.
Real Plane transactions prove restart fencing, rollback, owner/session denials,
and admission serialization without replacing production persistence with fakes.
"""

from datetime import UTC, datetime

import pytest
import httpx
import asyncio
import threading

from orchestrator.emergency_stop import EmergencyStopCoordinator, EmergencyStopRefused
from orchestrator.emergency_stop_store import EmergencyStopStore
from orchestrator.web_auth import _sign
from orchestrator.human_request_authority import authenticate_current_human_request
from orchestrator.work_admission import (
    AdmissionClass, AdmissionClassConfig, PlaneWorkAdmissionRepository, WorkAdmissionCoordinator,
)
from persistent_agents.models import AssignmentError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_human_request_authority_088 import human as human
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, incoming,
)
from tests.test_emergency_stop_gates import _request, _user_owner
from tests.test_human_socket_authority_088 import socket_request as socket_request

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stop(human):
    store = EmergencyStopStore(human[2])
    coordinator = EmergencyStopCoordinator(audit=store.transition, rearm_loader=store.load, fresh_rearm=True)
    human[2].emergency_stop = coordinator
    human[2].emergency_stop_store = store
    return coordinator, store


async def caller(human, bound, fixture, **kwargs):
    return await authenticate_current_human_request(incoming(bound, fixture, **kwargs), boundary=human[1])


async def engage(stop, human, bound, fixture):
    owner = fixture[1]
    current = await caller(human, bound, fixture)
    result = await stop[0].engage(owner, caller=current, claims=current.claims, sweep=False)
    return result, current


async def test_real_owner_stop_resume_restart_and_monotonic_epoch(stop, human, bound, fixture):
    first, current = await engage(stop, human, bound, fixture)
    owner = fixture[1]
    assert (first["revision"], first["epoch"], first["engaged"]) == (1, 1, True)
    restarted = EmergencyStopCoordinator(audit=stop[1].transition, rearm_loader=stop[1].load, fresh_rearm=True)
    assert not restarted.admission_allowed(owner)
    resumed = await restarted.resume(owner, actor_id=owner, expected_revision=1, caller=current)
    assert (resumed["revision"], resumed["epoch"], resumed["engaged"]) == (2, 1, False)
    assert stop[0].admission_allowed(owner)
    second = await stop[0].engage(owner, caller=current, sweep=False)
    assert (second["revision"], second["epoch"]) == (3, 2)
    assert not restarted.admission_allowed(owner)
    assert restarted.admission_allowed("different-owner")


@pytest.mark.parametrize("failure", ["raise", "missing", "mismatch"])
async def test_audit_failure_rolls_back_ledger_and_keeps_local_fence(stop, human, bound, fixture, monkeypatch, failure):
    method = stop[1].audit.insert_in_transaction

    def broken(*args, **kwargs):
        result = method(*args, **kwargs)
        if failure == "raise":
            raise RuntimeError("synthetic audit failure")
        if failure == "missing":
            return None
        return result.model_copy(update={"event_id": "foreign-event"})

    monkeypatch.setattr(stop[1].audit, "insert_in_transaction", broken)
    current = await caller(human, bound, fixture)
    with pytest.raises(EmergencyStopRefused, match="emergency_stop_unavailable"):
        await stop[0].engage(fixture[1], caller=current, sweep=False)
    assert stop[1].load(fixture[1]) is None
    assert not stop[0].admission_allowed(fixture[1])
    with stop[1].runtime.transaction() as tx:
        assert tx.fetch_one("SELECT COUNT(*) AS n FROM audit_events WHERE actor_user_id = %s", (fixture[1],))["n"] == 0


async def test_failed_resume_cannot_clear_the_durable_stop(stop, human, bound, fixture, monkeypatch):
    first, current = await engage(stop, human, bound, fixture)
    monkeypatch.setattr(stop[1].audit, "insert_in_transaction", lambda *args, **kwargs: None)
    with pytest.raises(EmergencyStopRefused):
        await stop[0].resume(fixture[1], expected_revision=first["revision"], actor_id=fixture[1], caller=current)
    assert stop[1].load(fixture[1])["engaged"]
    assert not stop[0].admission_allowed(fixture[1])


async def test_stale_other_process_resume_cannot_clear_new_epoch(stop, human, bound, fixture):
    first, current = await engage(stop, human, bound, fixture)
    other = EmergencyStopCoordinator(audit=stop[1].transition, rearm_loader=stop[1].load, fresh_rearm=True)
    await stop[0].resume(fixture[1], expected_revision=1, actor_id=fixture[1], caller=current)
    second = await stop[0].engage(fixture[1], caller=current, sweep=False)
    with pytest.raises(EmergencyStopRefused, match="emergency_stop_stale_revision"):
        await other.resume(fixture[1], expected_revision=first["revision"], actor_id=fixture[1], caller=current)
    assert stop[1].load(fixture[1])["revision"] == second["revision"]
    assert not other.admission_allowed(fixture[1])


async def test_replaced_owner_session_never_commits_resume(stop, human, bound, fixture, runtime):
    first, current = await engage(stop, human, bound, fixture)
    replace_session_record(runtime, get_session_record(runtime, fixture[2]))
    with pytest.raises((AssignmentError, EmergencyStopRefused)):
        await stop[0].resume(fixture[1], expected_revision=first["revision"], actor_id=fixture[1], caller=current)
    assert stop[1].load(fixture[1])["engaged"]


async def test_wrong_owner_read_only_and_absent_caller_never_mutate(stop, human, bound, fixture):
    current = await caller(human, bound, fixture, method="GET")
    with pytest.raises(AssignmentError, match="human_write_required"):
        await stop[1].transition("emergency_stop.engage", "success", owner_id=fixture[1], caller=current)
    with pytest.raises(EmergencyStopRefused, match="owner_authentication_required"):
        await stop[1].transition("emergency_stop.engage", "success", owner_id=fixture[1], caller=None)
    write = await caller(human, bound, fixture)
    with pytest.raises(EmergencyStopRefused):
        await stop[1].transition("emergency_stop.engage", "success", owner_id="wrong-owner", caller=write)
    with pytest.raises(EmergencyStopRefused, match="invalid"):
        await stop[1].transition("emergency_stop.unknown", "success", owner_id=fixture[1], caller=write)
    with pytest.raises(EmergencyStopRefused, match="not_engaged"):
        await stop[1].transition("emergency_stop.resume", "success", owner_id=fixture[1],
                                caller=write, detail={"expected_revision": 1})
    assert stop[1].load(fixture[1]) is None


async def test_plane_admission_and_current_execution_fence_stop_in_same_transaction(stop, human, bound, fixture, runtime):
    configs = [AdmissionClassConfig(AdmissionClass.GLOBAL, None, 8, 0, None, "test-global"),
               AdmissionClassConfig(AdmissionClass.INTERACTIVE, AdmissionClass.GLOBAL, 2, 2, 60000, "test-interactive")]
    repository = PlaneWorkAdmissionRepository(plane_runtime=runtime, execution_gate=stop[1].assert_running)
    admission = WorkAdmissionCoordinator(admission_classes=configs, repository=repository, clock=lambda: datetime.now(UTC))
    request = _request(_user_owner(fixture[1]))
    accepted = admission.submit(request)
    assert accepted.accepted
    claimed = admission.claim_operation(AdmissionClass.INTERACTIVE, accepted.operation_id)
    assert claimed is not None
    await engage(stop, human, bound, fixture)
    refused = admission.submit(_request(_user_owner(fixture[1])))
    assert not refused.accepted and refused.code == "emergency_stop_active"
    with pytest.raises(EmergencyStopRefused):
        admission.assert_current_execution(claimed.fence)
    with pytest.raises(EmergencyStopRefused), admission.fenced_transaction(claimed.fence):
        raise AssertionError("stopped owner must never enter a publication transaction")
    assert admission.query_operation(owner=request.owner, operation_id=accepted.operation_id) is not None
    current = await caller(human, bound, fixture)
    await stop[0].resume(fixture[1], expected_revision=1, actor_id=fixture[1], caller=current)
    with pytest.raises(EmergencyStopRefused, match="stale_epoch"):
        admission.assert_current_execution(claimed.fence)
    replay = admission.submit(request)
    assert not replay.accepted and replay.code == "emergency_stop_stale_epoch"
    fresh = admission.submit(_request(_user_owner(fixture[1])))
    assert fresh.accepted
    fresh_claim = admission.claim_operation(AdmissionClass.INTERACTIVE, fresh.operation_id)
    assert fresh_claim is not None
    admission.assert_current_execution(fresh_claim.fence)


async def test_actual_audit_chain_preserves_current_owner_principal(stop, human, bound, fixture, runtime):
    await engage(stop, human, bound, fixture)
    with runtime.transaction() as tx:
        row = tx.fetch_one("SELECT actor_user_id, auth_principal, event_class, action_type, entry_hash "
                           "FROM audit_events WHERE actor_user_id = %s", (fixture[1],))
    assert row["actor_user_id"] == fixture[1] and row["auth_principal"] == fixture[1]
    assert row["event_class"] == "emergency_stop" and row["action_type"] == "emergency_stop.engage"
    assert row["entry_hash"]


async def test_real_owner_rest_stop_verify_resume_and_denial_while_engaged(stop, human, bound, fixture):
    from orchestrator.emergency_stop_api import emergency_stop_router

    bound[1].include_router(emergency_stop_router)
    headers = {"Authorization": "Bearer " + fixture[3](), "Origin": "https://app.invalid"}
    transport = httpx.ASGITransport(app=bound[1])
    async with httpx.AsyncClient(transport=transport, base_url="https://app.invalid", headers=headers,
                               cookies={"astral_session": _sign(fixture[2])}) as client:
        first = await client.post("/api/emergency-stop/stop", json={"reason": "owner drill"})
        assert first.status_code == 200 and first.json()["engaged"]
        assert first.headers["cache-control"] == "no-store"
        assert (await client.get("/api/emergency-stop")).json()["engaged"]
        verified = await client.post("/api/emergency-stop/verify", json={})
        assert verified.status_code == 200 and verified.json()["engaged"]
        stale = await client.post("/api/emergency-stop/resume", json={"expected_revision": 2})
        assert stale.status_code == 409 and stop[1].load(fixture[1])["engaged"]
        invalid = await client.post("/api/emergency-stop/resume", json={"expected_revision": True})
        assert invalid.status_code == 422
        resumed = await client.post("/api/emergency-stop/resume", json={"expected_revision": 1})
        assert resumed.status_code == 200 and not resumed.json()["engaged"]
        forbidden = await client.post("/api/emergency-stop/stop", json={}, headers={"Authorization": "Bearer invalid"})
        assert forbidden.status_code == 401 and not stop[1].load(fixture[1])["engaged"]


async def test_claim_next_retires_old_epoch_without_reviving_or_starving_new_work(stop, human, bound, fixture, runtime):
    configs = [AdmissionClassConfig(AdmissionClass.GLOBAL, None, 8, 0, None, "test-global"),
               AdmissionClassConfig(AdmissionClass.INTERACTIVE, AdmissionClass.GLOBAL, 2, 2, 60000, "test-interactive")]
    admission = WorkAdmissionCoordinator(admission_classes=configs,
        repository=PlaneWorkAdmissionRepository(plane_runtime=runtime, execution_gate=stop[1].assert_running),
        clock=lambda: datetime.now(UTC))
    old = admission.submit(_request(_user_owner(fixture[1])))
    assert old.accepted
    first, current = await engage(stop, human, bound, fixture)
    await stop[0].resume(fixture[1], expected_revision=first["revision"], actor_id=fixture[1], caller=current)
    fresh = admission.submit(_request(_user_owner(fixture[1])))
    assert fresh.accepted
    claim = admission.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None and claim.operation.operation_id == fresh.operation_id
    previous = admission.repository.get_operation_for_administration(old.operation_id)
    assert previous.cancel_requested_at is not None


async def test_registered_socket_controls_use_current_owner_and_reject_stale_connection(
    stop, human, socket_request, fixture,
):
    from orchestrator.human_request_authority import bind_human_caller, capture_human_socket_request
    from orchestrator.projection_surfaces import safety

    socket, context, message = socket_request

    async def control(action, payload):
        message.update(action=action, payload={"surface": "safety", **payload})
        pending = capture_human_socket_request(human[1], websocket=socket, context=context, message=message)
        try:
            current = await pending.authenticate()
            with bind_human_caller(current):
                result = await safety.HANDLERS[action](human[2], socket, fixture[1], ["user"], message["payload"])
            assert result[0] == "safety"
        finally:
            pending.close()

    await control("chrome_safety_stop", {"reason": "socket owner drill"})
    assert stop[1].load(fixture[1])["engaged"]
    await control("chrome_safety_verify", {})
    with pytest.raises(EmergencyStopRefused, match="stale_revision"):
        await control("chrome_safety_resume", {"expected_revision": 2})
    await control("chrome_safety_resume", {"expected_revision": 1})
    assert not stop[1].load(fixture[1])["engaged"]
    context.closing = True
    with pytest.raises(AssignmentError):
        await control("chrome_safety_stop", {})
    assert not stop[1].load(fixture[1])["engaged"]


async def test_cancelled_resume_waits_for_real_audit_commit_before_readmission(
    stop, human, bound, fixture, monkeypatch,
):
    first, current = await engage(stop, human, bound, fixture)
    entered, release = threading.Event(), threading.Event()
    original = stop[1].audit.insert_in_transaction

    def held(event, **kwargs):
        receipt = original(event, **kwargs)
        if event.action_type == "emergency_stop.resume":
            entered.set()
            assert release.wait(10)
        return receipt

    monkeypatch.setattr(stop[1].audit, "insert_in_transaction", held)
    task = asyncio.create_task(stop[0].resume(fixture[1], expected_revision=first["revision"],
                                            actor_id=fixture[1], caller=current))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not stop[0].admission_allowed(fixture[1])
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not stop[1].load(fixture[1])["engaged"]
    assert stop[0].admission_allowed(fixture[1])


async def test_real_framework_stop_records_credential_principal_and_revocation_denies(
    stop, human, fixture, runtime,
):
    from orchestrator.framework_credentials import FrameworkCredentialService
    from tests.helpers.session_consent_088 import consent_from_store

    credentials = FrameworkCredentialService(plane_runtime=runtime, audit=human[2].audit_repo)
    human[2].framework_work_operations = type("Operations", (), {"credentials": credentials})()
    view, token = credentials.issue(owner_id=fixture[1],
        caller=consent_from_store(fixture[0], fixture[1], fixture[2]).observation,
        name="Synthetic stop automation", scopes=("operations.read", "operations.control"),
        expires_in_seconds=3600, max_admissions=10)
    framework = credentials.resolve_bearer(token)
    assert framework is not None
    first = await stop[1].transition("emergency_stop.engage", "success", owner_id=fixture[1],
                                   detail={"reason": "automation drill"}, caller=framework)
    assert first["engaged"]
    with runtime.transaction() as tx:
        row = tx.fetch_one("SELECT auth_principal FROM audit_events WHERE actor_user_id=%s "
                           "AND action_type='emergency_stop.engage'", (fixture[1],))
    assert row["auth_principal"] == "framework:" + view["credential_id"]
    with pytest.raises(EmergencyStopRefused, match="owner_authentication_required"):
        await stop[1].transition("emergency_stop.resume", "success", owner_id=fixture[1],
                                detail={"expected_revision": first["revision"]}, caller=framework)
    credentials.revoke(owner_id=fixture[1], credential_id=view["credential_id"])
    with pytest.raises(EmergencyStopRefused):
        await stop[1].transition("emergency_stop.engage", "success", owner_id=fixture[1], caller=framework)
    assert stop[1].load(fixture[1])["revision"] == first["revision"]


async def test_stop_store_rejects_replaced_runtime_audit_and_missing_framework_authority(
    stop, human, bound, fixture, monkeypatch,
):
    from orchestrator.framework_credentials import FrameworkCaller

    current = await caller(human, bound, fixture)
    framework = FrameworkCaller(fixture[1], "missing-credential", frozenset({"operations.control"}), "hash")
    with pytest.raises(EmergencyStopRefused, match="unavailable"):
        await stop[1].transition("emergency_stop.engage", "success", owner_id=fixture[1], caller=framework)
    assert stop[1].load(fixture[1]) is None
    with pytest.raises(EmergencyStopRefused):
        stop[1]._record(object(), fixture[1])
    monkeypatch.setattr(human[2], "audit_repo", None)
    with pytest.raises(EmergencyStopRefused, match="unavailable"):
        await stop[1].transition("emergency_stop.engage", "success", owner_id=fixture[1], caller=current)


async def test_resume_receipt_cannot_remove_a_new_stop_observed_after_its_commit(stop, human, bound, fixture):
    first, current = await engage(stop, human, bound, fixture)
    committed, release = asyncio.Event(), asyncio.Event()
    original = stop[1].transition

    async def held(action, outcome, **kwargs):
        receipt = await original(action, outcome, **kwargs)
        if action == "emergency_stop.resume":
            committed.set()
            await release.wait()
        return receipt

    stop[0]._audit = held
    resume = asyncio.create_task(stop[0].resume(fixture[1], expected_revision=first["revision"],
                                              actor_id=fixture[1], caller=current))
    try:
        await committed.wait()
        assert not stop[0].status(fixture[1])["engaged"]
        other = EmergencyStopCoordinator(audit=original, rearm_loader=stop[1].load, fresh_rearm=True)
        latest = await other.engage(fixture[1], caller=current, sweep=False)
        assert latest["revision"] == 3
        assert stop[0].status(fixture[1])["revision"] == 3
    finally:
        release.set()
    result = await resume
    assert result["engaged"] and result["revision"] == 3
    assert not stop[0].admission_allowed(fixture[1])
