"""Feature 028 — sign-out revocation lifecycle (FR-012..FR-014, research D5/D6).

Exercises the server-side sign-out path in ``orchestrator.web_auth``:
best-effort refresh-token revocation with the offline-tolerant retry queue
(``auth_revocation_queue``), the ``/auth/logout`` end-to-end flow (local
session destruction + offline-grant revocation + Keycloak end-session
redirect), and user-switch revocation in ``/auth/callback``.

Uses one isolated current AstralPlane PostgreSQL runtime; every row is keyed
by a uuid4 user_id and removed through typed owner-scoped contracts.
"""
import asyncio
import base64
import json
import secrets
import time
from types import SimpleNamespace
import uuid

import pytest
from cryptography.fernet import Fernet

from orchestrator import web_auth
from tests.helpers.session_plane_runtime import (
    isolated_plane_runtime,
    purge_revocations,
    revocation_records,
    web_session_store,
)


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
    with isolated_plane_runtime("logout_revocation") as runtime:
        yield runtime


@pytest.fixture()
def store(plane_runtime, monkeypatch):
    """A WebSessionStore with a real Fernet key, wired into web_auth."""
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    s = web_session_store(plane_runtime)
    monkeypatch.setattr(web_auth, "_get_store", lambda: s)
    return s


@pytest.fixture()
def real_auth_env(monkeypatch):
    """Mock auth OFF + a Keycloak authority so end-session redirects build."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)


def _queue_rows(plane_runtime, user_id):
    return revocation_records(plane_runtime, user_id)


def _purge_queue(plane_runtime, *user_ids):
    purge_revocations(plane_runtime, user_ids)


def _fake_jwt(payload: dict) -> str:
    """Unsigned base64url header.payload.sig JWT (web_auth decodes best-effort)."""
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.sig"


# ---------------------------------------------------------------------------
# _revoke_or_queue (FR-012/FR-013, D5)
# ---------------------------------------------------------------------------

def test_revoke_or_queue_success_queues_nothing(plane_runtime, store, monkeypatch):
    """028 FR-012: a successful IdP revocation leaves the retry queue empty."""
    user_id = f"u-{uuid.uuid4()}"
    calls = []

    async def ok(token, client_id=None):
        calls.append(token)
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", ok)
    asyncio.run(web_auth._revoke_or_queue(user_id, "rt-success"))
    assert calls == ["rt-success"]
    assert _queue_rows(plane_runtime, user_id) == ()

    # Empty refresh token short-circuits without even calling the IdP.
    asyncio.run(web_auth._revoke_or_queue(user_id, ""))
    assert calls == ["rt-success"]
    assert _queue_rows(plane_runtime, user_id) == ()


def test_revoke_or_queue_failure_enqueues_token(plane_runtime, store, monkeypatch):
    """028 FR-013 (D5): a failed IdP revocation lands the token in the queue."""
    user_id = f"u-{uuid.uuid4()}"
    token = f"rt-{uuid.uuid4()}"

    async def fail(_token, client_id=None):
        return False

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", fail)
    try:
        asyncio.run(web_auth._revoke_or_queue(user_id, token))
        rows = _queue_rows(plane_runtime, user_id)
        assert len(rows) == 1
        assert store._dec(rows[0].refresh_token_ciphertext) == token
        assert rows[0].attempts == 0
    finally:
        _purge_queue(plane_runtime, user_id)


# ---------------------------------------------------------------------------
# process_revocation_queue_once (FR-013, D5)
# ---------------------------------------------------------------------------

def test_revocation_queue_drains_on_success(plane_runtime, store, monkeypatch):
    """028 FR-013: queued tokens are revoked and removed; count returned."""
    user_id = f"u-{uuid.uuid4()}"
    mine = {f"rt-{uuid.uuid4()}", f"rt-{uuid.uuid4()}"}
    for t in mine:
        store.enqueue_revocation(user_id, t)

    async def revoke(token, client_id=None):
        return token in mine  # only resolve OUR rows — deterministic count

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", revoke)
    try:
        resolved = asyncio.run(web_auth.process_revocation_queue_once())
        assert resolved == 2
        assert _queue_rows(plane_runtime, user_id) == ()
    finally:
        _purge_queue(plane_runtime, user_id)


def test_revocation_queue_failure_bumps_attempts_and_keeps_row(
    plane_runtime, store, monkeypatch
):
    """028 FR-013: an unreachable IdP bumps attempts; the row survives."""
    user_id = f"u-{uuid.uuid4()}"
    token = f"rt-{uuid.uuid4()}"
    store.enqueue_revocation(user_id, token)

    async def fail(_token, client_id=None):
        return False

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", fail)
    try:
        resolved = asyncio.run(web_auth.process_revocation_queue_once())
        assert resolved == 0
        rows = _queue_rows(plane_runtime, user_id)
        assert len(rows) == 1
        assert rows[0].attempts == 1
        assert store._dec(rows[0].refresh_token_ciphertext) == token
    finally:
        _purge_queue(plane_runtime, user_id)


def test_revocation_queue_drops_row_after_max_attempts(
    plane_runtime, store, monkeypatch
):
    """028 FR-013 (D5): a row at the 30-attempt cap is dropped, not retried forever."""
    user_id = f"u-{uuid.uuid4()}"
    store.enqueue_revocation(user_id, "rt-doomed")
    for _ in range(web_auth._MAX_REVOCATION_ATTEMPTS):
        queued = next(
            row
            for row in store.pending_revocations(limit=200)
            if row["user_id"] == user_id
        )
        store.bump_revocation_attempt(queued["id"])

    async def fail(_token, client_id=None):
        return False

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", fail)
    try:
        resolved = asyncio.run(web_auth.process_revocation_queue_once())
        assert resolved == 0  # dropped rows don't count as resolved
        assert _queue_rows(plane_runtime, user_id) == ()  # but the row is gone
    finally:
        _purge_queue(plane_runtime, user_id)


# ---------------------------------------------------------------------------
# /auth/logout end-to-end (FR-012/FR-013)
# ---------------------------------------------------------------------------

def test_auth_logout_revokes_everything_and_redirects(
    plane_runtime, store, monkeypatch, real_auth_env
):
    """028 FR-012: logout kills the session (cache + store), revokes the
    refresh token and the user's offline grants, deletes the cookie, and
    redirects to the Keycloak end-session endpoint."""
    user_id = f"u-{uuid.uuid4()}"
    sid = secrets.token_urlsafe(24)
    refresh = f"rt-{uuid.uuid4()}"
    store.create(sid, user_id=user_id, access_token="at", refresh_token=refresh,
                 hard_max_seconds=web_auth.HARD_MAX_SECONDS)

    revoked_tokens = []

    async def ok(token, client_id=None):
        revoked_tokens.append(token)
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", ok)

    grant_calls = []
    monkeypatch.setattr(
        "orchestrator.offline_grant.get_offline_grant_store",
        lambda: SimpleNamespace(
            revoke_for_user=lambda uid: grant_calls.append(uid) or 0
        ),
    )

    req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})
    try:
        resp = asyncio.run(web_auth.auth_logout(req))

        assert sid not in web_auth._SESSIONS          # in-process cache cleared
        assert store.get(sid) is None                 # durable row deleted
        assert revoked_tokens == [refresh]            # refresh token revoked
        assert grant_calls == [user_id]               # 025 offline grants revoked
        assert _queue_rows(plane_runtime, user_id) == ()  # nothing queued

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "http://keycloak.test/realms/astral/protocol/openid-connect/logout" in location
        set_cookie = resp.headers.get("set-cookie", "")
        assert web_auth.COOKIE_NAME in set_cookie
        assert "Max-Age=0" in set_cookie              # cookie deleted
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)
        _purge_queue(plane_runtime, user_id)


def test_auth_logout_offline_idp_still_signs_out_and_queues(
    plane_runtime, store, monkeypatch, real_auth_env
):
    """028 FR-013: with the IdP unreachable, logout still completes locally
    (redirect + session gone) and the refresh token is queued for retry."""
    user_id = f"u-{uuid.uuid4()}"
    sid = secrets.token_urlsafe(24)
    refresh = f"rt-{uuid.uuid4()}"
    store.create(sid, user_id=user_id, access_token="at", refresh_token=refresh,
                 hard_max_seconds=web_auth.HARD_MAX_SECONDS)

    async def unreachable(_token, client_id=None):
        return False

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", unreachable)

    monkeypatch.setattr(
        "orchestrator.offline_grant.get_offline_grant_store",
        lambda: SimpleNamespace(revoke_for_user=lambda _uid: 0),
    )

    req = _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})
    try:
        resp = asyncio.run(web_auth.auth_logout(req))

        assert resp.status_code == 303                # local sign-out never blocks
        assert "/protocol/openid-connect/logout" in resp.headers["location"]
        assert sid not in web_auth._SESSIONS
        assert store.get(sid) is None

        rows = _queue_rows(plane_runtime, user_id)  # token awaits retry
        assert len(rows) == 1
        assert store._dec(rows[0].refresh_token_ciphertext) == refresh
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)
        _purge_queue(plane_runtime, user_id)


# ---------------------------------------------------------------------------
# User-switch revocation in /auth/callback (FR-014, D6)
# ---------------------------------------------------------------------------

def test_auth_callback_user_switch_revokes_prior_session(
    plane_runtime, store, monkeypatch, real_auth_env
):
    """028 FR-014 (D6): user B signing in over user A's live cookie revokes
    A's session (cache + store) and A's refresh token (revoked-or-queued)."""
    user_a = f"uA-{uuid.uuid4()}"
    user_b = f"uB-{uuid.uuid4()}"
    sid_a = secrets.token_urlsafe(24)
    refresh_a = f"rtA-{uuid.uuid4()}"
    web_auth._SESSIONS[sid_a] = {
        "sid": sid_a, "access_token": "atA", "refresh_token": refresh_a,
        "sub": user_a, "created_at": time.time(), "resumed": False,
    }
    store.create(sid_a, user_id=user_a, access_token="atA", refresh_token=refresh_a,
                 hard_max_seconds=web_auth.HARD_MAX_SECONDS)

    state = secrets.token_urlsafe(16)
    web_auth._PENDING[state] = {"code_verifier": "v" * 43, "created_at": time.time(), "next": "/"}

    token_response = {
        # carries the 'user' role: entry is role-gated since FR-005
        "access_token": _fake_jwt({"sub": user_b, "exp": int(time.time()) + 300,
                                   "realm_access": {"roles": ["user"]}}),
        "refresh_token": f"rtB-{uuid.uuid4()}",
    }

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return token_response

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse()

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _FakeAsyncClient)

    revoke_attempts = []

    async def unreachable(token, client_id=None):
        revoke_attempts.append(token)
        return False  # force the queue path so "revoked-or-queued" is provable

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", unreachable)

    req = _FakeRequest(
        cookies={web_auth.COOKIE_NAME: web_auth._sign(sid_a),
                 web_auth.STATE_COOKIE_NAME: web_auth._sign(state)},
        query_params={"code": "authcode-xyz", "state": state},
    )
    new_sids = []
    try:
        resp = asyncio.run(web_auth.auth_callback(req))

        # A's session is gone everywhere.
        assert sid_a not in web_auth._SESSIONS
        assert store.get(sid_a) is None

        # A's refresh token was revoked-or-queued (queued here: IdP "down").
        assert refresh_a in revoke_attempts
        rows = _queue_rows(plane_runtime, user_a)
        assert len(rows) == 1
        assert store._dec(rows[0].refresh_token_ciphertext) == refresh_a

        # And B got a fresh session + cookie.
        assert resp.status_code == 303
        assert web_auth.COOKIE_NAME in resp.headers.get("set-cookie", "")
        new_sids = [s for s, v in web_auth._SESSIONS.items() if v.get("sub") == user_b]
        assert len(new_sids) == 1
        assert store.get(new_sids[0]) is not None
    finally:
        web_auth._SESSIONS.pop(sid_a, None)
        for s in new_sids:
            web_auth._SESSIONS.pop(s, None)
        store.delete(sid_a)
        store.delete_for_user(user_b)
        _purge_queue(plane_runtime, user_a, user_b)
        web_auth._PENDING.pop(state, None)


def test_auth_callback_forged_state_leaves_prior_session_intact(
    plane_runtime, store, monkeypatch, real_auth_env
):
    """The user-switch revocation above is destructive, so it must be
    unreachable without the browser-bound state cookie: a forged callback
    carrying only the victim's session cookie is refused before the token
    exchange, leaving the victim signed in with an unrevoked refresh token."""
    user_a = f"uA-{uuid.uuid4()}"
    sid_a = secrets.token_urlsafe(24)
    refresh_a = f"rtA-{uuid.uuid4()}"
    web_auth._SESSIONS[sid_a] = {
        "sid": sid_a, "access_token": "atA", "refresh_token": refresh_a,
        "sub": user_a, "created_at": time.time(), "resumed": False,
    }
    store.create(sid_a, user_id=user_a, access_token="atA", refresh_token=refresh_a,
                 hard_max_seconds=web_auth.HARD_MAX_SECONDS)

    state = secrets.token_urlsafe(16)
    web_auth._PENDING[state] = {"code_verifier": "v" * 43, "created_at": time.time(), "next": "/"}

    posts = []

    class _ForbiddenClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            posts.append(url)
            raise AssertionError("token exchange must not run for an unbound state")

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _ForbiddenClient)

    revoke_attempts = []

    async def revoked(token, client_id=None):
        revoke_attempts.append(token)
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", revoked)

    req = _FakeRequest(
        cookies={web_auth.COOKIE_NAME: web_auth._sign(sid_a)},   # no state cookie
        query_params={"code": "authcode-forged", "state": state},
    )
    try:
        resp = asyncio.run(web_auth.auth_callback(req))

        assert resp.status_code == 200
        assert "invalid callback" in resp.body.decode("utf-8")
        assert posts == []                                  # the code was never spent
        assert revoke_attempts == []                        # A's refresh token untouched
        assert web_auth._SESSIONS.get(sid_a) is not None     # A is still signed in
        assert store.get(sid_a) is not None
        assert _queue_rows(plane_runtime, user_a) == ()
    finally:
        web_auth._SESSIONS.pop(sid_a, None)
        store.delete(sid_a)
        _purge_queue(plane_runtime, user_a)
        web_auth._PENDING.pop(state, None)
