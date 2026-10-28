"""Authenticated presence API regressions keep foreground leases bound to live
UI connection incarnations, increasing sequence numbers and destination state.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.auth import verify_user
from orchestrator.notification_api import notification_router


@pytest.mark.parametrize("claims,expected", [({}, 401), ({"sub": "owner", "exp": 4_000_000_000}, 409)])
def test_presence_requires_authentication_and_a_registered_ui_connection(claims, expected):
    app = FastAPI()
    app.include_router(notification_router)
    app.state.orchestrator = SimpleNamespace(ui_sessions={})
    app.dependency_overrides[verify_user] = lambda: claims
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/notifications/presence", json={
        "device_id": "device-00001", "connection_generation": "connection-1",
        "sequence": 1, "foreground": True,
    })
    assert response.status_code == expected


def test_foreground_presence_is_leased_ordered_and_owner_bound():
    from orchestrator.notification_presence import NotificationPresence, NotificationPresenceError

    clock = SimpleNamespace(now=100.0)
    presence = NotificationPresence(clock=lambda: clock.now)
    first, second, other = object(), object(), object()
    sessions = {first: {"sub": "owner", "exp": 4_000_000_000}, second: {"sub": "owner", "exp": 4_000_000_000}, other: {"sub": "other", "exp": 4_000_000_000}}
    presence.register(first, "owner", "device-00001", "connection-1")
    presence.register(second, "owner", "device-00002", "connection-2")
    presence.register(other, "other", "device-00003", "connection-3")
    identities = {}
    def lookup(owner, device):
        return identities.get((owner, device))
    assert not presence.active("owner", sessions, lookup)
    result = presence.update("owner", "device-00001", "connection-1", 1, True, sessions, lookup)
    assert result == {"accepted_sequence": 1, "expires_in_seconds": 45}
    assert presence.active("owner", sessions, lookup)
    assert not presence.active("other", sessions, lookup)
    clock.now += 45
    assert not presence.active("owner", sessions, lookup)
    presence.update("owner", "device-00001", "connection-1", 2, True, sessions, lookup)
    presence.update("owner", "device-00001", "connection-1", 3, False, sessions, lookup)
    with pytest.raises(NotificationPresenceError):
        presence.update("owner", "device-00001", "connection-1", 2, True, sessions, lookup)
    assert not presence.active("owner", sessions, lookup)
    presence.update("owner", "device-00002", "connection-2", 1, True, sessions, lookup)
    assert presence.active("owner", sessions, lookup)
    presence.clear(second)
    assert not presence.active("owner", sessions, lookup)
    with pytest.raises(NotificationPresenceError):
        presence.update("other", "device-00001", "connection-1", 4, True, sessions, lookup)


def test_reconnect_account_switch_and_token_changes_invalidate_presence():
    from orchestrator.notification_presence import NotificationPresence, NotificationPresenceError

    presence = NotificationPresence()
    old, new = object(), object()
    sessions = {old: {"sub": "owner", "exp": 4_000_000_000}, new: {"sub": "owner", "exp": 4_000_000_000}}
    identities = {("owner", "device-00001"): ("active", "fingerprint-a", 1)}
    def lookup(owner, device):
        return identities.get((owner, device))
    presence.register(old, "owner", "device-00001", "old-connection")
    presence.update("owner", "device-00001", "old-connection", 1, True, sessions, lookup)
    identities[("owner", "device-00001")] = ("active", "fingerprint-a", 3)
    assert not presence.active("owner", sessions, lookup)
    presence.register(new, "owner", "device-00001", "new-connection")
    with pytest.raises(NotificationPresenceError):
        presence.update("owner", "device-00001", "old-connection", 2, True, sessions, lookup)
    presence.update("owner", "device-00001", "new-connection", 1, True, sessions, lookup)
    assert presence.active("owner", sessions, lookup)
    sessions[new] = {"sub": "other", "exp": 4_000_000_000}
    assert not presence.active("owner", sessions, lookup)
    with pytest.raises(NotificationPresenceError):
        presence.update("owner", "device-00001", "new-connection", 2, True, sessions, lookup)
    presence.register(new, "other", "device-00001", "other-connection")
    presence.update("other", "device-00001", "other-connection", 1, True, sessions, lookup)
    identities[("other", "device-00001")] = ("active", "fingerprint-b", 1)
    assert not presence.active("other", sessions, lookup)


def test_store_outage_cannot_reuse_a_foreground_lease():
    from orchestrator.notification_presence import NotificationPresence, NotificationPresenceError

    presence = NotificationPresence()
    socket = object()
    sessions = {socket: {"sub": "owner", "exp": 4_000_000_000}}
    presence.register(socket, "owner", "device-00001", "connection-1")
    presence.update("owner", "device-00001", "connection-1", 1, True, sessions, lambda *_: None)

    def unavailable(*_):
        raise RuntimeError("unavailable")

    assert not presence.active("owner", sessions, unavailable)
    assert not presence.active("owner", sessions, lambda *_: None)
    with pytest.raises(NotificationPresenceError):
        presence.update("owner", "device-00001", "connection-1", 2, True, sessions, unavailable)
    assert presence.update("owner", "device-00001", "connection-1", 3, False,
                           sessions, unavailable)["expires_in_seconds"] == 0


def test_presence_http_roundtrip_and_strict_validation():
    from orchestrator.notification_presence import NotificationPresence

    presence, socket = NotificationPresence(), object()
    identities = SimpleNamespace(value=None, error=False)

    def lookup(*_):
        if identities.error:
            raise RuntimeError("store unavailable")
        return identities.value

    app = FastAPI()
    app.include_router(notification_router)
    app.state.orchestrator = SimpleNamespace(notification_presence=presence, ui_sessions={socket: {"sub": "owner", "exp": 4_000_000_000}},
                                             push_destinations=SimpleNamespace(presence_identity=lookup))
    app.dependency_overrides[verify_user] = lambda: {"sub": "owner", "exp": 4_000_000_000}
    client = TestClient(app, raise_server_exceptions=False)
    presence.register(socket, "owner", "device-00001", "connection-1")
    body = {"device_id": "device-00001", "connection_generation": "connection-1", "sequence": 1,
            "foreground": True}
    response = client.post("/api/notifications/presence", json=body)
    assert response.status_code == 200
    assert response.json() == {"accepted_sequence": 1, "expires_in_seconds": 45}
    assert client.post("/api/notifications/presence", json=body).status_code == 409
    for patch in ({"sequence": True}, {"sequence": 0}, {"sequence": 2**53}, {"sequence": "2"},
                  {"foreground": 1}, {"foreground": "true"}, {"owner_id": "other"}):
        assert client.post("/api/notifications/presence", json={**body, **patch}).status_code == 422
    identities.error = True
    unavailable = client.post("/api/notifications/presence", json={**body, "sequence": 2})
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "notification_presence_unavailable"
    assert client.post("/api/notifications/presence", json={**body, "sequence": 3,
                                                          "foreground": False}).json()["expires_in_seconds"] == 0
    identities.error, identities.value = False, ("revoked", "fingerprint", 1)
    assert client.post("/api/notifications/presence", json={**body, "sequence": 4}).status_code == 409


def test_connection_capacity_invalid_registration_and_same_binding_refresh():
    from orchestrator.notification_presence import NotificationPresence, NotificationPresenceError

    presence, socket = NotificationPresence(), object()
    sessions = {socket: {"sub": "owner", "exp": 4_000_000_000}}
    presence.register(socket, "owner", "device-00001", "connection-1")
    presence.update("owner", "device-00001", "connection-1", 1, True, sessions, lambda *_: None)
    presence.register(socket, "owner", "device-00001", "connection-1")
    with pytest.raises(NotificationPresenceError):
        presence.update("owner", "device-00001", "connection-1", 1, True, sessions, lambda *_: None)
    for sequence, foreground in [(True, True), (0, True), (2**53, True), (2, "true")]:
        with pytest.raises(NotificationPresenceError):
            presence.update("owner", "device-00001", "connection-1", sequence, foreground, sessions, lambda *_: None)
    presence.register(socket, "owner", None, "connection-1")
    assert not presence.active("owner", sessions, lambda *_: None)


@pytest.mark.parametrize("transition", ["background", "disconnect", "account_switch", "reconnect"])
def test_state_changed_during_destination_read_cannot_reuse_foreground_proof(transition):
    from orchestrator.notification_presence import NotificationPresence

    presence, socket = NotificationPresence(), object()
    sessions = {socket: {"sub": "owner", "exp": 4_000_000_000}}
    presence.register(socket, "owner", "device-00001", "connection-1")
    presence.update("owner", "device-00001", "connection-1", 1, True, sessions, lambda *_: None)

    def change_during_read(*_):
        if transition == "background":
            presence.update("owner", "device-00001", "connection-1", 2, False, sessions, lambda *_: None)
        elif transition == "disconnect":
            presence.clear(socket)
        elif transition == "account_switch":
            sessions[socket] = {"sub": "other", "exp": 4_000_000_000}
        else:
            presence.register(socket, "owner", "device-00001", "connection-2")
        return None

    assert not presence.active("owner", sessions, change_during_read)


def test_expired_registered_credentials_cannot_prove_current_foreground():
    from orchestrator.notification_presence import NotificationPresence, NotificationPresenceError

    presence, socket = NotificationPresence(), object()
    sessions = {socket: {"sub": "owner", "exp": 1}}
    presence.register(socket, "owner", "device-00001", "connection-1")
    with pytest.raises(NotificationPresenceError):
        presence.update("owner", "device-00001", "connection-1", 1, True, sessions, lambda *_: None)
