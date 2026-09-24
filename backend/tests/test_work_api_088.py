"""Tests for orchestrator/work_api.py's owner-only operation reads over work_service.py:
an exact public field allowlist, owner-policy checks before any repository read,
cursor-based paging, and SSE polling with re-authentication on every request.
"""

from datetime import UTC, datetime
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request

from orchestrator.auth import get_web_or_bearer_user_payload
from orchestrator.work_api import work_router
from orchestrator.work_service import WorkService
from persistent_agents.models import AssignmentError
from persistent_agents.service import AssignmentService


def read_claims(roles=("user",)):
    return {"sub": "owner", "exp": int(time.time()) + 3600,
            "realm_access": {"roles": list(roles)}}


def override_read_auth(app, monkeypatch, roles=("user",)):
    claims = read_claims(roles)
    async def authenticate(request: Request):
        request.state.delegation_subject_token = "fixture.access.token"
        return claims
    app.dependency_overrides[get_web_or_bearer_user_payload] = authenticate
    monkeypatch.setattr("orchestrator.auth.verify_production_token", AsyncMock(return_value=claims))
    return authenticate


def operation(owner="owner", *, supported=True):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return SimpleNamespace(
        assignment=SimpleNamespace(
            assignment_id=str(uuid4()), owner_id=owner, execution_profile="one_shot",
            state_version=7, instruction_revision=2, control_epoch=3,
            definition=SimpleNamespace(name="Research task", instructions="private instruction"),
            lifecycle="active", phase="waiting", created_at=now, updated_at=now,
            next_wake_at=now, safe_error_code="assignment_action_uncertain",
            usage={"spent": {"tokens": 12, "tool_calls": 1, "private": "private usage"},
                   "outstanding": {"tokens": 5}, "private": {"secret": "private usage"}},
            operation={"version": 1 if supported else 2, "kind": "research", "deadline_at": now,
                       "authority": {"reference_id": "private credential"},
                       "result_reference": "private artifact", "source_text": "private source"},
            checkpoint={"secret": "private checkpoint"}, tasks=[{"secret": "private task"}],
        ), disposition="queued" if supported else "unsupported_version",
        continuation_supported=supported, result_reference="private artifact", terminal_outcome=None,
    )


@pytest.fixture
def host():
    read = operation()
    repository = SimpleNamespace(get_operation=Mock(return_value=read), list_operations=Mock(return_value=(read,)))
    async def transaction(callback, **kwargs):
        return callback(object(), repository)
    state = SimpleNamespace(observed_at=datetime.now(UTC), credential=SimpleNamespace(
        owner_id="owner", session_id="fixture-sid", incarnation_id="fixture-incarnation",
        interactive_anchor=0, hard_expires_at=int(time.time()) + 3600))
    runtime = SimpleNamespace(repositories=SimpleNamespace(history=SimpleNamespace(
        sessions=SimpleNamespace(get_execution_state=Mock(return_value=state)))))
    store = SimpleNamespace(repository=repository, transaction=AsyncMock(side_effect=transaction), plane_runtime=runtime)
    orch = SimpleNamespace()
    backing = AssignmentService(orch, store=store, enabled=True, phi_gate=object())
    orch.persistent_assignments = backing
    return orch, read, repository


@pytest.mark.asyncio
async def test_projection_is_an_exact_public_allowlist(host):
    orch, read, _ = host
    result = await WorkService(orch.persistent_assignments).get("owner", {"sub": "owner"}, read.assignment.assignment_id)
    assert set(result) == {"id", "revision", "instruction_revision", "control_epoch", "title",
                           "kind", "disposition", "lifecycle", "phase", "created_at", "updated_at",
                           "next_wake_at", "deadline_at", "schema_supported", "safe_error_code", "usage"}
    assert result["revision"] == 7 and result["kind"] == "research"
    assert result["usage"] == {"spent": {"tokens": 12, "tool_calls": 1}, "outstanding": {"tokens": 5}}
    assert "private" not in str(result)
    assert "actions" not in result and "result_reference" not in result


@pytest.mark.asyncio
async def test_unsupported_version_exposes_only_safe_outer_state(host):
    orch, read, repo = host
    unknown = operation(supported=False)
    unknown.assignment.operation = {"version": 2, "kind": {"secret": "private"}, "deadline_at": "private"}
    unknown.assignment.safe_error_code = "provider said private credential"
    repo.get_operation.return_value = unknown
    result = await WorkService(orch.persistent_assignments).get("owner", {"sub": "owner"}, unknown.assignment.assignment_id)
    assert result["schema_supported"] is False
    assert result["kind"] is None and result["deadline_at"] is None
    assert result["disposition"] == "unsupported_version"
    assert result["safe_error_code"] == "work_unavailable"
    assert "private" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{"sub": "another"}, {"sub": "owner", "act": {"sub": "delegate"}},
                                   {"sub": "owner", "machine_class": "framework"}, {}])
async def test_current_owner_policy_runs_before_any_repository_read(host, claims):
    orch, read, repo = host
    with pytest.raises(AssignmentError):
        await WorkService(orch.persistent_assignments).get("owner", claims, read.assignment.assignment_id)
    repo.get_operation.assert_not_called()


@pytest.mark.asyncio
async def test_absent_foreign_legacy_and_invalid_ids_share_not_found(host):
    orch, read, repo = host
    service = WorkService(orch.persistent_assignments)
    legacy = operation()
    legacy.assignment.execution_profile = "persistent"
    for record in (None, operation("other"), legacy):
        repo.get_operation.return_value = record
        with pytest.raises(AssignmentError) as failure:
            await service.get("owner", {"sub": "owner"}, read.assignment.assignment_id)
        assert (failure.value.status_code, failure.value.code) == (404, "work_not_found")
    for identity in ("no", str(uuid4()).upper(), True):
        with pytest.raises(AssignmentError) as failure:
            await service.get("owner", {"sub": "owner"}, identity)
        assert failure.value.status_code == 404


@pytest.mark.asyncio
async def test_disabled_or_unqualified_repository_never_claims_empty_success(host):
    orch, _, repo = host
    service = WorkService(orch.persistent_assignments)
    orch.persistent_assignments.enabled = False
    with pytest.raises(AssignmentError) as failure:
        await service.list("owner", {"sub": "owner"})
    assert failure.value.status_code == 503
    repo.list_operations.assert_not_called()
    orch.persistent_assignments.enabled = True
    repo.list_operations = None
    with pytest.raises(AssignmentError) as failure:
        await service.list("owner", {"sub": "owner"})
    assert failure.value.code == "work_repository_unavailable"


@pytest.mark.asyncio
async def test_poll_reports_same_changed_and_ahead_revision_with_resync(host):
    orch, read, _ = host
    service = WorkService(orch.persistent_assignments)
    for observed in (None, 6, 7, 8):
        result = await service.poll("owner", {"sub": "owner"}, read.assignment.assignment_id, after_revision=observed)
        assert result["revision"] == 7
        assert result["changed"] == (observed != 7)
        assert result["resync_required"] == (observed == 8)
        assert (result["operation"] is not None) == (observed != 7)
    for invalid in (True, 1.5, 0, -1, 2**63):
        with pytest.raises(AssignmentError):
            await service.poll("owner", {"sub": "owner"}, read.assignment.assignment_id, after_revision=invalid)


@pytest.mark.asyncio
async def test_page_is_bounded_and_cursor_is_the_last_visible_uuid(host):
    orch, read, repo = host
    service = WorkService(orch.persistent_assignments)
    page = await service.list("owner", {"sub": "owner"}, limit=1)
    assert page["next_cursor"] == read.assignment.assignment_id
    assert page["page_full"] is True
    repo.list_operations.return_value = ()
    page = await service.list("owner", {"sub": "owner"}, limit=1, after_id=read.assignment.assignment_id)
    assert page == {"operations": [], "next_cursor": None, "page_full": False}
    assert repo.list_operations.call_args.kwargs == {"owner_id": "owner", "limit": 1, "after_id": read.assignment.assignment_id}
    for invalid in (True, 1.5, 0, 101):
        with pytest.raises(AssignmentError):
            await service.list("owner", {"sub": "owner"}, limit=invalid)


@pytest.mark.asyncio
async def test_http_poll_reauthenticates_every_request_and_exposes_no_mutations(host, monkeypatch):
    orch, read, repo = host
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    normal = override_read_auth(app, monkeypatch)
    calls = []
    async def authenticate(request: Request):
        from fastapi import HTTPException
        calls.append(True)
        if len(calls) > 1:
            raise HTTPException(401, "private token expired")
        return await normal(request)
    app.dependency_overrides[get_web_or_bearer_user_payload] = authenticate
    path = f"/api/work/v1/operations/{read.assignment.assignment_id}/poll?after_revision=7"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(path)
        second = await client.get(path)
        assert first.status_code == 200 and first.json()["changed"] is False
        assert second.status_code == 401 and "private" not in second.text
        assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"
        assert len(calls) == 2 and repo.get_operation.call_count == 1
        assert (await client.post("/api/work/v1/operations", json={})).status_code == 405


@pytest.mark.asyncio
async def test_http_rejects_noncanonical_integer_queries_and_registers_exact_prefix(host, monkeypatch):
    from orchestrator.api import operation_router
    orch, read, _ = host
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(operation_router)
    override_read_auth(app, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/work/v1/operations")).status_code == 200
        detail = await client.get(f"/api/work/v1/operations/{read.assignment.assignment_id}")
        assert detail.status_code == 200 and detail.json()["operation"]["id"] == read.assignment.assignment_id
        assert detail.headers["cache-control"] == "no-store" and "private" not in detail.text
        assert (await client.get("/api/work/v1/operations/not-an-id")).status_code == 404
        for value in ("true", "1.0", "01", "-1", "0", str(2**63)):
            response = await client.get(f"/api/work/v1/operations/{read.assignment.assignment_id}/poll", params={"after_revision": value})
            assert response.status_code == 422
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["cookie", "bearer"])
async def test_original_auth_path_rechecks_credential_and_roles(host, monkeypatch, transport):
    from jose import JWTError
    from orchestrator import auth, web_auth
    orch, read, repo = host
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setattr(auth, "_get_keycloak_config", lambda: ("https://iam.example/realms/test", "astral-frontend", ""))
    monkeypatch.setattr("shared.jwks_cache.get_jwks", AsyncMock(return_value={"keys": []}))
    session = AsyncMock(return_value={"access_token": "fixture.access.token",
        "sid": "fixture-sid", "incarnation_id": "fixture-incarnation"})
    monkeypatch.setattr(web_auth, "ensure_session", session)
    decode = Mock(return_value=read_claims())
    monkeypatch.setattr("jose.jwt.decode", decode)
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    headers = {"Authorization": "Bearer fixture.access.token"} if transport == "bearer" else {}
    path = f"/api/work/v1/operations/{read.assignment.assignment_id}/poll"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path, headers=headers)).status_code == 200
        decode.return_value = {"sub": "owner", "realm_access": {"roles": []}}
        denied = await client.get(path, headers=headers)
        assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
        decode.side_effect = JWTError("fixture credential expired")
        expired = await client.get(path, headers=headers)
        assert expired.status_code == 401 and "fixture" not in expired.text
        assert decode.call_count == 4 and repo.get_operation.call_count == 1
        assert session.call_count == (3 if transport == "cookie" else 0)


@pytest.mark.asyncio
async def test_signed_out_cookie_invalid_and_missing_service_are_safe(host, monkeypatch):
    from orchestrator import web_auth
    orch, _, repo = host
    session = AsyncMock(return_value=None)
    monkeypatch.setattr(web_auth, "ensure_session", session)
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/api/work/v1/operations")
        assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
        redirect = await client.get("/api/work/v1/operations", headers={"Accept": "text/html"})
        assert redirect.status_code == 302 and redirect.headers["location"].startswith("/auth/login?next=")
        assert redirect.headers["cache-control"] == "no-store"
        override_read_auth(app, monkeypatch, roles=("admin",))
        del app.state.orchestrator
        response = await client.get("/api/work/v1/operations")
        assert response.status_code == 503 and response.json() == {"error": "work_read_unavailable"}
        app._root_app = SimpleNamespace(state=SimpleNamespace(orchestrator=orch))
        assert (await client.get("/api/work/v1/operations")).status_code == 200
        repo.list_operations.side_effect = AssignmentError("private upstream message", 503)
        response = await client.get("/api/work/v1/operations")
        assert response.status_code == 503 and "private" not in response.text


@pytest.mark.asyncio
async def test_projection_failure_never_returns_partial_page_or_private_data(host):
    orch, read, repo = host
    service = WorkService(orch.persistent_assignments)
    read.assignment.usage["spent"]["tokens"] = None
    read.assignment.usage["spent"]["tool_calls"] = 0
    read.assignment.safe_error_code = None
    read.assignment.next_wake_at = None
    read.assignment.operation["deadline_at"] = "2026-09-12T00:00:00+00:00"
    valid = await service.get("owner", {"sub": "owner"}, read.assignment.assignment_id)
    assert valid["usage"]["spent"] == {"tokens": None, "tool_calls": 0}
    assert valid["safe_error_code"] is None and valid["next_wake_at"] is None
    for value in (-1, True, 1.5, 2**63):
        read.assignment.usage["spent"]["tokens"] = value
        with pytest.raises(AssignmentError) as failure:
            await service.list("owner", {"sub": "owner"})
        assert (failure.value.code, failure.value.status_code) == ("work_read_unavailable", 503)
    read.assignment.usage["spent"]["tokens"] = 0
    read.assignment.operation["deadline_at"] = "2026-09-12T00:00:00"
    with pytest.raises(AssignmentError, match="work_read_unavailable"):
        await service.get("owner", {"sub": "owner"}, read.assignment.assignment_id)
    read.assignment.operation["deadline_at"] = None
    read.disposition = "private vendor response"
    with pytest.raises(AssignmentError, match="work_read_unavailable"):
        await service.list("owner", {"sub": "owner"})
    repo.get_operation.side_effect = ValueError("private adapter error")
    with pytest.raises(AssignmentError, match="work_read_unavailable"):
        await service.get("owner", {"sub": "owner"}, read.assignment.assignment_id)


@pytest.mark.asyncio
async def test_revoked_cookie_cannot_reuse_the_previous_poll_identity(host, monkeypatch):
    from orchestrator import auth, web_auth
    orch, read, repo = host
    session = AsyncMock(side_effect=[{"access_token": "fixture.access.token",
        "sid": "fixture-sid", "incarnation_id": "fixture-incarnation"}, None])
    monkeypatch.setattr(web_auth, "ensure_session", session)
    claims = read_claims()
    async def verify(request, credentials):
        request.state.delegation_subject_token = credentials.credentials
        return claims
    validate = AsyncMock(side_effect=verify)
    monkeypatch.setattr(auth, "get_current_user_payload", validate)
    monkeypatch.setattr(auth, "verify_production_token", AsyncMock(return_value=claims))
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    path = f"/api/work/v1/operations/{read.assignment.assignment_id}/poll"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 200
        revoked = await client.get(path)
        assert revoked.status_code == 401 and revoked.headers["cache-control"] == "no-store"
        assert session.call_count == 2 and validate.call_count == repo.get_operation.call_count == 1


@pytest.mark.asyncio
async def test_server_private_dispatch_context_cannot_use_human_read_facade(host, monkeypatch):
    orch, read, repo = host
    monkeypatch.setattr("persistent_agents.dispatch_context.current_dispatch", lambda: object())
    with pytest.raises(AssignmentError, match="assignment_human_required"):
        await WorkService(orch.persistent_assignments).get("owner", {"sub": "owner"}, read.assignment.assignment_id)
    repo.get_operation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", [None, "", True, 12, [], {}])
async def test_http_read_requires_a_nonempty_text_subject_before_storage(host, subject):
    orch, _, repo = host
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: {
        "sub": subject, "realm_access": {"roles": ["user"]}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/api/work/v1/operations")
        assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    repo.list_operations.assert_not_called()


@pytest.mark.asyncio
async def test_list_reports_resync_when_the_cursor_no_longer_exists_for_the_owner(host, monkeypatch):
    orch, read, repo = host
    service = WorkService(orch.persistent_assignments)
    cursor = read.assignment.assignment_id
    valid = await service.list("owner", {"sub": "owner"}, limit=1, after_id=cursor)
    assert set(valid) == {"operations", "next_cursor", "page_full"}
    assert valid["next_cursor"] == cursor and valid["page_full"] is True
    repo.list_operations.reset_mock()
    legacy = operation()
    legacy.assignment.execution_profile = "persistent"
    for missing in (None, operation("other"), legacy):
        repo.get_operation.return_value = missing
        page = await service.list("owner", {"sub": "owner"}, limit=1, after_id=cursor)
        assert page == {"operations": [], "next_cursor": None, "page_full": False, "resync_required": True}
    repo.list_operations.assert_not_called()
    repo.get_operation.side_effect = AssignmentError("assignment_not_found", 404)
    page = await service.list("owner", {"sub": "owner"}, after_id=cursor)
    assert page["resync_required"] is True
    repo.get_operation.side_effect = AttributeError("private future envelope")
    with pytest.raises(AssignmentError, match="work_read_unavailable"):
        await service.list("owner", {"sub": "owner"}, after_id=cursor)
    repo.get_operation.side_effect = AssignmentError("assignment_transaction_unavailable", 503)
    with pytest.raises(AssignmentError, match="assignment_transaction_unavailable"):
        await service.list("owner", {"sub": "owner"}, after_id=cursor)
    repo.get_operation.side_effect = None
    repo.get_operation.return_value = None
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    override_read_auth(app, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/work/v1/operations", params={"after_id": cursor})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json() == {"operations": [], "next_cursor": None, "page_full": False,
                                   "resync_required": True}
        assert (await client.get("/api/work/v1/operations", params={"after_id": "not-an-id"})).status_code == 404
    repo.get_operation = None
    with pytest.raises(AssignmentError, match="work_repository_unavailable"):
        await service.list("owner", {"sub": "owner"}, after_id=cursor)


@pytest.mark.asyncio
async def test_sse_loop_is_closed_over_failures_and_the_original_cap(monkeypatch):
    from fastapi import HTTPException
    from orchestrator import work_api
    clock = [1000.0]
    monkeypatch.setattr(work_api, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(work_api, "SSE_INTERVAL_SECONDS", 0.001)
    assert work_api._sse_failure(HTTPException(401)) == "work_authentication_required"
    assert work_api._sse_failure(AssignmentError("work_not_found", 404)) == "work_not_found"
    assert work_api._sse_failure(AssignmentError("private upstream detail", 503)) == "work_read_unavailable"
    assert work_api._sse_failure(ValueError("private")) == "work_read_unavailable"

    def raising(exc):
        def call():
            raise exc
        return call

    async def collect(poll, caps, bound):
        clock[0] = 1000.0
        polls, verifies = iter(poll), iter(caps)
        service = SimpleNamespace(poll=AsyncMock(side_effect=lambda *a, **k: next(polls)()))
        delivery = SimpleNamespace(verify=AsyncMock(side_effect=lambda *_: next(verifies)))
        frames = []
        async for frame in work_api._events(service, "owner", {"sub": "owner"}, delivery, "id", None, 2000.0, bound):
            frames.append(frame.decode())
            clock[0] += 0.05
        return frames

    def changed():
        return {"revision": 3, "changed": True, "resync_required": False, "operation": {"id": "x"}}

    def same():
        return {"revision": 3, "changed": False, "resync_required": False, "operation": None}

    assert await collect([changed], [999.0], 5000.0) == [
        'event: error\ndata: {"error": "work_authentication_required"}\n\n']
    frames = await collect([changed, same], [1000.04, 1000.08], 5000.0)
    assert frames[0].startswith("id: 3\nevent: revision\n") and '"changed": true' in frames[0]
    assert frames[1:] == ['id: 3\nevent: error\ndata: {"error": "work_authentication_required"}\n\n']
    frames = await collect([same, same], [2000.0, 2000.0], 1000.07)
    assert frames == [": tick\n\n", ": tick\n\n",
                      'event: end\ndata: {"reason": "work_stream_bounded", "revision": null}\n\n']
    assert await collect([raising(AssignmentError("private", 503))], [], 5000.0) == [
        'event: error\ndata: {"error": "work_read_unavailable"}\n\n']
    assert await collect([lambda: {"changed": "no revision key"}], [2000.0], 5000.0) == [
        'event: error\ndata: {"error": "work_read_unavailable"}\n\n']
    assert await collect([raising(RuntimeError("private driver detail"))], [], 5000.0) == [
        'event: error\ndata: {"error": "work_read_unavailable"}\n\n']
    frames = await collect([changed, raising(OSError("private socket detail"))], [2000.0], 5000.0)
    assert frames[0].startswith("id: 3\nevent: revision\n")
    assert frames[1:] == ['id: 3\nevent: error\ndata: {"error": "work_read_unavailable"}\n\n']


@pytest.mark.asyncio
async def test_sse_refuses_a_backing_retired_before_the_stream_starts(host, monkeypatch):
    orch, read, _ = host
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    override_read_auth(app, monkeypatch)
    original = WorkService.get

    async def retiring(self, owner_id, claims, identity):
        value = await original(self, owner_id, claims, identity)
        orch.persistent_assignments = AssignmentService(orch, store=self.store, enabled=True, phi_gate=object())
        return value

    monkeypatch.setattr(WorkService, "get", retiring)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/work/v1/operations/{read.assignment.assignment_id}/events")
    assert response.status_code == 503 and response.json() == {"error": "work_read_unavailable"}
