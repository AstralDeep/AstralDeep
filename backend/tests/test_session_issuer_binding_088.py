"""Tests for issuer-bound session refresh and revocation
(orchestrator/session_authority.py, session_store.py, web_auth.py) against real
Plane, JWT policy, and synthetic HTTP: rotation, revocation-queue cycling, and claim
races.
"""

import asyncio
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from jose import jwt

from orchestrator import offline_grant as og, session_authority as sa, session_store as ss, web_auth
from tests.helpers.session_consent_088 import consent_from_store
from tests.helpers.session_plane_runtime import (
    get_session_record, isolated_plane_runtime, purge_revocations, replace_session_record,
    revocation_records, web_session_store,
)
from tests.test_request_session_authority_088 import signing_key as signing_key, request
from tests.test_operation_session_authority_088 import create_operation

ISSUER = "https://binding.invalid/realms/synthetic"
CLIENT = "astral-native"
REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("bound_session_088") as value:
        yield value


@pytest.fixture
def fixture(runtime, monkeypatch, signing_key):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", key)
    monkeypatch.setenv("WEB_SESSION_SECRET", "synthetic-binding-cookie-key")
    monkeypatch.setattr(og, "OFFLINE_GRANT_ENC_KEY", key)
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "synthetic-confidential-secret")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", CLIENT)
    monkeypatch.delenv("KEYCLOAK_TOKEN_URL", raising=False)
    owner, sid = str(uuid4()), uuid4().hex
    store = web_session_store(runtime)
    def token(**changes):
        claims = {"sub": owner, "iss": ISSUER, "azp": CLIENT, "aud": "account",
                  "exp": int(time.time()) + 300, "realm_access": {"roles": ["user"]}}
        claims.update(changes)
        return jwt.encode(claims, signing_key[0], algorithm="RS256",
                          headers={"kid": "synthetic-request-authority"})
    row = store.create(sid, user_id=owner, access_token=token(),
        refresh_token="synthetic-bound-refresh", hard_max_seconds=3600,
        issuing_issuer=ISSUER, issuing_client_id=CLIENT)
    wire = SimpleNamespace(requests=[], changes={}, payload=None, hook=None, keys_hook=None,
                           status=200, content=None)
    async def network(req):
        wire.requests.append((str(req.url), parse_qs(req.content.decode())))
        if wire.hook:
            await wire.hook()
        if wire.content is not None:
            return httpx.Response(wire.status, content=wire.content)
        return httpx.Response(wire.status, json=wire.payload if wire.payload is not None else
            {"access_token": token(**wire.changes), "refresh_token": "synthetic-rotated-refresh"})
    async def keys(*args, **kwargs):
        if wire.keys_hook:
            await wire.keys_hook()
        return signing_key[1]
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
        transport=httpx.MockTransport(network), **kw))
    monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
    monkeypatch.setattr(web_auth, "_get_store", lambda: store)
    yield store, owner, sid, token, wire, row
    og.OfflineGrantStore(plane_runtime=runtime).revoke_for_user(owner)
    store.delete_for_user(owner)
    purge_revocations(runtime, [owner])
    web_auth._SESSIONS.pop(sid, None)


async def forbidden(*args, **kwargs):
    pytest.fail("Bound session fell back to legacy exchange")


async def refresh(f, *, execution=False, **kwargs):
    store, owner, sid, _, _, _ = f
    callbacks = {"exchange": forbidden, "bound_exchange": web_auth._exchange_bound_session_refresh,
                 **kwargs}
    if execution:
        ref = await asyncio.to_thread(store.capture_execution_reference, owner_id=owner, session_id=sid)
        return await store.refresh_for_execution(ref, **callbacks)
    return await store.refresh_credential(sid, owner_id=owner, **callbacks)


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", [False, True])
async def test_verified_rotation_preserves_exact_pair_and_private_identity(fixture, runtime, execution):
    store, owner, sid, _, wire, row = fixture
    before = get_session_record(runtime, sid)
    result = await refresh(fixture, execution=execution)
    after = get_session_record(runtime, sid)
    assert before.incarnation_id == after.incarnation_id == row["incarnation_id"]
    assert (after.issuing_issuer, after.issuing_client_id) == (ISSUER, CLIENT)
    assert before.refresh_token_ciphertext != after.refresh_token_ciphertext
    assert store.get(sid)["refresh_token"] == "synthetic-rotated-refresh"
    assert wire.requests == [(ISSUER + "/protocol/openid-connect/token",
        {"grant_type": ["refresh_token"], "refresh_token": ["synthetic-bound-refresh"], "client_id": [CLIENT]})]
    if execution:
        assert result.credential == store._sessions.repository.execution_fence(after)
    identity = ss.SessionIssuingIdentity(owner, ISSUER, CLIENT)
    assert owner not in repr(identity) and ISSUER not in repr(identity)


@pytest.mark.asyncio
async def test_acreate_update_and_exact_delete_preserve_pair(fixture, runtime):
    store, owner, _, token, _, _ = fixture
    sid = uuid4().hex
    row = await store.acreate(sid, user_id=owner, access_token=token(), refresh_token="refresh",
        hard_max_seconds=3600, issuing_issuer=ISSUER, issuing_client_id=CLIENT)
    store.update_tokens(sid, access_token=token(), refresh_token="new-refresh",
                        expected_incarnation_id=row["incarnation_id"])
    assert (get_session_record(runtime, sid).issuing_issuer,
            get_session_record(runtime, sid).issuing_client_id) == (ISSUER, CLIENT)
    retired = await store.adelete(sid, expected_incarnation_id=row["incarnation_id"])
    assert retired["issuing_issuer"] == ISSUER and retired["issuing_client_id"] == CLIENT


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", [False, True])
async def test_unavailable_bound_callback_refuses_before_claim(fixture, runtime, execution):
    before = get_session_record(runtime, fixture[2])
    with pytest.raises(ss.SessionRefreshUnavailable, match="bound session refresh unavailable"):
        await refresh(fixture, execution=execution, bound_exchange=None)
    assert get_session_record(runtime, fixture[2]) == before and not fixture[4].requests


@pytest.mark.parametrize("issuer,client", [(ISSUER,None), (None,CLIENT), ("",CLIENT),
    (" " + ISSUER,CLIENT), (ISSUER,CLIENT+"\n"), (ISSUER,"x"*257),
    ("x"*2049,CLIENT), (ISSUER,False), (ISSUER,"a\ud800")])
def test_invalid_pair_never_inserts_or_queues(fixture, runtime, issuer, client):
    store, owner, _, _, _, _ = fixture
    sid = uuid4().hex
    with pytest.raises(ss.SessionStoreError):
        store.create(sid, user_id=owner, access_token="access", refresh_token="refresh",
                     hard_max_seconds=3600, issuing_issuer=issuer, issuing_client_id=client)
    assert get_session_record(runtime, sid) is None
    if issuer is not None:
        with pytest.raises(ss.SessionStoreError):
            store.enqueue_revocation(owner, "refresh", client_id=client, issuing_issuer=issuer)
        assert not revocation_records(runtime, owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,value", [("KEYCLOAK_AUTHORITY", "https://other.invalid/realm"),
    ("KEYCLOAK_ALLOWED_AZP", ""), ("USE_MOCK_AUTH", "true")])
async def test_changed_destination_sends_no_refresh_and_does_not_persist_new_token(fixture, runtime, monkeypatch, setting, value):
    before = get_session_record(runtime, fixture[2])
    monkeypatch.setenv(setting, value)
    with pytest.raises(ss.SessionRefreshUnavailable):
        await refresh(fixture)
    after = get_session_record(runtime, fixture[2])
    assert not fixture[4].requests
    assert after.access_token_ciphertext == before.access_token_ciphertext
    assert fixture[0]._dec(after.refresh_token_ciphertext) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{"sub":"foreign"}, {"iss":"https://other.invalid"},
    {"azp":"astral-web"}, {"azp":None}, {"iss":None}, {"exp":1}, {"exp":None},
    {"exp":True}, {"exp":"99999999999"}, {"exp":float("inf")},
    {"realm_access":{"roles":[]}}, {"act":{"sub":"agent"}}, {"aud":"astral-mcp"}])
async def test_bad_returned_identity_never_persists(fixture, runtime, claims, caplog):
    before = get_session_record(runtime, fixture[2])
    fixture[4].changes = claims
    with pytest.raises(ss.SessionRefreshUnavailable, match="bound session response unavailable"):
        await refresh(fixture)
    after = get_session_record(runtime, fixture[2])
    assert len(fixture[4].requests) == 1
    assert after.access_token_ciphertext == before.access_token_ciphertext
    assert fixture[0]._dec(after.refresh_token_ciphertext) == ""
    assert fixture[3]() not in caplog.text and "synthetic-bound-refresh" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"access_token":""}, {"access_token":"opaque"},
    {"access_token":"a b"}, {"access_token":"opaque", "refresh_token":None}])
async def test_malformed_response_never_persists(fixture, runtime, payload):
    before = get_session_record(runtime, fixture[2])
    fixture[4].payload = payload
    with pytest.raises(ss.SessionRefreshUnavailable):
        await refresh(fixture)
    assert get_session_record(runtime, fixture[2]).access_token_ciphertext == before.access_token_ciphertext


@pytest.mark.asyncio
async def test_configuration_changed_during_jwt_await_refuses_persistence(fixture, runtime, monkeypatch):
    before = get_session_record(runtime, fixture[2])
    async def change():
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://changed.invalid/realm")
    fixture[4].keys_hook = change
    with pytest.raises(ss.SessionRefreshUnavailable):
        await refresh(fixture)
    assert get_session_record(runtime, fixture[2]).access_token_ciphertext == before.access_token_ciphertext


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["http", "jwt"])
async def test_same_sid_replacement_during_external_await_is_never_adopted(fixture, runtime, boundary):
    before = get_session_record(runtime, fixture[2])
    replacements = []
    async def change():
        replacements.append(await asyncio.to_thread(replace_session_record, runtime, before))
    setattr(fixture[4], "hook" if boundary == "http" else "keys_hook", change)
    with pytest.raises(ss.SessionStoreError):
        await refresh(fixture)
    assert get_session_record(runtime, fixture[2]) == replacements[0]
    assert replacements[0].incarnation_id != before.incarnation_id


@pytest.mark.asyncio
async def test_cancelled_unknown_exchange_is_not_retried(fixture, runtime):
    entered = asyncio.Event()
    async def wait():
        entered.set()
        await asyncio.Event().wait()
    fixture[4].hook = wait
    task = asyncio.create_task(refresh(fixture))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = get_session_record(runtime, fixture[2])
    assert fixture[0]._dec(after.refresh_token_ciphertext) == ""
    with pytest.raises(ss.SessionRefreshUnavailable):
        await refresh(fixture, execution=True)
    assert len(fixture[4].requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["web", "request", "operation", "work", "grant"])
async def test_explicit_consumers_use_bound_transport(fixture, runtime, monkeypatch, consumer):
    store, owner, sid, token, wire, row = fixture
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", forbidden)
    if consumer == "web":
        result = await web_auth._refresh_session(sid, web_auth._session_from_row(row))
        assert result["refresh_token"] == "synthetic-rotated-refresh"
    elif consumer == "request":
        result = await sa.refresh_web_execution_authority(request(sid), principal={"sub":owner})
        store.assert_execution_observation(result)
    elif consumer == "operation":
        record = create_operation(fixture[:5], runtime)
        result = await sa.refresh_operation_execution_authority(owner_id=owner,
            assignment_id=record.assignment_id, sessions=store, plane_runtime=runtime)
        assert result.claims["azp"] == CLIENT
    elif consumer == "work":
        from orchestrator import work_submit_authority as wa
        req = request(sid, headers=[(b"origin",b"https://app.invalid"),(b"content-type",b"application/json")])
        selected = await wa.authenticate_work_submission_request(req, sessions=store, plane_runtime=runtime)
        result = await wa.refresh_work_submission_authority(selected, sessions=store)
        assert result.observation.credential.issuing_client_id == CLIENT
    else:
        grants = og.OfflineGrantStore(plane_runtime=runtime)
        gid = grants.capture(owner, consent_from_store(store, owner, sid))
        result = await grants.mint_access_token(gid, user_id=owner)
        assert jwt.get_unverified_claims(result)["azp"] == CLIENT
    assert len(wire.requests) == 1 and "client_secret" not in wire.requests[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before", "http", "after"])
async def test_bound_grant_retains_pre_post_revocation_checks(fixture, runtime, monkeypatch, boundary):
    store, owner, sid, _, wire, _ = fixture
    grants = og.OfflineGrantStore(plane_runtime=runtime)
    gid = grants.capture(owner, consent_from_store(store, owner, sid))
    monkeypatch.setattr(grants, "_sessions", lambda: store)
    if boundary == "before":
        original = store._claim_refresh
        def claim(*args, **kwargs):
            value = original(*args, **kwargs)
            grants.revoke_for_user(owner)
            return value
        monkeypatch.setattr(store, "_claim_refresh", claim)
    elif boundary == "http":
        async def revoke():
            await asyncio.to_thread(grants.revoke_for_user, owner)
        wire.hook = revoke
    else:
        original = store._settle_refresh
        def settle(*args, **kwargs):
            value = original(*args, **kwargs)
            grants.revoke_for_user(owner)
            return value
        monkeypatch.setattr(store, "_settle_refresh", settle)
    before = get_session_record(runtime, sid)
    with pytest.raises(og.OfflineGrantError, match="revoked"):
        await grants.mint_access_token(gid, user_id=owner)
    after = get_session_record(runtime, sid)
    assert len(wire.requests) == (0 if boundary == "before" else 1)
    assert (after.access_token_ciphertext == before.access_token_ciphertext) == (boundary != "after")


@pytest.mark.asyncio
async def test_exact_retirement_and_queued_issuer_never_follow_changed_realm(fixture, runtime, monkeypatch):
    store, owner, sid, _, wire, row = fixture
    sess = web_auth._session_from_row(row)
    assert await web_auth._kill_session(sid, sess)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://replacement.invalid/realm")
    assert await web_auth._revoke_session_or_queue(sess) == "queued"
    assert not wire.requests
    pending = revocation_records(runtime, owner)
    assert len(pending) == 1 and pending[0].issuing_issuer == ISSUER and pending[0].client_id == CLIENT
    assert await web_auth.process_revocation_queue_once() == 0
    assert revocation_records(runtime, owner)[0].attempts == 1 and not wire.requests
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISSUER)
    assert await web_auth.process_revocation_queue_once() == 1
    assert revocation_records(runtime, owner) == ()
    assert wire.requests == [(ISSUER + "/protocol/openid-connect/revoke",
        {"token": ["synthetic-bound-refresh"], "token_type_hint": ["refresh_token"], "client_id": [CLIENT]})]


@pytest.mark.asyncio
async def test_revocation_explicit_web_secret_and_invalid_pair_no_fallback(fixture, monkeypatch):
    wire = fixture[4]
    assert await web_auth._revoke_refresh_token("refresh", client_id="astral-web", issuing_issuer=ISSUER)
    assert wire.requests[0][1]["client_secret"] == ["synthetic-confidential-secret"]
    wire.requests.clear()
    assert await web_auth._revoke_session_or_queue({"sub":fixture[1],"refresh_token":"secret",
        "issuing_issuer":ISSUER,"issuing_client_id":None}) == "failed"
    assert not await web_auth._revoke_refresh_token("refresh", client_id=CLIENT, issuing_issuer="")
    assert not wire.requests


@pytest.mark.asyncio
async def test_legacy_callbacks_and_queue_remain_two_argument(fixture, runtime):
    store, owner, _, token, wire, _ = fixture
    sid = uuid4().hex
    row = store.create(sid, user_id=owner, access_token=token(), refresh_token="legacy", hard_max_seconds=3600)
    seen = []
    async def old(refresh, access):
        seen.append((refresh,access))
        return {"access_token":token(),"refresh_token":"legacy-rotated"}
    result = await store.refresh_credential(sid, owner_id=owner, exchange=old, bound_exchange=forbidden)
    assert seen == [("legacy",row["access_token"])] and result["issuing_issuer"] is None
    await store.aenqueue_revocation(owner,"legacy-rotated",client_id=CLIENT)
    assert revocation_records(runtime,owner)[0].issuing_issuer is None
    assert await web_auth.process_revocation_queue_once() == 1
    assert len(wire.requests) == 1


@pytest.mark.parametrize("kind", ["old-type", "dropped-pair", "queue-dropped-pair"])
def test_incompatible_storage_never_commits_unbound_credential(fixture, runtime, monkeypatch, kind):
    store, owner, _, token, _, _ = fixture
    sid = uuid4().hex
    if kind == "old-type":
        monkeypatch.setattr(ss, "SessionRecord", object)
    else:
        repo = store._revocations.repository if kind == "queue-dropped-pair" else store._sessions.repository
        name = "enqueue" if kind == "queue-dropped-pair" else "put"
        original = getattr(repo, name)
        def discard(*args, **kwargs):
            row = original(*args, **kwargs)
            return replace(row, issuing_issuer=None) if kind == "queue-dropped-pair" else replace(
                row, issuing_issuer=None, issuing_client_id=None)
        monkeypatch.setattr(repo, name, discard)
    with pytest.raises(ss.SessionStoreError, match="storage unavailable"):
        if kind == "queue-dropped-pair":
            store.enqueue_revocation(owner, "refresh", client_id=CLIENT, issuing_issuer=ISSUER)
        else:
            store.create(sid, user_id=owner, access_token=token(), refresh_token="refresh",
                         hard_max_seconds=3600, issuing_issuer=ISSUER, issuing_client_id=CLIENT)
    assert get_session_record(runtime, sid) is None and not revocation_records(runtime, owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before-claim", "claimed-result"])
async def test_actual_claim_pair_is_rechecked_before_dispatch(fixture, runtime, monkeypatch, boundary):
    store, _, sid, _, wire, _ = fixture
    before = get_session_record(runtime, sid)
    if boundary == "before-claim":
        original = store._refresh_record
        reads = []
        def read(*args, **kwargs):
            value = original(*args, **kwargs)
            reads.append(value)
            return replace(value, issuing_client_id="astral-web") if len(reads) > 1 else value
        monkeypatch.setattr(store, "_refresh_record", read)
    else:
        original = store._claim_refresh
        def claimed(*args, **kwargs):
            record, refresh_token, access = original(*args, **kwargs)
            return replace(record, issuing_client_id="astral-web"), refresh_token, access
        monkeypatch.setattr(store, "_claim_refresh", claimed)
    with pytest.raises(ss.SessionRefreshUnavailable, match="issuing identity changed"):
        await refresh(fixture)
    after = get_session_record(runtime, sid)
    assert not wire.requests and before.access_token_ciphertext == after.access_token_ciphertext
    if boundary == "before-claim":
        assert after == before


@pytest.mark.asyncio
async def test_bound_web_client_secret_is_sent_only_to_exact_client(fixture, runtime):
    store, _, sid, _, wire, _ = fixture
    old = get_session_record(runtime, sid)
    replace_session_record(runtime, replace(old, issuing_client_id="astral-web"))
    wire.changes = {"azp":"astral-web"}
    await refresh(fixture)
    assert wire.requests[0][1]["client_secret"] == ["synthetic-confidential-secret"]
    assert wire.requests[0][1]["client_id"] == ["astral-web"]
    assert store.get(sid)["issuing_client_id"] == "astral-web"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["oversize", "redirect", "http", "json", "transport", "timeout"])
async def test_bound_transport_failure_leaves_claim_without_retry(fixture, runtime, monkeypatch, failure):
    wire = fixture[4]
    before = get_session_record(runtime, fixture[2])
    if failure == "oversize":
        wire.content = b"x" * 65537
    elif failure in {"redirect", "http"}:
        wire.status = 302 if failure == "redirect" else 503
    elif failure == "json":
        wire.content = b"not-json"
    else:
        async def fail():
            if failure == "transport":
                raise httpx.ReadError("synthetic transport unavailable")
            await asyncio.Event().wait()
        wire.hook = fail
        monkeypatch.setattr(ss, "REFRESH_WAIT_SECONDS", .2)
    with pytest.raises((ss.SessionStoreError, httpx.HTTPError, ValueError)):
        await refresh(fixture)
    after = get_session_record(runtime, fixture[2])
    assert len(wire.requests) == 1 and before.access_token_ciphertext == after.access_token_ciphertext
    assert fixture[0]._dec(after.refresh_token_ciphertext) == ""


@pytest.mark.asyncio
async def test_original_incarnation_waiting_on_real_claim_lock_cannot_adopt_replacement(fixture, runtime, monkeypatch):
    store, _, sid, _, wire, _ = fixture
    original = store._sessions.repository.compare_and_set_refresh
    entered = threading.Event()
    def observed(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(store._sessions.repository, "compare_and_set_refresh", observed)
    selected = consent_from_store(store, fixture[1], sid)
    task = None
    try:
        with runtime.transaction() as tx:
            store._sessions.repository.assert_current_consent(tx, observation=selected.observation)
            record = store._sessions.repository.get_by_session_id_for_administration(tx, session_id=sid)
            task = asyncio.create_task(refresh(fixture))
            assert await asyncio.to_thread(entered.wait, 2)
            store._sessions.repository.delete(tx, owner_id=record.owner_id, session_id=sid,
                                               expected_incarnation_id=record.incarnation_id)
            new = store._sessions.repository.put(tx, replace(record, incarnation_id=None))
        with pytest.raises(ss.SessionStoreError):
            await asyncio.wait_for(task, 3)
        assert get_session_record(runtime, sid) == new and not wire.requests
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 503, 400, 401])
async def test_normal_web_distinguishes_explicit_refusal_from_unknown_status(fixture, runtime, status):
    store, _, sid, _, wire, row = fixture
    wire.status = status
    result = await web_auth._refresh_session(sid, web_auth._session_from_row(row))
    after = get_session_record(runtime, sid)
    if status in {400, 401}:
        assert result is None and after is None
    else:
        assert result["access_token"] == row["access_token"] and result["refresh_token"] == ""
        assert after is not None and store._dec(after.refresh_token_ciphertext) == ""
    assert len(wire.requests) == 1


@pytest.mark.asyncio
async def test_bad_returned_identity_cannot_delete_normal_session(fixture, runtime):
    store, _, sid, _, wire, row = fixture
    wire.changes = {"sub":"foreign-owner"}
    result = await web_auth._refresh_session(sid, web_auth._session_from_row(row))
    assert result["access_token"] == row["access_token"] and result["refresh_token"] == ""
    assert store._dec(get_session_record(runtime, sid).refresh_token_ciphertext) == ""


@pytest.mark.asyncio
async def test_bound_grant_bad_jwt_is_closed_and_retains_claim(fixture, runtime):
    store, owner, sid, _, wire, _ = fixture
    grants = og.OfflineGrantStore(plane_runtime=runtime)
    gid = grants.capture(owner, consent_from_store(store, owner, sid))
    wire.changes = {"sub":"foreign-owner"}
    with pytest.raises(og.OfflineGrantError, match="session refresh unavailable"):
        await grants.mint_access_token(gid, user_id=owner)
    assert store._dec(get_session_record(runtime, sid).refresh_token_ciphertext) == ""
    assert len(wire.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 503])
async def test_bound_revocation_status_cannot_discard_queue(fixture, runtime, status):
    store, owner, _, _, wire, _ = fixture
    wire.status = status
    await store.aenqueue_revocation(owner,"refresh",client_id=CLIENT,issuing_issuer=ISSUER)
    assert await web_auth.process_revocation_queue_once() == 0
    rows = revocation_records(runtime,owner)
    assert len(rows) == 1 and rows[0].attempts == 1
    assert len(wire.requests) == 1


@pytest.mark.asyncio
async def test_bound_revocation_total_deadline_keeps_retryable_credential(fixture, runtime, monkeypatch):
    store, owner, _, _, wire, _ = fixture
    actual_timeout = asyncio.timeout
    durations = []
    def short(seconds):
        durations.append(seconds)
        return actual_timeout(.05)
    monkeypatch.setattr(web_auth.asyncio,"timeout",short)
    async def wait():
        await asyncio.Event().wait()
    wire.hook = wait
    assert await web_auth._revoke_or_queue(owner,"refresh",client_id=CLIENT,issuing_issuer=ISSUER) == "queued"
    assert durations == [10] and len(wire.requests) == 1
    assert revocation_records(runtime,owner)[0].issuing_issuer == ISSUER


@pytest.mark.asyncio
async def test_removed_bound_client_never_transports_queued_credential(fixture, runtime, monkeypatch):
    store, owner, _, _, wire, _ = fixture
    await store.aenqueue_revocation(owner,"refresh",client_id=CLIENT,issuing_issuer=ISSUER)
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP","")
    assert await web_auth.process_revocation_queue_once() == 0
    assert not wire.requests and revocation_records(runtime,owner)[0].attempts == 1


@pytest.mark.asyncio
async def test_bound_revocation_never_reads_remote_body_or_redirects(fixture, monkeypatch):
    requested = []
    class UnreadableBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Revocation must not consume a remote response body")
            yield b""
    async def network(req):
        requested.append(str(req.url))
        return httpx.Response(204, stream=UnreadableBody())
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw: REAL_CLIENT(
        transport=httpx.MockTransport(network), **kw))
    assert await web_auth._revoke_refresh_token("refresh", client_id=CLIENT, issuing_issuer=ISSUER)
    assert requested == [ISSUER+"/protocol/openid-connect/revoke"]


@pytest.mark.asyncio
async def test_untyped_bound_identity_is_not_a_refresh_destination(fixture):
    with pytest.raises(ss.SessionRefreshUnavailable):
        await web_auth._exchange_bound_session_refresh("refresh", (fixture[1],ISSUER,CLIENT))
    assert not fixture[4].requests


def queued(runtime, store, owner, *, count=1, issuer=ISSUER, attempts=0, timestamp=1, ciphertext=None):
    repo = runtime.repositories.revocations
    rows = []
    with runtime.transaction() as tx:
        for _ in range(count):
            row = repo.enqueue(tx, owner_id=owner, client_id=CLIENT, issuing_issuer=issuer,
                refresh_token_ciphertext=ciphertext or store._enc("synthetic-queued-refresh"), enqueued_at=timestamp)
            for attempt in range(attempts):
                row = repo.bump_attempt(tx, owner_id=owner, queue_id=row.queue_id, expected_attempts=attempt)
            rows.append(row)
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["realm", "client", "503", "ciphertext"])
async def test_bound_threshold_never_acknowledges_unconfirmed_revocation(fixture, runtime, monkeypatch, reason):
    store, owner, _, _, wire, _ = fixture
    row = queued(runtime, store, owner, attempts=30,
                 ciphertext="synthetic-invalid-ciphertext" if reason == "ciphertext" else None)[0]
    if reason == "realm":
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://new.invalid/realm")
    elif reason == "client":
        monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "")
    else:
        wire.status = 503
    assert await web_auth.process_revocation_queue_once() == 0
    assert revocation_records(runtime, owner) == (row,)
    assert len(wire.requests) == (1 if reason == "503" else 0)
    if reason != "ciphertext":
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISSUER)
        monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", CLIENT)
        wire.status = 200
        assert await web_auth.process_revocation_queue_once() == 1
        assert not revocation_records(runtime, owner)


@pytest.mark.asyncio
async def test_retained_prefix_does_not_starve_later_eligible_revocation(fixture, runtime, monkeypatch):
    store, owner, _, _, wire, _ = fixture
    held = queued(runtime, store, owner, count=20, attempts=30)
    later = queued(runtime, store, owner, issuer=None, timestamp=2)[0]
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://new.invalid/realm")
    assert await web_auth.process_revocation_queue_once() == 0
    assert not wire.requests and len(revocation_records(runtime, owner)) == 21
    assert len(store.pending_revocations()) == 20
    assert await web_auth.process_revocation_queue_once() == 1
    assert revocation_records(runtime, owner) == tuple(held)
    assert len(wire.requests) == 1 and wire.requests[0][0].startswith("https://new.invalid/")
    assert later.queue_id not in {r.queue_id for r in revocation_records(runtime, owner)}
    assert store._revocation_cursor is None and store._revocation_ceiling is None


@pytest.mark.asyncio
async def test_cycle_ceiling_wraps_despite_new_backdated_enqueues(fixture, runtime, monkeypatch):
    store, owner, _, _, wire, _ = fixture
    queued(runtime, store, owner, count=21, attempts=30, timestamp=10)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://new.invalid/realm")
    assert await web_auth.process_revocation_queue_once() == 0
    ceiling = store._revocation_ceiling
    inserted = queued(runtime, store, owner, issuer=None, timestamp=0)[0]
    assert inserted.queue_id > ceiling
    assert await web_auth.process_revocation_queue_once() == 0
    assert not wire.requests and store._revocation_cursor is None
    assert await web_auth.process_revocation_queue_once() == 1
    assert len(wire.requests) == 1
    assert inserted.queue_id not in {r.queue_id for r in revocation_records(runtime, owner)}


@pytest.mark.asyncio
async def test_separate_store_drainers_have_independent_cycles(fixture, runtime):
    first, owner, _, _, _, _ = fixture
    records = queued(runtime, first, owner, count=21, attempts=30)
    second = web_session_store(runtime)
    async with first.revocation_pass() as a:
        assert [x["id"] for x in a] == [x.queue_id for x in records[:20]]
    async with second.revocation_pass() as b:
        assert [x["id"] for x in b] == [x.queue_id for x in records[:20]]
    async with first.revocation_pass() as a:
        assert [x["id"] for x in a] == [records[-1].queue_id]
    assert first._revocation_cursor is None and second._revocation_cursor is not None
    async with second.revocation_pass() as b:
        assert [x["id"] for x in b] == [records[-1].queue_id]


@pytest.mark.asyncio
async def test_cancelled_page_worker_cannot_late_advance_another_pass(fixture, runtime, monkeypatch):
    store, owner, _, _, _, _ = fixture
    rows = queued(runtime, store, owner, count=21, attempts=30)
    original = store._revocations.repository.page_for_administration
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    first = True
    def delayed(*args, **kwargs):
        nonlocal first
        value = original(*args, **kwargs)
        if first:
            first = False
            entered.set()
            try:
                assert release.wait(3)
            finally:
                completed.set()
        return value
    monkeypatch.setattr(store._revocations.repository, "page_for_administration", delayed)
    async def read():
        async with store.revocation_pass() as items:
            return items
    task = asyncio.create_task(read())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store._revocation_cursor is None and store._revocation_ceiling is None
        assert [x["id"] for x in await read()] == [x.queue_id for x in rows[:20]]
        current = (store._revocation_cursor,store._revocation_ceiling)
        release.set()
        assert await asyncio.to_thread(completed.wait, 2)
        assert (store._revocation_cursor,store._revocation_ceiling) == current
        assert [x["id"] for x in await read()] == [rows[-1].queue_id]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        assert await asyncio.to_thread(completed.wait, 3)


@pytest.mark.asyncio
async def test_missing_paging_capability_and_read_failure_refuse_without_transport(fixture, runtime, monkeypatch):
    store, owner, _, _, wire, _ = fixture
    row = queued(runtime,store,owner)[0]
    monkeypatch.setattr(store._revocations.repository,"page_for_administration",None)
    assert await web_auth.process_revocation_queue_once() == 0
    assert not wire.requests and revocation_records(runtime,owner) == (row,)
    assert store.pending_revocations()[0]["id"] == row.queue_id
    assert store._revocation_cursor is None


@pytest.mark.asyncio
async def test_revocation_mutation_failure_is_not_reported_as_success(fixture,runtime,monkeypatch):
    store,owner,_,_,wire,_ = fixture
    row = queued(runtime,store,owner)[0]
    async def failed(_):
        raise ss.SessionStoreError("synthetic acknowledgement unavailable")
    monkeypatch.setattr(store,"aresolve_revocation",failed)
    with pytest.raises(ss.SessionStoreError,match="acknowledgement"):
        await web_auth.process_revocation_queue_once()
    assert len(wire.requests) == 1 and revocation_records(runtime,owner) == (row,)


def logout_audits(monkeypatch, runtime):
    calls = []
    async def audit(action, owner, description, *, outcome="success"):
        calls.append((action, owner, description, outcome))
    async def no_machine_credentials(*args):
        return None
    monkeypatch.setattr(web_auth, "_audit", audit)
    monkeypatch.setattr(web_auth, "_destroy_machine_credentials", no_machine_credentials)
    monkeypatch.setattr(og, "get_offline_grant_store", lambda: og.OfflineGrantStore(plane_runtime=runtime))
    return calls


def retirement_request(sid=None, **kwargs):
    value = request(sid, **kwargs)
    value.scope["app"] = SimpleNamespace(state=SimpleNamespace())
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("disposition", ["confirmed", "queued", "failed", "empty"])
async def test_logout_distinguishes_retirement_from_remote_disposition(
    fixture, runtime, monkeypatch, bound, disposition,
):
    store, owner, sid, _, wire, _ = fixture
    record = get_session_record(runtime, sid)
    if not bound:
        record = replace_session_record(runtime, replace(record, issuing_issuer=None, issuing_client_id=None))
    if disposition == "empty":
        store.update_tokens(sid, access_token=store._dec(record.access_token_ciphertext),
                            refresh_token="", expected_incarnation_id=record.incarnation_id)
    wire.status = 200 if disposition == "confirmed" else 503
    if disposition == "failed":
        async def unavailable(*args, **kwargs):
            raise ss.SessionStoreError("synthetic queue unavailable")
        monkeypatch.setattr(store, "aenqueue_revocation", unavailable)
    calls = logout_audits(monkeypatch, runtime)
    response = await web_auth.auth_logout(retirement_request(sid))
    assert response.status_code == 303 and get_session_record(runtime, sid) is None
    remote = disposition if disposition in ("confirmed", "queued") else "unconfirmed"
    assert calls == [("logout", owner, "User signed out; local session retired; "
                     f"remote refresh credential revocation {remote}", "success")]
    assert len(revocation_records(runtime, owner)) == (1 if disposition == "queued" else 0)
    assert len(wire.requests) == (0 if disposition == "empty" else 1)


@pytest.mark.asyncio
async def test_bound_malformed_retired_ciphertext_is_unconfirmed_without_transport(fixture, runtime, monkeypatch):
    store, owner, sid, _, wire, _ = fixture
    record = get_session_record(runtime, sid)
    with runtime.transaction() as tx:
        repository = runtime.repositories.history.sessions
        repository.compare_and_set_refresh(tx,
            record=replace(record, refresh_token_ciphertext="synthetic-unreadable-ciphertext",
                           last_refresh_at=record.last_refresh_at + 1),
            expected_last_refresh_at=record.last_refresh_at,
            expected_credential=repository.execution_fence(record))
    calls = logout_audits(monkeypatch, runtime)
    assert (await web_auth.auth_logout(retirement_request(sid))).status_code == 303
    assert get_session_record(runtime, sid) is None
    assert not wire.requests and not revocation_records(runtime, owner)
    assert calls[0][2].endswith("remote refresh credential revocation unconfirmed")


@pytest.mark.asyncio
async def test_logout_during_actual_bound_refresh_never_revokes_or_queues_claim_marker(
    fixture, runtime, monkeypatch,
):
    store, owner, sid, _, wire, _ = fixture
    entered, release = asyncio.Event(), asyncio.Event()
    async def held():
        entered.set()
        await release.wait()
    wire.hook = held
    calls = logout_audits(monkeypatch, runtime)
    task = asyncio.create_task(refresh(fixture))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        claimed = get_session_record(runtime, sid)
        assert store._dec(claimed.refresh_token_ciphertext) == ""
        assert (await web_auth.auth_logout(retirement_request(sid))).status_code == 303
        assert get_session_record(runtime, sid) is None
        assert len(wire.requests) == 1 and not revocation_records(runtime, owner)
        assert calls[0][2].endswith("remote refresh credential revocation unconfirmed")
        release.set()
        with pytest.raises(ss.SessionRefreshUnavailable):
            await asyncio.wait_for(task, 2)
        assert get_session_record(runtime, sid) is None and len(wire.requests) == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_token", ["", "bad token", ss._REFRESH_CLAIM_PREFIX + "marker"])
async def test_bound_unusable_retirement_input_is_never_queued(fixture, runtime, refresh_token):
    store, owner, _, _, wire, _ = fixture
    assert await web_auth._revoke_or_queue(owner, refresh_token, client_id=CLIENT,
                                           issuing_issuer=ISSUER) == "failed"
    assert not wire.requests and not revocation_records(runtime, owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_callback_retirement_preserves_legacy_default_and_exact_bound_client(
    fixture, runtime, monkeypatch, bound,
):
    from fastapi.responses import RedirectResponse
    _, owner, sid, _, wire, _ = fixture
    if not bound:
        replace_session_record(runtime, replace(get_session_record(runtime, sid),
                               issuing_issuer=None, issuing_client_id=None))
    wire.changes = {"sub": str(uuid4())}
    logout_audits(monkeypatch, runtime)
    monkeypatch.setattr(web_auth, "_establish_session", lambda *args: RedirectResponse("/", status_code=303))
    state = uuid4().hex
    web_auth._PENDING[state] = {"code_verifier": "v" * 43, "created_at": time.time(), "next": "/"}
    cookies = f"{web_auth.COOKIE_NAME}={web_auth._sign(sid)}; {web_auth.STATE_COOKIE_NAME}={web_auth._sign(state)}"
    try:
        response = await web_auth.auth_callback(retirement_request(headers=[(b"cookie", cookies.encode())],
            query=f"code=synthetic-code&state={state}".encode(), method="GET"))
        assert response.status_code == 303 and get_session_record(runtime, sid) is None
        assert len(wire.requests) == 2
        endpoint, body = wire.requests[-1]
        assert endpoint == ISSUER + "/protocol/openid-connect/revoke"
        assert body["client_id"] == [CLIENT if bound else "astral-web"]
        assert ("client_secret" in body) == (not bound)
        assert not revocation_records(runtime, owner)
    finally:
        web_auth._PENDING.pop(state, None)
