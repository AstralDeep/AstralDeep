"""Feature 028 — FR-006/CT-auth decision layer: ensure_session + /auth/session.

Unit-tests ``orchestrator.web_auth.ensure_session`` (the 60-second silent-
refresh window, refused refresh, IdP-offline skew tolerance) and the
``/auth/session`` route handler called directly with a fake Request: the
contracted ``reason: 'hard_cap'`` on both the in-memory-cache and the
durable-store death paths, and the one-shot ``resumed`` semantics
(auth-session.md), plus the ``session_resumed_flag`` shell-injection helper.

Style follows tests/test_logout_revocation.py (_FakeRequest and _fake_jwt).
All durable scenarios use a uniquely named, current Plane database.
"""
import asyncio
import base64
from dataclasses import replace
import json
import time
import uuid

import pytest
from cryptography.fernet import Fernet

from orchestrator import web_auth
from tests.helpers.session_plane_runtime import (
    get_session_record,
    isolated_plane_runtime,
    replace_session_record,
    web_session_store,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _FakeRequest:
    """auth_session / ensure_session / session_resumed_flag read only .cookies."""

    def __init__(self, cookies=None, query_params=None, base_url="http://localhost:8001/"):
        self.cookies = cookies or {}
        self.query_params = query_params or {}
        self.base_url = base_url


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("session_ensure") as runtime:
        yield runtime


@pytest.fixture()
def real_auth_env(monkeypatch):
    """Mock auth OFF so the real session/refresh decision layer runs."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)


@pytest.fixture()
def memory_only(monkeypatch, real_auth_env):
    """No durable store — web_auth runs purely on the _SESSIONS cache."""
    monkeypatch.setattr(web_auth, "_get_store", lambda: None)


@pytest.fixture()
def store(plane_runtime, monkeypatch, real_auth_env):
    """A WebSessionStore with a real Fernet key, wired into web_auth."""
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    s = web_session_store(plane_runtime)
    monkeypatch.setattr(web_auth, "_get_store", lambda: s)
    return s


def _ids():
    return f"sid-{uuid.uuid4()}", f"user-{uuid.uuid4()}"


def _fake_jwt(payload: dict) -> str:
    """Unsigned base64url header.payload.sig JWT (web_auth decodes best-effort)."""
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.sig"


def _seed_memory(sid, *, sub, access_token, refresh_token="rt",
                 created_at=None, resumed=True):
    sess = {
        "sid": sid,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "sub": sub,
        "created_at": time.time() if created_at is None else created_at,
        "resumed": resumed,
    }
    web_auth._SESSIONS[sid] = sess
    return sess


def _cookie_req(sid):
    return _FakeRequest(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})


def _counting_refresh(monkeypatch, behavior="passthrough"):
    """Replace _refresh_session with a call-counting fake.

    behavior='passthrough' simulates the IdP-unreachable path (the session is
    returned unchanged); behavior=None simulates a refused refresh."""
    calls = []

    async def fake(sid, sess):
        calls.append(sid)
        return sess if behavior == "passthrough" else None

    monkeypatch.setattr(web_auth, "_refresh_session", fake)
    return calls


def _json(resp):
    return json.loads(resp.body)


# ---------------------------------------------------------------------------
# ensure_session — silent-refresh decision window (FR-006, D2)
# ---------------------------------------------------------------------------

def test_ensure_session_fresh_token_skips_refresh(memory_only, monkeypatch):
    """028 FR-006 (D2): an access token expiring far outside the 60s refresh
    window is served as-is — _refresh_session is never invoked."""
    sid, user = _ids()
    sess = _seed_memory(sid, sub=user,
                        access_token=_fake_jwt({"sub": user, "exp": int(time.time()) + 3600}))
    calls = _counting_refresh(monkeypatch)
    try:
        out = asyncio.run(web_auth.ensure_session(_cookie_req(sid)))
        assert out is sess
        assert calls == []
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_ensure_session_refreshes_inside_window(memory_only, monkeypatch):
    """028 FR-006 (D2): a token with less than 60s left triggers exactly one
    silent refresh and the (refreshed) session is served."""
    sid, user = _ids()
    sess = _seed_memory(sid, sub=user,
                        access_token=_fake_jwt({"sub": user, "exp": int(time.time()) + 30}))
    calls = _counting_refresh(monkeypatch)
    try:
        out = asyncio.run(web_auth.ensure_session(_cookie_req(sid)))
        assert out is sess
        assert calls == [sid]
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_ensure_session_opaque_token_triggers_refresh(memory_only, monkeypatch):
    """028 FR-006: a token whose exp cannot be decoded (opaque/no exp claim)
    is treated as inside the window — refresh is attempted; with no decodable
    expiry afterwards the session is still served (no hard-expiry proof)."""
    sid, user = _ids()
    sess = _seed_memory(sid, sub=user, access_token=f"at-opaque-{uuid.uuid4()}")
    calls = _counting_refresh(monkeypatch)
    try:
        out = asyncio.run(web_auth.ensure_session(_cookie_req(sid)))
        assert out is sess
        assert calls == [sid]
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_ensure_session_none_when_refresh_refused(memory_only, monkeypatch):
    """028 FR-006 (D2): when the refresh is refused (_refresh_session returns
    None — revoked/expired refresh token) ensure_session yields None and
    interactive login is required."""
    sid, user = _ids()
    _seed_memory(sid, sub=user,
                 access_token=_fake_jwt({"sub": user, "exp": int(time.time()) + 10}))
    calls = _counting_refresh(monkeypatch, behavior=None)
    try:
        assert asyncio.run(web_auth.ensure_session(_cookie_req(sid))) is None
        assert calls == [sid]
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_ensure_session_offline_within_skew_still_serves(memory_only, monkeypatch):
    """028 FR-009 (D2 offline tolerance): IdP unreachable (_refresh_session
    passthrough) + token expired but within the ±300s clock-skew window —
    the session is still served."""
    sid, user = _ids()
    sess = _seed_memory(sid, sub=user,
                        access_token=_fake_jwt({"sub": user, "exp": int(time.time()) - 100}))
    calls = _counting_refresh(monkeypatch)
    try:
        out = asyncio.run(web_auth.ensure_session(_cookie_req(sid)))
        assert out is sess
        assert calls == [sid]
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_ensure_session_offline_beyond_skew_dies(memory_only, monkeypatch):
    """028 FR-006/FR-009: IdP unreachable but the token is hard-expired beyond
    the 300s skew — ensure_session returns None, and /auth/session reports the
    generic 'refresh_failed' reason (no recorded death cause)."""
    sid, user = _ids()
    _seed_memory(sid, sub=user,
                 access_token=_fake_jwt({"sub": user, "exp": int(time.time()) - 400}))
    _counting_refresh(monkeypatch)
    try:
        assert asyncio.run(web_auth.ensure_session(_cookie_req(sid))) is None

        body = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert body["authenticated"] is False
        assert body["reason"] == "refresh_failed"
    finally:
        web_auth._SESSIONS.pop(sid, None)


# ---------------------------------------------------------------------------
# /auth/session — reason 'hard_cap' (FR-006/FR-007, auth-session.md contract)
# ---------------------------------------------------------------------------

def test_auth_session_hard_cap_reason_memory_cache(memory_only):
    """028 FR-007: a cached session whose interactive anchor is older than the
    365-day cap dies at lookup, and /auth/session reports the contracted
    reason 'hard_cap' (one-shot — a later probe falls back to refresh_failed)."""
    sid, user = _ids()
    _seed_memory(sid, sub=user, access_token="at",
                 created_at=time.time() - web_auth.HARD_MAX_SECONDS - 10)
    try:
        body = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert body == {"authenticated": False, "access_token": "",
                        "resumed": False, "reason": "hard_cap"}
        assert sid not in web_auth._SESSIONS

        # Death reason is consumed on read: second probe is generic.
        body2 = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert body2["reason"] == "refresh_failed"
    finally:
        web_auth._SESSIONS.pop(sid, None)
        web_auth._DEATH_REASONS.pop(sid, None)


def test_store_get_capped_row_records_death_reason(plane_runtime, store):
    """028 FR-006/FR-007: WebSessionStore.get deletes a hard-capped row and
    records 'hard_cap' for pop_death_reason (itself one-shot)."""
    sid, user = _ids()
    store.create(sid, user_id=user, access_token="at", refresh_token="rt",
                 hard_max_seconds=0)
    try:
        assert store.get(sid) is None
        assert get_session_record(plane_runtime, sid) is None
        assert store.pop_death_reason(sid) == "hard_cap"
        assert store.pop_death_reason(sid) is None   # consumed
    finally:
        store.delete(sid)


def test_auth_session_hard_cap_reason_store_path(plane_runtime, store):
    """028 FR-007 (D3): with no in-process cache (restart), the durable-store
    read path surfaces the hard cap end-to-end — the capped web_session row is
    deleted and /auth/session answers authenticated:false reason:'hard_cap'."""
    sid, user = _ids()
    store.create(sid, user_id=user, access_token="at", refresh_token="rt",
                 hard_max_seconds=3600)
    current = get_session_record(plane_runtime, sid)
    assert current is not None
    replace_session_record(
        plane_runtime,
        replace(current, hard_expires_at=int(time.time()) - 60),
    )
    store._cache.pop(sid, None)          # simulate a fresh process
    web_auth._SESSIONS.pop(sid, None)
    try:
        body = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert body["authenticated"] is False
        assert body["access_token"] == ""
        assert body["reason"] == "hard_cap"
        assert get_session_record(plane_runtime, sid) is None
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)
        web_auth._DEATH_REASONS.pop(sid, None)


# ---------------------------------------------------------------------------
# One-shot resumed semantics (FR-011, auth-session.md)
# ---------------------------------------------------------------------------

def test_auth_session_one_shot_resumed(memory_only, monkeypatch):
    """028 FR-011: only the first /auth/session fetch after interactive login
    (resumed=False as _establish_session seeds it) reports resumed:false; the
    fetch itself flips the session so every later one reports resumed:true."""
    sid, user = _ids()
    token = _fake_jwt({"sub": user, "exp": int(time.time()) + 3600})
    _seed_memory(sid, sub=user, access_token=token, resumed=False)
    calls = _counting_refresh(monkeypatch)
    try:
        first = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert first == {"authenticated": True, "access_token": token,
                         "resumed": False, "user_id": user}

        second = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert second["authenticated"] is True
        assert second["resumed"] is True
        assert calls == []                # fresh token: no refresh either fetch
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_auth_session_resumed_flip_writes_through_the_store(
    plane_runtime, store, monkeypatch
):
    """028 FR-011 + 052: the async /auth/session one-shot flip persists to the
    durable row off the event loop (store.amark_resumed), so a restart after
    the first fetch still reports resumed:true."""
    sid, user = _ids()
    token = _fake_jwt({"sub": user, "exp": int(time.time()) + 3600})
    store.create(sid, user_id=user, access_token=token, refresh_token="rt",
                 hard_max_seconds=3600, resumed=False)
    _seed_memory(sid, sub=user, access_token=token, resumed=False)
    _counting_refresh(monkeypatch)
    try:
        first = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert first["authenticated"] is True and first["resumed"] is False

        row = get_session_record(plane_runtime, sid)
        assert row is not None and row.resumed is True
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)


def test_session_resumed_flag_one_shot_and_persists(plane_runtime, store):
    """028 FR-011: session_resumed_flag has the same one-shot flip as
    /auth/session — False once right after interactive login, True after —
    and the flip is persisted to the durable row via mark_resumed."""
    sid, user = _ids()
    token = _fake_jwt({"sub": user, "exp": int(time.time()) + 3600})
    store.create(sid, user_id=user, access_token=token, refresh_token="rt",
                 hard_max_seconds=3600, resumed=False)
    _seed_memory(sid, sub=user, access_token=token, resumed=False)
    try:
        assert web_auth.session_resumed_flag(_cookie_req(sid)) is False

        row = get_session_record(plane_runtime, sid)
        assert row is not None and row.resumed is True  # persisted in Plane

        assert web_auth.session_resumed_flag(_cookie_req(sid)) is True
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)


def test_session_resumed_flag_no_session_defaults_true(memory_only):
    """028 FR-011: with no session at all the shell helper reports True (a
    resume) — resumed:false is reserved for the post-interactive-login load."""
    assert web_auth.session_resumed_flag(_FakeRequest()) is True
