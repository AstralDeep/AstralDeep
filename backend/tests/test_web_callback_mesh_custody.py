"""Verifies browser callback issuance against signed Keycloak claims and real Plane storage.
The resulting browser cookie must authorize mesh confirmation without pre-created custody.
Only the identity provider HTTP and key-discovery boundaries are replaced.
"""

from __future__ import annotations

import base64
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
import httpx
from jose import jwt
import pytest

from audit.repository import AuditRepository
from orchestrator import mesh_api, web_auth
from orchestrator.plane_repository_context import ApplicationPlaneSource
from tests.helpers.session_plane_runtime import (
    get_session_record,
    isolated_plane_runtime,
    web_session_store,
)
from tests.test_request_session_authority_088 import signing_key as signing_key

REAL_CLIENT = httpx.AsyncClient
ISSUER = "https://browser-custody.invalid/realms/fixture"
CLIENT = "astral-frontend"
BASE = "https://mesh.invalid"


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("browser_mesh_custody") as value:
        yield value


@pytest.fixture
def browser(runtime, monkeypatch, signing_key):
    for name, value in {
        "ASTRAL_ENV": "production",
        "USE_MOCK_AUTH": "false",
        "KEYCLOAK_AUTHORITY": ISSUER,
        "KEYCLOAK_CLIENT_ID": CLIENT,
        "KEYCLOAK_CLIENT_SECRET": "synthetic-browser-secret",
        "KEYCLOAK_ALLOWED_AZP": CLIENT,
        "WEB_SESSION_ENC_KEY": Fernet.generate_key().decode(),
        "WEB_SESSION_SECRET": uuid.uuid4().hex,
        "PUBLIC_BASE_URL": BASE,
    }.items():
        monkeypatch.setenv(name, value)
    store = web_session_store(runtime)
    owner = "browser-owner-" + uuid.uuid4().hex
    wire = SimpleNamespace(changes={}, body=None, hook=None, requests=[], audits=[])

    def token(**changes):
        claims = {"sub": owner, "iss": ISSUER, "azp": CLIENT, "aud": "account",
                  "exp": int(time.time()) + 300, "realm_access": {"roles": ["user"]}}
        claims.update(changes)
        return jwt.encode(claims, signing_key[0], algorithm="RS256",
                          headers={"kid": "synthetic-request-authority"})

    async def keys(*args, **kwargs):
        return signing_key[1]

    async def network(request):
        wire.requests.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={})
        assert str(request.url) == ISSUER + "/protocol/openid-connect/token"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["client_id"] == [CLIENT]
        assert len(form["code_verifier"][0]) >= 43
        if wire.hook is not None:
            wire.hook()
        return httpx.Response(200, json=wire.body if wire.body is not None else {
            "access_token": token(**wire.changes), "refresh_token": "synthetic-browser-refresh"})

    async def audit(action, sub, description, *, outcome="success"):
        wire.audits.append((action, sub, outcome))

    monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(
        transport=httpx.MockTransport(network), **kwargs))
    monkeypatch.setattr(web_auth, "_audit", audit)
    monkeypatch.setattr(web_auth, "_STORE", store)
    monkeypatch.setattr(web_auth, "_SESSIONS", {})
    monkeypatch.setattr(web_auth, "_PENDING", {})
    monkeypatch.setattr(web_auth, "_IDP_OK_UNTIL", 0)
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(
        plane_repository_source=ApplicationPlaneSource(runtime, runtime.repositories),
        audit_repo=AuditRepository(plane_runtime=runtime, plane_repositories=runtime.repositories),
        web_sessions=store,
    )
    app.include_router(web_auth.web_auth_router)
    app.include_router(mesh_api.mesh_router)
    yield SimpleNamespace(app=app, store=store, owner=owner, wire=wire, token=token, runtime=runtime)
    store.delete_for_user(owner)


async def callback(client):
    login = await client.get("/auth/login", params={"next": "/enroll"})
    assert login.status_code == 307
    query = parse_qs(urlsplit(login.headers["location"]).query)
    return await client.get("/auth/callback", params={"code": "synthetic-code", "state": query["state"][0]})


async def confirmation(client):
    public = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = {"kty": "OKP", "crv": "Ed25519", "x": base64.urlsafe_b64encode(public).decode().rstrip("=")}
    headers = {"Origin": BASE}
    response = await client.post("/api/mesh/invitations", headers=headers, json={
        "label": "Synthetic browser enrollment", "device_key": key, "scopes": ["mesh:confirm", "tools:read"]})
    assert response.status_code == 200, response.text
    return await client.post("/api/mesh/invitations/" + response.json()["invitation"]["invite_id"] + "/confirm",
                             headers=headers)


def latest(browser):
    with browser.runtime.transaction() as transaction:
        return browser.runtime.repositories.history.sessions.get_latest_live_for_owner(
            transaction, owner_id=browser.owner, observed_at=int(time.time()))


@pytest.mark.asyncio
async def test_signed_browser_callback_creates_custody_for_owner_confirmation(browser):
    assert latest(browser) is None
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=browser.app), base_url=BASE) as client:
        response = await callback(client)
        assert response.status_code == 303
        sid = web_auth._unsign(client.cookies[web_auth.COOKIE_NAME])
        stored = get_session_record(browser.runtime, sid)
        assert (stored.owner_id, stored.issuing_issuer, stored.issuing_client_id) == (browser.owner, ISSUER, CLIENT)
        observed = browser.store.capture_execution_reference(owner_id=browser.owner, session_id=sid)
        assert observed.state.credential.incarnation_id == stored.incarnation_id
        confirmed = await confirmation(client)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["invitation"]["status"] == "confirmed"
    assert browser.wire.audits == [("login_interactive", browser.owner, "success")]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"iss": None}, {"iss": "https://foreign.invalid/realm"}, {"azp": None},
    {"azp": "foreign-client"}, {"sub": ""}, {"sub": None}, {"sub": "x" * 257},
    {"exp": None}, {"exp": True}, {"exp": float("inf")}, {"exp": 1},
])
async def test_callback_rejects_invalid_signed_issuing_claims(browser, changes):
    browser.wire.changes = changes
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=browser.app), base_url=BASE) as client:
        response = await callback(client)
        assert response.status_code == 401
        assert web_auth.COOKIE_NAME not in client.cookies
    assert latest(browser) is None and not web_auth._SESSIONS and not browser.wire.audits


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bad-signature", "missing-refresh", "malformed-body", "configuration-drift"])
async def test_callback_rejects_untrusted_exchange_before_issuance(browser, monkeypatch, mode):
    if mode == "bad-signature":
        header, payload, signature = browser.token().split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        browser.wire.body = {"access_token": ".".join((header, payload, signature)), "refresh_token": "refresh"}
    elif mode == "missing-refresh":
        browser.wire.body = {"access_token": browser.token()}
    elif mode == "malformed-body":
        browser.wire.body = []
    else:
        browser.wire.hook = lambda: monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "changed-client")
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=browser.app), base_url=BASE) as client:
        response = await callback(client)
        assert response.status_code == 401
        assert web_auth.COOKIE_NAME not in client.cookies
    assert latest(browser) is None and not web_auth._SESSIONS and not browser.wire.audits


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing-store", "write-failure", "before-write-drift", "after-write-drift", "store-rebound"])
async def test_callback_refuses_cookie_when_durable_custody_is_unavailable(browser, monkeypatch, mode):
    if mode == "missing-store":
        monkeypatch.setattr(web_auth, "_STORE", None)
    elif mode == "before-write-drift":
        establish = web_auth._establish_session

        def changed(*args):
            monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://changed.invalid/realm")
            return establish(*args)

        monkeypatch.setattr(web_auth, "_establish_session", changed)
    else:
        create = browser.store.create

        def unavailable(*args, **kwargs):
            if mode == "write-failure":
                raise TimeoutError("synthetic durable-store failure")
            row = create(*args, **kwargs)
            if mode == "after-write-drift":
                monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "changed-client")
            else:
                monkeypatch.setattr(web_auth, "_STORE", None)
            return row

        monkeypatch.setattr(browser.store, "create", unavailable)
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=browser.app), base_url=BASE) as client:
        response = await callback(client)
        assert response.status_code == 503
        assert web_auth.COOKIE_NAME not in client.cookies
    assert latest(browser) is None and not web_auth._SESSIONS and not browser.wire.audits


@pytest.mark.asyncio
async def test_legacy_unbound_browser_session_does_not_gain_mesh_custody(browser):
    sid = uuid.uuid4().hex
    browser.store.create(sid, user_id=browser.owner, access_token=browser.token(),
                         refresh_token="synthetic-legacy-refresh", hard_max_seconds=3600)
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=browser.app), base_url=BASE) as client:
        client.cookies.set(web_auth.COOKIE_NAME, web_auth._sign(sid))
        response = await confirmation(client)
        assert response.status_code == 403 and response.json()["detail"] == "mesh_admission_refused"
    stored = get_session_record(browser.runtime, sid)
    assert stored.issuing_issuer is None and stored.issuing_client_id is None
