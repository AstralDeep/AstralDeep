"""Fresh human authentication, cookie-write origin checks and bounded commands."""
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from orchestrator.auth import get_web_or_bearer_user_payload
from orchestrator.work_api import _origin, _write_owner, work_router
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_controls import WorkControlRequest, WorkControlService, WorkDeleteRequest
from orchestrator.work_submit_authority import AuthenticatedWorkRequest
from persistent_agents.models import AssignmentError
from tests.test_work_api_088 import host as host


@pytest.fixture
def controls(host, monkeypatch):
    orch, read, repo = host
    repo.get_submission_receipt = Mock(return_value=None)
    repo.apply_control = Mock(return_value=SimpleNamespace(applied=True))
    repo.delete_for_owner = Mock(return_value=True)
    orch.persistent_assignments._audit = AsyncMock()
    # HTTP/command-shaping unit boundary only. The dedicated PostgreSQL suite
    # exercises the actual audit repository and mutation rollback together.
    orch.atomic_audit = Mock()
    monkeypatch.setattr("orchestrator.work_control_audit.WorkControlAudit.append", orch.atomic_audit)
    monkeypatch.setattr("orchestrator.work_control_audit.WorkControlAudit.assert_store_current", Mock())
    orch.persistent_assignments.store.async_runtime = object()
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: {
        "sub": "owner", "realm_access": {"roles": ["user"]}}

    # This module tests HTTP/command shape with mocked storage. Mock the new
    # private auth boundary explicitly; real signed IAM/session/commit races are
    # covered by test_work_write_http_postgres_088 and the PG control cohorts.
    def local(caller, assignments):
        assignments._owner(caller.context.owner_id, caller.context.claims)

    monkeypatch.setattr(WorkCallerAuthority, "_assert_local", local)
    monkeypatch.setattr(WorkCallerAuthority, "assert_current",
                        lambda caller, tx, *, assignments: local(caller, assignments))
    monkeypatch.setattr(WorkCallerAuthority, "verify_delivery", AsyncMock())

    def caller_for(claims=None):
        if claims is None:
            claims = {"sub": "owner", "realm_access": {"roles": ["user"]}}
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        context = AuthenticatedWorkRequest(claims.get("sub"), expiry, json.dumps(claims),
            None, None, orch.persistent_assignments.store.plane_runtime)
        return WorkCallerAuthority(context, None, SimpleNamespace(assignments=orch.persistent_assignments),
            "fixture.access.token", time.monotonic() + 15, datetime.now(timezone.utc) + timedelta(seconds=15))

    async def authenticate(request, *, assignments, sessions):
        from orchestrator import auth
        override = app.dependency_overrides.get(get_web_or_bearer_user_payload)
        claims = override() if override else await auth.get_web_or_bearer_user_payload(
            request, await auth.security(request))
        claims = await auth.verify_user(claims)
        await _write_owner(request, claims.get("sub"))
        caller = caller_for(claims)
        caller._assert_local(assignments)
        return caller

    monkeypatch.setattr("orchestrator.work_api.authenticate_work_control_request", authenticate)
    orch.control_caller = caller_for
    return app, orch, read, repo


def body(**changes):
    return {"submission_id": str(uuid4()), "expected_revision": 7, **changes}


@pytest.mark.parametrize("revision", [True, False, "7", 7.0, 0, -1, 2**63, None])
def test_command_revision_never_coerces_or_exceeds_wire_integer(revision):
    with pytest.raises(ValidationError):
        WorkControlRequest.model_validate(body(expected_revision=revision))


@pytest.mark.parametrize("submission", ["x", str(uuid4()).upper(), True, None, "00000000-0000-1000-8000-000000000000"])
def test_submission_identity_is_canonical_uuid4(submission):
    with pytest.raises(ValidationError):
        WorkControlRequest.model_validate(body(submission_id=submission))


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["cancel", "pause"])
async def test_exact_write_routes_pass_only_server_owner_and_versioned_command(controls, command):
    app, orch, read, repo = controls
    payload = body()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(f"/api/work/v1/operations/{read.assignment.assignment_id}/{command}",
                                   json=payload, headers={"Origin": "http://test"})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert set(result.json()) == {"operation", "applied"} and result.json()["applied"] is True
    assert "private" not in result.text
    values = repo.apply_control.call_args.kwargs
    assert values["owner_id"] == "owner" and values["expected_state_version"] == 7
    assert values["expected_instruction_revision"] == 2 and values["expected_control_epoch"] == 3
    assert values["control"] == ("stop" if command == "cancel" else "pause")
    assert values["submission_id"] == payload["submission_id"]
    orch.atomic_audit.assert_called_once()
    assert orch.atomic_audit.call_args.kwargs["command"] == values["control"]
    orch.persistent_assignments._audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_and_duplicate_receipt_do_not_invent_effect_or_audit(controls):
    app, orch, read, repo = controls
    repo.get_submission_receipt.return_value = read.assignment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/work/v1/operations/{read.assignment.assignment_id}"
        replay = await client.post(path + "/pause", json=body(), headers={"Origin": "http://test"})
        assert replay.json()["applied"] is False
        repo.apply_control.assert_not_called()
        orch.atomic_audit.assert_not_called()
        orch.persistent_assignments._audit.assert_not_awaited()
        deleted = await client.request("DELETE", path, json={"expected_revision": 7},
                                       headers={"Authorization": "Bearer fixture"})
        assert deleted.status_code == 200 and deleted.json() == {"id": read.assignment.assignment_id, "deleted": True}
        assert repo.delete_for_owner.call_args.kwargs["expected_control_epoch"] == 3
        assert repo.delete_for_owner.call_args.kwargs["expected_state_version"] == 7
        orch.atomic_audit.assert_called_once()
        assert orch.atomic_audit.call_args.kwargs["command"] == "delete"
        orch.persistent_assignments._audit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"Origin": "null"}, {"Origin": "https://foreign.test"}, {"Origin": "http://test:81"},
    {"Origin": "http://test:0"},
    {"Origin": "http://user@test"}, {"Origin": "http://test/path"},
    {"Origin": "http://test?secret=x"}, {"Origin": "http://test#fragment"},
    {"Origin": " http://test"}, {"Origin": "http://test:wrong"},
    [("Origin", "http://test"), ("Origin", "http://foreign.test")],
])
async def test_ambient_cookie_write_requires_one_exact_origin(controls, headers):
    app, _, read, repo = controls
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/work/v1/operations/{read.assignment.assignment_id}/cancel",
                                     json=body(), headers=headers)
    assert response.status_code == 403 and response.json() == {"error": "work_origin_refused"}
    assert response.headers["cache-control"] == "no-store"
    repo.get_operation.assert_not_called()


def test_origin_parser_refuses_nontext_and_normalizes_default_port():
    with pytest.raises(AssignmentError, match="work_origin_refused"):
        _origin(None)
    assert _origin("https://TEST:443") == _origin("https://test")
    assert _origin("http://test:80") == _origin("http://test")
    assert _origin("https://test:0") != _origin("https://test")


@pytest.mark.asyncio
async def test_configured_public_origin_and_explicit_bearer_are_separate_boundaries(controls, monkeypatch):
    app, _, read, _ = controls
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://public.example/work")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal") as client:
        path = f"/api/work/v1/operations/{read.assignment.assignment_id}/pause"
        assert (await client.post(path, json=body(), headers={"Origin": "http://internal"})).status_code == 403
        assert (await client.post(path, json=body(), headers={"Origin": "https://public.example"})).status_code == 200
        assert (await client.post(path, json=body(), headers={"Authorization": "Bearer fixture",
                                                             "Origin": "https://foreign.example"})).status_code == 200
        assert (await client.post(path + "?token=", json=body(), headers={"Authorization": "Bearer fixture"})).status_code == 403
        invalid = await client.post(path, content='{"expected_revision":7}',
                                    headers={"Authorization": "Bearer fixture", "Content-Type": "text/plain"})
        assert invalid.status_code == 415 and invalid.json() == {"error": "work_json_required"}
        monkeypatch.setenv("PUBLIC_BASE_URL", "not-an-origin")
        assert (await client.post(path, json=body(), headers={"Origin": "http://internal"})).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [
    {"sub": "owner", "act": {"sub": "delegate"}}, {"sub": "owner", "machine_class": "framework"},
    {"sub": "owner", "realm_access": {"roles": []}}, {},
])
async def test_nonhuman_or_unauthorized_principal_never_reaches_storage(controls, claims):
    app, _, read, repo = controls
    app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: claims
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/work/v1/operations/{read.assignment.assignment_id}/pause",
                                     json=body(), headers={"Origin": "http://test"})
    assert response.status_code in {401, 403} and response.headers["cache-control"] == "no-store"
    repo.get_operation.assert_not_called()


@pytest.mark.asyncio
async def test_flag_off_extra_authority_unknown_commands_and_private_errors_fail_closed(controls):
    app, orch, read, repo = controls
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/work/v1/operations/{read.assignment.assignment_id}"
        assert (await client.post(path + "/resume", json=body())).status_code == 404
        assert (await client.post("/api/work/v1/operations", json={})).status_code == 405
        invalid = await client.post(path + "/pause", json=body(owner_id="other"), headers={"Origin": "http://test"})
        assert invalid.status_code == 422 and invalid.json() == {"error": "work_control_invalid"}
        orch.persistent_assignments.enabled = False
        assert (await client.post(path + "/pause", json=body(), headers={"Origin": "http://test"})).status_code == 503
        repo.get_operation.assert_not_called()
        orch.persistent_assignments.enabled = True
        repo.apply_control.side_effect = AssignmentError("private upstream detail", 503)
        failed = await client.post(path + "/pause", json=body(), headers={"Origin": "http://test"})
        assert failed.json() == {"error": "work_control_unavailable"}
        assert failed.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_service_missing_contract_malformed_record_and_delete_race_are_bounded(controls):
    _, orch, read, repo = controls
    service = WorkControlService(orch.persistent_assignments)
    caller = orch.control_caller()
    args = ("owner", caller.context.claims, read.assignment.assignment_id)
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        await service.control(*args, "resume", WorkControlRequest(**body()), caller=caller)
    repo.apply_control = None
    with pytest.raises(AssignmentError, match="work_repository_unavailable"):
        await service.control(*args, "pause", WorkControlRequest(**body()), caller=caller)
    repo.delete_for_owner.return_value = False
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service.delete(*args, WorkDeleteRequest(expected_revision=7), caller=caller)
    repo.get_operation.side_effect = AttributeError("private future envelope")
    with pytest.raises(AssignmentError, match="work_control_unavailable"):
        await service.delete(*args, WorkDeleteRequest(expected_revision=7), caller=caller)
    repo.get_operation.side_effect = AssignmentError("assignment_not_found", 404)
    with pytest.raises(AssignmentError, match="work_not_found"):
        await service.delete(*args, WorkDeleteRequest(expected_revision=7), caller=caller)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["cookie", "bearer"])
async def test_write_uses_original_credential_verification_on_every_retry(controls, monkeypatch, transport):
    from jose import JWTError
    from orchestrator import auth, web_auth

    app, _, read, repo = controls
    app.dependency_overrides.clear()
    # Broad suite collection may enable the development mock in other modules.
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setattr(auth, "_get_keycloak_config", lambda: ("https://iam.example/realms/test", "astral-frontend", ""))
    monkeypatch.setattr("shared.jwks_cache.get_jwks", AsyncMock(return_value={"keys": []}))
    session = AsyncMock(return_value={"access_token": "fixture.access.token"})
    monkeypatch.setattr(web_auth, "ensure_session", session)
    decode = Mock(return_value={"sub": "owner", "realm_access": {"roles": ["user"]}})
    monkeypatch.setattr("jose.jwt.decode", decode)
    headers = {"Authorization": "Bearer fixture.access.token"} if transport == "bearer" else {"Origin": "http://test"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/work/v1/operations/{read.assignment.assignment_id}/pause"
        payload = body()
        assert (await client.post(path, json=payload, headers=headers)).status_code == 200
        decode.return_value = {"sub": "owner", "realm_access": {"roles": []}}
        assert (await client.post(path, json=payload, headers=headers)).status_code == 403
        decode.side_effect = JWTError("private expired credential")
        expired = await client.post(path, json=payload, headers=headers)
        assert expired.status_code == 401 and "private" not in expired.text
        assert expired.headers["cache-control"] == "no-store"
    assert decode.call_count == 3 and repo.apply_control.call_count == 1
    assert session.call_count == (3 if transport == "cookie" else 0)
