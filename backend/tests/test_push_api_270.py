"""Owner-authenticated push REST boundary: destination enrollment, listing,
revocation, client acknowledgment, delivery-outcome introspection, and the
unauthenticated / wrong-owner / unconfigured-provider denials.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit.repository import AuditRepository
from orchestrator import push_api
from orchestrator.auth import verify_user
from orchestrator.push_delivery import PushDeliveryCoordinator
from orchestrator.push_destinations import PushDestinationStore
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

from tests.test_push_delivery_270 import FakeAdapter

KEY = "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("push_api") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY)


@pytest.fixture()
def owner() -> str:
    return f"push-owner-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def other_owner() -> str:
    return f"push-owner-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def audit_repository(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture()
def store(plane_runtime, audit_repository):
    return PushDestinationStore(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repository=audit_repository,
    )


@pytest.fixture()
def coordinator(plane_runtime, store, audit_repository):
    return PushDeliveryCoordinator(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        destinations=store,
        adapters={"fcm": FakeAdapter(), "apns": FakeAdapter()},
        audit_repository=audit_repository,
    )


class FakeOrchestrator:
    def __init__(self, coordinator):
        self.push_delivery = coordinator


@pytest.fixture()
def app(plane_runtime, coordinator):
    app = FastAPI()
    app.include_router(push_api.push_router)
    app.state.orchestrator = FakeOrchestrator(coordinator)
    app.dependency_overrides[verify_user] = lambda: {"sub": None}
    return app


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def authed_client(app, client, owner):
    app.dependency_overrides[verify_user] = lambda: {"sub": owner}
    return client


def _enroll_body(**overrides):
    body = {
        "device_id": "device-api-00001",
        "platform": "android",
        "token": "fcm-api-token-1",
    }
    body.update(overrides)
    return body


def test_enroll_requires_authentication(client):
    response = client.post("/api/push/destinations", json=_enroll_body())
    assert response.status_code == 401


def test_enroll_list_and_get_roundtrip(authed_client, owner):
    response = authed_client.post("/api/push/destinations", json=_enroll_body(label="Phone"))
    assert response.status_code == 200
    record = response.json()
    assert record["device_id"] == "device-api-00001"
    assert record["status"] == "active"
    assert record["provider"] == "fcm"
    assert "token" not in record
    listing = authed_client.get("/api/push/destinations").json()
    assert [item["device_id"] for item in listing] == ["device-api-00001"]
    single = authed_client.get("/api/push/destinations/device-api-00001")
    assert single.status_code == 200
    assert single.json()["label"] == "Phone"


def test_enroll_rotation_and_revocation_flow(authed_client):
    authed_client.post("/api/push/destinations", json=_enroll_body())
    rotated = authed_client.post(
        "/api/push/destinations",
        json=_enroll_body(token="fcm-api-token-2"),
    )
    assert rotated.json()["token_generation"] == 2
    revoked = authed_client.delete("/api/push/destinations/device-api-00001")
    assert revoked.status_code == 204
    missing = authed_client.get("/api/push/destinations/device-api-00001")
    assert missing.status_code == 200
    assert missing.json()["status"] == "revoked"


def test_enroll_invalid_input_rejected(authed_client):
    response = authed_client.post(
        "/api/push/destinations", json=_enroll_body(platform="windows")
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "destination_invalid"
    bad_device = authed_client.post(
        "/api/push/destinations", json=_enroll_body(device_id="no")
    )
    assert bad_device.status_code == 400


def test_enroll_capacity_denied(authed_client):
    for index in range(32):
        response = authed_client.post(
            "/api/push/destinations",
            json=_enroll_body(device_id=f"device-cap-{index:04d}", token=f"t-{index}"),
        )
        assert response.status_code == 200
    denial = authed_client.post(
        "/api/push/destinations",
        json=_enroll_body(device_id="device-cap-overflow", token="t-overflow"),
    )
    assert denial.status_code == 409
    assert denial.json()["detail"] == "destination_capacity"


def test_wrong_owner_sees_nothing(app, authed_client, other_owner):
    authed_client.post("/api/push/destinations", json=_enroll_body())
    app.dependency_overrides[verify_user] = lambda: {"sub": other_owner}
    assert authed_client.get("/api/push/destinations").json() == []
    assert (
        authed_client.get("/api/push/destinations/device-api-00001").status_code == 404
    )
    assert (
        authed_client.delete("/api/push/destinations/device-api-00001").status_code
        == 404
    )
    assert (
        authed_client.post(
            "/api/push/ack",
            json={
                "device_id": "device-api-00001",
                "notification_id": "sched-20260101-a",
            },
        ).status_code
        == 404
    )


def test_acknowledgement_flow(authed_client, coordinator, store, owner):
    store.enroll(owner, device_id="device-ack-api-01", platform="android", token="tok-1")
    ok = authed_client.post(
        "/api/push/ack",
        json={
            "device_id": "device-ack-api-01",
            "notification_id": "sched-20260101-a",
        },
    )
    assert ok.status_code == 204
    malformed = authed_client.post(
        "/api/push/ack",
        json={"device_id": "device-ack-api-01", "notification_id": "bad id!"},
    )
    assert malformed.status_code == 400
    revoked = store.revoke(owner, "device-ack-api-01")
    assert revoked["status"] == "revoked"
    stale = authed_client.post(
        "/api/push/ack",
        json={
            "device_id": "device-ack-api-01",
            "notification_id": "sched-20260101-a",
        },
    )
    assert stale.status_code == 410


def test_delivery_outcomes_introspection(
    authed_client, coordinator, store, owner, audit_repository, plane_runtime
):
    store.enroll(owner, device_id="device-outcome-01", platform="android", token="tok-1")
    payload = {
        "type": "notification",
        "level": "info",
        "source": "schedule",
        "title": "Job finished",
        "body": "Done",
        "notification_id": "sched-20260101-a",
    }
    asyncio.run(coordinator.enqueue_notification(owner, payload))
    asyncio.run(coordinator.dispatch_once())
    response = authed_client.get("/api/push/deliveries/sched-20260101-a")
    assert response.status_code == 200
    outcomes = response.json()
    assert outcomes, "expected at least one delivery outcome event"
    actions = {item["action"] for item in outcomes}
    assert "push.delivery.queued" in actions
    assert "push.delivery.delivered" in actions
    delivered = [item for item in outcomes if item["action"] == "push.delivery.delivered"]
    assert delivered[0]["delivery_outcome"] == "provider_accepted"
    assert delivered[0]["device_id"] == "device-outcome-01"
    foreign = authed_client.get("/api/push/deliveries/never-seen-notification")
    assert foreign.status_code == 200
    assert foreign.json() == []


def test_unauthenticated_owner_edge_and_missing_body(app, client):
    app.dependency_overrides[verify_user] = lambda: {}
    response = client.post("/api/push/destinations", json=_enroll_body())
    assert response.status_code == 401


def test_provider_not_configured_denies_enrollment(
    app, authed_client, coordinator
):
    coordinator._adapters["apns"] = FakeAdapter(configured=False)
    response = authed_client.post(
        "/api/push/destinations",
        json=_enroll_body(platform="ios", device_id="device-unconf-api-1"),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "provider_not_configured"


def test_unknown_platform_rejected_before_provider_check(authed_client):
    response = authed_client.post(
        "/api/push/destinations", json=_enroll_body(platform="harmonyos")
    )
    assert response.status_code == 400


def test_delivery_outcomes_when_audit_unavailable(app, authed_client, coordinator):
    coordinator_audit = coordinator.audit_repository
    app_dependent = authed_client
    coordinator._audit = None
    try:
        response = app_dependent.get("/api/push/deliveries/sched-20260101-a")
        assert response.status_code == 503
    finally:
        coordinator._audit = coordinator_audit


def test_delivery_outcomes_when_audit_query_fails(
    app, authed_client, coordinator, audit_repository, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit_repository, "list_for_user", broken)
    response = authed_client.get("/api/push/deliveries/sched-20260101-a")
    assert response.status_code == 503
