"""POST /auth/token — the BFF proxy that injects the confidential client_secret.

The endpoint takes NO caller credential (the Windows client in legacy BFF mode
posts a bare form), so it is constrained instead: a grant allow-list, a field
allow-list, a server-pinned client_id and a per-IP rate limit. Without those it
is a free oracle that speaks AS the confidential client — including the
token-exchange grant DelegationService uses with the same secret.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import auth as auth_mod  # noqa: E402

AUTHORITY = "https://idp.example/realms/astral"
TOKEN_URL = f"{AUTHORITY}/protocol/openid-connect/token"


class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stands in for aiohttp.ClientSession; records the upstream form."""

    captured: dict = {}
    status = 200
    body = {"access_token": "at", "refresh_token": "rt"}

    def post(self, url, data=None):
        type(self).captured = {"url": url, "data": dict(data or {})}
        return _FakeResponse(self.status, self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", AUTHORITY)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "s3cr3t")
    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    _FakeSession.captured = {}
    auth_mod.reset_token_proxy_state()

    app = FastAPI()
    app.include_router(auth_mod.auth_router)
    with TestClient(app) as c:
        yield c
    auth_mod.reset_token_proxy_state()


# ---------------------------------------------------------------------------
# The two live grants keep working (Windows BFF client)
# ---------------------------------------------------------------------------

def test_authorization_code_grant_proxies_through(client):
    res = client.post("/auth/token", data={
        "grant_type": "authorization_code",
        "code": "abc",
        "redirect_uri": "http://127.0.0.1:5321/callback",
        "client_id": "astral-frontend",
        "code_verifier": "verifier",
    })
    assert res.status_code == 200, res.text
    assert res.json()["access_token"] == "at"
    assert _FakeSession.captured["url"] == TOKEN_URL
    sent = _FakeSession.captured["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "abc"
    assert sent["code_verifier"] == "verifier"
    assert sent["redirect_uri"] == "http://127.0.0.1:5321/callback"
    assert sent["client_id"] == "astral-frontend"
    assert sent["client_secret"] == "s3cr3t"


def test_refresh_token_grant_proxies_through(client):
    res = client.post("/auth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": "rt-1",
        "client_id": "astral-frontend",
    })
    assert res.status_code == 200, res.text
    sent = _FakeSession.captured["data"]
    assert sent == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-1",
        "client_id": "astral-frontend",
        "client_secret": "s3cr3t",
    }


# ---------------------------------------------------------------------------
# Grant allow-list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grant_type", [
    "urn:ietf:params:oauth:grant-type:token-exchange",
    "password",
    "client_credentials",
    "",
])
def test_refused_grant_types_never_reach_keycloak(client, grant_type):
    res = client.post("/auth/token", data={
        "grant_type": grant_type,
        "subject_token": "stolen",
        "username": "victim",
        "password": "hunter2",
    })
    assert res.status_code == 400
    assert res.json()["error"] == "unsupported_grant_type"
    assert _FakeSession.captured == {}, "refused request must not be forwarded"


def test_missing_grant_type_refused(client):
    res = client.post("/auth/token", data={"code": "abc"})
    assert res.status_code == 400
    assert _FakeSession.captured == {}


# ---------------------------------------------------------------------------
# Field allow-list + client_id pinning
# ---------------------------------------------------------------------------

def test_smuggled_fields_are_dropped(client):
    res = client.post("/auth/token", data={
        "grant_type": "authorization_code",
        "code": "abc",
        "subject_token": "stolen",
        "audience": "astral-agent-service",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "username": "victim",
        "password": "hunter2",
        "client_secret": "attacker-supplied",
    })
    assert res.status_code == 200
    sent = _FakeSession.captured["data"]
    for smuggled in ("subject_token", "audience", "requested_token_type",
                     "username", "password"):
        assert smuggled not in sent
    assert sent["client_secret"] == "s3cr3t", "caller cannot override the secret"


def test_caller_client_id_is_overridden_by_the_server(client):
    res = client.post("/auth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": "rt-1",
        "client_id": "astral-agent-service",
    })
    assert res.status_code == 200
    assert _FakeSession.captured["data"]["client_id"] == "astral-frontend"


def test_client_id_pinned_when_caller_omits_it(client):
    res = client.post("/auth/token", data={
        "grant_type": "refresh_token", "refresh_token": "rt-1"})
    assert res.status_code == 200
    assert _FakeSession.captured["data"]["client_id"] == "astral-frontend"


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

def test_per_ip_rate_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(auth_mod, "_TOKEN_MAX_PER_WINDOW", 2)
    body = {"grant_type": "refresh_token", "refresh_token": "rt-1"}
    assert client.post("/auth/token", data=body).status_code == 200
    assert client.post("/auth/token", data=body).status_code == 200

    _FakeSession.captured = {}
    res = client.post("/auth/token", data=body)
    assert res.status_code == 429
    assert res.json()["error"] == "rate_limited"
    assert _FakeSession.captured == {}, "rate-limited request must not be forwarded"


def test_rate_limit_counts_refused_grants_too(client, monkeypatch):
    """The limiter runs BEFORE the grant check, so a probe loop cannot spin
    against the allow-list for free."""
    monkeypatch.setattr(auth_mod, "_TOKEN_MAX_PER_WINDOW", 1)
    assert client.post("/auth/token", data={"grant_type": "password"}).status_code == 400
    assert client.post("/auth/token", data={
        "grant_type": "refresh_token", "refresh_token": "rt"}).status_code == 429


# ---------------------------------------------------------------------------
# Refusals are audited (the HTTP audit middleware skips every /auth/ path)
# ---------------------------------------------------------------------------

def test_refusals_are_audited(client, monkeypatch):
    recorded = []

    async def _record(*, claims, action, description, outcome="success", **kw):
        recorded.append((action, outcome))

    monkeypatch.setattr("audit.hooks.record_auth_event", _record)
    monkeypatch.setattr(auth_mod, "_TOKEN_MAX_PER_WINDOW", 1)

    client.post("/auth/token", data={"grant_type": "client_credentials"})
    client.post("/auth/token", data={"grant_type": "refresh_token",
                                     "refresh_token": "rt"})
    assert recorded == [
        ("token_proxy_grant_refused", "failure"),
        ("token_proxy_rate_limited", "failure"),
    ]


def test_successful_proxy_is_not_audited(client, monkeypatch):
    """Only refusals are recorded — a successful refresh would otherwise write
    an auth row on every desktop token renewal."""
    recorded = []

    async def _record(**kw):
        recorded.append(kw.get("action"))

    monkeypatch.setattr("audit.hooks.record_auth_event", _record)
    client.post("/auth/token", data={"grant_type": "refresh_token",
                                     "refresh_token": "rt"})
    assert recorded == []


# ---------------------------------------------------------------------------
# Unconfigured backend keeps its 500 shape
# ---------------------------------------------------------------------------

def test_unconfigured_backend_returns_500_shape(client, monkeypatch):
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
    res = client.post("/auth/token", data={
        "grant_type": "refresh_token", "refresh_token": "rt"})
    assert res.status_code == 500
    assert res.json()["error"] == "server_error"
