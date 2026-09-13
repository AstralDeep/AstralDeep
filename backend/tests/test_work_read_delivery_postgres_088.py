"""Work delivery uses original normal JWT and real, uncached session state.

Only external JWKS/refresh replies are synthetic. A held public read reproduces
credential changes after authentication without changing the stored Work record.
"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from orchestrator import web_auth
from orchestrator.work_api import work_router
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_work_submit_postgres_088 import (
    command, context, fixture as fixture, runtime as runtime,
    service as service, signing_key as signing_key,
)


@pytest.fixture
async def read_host(service, fixture, runtime):
    accepted = await service.submit(await context(fixture, runtime), command())
    app = FastAPI()
    app.state.orchestrator = service.assignments.orch
    app.state.orchestrator.persistent_assignments = service.assignments
    app.include_router(work_router, prefix="/api")
    yield app, accepted.record.assignment_id
    service.assignments.store.close()


def read_headers(fixture, kind="cookie", **token_changes):
    if kind == "cookie":
        return {"cookie": "astral_session=" + web_auth._sign(fixture[2])}
    return {"authorization": "Bearer " + fixture[3](**token_changes)}


async def held_read(app, identity, service, monkeypatch, headers, during, *, endpoint="detail"):
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.assignments.store.transaction
    held = False

    async def transaction(callback, **kwargs):
        nonlocal held
        value = await original(callback, **kwargs)
        # Hold only the actual public operation projection, not later liveness reads.
        public = isinstance(value, dict) and value.get("id") == identity
        public |= isinstance(value, list) and any(row.get("id") == identity for row in value)
        if public and not held:
            held = True
            entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(service.assignments.store, "transaction", transaction)
    suffix = {"detail": "/" + identity, "poll": "/" + identity + "/poll", "list": ""}[endpoint]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        pending = asyncio.create_task(client.get("/api/work/v1/operations" + suffix, headers=headers))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await during()
            release.set()
            return await asyncio.wait_for(pending, 5)
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "owner_delete", "same_sid_replacement"])
async def test_session_changed_during_read_never_delivers_old_owner_data(
    read_host, service, fixture, runtime, monkeypatch, change,
):
    app, identity = read_host
    original = get_session_record(runtime, fixture[2])

    async def change_session():
        sessions = runtime.repositories.history.sessions
        if change in {"delete", "owner_delete"}:
            with runtime.transaction() as tx:
                if change == "delete":
                    sessions.delete(tx, owner_id=fixture[1], session_id=fixture[2],
                                    expected_incarnation_id=original.incarnation_id)
                else:
                    sessions.delete_owner(tx, owner_id=fixture[1])
        else:
            replace_session_record(runtime, original)

    response = await held_read(app, identity, service, monkeypatch, read_headers(fixture), change_session)
    assert response.status_code == 401
    assert response.json() == {"error": "work_authentication_required"}
    assert identity not in response.text and response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["detail", "poll", "list"])
async def test_original_bearer_expires_while_read_waits(read_host, service, fixture, monkeypatch, endpoint):
    app, identity = read_host
    expiry = int(time.time()) + 2

    async def expire():
        # Release against the actual signed credential's deadline, not a guessed delay.
        await asyncio.sleep(max(0, expiry + 0.05 - time.time()))

    response = await held_read(app, identity, service, monkeypatch,
                               read_headers(fixture, "bearer", exp=expiry), expire, endpoint=endpoint)
    assert response.status_code == 401
    assert identity not in response.text


@pytest.mark.asyncio
async def test_current_client_policy_revocation_during_read_refuses(read_host, service, fixture, monkeypatch):
    app, identity = read_host

    async def revoke():
        monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "")

    response = await held_read(app, identity, service, monkeypatch,
                               read_headers(fixture, "bearer", azp="astral-native"), revoke)
    assert response.status_code == 401 and identity not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cookie", "bearer"])
async def test_unchanged_current_read_preserves_existing_response(read_host, service, fixture, monkeypatch, kind):
    app, identity = read_host

    async def unchanged():
        pass

    response = await held_read(app, identity, service, monkeypatch, read_headers(fixture, kind), unchanged)
    assert response.status_code == 200 and response.json()["operation"]["id"] == identity
    assert "checkpoint" not in response.text and "authority" not in response.text


@pytest.mark.asyncio
async def test_original_cookie_hard_cap_expires_without_replacement(read_host, service, fixture, runtime, monkeypatch):
    app, identity = read_host
    sid = uuid4().hex
    fixture[0].create(sid, user_id=fixture[1], access_token=fixture[3](),
                      refresh_token="synthetic-read-cap", hard_max_seconds=2)
    expiry = get_session_record(runtime, sid).hard_expires_at

    async def expire():
        await asyncio.sleep(max(0, expiry + 0.05 - time.time()))

    try:
        response = await held_read(app, identity, service, monkeypatch,
            {"cookie": "astral_session=" + web_auth._sign(sid)}, expire)
        assert response.status_code == 401 and identity not in response.text
    finally:
        fixture[0].delete(sid)


@pytest.mark.asyncio
async def test_bearer_does_not_borrow_or_require_a_cookie_session(read_host, service, fixture, runtime, monkeypatch):
    app, identity = read_host

    async def delete_cookie():
        with runtime.transaction() as tx:
            runtime.repositories.history.sessions.delete_owner(tx, owner_id=fixture[1])

    headers = {**read_headers(fixture), **read_headers(fixture, "bearer")}
    response = await held_read(app, identity, service, monkeypatch, headers, delete_cookie)
    assert response.status_code == 200 and response.json()["operation"]["id"] == identity


@pytest.mark.asyncio
async def test_current_query_token_keeps_existing_read_compatibility(read_host, fixture):
    app, identity = read_host
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity, params={"token": fixture[3]()})
    assert response.status_code == 200 and response.json()["operation"]["id"] == identity


@pytest.mark.asyncio
async def test_cookie_deleted_during_final_jwt_wait_is_refused(read_host, service, fixture, runtime, monkeypatch):
    from shared import jwks_cache
    app, identity = read_host
    original = jwks_cache.get_jwks
    calls = 0

    async def keys(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await original(*args, **kwargs)
        if calls == 2:
            with runtime.transaction() as tx:
                runtime.repositories.history.sessions.delete_owner(tx, owner_id=fixture[1])
        return result

    monkeypatch.setattr(jwks_cache, "get_jwks", keys)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity, headers=read_headers(fixture))
    assert response.status_code == 401 and identity not in response.text


@pytest.mark.asyncio
async def test_mutable_incoming_request_cannot_switch_private_original_credential(read_host, fixture, monkeypatch):
    from shared import jwks_cache
    app, identity = read_host
    original = jwks_cache.get_jwks
    incoming = {}

    async def capture(scope, receive, send):
        incoming["scope"] = scope
        await app(scope, receive, send)

    async def keys(*args, **kwargs):
        result = await original(*args, **kwargs)
        # A delayed middleware still owns the original ASGI scope. It cannot
        # rewrite the authenticated token or cookie issuance during JWT's await.
        incoming["scope"].setdefault("state", {})["delegation_subject_token"] = fixture[3](sub="other-owner")
        incoming["scope"]["headers"] = [(b"authorization", b"Bearer invalid")]
        return result

    monkeypatch.setattr(jwks_cache, "get_jwks", keys)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=capture), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity, headers=read_headers(fixture))
    assert response.status_code == 200 and response.json()["operation"]["id"] == identity


@pytest.mark.asyncio
async def test_normal_auth_without_private_token_handoff_cannot_deliver(read_host, fixture, monkeypatch):
    from orchestrator import auth
    app, identity = read_host
    original = auth.get_current_user_payload

    async def missing(request, credentials):
        claims = await original(request, credentials)
        del request.state.delegation_subject_token
        return claims

    monkeypatch.setattr(auth, "get_current_user_payload", missing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity, headers=read_headers(fixture, "bearer"))
    assert response.status_code == 401 and identity not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [True, str(int(time.time()) + 3600), 10**400])
async def test_nonfinite_or_non_numeric_principal_expiry_is_closed(read_host, fixture, expiry):
    app, identity = read_host
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity,
                                    headers=read_headers(fixture, "bearer", exp=expiry))
    assert response.status_code == 401 and identity not in response.text


@pytest.mark.asyncio
async def test_read_retains_existing_authenticated_http_audit(read_host, service, fixture, monkeypatch, tmp_path):
    from audit import hooks, middleware
    from audit.recorder import Recorder
    app, identity = read_host
    recorder = Recorder(service.audit, retry_queue=tmp_path / "audit-retry.jsonl")
    monkeypatch.setattr(hooks, "get_recorder", lambda: recorder)
    monkeypatch.setattr(middleware, "get_recorder", lambda: recorder)
    app.add_middleware(middleware.AuditHTTPMiddleware)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
            response = await client.get("/api/work/v1/operations/" + identity,
                                        headers=read_headers(fixture, "bearer"))
        assert response.status_code == 200
        rows, _ = service.audit.list_for_user(fixture[1])
        recorded = [row for row in rows if row.action_type == "http.get"]
        assert len(recorded) == 1
        assert service.audit.list_for_user(str(uuid4()))[0] == []
        assert service.audit.verify_chain(fixture[1]) is None
    finally:
        await recorder.close()
    assert not (tmp_path / "audit-retry.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["backing", "feature"])
async def test_current_service_retirement_during_read_refuses(read_host, service, fixture, monkeypatch, change):
    from types import SimpleNamespace
    app, identity = read_host

    async def retire():
        if change == "backing":
            app.state.orchestrator.persistent_assignments = SimpleNamespace(store=service.assignments.store)
        else:
            service.assignments.enabled = False

    response = await held_read(app, identity, service, monkeypatch, read_headers(fixture, "bearer"), retire)
    assert response.status_code == 503 and identity not in response.text


@pytest.mark.asyncio
async def test_cancelled_held_read_delivers_nothing_and_leaves_operation_unchanged(
    read_host, service, fixture, runtime, monkeypatch,
):
    app, identity = read_host
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.assignments.store.transaction
    with runtime.transaction() as tx:
        before = runtime.repositories.assignments.get_operation(tx, owner_id=fixture[1], assignment_id=identity)

    async def hold(callback, **kwargs):
        value = await original(callback, **kwargs)
        entered.set()
        await release.wait()
        return value

    monkeypatch.setattr(service.assignments.store, "transaction", hold)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        pending = asyncio.create_task(client.get("/api/work/v1/operations/" + identity,
                                                headers=read_headers(fixture, "bearer")))
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
    with runtime.transaction() as tx:
        after = runtime.repositories.assignments.get_operation(tx, owner_id=fixture[1], assignment_id=identity)
    assert after == before


@pytest.mark.asyncio
async def test_original_principal_expiry_after_final_session_read_still_refuses(
    read_host, service, fixture, monkeypatch,
):
    app, identity = read_host
    expiry = int(time.time()) + 3
    fixture[0].update_tokens(fixture[2], access_token=fixture[3](exp=expiry),
                             refresh_token="synthetic-short-read-refresh")

    async def exchange(*_):
        return {"access_token": fixture[3](exp=expiry), "refresh_token": "synthetic-short-read-rotated"}

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    original = service.assignments.store.transaction
    final_read = False

    async def hold(callback, **kwargs):
        nonlocal final_read
        result = await original(callback, **kwargs)
        if kwargs.get("bound_session_waits"):
            final_read = True
            await asyncio.sleep(max(0, expiry + 0.05 - time.time()))
        return result

    monkeypatch.setattr(service.assignments.store, "transaction", hold)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity, headers=read_headers(fixture))
    assert final_read and response.status_code == 401 and identity not in response.text


@pytest.mark.asyncio
async def test_same_issuance_rotation_does_not_substitute_original_verified_token(
    read_host, service, fixture, monkeypatch,
):
    app, identity = read_host

    async def rotate():
        fixture[0].update_tokens(fixture[2], access_token=fixture[3](jti="next-generation"),
                                refresh_token="synthetic-next-read-refresh")

    response = await held_read(app, identity, service, monkeypatch, read_headers(fixture), rotate)
    assert response.status_code == 200 and response.json()["operation"]["id"] == identity


@pytest.mark.asyncio
async def test_cookie_iam_without_durable_issuance_handoff_refuses(read_host, fixture, monkeypatch):
    app, identity = read_host
    original = web_auth.ensure_session

    async def incomplete(request):
        result = await original(request)
        return {**result, "incarnation_id": None}

    monkeypatch.setattr(web_auth, "ensure_session", incomplete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity, headers=read_headers(fixture))
    assert response.status_code == 401 and identity not in response.text


@pytest.mark.asyncio
async def test_final_database_clock_caps_original_principal_even_when_host_clock_is_behind(
    read_host, fixture, runtime, monkeypatch,
):
    from jose import jwt
    app, identity = read_host
    token = (await asyncio.to_thread(fixture[0].get, fixture[2]))["access_token"]
    expiry = jwt.get_unverified_claims(token)["exp"]  # Fixture clock input, never authority.
    assert time.time() < expiry
    sessions = runtime.repositories.history.sessions
    original = sessions.get_execution_state

    def observed(query, **kwargs):
        actual = original(query, **kwargs)
        assert actual is not None and actual.credential.hard_expires_at > expiry + 1
        # Keep the actual PG identity/fence and vary only the reported clock;
        # neither the system clock nor the database clock/config is changed.
        return replace(actual, observed_at=datetime.fromtimestamp(expiry + 1, UTC))

    monkeypatch.setattr(sessions, "get_execution_state", observed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.invalid") as client:
        response = await client.get("/api/work/v1/operations/" + identity, headers=read_headers(fixture))
    assert response.status_code == 401 and identity not in response.text
