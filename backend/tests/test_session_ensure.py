"""Tests for ensure_session and /auth/session (orchestrator/web_auth.py): the
silent-refresh window, refused-refresh and IdP-offline-skew handling, the hard_cap
death reason, and one-shot resumed semantics.
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


class _FakeRequest:
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
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "http://keycloak.test/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)


@pytest.fixture()
def memory_only(monkeypatch, real_auth_env):
    monkeypatch.setattr(web_auth, "_get_store", lambda: None)


@pytest.fixture()
def store(plane_runtime, monkeypatch, real_auth_env):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    s = web_session_store(plane_runtime)
    monkeypatch.setattr(web_auth, "_get_store", lambda: s)
    return s


def _ids():
    return f"sid-{uuid.uuid4()}", f"user-{uuid.uuid4()}"


def _fake_jwt(payload: dict) -> str:
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
    calls = []

    async def fake(sid, sess, **kwargs):
        calls.append(sid)
        return sess if behavior == "passthrough" else None

    monkeypatch.setattr(web_auth, "_refresh_session", fake)
    return calls


def _json(resp):
    return json.loads(resp.body)


def test_ensure_session_fresh_token_skips_refresh(memory_only, monkeypatch):
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


def test_auth_session_hard_cap_reason_memory_cache(memory_only):
    sid, user = _ids()
    _seed_memory(sid, sub=user, access_token="at",
                 created_at=time.time() - web_auth.HARD_MAX_SECONDS - 10)
    try:
        body = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert body == {"authenticated": False, "access_token": "",
                        "resumed": False, "reason": "hard_cap"}
        assert sid not in web_auth._SESSIONS

        body2 = _json(asyncio.run(web_auth.auth_session(_cookie_req(sid))))
        assert body2["reason"] == "refresh_failed"
    finally:
        web_auth._SESSIONS.pop(sid, None)
        web_auth._DEATH_REASONS.pop(sid, None)


def test_store_get_capped_row_records_death_reason(plane_runtime, store):
    sid, user = _ids()
    store.create(sid, user_id=user, access_token="at", refresh_token="rt",
                 hard_max_seconds=0)
    try:
        assert store.get(sid) is None
        assert get_session_record(plane_runtime, sid) is None
        assert store.pop_death_reason(sid) == "hard_cap"
        assert store.pop_death_reason(sid) is None
    finally:
        store.delete(sid)


def test_auth_session_hard_cap_reason_store_path(plane_runtime, store):
    sid, user = _ids()
    store.create(sid, user_id=user, access_token="at", refresh_token="rt",
                 hard_max_seconds=3600)
    current = get_session_record(plane_runtime, sid)
    assert current is not None
    replace_session_record(
        plane_runtime,
        replace(current, hard_expires_at=int(time.time()) - 60),
    )
    store._cache.pop(sid, None)
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


def test_auth_session_one_shot_resumed(memory_only, monkeypatch):
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
        assert calls == []
    finally:
        web_auth._SESSIONS.pop(sid, None)


def test_auth_session_resumed_flip_writes_through_the_store(
    plane_runtime, store, monkeypatch
):
    sid, user = _ids()
    token = _fake_jwt({"sub": user, "exp": int(time.time()) + 3600})
    store.create(sid, user_id=user, access_token=token, refresh_token="rt",
                 hard_max_seconds=3600, resumed=False)
    _seed_memory(sid, sub=user, access_token=token, resumed=False)["incarnation_id"] = store.get(sid)["incarnation_id"]
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
    sid, user = _ids()
    token = _fake_jwt({"sub": user, "exp": int(time.time()) + 3600})
    store.create(sid, user_id=user, access_token=token, refresh_token="rt",
                 hard_max_seconds=3600, resumed=False)
    _seed_memory(sid, sub=user, access_token=token, resumed=False)["incarnation_id"] = store.get(sid)["incarnation_id"]
    try:
        assert web_auth.session_resumed_flag(_cookie_req(sid)) is False

        row = get_session_record(plane_runtime, sid)
        assert row is not None and row.resumed is True

        assert web_auth.session_resumed_flag(_cookie_req(sid)) is True
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)


def test_session_resumed_flag_no_session_defaults_true(memory_only):
    assert web_auth.session_resumed_flag(_FakeRequest()) is True
