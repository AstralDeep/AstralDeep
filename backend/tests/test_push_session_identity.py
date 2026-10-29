"""Exercises native push session identity through normal signed-JWT REST dependencies and real Plane custody. Synthetic provider/JWKS transports keep issuer changes, renewal, and denial deterministic."""

import json
import time
from types import SimpleNamespace
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwk, jwt
import pytest

from audit.repository import AuditRepository
from orchestrator import auth, push_api, web_auth
from orchestrator.push_delivery import PushDeliveryCoordinator
from orchestrator.push_destinations import DestinationStoreUnavailable, PushDestinationStore
from orchestrator.session_store import WebSessionStore
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_push_delivery_270 import FakeAdapter, _notification

ISSUER = "https://push-current.invalid/realms/current"
OLD_ISSUER = "https://push-prior.invalid/realms/prior"
CLIENT = "astral-mobile"
OTHER_CLIENT = "astral-desktop"
KEY = "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="


@pytest.fixture(scope="module")
def runtime():
    with isolated_plane_runtime("push_identity") as value:
        yield value


@pytest.fixture(scope="module")
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    public_jwk = jwk.construct(public, algorithm="RS256").to_dict()
    public_jwk["kid"] = "push-session-identity"
    return key, {"keys": [public_jwk]}


@pytest.fixture
def harness(runtime, signing_key, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", KEY)
    monkeypatch.setenv("WEB_SESSION_SECRET", "synthetic-push-session-cookie")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", CLIENT + "," + OTHER_CLIENT)
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "synthetic-push-session-secret")
    runtime.execute("DELETE FROM astralplane_outbox WHERE topic = %s", ("push.notification",))
    sessions = WebSessionStore(plane_runtime=runtime)
    audit = AuditRepository(plane_runtime=runtime, plane_repositories=runtime.repositories)
    store = PushDestinationStore(plane_runtime=runtime, audit_repository=audit)
    adapter = FakeAdapter()
    coordinator = PushDeliveryCoordinator(plane_runtime=runtime, destinations=store,
        adapters={"fcm": adapter}, audit_repository=audit, delivery_policy=lambda *_: True)
    app = FastAPI()
    app.include_router(push_api.push_router)
    app.state.orchestrator = SimpleNamespace(push_delivery=coordinator, web_sessions=sessions)
    assert not app.dependency_overrides
    monkeypatch.setattr(web_auth, "_get_store", lambda: sessions)

    async def jwks(*args, **kwargs):
        return signing_key[1]

    monkeypatch.setattr("shared.jwks_cache.get_jwks", jwks)
    owner, device = str(uuid4()), "device-" + uuid4().hex

    def token(*, issuer=ISSUER, client=CLIENT, subject=owner):
        return jwt.encode({"sub": subject, "iss": issuer, "azp": client, "aud": "account",
            "exp": int(time.time()) + 600, "realm_access": {"roles": ["user"]}, "jti": uuid4().hex}, signing_key[0],
            algorithm="RS256", headers={"kid": "push-session-identity"})

    def session(*, issuer=ISSUER, client=CLIENT, subject=owner):
        return sessions.create(uuid4().hex, user_id=subject,
            access_token=token(issuer=issuer or ISSUER, client=client or CLIENT, subject=subject),
            refresh_token="synthetic-refresh", hard_max_seconds=3600,
            issuing_issuer=issuer, issuing_client_id=client)

    def headers(row, *, issuer=ISSUER, client=CLIENT):
        return {"Authorization": "Bearer " + token(issuer=issuer, client=client),
                "Cookie": f"{web_auth.COOKIE_NAME}={web_auth._sign(row['sid'])}"}

    with TestClient(app, base_url="https://app.invalid") as client:
        def enroll(row, *, revision=0, **claims):
            return client.post("/api/push/destinations", json={"device_id": device,
                "platform": "android", "token": "synthetic-provider-token",
                "expected_destination_revision": revision}, headers=headers(row, **claims))

        yield SimpleNamespace(owner=owner, device=device, sessions=sessions, store=store, audit=audit,
            adapter=adapter, coordinator=coordinator, client=client, token=token, session=session,
            headers=headers, enroll=enroll)
    sessions.delete_for_user(owner)


def test_matching_pair_remains_private_and_eligible(harness):
    h = harness
    row = h.session()
    result = h.enroll(row)
    assert result.status_code == 200
    serialized = json.dumps(result.json())
    assert all(value not in serialized for value in (row["sid"], row["incarnation_id"], ISSUER, CLIENT))
    assert h.store.delivery_target(h.owner, h.device) is not None
    with h.store._credentials.transaction() as transaction:
        reference = h.store._get_record(transaction, h.owner, h.device)["session_reference"]
    assert reference["issuing_issuer"] == ISSUER and reference["issuing_client_id"] == CLIENT


@pytest.mark.parametrize("issuer,client,bearer_client", [
    (OLD_ISSUER, CLIENT, CLIENT), (ISSUER, OTHER_CLIENT, CLIENT), (None, None, CLIENT),
])
def test_conditional_claim_rejects_incoherent_or_legacy_session(harness, issuer, client, bearer_client):
    h = harness
    row = h.session(issuer=issuer, client=client)
    result = h.enroll(row, client=bearer_client)
    assert result.status_code == 503 and result.json()["detail"] == "destination_store_unavailable"
    assert h.store.list_destinations(h.owner) == []


def test_removed_client_cookie_cannot_claim_with_current_allowed_bearer(harness, monkeypatch):
    h = harness
    row = h.session()
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", OTHER_CLIENT)
    result = h.enroll(row, client=OTHER_CLIENT)
    assert result.status_code == 503 and h.store.list_destinations(h.owner) == []


@pytest.mark.parametrize("issuer,client", [(OLD_ISSUER, CLIENT), (ISSUER, "not-allowed")])
def test_normal_jwt_policy_denies_untrusted_bearer_before_claim(harness, issuer, client):
    h = harness
    result = h.enroll(h.session(), issuer=issuer, client=client)
    assert result.status_code == 401 and h.store.list_destinations(h.owner) == []


@pytest.mark.parametrize("claims", [{"issuer": None}, {"client": None}])
def test_optional_legacy_jwt_claims_cannot_authorize_native_custody(harness, claims):
    h = harness
    result = h.enroll(h.session(), **claims)
    assert result.status_code == 503 and h.store.list_destinations(h.owner) == []


@pytest.mark.parametrize("fault", ["missing", "duplicate", "foreign", "retired"])
def test_signed_cookie_denials_leave_no_destination(harness, fault):
    h = harness
    row = h.session(subject="other-owner" if fault == "foreign" else h.owner)
    headers = h.headers(row)
    if fault == "missing":
        del headers["Cookie"]
    elif fault == "duplicate":
        headers["Cookie"] += "; " + headers["Cookie"]
    elif fault == "retired":
        h.sessions.delete(row["sid"], expected_incarnation_id=row["incarnation_id"])
    result = h.client.post("/api/push/destinations", json={"device_id": h.device, "platform": "android",
        "token": "synthetic-provider-token", "expected_destination_revision": 0}, headers=headers)
    assert result.status_code == 503 and h.store.list_destinations(h.owner) == []


@pytest.mark.parametrize("setting,value", [("KEYCLOAK_AUTHORITY", OLD_ISSUER),
    ("KEYCLOAK_ALLOWED_AZP", OTHER_CLIENT), ("KEYCLOAK_AUTHORITY", ""),
    ("KEYCLOAK_CLIENT_ID", ""), ("USE_MOCK_AUTH", "true")])
async def test_current_iam_change_denies_existing_target_dispatch_and_ack(harness, monkeypatch, setting, value):
    h = harness
    row = h.session()
    assert h.enroll(row).status_code == 200
    notification_id = str(uuid4())
    assert await h.coordinator.enqueue_notification(h.owner, _notification(notification_id=notification_id)) == 1
    monkeypatch.setenv(setting, value)
    with pytest.raises(DestinationStoreUnavailable):
        h.store.delivery_target(h.owner, h.device)
    await h.coordinator.dispatch_once()
    assert h.adapter.calls == []
    with pytest.raises(DestinationStoreUnavailable):
        await h.coordinator.acknowledge(h.owner, h.device, notification_id)


async def test_policy_change_before_second_provider_admission_denies_send(harness, monkeypatch):
    h = harness
    assert h.enroll(h.session()).status_code == 200
    assert await h.coordinator.enqueue_notification(h.owner, _notification(notification_id=str(uuid4()))) == 1

    def policy(*args):
        monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", OTHER_CLIENT)
        return True

    h.coordinator._delivery_policy = policy
    await h.coordinator.dispatch_once()
    assert h.adapter.calls == []


def test_iam_change_during_cookie_capture_cannot_commit_claim(harness, monkeypatch):
    h = harness
    original = h.sessions.capture_execution_reference

    def capture(**kwargs):
        reference = original(**kwargs)
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", OLD_ISSUER)
        return reference

    monkeypatch.setattr(h.sessions, "capture_execution_reference", capture)
    result = h.enroll(h.session())
    assert result.status_code == 503 and h.store.list_destinations(h.owner) == []


@pytest.mark.parametrize("enrolling", [True, False])
def test_iam_change_while_asserting_session_cannot_authorize_push(harness, monkeypatch, enrolling):
    h = harness
    row = h.session()
    if not enrolling:
        assert h.enroll(row).status_code == 200
    sessions = h.store._credentials.plane_runtime.repositories.history.sessions
    original = sessions.assert_current_consent

    def assert_then_change(transaction, *, observation):
        result = original(transaction, observation=observation)
        monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", OTHER_CLIENT)
        return result

    monkeypatch.setattr(sessions, "assert_current_consent", assert_then_change)
    if enrolling:
        result = h.enroll(row)
        assert result.status_code == 503 and h.store.list_destinations(h.owner) == []
    else:
        with pytest.raises(DestinationStoreUnavailable):
            h.store.delivery_target(h.owner, h.device)


async def test_real_bound_refresh_preserves_pair_incarnation_and_push_proof(harness, monkeypatch):
    h = harness
    row = h.session()
    first = h.enroll(row).json()
    notice = str(uuid4())
    assert await h.coordinator.enqueue_notification(h.owner, _notification(notification_id=notice)) == 1

    async def exchange(refresh, authority, client, secret):
        assert refresh == "synthetic-refresh" and authority == ISSUER and client == CLIENT and secret == ""
        return {"access_token": h.token(), "refresh_token": "synthetic-rotated-refresh"}

    async def no_legacy(*args):
        pytest.fail("bound session used legacy token exchange")

    monkeypatch.setattr(web_auth, "_post_session_refresh", exchange)
    refreshed = await h.sessions.refresh_credential(row["sid"], owner_id=h.owner,
        exchange=no_legacy, bound_exchange=web_auth._exchange_bound_session_refresh)
    assert refreshed["incarnation_id"] == row["incarnation_id"]
    assert refreshed["access_token"] != row["access_token"]
    assert h.sessions.get(row["sid"])["refresh_token"] == "synthetic-rotated-refresh"
    assert h.store.delivery_target(h.owner, h.device)["destination_revision"] == first["updated_at"]
    await h.coordinator.dispatch_once()
    assert len(h.adapter.calls) == 1
    await h.coordinator.acknowledge(h.owner, h.device, notice)


async def test_same_owner_fresh_session_claim_and_exact_retirement(harness):
    h = harness
    old = h.session()
    first = h.enroll(old).json()
    notice = str(uuid4())
    await h.coordinator.enqueue_notification(h.owner, _notification(notification_id=notice))
    fresh = h.session()
    current = h.enroll(fresh, revision=first["updated_at"])
    assert current.status_code == 200 and current.json()["updated_at"] > first["updated_at"]
    assert current.json()["token_generation"] == first["token_generation"]
    h.sessions.delete(old["sid"], expected_incarnation_id=old["incarnation_id"])
    assert h.store.delivery_target(h.owner, h.device) is not None
    await h.coordinator.dispatch_once()
    assert h.adapter.calls == []
    h.sessions.delete(fresh["sid"], expected_incarnation_id=fresh["incarnation_id"])
    assert h.store.delivery_target(h.owner, h.device) is None


def test_private_legacy_destination_requires_fresh_conditional_claim(harness):
    h = harness
    row = h.session()
    first = h.enroll(row).json()
    with h.store._credentials.transaction() as transaction:
        record = h.store._get_record(transaction, h.owner, h.device)
        record["session_reference"].pop("issuing_issuer", None)
        record["session_reference"].pop("issuing_client_id", None)
        record["updated_at"] += 1
        h.store._write_record(transaction, h.owner, record, expected_updated_at=first["updated_at"])
    assert h.store.delivery_target(h.owner, h.device) is None
    current = h.enroll(row, revision=first["updated_at"] + 1)
    assert current.status_code == 200 and current.json()["updated_at"] > first["updated_at"]
    assert h.store.delivery_target(h.owner, h.device) is not None


def test_configuration_read_failure_cannot_report_success(harness, monkeypatch):
    h = harness
    row = h.session()
    assert h.enroll(row).status_code == 200

    def unavailable():
        raise RuntimeError("synthetic configuration store unavailable")

    monkeypatch.setattr(auth, "_get_keycloak_config", unavailable)
    with pytest.raises(DestinationStoreUnavailable):
        h.store.delivery_target(h.owner, h.device)
