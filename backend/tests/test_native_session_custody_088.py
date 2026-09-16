"""Fresh native grants enter real Plane custody; all IdP traffic is synthetic."""

import asyncio
import json
import threading
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from orchestrator import auth, device_login, web_auth
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_device_login import start_body
from tests.test_auth_token_proxy import _FakeSession
from tests.test_session_issuer_binding_088 import (
    ISSUER,
    REAL_CLIENT,
    fixture as fixture,
    runtime as runtime,
    signing_key as signing_key,
)

MODE = "server_v1"
HEADER = {"X-Astral-Session-Custody": MODE}
REDIRECT = "com.personalailabs.astraldeep:/oauth2redirect"


@pytest.fixture
async def client(fixture, monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-native,astral-mobile,astral-watch")
    monkeypatch.setenv("KEYCLOAK_DEVICE_CLIENTS", "astral-watch")
    monkeypatch.setenv("FF_DEVICE_LOGIN", "1")
    fixture[4].changes = {"azp": "astral-mobile"}

    class LegacyReply(_FakeSession):
        body = {"access_token": fixture[3](azp="astral-mobile"),
                "refresh_token": "synthetic-fresh-custody-refresh", "expires_in": 300}

    monkeypatch.setattr("aiohttp.ClientSession", LegacyReply)
    original_post = device_login._default_post_form

    async def discovery(url):
        return 200, {"token_endpoint": ISSUER + "/protocol/openid-connect/token",
                     "device_authorization_endpoint": ISSUER + "/protocol/openid-connect/auth/device"}

    async def device_post(url, data):
        if url.endswith("/auth/device"):
            return 200, start_body(interval=1, verification_uri=ISSUER + "/device",
                                   verification_uri_complete=ISSUER + "/device?user_code=SYNTHETIC")
        return await original_post(url, data)

    monkeypatch.setattr(device_login, "_default_get_json", discovery)
    monkeypatch.setattr(device_login, "_default_post_form", device_post)
    app = FastAPI()
    app.include_router(auth.auth_router)
    app.include_router(web_auth.web_auth_router)
    auth.reset_token_proxy_state()
    device_login.reset_state()
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=app),
                           base_url="https://app.invalid") as value:
        yield value
    device_login.reset_state()
    auth.reset_token_proxy_state()
    # The fixture owns every issued row and the original issuer test session.
    fixture[0].delete_for_user(fixture[1])


def form(**changes):
    result = {"session_custody": MODE, "grant_type": "authorization_code",
              "client_id": "astral-mobile", "redirect_uri": REDIRECT,
              "code": "synthetic-code-" + uuid4().hex, "code_verifier": "a" * 64}
    result.update(changes)
    return result


@pytest.mark.asyncio
async def test_fresh_public_code_issues_only_the_actual_stored_session(client, fixture, runtime):
    response = await client.post("/auth/token", data=form(), headers=HEADER)
    assert response.status_code == 200
    assert response.json().get("authenticated") is True
    assert set(response.json()) == {
        "authenticated", "access_token", "token_type", "expires_in", "user_id", "resumed"}
    assert "refresh_token" not in response.text and "incarnation" not in response.text
    cookie = response.cookies.get(web_auth.COOKIE_NAME)
    assert cookie and "httponly" in response.headers["set-cookie"].lower()
    sid = web_auth._unsign(cookie)
    stored = get_session_record(runtime, sid)
    assert stored.owner_id == fixture[1]
    assert stored.issuing_issuer == ISSUER and stored.issuing_client_id == "astral-mobile"
    assert stored.incarnation_id and stored.refresh_token_ciphertext
    assert fixture[0].get(sid)["access_token"] == response.json()["access_token"]
    sent = fixture[4].requests[-1][1]
    assert sent["client_id"] == ["astral-mobile"] and "client_secret" not in sent


@pytest.mark.asyncio
async def test_custody_does_not_return_a_process_only_success(client, fixture, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(fixture[0], "acreate", fail)
    response = await client.post("/auth/token", data=form(), headers=HEADER)
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert "access_token" not in response.text and "refresh_token" not in response.text


@pytest.mark.asyncio
async def test_same_fresh_code_cannot_issue_twice(client, fixture):
    body = form()
    first = await client.post("/auth/token", data=body, headers=HEADER)
    second = await client.post("/auth/token", data=body, headers=HEADER)
    assert first.status_code == 200 and second.status_code == 409
    assert "set-cookie" not in second.headers and len(fixture[4].requests) == 1


@pytest.mark.asyncio
async def test_original_cookie_cannot_be_reselected_after_body_wait(client, fixture, runtime):
    original = get_session_record(runtime, fixture[2])
    body = urlencode(form()).encode()

    async def chunks():
        yield body[:8]
        replace_session_record(runtime, original)
        yield body[8:]

    response = await client.post("/auth/token", content=chunks(), headers={
        **HEADER, "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": web_auth.COOKIE_NAME + "=" + web_auth._sign(fixture[2]),
        "Authorization": "Bearer " + fixture[3]()})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers and not fixture[4].requests


@pytest.mark.asyncio
async def test_device_custody_issues_cookie_without_refresh_token_release(client, fixture, runtime):
    fixture[4].changes = {"azp": "astral-watch"}
    started = await client.post("/api/auth/device/start", headers=HEADER,
                               json={"client": "astral-watch", "session_custody": MODE})
    assert started.status_code == 200
    handle = started.json()["handle"]
    device_login._POLL_STATE[device_login._handle_digest(handle)]["next_ok"] = 0
    response = await client.post("/api/auth/device/poll", headers=HEADER,
                                json={"handle": handle, "session_custody": MODE})
    assert response.status_code == 200 and response.json().get("authenticated") is True
    assert response.json()["status"] == "approved" and "refresh_token" not in response.text
    sid = web_auth._unsign(response.cookies[web_auth.COOKIE_NAME])
    assert get_session_record(runtime, sid).issuing_client_id == "astral-watch"
    replay = await client.post("/api/auth/device/poll", headers=HEADER,
                              json={"handle": handle, "session_custody": MODE})
    assert replay.status_code == 400 and "set-cookie" not in replay.headers


@pytest.mark.asyncio
async def test_legacy_poll_cannot_downgrade_custody_handle(client, fixture):
    fixture[4].changes = {"azp": "astral-watch"}
    started = await client.post("/api/auth/device/start", headers=HEADER,
                               json={"client": "astral-watch", "session_custody": MODE})
    handle = started.json()["handle"]
    device_login._POLL_STATE[device_login._handle_digest(handle)]["next_ok"] = 0
    response = await client.post("/api/auth/device/poll", json={"handle": handle})
    assert response.status_code == 400 and not fixture[4].requests


@pytest.mark.asyncio
async def test_custody_logout_retires_actual_issuance_without_token_upload(client, fixture, runtime, monkeypatch):
    issued = await client.post("/auth/token", data=form(), headers=HEADER)
    sid = web_auth._unsign(issued.cookies[web_auth.COOKIE_NAME])
    old = get_session_record(runtime, sid)
    # Provider revocation alone is synthetic; selected local retirement is real.
    async def revoke(token, client_id=None, **binding):
        assert client_id == "astral-mobile" and binding == {"issuing_issuer": ISSUER}
        assert token == "synthetic-rotated-refresh"
        return True

    monkeypatch.setattr(web_auth, "_revoke_refresh_token", revoke)
    response = await client.post("/api/auth/logout", json={"session_custody": MODE},
        headers={**HEADER, "Authorization": "Bearer " + issued.json()["access_token"]})
    assert response.status_code == 200 and response.json()["revoked"] is True
    assert get_session_record(runtime, sid) is None and old.incarnation_id
    assert "max-age=0" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_code_network_unknown_is_terminal_and_data_free(client, fixture, monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError("synthetic-private-provider-detail")

    monkeypatch.setattr(device_login, "_default_post_form", fail)
    body = form()
    response = await client.post("/auth/token", data=body, headers=HEADER)
    assert response.status_code == 503
    assert "private-provider" not in response.text and "set-cookie" not in response.headers
    retry = await client.post("/auth/token", data=body, headers=HEADER)
    assert retry.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_code_has_one_provider_exchange(client, fixture):
    entered, release = asyncio.Event(), asyncio.Event()
    async def held():
        entered.set()
        await release.wait()
    fixture[4].hook = held
    body = form()
    task = asyncio.create_task(client.post("/auth/token", data=body, headers=HEADER))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        other = await client.post("/auth/token", data=body, headers=HEADER)
        assert other.status_code == 409 and len(fixture[4].requests) == 1
    finally:
        release.set()
        first = await task
    assert first.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("grant_type", "refresh_token"), ("client_id", "astral-web"),
    ("client_id", "unregistered"), ("redirect_uri", "https://foreign.invalid/callback"),
    ("code_verifier", "short"), ("code_verifier", "a" * 129), ("code", "line\nbreak"),
    ("refresh_token", "synthetic-do-not-import"), ("client_secret", "synthetic-secret"),
])
async def test_closed_public_code_request_never_reaches_provider(client, fixture, field, value):
    response = await client.post("/auth/token", data=form(**{field: value}), headers=HEADER)
    assert response.status_code == 400 and not fixture[4].requests
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"iss": None}, {"azp": None}, {"azp": "astral-web"}, {"iss": "https://foreign.invalid"},
    {"exp": 1}, {"exp": True}, {"exp": "999999999999"}, {"sub": ""},
    {"realm_access": {"roles": []}}, {"act": {"sub": "agent"}}, {"aud": "astral-mcp"},
])
async def test_invalid_grant_identity_is_not_a_session(client, fixture, changes):
    fixture[4].changes = {"azp": "astral-mobile", **changes}
    response = await client.post("/auth/token", data=form(), headers=HEADER)
    assert response.status_code in {401, 403}
    assert "set-cookie" not in response.headers and "refresh_token" not in response.text


@pytest.mark.asyncio
async def test_held_session_table_refuses_issuance_with_bounded_sql(client, fixture, runtime):
    acquired, release = threading.Event(), threading.Event()
    def lock():
        with runtime.transaction() as tx:
            tx.execute("LOCK TABLE web_session IN ACCESS EXCLUSIVE MODE")
            acquired.set()
            assert release.wait(5)
    locked = asyncio.create_task(asyncio.to_thread(lock))
    try:
        assert await asyncio.to_thread(acquired.wait, 2)
        body = form()
        response = await asyncio.wait_for(client.post("/auth/token", data=body, headers=HEADER), 2)
        assert response.status_code == 503 and "set-cookie" not in response.headers
    finally:
        release.set()
        await locked
    retry = await client.post("/auth/token", data=body, headers=HEADER)
    assert retry.status_code == 409 and len(fixture[4].requests) == 1


@pytest.mark.asyncio
async def test_cancelled_delivery_retains_worker_slot_until_actual_completion(client, fixture, monkeypatch):
    from orchestrator import native_session_custody as custody
    original = fixture[0].acreate
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(custody, "ISSUANCE_LIMIT", 1)
    async def held(*args, **kwargs):
        row = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return row
    monkeypatch.setattr(fixture[0], "acreate", held)
    body = form()
    request = asyncio.create_task(client.post("/auth/token", data=body, headers=HEADER))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        retry = await client.post("/auth/token", data=body, headers=HEADER)
        assert retry.status_code == 409
        other = await client.post("/auth/token", data=form(), headers=HEADER)
        assert other.status_code == 429 and "set-cookie" not in other.headers
    finally:
        release.set()
        await asyncio.gather(*tuple(custody._ISSUANCE_TASKS), return_exceptions=True)
    final = await client.post("/auth/token", data=form(), headers=HEADER)
    assert final.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["store", "issuer", "client", "cookie_key", "cipher", "hard_cap"])
async def test_changed_custody_composition_after_exchange_refuses_cookie(client, fixture, monkeypatch, change):
    async def mutate():
        if change == "store":
            monkeypatch.setattr(web_auth, "_get_store", lambda: None)
        elif change == "issuer":
            monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://other.invalid/realm")
        elif change == "client":
            monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-native")
        elif change == "cookie_key":
            monkeypatch.setenv("WEB_SESSION_SECRET", "synthetic-new-cookie-key")
        elif change == "cipher":
            monkeypatch.setattr(fixture[0], "_fernet", object())
        else:
            monkeypatch.setattr(web_auth, "HARD_MAX_SECONDS", web_auth.HARD_MAX_SECONDS + 1)
    fixture[4].hook = mutate
    response = await client.post("/auth/token", data=form(), headers=HEADER)
    assert response.status_code in {400, 503}
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "expired", "issuer", "concurrent", "unknown"])
async def test_device_attempt_lifetime_is_closed(client, fixture, monkeypatch, fault):
    fixture[4].changes = {"azp": "astral-watch"}
    response = await client.post("/api/auth/device/start", headers=HEADER,
                                json={"client": "astral-watch", "session_custody": MODE})
    handle = response.json()["handle"]
    key = device_login._handle_digest(handle)
    state = device_login._POLL_STATE[key]
    state["next_ok"] = 0
    if fault == "missing":
        device_login._POLL_STATE.clear()
    elif fault == "expired":
        state["custody"].expires = 0
    elif fault == "issuer":
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://other.invalid/realm")
    elif fault == "concurrent":
        state["in_flight"] = True
    else:
        async def broken(*args, **kwargs):
            raise OSError("synthetic-private-provider-failure")
        monkeypatch.setattr(device_login, "_default_post_form", broken)
    refused = await client.post("/api/auth/device/poll", headers=HEADER,
                               json={"handle": handle, "session_custody": MODE})
    assert refused.status_code in {400, 503}
    assert "set-cookie" not in refused.headers and "private-provider" not in refused.text
    assert not fixture[4].requests


@pytest.mark.asyncio
async def test_original_retirement_during_final_new_row_read_refuses_cookie(client, fixture, runtime, monkeypatch):
    repository = fixture[0]._sessions.repository
    attempted = []
    def around(method):
        def read(tx, **kwargs):
            record = method(tx, **kwargs)
            if record is not None and record.session_id != fixture[2] and not attempted:
                attempted.append(True)
                fixture[0].delete(fixture[2], expected_incarnation_id=fixture[5]["incarnation_id"],
                                  request_execution=True)
            return record
        return read
    monkeypatch.setattr(repository, "get_by_session_id_for_administration",
                        around(repository.get_by_session_id_for_administration))
    monkeypatch.setattr(repository, "get", around(repository.get))
    response = await client.post("/auth/token", data=form(), headers={
        **HEADER, "Cookie": web_auth.COOKIE_NAME + "=" + web_auth._sign(fixture[2]),
        "Authorization": "Bearer " + fixture[3]()})
    assert attempted and response.status_code in {401, 503}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_issuance_request_bound_requires_exact_boolean(fixture, runtime, value):
    sid = uuid4().hex
    with pytest.raises(Exception, match="invalid session request bound"):
        fixture[0].create(sid, user_id=fixture[1], access_token=fixture[3](),
            refresh_token="synthetic", hard_max_seconds=100, request_execution=value)
    assert get_session_record(runtime, sid) is None


@pytest.mark.asyncio
async def test_anonymous_discovery_advertises_only_protocol_support(client, fixture, monkeypatch):
    async def no_session_read(*args, **kwargs):
        pytest.fail("Anonymous protocol discovery must not read session storage")
    monkeypatch.setattr(fixture[0], "aget", no_session_read)
    response = await client.get("/auth/session")
    assert response.status_code == 200
    assert response.headers.get("X-Astral-Session-Custody") == MODE
    assert response.json() == {"authenticated": False, "access_token": "", "resumed": False,
                               "reason": "no_session"}
    assert "set-cookie" not in response.headers and not fixture[4].requests


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["origin", "header", "query", "duplicate", "json_duplicate",
                                   "oversize", "chunked", "utf8", "content_type", "missing"])
async def test_malformed_custody_wire_is_refused_before_provider(client, fixture, fault):
    body = urlencode(form()).encode()
    headers = {**HEADER, "Content-Type": "application/x-www-form-urlencoded"}
    path = "/auth/token"
    if fault == "origin":
        headers["Origin"] = "https://attacker.invalid"
    elif fault == "header":
        headers["X-Astral-Session-Custody"] = "unknown"
    elif fault == "query":
        path += "?token=synthetic"
    elif fault == "duplicate":
        body += b"&code=second"
    elif fault == "json_duplicate":
        headers["Content-Type"] = "application/json"
        body = b'{"session_custody":"server_v1","session_custody":"server_v1"}'
    elif fault == "oversize":
        body = b"a" * 16_385
    elif fault == "chunked":
        async def chunks():
            yield b"a" * 8000
            yield b"b" * 9000
        body = chunks()
    elif fault == "utf8":
        body = b"\xff"
    elif fault == "content_type":
        headers["Content-Type"] = "text/plain"
    else:
        headers.pop("X-Astral-Session-Custody")
    response = await client.post(path, content=body, headers=headers)
    assert response.status_code in {400, 413}
    assert "set-cookie" not in response.headers and not fixture[4].requests


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie", ["invalid", "retired", "duplicate"])
async def test_invalid_original_cookie_never_becomes_anonymous_custody(client, fixture, cookie):
    value = "bad-signature" if cookie == "invalid" else web_auth._sign(uuid4().hex)
    if cookie == "duplicate":
        value = web_auth._sign(fixture[2]) + "; " + web_auth.COOKIE_NAME + "=" + web_auth._sign(fixture[2])
    response = await client.post("/auth/token", data=form(), headers={
        **HEADER, "Cookie": web_auth.COOKIE_NAME + "=" + value})
    assert response.status_code == 401 and not fixture[4].requests


@pytest.mark.asyncio
async def test_new_grant_must_match_original_authenticated_owner(client, fixture):
    fixture[4].changes = {"azp": "astral-mobile", "sub": str(uuid4())}
    response = await client.post("/auth/token", data=form(), headers={
        **HEADER, "Authorization": "Bearer " + fixture[3]()})
    assert response.status_code == 401 and "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_existing_session_route_refreshes_server_custody_without_cookie_rotation(client, fixture, runtime):
    issued = await client.post("/auth/token", data=form(), headers=HEADER)
    cookie = issued.cookies[web_auth.COOKIE_NAME]
    sid = web_auth._unsign(cookie)
    before = get_session_record(runtime, sid)
    response = await client.get("/auth/session")
    assert response.status_code == 200 and response.json()["authenticated"] is True
    assert response.json()["user_id"] == fixture[1]
    assert "refresh_token" not in response.text and "set-cookie" not in response.headers
    assert client.cookies[web_auth.COOKIE_NAME] == cookie
    assert get_session_record(runtime, sid).incarnation_id == before.incarnation_id
