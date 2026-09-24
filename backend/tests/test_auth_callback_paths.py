"""Tests for orchestrator/web_auth.py's /auth/callback and /auth/login: deep-link
preservation across every error exit, the role gate's 403 refusal, and the IdP
pre-flight probe with its 60s cache.
"""

import asyncio
import base64
import json
import secrets
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.responses import HTMLResponse

from orchestrator import web_auth
from tests.helpers.session_plane_runtime import (
    isolated_plane_runtime,
    purge_revocations,
    web_session_store,
)

DEEP_LINK = "/?chat=abc"
DEEP_LINK_ENC = "%2F%3Fchat%3Dabc"


class _FakeRequest:
    def __init__(self, cookies=None, query_params=None, base_url="http://localhost:8001/"):
        self.cookies = cookies or {}
        self.query_params = query_params or {}
        self.base_url = base_url


def _fake_jwt(payload: dict) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.sig"


def _token_client(token_response: dict):
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

    return _FakeAsyncClient


def _capture_audit(monkeypatch):
    calls = []

    async def fake_audit(action, sub, description, *, outcome="success"):
        calls.append({"action": action, "sub": sub,
                      "description": description, "outcome": outcome})

    monkeypatch.setattr(web_auth, "_audit", fake_audit)
    return calls


def _seed_pending(nxt=DEEP_LINK):
    state = secrets.token_urlsafe(16)
    web_auth._PENDING[state] = {"code_verifier": "v" * 43,
                                "created_at": time.time(), "next": nxt}
    return state


def _state_cookies(state):
    return {web_auth.STATE_COOKIE_NAME: web_auth._sign(state)}


def _no_exchange_client(posts):
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            posts.append(url)
            raise AssertionError("token exchange must not run for an unbound state")

    return _FakeAsyncClient


@pytest.fixture(autouse=True)
def _reset_web_auth():
    web_auth.reset_store_for_tests()
    yield
    web_auth.reset_store_for_tests()


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("auth_callback") as runtime:
        yield runtime


@pytest.fixture()
def store(plane_runtime, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    s = web_session_store(plane_runtime)
    monkeypatch.setattr(web_auth, "_get_store", lambda: s)
    return s


@pytest.fixture()
def real_auth_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)


def _purge_queue(plane_runtime, *user_ids):
    purge_revocations(plane_runtime, user_ids)


def _session_rows(plane_runtime, user_id):
    with plane_runtime.transaction() as transaction:
        record = plane_runtime.repositories.history.sessions.get_latest_live_for_owner(
            transaction,
            owner_id=user_id,
            observed_at=int(time.time()),
        )
    return () if record is None else (record,)


def test_callback_success_redirects_to_deep_link(store, monkeypatch, real_auth_env):
    user_id = f"u-{uuid.uuid4()}"
    state = _seed_pending(DEEP_LINK)
    token_response = {
        "access_token": _fake_jwt({"sub": user_id, "exp": int(time.time()) + 300,
                                   "realm_access": {"roles": ["user"]}}),
        "refresh_token": f"rt-{uuid.uuid4()}",
    }
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _token_client(token_response))
    _capture_audit(monkeypatch)

    req = _FakeRequest(query_params={"code": "authcode-1", "state": state},
                       cookies=_state_cookies(state))
    new_sids = []
    try:
        resp = asyncio.run(web_auth.auth_callback(req))
        assert resp.status_code == 303
        assert resp.headers["location"] == DEEP_LINK
        assert web_auth.COOKIE_NAME in resp.headers.get("set-cookie", "")
        assert any(c.startswith(f"{web_auth.STATE_COOKIE_NAME}=")
                   and "max-age=0" in c.lower()
                   and f"Path={web_auth.STATE_COOKIE_PATH}" in c
                   for c in resp.headers.getlist("set-cookie"))
        new_sids = [s for s, v in web_auth._SESSIONS.items() if v.get("sub") == user_id]
        assert len(new_sids) == 1
    finally:
        for s in new_sids:
            web_auth._SESSIONS.pop(s, None)
        store.delete_for_user(user_id)
        web_auth._PENDING.pop(state, None)


def test_callback_idp_error_preserves_encoded_deep_link(real_auth_env):
    state = _seed_pending(DEEP_LINK)
    try:
        req = _FakeRequest(query_params={"state": state, "error": "access_denied"})
        resp = asyncio.run(web_auth.auth_callback(req))
        assert isinstance(resp, HTMLResponse)
        body = resp.body.decode("utf-8")
        assert f"/auth/login?next={DEEP_LINK_ENC}" in body
        assert 'next=%2F"' not in body
        assert "access_denied" in body
        assert state in web_auth._PENDING
    finally:
        web_auth._PENDING.pop(state, None)


def test_callback_missing_state_bounded_error():
    resp = asyncio.run(web_auth.auth_callback(_FakeRequest(query_params={})))
    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "invalid callback" in body
    assert "/auth/login?next=%2F" in body
    assert "http-equiv" not in body.lower()
    assert "<script" not in body.lower()


def test_callback_unknown_state_bounded_error():
    bogus_state = f"forged-{uuid.uuid4()}"
    req = _FakeRequest(query_params={"code": "authcode-2", "state": bogus_state})
    resp = asyncio.run(web_auth.auth_callback(req))
    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "invalid callback" in body
    assert "/auth/login?next=%2F" in body


def test_callback_token_exchange_failure_preserves_next(monkeypatch, real_auth_env):
    state = _seed_pending(DEEP_LINK)

    class _BoomClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            raise RuntimeError("token endpoint unreachable")

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _BoomClient)
    req = _FakeRequest(query_params={"code": "authcode-3", "state": state},
                       cookies=_state_cookies(state))
    resp = asyncio.run(web_auth.auth_callback(req))
    assert isinstance(resp, HTMLResponse)
    body = resp.body.decode("utf-8")
    assert "rejected the sign-in" in body
    assert f"/auth/login?next={DEEP_LINK_ENC}" in body
    assert state not in web_auth._PENDING


def test_callback_no_access_role_refused(
    plane_runtime, store, monkeypatch, real_auth_env
):
    user_id = f"u-{uuid.uuid4()}"
    refresh = f"rt-{uuid.uuid4()}"
    state = _seed_pending(DEEP_LINK)
    token_response = {
        "access_token": _fake_jwt({"sub": user_id, "exp": int(time.time()) + 300,
                                   "realm_access": {"roles": ["offline_access"]},
                                   "resource_access": {"acct": {"roles": ["view-profile"]}}}),
        "refresh_token": refresh,
    }
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _token_client(token_response))

    revoke_attempts = []

    async def revoked(token, client_id=None):
        revoke_attempts.append(token)
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", revoked)
    audits = _capture_audit(monkeypatch)

    sessions_before = set(web_auth._SESSIONS)
    req = _FakeRequest(query_params={"code": "authcode-4", "state": state},
                       cookies=_state_cookies(state))
    try:
        resp = asyncio.run(web_auth.auth_callback(req))

        assert resp.status_code == 403
        body = resp.body.decode("utf-8")
        assert "No access" in body
        assert web_auth.COOKIE_NAME not in "; ".join(resp.headers.getlist("set-cookie"))

        assert set(web_auth._SESSIONS) == sessions_before
        assert _session_rows(plane_runtime, user_id) == ()

        assert revoke_attempts == [refresh]

        assert any(a["action"] == "login_interactive" and a["outcome"] == "failure"
                   and a["sub"] == user_id for a in audits)
    finally:
        store.delete_for_user(user_id)
        _purge_queue(plane_runtime, user_id)
        web_auth._PENDING.pop(state, None)


def test_callback_user_role_passes_gate(
    plane_runtime, store, monkeypatch, real_auth_env
):
    user_id = f"u-{uuid.uuid4()}"
    state = _seed_pending("/")
    token_response = {
        "access_token": _fake_jwt({"sub": user_id, "exp": int(time.time()) + 300,
                                   "realm_access": {"roles": ["user"]}}),
        "refresh_token": f"rt-{uuid.uuid4()}",
    }
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _token_client(token_response))
    audits = _capture_audit(monkeypatch)

    req = _FakeRequest(query_params={"code": "authcode-5", "state": state},
                       cookies=_state_cookies(state))
    new_sids = []
    try:
        resp = asyncio.run(web_auth.auth_callback(req))
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        new_sids = [s for s, v in web_auth._SESSIONS.items() if v.get("sub") == user_id]
        assert len(new_sids) == 1
        assert len(_session_rows(plane_runtime, user_id)) == 1
        assert any(a["action"] == "login_interactive" and a["outcome"] == "success"
                   for a in audits)
    finally:
        for s in new_sids:
            web_auth._SESSIONS.pop(s, None)
        store.delete_for_user(user_id)
        web_auth._PENDING.pop(state, None)


def test_auth_login_binds_state_to_a_signed_cookie(monkeypatch, real_auth_env):
    class _UpResponse:
        status_code = 200

    class _UpClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _UpResponse()

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _UpClient)

    pending_before = set(web_auth._PENDING)
    try:
        resp = asyncio.run(web_auth.auth_login(_FakeRequest(query_params={"next": "/"})))
        minted = (set(web_auth._PENDING) - pending_before).pop()

        cookie = resp.headers["set-cookie"]
        assert cookie.startswith(f"{web_auth.STATE_COOKIE_NAME}=")
        assert "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()
        assert f"Path={web_auth.STATE_COOKIE_PATH}" in cookie
        value = cookie.split("=", 1)[1].split(";", 1)[0]
        assert web_auth._unsign(value) == minted
    finally:
        for s in set(web_auth._PENDING) - pending_before:
            web_auth._PENDING.pop(s, None)


def test_callback_without_state_cookie_refused_before_token_exchange(monkeypatch, real_auth_env):
    state = _seed_pending(DEEP_LINK)
    posts = []
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _no_exchange_client(posts))

    try:
        req = _FakeRequest(query_params={"code": "authcode-forged", "state": state})
        resp = asyncio.run(web_auth.auth_callback(req))

        assert isinstance(resp, HTMLResponse)
        assert "invalid callback" in resp.body.decode("utf-8")
        assert posts == []
        # Not consumed — an unbound hit must not burn the real login
        assert state in web_auth._PENDING
    finally:
        web_auth._PENDING.pop(state, None)


def test_callback_state_cookie_from_another_login_refused(monkeypatch, real_auth_env):
    state = _seed_pending(DEEP_LINK)
    other = _seed_pending("/")
    posts = []
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _no_exchange_client(posts))

    try:
        req = _FakeRequest(query_params={"code": "authcode-forged-2", "state": state},
                           cookies=_state_cookies(other))
        resp = asyncio.run(web_auth.auth_callback(req))
        assert "invalid callback" in resp.body.decode("utf-8")
        assert posts == []
    finally:
        web_auth._PENDING.pop(other, None)


def test_callback_unsigned_state_cookie_refused(monkeypatch, real_auth_env):
    state = _seed_pending(DEEP_LINK)
    posts = []
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _no_exchange_client(posts))

    req = _FakeRequest(query_params={"code": "authcode-forged-3", "state": state},
                       cookies={web_auth.STATE_COOKIE_NAME: state})
    resp = asyncio.run(web_auth.auth_callback(req))
    assert "invalid callback" in resp.body.decode("utf-8")
    assert posts == []


def test_auth_login_idp_unreachable_returns_503_with_retry(monkeypatch, real_auth_env):
    probes = []

    class _DownClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            probes.append(url)
            raise RuntimeError("connection refused")

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _DownClient)

    pending_before = set(web_auth._PENDING)
    req = _FakeRequest(query_params={"next": DEEP_LINK})
    resp = asyncio.run(web_auth.auth_login(req))

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 503
    body = resp.body.decode("utf-8")
    assert "unreachable" in body
    assert f"/auth/login?next={DEEP_LINK_ENC}" in body
    assert "http-equiv" not in body.lower()
    assert "<script" not in body.lower()

    assert len(probes) == 1
    assert probes[0].endswith("/.well-known/openid-configuration")
    assert set(web_auth._PENDING) == pending_before


def test_auth_login_probe_success_cached_60s(monkeypatch, real_auth_env):
    probe_count = {"n": 0}

    class _UpResponse:
        status_code = 200

    class _UpClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            probe_count["n"] += 1
            return _UpResponse()

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", _UpClient)

    pending_before = set(web_auth._PENDING)
    try:
        resp1 = asyncio.run(web_auth.auth_login(_FakeRequest(query_params={"next": "/"})))
        resp2 = asyncio.run(web_auth.auth_login(_FakeRequest(query_params={"next": "/"})))

        assert probe_count["n"] == 1
        for resp in (resp1, resp2):
            assert resp.status_code in (302, 303, 307)
            location = resp.headers["location"]
            assert location.startswith(
                "http://keycloak.test/realms/astral/protocol/openid-connect/auth?")
            assert "code_challenge_method=S256" in location
        assert len(set(web_auth._PENDING) - pending_before) == 2
    finally:
        for s in set(web_auth._PENDING) - pending_before:
            web_auth._PENDING.pop(s, None)
