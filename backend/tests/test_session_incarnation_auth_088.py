"""088: original issued-session observations survive delayed auth work."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Response

from orchestrator import web_auth

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


def session_row(incarnation=A):
    """Return synthetic equal-content rows differing only by issued identity."""
    return {"sid": "same-sid", "user_id": "owner", "access_token": "access",
            "refresh_token": "refresh", "created_at": 100,
            "interactive_anchor": 100, "hard_expires_at": 9999999999,
            "last_refresh_at": 100, "resumed": False,
            "incarnation_id": incarnation}


def web_session(incarnation=A):
    """Return a private cache observation, never an authentication bypass."""
    return {**session_row(incarnation), "sub": "owner"}


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    monkeypatch.setattr(web_auth, "_SESSIONS", {})
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://issuer.invalid")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "web")


def test_read_returns_actual_durable_incarnation(monkeypatch):
    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(get=lambda sid: session_row()))
    assert web_auth._session_by_sid("same-sid")["incarnation_id"] == A


def test_reading_replacement_does_not_mutate_original_observation(monkeypatch):
    original = web_session()
    web_auth._SESSIONS["same-sid"] = original
    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(get=lambda sid: session_row(B)))
    current = web_auth._session_by_sid("same-sid")
    assert current is not original
    assert current["incarnation_id"] == B
    assert original["incarnation_id"] == A


def test_attach_caches_returned_issued_row_without_changing_cookie_payload(monkeypatch):
    returned = session_row()
    monkeypatch.setattr(web_auth.secrets, "token_urlsafe", lambda length: "same-sid")
    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(create=lambda *args, **kwargs: returned))
    monkeypatch.setattr(web_auth, "_cookie_secure", lambda request: True)
    response = Response()
    sid = web_auth._attach_session(SimpleNamespace(), {"sub": "owner", "access_token": "access", "refresh_token": "refresh"}, response)
    assert sid == "same-sid"
    assert web_auth._SESSIONS[sid]["incarnation_id"] == A
    assert web_auth._SESSIONS[sid]["created_at"] == returned["interactive_anchor"]
    assert A not in response.headers["set-cookie"]
    assert web_auth._unsign(response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]) == sid


def test_late_refresh_refusal_does_not_delete_or_cache_evict_same_sid_replacement(monkeypatch):
    original, replacement = web_session(), web_session(B)
    web_auth._SESSIONS["same-sid"] = original

    class Store:
        current = session_row()

        async def refresh_credential(self, sid, **kwargs):
            self.current = session_row(B)
            web_auth._SESSIONS[sid] = replacement
            response = httpx.Response(401, request=httpx.Request("POST", "https://issuer.invalid/token"))
            raise httpx.HTTPStatusError("synthetic refusal", request=response.request, response=response)

        async def adelete(self, sid, *, expected_incarnation_id=None):
            if self.current is None or (expected_incarnation_id is not None and self.current["incarnation_id"] != expected_incarnation_id):
                return None
            previous, self.current = self.current, None
            return previous

    store = Store()
    monkeypatch.setattr(web_auth, "_get_store", lambda: store)

    async def audit(*args, **kwargs):
        pass

    monkeypatch.setattr(web_auth, "_audit", audit)
    assert asyncio.run(web_auth._refresh_session("same-sid", original)) is None
    assert store.current == session_row(B)
    assert web_auth._SESSIONS["same-sid"] is replacement
    assert replacement["refresh_token"] == "refresh"
    assert original["incarnation_id"] == A


@pytest.fixture(scope="module")
def runtime():
    from tests.helpers.session_plane_runtime import isolated_plane_runtime
    with isolated_plane_runtime("session_incarnation_auth") as value:
        yield value


@pytest.fixture
def issued(runtime, monkeypatch):
    from uuid import uuid4
    from cryptography.fernet import Fernet
    from tests.helpers.session_plane_runtime import web_session_store
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    store = web_session_store(runtime)
    sid, owner = f"sid-{uuid4()}", f"owner-{uuid4()}"
    row = store.create(sid, user_id=owner, access_token="synthetic-access",
                       refresh_token="synthetic-refresh", hard_max_seconds=3600)
    monkeypatch.setattr(web_auth, "_get_store", lambda: store)
    yield store, sid, owner, row
    store.delete_for_user(owner)


def replacement(runtime, sid):
    """Reissue identical durable bytes through real owner-scoped Plane APIs."""
    from dataclasses import replace
    from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
    original = get_session_record(runtime, sid)
    current = replace_session_record(runtime, original)
    assert current.incarnation_id != original.incarnation_id
    assert replace(current, incarnation_id=original.incarnation_id) == original
    return current


def test_real_issuance_restart_and_exact_owner_incarnation_reference(issued, runtime):
    from tests.helpers.session_plane_runtime import get_session_record, web_session_store
    store, sid, owner, row = issued
    record = get_session_record(runtime, sid)
    assert row["incarnation_id"] == record.incarnation_id
    assert web_session_store(runtime).get(sid)["incarnation_id"] == record.incarnation_id
    assert store.session_reference(owner, session_id=sid, incarnation_id=record.incarnation_id) == {
        "session_id": sid, "incarnation_id": record.incarnation_id,
        "created_at": record.created_at, "interactive_anchor": record.interactive_anchor,
    }


@pytest.mark.parametrize("wrong", ["owner", "sid", "incarnation", "malformed", "missing"])
def test_exact_reference_never_adopts_latest_or_foreign_session(issued, wrong):
    from orchestrator.session_store import SessionRefreshUnavailable
    store, sid, owner, row = issued
    with pytest.raises(SessionRefreshUnavailable):
        store.session_reference("different-owner" if wrong == "owner" else owner,
                                session_id="different-sid" if wrong == "sid" else sid,
                                incarnation_id={"incarnation": A, "malformed": "invalid", "missing": None}.get(wrong, row["incarnation_id"]))


def test_claim_retry_cannot_adopt_recreated_identical_session(issued, runtime, monkeypatch):
    from orchestrator.session_store import SessionRefreshUnavailable
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, owner, row = issued
    repository = store._sessions.repository
    original = repository.compare_and_set_refresh
    replacements, calls = [], []

    def before_claim(transaction, record, **kwargs):
        replacements.append(replacement(runtime, sid))
        return original(transaction, record, **kwargs)

    async def exchange(*args):
        calls.append(args)
        pytest.fail("replacement cannot inherit the old consumer's refresh")

    monkeypatch.setattr(repository, "compare_and_set_refresh", before_claim)
    with pytest.raises(SessionRefreshUnavailable):
        asyncio.run(store.refresh_credential(sid, owner_id=owner, exchange=exchange,
                                             expected_incarnation_id=row["incarnation_id"]))
    assert len(replacements) == 1 and calls == []
    assert get_session_record(runtime, sid) == replacements[0]


def test_held_refresh_cannot_settle_into_identical_recreated_claim(issued, runtime):
    from orchestrator.session_store import SessionRefreshUnavailable
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, owner, row = issued
    references, calls = [], []

    async def exchange(*args):
        calls.append(args)
        references.append(await asyncio.to_thread(replacement, runtime, sid))
        return {"access_token": "old-result", "refresh_token": "old-rotation"}

    with pytest.raises(SessionRefreshUnavailable):
        asyncio.run(store.refresh_credential(sid, owner_id=owner, exchange=exchange,
                                             expected_incarnation_id=row["incarnation_id"]))
    assert calls == [("synthetic-refresh", "synthetic-access")]
    assert get_session_record(runtime, sid) == references[0]


@pytest.mark.parametrize("method", ["delete", "mark_resumed", "update_tokens"])
def test_delayed_mutations_keep_the_initial_expected_incarnation(issued, runtime, method):
    from orchestrator.session_store import SessionRefreshUnavailable
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, _, row = issued
    current = replacement(runtime, sid)
    if method == "update_tokens":
        with pytest.raises(SessionRefreshUnavailable):
            store.update_tokens(sid, access_token="old-result", refresh_token="old-result",
                                expected_incarnation_id=row["incarnation_id"])
    else:
        assert getattr(store, method)(sid, expected_incarnation_id=row["incarnation_id"]) is None
    assert get_session_record(runtime, sid) == current


def test_read_then_delete_cas_returns_no_replacement_credential(issued, runtime, monkeypatch):
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, _, row = issued
    repository = store._sessions.repository
    original = repository.delete_and_return
    replacements = []

    def before_delete(transaction, **kwargs):
        current = replacement(runtime, sid)
        replacements.append(current)
        store._cache[sid] = store._from_record(current)
        return original(transaction, **kwargs)

    monkeypatch.setattr(repository, "delete_and_return", before_delete)
    assert store.delete(sid, expected_incarnation_id=row["incarnation_id"]) is None
    assert get_session_record(runtime, sid) == replacements[0]
    assert store._cache[sid]["incarnation_id"] == replacements[0].incarnation_id


def test_real_late_http401_cannot_delete_replacement_or_return_its_credential(issued, runtime, monkeypatch):
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, _, _ = issued
    selected = web_auth._session_by_sid(sid)
    current = []

    async def exchange(*args):
        current.append(await asyncio.to_thread(replacement, runtime, sid))
        await asyncio.to_thread(web_auth._session_by_sid, sid)
        response = httpx.Response(401, request=httpx.Request("POST", "https://issuer.invalid/token"))
        raise httpx.HTTPStatusError("synthetic refusal", request=response.request, response=response)

    async def audit(*args, **kwargs):
        pass

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    monkeypatch.setattr(web_auth, "_audit", audit)
    assert asyncio.run(web_auth._refresh_session(sid, selected)) is None
    assert get_session_record(runtime, sid) == current[0]
    assert web_auth._SESSIONS[sid]["incarnation_id"] == current[0].incarnation_id
    assert selected["incarnation_id"] != current[0].incarnation_id


def test_v1_execution_fence_is_refused_before_oauth(issued):
    from dataclasses import replace
    from orchestrator.session_store import SessionRefreshUnavailable
    store, sid, owner, _ = issued
    reference = store.capture_execution_reference(owner_id=owner, session_id=sid)
    assert reference.state.credential.version == 2
    reference = replace(reference, state=replace(reference.state,
        credential=replace(reference.state.credential, version=1)))

    async def exchange(*args):
        pytest.fail("legacy execution fences must not cause OAuth traffic")

    with pytest.raises(SessionRefreshUnavailable):
        asyncio.run(store.refresh_for_execution(reference, exchange=exchange))


@pytest.mark.parametrize("bad", [None, {}, {"session_id": "same-sid", "created_at": 1, "interactive_anchor": 1}])
def test_legacy_reference_or_missing_expected_id_cannot_refresh_selected_b(issued, runtime, bad):
    from orchestrator.session_store import SessionRefreshUnavailable
    store, sid, owner, row = issued
    replacement(runtime, sid)

    async def exchange(*args):
        pytest.fail("no request may adopt the replacement's OAuth credential")

    with pytest.raises(SessionRefreshUnavailable):
        asyncio.run(store.refresh_credential(sid, owner_id=owner, exchange=exchange,
            reference=bad, expected_incarnation_id=row["incarnation_id"]))


def test_stale_logout_does_not_revoke_replacement_or_owner_credentials(issued, runtime, monkeypatch):
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, _, _ = issued
    selected = web_auth._session_by_sid(sid)
    current = replacement(runtime, sid)
    request = SimpleNamespace(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)}, base_url="https://app.invalid/")

    async def selected_before_wait(_sid):
        return selected

    async def forbidden(*args, **kwargs):
        pytest.fail("stale logout cannot revoke replacement or owner credentials")

    monkeypatch.setattr(web_auth, "_asession_by_sid", selected_before_wait)
    for name in ("_end_voice_session", "_revoke_or_queue", "_destroy_machine_credentials", "_audit"):
        monkeypatch.setattr(web_auth, name, forbidden)
    response = asyncio.run(web_auth.auth_logout(request))
    assert response.status_code == 303
    assert 'Max-Age=0' in response.headers['set-cookie']
    assert selected["refresh_token"] == ""
    assert get_session_record(runtime, sid) == current


@pytest.mark.parametrize("flow", ["callback", "kiosk"])
def test_user_switch_retains_prior_incarnation_across_identity_provider_wait(issued, runtime, monkeypatch, flow):
    from orchestrator import device_login
    from tests.helpers.session_plane_runtime import get_session_record
    _, sid, _, _ = issued
    current, attached, revoked = [], [], []
    request = SimpleNamespace(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)},
                              query_params={"code": "synthetic-code", "state": "bound-state"},
                              base_url="https://app.invalid/", client=None)

    async def finish_remote(*args, **kwargs):
        current.append(await asyncio.to_thread(replacement, runtime, sid))
        await asyncio.to_thread(web_auth._session_by_sid, sid)
        return {"access_token": "new-owner-access", "refresh_token": "new-owner-refresh"}

    async def no_audit(*args, **kwargs):
        pass

    async def revoke(*args, **kwargs):
        revoked.append(args)

    monkeypatch.setattr(web_auth, "_sub_from_jwt", lambda token: "new-owner")
    monkeypatch.setattr(web_auth, "_audit", no_audit)
    monkeypatch.setattr(web_auth, "_end_voice_session", revoke)
    monkeypatch.setattr(web_auth, "_revoke_or_queue", revoke)
    monkeypatch.setattr(web_auth, "_attach_session", lambda request, payload, response: attached.append(payload))
    if flow == "callback":
        monkeypatch.setattr(web_auth, "_PENDING", {"bound-state": {"code_verifier": "synthetic", "next": "/"}})
        monkeypatch.setattr(web_auth, "_state_is_bound", lambda *args: True)
        monkeypatch.setattr(web_auth, "_roles_from_token", lambda *args: ["user"])

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                payload = await finish_remote()
                return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://issuer.invalid/token"))

        monkeypatch.setattr(web_auth.httpx, "AsyncClient", Client)
        response = asyncio.run(web_auth.auth_callback(request))
    else:
        monkeypatch.setattr(web_auth, "_kiosk_flow_handle", lambda request: "owned-flow")

        async def poll(*args):
            return {"status": "approved", "tokens": await finish_remote()}

        monkeypatch.setattr(device_login, "poll", poll)
        response = asyncio.run(web_auth.kiosk_poll(request))
    assert response.status_code in (200, 303)
    assert len(attached) == 1 and revoked == []
    assert get_session_record(runtime, sid) == current[0]
    assert web_auth._SESSIONS[sid]["incarnation_id"] == current[0].incarnation_id


@pytest.mark.parametrize("read_kind", ["get", "latest_refresh_token_for"])
def test_decrypt_cleanup_cannot_delete_recreated_invalid_ciphertext(issued, runtime, monkeypatch, read_kind):
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, owner, _ = issued
    repository = store._sessions.repository
    original_delete = repository.delete
    current = []
    monkeypatch.setattr(store, "_dec", lambda value: "")

    def delete_after_replacement(transaction, **kwargs):
        monkeypatch.setattr(repository, "delete", original_delete)
        current.append(replacement(runtime, sid))
        store._cache[sid] = {"sid": sid, "incarnation_id": current[0].incarnation_id}
        return original_delete(transaction, **kwargs)

    monkeypatch.setattr(repository, "delete", delete_after_replacement)
    assert getattr(store, read_kind)(sid if read_kind == "get" else owner) is None
    assert get_session_record(runtime, sid) == current[0]
    assert store._cache[sid]["incarnation_id"] == current[0].incarnation_id


def test_expiry_cleanup_retains_original_incarnation(issued, runtime, monkeypatch):
    from dataclasses import replace
    from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
    store, sid, _, _ = issued
    expired = replace_session_record(runtime, replace(get_session_record(runtime, sid), hard_expires_at=1))
    real_delete = store.delete
    current = []

    def delayed_delete(value, **kwargs):
        assert kwargs["expected_incarnation_id"] == expired.incarnation_id
        current.append(replace_session_record(runtime, replace(expired, hard_expires_at=9999999999)))
        store._cache[sid] = store._from_record(current[0])
        return real_delete(value, **kwargs)

    monkeypatch.setattr(store, "delete", delayed_delete)
    assert store.get(sid) is None
    assert get_session_record(runtime, sid) == current[0]
    assert store._cache[sid]["incarnation_id"] == current[0].incarnation_id


def test_out_of_order_durable_read_never_overwrites_newer_cache(issued, runtime, monkeypatch):
    store, sid, _, _ = issued
    original = store._from_record
    current = []

    def delayed_decode(record):
        current.append(replacement(runtime, sid))
        store._cache[sid] = original(current[0])
        return original(record)

    monkeypatch.setattr(store, "_from_record", delayed_decode)
    old = store.get(sid)
    assert old["incarnation_id"] != current[0].incarnation_id
    assert store._cache[sid]["incarnation_id"] == current[0].incarnation_id


def test_missing_read_cannot_evict_later_web_cache(monkeypatch):
    replacement = web_session(B)
    web_auth._SESSIONS["same-sid"] = web_session()

    def delayed_missing(sid):
        web_auth._SESSIONS[sid] = replacement
        return None

    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(get=delayed_missing))
    assert web_auth._session_by_sid("same-sid") is None
    assert web_auth._SESSIONS["same-sid"] is replacement


@pytest.mark.parametrize("incarnation", [None, "invalid", "11111111-1111-1111-8111-111111111111"])
def test_refresh_without_an_issued_identity_never_calls_exchange(monkeypatch, incarnation):
    async def forbidden(*args, **kwargs):
        pytest.fail("a cache entry cannot invent a durable issuance")

    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(refresh_credential=forbidden))
    selected = web_session(incarnation)
    result = asyncio.run(web_auth._refresh_session("same-sid", selected))
    assert result is None


def test_refresher_cannot_return_a_different_issuance(monkeypatch):
    async def different(*args, **kwargs):
        return session_row(B)

    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(refresh_credential=different))
    assert asyncio.run(web_auth._refresh_session("same-sid", web_session())) is None


def test_ambiguous_delete_never_returns_a_revocation_credential(monkeypatch):
    selected = web_session()

    async def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic unavailable database")

    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(adelete=unavailable))
    assert asyncio.run(web_auth._kill_session("same-sid", selected)) is False
    assert selected["refresh_token"] == ""


def test_delayed_attach_does_not_overwrite_newer_cached_incarnation(monkeypatch):
    current = web_session(B)

    def create(*args, **kwargs):
        web_auth._SESSIONS["same-sid"] = current
        return session_row()

    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(create=create))
    monkeypatch.setattr(web_auth.secrets, "token_urlsafe", lambda length: "same-sid")
    monkeypatch.setattr(web_auth, "_cookie_secure", lambda request: True)
    web_auth._attach_session(SimpleNamespace(), {"sub": "owner"}, Response())
    assert web_auth._SESSIONS["same-sid"] is current


def test_valid_reference_is_copied_before_first_database_wait(issued, monkeypatch):
    store, sid, owner, row = issued
    reference = store.session_reference(owner, session_id=sid, incarnation_id=row["incarnation_id"])
    read = store._refresh_record
    observations = []

    def delayed_read(*args, **kwargs):
        reference["incarnation_id"] = B
        observed = read(*args, **kwargs)
        observations.append(observed.incarnation_id)
        return observed

    async def exchange(*args):
        return {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}

    monkeypatch.setattr(store, "_refresh_record", delayed_read)
    result = asyncio.run(store.refresh_credential(sid, owner_id=owner, reference=reference, exchange=exchange))
    assert result["incarnation_id"] == row["incarnation_id"]
    assert observations and set(observations) == {row["incarnation_id"]}


def test_malformed_expected_incarnation_refuses_before_database_or_oauth(issued, monkeypatch):
    from orchestrator.session_store import SessionRefreshUnavailable
    store, sid, owner, _ = issued

    def forbidden(*args):
        pytest.fail("invalid observed ID cannot reach storage")

    monkeypatch.setattr(store, "_refresh_record", forbidden)
    with pytest.raises(SessionRefreshUnavailable):
        asyncio.run(store.refresh_credential(sid, owner_id=owner, expected_incarnation_id="invalid", exchange=None))


@pytest.mark.parametrize("outcome", ["retired", "replaced", "uncertain"])
def test_refresh_voice_cleanup_requires_successful_exact_retirement(issued, runtime, monkeypatch, outcome):
    from tests.helpers.session_plane_runtime import get_session_record
    _, sid, _, _ = issued
    calls, current = [], []
    request = SimpleNamespace(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})

    async def exchange(*args):
        if outcome == "uncertain":
            raise httpx.ConnectError("synthetic offline outcome")
        if outcome == "replaced":
            current.append(await asyncio.to_thread(replacement, runtime, sid))
        response = httpx.Response(401, request=httpx.Request("POST", "https://issuer.invalid/token"))
        raise httpx.HTTPStatusError("synthetic refusal", request=response.request, response=response)

    async def voice(*args, **kwargs):
        calls.append(args)

    async def audit(*args, **kwargs):
        pass

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    monkeypatch.setattr(web_auth, "_end_voice_session", voice)
    monkeypatch.setattr(web_auth, "_audit", audit)
    monkeypatch.setattr(web_auth, "_token_expires_at", lambda token: 0)
    assert asyncio.run(web_auth.ensure_session(request)) is None
    assert len(calls) == (1 if outcome == "retired" else 0)
    if outcome == "replaced":
        assert get_session_record(runtime, sid) == current[0]
    elif outcome == "retired":
        assert get_session_record(runtime, sid) is None
    else:
        assert get_session_record(runtime, sid) is not None


def test_unavailable_store_does_not_turn_durable_observation_into_local_retirement(monkeypatch):
    selected = web_session()
    web_auth._SESSIONS["same-sid"] = selected
    monkeypatch.setattr(web_auth, "_get_store", lambda: None)
    assert asyncio.run(web_auth._kill_session("same-sid", selected)) is False
    assert selected["refresh_token"] == ""
    assert "same-sid" not in web_auth._SESSIONS


@pytest.mark.parametrize("method", ["amark_resumed", "aupdate_tokens", "adelete"])
def test_async_mutation_wrappers_propagate_original_issuance(issued, runtime, monkeypatch, method):
    from orchestrator.session_store import SessionRefreshUnavailable
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, _, row = issued
    sync_method = method[1:]
    original = getattr(store, sync_method)
    current = []

    def delayed(value, *args, **kwargs):
        current.append(replacement(runtime, sid))
        assert kwargs["expected_incarnation_id"] == row["incarnation_id"]
        return original(value, *args, **kwargs)

    monkeypatch.setattr(store, sync_method, delayed)
    kwargs = {"expected_incarnation_id": row["incarnation_id"]}
    if method == "aupdate_tokens":
        kwargs.update(access_token="late-access", refresh_token="late-refresh")
        with pytest.raises(SessionRefreshUnavailable):
            asyncio.run(getattr(store, method)(sid, **kwargs))
    else:
        assert asyncio.run(getattr(store, method)(sid, **kwargs)) is None
    assert get_session_record(runtime, sid) == current[0]


@pytest.mark.parametrize("replacement_stage", ["claim", "http", "none"])
def test_still_valid_access_cannot_survive_known_incarnation_replacement(issued, runtime, monkeypatch, replacement_stage):
    import base64
    import json
    import time
    from tests.helpers.session_plane_runtime import get_session_record
    store, sid, owner, row = issued
    payload = base64.urlsafe_b64encode(json.dumps({"sub": owner, "exp": int(time.time()) + 30}).encode()).rstrip(b"=").decode()
    access = f"synthetic.{payload}.signature"
    store.update_tokens(sid, access_token=access, refresh_token="synthetic-refresh",
                        expected_incarnation_id=row["incarnation_id"])
    current, exchanged, voice = [], [], []
    request = SimpleNamespace(cookies={web_auth.COOKIE_NAME: web_auth._sign(sid)})
    repository = store._sessions.repository
    cas = repository.compare_and_set_refresh

    def replace_before_claim(transaction, record, **kwargs):
        current.append(replacement(runtime, sid))
        return cas(transaction, record, **kwargs)

    async def exchange(*args):
        exchanged.append(args)
        if replacement_stage == "http":
            current.append(await asyncio.to_thread(replacement, runtime, sid))
        raise httpx.ConnectError("synthetic uncertain remote outcome")

    async def cleanup(*args):
        voice.append(args)

    if replacement_stage == "claim":
        monkeypatch.setattr(repository, "compare_and_set_refresh", replace_before_claim)
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    monkeypatch.setattr(web_auth, "_end_voice_session", cleanup)
    result = asyncio.run(web_auth.ensure_session(request))
    assert voice == []
    assert len(exchanged) == (0 if replacement_stage == "claim" else 1)
    if replacement_stage == "none":
        assert result["access_token"] == access and result["refresh_token"] == ""
        assert result["incarnation_id"] == row["incarnation_id"]
    else:
        assert result is None
        assert get_session_record(runtime, sid) == current[0]


def test_unknown_refresh_and_unavailable_identity_read_refuse_access(monkeypatch):
    async def failed(*args, **kwargs):
        raise httpx.ConnectError("synthetic unknown outcome")

    def unreadable(*args, **kwargs):
        raise RuntimeError("synthetic unavailable database")

    monkeypatch.setattr(web_auth, "_get_store", lambda: SimpleNamespace(
        refresh_credential=failed, is_current_incarnation=unreadable))
    assert asyncio.run(web_auth._refresh_session("same-sid", web_session())) is None


def test_identity_check_never_selects_wrong_sid_or_invalid_id(issued):
    store, sid, owner, row = issued
    assert store.is_current_incarnation(owner, session_id=sid, incarnation_id=row["incarnation_id"])
    assert not store.is_current_incarnation(owner, session_id="wrong-sid", incarnation_id=row["incarnation_id"])
    assert not store.is_current_incarnation(owner, session_id=sid, incarnation_id="invalid")
