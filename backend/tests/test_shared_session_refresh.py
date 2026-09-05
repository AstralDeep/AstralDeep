"""079: one durable credential family across browser and background consumers."""
import asyncio
import json
import time
import uuid
from dataclasses import replace
from contextlib import asynccontextmanager

import pytest
from cryptography.fernet import Fernet

from orchestrator import offline_grant as og
from orchestrator import session_store as ss
from orchestrator import web_auth
from tests.helpers.session_plane_runtime import (
    get_session_record, isolated_plane_runtime, replace_session_record, web_session_store,
)


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("shared_refresh") as value:
        yield value


@pytest.fixture
def stores(runtime, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", key)
    monkeypatch.setenv("OFFLINE_GRANT_ENC_KEY", key)
    monkeypatch.setattr(og, "OFFLINE_GRANT_ENC_KEY", key)
    owner, sid = str(uuid.uuid4()), str(uuid.uuid4())
    sessions = web_session_store(runtime)
    sessions.create(sid, user_id=owner, access_token="access-initial",
                    refresh_token="refresh-initial", hard_max_seconds=3600)
    grants = og.OfflineGrantStore(plane_runtime=runtime)
    yield sessions, grants, owner, sid
    grants.revoke_for_user(owner)
    sessions.delete(sid)


def test_two_process_consumers_serialize_rotating_credentials(stores, runtime):
    sessions, _, owner, sid = stores
    other = web_session_store(runtime)
    seen = []

    async def exchange(refresh, access):
        seen.append(refresh)
        await asyncio.sleep(.02)
        return {"access_token": "access-" + str(len(seen)),
                "refresh_token": "refresh-" + str(len(seen))}

    async def scenario():
        return await asyncio.gather(
            sessions.refresh_credential(sid, owner_id=owner, exchange=exchange),
            other.refresh_credential(sid, owner_id=owner, exchange=exchange),
        )

    results = asyncio.run(scenario())
    assert seen == ["refresh-initial", "refresh-1"]
    assert {r["access_token"] for r in results} == {"access-1", "access-2"}
    assert web_session_store(runtime).get(sid)["refresh_token"] == "refresh-2"


def test_cancelled_rotation_never_replays_old_token(stores, monkeypatch):
    sessions, _, owner, sid = stores
    calls = []

    async def exchange(refresh, access):
        calls.append(refresh)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))
    monkeypatch.setattr(ss, "REFRESH_WAIT_SECONDS", .02)
    with pytest.raises(ss.SessionRefreshUnavailable):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))
    assert calls == ["refresh-initial"]
    assert web_session_store(sessions._sessions.plane_runtime).get(sid)["refresh_token"] == ""


def test_deleted_session_cannot_be_resurrected_by_late_rotation(stores):
    sessions, _, owner, sid = stores

    async def exchange(refresh, access):
        await sessions.adelete(sid)
        return {"access_token": "must-not-return", "refresh_token": "rotated"}

    with pytest.raises(ss.SessionRefreshUnavailable):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))
    assert sessions.get(sid) is None


def test_capture_links_siblings_to_one_session_and_rejects_unmatched_token(stores):
    sessions, grants, owner, sid = stores
    first = grants.capture(owner, "refresh-initial", agent_id="a")
    second = grants.capture(owner, "refresh-initial", agent_id="b")
    assert first != second
    for grant_id in (first, second):
        record = grants._grant(owner, grant_id)
        body = og._fernet().decrypt(record.encrypted_refresh_token)
        assert b"refresh-initial" not in body and sid.encode() in body
    with pytest.raises(og.OfflineGrantError, match="session"):
        grants.capture(owner, "unmatched-old-refresh")


def _idp(monkeypatch, callback):
    class Response:
        status = 200

        def __init__(self, data):
            self.data, self.content = data, self

        async def __aenter__(self):
            self.payload = await callback(self.data)
            return self

        async def __aexit__(self, *args):
            return False

        async def readexactly(self, size):
            body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
            if len(body) >= size:
                return body[:size]
            raise asyncio.IncompleteReadError(body, size)

    class Client:
        def __init__(self, *, timeout):
            assert timeout.total == 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, *, data, allow_redirects):
            assert not allow_redirects
            return Response(data)

    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "https://idp.test/token")
    monkeypatch.setattr(og.aiohttp, "ClientSession", Client)


def test_parallel_sibling_grants_survive_repeated_rotation_and_restart(stores, runtime, monkeypatch):
    sessions, grants, owner, sid = stores
    grant_ids = [grants.capture(owner, "refresh-initial", agent_id=str(i)) for i in range(2)]
    seen = []

    async def exchange(data):
        seen.append(data["refresh_token"])
        await asyncio.sleep(.02)
        return {"access_token": "access-" + str(len(seen)),
                "refresh_token": "refresh-" + str(len(seen))}

    _idp(monkeypatch, exchange)

    async def scenario():
        return await asyncio.gather(*(og.OfflineGrantStore(plane_runtime=runtime).mint_access_token(
            gid, user_id=owner) for gid in grant_ids))

    assert set(asyncio.run(scenario())) == {"access-1", "access-2"}
    assert asyncio.run(grants.mint_access_token(grant_ids[0], user_id=owner)) == "access-3"
    assert seen == ["refresh-initial", "refresh-1", "refresh-2"]
    # Even a previously warm cache reads the canonical rotated credential.
    assert sessions.get(sid)["refresh_token"] == "refresh-3"


def test_revocation_during_rotation_persists_family_but_returns_no_access(stores, monkeypatch):
    sessions, grants, owner, sid = stores
    grant_id = grants.capture(owner, "refresh-initial")

    async def exchange(data):
        await asyncio.to_thread(grants.revoke_for_user, owner)
        return {"access_token": "forbidden-access", "refresh_token": "rotated"}

    _idp(monkeypatch, exchange)
    with pytest.raises(og.OfflineGrantError, match="revoked"):
        asyncio.run(grants.mint_access_token(grant_id, user_id=owner))
    assert not grants._grant(owner, grant_id).active
    assert sessions.get(sid)["refresh_token"] == "rotated"


@pytest.mark.parametrize("payload", [
    None, [], {}, {"access_token": ""}, {"access_token": 1},
    {"access_token": "ok", "refresh_token": ""},
    {"access_token": "ok", "refresh_token": 42},
    {"access_token": "a" * 32769}, {"access_token": "has space"},
])
def test_malformed_rotation_is_not_returned_or_replayed(stores, payload):
    sessions, _, owner, sid = stores

    async def exchange(refresh, access):
        return payload

    with pytest.raises(ss.SessionRefreshUnavailable, match="malformed"):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))
    assert sessions.get(sid)["refresh_token"] == ""


@pytest.mark.parametrize("body", [b"{broken", b"x" * 65537, b"\xff"])
def test_http_response_is_bounded_and_malformed_body_is_sanitized(stores, monkeypatch, body):
    sessions, grants, owner, sid = stores
    grant_id = grants.capture(owner, "refresh-initial")

    async def exchange(data):
        return body

    _idp(monkeypatch, exchange)
    with pytest.raises(og.OfflineGrantError, match="malformed|limit") as error:
        asyncio.run(grants.mint_access_token(grant_id, user_id=owner))
    assert "refresh-initial" not in str(error.value)
    assert sessions.get(sid)["refresh_token"] == ""


def test_timeout_is_bounded_without_busy_polling(stores, monkeypatch):
    sessions, _, owner, sid = stores
    monkeypatch.setattr(ss, "REFRESH_WAIT_SECONDS", .03)
    calls = []

    async def exchange(refresh, access):
        calls.append(refresh)
        await asyncio.sleep(2)

    start = time.monotonic()
    with pytest.raises(ss.SessionRefreshUnavailable, match="time limit"):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))
    assert time.monotonic() - start < 1
    assert calls == ["refresh-initial"]


def test_legacy_exact_match_converts_without_copying_or_reusing_old_token(stores, runtime, monkeypatch):
    sessions, grants, owner, sid = stores
    grant_id = str(uuid.uuid4())
    with runtime.transaction() as tx:
        runtime.repositories.offline_grants.create_grant(
            tx, grant_id=grant_id, owner_id=owner, agent_id=None,
            encrypted_refresh_token=og._fernet().encrypt(b"refresh-initial"),
            issued_at=og._now_ms(), expires_at=og._now_ms() + 60000)

    async def exchange(data):
        assert data["refresh_token"] == "refresh-initial"
        return {"access_token": "fresh", "refresh_token": "rotated"}

    _idp(monkeypatch, exchange)
    assert asyncio.run(grants.mint_access_token(grant_id, user_id=owner)) == "fresh"
    plaintext = og._fernet().decrypt(grants._grant(owner, grant_id).encrypted_refresh_token)
    assert sid.encode() in plaintext and b"refresh-initial" not in plaintext


def test_legacy_unmatched_token_never_contacts_idp(stores, runtime, monkeypatch):
    _, grants, owner, _ = stores
    grant_id = str(uuid.uuid4())
    with runtime.transaction() as tx:
        runtime.repositories.offline_grants.create_grant(
            tx, grant_id=grant_id, owner_id=owner, agent_id=None,
            encrypted_refresh_token=og._fernet().encrypt(b"stale-copy"),
            issued_at=og._now_ms(), expires_at=og._now_ms() + 60000)
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "https://idp.test/token")
    monkeypatch.setattr(og.aiohttp, "ClientSession", lambda **kw: pytest.fail("no HTTP"))
    with pytest.raises(og.OfflineGrantError, match="re-consent"):
        asyncio.run(grants.mint_access_token(grant_id, user_id=owner))


def test_session_identity_and_owner_are_checked_before_exchange(stores, runtime):
    sessions, grants, owner, sid = stores
    reference = sessions.session_reference(owner, "refresh-initial")

    async def exchange(*args):
        pytest.fail("no HTTP")

    for subject, binding in (("other", None), (owner, {**reference, "created_at": 1})):
        with pytest.raises(ss.SessionRefreshUnavailable):
            asyncio.run(sessions.refresh_credential(sid, owner_id=subject,
                                                   reference=binding, exchange=exchange))
    record = get_session_record(runtime, sid)
    replace_session_record(runtime, replace(record, hard_expires_at=int(time.time()) - 1))
    with pytest.raises(ss.SessionRefreshUnavailable, match="expired"):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))


def test_failed_cas_settlement_returns_no_access(stores, monkeypatch):
    sessions, _, owner, sid = stores

    async def exchange(refresh, access):
        def stale(*args, **kwargs):
            raise ss.RepositoryConflictError("stale")

        monkeypatch.setattr(sessions._sessions.repository, "compare_and_set_refresh", stale)
        return {"access_token": "never-return", "refresh_token": "rotated"}

    with pytest.raises(ss.SessionRefreshUnavailable, match="changed"):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))


@pytest.mark.parametrize("plaintext", [
    "", "not ascii\u00e9", ss._REFRESH_CLAIM_PREFIX + "{}",
    ss._REFRESH_CLAIM_PREFIX + '{"started":0}',
    ss._REFRESH_CLAIM_PREFIX + '{"started":"bad"}',
])
def test_invalid_or_abandoned_credential_state_never_reaches_http(stores, runtime, plaintext):
    sessions, _, owner, sid = stores
    record = get_session_record(runtime, sid)
    replace_session_record(runtime, replace(record,
        refresh_token_ciphertext=sessions._enc(plaintext)))

    async def exchange(*args):
        pytest.fail("credential state is not an OAuth token")

    with pytest.raises(ss.SessionRefreshUnavailable):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))


def test_wrong_key_and_keyless_coordinator_refuse_before_http(stores, runtime):
    sessions, _, owner, sid = stores

    async def exchange(*args):
        pytest.fail("no HTTP")

    sessions._fernet = Fernet(Fernet.generate_key())
    with pytest.raises(ss.SessionRefreshUnavailable, match="decrypted"):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))
    sessions._fernet = None
    with pytest.raises(ss.SessionRefreshUnavailable, match="encrypted"):
        asyncio.run(sessions.refresh_credential(sid, owner_id=owner, exchange=exchange))
    with pytest.raises(ss.SessionRefreshUnavailable, match="encrypted"):
        sessions.session_reference(owner, "refresh-initial")


@pytest.mark.parametrize("body", [b"not-fernet", og._SESSION_REFERENCE_PREFIX.encode() + b"{}",
    og._SESSION_REFERENCE_PREFIX.encode() + b"{bad"])
def test_corrupted_grant_reference_is_sanitized(stores, runtime, monkeypatch, body):
    _, grants, owner, _ = stores
    gid = str(uuid.uuid4())
    ciphertext = body if body == b"not-fernet" else og._fernet().encrypt(body)
    with runtime.transaction() as tx:
        runtime.repositories.offline_grants.create_grant(
            tx, grant_id=gid, owner_id=owner, agent_id=None,
            encrypted_refresh_token=ciphertext,
            issued_at=og._now_ms(), expires_at=og._now_ms() + 60000)
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "https://idp.test/token")
    monkeypatch.setattr(og.aiohttp, "ClientSession", lambda **kw: pytest.fail("no HTTP"))
    with pytest.raises(og.OfflineGrantError, match="decrypted|malformed"):
        asyncio.run(grants.mint_access_token(gid, user_id=owner))


def test_legacy_conversion_cannot_revive_a_revoked_grant(stores, runtime, monkeypatch):
    _, grants, owner, _ = stores
    gid = str(uuid.uuid4())
    with runtime.transaction() as tx:
        runtime.repositories.offline_grants.create_grant(
            tx, grant_id=gid, owner_id=owner, agent_id=None,
            encrypted_refresh_token=og._fernet().encrypt(b"refresh-initial"),
            issued_at=og._now_ms(), expires_at=og._now_ms() + 60000)
    original = grants._session_reference

    def revoke_then_reference(*args):
        reference = original(*args)
        grants.revoke_for_user(owner)
        return reference

    monkeypatch.setattr(grants, "_session_reference", revoke_then_reference)
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "https://idp.test/token")
    with pytest.raises(og.OfflineGrantError, match="changed"):
        asyncio.run(grants.mint_access_token(gid, user_id=owner))
    assert not grants._grant(owner, gid).active


def test_deleted_session_grant_is_unusable_even_if_grant_row_is_live(stores, monkeypatch):
    sessions, grants, owner, sid = stores
    gid = grants.capture(owner, "refresh-initial")
    sessions.delete(sid)
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", "https://idp.test/token")
    with pytest.raises(og.OfflineGrantError, match="unavailable"):
        asyncio.run(grants.mint_access_token(gid, user_id=owner))


def test_ordinary_update_cannot_overwrite_an_unsettled_claim(stores):
    sessions, _, owner, sid = stores
    assert sessions._claim_refresh(sid, owner, None) is not None
    with pytest.raises(ss.SessionRefreshUnavailable, match="unsettled"):
        sessions.update_tokens(sid, access_token="stale", refresh_token="refresh-initial")


def test_entire_grant_path_has_a_hard_time_bound(stores, monkeypatch):
    _, grants, owner, _ = stores
    monkeypatch.setattr(og, "_GRANT_MINT_SECONDS", .02)

    async def stalled(*args, **kwargs):
        await asyncio.sleep(2)

    monkeypatch.setattr(grants, "_mint_access_token", stalled)
    with pytest.raises(og.OfflineGrantError, match="time limit"):
        asyncio.run(grants.mint_access_token(str(uuid.uuid4()), user_id=owner))


def test_browser_and_assignment_refresh_share_the_actual_canonical_family(stores, monkeypatch):
    sessions, grants, owner, sid = stores
    gid = grants.capture(owner, "refresh-initial")
    seen = []

    async def exchange(data):
        seen.append(data["refresh_token"])
        await asyncio.sleep(.02)
        return {"access_token": f"access-{len(seen)}",
                "refresh_token": f"refresh-{len(seen)}"}

    _idp(monkeypatch, exchange)

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, chunk_size):
            yield json.dumps(self.payload).encode()

    class Client:
        def __init__(self, *, timeout):
            assert timeout == 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @asynccontextmanager
        async def stream(self, method, url, *, data, follow_redirects):
            assert method == "POST" and not follow_redirects
            yield Response(await exchange(data))

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", Client)
    monkeypatch.setattr(web_auth, "_get_store", lambda: sessions)
    monkeypatch.setattr(web_auth, "_keycloak_config", lambda: (
        "https://idp.test/realm", "astral-frontend", ""))
    session = {"sid": sid, "sub": owner, "access_token": "access-initial",
               "refresh_token": "refresh-initial", "created_at": time.time()}
    monkeypatch.setattr(web_auth, "_SESSIONS", {sid: session})

    async def scenario():
        return await asyncio.gather(grants.mint_access_token(gid, user_id=owner),
                                    web_auth._refresh_session(sid, session))

    minted, browser = asyncio.run(scenario())
    assert minted in {"access-1", "access-2"}
    assert browser["access_token"] in {"access-1", "access-2"}
    assert seen == ["refresh-initial", "refresh-1"]
    # A subsequent cookie lookup cannot send a stale token to logout/refresh.
    assert web_auth._session_by_sid(sid)["refresh_token"] == "refresh-2"


def test_logout_uses_atomically_deleted_credential_not_the_process_cache(stores, runtime, monkeypatch):
    sessions, _, owner, sid = stores
    other = web_session_store(runtime)
    stale = dict(sessions.get(sid))

    async def exchange(*args):
        return {"access_token": "access-latest", "refresh_token": "refresh-latest"}

    asyncio.run(other.refresh_credential(sid, owner_id=owner, exchange=exchange))
    monkeypatch.setattr(web_auth, "_get_store", lambda: sessions)
    session = {"sub": owner, "access_token": stale["access_token"],
               "refresh_token": stale["refresh_token"]}
    asyncio.run(web_auth._kill_session(sid, session))
    assert session["refresh_token"] == "refresh-latest"
    assert sessions.get(sid) is None
    sessions._cache[sid] = stale
    assert sessions.delete(sid) is None


def test_keyless_decoder_never_returns_a_typed_claim(stores):
    sessions, _, _, _ = stores
    sessions._fernet = None
    assert sessions._dec(ss._REFRESH_CLAIM_PREFIX + "{}") == ""
    assert sessions._dec("plain-development-token") == "plain-development-token"
