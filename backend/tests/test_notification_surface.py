"""Notification chrome host adapters expose current owner policy and inbox state
and enforce socket identity, revision fences and current source ownership.
"""

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.projection_surfaces import get_surface
from shared.feature_flags import flags
from tests import test_notification_api_268 as notification_fixtures

audit_repository = notification_fixtures.audit_repository
clock = notification_fixtures.clock
coordinator = notification_fixtures.coordinator
encryption_key = notification_fixtures.encryption_key
owner = notification_fixtures.owner
plane_runtime = notification_fixtures.plane_runtime


def test_notification_surface_is_registered():
    assert get_surface("notifications") is not None


@pytest.fixture
def surface_env(monkeypatch, coordinator, owner):
    monkeypatch.setitem(flags._flags, "notification_routing", True)
    socket = object()
    orch = SimpleNamespace(notification_routing=coordinator, ui_sessions={socket: {"sub": owner}})
    orch.history = SimpleNamespace(get_chat=lambda chat, user_id: {"id": chat} if user_id == owner else None)
    return get_surface("notifications"), orch, socket


def _entry(coordinator, owner):
    asyncio.run(coordinator.route(owner_id=owner, payload={"type": "notification", "title": "Ready",
        "body": "Details", "chat_id": "chat-1"}, has_active_surface=False, push_available=True))
    return coordinator.list_inbox(owner)[0]


def test_host_state_requires_current_owner_source_authorization(surface_env, coordinator, owner):
    surface, orch, _socket = surface_env
    entry = _entry(coordinator, owner)
    state = surface._state(orch, owner, {"entry_id": entry["entry_id"], "chat_id": "forged"})
    assert state["selected_entry"]["content"]["title"] == "Ready"
    assert state["source"] == {"chat_id": "chat-1"}
    orch.history.get_chat = lambda *_args, **_kwargs: None
    assert surface._state(orch, owner, {"entry_id": entry["entry_id"]})["source"] is None
    assert surface._state(orch, "other", {})["entries"] == []
    from orchestrator.notification_routing import NotificationEntryNotFound
    with pytest.raises(NotificationEntryNotFound):
        surface._state(orch, "other", {"entry_id": entry["entry_id"]})


async def test_surface_mutations_apply_revision_fences_and_refresh_state(surface_env, coordinator, owner):
    from orchestrator.notification_routing import NotificationEntryConflict, NotificationPolicyConflict, default_preferences

    surface, orch, socket = surface_env
    preferences = {key: value for key, value in default_preferences().items() if key not in {"revision", "updated_at"}}
    update = {"expected_revision": 0, **preferences, "push_completion": False}
    assert await surface._preferences(orch, socket, owner, [], {"fields": update}) == ("notifications", {}, "")
    assert not coordinator.preferences(owner)["push_completion"]
    with pytest.raises(NotificationPolicyConflict):
        await surface._preferences(orch, socket, owner, [], update)
    await surface._quiet_hours(orch, socket, owner, [], {"expected_revision": 0, "enabled": True,
        "start_minute": 1320, "end_minute": 480, "tz_offset_minutes": -240})
    assert coordinator.quiet_hours(owner)["tz_offset_minutes"] == -240
    decision = await coordinator.route(owner_id=owner, payload={"type": "notification", "title": "Ready"},
                                        has_active_surface=False, push_available=False)
    entry = coordinator.list_inbox(owner)[0]
    assert decision["entry_id"] == entry["entry_id"]
    assert await surface._open(orch, socket, owner, [], {"entry_id": entry["entry_id"]}) == (
        "notifications", {"entry_id": entry["entry_id"]}, "")
    payload = {"entry_id": entry["entry_id"], "expected_revision": entry["revision"]}
    await surface._acknowledge(orch, socket, owner, [], payload)
    with pytest.raises(NotificationEntryConflict):
        await surface._delete(orch, socket, owner, [], payload)
    await surface._delete(orch, socket, owner, [], {**payload, "expected_revision": entry["revision"] + 1})
    assert coordinator.list_inbox(owner) == []


async def test_forged_owner_extra_fields_and_disabled_surface_are_denied(surface_env, coordinator, owner, monkeypatch):
    from orchestrator.notification_routing import NotificationPolicyInvalid, NotificationStoreUnavailable
    from pydantic import ValidationError

    surface, orch, socket = surface_env
    with pytest.raises(NotificationPolicyInvalid):
        await surface._open(orch, socket, "other", [], {"entry_id": "untrusted"})
    for handler, payload in [(surface._open, []), (surface._open, {"entry_id": "untrusted", "chat_id": "forged"}),
                             (surface._delete, {"entry_id": "untrusted"})]:
        with pytest.raises(NotificationPolicyInvalid):
            await handler(orch, socket, owner, [], payload)
    with pytest.raises(ValidationError):
        await surface._quiet_hours(orch, socket, owner, [], {"expected_revision": True, "enabled": "true",
            "start_minute": 1320, "end_minute": 480, "tz_offset_minutes": 0})
    monkeypatch.setitem(flags._flags, "notification_routing", False)
    with pytest.raises(NotificationStoreUnavailable):
        surface._state(orch, owner, {})
