"""Tests for mounted Work writes (backend/orchestrator/work_api.py,
work_control_audit.py, work_controls.py): caller identity retained across body and
audit waits, and lost-delivery retry without re-auditing.
"""

import asyncio
from dataclasses import replace
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from orchestrator import auth
from orchestrator.work_api import work_router
from orchestrator.work_control_audit import WorkControlAudit
from orchestrator.work_controls import WorkControlRequest, WorkControlService, WorkDeleteRequest
from persistent_agents.models import AssignmentError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_operation_session_authority_088 import create_operation
from tests.test_work_continuation_authority_088 import current
from tests.test_work_control_authority_088 import bound as bound
from tests.test_work_submit_postgres_088 import (
    fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mounted(bound, fixture, runtime):
    assignments, app = bound
    app.include_router(work_router, prefix="/api")
    record = create_operation(fixture, runtime)
    return assignments, app, record


def payload(revision=1):
    return {"submission_id": str(uuid4()), "expected_revision": revision}


def headers(fixture, *, cookie=True, expires=None):
    from orchestrator import web_auth
    fields = {"Authorization": "Bearer " + fixture[3](**({"exp": expires} if expires else {})),
              "Content-Type": "application/json"}
    if cookie:
        fields["Cookie"] = "astral_session=" + web_auth._sign(fixture[2])
    return fields


def path(record, command):
    return f"/api/work/v1/operations/{record.assignment_id}/{command}"


@pytest.mark.parametrize("cookie", [True, False])
async def test_mounted_pause_retry_cancel_delete_use_real_iam_without_refresh(mounted, fixture, runtime, cookie):
    _, app, record = mounted
    body = payload(record.state_version)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        first = await client.post(path(record, "pause"), json=body, headers=headers(fixture, cookie=cookie))
        assert first.status_code == 200 and first.json()["applied"] is True
        retry = await client.post(path(record, "pause"), json=body, headers=headers(fixture, cookie=cookie))
        assert retry.status_code == 200 and retry.json()["applied"] is False
        stop = await client.post(path(record, "cancel"), json=payload(first.json()["operation"]["revision"]),
                                 headers=headers(fixture, cookie=cookie))
        assert stop.status_code == 200
        deleted = await client.request("DELETE", path(record, "").rstrip("/"),
            json={"expected_revision": stop.json()["operation"]["revision"]}, headers=headers(fixture, cookie=cookie))
        assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert fixture[-1] == []
    rows, _ = app.state.orchestrator.audit_repo.list_for_user(fixture[1])
    assert len(rows) == 3
    assert app.state.orchestrator.audit_repo.verify_chain(fixture[1]) is None


async def test_cookie_replacement_during_body_wait_cannot_be_adopted(mounted, fixture, runtime):
    _, app, record = mounted
    async def stream():
        old = await asyncio.to_thread(get_session_record, runtime, fixture[2])
        replaced = await asyncio.to_thread(replace_session_record, runtime, old)
        assert replaced.incarnation_id != old.incarnation_id
        yield json.dumps(payload(record.state_version)).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.post(path(record, "pause"), content=stream(), headers=headers(fixture))
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert current(runtime, record) == record
    assert app.state.orchestrator.audit_repo.list_for_user(fixture[1])[0] == []


@pytest.mark.parametrize("cookie", [True, False])
async def test_principal_expiry_during_audit_rolls_back_operation_receipt_and_audit(
        mounted, fixture, runtime, monkeypatch, cookie):
    _, app, record = mounted
    expiry = time.time() + 2
    original = WorkControlAudit.append
    def append(*args, **kwargs):
        value = original(*args, **kwargs)
        time.sleep(max(0, expiry - time.time()) + .03)
        return value
    monkeypatch.setattr(WorkControlAudit, "append", append)
    body = payload(record.state_version)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.post(path(record, "pause"), json=body,
                                     headers=headers(fixture, cookie=cookie, expires=expiry))
        assert response.status_code == 401
        assert current(runtime, record) == record
        assert app.state.orchestrator.audit_repo.list_for_user(fixture[1])[0] == []
        monkeypatch.setattr(WorkControlAudit, "append", original)
        retry = await client.post(path(record, "pause"), json=body, headers=headers(fixture, cookie=cookie))
        assert retry.status_code == 200 and retry.json()["applied"] is True


@pytest.mark.parametrize("raw", [
    b'{"expected_revision":1,"expected_revision":1,"submission_id":"SUBMISSION"}',
    b'{"expected_revision":1,"submission_id":"SUBMISSION","secret":"' + b'x' * 17000 + b'"}',
], ids=["duplicate-key", "oversized"])
async def test_mounted_write_rejects_ambiguous_or_oversized_raw_json(mounted, fixture, runtime, raw):
    _, app, record = mounted
    raw = raw.replace(b"SUBMISSION", str(uuid4()).encode())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.post(path(record, "pause"), content=raw, headers=headers(fixture))
    assert response.status_code == (413 if len(raw) > 16384 else 400)
    assert response.headers["cache-control"] == "no-store"
    assert current(runtime, record) == record
    assert app.state.orchestrator.audit_repo.list_for_user(fixture[1])[0] == []


async def test_lost_delivery_keeps_accepted_receipt_and_retry_does_not_repeat_audit(
        mounted, fixture, runtime, monkeypatch):
    _, app, record = mounted
    body = payload(record.state_version)
    original = auth.verify_production_token
    verifications = []
    async def verify(token):
        verifications.append(True)
        if len(verifications) == 2:
            raise HTTPException(401, "private verifier failure")
        return await original(token)
    monkeypatch.setattr(auth, "verify_production_token", verify)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        refused = await client.post(path(record, "pause"), json=body, headers=headers(fixture))
        assert refused.status_code == 401 and "private" not in refused.text
        accepted = current(runtime, record)
        assert accepted.lifecycle == "paused" and accepted.state_version == record.state_version + 1
        assert len(app.state.orchestrator.audit_repo.list_for_user(fixture[1])[0]) == 1
        monkeypatch.setattr(auth, "verify_production_token", original)
        retry = await client.post(path(record, "pause"), json=body, headers=headers(fixture))
        assert retry.status_code == 200 and retry.json()["applied"] is False
    assert current(runtime, record) == accepted
    assert len(app.state.orchestrator.audit_repo.list_for_user(fixture[1])[0]) == 1
    assert fixture[-1] == []


@pytest.mark.parametrize("command", ["pause", "delete"])
async def test_direct_service_cannot_fall_back_to_claims_without_private_caller(mounted, fixture, runtime, command):
    assignments, _, record = mounted
    service = WorkControlService(assignments)
    claims = {"sub": fixture[1], "realm_access": {"roles": ["user"]}}
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        if command == "pause":
            await service.control(fixture[1], claims, record.assignment_id, command,
                                  WorkControlRequest(**payload(record.state_version)))
        else:
            await service.delete(fixture[1], claims, record.assignment_id,
                                 WorkDeleteRequest(expected_revision=record.state_version))
    assert current(runtime, record) == record


async def test_original_attempt_deadline_bounds_body_wait_without_mutation(mounted, fixture, runtime, monkeypatch):
    from orchestrator import work_api
    _, app, record = mounted
    original = work_api.authenticate_work_control_request
    captured = []
    async def authenticate(*args, **kwargs):
        caller = await original(*args, **kwargs)
        captured.append(caller)
        return replace(caller, _deadline=time.monotonic() + .03)
    monkeypatch.setattr(work_api, "authenticate_work_control_request", authenticate)
    async def stream():
        await asyncio.sleep(.2)
        yield json.dumps(payload(record.state_version)).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.post(path(record, "pause"), content=stream(), headers=headers(fixture))
    assert len(captured) == 1
    assert response.status_code == 503 and response.json() == {"error": "work_control_unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert current(runtime, record) == record
    assert app.state.orchestrator.audit_repo.list_for_user(fixture[1])[0] == []
