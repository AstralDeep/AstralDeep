"""Feature 044 (FR-005/SC-004) — POST /api/auth/logout: native sign-out parity.

Covers: allowlist validation, revoked/queued outcomes with the originating
client_id, the public-client revocation payload (no secret for native client
ids), the retrier honoring the stored client_id, and Deep's use of Plane's
nullable client-id revocation contract.
"""
import asyncio
import json
from pathlib import Path

import pytest
from astralplane.repositories.revocations import RevocationQueueRecord
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import web_auth
from orchestrator.auth import auth_router, get_current_user_payload


REPO_ROOT = Path(__file__).resolve().parents[2]


def _swift_function(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated Swift function: {signature}")


def _client(monkeypatch, payload=None):
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop,astral-mobile")
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_current_user_payload] = lambda: (
        payload or {"sub": "u-044", "preferred_username": "u-044"})
    return TestClient(app)


def test_logout_revokes_with_originating_client_id(monkeypatch):
    calls = {}

    async def fake_revoke(refresh_token, client_id=None):
        calls["args"] = (refresh_token, client_id)
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", fake_revoke)
    c = _client(monkeypatch)
    r = c.post("/api/auth/logout",
               json={"refresh_token": "rt-1", "client_id": "astral-desktop"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "revoked" and r.json()["revoked"] is True
    assert calls["args"] == ("rt-1", "astral-desktop")


def test_logout_queues_when_idp_unreachable(monkeypatch):
    async def fake_revoke(refresh_token, client_id=None):
        return False

    enq = {}

    class FakeStore:
        async def aenqueue_revocation(self, user_id, refresh_token, client_id=None):
            """Async twin mirroring WebSessionStore's event-loop-safe facade."""
            enq.update(user_id=user_id, refresh_token=refresh_token, client_id=client_id)

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", fake_revoke)
    monkeypatch.setattr(web_auth, "_get_store", lambda: FakeStore())
    c = _client(monkeypatch)
    r = c.post("/api/auth/logout",
               json={"refresh_token": "rt-2", "client_id": "astral-mobile"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "queued" and r.json()["queued"] is True
    assert enq == {"user_id": "u-044", "refresh_token": "rt-2", "client_id": "astral-mobile"}


@pytest.mark.parametrize("body", [
    {},                                                       # nothing
    {"refresh_token": "rt"},                                  # no client_id
    {"refresh_token": "rt", "client_id": "evil-client"},      # not allow-listed
    {"client_id": "astral-desktop"},                          # no refresh token
])
def test_logout_rejects_bad_bodies(monkeypatch, body):
    c = _client(monkeypatch)
    assert c.post("/api/auth/logout", json=body).status_code == 400


def test_logout_refuses_the_confidential_web_client(monkeypatch):
    """Security: the native endpoint must NOT accept the web client id — that
    would apply the server's confidential secret to a caller-supplied token
    (a revocation oracle). The web app uses the cookie-bound /auth/logout."""
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")

    called = {"revoke": False}

    async def fake_revoke(refresh_token, client_id=None):
        called["revoke"] = True
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", fake_revoke)
    c = _client(monkeypatch)  # sets KEYCLOAK_ALLOWED_AZP=astral-desktop,astral-mobile
    r = c.post("/api/auth/logout",
               json={"refresh_token": "victim-web-rt", "client_id": "astral-frontend"})
    assert r.status_code == 400
    assert called["revoke"] is False  # never reached the secret-backed revoke


def test_revocation_post_omits_secret_for_native_public_clients(monkeypatch):
    """Keycloak public clients (astral-desktop/mobile) must not receive the web
    client's secret; the web client keeps sending it."""
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://kc.example/realms/Astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "s3cr3t")

    posts = []

    class FakeResp:
        status_code = 200

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            posts.append((url, dict(data or {})))
            return FakeResp()

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", FakeAsyncClient)
    loop = asyncio.new_event_loop()

    assert loop.run_until_complete(
        web_auth._revoke_refresh_token("rt", client_id="astral-desktop")) is True
    assert posts[-1][1]["client_id"] == "astral-desktop"
    assert "client_secret" not in posts[-1][1]

    assert loop.run_until_complete(web_auth._revoke_refresh_token("rt")) is True
    assert posts[-1][1]["client_id"] == "astral-frontend"
    assert posts[-1][1]["client_secret"] == "s3cr3t"


def test_retrier_uses_stored_client_id(monkeypatch):
    seen = []

    async def fake_revoke(refresh_token, client_id=None):
        seen.append((refresh_token, client_id))
        return True

    class FakeStore:
        """Mirrors WebSessionStore's async facade (the retrier's contract)."""

        async def apending_revocations(self, limit=20):
            return [
                {"id": 1, "user_id": "u", "refresh_token": "rt-native",
                 "attempts": 0, "enqueued_at": 0, "client_id": "astral-mobile"},
                {"id": 2, "user_id": "u", "refresh_token": "rt-web",
                 "attempts": 0, "enqueued_at": 0, "client_id": None},
            ]

        async def aresolve_revocation(self, qid):
            pass

        async def abump_revocation_attempt(self, qid):
            pass

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", fake_revoke)
    monkeypatch.setattr(web_auth, "_get_store", lambda: FakeStore())
    resolved = asyncio.new_event_loop().run_until_complete(
        web_auth.process_revocation_queue_once())
    assert resolved == 2
    assert ("rt-native", "astral-mobile") in seen
    assert ("rt-web", None) in seen  # NULL → falls back to the web client id


def test_plane_revocation_contract_keeps_client_id_nullable():
    """Pre-044 rows remain representable without a client identity."""

    legacy = RevocationQueueRecord(
        queue_id=1,
        owner_id="owner",
        refresh_token_ciphertext="ciphertext",
        client_id=None,
    )
    native = RevocationQueueRecord(
        queue_id=2,
        owner_id="owner",
        refresh_token_ciphertext="ciphertext",
        client_id="astral-mobile",
    )

    assert legacy.client_id is None
    assert native.client_id == "astral-mobile"


def test_client_local_manifest_untouched():
    """The endpoint is REST — the WS accept_actions manifest must not grow."""
    manifest = json.loads(
        (
            REPO_ROOT
            / "components"
            / "AstralProjection"
            / "contracts"
            / "ui_protocol.json"
        ).read_text(encoding="utf-8")
    )
    assert "native_logout" not in manifest["accept_actions"]


@pytest.mark.skipif(
    not (
        REPO_ROOT / "components" / "AstralProjection" / "apple-clients"
    ).is_dir(),  # composition source absent inside the product image
    reason="repo-root tooling files are not part of the product image",
)
@pytest.mark.parametrize(
    "relative_path",
    [
        "components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift",
        "components/AstralProjection/apple-clients/AstralWatch/WatchModel.swift",
    ],
)
def test_apple_sign_out_wipes_local_credentials_before_any_network_await(
    relative_path,
):
    """A killed or frozen revocation request cannot restore the prior account."""

    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    body = _swift_function(source, "func signOut(revokeRemote: Bool = true) async")

    wipe = body.index("store.wipe()")
    token_clear = body.index("tokens = nil")
    first_await = body.index("await ")
    remote_logout = body.index("logoutClient.logout")
    assert "let logoutClient = RestClient(serverBase: serverBase) { access }" in body
    assert wipe < first_await < remote_logout
    assert token_clear < first_await
    assert "refreshTask?.cancel()" in body
    assert "wsTask?.cancel()" in body
