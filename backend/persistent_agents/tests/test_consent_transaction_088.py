"""Tests for persistent_agents/service.py and store.py against real Postgres: consent
and its assignment commit or roll back together across creation, revision, CAS
conflicts, lost-ack retries and concurrent duplicate submissions.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignment_models import AssignmentControlResult
from orchestrator import offline_grant as og
from orchestrator.session_consent import ConsentSession
from persistent_agents.models import AssignmentError, CreateAssignmentRequest, ReviseAssignmentRequest
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_models import create_payload
from tests.helpers.session_consent_088 import consent_from_store
from tests.helpers.session_plane_runtime import isolated_plane_runtime, web_session_store


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("persistent_consent_atomic_088") as value:
        yield value


@pytest.fixture
def fixture(runtime, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", key)
    monkeypatch.setattr(og, "OFFLINE_GRANT_ENC_KEY", key)
    sessions = web_session_store(runtime)
    owner, sid = str(uuid4()), str(uuid4())
    sessions.create(sid, user_id=owner, access_token="synthetic-access",
                    refresh_token="synthetic-refresh", hard_max_seconds=3600)
    grants = og.OfflineGrantStore(plane_runtime=runtime)
    host = SimpleNamespace(offline_grants=grants, web_sessions=sessions,
        tool_permissions=SimpleNamespace(list_disabled_agents=lambda owner: [],
                                         get_tool_scope=lambda *args: "tools:read"))
    monkeypatch.setattr("persistent_agents.service.eligible_tool_pairs",
                        lambda *args, **kwargs: [("web-research-1", SimpleNamespace(id="fetch_page"))])
    monkeypatch.setattr("persistent_agents.service.validate_egress_url", lambda url: None)
    store = AssignmentStore(plane_runtime=runtime)
    service = AssignmentService(host, store, enabled=True,
        phi_gate=SimpleNamespace(contains_phi=lambda value: False))
    service._audit = AsyncMock()
    prepared = []
    prepare = grants.prepare_capture

    def retain(*args, **kwargs):
        value = prepare(*args, **kwargs)
        prepared.append(value)
        return value

    monkeypatch.setattr(grants, "prepare_capture", retain)
    yield SimpleNamespace(service=service, store=store, sessions=sessions, grants=grants,
        owner=owner, sid=sid, selected=consent_from_store(sessions, owner, sid), prepared=prepared)
    store.close()
    grants.revoke_for_user(owner)
    sessions.delete_for_user(owner)


async def create(fixture, body=None, *, selected=True):
    body = body or CreateAssignmentRequest.model_validate(create_payload())
    return await fixture.service.create(fixture.owner, {"sub": fixture.owner}, body,
        selected_session=fixture.selected if selected else None)


def revision(record):
    return ReviseAssignmentRequest.model_validate({**create_payload(),
        "instructions": "Report only a newly published stable release.",
        "expected_instruction_revision": record.instruction_revision,
        "expected_control_epoch": record.control_epoch})


async def revise(fixture, record, body, *, selected=True):
    return await fixture.service.revise(fixture.owner, {"sub": fixture.owner}, record.assignment_id,
        body, selected_session=fixture.selected if selected else None)


def retained(fixture):
    return [item.grant_id for item in fixture.prepared
            if fixture.grants._grant(fixture.owner, item.grant_id) is not None]


@pytest.mark.asyncio
async def test_create_and_exact_lost_ack_retry_bind_one_grant(fixture):
    body = CreateAssignmentRequest.model_validate(create_payload())
    first = await create(fixture, body)
    replay = await create(fixture, body, selected=False)
    assert first == replay
    assert retained(fixture) == [first.definition.offline_grant_id]
    assert len(fixture.prepared) == 1
    assert fixture.grants._resolve_reference(fixture.grants._grant(
        fixture.owner, first.definition.offline_grant_id)) == fixture.selected.reference(fixture.owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capacity", "after_assignment"])
async def test_rejected_creation_rolls_back_grant_and_assignment(fixture, runtime, monkeypatch, failure):
    repository = runtime.repositories.assignments
    original = repository.create_assignment
    inserted = []

    def reject(transaction, **kwargs):
        if failure == "after_assignment":
            inserted.append(original(transaction, **kwargs).assignment_id)
        raise RepositoryConflictError("assignment_capacity_exhausted")

    monkeypatch.setattr(repository, "create_assignment", reject)
    with pytest.raises(AssignmentError):
        await create(fixture)
    assert retained(fixture) == [] and len(fixture.prepared) == 1
    assert await fixture.service.list(fixture.owner, {"sub": fixture.owner}) == ()
    assert bool(inserted) == (failure == "after_assignment")


@pytest.mark.asyncio
async def test_final_consent_expiry_rolls_back_new_assignment_and_grant(fixture, runtime, monkeypatch):
    fixture.selected = ConsentSession(replace(fixture.selected.observation,
        valid_until=fixture.selected.observation.started_at + timedelta(seconds=1)))
    original = runtime.repositories.assignments.create_assignment
    inserted = []

    def delayed(transaction, **kwargs):
        result = original(transaction, **kwargs)
        inserted.append(result.assignment_id)
        time.sleep(1.05)
        return result

    monkeypatch.setattr(runtime.repositories.assignments, "create_assignment", delayed)
    with pytest.raises(AssignmentError, match="authorization_required"):
        await create(fixture)
    assert len(inserted) == 1
    assert retained(fixture) == []
    assert await fixture.service.list(fixture.owner, {"sub": fixture.owner}) == ()


@pytest.mark.asyncio
async def test_concurrent_exact_create_retries_leave_no_orphan_grant(fixture, monkeypatch):
    original = fixture.grants.prepare_capture
    ready = threading.Barrier(2)

    def together(*args, **kwargs):
        prepared = original(*args, **kwargs)
        ready.wait(timeout=5)
        return prepared

    monkeypatch.setattr(fixture.grants, "prepare_capture", together)
    body = CreateAssignmentRequest.model_validate(create_payload())
    first, second = await asyncio.gather(create(fixture, body), create(fixture, body))
    assert first == second
    assert len(fixture.prepared) == 2
    assert retained(fixture) == [first.definition.offline_grant_id]


@pytest.mark.asyncio
async def test_revise_and_exact_lost_ack_retry_bind_only_accepted_grant(fixture):
    first = await create(fixture)
    body = revision(first)
    result = await revise(fixture, first, body)
    replay = await revise(fixture, first, body, selected=False)
    assert result.applied and not replay.applied
    assert result.assignment == replay.assignment
    assert result.assignment.instruction_revision == first.instruction_revision + 1
    assert len(fixture.prepared) == 2
    assert set(retained(fixture)) == {first.definition.offline_grant_id,
                                    result.assignment.definition.offline_grant_id}


@pytest.mark.asyncio
async def test_revision_cas_failure_preserves_previous_grant_without_new_authority(fixture, runtime, monkeypatch):
    first = await create(fixture)
    original = runtime.repositories.assignments.apply_control

    def reject(transaction, **kwargs):
        original(transaction, **kwargs)
        raise RepositoryConflictError("assignment_stale_control")

    monkeypatch.setattr(runtime.repositories.assignments, "apply_control", reject)
    with pytest.raises(AssignmentError):
        await revise(fixture, first, revision(first))
    assert retained(fixture) == [first.definition.offline_grant_id]
    assert await fixture.service.get(fixture.owner, {"sub": fixture.owner}, first.assignment_id) == first


@pytest.mark.asyncio
async def test_new_stale_revision_refuses_without_preparing_another_grant(fixture):
    first = await create(fixture)
    accepted = (await revise(fixture, first, revision(first))).assignment
    grants = retained(fixture)
    with pytest.raises(AssignmentError) as refused:
        await revise(fixture, first, revision(first), selected=False)
    assert refused.value.status_code == 409
    assert len(fixture.prepared) == 2 and retained(fixture) == grants
    assert await fixture.service.get(fixture.owner, {"sub": fixture.owner}, first.assignment_id) == accepted


@pytest.mark.asyncio
async def test_future_cas_becoming_current_cannot_apply_without_new_consent(fixture, monkeypatch):
    first = await create(fixture)
    unconsented = ReviseAssignmentRequest.model_validate({**create_payload(),
        "consent": False, "expected_instruction_revision": first.instruction_revision + 1,
        "expected_control_epoch": first.control_epoch + 1})
    original_receipt = fixture.service._receipt
    accepted = []

    async def concurrent_revision(owner, identity, submission, digest, command):
        result = await original_receipt(owner, identity, submission, digest, command)
        if submission == unconsented.submission_id and not accepted:
            accepted.append((await revise(fixture, first, revision(first))).assignment)
        return result

    monkeypatch.setattr(fixture.service, "_receipt", concurrent_revision)
    with pytest.raises(AssignmentError) as refused:
        await revise(fixture, first, unconsented, selected=False)
    assert refused.value.status_code == 409
    assert len(accepted) == 1 and len(fixture.prepared) == 2
    assert await fixture.service.get(fixture.owner, {"sub": fixture.owner}, first.assignment_id) == accepted[0]
    assert set(retained(fixture)) == {first.definition.offline_grant_id,
                                    accepted[0].definition.offline_grant_id}


@pytest.mark.asyncio
async def test_mismatched_initial_version_can_only_replay_an_exact_concurrent_receipt(fixture, monkeypatch):
    first = await create(fixture)
    body = ReviseAssignmentRequest.model_validate({**create_payload(),
        "expected_instruction_revision": first.instruction_revision + 1,
        "expected_control_epoch": first.control_epoch + 1})
    original_receipt = fixture.service._receipt
    accepted = []
    entered = False

    async def accepted_after_initial_read(owner, identity, submission, digest, command):
        nonlocal entered
        result = await original_receipt(owner, identity, submission, digest, command)
        if submission == body.submission_id and not entered:
            entered = True
            advanced = (await revise(fixture, first, revision(first))).assignment
            accepted.append((await revise(fixture, advanced, body)).assignment)
        return result

    monkeypatch.setattr(fixture.service, "_receipt", accepted_after_initial_read)
    replay = await revise(fixture, first, body, selected=False)
    assert not replay.applied and replay.assignment == accepted[0]
    assert len(fixture.prepared) == 3 and len(retained(fixture)) == 3


@pytest.mark.asyncio
async def test_concurrent_exact_revision_retries_roll_back_losing_candidate(fixture, monkeypatch):
    first = await create(fixture)
    original = fixture.grants.prepare_capture
    ready = threading.Barrier(2)

    def together(*args, **kwargs):
        prepared = original(*args, **kwargs)
        ready.wait(timeout=5)
        return prepared

    monkeypatch.setattr(fixture.grants, "prepare_capture", together)
    body = revision(first)
    left, right = await asyncio.gather(revise(fixture, first, body), revise(fixture, first, body))
    assert left.assignment == right.assignment
    assert sorted([left.applied, right.applied]) == [False, True]
    assert len(fixture.prepared) == 3
    assert set(retained(fixture)) == {first.definition.offline_grant_id,
                                    left.assignment.definition.offline_grant_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("return_accepted_receipt", [False, True])
async def test_receipt_committed_before_capture_rolls_back_losing_grant(
    fixture, runtime, monkeypatch, return_accepted_receipt,
):
    first = await create(fixture)
    body = revision(first)
    before_lock, release = threading.Event(), threading.Event()
    pending = {}
    original_capture = fixture.grants.capture_in_transaction
    repository = runtime.repositories.assignments
    original_apply = repository.apply_control

    def capture(transaction, prepared, **kwargs):
        if not pending:
            pending.update(thread=threading.get_ident(), grant=prepared.grant_id)
            before_lock.set()
            assert release.wait(timeout=5)
        return original_capture(transaction, prepared, **kwargs)

    def apply(transaction, **kwargs):
        if return_accepted_receipt and threading.get_ident() == pending["thread"]:
            accepted = repository.get_submission_receipt(transaction,
                owner_id=kwargs["owner_id"], assignment_id=kwargs["assignment_id"],
                submission_id=kwargs["submission_id"], submission_digest=kwargs["submission_digest"],
                command="revise")
            assert accepted is not None
            pending["returned_grant"] = accepted.definition.offline_grant_id
            return AssignmentControlResult(accepted, False)
        return original_apply(transaction, **kwargs)

    monkeypatch.setattr(fixture.grants, "capture_in_transaction", capture)
    monkeypatch.setattr(repository, "apply_control", apply)
    loser = asyncio.create_task(revise(fixture, first, body))
    try:
        assert await asyncio.to_thread(before_lock.wait, 5)
        winner = await revise(fixture, first, body)
    finally:
        release.set()
    replay = await loser
    assert winner.applied and not replay.applied
    assert replay.assignment == winner.assignment
    assert len(fixture.prepared) == 3
    assert set(retained(fixture)) == {first.definition.offline_grant_id,
                                    winner.assignment.definition.offline_grant_id}
    assert pending["grant"] not in retained(fixture)
    assert ("returned_grant" in pending) == return_accepted_receipt
    if return_accepted_receipt:
        assert pending["returned_grant"] == winner.assignment.definition.offline_grant_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "revise"])
async def test_unknown_commit_response_never_revokes_possibly_accepted_grant(fixture, monkeypatch, kind):
    first = await create(fixture) if kind == "revise" else None
    body = revision(first) if first else CreateAssignmentRequest.model_validate(create_payload())
    transaction = fixture.store.transaction
    fail_once = True
    initial_grants = set(retained(fixture))

    async def lost_ack(callback, **kwargs):
        nonlocal fail_once
        result = await transaction(callback, **kwargs)
        if fail_once and set(retained(fixture)) != initial_grants:
            fail_once = False
            raise AssignmentError("assignment_response_unavailable", 503)
        return result

    monkeypatch.setattr(fixture.store, "transaction", lost_ack)
    with pytest.raises(AssignmentError, match="response_unavailable"):
        if first:
            await revise(fixture, first, body)
        else:
            await create(fixture, body)
    before = retained(fixture)
    result = (await revise(fixture, first, body, selected=False)).assignment if first else await create(fixture, body, selected=False)
    assert retained(fixture) == before
    assert result.definition.offline_grant_id in before
    assert fixture.grants.is_valid(result.definition.offline_grant_id, user_id=fixture.owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "revise"])
async def test_replacement_after_preparation_cannot_create_new_grant(fixture, runtime, monkeypatch, kind):
    from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
    first = await create(fixture) if kind == "revise" else None
    existing = retained(fixture)
    original = fixture.grants.prepare_capture
    replacements = []

    def replace_after_prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        replacements.append(replace_session_record(runtime, get_session_record(runtime, fixture.sid)))
        return prepared

    monkeypatch.setattr(fixture.grants, "prepare_capture", replace_after_prepare)
    with pytest.raises(AssignmentError, match="authorization_required"):
        if first:
            await revise(fixture, first, revision(first))
        else:
            await create(fixture)
    assert retained(fixture) == existing
    assert len(replacements) == 1
    assert get_session_record(runtime, fixture.sid) == replacements[0]
    if first:
        assert await fixture.service.get(fixture.owner, {"sub": fixture.owner}, first.assignment_id) == first
    else:
        assert await fixture.service.list(fixture.owner, {"sub": fixture.owner}) == ()


@pytest.mark.asyncio
async def test_dependent_store_runtime_mismatch_rolls_back_candidate(fixture, monkeypatch):
    prepare = fixture.grants.prepare_capture
    monkeypatch.setattr(fixture.grants, "prepare_capture",
        lambda *args, **kwargs: replace(prepare(*args, **kwargs), plane_runtime=object()))
    with pytest.raises(AssignmentError, match="authorization_required"):
        await create(fixture)
    assert retained(fixture) == []
    assert await fixture.service.list(fixture.owner, {"sub": fixture.owner}) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial_receipt", "transaction_receipt", "revision_record"])
async def test_locked_receipt_read_refuses_before_blocker_is_released(fixture, runtime, monkeypatch, phase):
    first = await create(fixture) if phase == "revision_record" else None
    existing = retained(fixture)
    if phase == "transaction_receipt":
        monkeypatch.setattr(fixture.service, "_receipt", AsyncMock(return_value=None))
    started = time.monotonic()
    with runtime.transaction() as blocker:
        blocker.execute("LOCK TABLE persistent_assignment IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(AssignmentError) as refused:
            request = revise(fixture, first, revision(first)) if first else create(fixture)
            await asyncio.wait_for(request, timeout=3)
        assert refused.value.status_code == 503
        assert time.monotonic() - started < 2
        assert blocker.fetch_one("SELECT 1 AS alive")["alive"] == 1
        assert len(fixture.prepared) == (phase in {"transaction_receipt", "revision_record"})
    assert retained(fixture) == existing
    assert await fixture.service.list(fixture.owner, {"sub": fixture.owner}) == ((first,) if first else ())
