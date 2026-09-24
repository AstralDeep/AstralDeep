"""Tests for orchestrator/work_submit.py and work_submit_authority.py: native bearer and
custody-cookie admission through registered routes with real JWT/JWKS validation and
synthetic IdP responses.
"""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from audit import hooks
from audit.recorder import Recorder
from orchestrator import (
    auth,
    device_login,
    offline_grant,
    web_auth,
    work_submit,
    work_submit_authority,
)
from orchestrator.api import operation_router
from orchestrator.credential_manager import CredentialManager
from persistent_agents.models import AssignmentError
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.helpers.session_plane_runtime import get_session_record, revocation_records
from tests.test_auth_token_proxy import _FakeSession
from tests.test_device_login import FakePost, start_body
from tests import test_request_session_authority_088 as request_authority_fixtures
from tests.test_request_session_authority_088 import signing_key as signing_key
from tests.test_work_submit_postgres_088 import (
    command,
    context,
    service as service,
    totals,
)


ISSUER = "https://iam.ai.uky.edu/realms/Astral"
NATIVE_CLIENTS = ("astral-desktop", "astral-mobile", "astral-watch")
WORK = "/api/work/v1/operations"
runtime = plane
base_session = request_authority_fixtures.fixture


@pytest.fixture
def fixture(base_session, monkeypatch):
    store, owner, sid, original_token, seen = base_session
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISSUER)
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", ",".join(NATIVE_CLIENTS))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", os.environ["WEB_SESSION_ENC_KEY"])

    def token(**changes):
        return original_token(**{"iss": ISSUER, **changes})

    store.update_tokens(
        sid, access_token=token(), refresh_token="synthetic-native-compat-refresh"
    )

    async def exchange(refresh, prior_access):
        seen.append((refresh, prior_access))
        return {
            "access_token": token(),
            "refresh_token": "synthetic-native-compat-rotated",
        }

    async def no_network(*args, **kwargs):
        raise AssertionError("T014 fixture attempted unscripted IdP transport")

    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    monkeypatch.setattr(device_login, "_default_get_json", no_network)
    monkeypatch.setattr(device_login, "_default_post_form", no_network)
    auth.reset_token_proxy_state()
    device_login.reset_state()
    yield store, owner, sid, token, seen
    device_login.reset_state()
    auth.reset_token_proxy_state()


@pytest.fixture
async def client(service, fixture, runtime, monkeypatch, tmp_path):
    app = FastAPI()
    app.include_router(operation_router)
    app.include_router(auth.auth_router)
    app.state.orchestrator = service.assignments.orch
    app.state.orchestrator.persistent_assignments = service.assignments
    app.state.orchestrator.audit_repo = service.audit
    app.state.orchestrator.web_sessions = service.sessions
    app.state.orchestrator.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories)
    )
    recorder = Recorder(service.audit, retry_queue=tmp_path / "audit-retry.jsonl")
    monkeypatch.setattr(hooks, "get_recorder", lambda: recorder)
    grants = offline_grant.OfflineGrantStore(plane_runtime=runtime)
    monkeypatch.setattr(offline_grant, "get_offline_grant_store", lambda: grants)
    monkeypatch.setattr(
        web_auth, "_CREDENTIAL_MANAGER", CredentialManager(plane_runtime=runtime)
    )
    assert app.dependency_overrides == {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://app.invalid"
    ) as value:
        yield value
    await recorder.close()
    assert not (tmp_path / "audit-retry.jsonl").exists()


def bearer(fixture, client_id="astral-mobile", **changes):
    return {"Authorization": "Bearer " + fixture[3](**{"azp": client_id, **changes})}


async def accepted(service, fixture, runtime):
    result = await service.submit(await context(fixture, runtime), command())
    assert result.created
    return result.record


def session_count(runtime):
    with runtime.transaction() as transaction:
        return transaction.fetch_one("SELECT count(*) AS n FROM web_session")["n"]


@pytest.mark.parametrize("client_id", (*NATIVE_CLIENTS, "astral-web"))
async def test_registered_owner_work_read_control_and_replay(
    client, service, fixture, runtime, client_id
):
    record = await accepted(service, fixture, runtime)
    headers = bearer(fixture, client_id)
    detail = await client.get(f"{WORK}/{record.assignment_id}", headers=headers)
    assert detail.status_code == 200
    operation = detail.json()["operation"]
    assert (
        operation["id"] == record.assignment_id
        and operation["schema_supported"] is True
    )
    assert detail.headers["cache-control"] == "no-store"
    body = {"expected_revision": operation["revision"], "submission_id": str(uuid4())}
    paused = await client.post(
        f"{WORK}/{record.assignment_id}/pause", headers=headers, json=body
    )
    assert paused.status_code == 200 and paused.json()["applied"] is True
    duplicate = await client.post(
        f"{WORK}/{record.assignment_id}/pause", headers=headers, json=body
    )
    assert duplicate.status_code == 200 and duplicate.json()["applied"] is False
    polled = await client.get(f"{WORK}/{record.assignment_id}/poll", headers=headers)
    assert (
        polled.status_code == 200
        and polled.json()["operation"]["lifecycle"] == "paused"
    )
    events, _ = service.audit.list_for_user(fixture[1])
    assert sum(event.action_type == "assignment_pause" for event in events) == 1
    assert service.audit.verify_chain(fixture[1]) is None


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"sub": "other-synthetic-owner"}, 404),
        ({"iss": "https://wrong-issuer.invalid/realm"}, 401),
        ({"azp": "unregistered-client"}, 401),
        ({"realm_access": {"roles": []}}, 403),
        ({"exp": 1}, 401),
        ({"act": {"sub": "delegated-agent"}}, 401),
        ({"aud": "astral-mcp"}, 401),
    ],
)
async def test_registered_work_auth_owner_and_delegation_denials(
    client, service, fixture, runtime, changes, status
):
    record = await accepted(service, fixture, runtime)
    headers = bearer(fixture, **changes)
    before = totals(runtime, fixture[1])
    detail = await client.get(f"{WORK}/{record.assignment_id}", headers=headers)
    control = await client.post(
        f"{WORK}/{record.assignment_id}/cancel",
        headers=headers,
        json={"expected_revision": record.state_version, "submission_id": str(uuid4())},
    )
    assert detail.status_code == control.status_code == status
    assert (
        detail.headers["cache-control"]
        == control.headers["cache-control"]
        == "no-store"
    )
    if status == 404:
        absent = await client.get(f"{WORK}/{uuid4()}", headers=headers)
        assert (
            detail.json()
            == control.json()
            == absent.json()
            == {"error": "work_not_found"}
        )
    current = await client.get(
        f"{WORK}/{record.assignment_id}", headers=bearer(fixture)
    )
    assert current.json()["operation"]["revision"] == record.state_version
    assert totals(runtime, fixture[1]) == before
    events, _ = service.audit.list_for_user(fixture[1])
    assert [event.action_type for event in events] == ["work.accept"]


@pytest.mark.parametrize(
    "origin,status",
    [
        (None, 403),
        ("https://other.invalid", 403),
        ("https://app.invalid:0", 403),
        ("https://app.invalid", 200),
        ("https://app.invalid:443", 200),
    ],
)
async def test_registered_bff_cookie_write_preserves_exact_origin(
    client, service, fixture, runtime, origin, status
):
    record = await accepted(service, fixture, runtime)
    headers = {"Cookie": "astral_session=" + web_auth._sign(fixture[2])}
    if origin is not None:
        headers["Origin"] = origin
    response = await client.post(
        f"{WORK}/{record.assignment_id}/pause",
        headers=headers,
        json={"expected_revision": record.state_version, "submission_id": str(uuid4())},
    )
    assert response.status_code == status
    current = await client.get(
        f"{WORK}/{record.assignment_id}", headers=bearer(fixture)
    )
    assert (current.json()["operation"]["lifecycle"] == "paused") is (status == 200)


@pytest.mark.parametrize(
    "mode,expected",
    [("query-token", 403), ("non-json", 415), ("duplicate-origin", 403)],
)
async def test_registered_write_denials_do_not_mutate(
    client, service, fixture, runtime, mode, expected
):
    record = await accepted(service, fixture, runtime)
    headers = list(bearer(fixture).items())
    suffix = "?token=synthetic-query-token" if mode == "query-token" else ""
    if mode == "duplicate-origin":
        headers = [
            ("Cookie", "astral_session=" + web_auth._sign(fixture[2])),
            ("Origin", "https://app.invalid"),
            ("Origin", "https://app.invalid"),
        ]
    payload = {"expected_revision": record.state_version, "submission_id": str(uuid4())}
    kwargs = (
        {"content": "{}", "headers": headers + [("Content-Type", "text/plain")]}
        if mode == "non-json"
        else {"json": payload, "headers": headers}
    )
    response = await client.post(
        f"{WORK}/{record.assignment_id}/pause{suffix}", **kwargs
    )
    assert response.status_code == expected
    current = await client.get(
        f"{WORK}/{record.assignment_id}", headers=bearer(fixture)
    )
    assert current.json()["operation"]["revision"] == record.state_version


@pytest.mark.parametrize("client_id", NATIVE_CLIENTS)
async def test_native_bearer_refuses_new_admission_but_replays_after_session_retirement(
    client, service, fixture, runtime, monkeypatch, client_id
):
    selected = await context(
        fixture, runtime, cookie=False, bearer=True, changes={"azp": client_id}
    )
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(selected, command())
    assert totals(runtime, fixture[1]) == (0, 0, 0) and not fixture[-1]
    body = command()
    original = await service.submit(await context(fixture, runtime), body)
    fixture[0].delete(fixture[2])
    assert get_session_record(runtime, fixture[2]) is None
    service.assignments.orch.agent_cards.clear()

    def no_expansion(*args, **kwargs):
        raise AssertionError(
            "accepted native replay re-expanded intent or refreshed authority"
        )

    with monkeypatch.context() as replay_patch:
        replay_patch.setattr(service, "_definition", no_expansion)
        replay_patch.setattr(
            work_submit, "refresh_work_submission_authority", no_expansion
        )
        replay = await service.submit(
            await context(
                fixture, runtime, cookie=False, bearer=True, changes={"azp": client_id}
            ),
            body,
        )
    assert not replay.created and replay.record == original.record
    assert totals(runtime, fixture[1]) == (1, 1, 1) and len(fixture[-1]) == 1
    detail = await client.get(
        f"{WORK}/{original.record.assignment_id}", headers=bearer(fixture, client_id)
    )
    assert detail.status_code == 200
    unavailable = await client.post(
        WORK, headers=bearer(fixture, client_id), content=body
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "work_submit_unavailable"}
    assert totals(runtime, fixture[1]) == (1, 1, 1) and len(fixture[-1]) == 1
    foreign = await context(
        fixture,
        runtime,
        cookie=False,
        bearer=True,
        changes={"azp": client_id, "sub": "other-synthetic-owner"},
    )
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(foreign, body)


@pytest.mark.parametrize("client_id", NATIVE_CLIENTS)
@pytest.mark.parametrize("upstream_status,outcome", [(204, "revoked"), (503, "queued")])
async def test_registered_native_logout_preserves_public_client_and_owner_queue(
    client, fixture, runtime, service, monkeypatch, client_id, upstream_status, outcome
):
    from urllib.parse import parse_qs

    sent = []
    actual_client = httpx.AsyncClient

    def reply(request):
        assert str(request.url) == ISSUER + "/protocol/openid-connect/revoke"
        sent.append(parse_qs(request.content.decode()))
        return httpx.Response(upstream_status)

    def transport_client(*args, **kwargs):
        return actual_client(*args, **kwargs, transport=httpx.MockTransport(reply))

    monkeypatch.setattr(web_auth.httpx, "AsyncClient", transport_client)
    before = get_session_record(runtime, fixture[2])
    response = await client.post(
        "/api/auth/logout",
        headers=bearer(fixture, client_id),
        json={"client_id": client_id, "refresh_token": "synthetic-native-held-refresh"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "outcome": outcome,
        "revoked": outcome == "revoked",
        "queued": outcome == "queued",
    }
    assert sent == [
        {
            "token": ["synthetic-native-held-refresh"],
            "token_type_hint": ["refresh_token"],
            "client_id": [client_id],
        }
    ]
    rows = revocation_records(runtime, fixture[1])
    assert len(rows) == (1 if outcome == "queued" else 0)
    if rows:
        assert (
            rows[0].client_id == client_id
            and getattr(rows[0], "issuing_issuer", None) is None
        )
        assert "synthetic-native-held-refresh" not in rows[0].refresh_token_ciphertext
    assert get_session_record(runtime, fixture[2]) == before
    assert session_count(runtime) == 1
    events, _ = service.audit.list_for_user(fixture[1])
    assert len(events) == 1 and events[0].action_type == "auth.logout"
    assert outcome in events[0].description
    assert "synthetic-native-held-refresh" not in events[0].description


@pytest.mark.parametrize("flow", ["windows-bff-proxy", "watch-device"])
async def test_existing_native_token_routes_do_not_mint_work_session_authority(
    client, fixture, runtime, service, monkeypatch, flow
):
    before = get_session_record(runtime, fixture[2])
    public_client = "astral-web" if flow == "windows-bff-proxy" else "astral-watch"
    payload = {
        "access_token": fixture[3](azp=public_client),
        "refresh_token": "synthetic-native-route-refresh",
        "expires_in": 300,
        "token_type": "Bearer",
    }
    if flow == "windows-bff-proxy":

        class TokenReply(_FakeSession):
            body = payload
            captured = {}

        monkeypatch.setattr("aiohttp.ClientSession", TokenReply)
        response = await client.post(
            "/auth/token",
            data={
                "grant_type": "authorization_code",
                "code": "synthetic-one-use-code",
                "code_verifier": "synthetic-pkce-verifier",
                "redirect_uri": "http://127.0.0.1:5321/callback",
                "client_id": "ignored-native-override",
            },
        )
        assert response.status_code == 200
        assert TokenReply.captured["url"] == ISSUER + "/protocol/openid-connect/token"
        assert TokenReply.captured["data"]["client_id"] == "astral-web"
        assert TokenReply.captured["data"]["client_secret"] == "synthetic-secret"
        returned = response.json()
    else:
        monkeypatch.setenv("FF_DEVICE_LOGIN", "1")
        monkeypatch.setenv("KEYCLOAK_DEVICE_CLIENTS", "astral-watch")

        async def discovery(url):
            assert url == ISSUER + "/.well-known/openid-configuration"
            return 200, {
                "token_endpoint": ISSUER + "/protocol/openid-connect/token",
                "device_authorization_endpoint": ISSUER
                + "/protocol/openid-connect/auth/device",
            }

        post = FakePost(
            (
                200,
                start_body(
                    interval=1,
                    verification_uri=ISSUER + "/device",
                    verification_uri_complete=ISSUER + "/device?user_code=SYNTHETIC",
                ),
            ),
            (200, payload),
        )
        monkeypatch.setattr(device_login, "_default_get_json", discovery)
        monkeypatch.setattr(device_login, "_default_post_form", post)
        started = await client.post(
            "/api/auth/device/start", json={"client": "astral-watch"}
        )
        assert started.status_code == 200
        early = await client.post(
            "/api/auth/device/poll", json={"handle": started.json()["handle"]}
        )
        assert (
            early.json() == {"status": "slow_down", "interval": 1}
            and len(post.calls) == 1
        )
        await asyncio.sleep(early.json()["interval"])
        response = await client.post(
            "/api/auth/device/poll", json={"handle": started.json()["handle"]}
        )
        assert response.status_code == 200
        assert post.calls[0][1]["code_challenge_method"] == "S256"
        assert post.calls[1][1]["client_id"] == "astral-watch"
        assert "client_secret" not in post.calls[1][1]
        assert response.json()["status"] == "approved"
        returned = response.json()["tokens"]
        repeated = await client.post(
            "/api/auth/device/poll", json={"handle": started.json()["handle"]}
        )
        assert repeated.status_code == 400 and len(post.calls) == 2
    assert returned["access_token"] == payload["access_token"]
    assert "astral_session" not in response.cookies
    assert get_session_record(runtime, fixture[2]) == before
    assert session_count(runtime) == 1
    headers = {"Authorization": "Bearer " + returned["access_token"]}
    listing = await client.get(WORK, headers=headers)
    assert listing.status_code == 200 and listing.json()["operations"] == []
    selected = await work_submit_authority.authenticate_work_submission_request(
        request_authority_fixtures.request(
            headers=[
                (b"authorization", headers["Authorization"].encode()),
                (b"content-type", b"application/json"),
            ]
        ),
        sessions=fixture[0],
        plane_runtime=runtime,
    )
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(selected, command())
    assert totals(runtime, fixture[1]) == (0, 0, 0) and not fixture[-1]


@pytest.mark.parametrize("client_id", ("astral-desktop", "astral-mobile"))
async def test_native_custody_cookie_replaces_bare_bearer_for_new_admission(
    client, service, fixture, runtime, monkeypatch, client_id
):
    store, owner, original_sid, token, seen = fixture
    selected = await context(
        fixture, runtime, cookie=False, bearer=True, changes={"azp": client_id}
    )
    assert selected.session_id is None and selected.cookie_session is None
    with pytest.raises(AssignmentError) as refused:
        await service.submit(selected, command())
    assert (refused.value.code, refused.value.status_code) == (
        "work_authority_unavailable",
        403,
    )
    assert totals(runtime, owner) == (0, 0, 0) and not seen
    assert session_count(runtime) == 1
    exchanged = []

    async def token_post(url, data):
        exchanged.append((url, dict(data)))
        assert url == ISSUER + "/protocol/openid-connect/token"
        assert data["client_id"] == client_id and "client_secret" not in data
        return 200, {
            "access_token": token(azp=client_id),
            "refresh_token": "synthetic-native-custody-refresh",
            "expires_in": 300,
        }

    monkeypatch.setattr(device_login, "_default_post_form", token_post)
    issued = await client.post(
        "/auth/token",
        headers={"X-Astral-Session-Custody": "server_v1"},
        data={
            "session_custody": "server_v1",
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "com.personalailabs.astraldeep:/oauth2redirect",
            "code": "synthetic-code-" + uuid4().hex,
            "code_verifier": "a" * 64,
        },
    )
    assert issued.status_code == 200, issued.text
    assert len(exchanged) == 1 and "refresh_token" not in issued.text
    cookie = issued.cookies["astral_session"]
    sid = web_auth._unsign(cookie)
    client.cookies.clear()
    assert sid != original_sid and session_count(runtime) == 2
    custody = get_session_record(runtime, sid)
    assert (custody.issuing_issuer, custody.issuing_client_id) == (ISSUER, client_id)
    bound = []

    async def bound_exchange(refresh, identity):
        bound.append((refresh, identity.issuer, identity.client_id, identity.owner_id))
        return {
            "access_token": token(azp=client_id),
            "refresh_token": "synthetic-native-custody-rotated",
        }

    monkeypatch.setattr(web_auth, "_exchange_bound_session_refresh", bound_exchange)
    admitted = await work_submit_authority.authenticate_work_submission_request(
        request_authority_fixtures.request(
            sid,
            headers=[
                (b"content-type", b"application/json"),
                (b"origin", b"https://app.invalid"),
            ],
        ),
        sessions=store,
        plane_runtime=runtime,
    )
    assert admitted.session_id == sid
    result = await service.submit(admitted, command())
    assert result.created and totals(runtime, owner) == (1, 1, 1)
    assert bound == [("synthetic-native-custody-refresh", ISSUER, client_id, owner)]
    assert not seen
    assert (
        result.record.operation["authority"]["reference_id"]
        == get_session_record(runtime, sid).incarnation_id
    )
    assert get_session_record(runtime, original_sid).incarnation_id != (
        result.record.operation["authority"]["reference_id"]
    )
    detail = await client.get(
        f"{WORK}/{result.record.assignment_id}",
        headers={"Cookie": "astral_session=" + cookie},
    )
    assert detail.status_code == 200
    assert detail.json()["operation"]["id"] == result.record.assignment_id
    assert service.audit.verify_chain(owner) is None
