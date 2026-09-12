"""088 private forced-refresh adapter against real Plane and normal JWT policy."""
import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from jose import jwk, jwt

from orchestrator import session_authority as sa, session_store as ss, web_auth
from tests.helpers.session_plane_runtime import (
    get_session_record, isolated_plane_runtime, replace_session_record, web_session_store,
)

REAL_EXCHANGE = web_auth._exchange_session_refresh


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("request_authority") as value:
        yield value


@pytest.fixture(scope="module")
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    public_jwk = jwk.construct(public, algorithm="RS256").to_dict()
    public_jwk["kid"] = "synthetic-request-authority"
    return private, {"keys": [public_jwk]}


@pytest.fixture
def fixture(runtime, monkeypatch, signing_key):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEB_SESSION_SECRET", "synthetic-private-cookie-key")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://request-authority.invalid/realm")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-native")
    owner, sid = str(uuid.uuid4()), uuid.uuid4().hex
    store = web_session_store(runtime)

    def token(**changes):
        claims = {"sub": owner, "iss": "https://request-authority.invalid/realm",
                  "azp": "astral-web", "aud": "account", "exp": int(time.time()) + 300,
                  "realm_access": {"roles": ["user"]}, "sid": "not-the-cookie-sid"}
        claims.update(changes)
        return jwt.encode(claims, signing_key[0], algorithm="RS256",
                          headers={"kid": "synthetic-request-authority"})

    store.create(sid, user_id=owner, access_token=token(),
                 refresh_token="synthetic-initial-refresh", hard_max_seconds=3600)
    monkeypatch.setattr(web_auth, "_get_store", lambda: store)
    seen = []

    async def exchange(refresh, prior_access):
        seen.append((refresh, prior_access))
        return {"access_token": token(), "refresh_token": "synthetic-rotated-refresh"}

    async def keys(*args, **kwargs):
        return signing_key[1]

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
    yield store, owner, sid, token, seen
    store.delete(sid)


def request(sid=None, *, headers=(), query=b"", method="POST"):
    values = list(headers)
    if sid is not None:
        values.append((b"cookie", f"astral_session={web_auth._sign(sid)}".encode()))
    return Request({"type": "http", "method": method, "path": "/unregistered-test",
                    "query_string": query, "headers": values, "scheme": "https",
                    "server": ("app.invalid", 443), "state": {"untouched": True}})


def run(fixture, req=None, principal=None):
    _, owner, sid, _, _ = fixture
    return asyncio.run(sa.refresh_web_execution_authority(
        req or request(sid), principal={"sub": owner} if principal is None else principal))


def unavailable(call):
    with pytest.raises(sa.SessionAuthorityUnavailable) as caught:
        call()
    assert str(caught.value) == "session_authority_unavailable"


def test_valid_refresh_persists_then_verifies_exact_generation(fixture, runtime):
    store, owner, sid, _, seen = fixture
    before = get_session_record(runtime, sid)
    req = request(sid)
    observation = run(fixture, req)
    after = get_session_record(runtime, sid)
    assert len(seen) == 1 and seen[0][0] == "synthetic-initial-refresh"
    assert after.last_refresh_at > before.last_refresh_at
    assert observation.credential == store._sessions.repository.execution_fence(after)
    assert observation.credential.owner_id == owner and observation.credential.session_id == sid
    assert observation.valid_until - observation.started_at == timedelta(seconds=15)
    assert req.scope["state"] == {"untouched": True}
    assert "synthetic" not in repr(observation)
    store.assert_execution_observation(observation)


@pytest.mark.parametrize("kind", ["missing", "forged", "overlong", "duplicate", "bearer",
                                  "empty-bearer", "query", "empty-query", "options", "invalid-sid"])
def test_only_unambiguous_signed_cookie_selects_authority(fixture, kind):
    _, _, sid, _, seen = fixture
    kwargs = {}
    if kind == "missing":
        sid = None
    elif kind == "forged":
        sid = None
        kwargs["headers"] = [(b"cookie", b"astral_session=forged.bad")]
    elif kind == "overlong":
        sid = "x" * 257
    elif kind == "invalid-sid":
        sid = "contains.dot"
    elif kind == "duplicate":
        kwargs["headers"] = [(b"cookie", f"astral_session={web_auth._sign(sid)}".encode())]
    elif kind in {"bearer", "empty-bearer"}:
        kwargs["headers"] = [(b"authorization", b"Bearer anything" if kind == "bearer" else b"")]
    elif kind in {"query", "empty-query"}:
        kwargs["query"] = b"token=anything" if kind == "query" else b"token="
    else:
        kwargs["method"] = "OPTIONS"
    unavailable(lambda: run(fixture, request(sid, **kwargs)))
    assert seen == []


@pytest.mark.parametrize("principal", [{}, {"sub": ""}, {"sub": 1}, {"sub": "x" * 257},
                                        {"sub": "other-owner"}, []])
def test_missing_or_other_owner_refuses_before_remote(fixture, principal):
    unavailable(lambda: run(fixture, principal=principal))
    assert fixture[-1] == []


@pytest.mark.parametrize("mode", ["true", "1", "yes", " TRUE "])
def test_mock_posture_cannot_mint_observation(fixture, monkeypatch, mode):
    monkeypatch.setenv("USE_MOCK_AUTH", mode)
    unavailable(lambda: run(fixture))
    assert fixture[-1] == []


def test_current_signed_sid_is_used_even_with_newer_owner_session(fixture, runtime):
    store, owner, sid, _, seen = fixture
    other = uuid.uuid4().hex
    store.create(other, user_id=owner, access_token="other-access", refresh_token="other-refresh",
                 hard_max_seconds=3600)
    try:
        before = get_session_record(runtime, other)
        assert run(fixture).credential.session_id == sid
        assert seen[0][0] == "synthetic-initial-refresh"
        assert get_session_record(runtime, other) == before
    finally:
        store.delete(other)


@pytest.mark.parametrize("changes", [
    {"sub": "other-owner"}, {"iss": "https://wrong.invalid"}, {"azp": "untrusted"},
    {"realm_access": {"roles": []}}, {"act": {"sub": "agent"}}, {"aud": "astral-mcp"},
    {"exp": 1}, {"exp": None}, {"exp": "9999999999"}, {"exp": True},
    {"exp": float("inf")},
])
def test_fresh_token_still_passes_normal_iam_and_expiry_policy(fixture, monkeypatch, changes):
    _, _, _, token, seen = fixture

    async def exchange(refresh, prior):
        seen.append(refresh)
        return {"access_token": token(**changes)}

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    req = request(fixture[2])
    unavailable(lambda: run(fixture, req))
    assert len(seen) == 1
    assert req.scope["state"] == {"untouched": True}


def test_access_expiry_shortens_observation(fixture, monkeypatch):
    _, _, _, token, _ = fixture
    expiry = time.time() + 8

    async def exchange(*args):
        return {"access_token": token(exp=expiry)}

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    assert run(fixture).valid_until.timestamp() == pytest.approx(expiry, abs=0.000001)


def test_ciphertext_replacement_before_claim_does_not_adopt_generation(fixture, runtime):
    store, owner, sid, _, seen = fixture
    reference = store.capture_execution_reference(owner_id=owner, session_id=sid)
    original = get_session_record(runtime, sid)
    replacement = replace(original, access_token_ciphertext=store._enc("replacement-access"),
                          refresh_token_ciphertext=store._enc("replacement-refresh"))
    replace_session_record(runtime, replacement)
    with pytest.raises(ss.SessionRefreshUnavailable):
        asyncio.run(store.refresh_for_execution(reference, exchange=web_auth._exchange_session_refresh))
    assert get_session_record(runtime, sid) == replacement and seen == []


def test_conflict_between_initial_read_and_claim_is_not_retried(fixture, runtime, monkeypatch):
    store, _, sid, _, seen = fixture
    repository = store._sessions.repository
    original = repository.assert_current_execution
    replacement = None

    def replace_before_guard(transaction, **kwargs):
        nonlocal replacement
        replacement = replace(get_session_record(runtime, sid),
                              refresh_token_ciphertext=store._enc("replacement"))
        replace_session_record(runtime, replacement)
        return original(transaction, **kwargs)

    monkeypatch.setattr(repository, "assert_current_execution", replace_before_guard)
    unavailable(lambda: run(fixture))
    assert get_session_record(runtime, sid) == replacement and seen == []


def test_active_refresh_claim_is_never_waited_out_or_replayed(fixture):
    store, owner, sid, _, seen = fixture

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def first_exchange(*args):
            entered.set()
            await release.wait()
            return {"access_token": "ordinary-rotated-access"}

        first = asyncio.create_task(store.refresh_credential(sid, owner_id=owner, exchange=first_exchange))
        await entered.wait()
        try:
            with pytest.raises(sa.SessionAuthorityUnavailable):
                await sa.refresh_web_execution_authority(request(sid), principal={"sub": owner})
            assert seen == []
        finally:
            release.set()
            await first

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["delete", "replace", "retire"])
def test_remote_completion_cannot_resurrect_or_authorize_replaced_owner(fixture, runtime, monkeypatch, change):
    store, owner, sid, token, seen = fixture
    replacement = None

    async def exchange(*args):
        nonlocal replacement
        seen.append("called")
        if change == "delete":
            await store.adelete(sid)
        elif change == "retire":
            with runtime.transaction() as transaction:
                runtime.repositories.assignments.retire_operations_for_owner(transaction, owner_id=owner)
        else:
            replacement = replace(get_session_record(runtime, sid),
                                  refresh_token_ciphertext=store._enc("replacement-refresh"))
            replace_session_record(runtime, replacement)
        return {"access_token": token(), "refresh_token": "old-family-result"}

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    unavailable(lambda: run(fixture))
    assert seen == ["called"]
    if change == "replace":
        assert get_session_record(runtime, sid) == replacement
    elif change == "delete":
        assert get_session_record(runtime, sid) is None


def test_rotation_during_jwt_verification_invalidates_final_observation(fixture, monkeypatch, signing_key):
    store, _, sid, _, _ = fixture

    async def keys(*args, **kwargs):
        await asyncio.to_thread(store.update_tokens, sid,
                                access_token="newer-access", refresh_token="newer-refresh")
        return signing_key[1]

    monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
    unavailable(lambda: run(fixture))
    assert store.get(sid)["access_token"] == "newer-access"


def test_expired_pre_remote_db_sample_is_not_restarted(fixture, monkeypatch):
    store, _, _, _, seen = fixture
    capture = store.capture_execution_reference

    def old_reference(**kwargs):
        reference = capture(**kwargs)
        return replace(reference, state=replace(reference.state,
                       observed_at=reference.state.observed_at - timedelta(seconds=16)))

    monkeypatch.setattr(store, "capture_execution_reference", old_reference)
    unavailable(lambda: run(fixture))
    assert seen == []


def test_db_time_is_checked_after_session_lock_wait(fixture, runtime):
    store, owner, sid, _, seen = fixture
    reference = store.capture_execution_reference(owner_id=owner, session_id=sid)
    reference = replace(reference, state=replace(reference.state,
                        observed_at=reference.state.observed_at - timedelta(seconds=14)))
    entered = threading.Event()
    before = get_session_record(runtime, sid)

    def hold():
        with runtime.transaction() as transaction:
            transaction.fetch_one("SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (sid,))
            entered.set()
            time.sleep(1.2)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(ss.SessionRefreshUnavailable):
            asyncio.run(store.refresh_for_execution(reference, exchange=web_auth._exchange_session_refresh))
    finally:
        worker.join(5)
    assert not worker.is_alive()
    assert get_session_record(runtime, sid) == before and seen == []


@pytest.mark.parametrize("failure", ["timeout", "transport", "malformed", "cancel"])
def test_uncertain_refresh_never_returns_old_access_or_replays(fixture, monkeypatch, failure):
    store, _, sid, _, seen = fixture

    async def exchange(*args):
        seen.append("attempt")
        if failure == "timeout":
            raise TimeoutError("PRIVATE")
        if failure == "transport":
            raise RuntimeError("PRIVATE")
        if failure == "cancel":
            raise asyncio.CancelledError
        return {"access_token": "bad token PRIVATE"}

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            run(fixture)
    else:
        unavailable(lambda: run(fixture))
    unavailable(lambda: run(fixture))
    assert seen == ["attempt"]
    assert store.get(sid)["refresh_token"] == ""


def test_exchange_runs_without_holding_session_transaction(fixture, runtime, monkeypatch):
    _, _, sid, token, _ = fixture

    async def exchange(*args):
        def lock_independently():
            with runtime.transaction() as transaction:
                transaction.fetch_one("SELECT sid FROM web_session WHERE sid=%s FOR UPDATE NOWAIT", (sid,))
        await asyncio.to_thread(lock_independently)
        return {"access_token": token()}

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    assert run(fixture).credential.session_id == sid


def test_no_plaintext_development_store_or_missing_runtime(fixture, monkeypatch):
    store = fixture[0]
    monkeypatch.setattr(store, "_fernet", None)
    unavailable(lambda: run(fixture))
    monkeypatch.setattr(web_auth, "_get_store", lambda: None)
    unavailable(lambda: run(fixture))
    assert fixture[-1] == []


@pytest.mark.parametrize("reference", [None, {}, ss.WebSessionReference(None)])
def test_store_requires_typed_request_reference(fixture, reference):
    with pytest.raises(ss.SessionRefreshUnavailable):
        asyncio.run(fixture[0].refresh_for_execution(reference, exchange=web_auth._exchange_session_refresh))
    assert fixture[-1] == []


@pytest.mark.parametrize("status,body", [(200, b"not-json"), (200, b"x" * 65537),
                                       (302, b""), (401, b"PRIVATE"), (503, b"PRIVATE")])
def test_shared_exchange_bounds_failure_without_fallback(fixture, monkeypatch, status, body):
    calls = []
    real_client = httpx.AsyncClient

    async def respond(req):
        calls.append(parse_qs(req.content.decode())["refresh_token"])
        return httpx.Response(status, content=body, headers={"location": "https://other.invalid"})

    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", client)
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", REAL_EXCHANGE)
    unavailable(lambda: run(fixture))
    assert calls == [["synthetic-initial-refresh"]]
    assert fixture[0].get(fixture[2])["refresh_token"] == ""


def test_real_bounded_exchange_and_normal_jwt_verification(fixture, monkeypatch):
    store, _, sid, token, _ = fixture
    real_client = httpx.AsyncClient
    calls = []

    async def respond(req):
        data = parse_qs(req.content.decode())
        calls.append(data)
        assert str(req.url) == "https://request-authority.invalid/realm/protocol/openid-connect/token"
        return httpx.Response(200, json={"access_token": token(), "refresh_token": "rotated-http"})

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", REAL_EXCHANGE)
    assert run(fixture).credential.session_id == sid
    assert len(calls) == 1
    assert calls[0]["client_id"] == ["astral-web"]
    assert calls[0]["client_secret"] == ["synthetic-secret"]
    assert store.get(sid)["refresh_token"] == "rotated-http"


def test_missing_keycloak_configuration_refuses_without_fallback(fixture, monkeypatch):
    monkeypatch.setattr(web_auth, "_keycloak_config", lambda: ("", "", ""))
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", REAL_EXCHANGE)
    unavailable(lambda: run(fixture))


def test_cookie_context_change_during_remote_validation_refuses(fixture, monkeypatch):
    _, _, sid, token, _ = fixture
    req = request(sid)

    async def exchange(*args):
        # An enclosing request adapter must not repurpose this captured context.
        req.scope["headers"][:] = [(b"cookie", b"astral_session=another.bad")]
        return {"access_token": token()}

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    unavailable(lambda: run(fixture, req))


def test_remote_elapsed_time_cannot_extend_original_db_deadline(fixture, monkeypatch):
    store, _, sid, token, _ = fixture
    capture = store.capture_execution_reference

    def old_reference(**kwargs):
        reference = capture(**kwargs)
        return replace(reference, state=replace(reference.state,
                       observed_at=reference.state.observed_at - timedelta(seconds=14)))

    async def exchange(*args):
        await asyncio.sleep(1.2)
        return {"access_token": token(), "refresh_token": "persisted-late-rotation"}

    monkeypatch.setattr(store, "capture_execution_reference", old_reference)
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    unavailable(lambda: run(fixture))
    # Rotation must remain persisted even when the observation has expired.
    assert store.get(sid)["refresh_token"] == "persisted-late-rotation"


def test_total_timeout_cancels_slow_exchange_and_leaves_claim(fixture, monkeypatch):
    store, _, sid, _, seen = fixture
    cancelled = []

    async def exchange(*args):
        seen.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(ss, "REFRESH_WAIT_SECONDS", .05)
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    unavailable(lambda: run(fixture))
    assert seen == ["started"] and cancelled == [True]
    assert store.get(sid)["refresh_token"] == ""


def test_cancellation_during_persistence_never_returns_authority(fixture, monkeypatch):
    store, owner, sid, _, seen = fixture
    original = store._settle_refresh_record
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def held_settlement(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(store, "_settle_refresh_record", held_settlement)

    async def scenario():
        task = asyncio.create_task(sa.refresh_web_execution_authority(
            request(sid), principal={"sub": owner}))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(completed.wait, 5)

    asyncio.run(scenario())
    assert len(seen) == 1
    assert store.get(sid)["refresh_token"] == "synthetic-rotated-refresh"


def _wait_operation(fixture, stage):
    store, owner, sid, _, _ = fixture
    state = store.capture_execution_reference(owner_id=owner, session_id=sid).state
    observation = sa.SessionExecutionObservation(
        state.credential, state.observed_at, state.observed_at + timedelta(seconds=15))
    if stage == "capture":
        return lambda: store.capture_execution_reference(owner_id=owner, session_id=sid)
    if stage == "claim":
        return lambda: store._claim_refresh(sid, owner, None, execution=observation)
    if stage == "settle":
        claimed, refresh, access = store._claim_refresh(sid, owner, None)
        return lambda: store._settle_refresh_record(
            claimed, access, refresh, None, request_execution=True)
    assert stage == "assert"
    return lambda: store.assert_execution_observation(observation)


@pytest.mark.parametrize("stage,lock_kind", [
    ("capture", "table"), ("claim", "table"), ("claim", "owner"), ("claim", "session"),
    ("settle", "table"), ("settle", "owner"), ("settle", "session"),
    ("assert", "table"), ("assert", "owner"), ("assert", "session"),
])
def test_request_sql_wait_ends_and_releases_pool_while_blocker_is_still_held(
    fixture, runtime, stage, lock_kind,
):
    store, owner, sid, _, seen = fixture
    operation = _wait_operation(fixture, stage)
    before = get_session_record(runtime, sid)
    with runtime.transaction() as blocker:
        if lock_kind == "table":
            blocker.execute("LOCK TABLE web_session IN ACCESS EXCLUSIVE MODE")
        elif lock_kind == "owner":
            blocker.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner,))
        else:
            blocker.fetch_one("SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (sid,))
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(ss.SessionRefreshUnavailable) as caught:
                executor.submit(operation).result(timeout=3)
        assert str(caught.value) == "session execution database unavailable"
        assert runtime._pool.snapshot.borrowed == 1  # The blocker is still inside its transaction.
        assert store._sessions.repository.get(blocker, owner_id=owner, session_id=sid) == before
        with runtime.transaction() as probe:
            assert probe.fetch_one("SELECT 1 AS alive")["alive"] == 1
            if lock_kind != "owner":
                assert probe.fetch_one(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,79)) AS acquired",
                    (owner,),
                )["acquired"]
    assert seen == []


def test_initial_execution_read_has_a_statement_bound(fixture, runtime, monkeypatch):
    store, owner, sid, _, seen = fixture

    def slow_read(transaction, **kwargs):
        transaction.fetch_one("SELECT pg_sleep(10)")
        pytest.fail("request statement deadline was not enforced")

    monkeypatch.setattr(store._sessions.repository, "get_execution_state", slow_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(ss.SessionRefreshUnavailable):
            executor.submit(store.capture_execution_reference,
                            owner_id=owner, session_id=sid).result(timeout=3)
    assert runtime._pool.snapshot.borrowed == 0 and seen == []


def test_missing_request_wait_api_refuses_without_changing_ordinary_refresh(fixture, monkeypatch):
    store, owner, sid, _, seen = fixture
    monkeypatch.setattr(store._sessions.repository, "bound_request_execution_waits", None)
    unavailable(lambda: run(fixture))
    assert seen == []
    row = asyncio.run(store.refresh_credential(
        sid, owner_id=owner, exchange=web_auth._exchange_session_refresh))
    assert row["user_id"] == owner and row["refresh_token"] == "synthetic-rotated-refresh"
    assert len(seen) == 1


def test_outer_timeout_leaves_no_worker_or_owner_lock_after_database_wait_bound(
    fixture, runtime, monkeypatch,
):
    store, owner, sid, _, seen = fixture
    owner_locked, worker_finished = threading.Event(), threading.Event()
    original_lock = store._sessions.repository._lock_execution_owner
    original_claim = store._claim_refresh

    def observed_owner_lock(transaction, selected_owner):
        original_lock(transaction, selected_owner)
        owner_locked.set()

    def observed_claim(*args, **kwargs):
        try:
            return original_claim(*args, **kwargs)
        finally:
            worker_finished.set()

    monkeypatch.setattr(store._sessions.repository, "_lock_execution_owner", observed_owner_lock)
    monkeypatch.setattr(store, "_claim_refresh", observed_claim)

    async def scenario():
        task = asyncio.create_task(sa.refresh_web_execution_authority(
            request(sid), principal={"sub": owner}))
        try:
            assert await asyncio.to_thread(owner_locked.wait, 2)
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.01):
                    await task
            assert task.cancelled()
            assert await asyncio.to_thread(worker_finished.wait, 2)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    with runtime.transaction() as blocker:
        blocker.fetch_one("SELECT sid FROM web_session WHERE sid=%s FOR UPDATE", (sid,))
        asyncio.run(scenario())
        assert runtime._pool.snapshot.borrowed == 1
        with runtime.transaction() as probe:
            assert probe.fetch_one(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,79)) AS acquired", (owner,),
            )["acquired"]
    assert seen == []
