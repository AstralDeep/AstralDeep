"""Deterministic notification routing and the durable inbox over the real Plane
notification and credential repositories: classification, urgency, quiet hours,
preference-gated foreground/inbox/push decisions, deduplication of unchanged
progress, delivery identities, retention purge, and the notify_user hook.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from audit.repository import AuditRepository
from cryptography.fernet import Fernet
from orchestrator import notification_routing as nre
from orchestrator.notification_routing import (
    NotificationRoutingCoordinator,
    classify_event,
    default_preferences,
    in_quiet_hours,
    resolve_urgency,
    validate_quiet_window,
)
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

KEY = "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="
BASE_NOW = 1_800_000_000.0


def _notification(**overrides):
    payload = {
        "type": "notification",
        "level": "info",
        "source": "schedule",
        "title": "Job finished",
        "body": "Your scheduled task finished.",
        "job_id": "job-1",
        "chat_id": "chat-1",
    }
    payload.update(overrides)
    return payload


class FakeClock:
    def __init__(self, now_s: float = BASE_NOW):
        self.now_s = now_s

    def __call__(self) -> float:
        return self.now_s

    def advance(self, seconds: float) -> None:
        self.now_s += seconds


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("notification_routing") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY)


@pytest.fixture()
def owner() -> str:
    return f"notif-owner-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def other_owner() -> str:
    return f"notif-owner-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def audit_repository(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def metrics() -> list[str]:
    return []


@pytest.fixture()
def coordinator(plane_runtime, audit_repository, clock, metrics):
    def _metric(name: str) -> None:
        metrics.append(name)

    return NotificationRoutingCoordinator(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repository=audit_repository,
        clock=clock,
        metrics=_metric,
    )


def _all_flags(value) -> dict[str, bool]:
    return {
        name: value
        for name in default_preferences()
        if name not in ("revision", "updated_at")
    }

def test_classification_covers_kinds_urgency_and_progress():
    assert classify_event(_notification()) == "completion"
    assert classify_event(_notification(level="error")) == "failure"
    assert classify_event(_notification(level="critical")) == "failure"
    assert classify_event(_notification(action_required=True)) == "required_action"
    assert classify_event(_notification(event_kind="required_action")) == "required_action"
    assert classify_event(_notification(event_kind="bogus")) is None
    assert classify_event({"type": "chat_status", "status": "info"}) is None
    assert classify_event({"type": "task_started"}) is None
    assert classify_event("not-a-dict") is None
    assert classify_event(None) is None
    assert resolve_urgency(_notification()) == "normal"
    assert resolve_urgency(_notification(level="error")) == "high"
    assert resolve_urgency(_notification(level="critical")) == "critical"
    assert resolve_urgency(_notification(urgency="critical")) == "critical"
    assert resolve_urgency(_notification(urgency="bogus")) == "normal"
    assert resolve_urgency(None) == "normal"


def test_quiet_hours_window_math():
    validate_quiet_window(start_minute=0, end_minute=1439, tz_offset_minutes=0)
    with pytest.raises(nre.NotificationPolicyInvalid):
        validate_quiet_window(start_minute=5, end_minute=5, tz_offset_minutes=0)
    with pytest.raises(nre.NotificationPolicyInvalid):
        validate_quiet_window(start_minute=-1, end_minute=5, tz_offset_minutes=0)
    with pytest.raises(nre.NotificationPolicyInvalid):
        validate_quiet_window(start_minute=0, end_minute=5, tz_offset_minutes=841)
    policy = {
        "quiet_enabled": True,
        "quiet_start_minute": 22 * 60,
        "quiet_end_minute": 8 * 60,
        "tz_offset_minutes": 0,
    }
    assert in_quiet_hours(policy, 23 * 3600) is True
    assert in_quiet_hours(policy, 12 * 3600) is False
    assert in_quiet_hours(policy, 7 * 3600 + 59) is True
    assert in_quiet_hours(None, 23 * 3600) is False
    assert in_quiet_hours({**policy, "quiet_enabled": False}, 23 * 3600) is False
    wrapped_offset = {**policy, "tz_offset_minutes": 120}
    assert in_quiet_hours(wrapped_offset, 21 * 3600) is True
    non_wrapped = {
        "quiet_enabled": True,
        "quiet_start_minute": 9 * 60,
        "quiet_end_minute": 17 * 60,
        "tz_offset_minutes": 0,
    }
    assert in_quiet_hours(non_wrapped, 12 * 3600) is True
    assert in_quiet_hours(non_wrapped, 18 * 3600) is False


def test_defaults_are_deliver_everywhere():
    prefs = default_preferences()
    assert all(prefs[name] for name in prefs if name not in ("revision", "updated_at"))
    assert prefs["revision"] == 0


def test_route_defaults_fan_out_everywhere(coordinator, owner, metrics):
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=True,
        )
    )
    assert decision["foreground"] is True
    assert decision["inbox"] is True
    assert decision["push"] is False
    assert decision["kind"] == "completion"
    assert "notification_routing_decisions_total" in metrics
    entries = coordinator.list_inbox(owner)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event_kind"] == "completion"
    assert entry["content"]["title"] == "Job finished"
    assert entry["content"]["job_id"] == "job-1"
    assert entry["content"]["urgency"] == "normal"
    assert entry["acknowledged_at"] is None
    assert entry["retention_expires_at"] > entry["created_at"]


def test_route_content_is_encrypted_at_rest(coordinator, owner, plane_runtime):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="Sensitive clinical result"),
            has_active_surface=True,
            push_available=False,
        )
    )
    rows = plane_runtime.fetch_all(
        "SELECT content_ciphertext FROM notification_inbox_entry WHERE owner_id = %s",
        (owner,),
    )
    assert len(rows) == 1
    ciphertext = rows[0]["content_ciphertext"]
    raw = bytes(ciphertext)
    assert b"Sensitive clinical result" not in raw
    plaintext = Fernet(KEY.encode()).decrypt(raw)
    assert json.loads(plaintext)["title"] == "Sensitive clinical result"


def test_route_escalates_push_only_without_active_surface(coordinator, owner):
    with_surface = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="First"),
            has_active_surface=True,
            push_available=True,
        )
    )
    assert with_surface["push"] is False
    without_surface = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="Second"),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert without_surface["push"] is True
    identities = plane_runtime_identities(coordinator, owner)
    channels = {identity.delivery_key[: len("delivery:")] for identity in identities}
    assert channels == {"delivery:"}
    keys = [identity.delivery_key for identity in identities]
    assert len(keys) == len(set(keys))


def plane_runtime_identities(coordinator, owner):
    return coordinator._notifications.call(
        lambda transaction: coordinator._notifications.repository.list_delivery_identities(
            transaction, owner_id=owner, limit=200
        )
    )


def test_route_push_requires_push_available(coordinator, owner):
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=False,
            push_available=False,
        )
    )
    assert decision["push"] is False
    assert decision["inbox"] is True


def test_duplicate_within_window_is_suppressed(coordinator, owner, metrics, clock):
    first = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert first["inbox"] is True
    second = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert second["duplicate"] is True
    assert second["foreground"] is False
    assert second["push"] is False
    assert second["inbox"] is False
    assert "notification_routing_duplicates_total" in metrics
    assert len(coordinator.list_inbox(owner)) == 1
    clock.advance(nre.SUPPRESSION_WINDOW_SECONDS + 1)
    third = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert third["duplicate"] is False
    assert third["inbox"] is True
    assert len(coordinator.list_inbox(owner)) == 2


def test_quiet_hours_suppress_foreground_and_push_but_keep_inbox(
    coordinator, owner, clock
):
    coordinator.update_quiet_hours(
        owner,
        expected_revision=0,
        enabled=True,
        start_minute=0,
        end_minute=1439,
        tz_offset_minutes=0,
    )
    clock.advance(1)
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert decision["quiet"] is True
    assert decision["foreground"] is False
    assert decision["push"] is False
    assert decision["inbox"] is True


def test_critical_urgency_bypasses_quiet_hours(coordinator, owner, clock):
    coordinator.update_quiet_hours(
        owner,
        expected_revision=0,
        enabled=True,
        start_minute=0,
        end_minute=1439,
        tz_offset_minutes=0,
    )
    clock.advance(1)
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(urgency="critical", title="Approval needed"),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert decision["quiet"] is True
    assert decision["foreground"] is True
    assert decision["push"] is True


def test_preferences_gate_each_channel(coordinator, owner, clock):
    stored = coordinator.update_preferences(
        owner,
        expected_revision=0,
        in_app_completion=True,
        in_app_failure=False,
        in_app_required_action=True,
        inbox_completion=False,
        inbox_failure=True,
        inbox_required_action=True,
        push_completion=True,
        push_failure=False,
        push_required_action=True,
    )
    assert stored["revision"] == 1
    clock.advance(1)
    completion = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="Done"),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert completion["foreground"] is True
    assert completion["inbox"] is False
    assert completion["push"] is True
    failure = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(level="error", title="Failed"),
            has_active_surface=False,
            push_available=True,
        )
    )
    assert failure["foreground"] is False
    assert failure["inbox"] is True
    assert failure["push"] is False


def test_inbox_owner_isolation(coordinator, owner, other_owner):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="Private"),
            has_active_surface=True,
            push_available=False,
        )
    )
    assert coordinator.list_inbox(other_owner) == []
    entry = coordinator.list_inbox(owner)[0]
    with pytest.raises(nre.NotificationEntryNotFound):
        coordinator.get_entry(other_owner, entry["entry_id"])
    with pytest.raises(nre.NotificationEntryNotFound):
        coordinator.get_entry(owner, str(uuid.uuid4()))
    with pytest.raises(nre.NotificationEntryNotFound):
        coordinator.acknowledge(
            other_owner, entry["entry_id"], expected_revision=entry["revision"]
        )


def test_acknowledge_and_delete_revision_flow(coordinator, owner, audit_repository):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=False,
        )
    )
    entry = coordinator.list_inbox(owner)[0]
    assert entry["acknowledged_at"] is None
    coordinator.acknowledge(
        owner, entry["entry_id"], expected_revision=entry["revision"]
    )
    acknowledged = coordinator.get_entry(owner, entry["entry_id"])
    assert acknowledged["acknowledged_at"] is not None
    assert acknowledged["revision"] == entry["revision"] + 1
    with pytest.raises(nre.NotificationEntryConflict):
        coordinator.acknowledge(
            owner, entry["entry_id"], expected_revision=entry["revision"]
        )
    coordinator.delete_entry(
        owner, entry["entry_id"], expected_revision=acknowledged["revision"]
    )
    with pytest.raises(nre.NotificationEntryNotFound):
        coordinator.get_entry(owner, entry["entry_id"])
    assert coordinator.list_inbox(owner) == []
    events, _ = audit_repository.list_for_user(owner, limit=50)
    actions = {event.action_type for event in events}
    assert "notification.inbox.acknowledged" in actions
    assert "notification.inbox.deleted" in actions


def test_preference_revision_conflict(coordinator, owner):
    first = coordinator.update_preferences(
        owner,
        expected_revision=0,
        **_all_flags(True),
    )
    with pytest.raises(nre.NotificationPolicyConflict):
        coordinator.update_preferences(
            owner,
            expected_revision=0,
            **_all_flags(False),
        )
    with pytest.raises(nre.NotificationPolicyConflict):
        coordinator.update_preferences(
            owner,
            expected_revision=first["revision"] + 5,
            **_all_flags(True),
        )
    with pytest.raises(nre.NotificationPolicyInvalid):
        coordinator.update_preferences(
            owner,
            expected_revision=first["revision"],
            **{**_all_flags(True), "in_app_completion": "yes"},
        )


def test_quiet_hours_revision_flow(coordinator, owner):
    stored = coordinator.update_quiet_hours(
        owner,
        expected_revision=0,
        enabled=True,
        start_minute=22 * 60,
        end_minute=8 * 60,
        tz_offset_minutes=60,
    )
    assert stored["quiet_enabled"] is True
    assert stored["tz_offset_minutes"] == 60
    readback = coordinator.quiet_hours(owner)
    assert readback == stored
    with pytest.raises(nre.NotificationPolicyConflict):
        coordinator.update_quiet_hours(
            owner,
            expected_revision=stored["updated_at"] - 1,
            enabled=False,
            start_minute=22 * 60,
            end_minute=8 * 60,
            tz_offset_minutes=60,
        )
    updated = coordinator.update_quiet_hours(
        owner,
        expected_revision=stored["updated_at"],
        enabled=False,
        start_minute=22 * 60,
        end_minute=8 * 60,
        tz_offset_minutes=60,
    )
    assert updated["quiet_enabled"] is False
    assert coordinator.quiet_hours(owner)["quiet_enabled"] is False


def test_expired_entries_are_purged(coordinator, owner, clock, plane_runtime):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="Old"),
            has_active_surface=True,
            push_available=False,
        )
    )
    plane_runtime.execute(
        "UPDATE notification_inbox_entry SET created_at = 100, "
        "retention_expires_at = 101 WHERE owner_id = %s",
        (owner,),
    )
    clock.advance(86400 + 1)
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="New"),
            has_active_surface=True,
            push_available=False,
        )
    )
    titles = [entry["content"]["title"] for entry in coordinator.list_inbox(owner)]
    assert titles == ["New"]


def test_purge_failure_is_swallowed_and_retried_later(coordinator, owner, monkeypatch):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=False,
        )
    )
    def _broken(*args, **kwargs):
        raise RuntimeError("purge down")

    monkeypatch.setattr(
        coordinator._notifications.repository,
        "purge_expired_entries",
        _broken,
    )
    coordinator._purged_days.pop(owner, None)
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(title="Again"),
            has_active_surface=True,
            push_available=False,
        )
    )
    assert decision["inbox"] is True
    assert owner not in coordinator._purged_days


def test_non_notification_payloads_are_not_routed(coordinator, owner):
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload={"type": "task_completed", "payload": {}},
            has_active_surface=False,
            push_available=True,
        )
    )
    assert decision is None
    assert coordinator.list_inbox(owner) == []


def test_notify_user_hook_routes_and_suppresses(monkeypatch, plane_runtime, owner):
    from orchestrator.orchestrator import Orchestrator

    coordinator = NotificationRoutingCoordinator(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        clock=FakeClock(BASE_NOW),
    )
    sends = []

    class FakeOrch:
        ui_clients = ["ws-1"]
        ui_sessions = {"ws-1": {"sub": owner}}
        notification_routing = coordinator
        push_delivery = None

        def _get_user_id(self, ws):
            claims = self.ui_sessions.get(ws) or {}
            return claims.get("sub", "legacy")

        async def _safe_send(self, ws, data):
            sends.append(json.loads(data))
            return True

        def _owner_surface_active(self, user_id):
            return any(
                (claims or {}).get("sub") == user_id
                for claims in self.ui_sessions.values()
            )

    class FakePush:
        def __init__(self):
            self.enqueued = []

        async def enqueue_notification(self, user_id, payload):
            self.enqueued.append((user_id, payload))
            return 1

    push = FakePush()
    orch = FakeOrch()
    orch.push_delivery = push

    import shared.feature_flags as ff

    monkeypatch.setitem(ff.flags._flags, "notification_routing", True)
    monkeypatch.setitem(ff.flags._flags, "push_delivery", True)

    asyncio.run(Orchestrator.notify_user(orch, owner, _notification()))
    assert len(sends) == 1
    assert push.enqueued == []

    orch.ui_sessions = {}
    asyncio.run(Orchestrator.notify_user(orch, owner, _notification(title="Escalate")))
    assert len(sends) == 1
    assert len(push.enqueued) == 1

    monkeypatch.setitem(ff.flags._flags, "notification_routing", False)
    orch.ui_sessions = {"ws-1": {"sub": owner}}
    before = len(push.enqueued)
    asyncio.run(Orchestrator.notify_user(orch, owner, _notification(title="Legacy")))
    assert len(sends) == 2
    assert len(push.enqueued) == before


def test_notify_user_hook_withholds_when_routing_fails(monkeypatch, owner):
    from orchestrator.orchestrator import Orchestrator

    class BrokenCoordinator:
        async def route(self, **kwargs):
            raise RuntimeError("plane down")

    sends = []

    class FakeOrch:
        ui_clients = ["ws-1"]
        ui_sessions = {"ws-1": {"sub": owner}}
        notification_routing = BrokenCoordinator()
        push_delivery = None

        def _get_user_id(self, ws):
            claims = self.ui_sessions.get(ws) or {}
            return claims.get("sub", "legacy")

        async def _safe_send(self, ws, data):
            sends.append(data)
            return True

    class OtherUserOrch(FakeOrch):
        def _get_user_id(self, ws):
            return "someone-else"

    import shared.feature_flags as ff

    monkeypatch.setitem(ff.flags._flags, "notification_routing", True)
    monkeypatch.setitem(ff.flags._flags, "push_delivery", False)

    orch = FakeOrch()
    asyncio.run(Orchestrator.notify_user(orch, owner, _notification()))
    assert sends == []

    other = OtherUserOrch()
    asyncio.run(Orchestrator.notify_user(other, owner, _notification()))
    assert sends == []


def test_fernet_key_gaps_fail_closed(monkeypatch, plane_runtime, owner):
    coordinator = NotificationRoutingCoordinator(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(nre.NotificationStoreUnavailable):
        asyncio.run(
            coordinator.route(
                owner_id=owner,
                payload=_notification(),
                has_active_surface=True,
                push_available=False,
            )
        )
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(nre.NotificationStoreUnavailable):
        asyncio.run(
            coordinator.route(
                owner_id=owner,
                payload=_notification(),
                has_active_surface=True,
                push_available=False,
            )
        )


def test_malformed_quiet_policy_is_not_quiet():
    assert (
        in_quiet_hours(
            {
                "quiet_enabled": True,
                "quiet_start_minute": "22:00",
                "quiet_end_minute": 480,
                "tz_offset_minutes": 0,
            },
            BASE_NOW,
        )
        is False
    )
    assert (
        in_quiet_hours(
            {
                "quiet_enabled": True,
                "quiet_start_minute": 5,
                "quiet_end_minute": 5,
                "tz_offset_minutes": 0,
            },
            BASE_NOW,
        )
        is False
    )


def test_entry_content_carries_explicit_notification_id(coordinator, owner):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(notification_id="custom-notif-0001"),
            has_active_surface=True,
            push_available=False,
        )
    )
    entry = coordinator.list_inbox(owner)[0]
    assert entry["content"]["notification_id"] == "custom-notif-0001"


def test_undecryptable_entry_content_is_unavailable(
    coordinator, owner, plane_runtime
):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=False,
        )
    )
    plane_runtime.execute(
        "UPDATE notification_inbox_entry SET content_ciphertext = %s "
        "WHERE owner_id = %s",
        (b"garbage-not-fernet", owner),
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.list_inbox(owner)


def test_non_dict_entry_content_is_unavailable(coordinator, owner, plane_runtime):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=False,
        )
    )
    valid = Fernet(KEY.encode()).encrypt(b"[]")
    plane_runtime.execute(
        "UPDATE notification_inbox_entry SET content_ciphertext = %s "
        "WHERE owner_id = %s",
        (valid, owner),
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.list_inbox(owner)


def test_metric_failures_are_swallowed(plane_runtime, audit_repository, owner):
    def _broken(name):
        raise RuntimeError("metric registry down")

    coordinator = NotificationRoutingCoordinator(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repository=audit_repository,
        metrics=_broken,
    )
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=False,
        )
    )
    assert decision["inbox"] is True


def test_policy_contention_and_outages_map_correctly(
    coordinator, owner, monkeypatch
):
    def raise_serialization(*args, **kwargs):
        error = RuntimeError("serialization failure")
        error.pgcode = "40001"
        raise error

    monkeypatch.setattr(
        coordinator._credentials.repository,
        "upsert_credential",
        raise_serialization,
    )
    with pytest.raises(nre.NotificationPolicyConflict):
        coordinator.update_quiet_hours(
            owner,
            expected_revision=0,
            enabled=True,
            start_minute=0,
            end_minute=1,
            tz_offset_minutes=0,
        )

    def raise_plain(*args, **kwargs):
        raise RuntimeError("credentials table down")

    monkeypatch.setattr(
        coordinator._credentials.repository,
        "upsert_credential",
        raise_plain,
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.update_quiet_hours(
            owner,
            expected_revision=0,
            enabled=True,
            start_minute=0,
            end_minute=1,
            tz_offset_minutes=0,
        )


def test_preference_updates_require_durable_audit(plane_runtime, owner):
    coordinator = NotificationRoutingCoordinator(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repository=None,
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.update_preferences(owner, expected_revision=0, **_all_flags(True))
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.update_quiet_hours(
            owner,
            expected_revision=0,
            enabled=True,
            start_minute=0,
            end_minute=1,
            tz_offset_minutes=0,
        )


def test_audit_receipt_mismatch_is_unavailable(
    coordinator, owner, audit_repository, monkeypatch
):
    def bogus_receipt(event, **kwargs):
        return object()

    monkeypatch.setattr(
        audit_repository, "insert_in_transaction", bogus_receipt
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.update_preferences(owner, expected_revision=0, **_all_flags(True))


def test_preference_store_outages_and_invalid_input(
    coordinator, owner, monkeypatch
):
    def raise_down(*args, **kwargs):
        raise RuntimeError("preference table down")

    with pytest.raises(nre.NotificationPolicyInvalid):
        coordinator.update_preferences(owner, expected_revision=None, **_all_flags(True))
    with pytest.raises(nre.NotificationPolicyInvalid):
        coordinator.update_preferences(
            owner, expected_revision=0, **_all_flags(True), bogus_field=True
        )
    monkeypatch.setattr(
        coordinator._notifications.repository, "get_preferences", raise_down
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.preferences(owner)


def test_quiet_hours_invalid_input_and_corrupt_records(
    coordinator, owner, plane_runtime, monkeypatch
):
    with pytest.raises(nre.NotificationPolicyInvalid):
        coordinator.update_quiet_hours(
            owner,
            expected_revision=None,
            enabled=True,
            start_minute=0,
            end_minute=1,
            tz_offset_minutes=0,
        )
    with pytest.raises(nre.NotificationPolicyInvalid):
        coordinator.update_quiet_hours(
            owner,
            expected_revision=0,
            enabled="yes",
            start_minute=0,
            end_minute=1,
            tz_offset_minutes=0,
        )
    plane_runtime.execute(
        "INSERT INTO user_credentials (user_id, agent_id, credential_key, "
        "encrypted_value, updated_at) VALUES (%s, %s, %s, %s, %s)",
        (owner, nre.POLICY_NAMESPACE, nre.POLICY_CREDENTIAL_KEY, "garbage", 1),
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.quiet_hours(owner)
    valid = Fernet(KEY.encode()).encrypt(b"[]")
    plane_runtime.execute(
        "UPDATE user_credentials SET encrypted_value = %s WHERE user_id = %s "
        "AND agent_id = %s",
        (valid.decode(), owner, nre.POLICY_NAMESPACE),
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.quiet_hours(owner)

    def raise_down(*args, **kwargs):
        raise RuntimeError("credential table down")

    monkeypatch.setattr(
        coordinator._credentials.repository, "get_credential", raise_down
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.quiet_hours(owner)


def test_entry_mutation_input_validation(coordinator, owner):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=False,
        )
    )
    entry = coordinator.list_inbox(owner)[0]
    with pytest.raises(nre.NotificationPolicyInvalid):
        coordinator.acknowledge(
            owner, entry["entry_id"], expected_revision=None
        )
    with pytest.raises(nre.NotificationPolicyInvalid):
        coordinator.delete_entry(owner, entry["entry_id"], expected_revision=-1)


def test_inbox_read_outages_map_to_unavailable(coordinator, owner, monkeypatch):
    asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=False,
        )
    )
    entry = coordinator.list_inbox(owner)[0]

    def raise_down(*args, **kwargs):
        raise RuntimeError("inbox table down")

    monkeypatch.setattr(
        coordinator._notifications.repository, "get_entry", raise_down
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.get_entry(owner, entry["entry_id"])
    monkeypatch.setattr(
        coordinator._notifications.repository, "list_entries", raise_down
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        coordinator.list_inbox(owner)


def test_route_inbox_write_outage_is_unavailable(coordinator, owner, monkeypatch):
    def raise_down(*args, **kwargs):
        raise RuntimeError("inbox table down")

    monkeypatch.setattr(
        coordinator._notifications.repository, "create_entry", raise_down
    )
    with pytest.raises(nre.NotificationStoreUnavailable):
        asyncio.run(
            coordinator.route(
                owner_id=owner,
                payload=_notification(),
                has_active_surface=True,
                push_available=False,
            )
        )


def test_delivery_identity_conflict_is_swallowed(coordinator, owner, monkeypatch):
    from astralplane.repositories import RepositoryConflictError

    calls = []
    original = coordinator._notifications.repository.register_delivery_identity

    def conflict_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RepositoryConflictError("identity already exists")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        coordinator._notifications.repository,
        "register_delivery_identity",
        conflict_once,
    )
    decision = asyncio.run(
        coordinator.route(
            owner_id=owner,
            payload=_notification(),
            has_active_surface=True,
            push_available=True,
        )
    )
    assert decision["foreground"] is True
    assert decision["inbox"] is True


def test_inbox_opt_out_keeps_durable_duplicate_suppression_without_content(coordinator, owner):
    routing = default_preferences()
    routing.pop("revision")
    routing.pop("updated_at")
    coordinator.update_preferences(owner, expected_revision=0, **{**routing, "inbox_completion": False})
    event = _notification(title="Private patient result", body="Private contents")
    first = asyncio.run(coordinator.route(owner_id=owner, payload=event,
                                        has_active_surface=False, push_available=True))
    assert first["foreground"] and first["push"] and not first["inbox"]
    assert coordinator.list_inbox(owner) == []
    second = asyncio.run(coordinator.route(owner_id=owner, payload=event,
                                         has_active_surface=False, push_available=True))
    assert second["duplicate"] and not second["foreground"] and not second["push"]
    assert coordinator.list_inbox(owner) == []


@pytest.mark.parametrize("kind", ["progress", "unchanged", "unknown", ""])
def test_non_actionable_event_kinds_are_not_completion_notifications(coordinator, owner, kind):
    event = _notification(event_kind=kind)
    assert asyncio.run(coordinator.route(owner_id=owner, payload=event,
                                        has_active_surface=False, push_available=True)) is None
    assert coordinator.list_inbox(owner) == []


@pytest.mark.parametrize("field,value", [("quiet_enabled", "false"), ("quiet_start_minute", "22:00"),
                                          ("quiet_end_minute", 1500), ("tz_offset_minutes", 841)])
def test_corrupt_stored_quiet_policy_never_allows_routing(coordinator, owner, plane_runtime, field, value):
    from orchestrator.push_destinations import _encode_record

    record = {**nre.default_quiet_hours(), field: value}
    coordinator._credentials.call(coordinator._credentials.repository.upsert_credential,
        owner_id=owner, agent_id=nre.POLICY_NAMESPACE, credential_key=nre.POLICY_CREDENTIAL_KEY,
        encrypted_value=_encode_record(record), updated_at=123)
    with pytest.raises(nre.NotificationStoreUnavailable):
        asyncio.run(coordinator.route(owner_id=owner, payload=_notification(),
                                      has_active_surface=False, push_available=True))


def test_expired_content_is_unavailable_even_without_another_notification(coordinator, owner, clock):
    asyncio.run(coordinator.route(owner_id=owner, payload=_notification(),
                                  has_active_surface=False, push_available=True))
    entry = coordinator.list_inbox(owner)[0]
    clock.advance(nre.INBOX_RETENTION_SECONDS + 1)
    assert coordinator.list_inbox(owner) == []
    with pytest.raises(nre.NotificationEntryNotFound):
        coordinator.get_entry(owner, entry["entry_id"])


def test_active_surface_cannot_suppress_push_when_foreground_delivery_is_disabled(coordinator, owner):
    routing = {**_all_flags(True), "in_app_completion": False}
    coordinator.update_preferences(owner, expected_revision=0, **routing)
    decision = asyncio.run(coordinator.route(owner_id=owner, payload=_notification(),
                                            has_active_surface=True, push_available=True))
    assert not decision["foreground"] and decision["push"]
