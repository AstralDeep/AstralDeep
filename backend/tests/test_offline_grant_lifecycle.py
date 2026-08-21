"""EC-3 — real offline-grant lifecycle (features 025/028), no OfflineGrantStore mocks.

Exercises ``orchestrator.offline_grant.OfflineGrantStore`` against an isolated
current Plane PostgreSQL runtime: capture (Fernet encryption at rest,
fail-closed without a key),
revoke_for_user, and the per-run ``mint_access_token`` gate — which must refuse
revoked / expired / unknown grants BEFORE any Keycloak HTTP call. Also proves
``web_auth.auth_logout`` revokes the signing-out user's grants through the REAL
store (only the Keycloak revoke HTTP is monkeypatched).

Every row is keyed by a uuid4 user_id and the database is dropped after the
module.
"""
import asyncio
import json
import secrets
import uuid

import pytest
from cryptography.fernet import Fernet

from orchestrator import offline_grant as og
from orchestrator import web_auth
from orchestrator.offline_grant import OfflineGrantError, OfflineGrantStore
from tests.helpers.session_plane_runtime import (
    isolated_plane_runtime,
    purge_revocations,
    web_session_store,
)

_DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, cookies=None, query_params=None, base_url="http://localhost:8001/"):
        self.cookies = cookies or {}
        self.query_params = query_params or {}
        self.base_url = base_url


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("offline_grant") as runtime:
        yield runtime


@pytest.fixture()
def fernet_key(monkeypatch):
    """A fresh Fernet key, visible both via env and via the module-level
    constant that offline_grant imported from agentic_settings at load time."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("OFFLINE_GRANT_ENC_KEY", key)
    monkeypatch.setattr(og, "OFFLINE_GRANT_ENC_KEY", key)
    return key


@pytest.fixture()
def grant_store(plane_runtime, fernet_key):
    return OfflineGrantStore(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


def _grant_record(plane_runtime, user_id, grant_id):
    with plane_runtime.transaction() as transaction:
        return plane_runtime.repositories.offline_grants.get_grant(
            transaction,
            owner_id=user_id,
            grant_id=grant_id,
        )


def _has_live_grant(plane_runtime, user_id):
    with plane_runtime.transaction() as transaction:
        return (
            plane_runtime.repositories.offline_grants.find_latest_valid(
                transaction,
                owner_id=user_id,
                as_of=og._now_ms(),
            )
            is not None
        )


def _revoke_grants(plane_runtime, *user_ids):
    with plane_runtime.transaction() as transaction:
        for user_id in user_ids:
            plane_runtime.repositories.offline_grants.revoke_owner(
                transaction,
                owner_id=user_id,
                revoked_at=og._now_ms(),
            )


def _install_exploding_idp(monkeypatch):
    """Any instantiation of aiohttp.ClientSession fails the test outright —
    refusal paths must trip BEFORE the IdP is contacted."""
    calls = []

    class _ExplodingClientSession:
        def __init__(self, *a, **kw):
            calls.append("ClientSession")
            raise AssertionError(
                "Keycloak HTTP was reached — mint must refuse before any IdP call"
            )

    monkeypatch.setattr(og.aiohttp, "ClientSession", _ExplodingClientSession)
    return calls


def _install_fake_idp(monkeypatch, status=200, payload=None):
    """Replace aiohttp.ClientSession with a capture-only fake token endpoint."""
    captured = []
    payload = payload or {}

    class _FakeResp:
        def __init__(self):
            self.status = status

        async def text(self):
            return json.dumps(payload)

        async def json(self):
            return payload

    class _FakePostCM:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *exc):
            return False

    class _FakeClientSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, data=None):
            captured.append((url, dict(data or {})))
            return _FakePostCM()

    monkeypatch.setattr(og.aiohttp, "ClientSession", _FakeClientSession)
    return captured


# ---------------------------------------------------------------------------
# capture — encryption at rest, fail-closed posture
# ---------------------------------------------------------------------------

def test_capture_persists_encrypted_grant(
    plane_runtime, grant_store, fernet_key
):
    """025 FR-022: capture stores the refresh token Fernet-encrypted with a
    365-day expiry; the plaintext never lands in the row."""
    user_id = f"u-{uuid.uuid4()}"
    plaintext = f"offline-rt-{uuid.uuid4()}"
    try:
        grant_id = grant_store.capture(user_id, plaintext, agent_id="grants")
        row = _grant_record(plane_runtime, user_id, grant_id)
        assert row is not None
        assert row.grant_id == grant_id
        assert row.agent_id == "grants"
        assert row.revoked_at is None

        enc = row.encrypted_refresh_token
        assert plaintext.encode() not in enc                      # not plaintext at rest
        assert Fernet(fernet_key.encode()).decrypt(enc).decode() == plaintext

        cap_days = og.OFFLINE_GRANT_MAX_DAYS
        assert row.expires_at - row.issued_at == cap_days * _DAY_MS
        assert grant_store.is_valid(grant_id, user_id=user_id) is True
    finally:
        _revoke_grants(plane_runtime, user_id)


def test_capture_fails_closed_without_key(plane_runtime, monkeypatch):
    """025: with OFFLINE_GRANT_ENC_KEY unset, capture refuses — it never falls
    back to plaintext storage."""
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    monkeypatch.setattr(og, "OFFLINE_GRANT_ENC_KEY", None)
    user_id = f"u-{uuid.uuid4()}"
    try:
        with pytest.raises(OfflineGrantError, match="OFFLINE_GRANT_ENC_KEY"):
            OfflineGrantStore(
                plane_runtime=plane_runtime,
                plane_repositories=plane_runtime.repositories,
            ).capture(user_id, "rt-should-never-store")
        assert _has_live_grant(plane_runtime, user_id) is False
    finally:
        _revoke_grants(plane_runtime, user_id)


def test_capture_rejects_empty_refresh_token(plane_runtime, grant_store):
    """025: a session without offline_access yields no refresh token — refuse."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        with pytest.raises(OfflineGrantError, match="no refresh token"):
            grant_store.capture(user_id, "")
        assert _has_live_grant(plane_runtime, user_id) is False
    finally:
        _revoke_grants(plane_runtime, user_id)


# ---------------------------------------------------------------------------
# revoke_for_user
# ---------------------------------------------------------------------------

def test_revoke_for_user_sets_revoked_at_and_returns_count(
    plane_runtime, grant_store
):
    """EC-3: revoke_for_user returns the number of live grants revoked and
    stamps revoked_at; a second call is a no-op (already revoked)."""
    user_id = f"u-{uuid.uuid4()}"
    try:
        grant_id = grant_store.capture(user_id, f"rt-{uuid.uuid4()}")

        assert grant_store.revoke_for_user(user_id) == 1
        row = _grant_record(plane_runtime, user_id, grant_id)
        assert row is not None and row.revoked_at is not None
        assert row.revoked_at > 0
        assert grant_store.is_valid(grant_id, user_id=user_id) is False

        assert grant_store.revoke_for_user(user_id) == 0  # idempotent
    finally:
        _revoke_grants(plane_runtime, user_id)


# ---------------------------------------------------------------------------
# mint_access_token — refusal BEFORE any IdP contact (FR-024)
# ---------------------------------------------------------------------------

def test_mint_refuses_revoked_grant_before_any_idp_call(
    plane_runtime, grant_store, monkeypatch
):
    """025 FR-024: a revoked grant is refused locally — Keycloak is never hit."""
    user_id = f"u-{uuid.uuid4()}"
    calls = _install_exploding_idp(monkeypatch)
    try:
        grant_id = grant_store.capture(user_id, f"rt-{uuid.uuid4()}")
        assert grant_store.revoke_for_user(user_id) == 1

        with pytest.raises(OfflineGrantError, match="revoked"):
            asyncio.run(grant_store.mint_access_token(grant_id, user_id=user_id))
        assert calls == []
    finally:
        _revoke_grants(plane_runtime, user_id)


def test_mint_refuses_expired_grant_before_any_idp_call(
    plane_runtime, grant_store, fernet_key, monkeypatch
):
    """025 FR-024: past the 365-day cap, mint refuses without contacting the IdP."""
    user_id = f"u-{uuid.uuid4()}"
    calls = _install_exploding_idp(monkeypatch)
    try:
        grant_id = str(uuid.uuid4())
        now = og._now_ms()
        ciphertext = Fernet(fernet_key.encode()).encrypt(
            f"rt-{uuid.uuid4()}".encode()
        )
        with plane_runtime.transaction() as transaction:
            plane_runtime.repositories.offline_grants.create_grant(
                transaction,
                grant_id=grant_id,
                owner_id=user_id,
                agent_id=None,
                encrypted_refresh_token=ciphertext,
                issued_at=now - 2,
                expires_at=now - 1,
            )
        assert grant_store.is_valid(grant_id, user_id=user_id) is False

        with pytest.raises(OfflineGrantError, match="expired"):
            asyncio.run(grant_store.mint_access_token(grant_id, user_id=user_id))
        assert calls == []
    finally:
        _revoke_grants(plane_runtime, user_id)


def test_mint_refuses_unknown_grant_before_any_idp_call(
    plane_runtime, fernet_key, monkeypatch
):
    """025: a nonexistent grant id is refused locally."""
    calls = _install_exploding_idp(monkeypatch)
    store = OfflineGrantStore(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    with pytest.raises(OfflineGrantError, match="not found"):
        asyncio.run(store.mint_access_token(str(uuid.uuid4()), user_id="missing-user"))
    assert calls == []
    assert store.is_valid(str(uuid.uuid4()), user_id="missing-user") is False


# ---------------------------------------------------------------------------
# mint_access_token — exchange path (HTTP boundary faked, store real)
# ---------------------------------------------------------------------------

def test_mint_happy_path_round_trips_refresh_token(
    plane_runtime, grant_store, monkeypatch
):
    """025: a live grant decrypts back to the original refresh token and posts
    a grant_type=refresh_token exchange; the fresh access token is returned."""
    user_id = f"u-{uuid.uuid4()}"
    plaintext = f"rt-{uuid.uuid4()}"
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "http://keycloak.test/token")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-test-client")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
    captured = _install_fake_idp(
        monkeypatch, status=200, payload={"access_token": "fresh-at-123"}
    )
    try:
        grant_id = grant_store.capture(user_id, plaintext)
        token = asyncio.run(
            grant_store.mint_access_token(grant_id, user_id=user_id)
        )
        assert token == "fresh-at-123"

        assert len(captured) == 1
        url, data = captured[0]
        assert url == "http://keycloak.test/token"
        assert data["grant_type"] == "refresh_token"
        assert data["client_id"] == "astral-test-client"
        assert data["refresh_token"] == plaintext        # Fernet round-trip
        assert "client_secret" not in data
    finally:
        _revoke_grants(plane_runtime, user_id)


def test_mint_fails_safe_on_idp_rejection(
    plane_runtime, grant_store, monkeypatch
):
    """025 FR-024: Keycloak-side revocation (non-200 exchange) fails the run safe."""
    user_id = f"u-{uuid.uuid4()}"
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "http://keycloak.test/token")
    _install_fake_idp(monkeypatch, status=401, payload={"error": "invalid_grant"})
    try:
        grant_id = grant_store.capture(user_id, f"rt-{uuid.uuid4()}")
        with pytest.raises(OfflineGrantError, match="refresh exchange failed"):
            asyncio.run(grant_store.mint_access_token(grant_id, user_id=user_id))
    finally:
        _revoke_grants(plane_runtime, user_id)


# ---------------------------------------------------------------------------
# auth_logout integration — REAL OfflineGrantStore (FR-012 / 025 linkage)
# ---------------------------------------------------------------------------

def test_auth_logout_revokes_real_offline_grant(
    plane_runtime, grant_store, monkeypatch
):
    """028 FR-012: /auth/logout revokes the user's feature-025 offline grants
    through the real store — only the Keycloak revoke HTTP is faked."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())

    session_store = web_session_store(plane_runtime)
    monkeypatch.setattr(web_auth, "_get_store", lambda: session_store)
    monkeypatch.setattr(og, "get_offline_grant_store", lambda: grant_store)

    revoked_refresh = []

    async def idp_revoke_ok(token, client_id=None):
        revoked_refresh.append(token)
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", idp_revoke_ok)

    user_id = f"u-{uuid.uuid4()}"
    sid = secrets.token_urlsafe(24)
    session_refresh = f"rt-{uuid.uuid4()}"
    session_store.create(
        sid, user_id=user_id, access_token="at", refresh_token=session_refresh,
        hard_max_seconds=web_auth.HARD_MAX_SECONDS,
    )
    grant_id = grant_store.capture(user_id, f"offline-rt-{uuid.uuid4()}")
    assert grant_store.is_valid(grant_id, user_id=user_id) is True

    req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})
    try:
        resp = asyncio.run(web_auth.auth_logout(req))

        assert resp.status_code == 303
        assert sid not in web_auth._SESSIONS
        assert session_store.get(sid) is None
        assert revoked_refresh == [session_refresh]

        # The REAL store marked the grant revoked, and mint now refuses it.
        row = _grant_record(plane_runtime, user_id, grant_id)
        assert row is not None and row.revoked_at is not None
        assert grant_store.is_valid(grant_id, user_id=user_id) is False
        calls = _install_exploding_idp(monkeypatch)
        with pytest.raises(OfflineGrantError, match="revoked"):
            asyncio.run(grant_store.mint_access_token(grant_id, user_id=user_id))
        assert calls == []
    finally:
        web_auth._SESSIONS.pop(sid, None)
        session_store.delete(sid)
        _revoke_grants(plane_runtime, user_id)
        purge_revocations(plane_runtime, (user_id,))
