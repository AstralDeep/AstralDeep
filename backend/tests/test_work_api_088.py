"""Owner-only operation reads; no admission, control or opaque payload surface."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.auth import get_web_or_bearer_user_payload
from orchestrator.work_api import work_router
from orchestrator.work_service import WorkService
from persistent_agents.models import AssignmentError
from persistent_agents.service import AssignmentService


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
    async def transaction(callback):
        return callback(object(), repository)
    store = SimpleNamespace(repository=repository, transaction=AsyncMock(side_effect=transaction))
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
async def test_http_poll_reauthenticates_every_request_and_exposes_no_mutations(host):
    orch, read, repo = host
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    calls = []
    async def authenticate():
        from fastapi import HTTPException
        calls.append(True)
        if len(calls) > 1:
            raise HTTPException(401, "private token expired")
        return {"sub": "owner", "realm_access": {"roles": ["user"]}}
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
async def test_http_rejects_noncanonical_integer_queries_and_registers_exact_prefix(host):
    from orchestrator.api import operation_router
    orch, read, _ = host
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(operation_router)
    app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: {
        "sub": "owner", "realm_access": {"roles": ["user"]}}
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
        assert decode.call_count == 3 and repo.get_operation.call_count == 1
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
        app.dependency_overrides[get_web_or_bearer_user_payload] = lambda: {
            "sub": "owner", "realm_access": {"roles": ["admin"]}}
        del app.state.orchestrator
        response = await client.get("/api/work/v1/operations")
        assert response.status_code == 503 and response.json() == {"error": "work_read_unavailable"}
        # Mounted apps resolve the existing root orchestrator exactly as other APIs.
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
    session = AsyncMock(side_effect=[{"access_token": "fixture.access.token"}, None])
    monkeypatch.setattr(web_auth, "ensure_session", session)
    validate = AsyncMock(return_value={"sub": "owner", "realm_access": {"roles": ["user"]}})
    monkeypatch.setattr(auth, "get_current_user_payload", validate)
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
