"""Tests for the durable web-session store and silent refresh
(orchestrator/session_store.py, web_auth.py): encrypted-at-rest CRUD, anchor
immutability, restart survival, the 365-day hard cap, and IdP refresh handling.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import time
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet

from orchestrator import web_auth
from orchestrator.session_store import SessionStoreError
from tests.helpers.session_plane_runtime import (
    get_session_record,
    isolated_plane_runtime,
    replace_session_record,
    revocation_records,
    web_session_store,
)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("session_refresh") as runtime:
        yield runtime


@pytest.fixture
def fernet_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", key)
    return key


@pytest.fixture
def keyed_store(plane_runtime, fernet_key):
    return web_session_store(plane_runtime)


@pytest.fixture
def auth_env(monkeypatch, plane_runtime, fernet_key):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    web_auth.reset_store_for_tests()
    store = web_session_store(plane_runtime)
    web_auth._STORE = store
    web_auth._SESSIONS.clear()

    async def _noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(web_auth, "_audit", _noop_audit)
    monkeypatch.setattr(
        web_auth, "_keycloak_config",
        lambda: ("http://idp.test/realms/astral", "astral-frontend", "csecret"),
    )
    yield store
    web_auth._SESSIONS.clear()
    web_auth.reset_store_for_tests()


def _ids():
    return f"sid-{uuid.uuid4()}", f"user-{uuid.uuid4()}"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://idp.test/token")
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=req,
                response=httpx.Response(self.status_code, request=req),
            )

    def json(self):
        return self._payload

    async def aiter_bytes(self, chunk_size=8192):
        yield json.dumps(self._payload).encode()


def _fake_async_client(post_result=None, post_exc=None):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None, **kwargs):
            if post_exc is not None:
                raise post_exc
            return post_result

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            assert method == "POST" and kwargs["follow_redirects"] is False
            if post_exc is not None:
                raise post_exc
            yield post_result

    return _Client


def _seed_session(store, *, sid, user_id, access="at-old", refresh="rt-old",
                  hard_max=3600):
    store.create(sid, user_id=user_id, access_token=access,
                 refresh_token=refresh, hard_max_seconds=hard_max)


def test_create_get_roundtrip_encrypted_at_rest(keyed_store, plane_runtime):
    sid, user_id = _ids()
    _seed_session(keyed_store, sid=sid, user_id=user_id,
                  access="at-secret", refresh="rt-secret")
    try:
        sess = keyed_store.get(sid)
        assert sess is not None
        assert sess["user_id"] == user_id
        assert sess["access_token"] == "at-secret"
        assert sess["refresh_token"] == "rt-secret"

        raw = get_session_record(plane_runtime, sid)
        assert raw is not None
        assert raw.access_token_ciphertext != "at-secret"
        assert raw.refresh_token_ciphertext != "rt-secret"
        assert raw.access_token_ciphertext.startswith("gAAAA")
    finally:
        keyed_store.delete(sid)


def test_update_tokens_rotates_but_never_moves_anchor(keyed_store, plane_runtime):
    sid, user_id = _ids()
    _seed_session(keyed_store, sid=sid, user_id=user_id)
    try:
        old_anchor = int(time.time()) - 12345
        current = get_session_record(plane_runtime, sid)
        assert current is not None
        replace_session_record(
            plane_runtime,
            replace(current, interactive_anchor=old_anchor),
        )

        keyed_store.update_tokens(sid, access_token="at-new", refresh_token="rt-new")

        raw = get_session_record(plane_runtime, sid)
        assert raw is not None
        assert raw.interactive_anchor == old_anchor
        assert raw.last_refresh_at >= old_anchor + 12345

        sess = keyed_store.get(sid)
        assert sess["access_token"] == "at-new"
        assert sess["refresh_token"] == "rt-new"
    finally:
        keyed_store.delete(sid)


def test_delete_returns_row_and_removes_it(keyed_store, plane_runtime):
    sid, user_id = _ids()
    _seed_session(keyed_store, sid=sid, user_id=user_id, refresh="rt-revoke-me")

    row = keyed_store.delete(sid)
    assert row is not None
    assert row["refresh_token"] == "rt-revoke-me"
    assert keyed_store.get(sid) is None
    assert get_session_record(plane_runtime, sid) is None

    assert keyed_store.delete(f"sid-{uuid.uuid4()}") is None


def test_delete_for_user_wipes_all_sessions(keyed_store):
    user_id = f"user-{uuid.uuid4()}"
    sids = [f"sid-{uuid.uuid4()}" for _ in range(2)]
    for sid in sids:
        _seed_session(keyed_store, sid=sid, user_id=user_id)
    other_sid, other_user = _ids()
    _seed_session(keyed_store, sid=other_sid, user_id=other_user)
    try:
        assert keyed_store.delete_for_user(user_id) == 2
        for sid in sids:
            assert keyed_store.get(sid) is None
        assert keyed_store.get(other_sid) is not None
    finally:
        keyed_store.delete(other_sid)


def test_restart_survival_fresh_store_instance(plane_runtime, fernet_key):
    sid, user_id = _ids()
    store_a = web_session_store(plane_runtime)
    _seed_session(store_a, sid=sid, user_id=user_id,
                  access="at-durable", refresh="rt-durable")
    try:
        store_b = web_session_store(plane_runtime)
        assert store_b._cache == {}
        sess = store_b.get(sid)
        assert sess is not None
        assert sess["user_id"] == user_id
        assert sess["access_token"] == "at-durable"
        assert sess["refresh_token"] == "rt-durable"
    finally:
        store_a.delete(sid)


def test_hard_cap_expired_session_is_deleted(plane_runtime, keyed_store):
    sid, user_id = _ids()
    _seed_session(keyed_store, sid=sid, user_id=user_id, hard_max=0)
    assert keyed_store.get(sid) is None
    assert get_session_record(plane_runtime, sid) is None

    sid2, user2 = _ids()
    _seed_session(keyed_store, sid=sid2, user_id=user2, hard_max=3600)
    current = get_session_record(plane_runtime, sid2)
    assert current is not None
    replace_session_record(
        plane_runtime,
        replace(current, hard_expires_at=int(time.time()) - 60),
    )
    fresh = web_session_store(plane_runtime)
    assert fresh.get(sid2) is None
    assert get_session_record(plane_runtime, sid2) is None
    keyed_store._cache.pop(sid2, None)


def test_dev_mode_keyless_plaintext_roundtrip(monkeypatch, plane_runtime):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)

    store = web_session_store(plane_runtime)
    assert store._fernet is None
    sid, user_id = _ids()
    _seed_session(store, sid=sid, user_id=user_id, access="at-plain", refresh="rt-plain")
    try:
        sess = store.get(sid)
        assert sess["access_token"] == "at-plain"
        raw = get_session_record(plane_runtime, sid)
        assert raw is not None
        assert raw.access_token_ciphertext == "at-plain"
    finally:
        store.delete(sid)


def test_production_keyless_fails_closed(monkeypatch, plane_runtime):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    with pytest.raises(SessionStoreError):
        web_session_store(plane_runtime)


def test_revocation_queue_lifecycle(keyed_store, plane_runtime):
    user_id = f"user-{uuid.uuid4()}"
    keyed_store.enqueue_revocation(user_id, "rt-queued")
    keyed_store.enqueue_revocation(user_id, "")

    def _mine():
        return [r for r in keyed_store.pending_revocations(limit=200)
                if r["user_id"] == user_id]

    mine = _mine()
    assert len(mine) == 1
    item = mine[0]
    assert item["refresh_token"] == "rt-queued"
    assert item["attempts"] == 0
    raw = next(
        record
        for record in revocation_records(plane_runtime, user_id)
        if record.queue_id == item["id"]
    )
    assert raw.refresh_token_ciphertext != "rt-queued"

    keyed_store.bump_revocation_attempt(item["id"])
    assert _mine()[0]["attempts"] == 1

    keyed_store.resolve_revocation(item["id"])
    assert _mine() == []


def test_session_by_sid_resumes_from_durable_store(auth_env):
    store = auth_env
    sid, user_id = _ids()
    _seed_session(store, sid=sid, user_id=user_id, access="at-1", refresh="rt-1")
    store._cache.clear()
    try:
        assert sid not in web_auth._SESSIONS
        sess = web_auth._session_by_sid(sid)
        assert sess is not None
        assert sess["sub"] == user_id
        assert sess["access_token"] == "at-1"
        assert sess["resumed"] is True
        assert web_auth._SESSIONS[sid] is sess
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)


def test_refresh_session_success_rotates_tokens(
    auth_env, monkeypatch, plane_runtime
):
    store = auth_env
    sid, user_id = _ids()
    _seed_session(store, sid=sid, user_id=user_id, access="at-old", refresh="rt-old")
    before = get_session_record(plane_runtime, sid)
    assert before is not None
    anchor_before = before.interactive_anchor
    sess = web_auth._session_by_sid(sid)
    monkeypatch.setattr(
        web_auth.httpx, "AsyncClient",
        _fake_async_client(post_result=_FakeResponse(
            200, {"access_token": "at-new", "refresh_token": "rt-new"})),
    )
    try:
        out = asyncio.run(web_auth._refresh_session(sid, sess))
        assert out is not sess
        assert out["incarnation_id"] == sess["incarnation_id"]
        assert out["access_token"] == "at-new"
        assert out["refresh_token"] == "rt-new"
        assert sess["access_token"] == "at-old"

        fresh = web_session_store(plane_runtime)
        row = fresh.get(sid)
        assert row["access_token"] == "at-new"
        assert row["refresh_token"] == "rt-new"
        assert row["interactive_anchor"] == anchor_before
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)


def test_refresh_refused_kills_session(auth_env, monkeypatch, plane_runtime):
    store = auth_env
    sid, user_id = _ids()
    _seed_session(store, sid=sid, user_id=user_id)
    sess = web_auth._session_by_sid(sid)
    monkeypatch.setattr(
        web_auth.httpx, "AsyncClient",
        _fake_async_client(post_result=_FakeResponse(400, {"error": "invalid_grant"})),
    )
    out = asyncio.run(web_auth._refresh_session(sid, sess))
    assert out is None
    assert sid not in web_auth._SESSIONS
    assert store.get(sid) is None
    assert get_session_record(plane_runtime, sid) is None


def test_refresh_network_error_keeps_session(auth_env, monkeypatch, plane_runtime):
    store = auth_env
    sid, user_id = _ids()
    _seed_session(store, sid=sid, user_id=user_id, access="at-keep", refresh="rt-keep")
    sess = web_auth._session_by_sid(sid)
    monkeypatch.setattr(
        web_auth.httpx, "AsyncClient",
        _fake_async_client(post_exc=httpx.ConnectError("idp unreachable")),
    )
    try:
        out = asyncio.run(web_auth._refresh_session(sid, sess))
        assert out is not sess
        assert out["incarnation_id"] == sess["incarnation_id"]
        assert out["access_token"] == "at-keep"
        assert out["refresh_token"] == ""
        assert sid in web_auth._SESSIONS
        assert get_session_record(plane_runtime, sid) is not None
    finally:
        web_auth._SESSIONS.pop(sid, None)
        store.delete(sid)
