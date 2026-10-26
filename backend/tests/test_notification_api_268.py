"""Authenticated notification REST boundary: routing preferences and quiet-hours
revision flows, durable inbox listing/get/acknowledge/delete, unauthenticated and
wrong-owner denials, and malformed-input rejections.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit.repository import AuditRepository
from orchestrator.auth import verify_user
from orchestrator.notification_api import notification_router
from orchestrator.notification_routing import NotificationRoutingCoordinator
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

KEY = "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="
BASE_NOW = 1_800_000_000.0


class FakeClock:
    def __init__(self, now_s: float = BASE_NOW):
        self.now_s = now_s

    def __call__(self) -> float:
        return self.now_s

    def advance(self, seconds: float) -> None:
        self.now_s += seconds


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("notification_api") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY)


@pytest.fixture()
def owner() -> str:
    return f"notif-api-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def other_owner() -> str:
    return f"notif-api-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def audit_repository(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def coordinator(plane_runtime, audit_repository, clock):
    return NotificationRoutingCoordinator(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repository=audit_repository,
        clock=clock,
    )


class FakeOrchestrator:
    def __init__(self, coordinator):
        self.notification_routing = coordinator


@pytest.fixture()
def app(coordinator):
    app = FastAPI()
    app.include_router(notification_router)
    app.state.orchestrator = FakeOrchestrator(coordinator)
    return app


@pytest.fixture()
def client(app):
    app.dependency_overrides[verify_user] = lambda: {"sub": None}
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def authed_client(app, client, owner):
    app.dependency_overrides[verify_user] = lambda: {"sub": owner}
    return client


def _preferences_body(**overrides):
    body = {
        "expected_revision": 0,
        "in_app_completion": True,
        "in_app_failure": True,
        "in_app_required_action": True,
        "inbox_completion": True,
        "inbox_failure": True,
        "inbox_required_action": True,
        "push_completion": True,
        "push_failure": True,
        "push_required_action": True,
    }
    body.update(overrides)
    return body


def _quiet_body(**overrides):
    body = {
        "expected_revision": 0,
        "enabled": True,
        "start_minute": 22 * 60,
        "end_minute": 8 * 60,
        "tz_offset_minutes": 0,
    }
    body.update(overrides)
    return body


def test_preferences_require_authentication(client):
    response = client.get("/api/notifications/preferences")
    assert response.status_code == 401
    response = client.put("/api/notifications/preferences", json=_preferences_body())
    assert response.status_code == 401


def test_preferences_roundtrip_and_revision_fence(authed_client):
    initial = authed_client.get("/api/notifications/preferences").json()
    assert initial["routing"]["revision"] == 0
    assert initial["routing"]["in_app_completion"] is True
    assert initial["quiet_hours"]["quiet_enabled"] is False
    updated = authed_client.put(
        "/api/notifications/preferences",
        json=_preferences_body(in_app_completion=False, expected_revision=0),
    )
    assert updated.status_code == 200
    assert updated.json()["in_app_completion"] is False
    assert updated.json()["revision"] == 1
    stale = authed_client.put(
        "/api/notifications/preferences",
        json=_preferences_body(in_app_completion=True, expected_revision=0),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "notification_policy_conflict"
    again = authed_client.put(
        "/api/notifications/preferences",
        json=_preferences_body(in_app_completion=True, expected_revision=1),
    )
    assert again.status_code == 200
    assert again.json()["in_app_completion"] is True


def test_preferences_reject_invalid_bodies(authed_client):
    missing = _preferences_body()
    del missing["push_failure"]
    response = authed_client.put("/api/notifications/preferences", json=missing)
    assert response.status_code == 422
    non_bool = _preferences_body(push_failure=None)
    response = authed_client.put("/api/notifications/preferences", json=non_bool)
    assert response.status_code == 422


def test_quiet_hours_roundtrip_and_validation(authed_client):
    stored = authed_client.put("/api/notifications/quiet-hours", json=_quiet_body())
    assert stored.status_code == 200
    assert stored.json()["quiet_enabled"] is True
    revision = stored.json()["updated_at"]
    updated = authed_client.put(
        "/api/notifications/quiet-hours",
        json=_quiet_body(enabled=False, expected_revision=revision),
    )
    assert updated.status_code == 200
    assert updated.json()["quiet_enabled"] is False
    stale = authed_client.put(
        "/api/notifications/quiet-hours",
        json=_quiet_body(enabled=True, expected_revision=revision),
    )
    assert stale.status_code == 409
    equal_window = authed_client.put(
        "/api/notifications/quiet-hours",
        json=_quiet_body(start_minute=5, end_minute=5),
    )
    assert equal_window.status_code == 400
    assert equal_window.json()["detail"] == "notification_policy_invalid"
    bad_offset = authed_client.put(
        "/api/notifications/quiet-hours",
        json=_quiet_body(tz_offset_minutes=900),
    )
    assert bad_offset.status_code == 422


def test_inbox_roundtrip_and_owner_isolation(
    authed_client, client, app, coordinator, owner, other_owner
):
    import asyncio

    asyncio.run(
        coordinator.route(
            owner_id=other_owner,
            payload={
                "type": "notification",
                "level": "info",
                "source": "schedule",
                "title": "For someone else",
                "body": "hidden",
            },
            has_active_surface=True,
            push_available=False,
        )
    )
    empty = authed_client.get("/api/notifications/inbox")
    assert empty.status_code == 200
    assert empty.json() == []
    app.dependency_overrides[verify_user] = lambda: {"sub": other_owner}
    other = client.get("/api/notifications/inbox")
    assert other.status_code == 200
    assert len(other.json()) == 1
    app.dependency_overrides[verify_user] = lambda: {"sub": owner}
    assert authed_client.get("/api/notifications/inbox").json() == []


def test_inbox_listing_and_content_decryption(authed_client, coordinator, clock, owner):
    import asyncio

    for index in range(3):
        asyncio.run(
            coordinator.route(
                owner_id=owner,
                payload={
                    "type": "notification",
                    "level": "error" if index == 1 else "info",
                    "source": "remote_job",
                    "title": f"Event {index}",
                    "body": "Body",
                    "job_id": f"job-{index}",
                },
                has_active_surface=True,
                push_available=False,
            )
        )
        clock.advance(1)
    listing = authed_client.get("/api/notifications/inbox")
    assert listing.status_code == 200
    entries = listing.json()
    assert len(entries) == 3
    assert [entry["content"]["title"] for entry in entries] == [
        "Event 2",
        "Event 1",
        "Event 0",
    ]
    assert entries[1]["event_kind"] == "failure"
    assert entries[1]["content"]["level"] == "error"
    assert "content_ciphertext" not in entries[1]
    unack = authed_client.get(
        "/api/notifications/inbox", params={"unacknowledged_only": "true"}
    )
    assert len(unack.json()) == 3
    first = entries[0]
    acked = authed_client.post(
        f"/api/notifications/inbox/{first['entry_id']}/acknowledge",
        json={"expected_revision": first["revision"]},
    )
    assert acked.status_code == 204
    unack = authed_client.get(
        "/api/notifications/inbox", params={"unacknowledged_only": "true"}
    )
    assert [entry["entry_id"] for entry in unack.json()] == [
        entry["entry_id"] for entry in entries[1:]
    ]
    filtered = authed_client.get("/api/notifications/inbox", params={"limit": 1})
    assert len(filtered.json()) == 1
    bad_limit = authed_client.get("/api/notifications/inbox", params={"limit": 500})
    assert bad_limit.status_code == 422


def test_inbox_get_acknowledge_delete_flows(authed_client, coordinator, owner):
    import asyncio

    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload={
                "type": "notification",
                "level": "info",
                "source": "schedule",
                "title": "Single",
                "body": "Body",
            },
            has_active_surface=True,
            push_available=False,
        )
    )
    entry = authed_client.get("/api/notifications/inbox").json()[0]
    entry_id = entry["entry_id"]
    single = authed_client.get(f"/api/notifications/inbox/{entry_id}")
    assert single.status_code == 200
    assert single.json()["content"]["title"] == "Single"
    stale_ack = authed_client.post(
        f"/api/notifications/inbox/{entry_id}/acknowledge",
        json={"expected_revision": entry["revision"] + 7},
    )
    assert stale_ack.status_code == 409
    assert stale_ack.json()["detail"] == "notification_entry_conflict"
    authed_client.post(
        f"/api/notifications/inbox/{entry_id}/acknowledge",
        json={"expected_revision": entry["revision"]},
    )
    deleted = authed_client.delete(
        f"/api/notifications/inbox/{entry_id}",
        params={"expected_revision": entry["revision"] + 1},
    )
    assert deleted.status_code == 204
    missing = authed_client.get(f"/api/notifications/inbox/{entry_id}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "notification_entry_not_found"
    malformed = authed_client.get("/api/notifications/inbox/not-a-uuid")
    assert malformed.status_code == 404


def test_inbox_operations_require_authentication(client):
    entry_id = str(uuid.uuid4())
    assert client.get("/api/notifications/inbox").status_code == 401
    assert (
        client.post(
            f"/api/notifications/inbox/{entry_id}/acknowledge",
            json={"expected_revision": 0},
        ).status_code
        == 401
    )
    assert (
        client.delete(
            f"/api/notifications/inbox/{entry_id}",
            params={"expected_revision": 0},
        ).status_code
        == 401
    )


def test_wrong_owner_cannot_read_or_mutate_entries(
    authed_client, app, client, coordinator, owner, other_owner
):
    import asyncio

    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload={
                "type": "notification",
                "level": "info",
                "source": "schedule",
                "title": "Private",
                "body": "Body",
            },
            has_active_surface=True,
            push_available=False,
        )
    )
    entry = authed_client.get("/api/notifications/inbox").json()[0]
    app.dependency_overrides[verify_user] = lambda: {"sub": other_owner}
    crossed = client.get(f"/api/notifications/inbox/{entry['entry_id']}")
    assert crossed.status_code == 404
    crossed_ack = client.post(
        f"/api/notifications/inbox/{entry['entry_id']}/acknowledge",
        json={"expected_revision": entry["revision"]},
    )
    assert crossed_ack.status_code == 404
    crossed_delete = client.delete(
        f"/api/notifications/inbox/{entry['entry_id']}",
        params={"expected_revision": entry["revision"]},
    )
    assert crossed_delete.status_code == 404


def test_inbox_store_outage_maps_to_503(
    authed_client, coordinator, owner, plane_runtime
):
    import asyncio

    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload={
                "type": "notification",
                "level": "info",
                "source": "schedule",
                "title": "Outage",
                "body": "Body",
            },
            has_active_surface=True,
            push_available=False,
        )
    )
    plane_runtime.execute(
        "UPDATE notification_inbox_entry SET content_ciphertext = %s "
        "WHERE owner_id = %s",
        (b"garbage-not-fernet", owner),
    )
    response = authed_client.get("/api/notifications/inbox")
    assert response.status_code == 503
    assert response.json()["detail"] == "notification_store_unavailable"
