"""Durable push delivery over the real Plane outbox: idempotent enqueue, bounded
transient retries, permanent failure dead-letters, stale destination/token
suppression, audited outcomes, client acknowledgment, and the notify_user hook.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from audit.repository import AuditRepository
from orchestrator import push_delivery as pde
from orchestrator.push_delivery import (
    AckDestinationRevoked,
    AckInvalid,
    AckRejected,
    PushDeliveryCoordinator,
    notification_identity,
)
from orchestrator.push_destinations import PushDestinationStore
from orchestrator.push_providers import PushProviderError
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.helpers.push_sessions import SessionBoundPushStore

KEY = "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="
BASE_NOW = 1_800_000_000.0


class FakeAdapter:
    provider = "fake"

    def __init__(self, *, configured=True, error=None):
        self._configured = configured
        self.error = error
        self.calls = []

    @property
    def configured(self):
        return self._configured

    def send(self, *, device_token, title, body, notification_id, expires_at=None):
        if not self._configured:
            raise PushProviderError(
                transient=True,
                code="provider_unconfigured",
                detail="sender credentials are not configured",
            )
        self.calls.append(
            {
                "device_token": device_token,
                "title": title,
                "body": body,
                "notification_id": notification_id,
            }
        )
        if self.error is not None:
            raise self.error


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


async def test_same_token_claim_invalidates_queued_delivery_and_ack(store, coordinator, owner):
    first = store.enroll(owner, device_id="device-claim-0001", platform="android", token="token")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification(notification_id="old-claim"))
    second = store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                          expected_destination_revision=first["updated_at"])
    assert first["token_generation"] == second["token_generation"]
    await push.dispatch_once()
    assert adapter.calls == []
    with pytest.raises(AckRejected):
        await push.acknowledge(owner, first["device_id"], "old-claim")


@pytest.mark.parametrize("field,value", [("token_generation", True), ("destination_revision", None)])
async def test_queued_binding_proof_requires_strict_generation_and_revision(
    store, coordinator, owner, plane_runtime, field, value,
):
    import hashlib
    import json
    store.enroll(owner, device_id="device-strict-proof", platform="android", token="token")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification(notification_id="strict-proof"))
    row = _outbox_row(plane_runtime, owner)
    payload = plane_runtime.fetch_one("SELECT canonical_payload FROM astralplane_outbox WHERE entry_id=%s", (row["entry_id"],))
    canonical = json.loads(bytes(payload["canonical_payload"]))
    canonical[field] = value
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    plane_runtime.execute("UPDATE astralplane_outbox SET canonical_payload=%s,payload_sha256=%s WHERE entry_id=%s",
                          (raw, hashlib.sha256(raw).hexdigest(), row["entry_id"]))
    await push.dispatch_once()
    assert adapter.calls == []


async def test_provider_invalid_callback_cannot_revoke_new_same_token_claim(store, coordinator, owner):
    first = store.enroll(owner, device_id="device-claim-0002", platform="android", token="token")

    class RefreshDuringSend(FakeAdapter):
        def send(self, **kwargs):
            store.enroll(owner, device_id=first["device_id"], platform="android", token="token",
                         expected_destination_revision=first["updated_at"])
            raise PushProviderError(transient=False, code="token_unregistered")

    push = coordinator({"fcm": RefreshDuringSend()})
    await push.enqueue_notification(owner, _notification(notification_id="old-invalid-claim"))
    await push.dispatch_once()
    current = store.get_destination(owner, first["device_id"])
    assert current["status"] == "active" and current["updated_at"] > first["updated_at"]


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("push_delivery") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", KEY)


@pytest.fixture()
def owner() -> str:
    return f"push-owner-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def clean_outbox(plane_runtime):
    plane_runtime.execute(
        "DELETE FROM astralplane_outbox WHERE topic = %s", (pde.OUTBOX_TOPIC,)
    )


@pytest.fixture()
def audit_repository(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture()
def audit_events(monkeypatch, audit_repository) -> list:
    events = []
    original_insert = audit_repository.insert
    original_in_txn = audit_repository.insert_in_transaction

    def append(event):
        receipt = original_insert(event)
        events.append(event)
        return receipt

    def append_in_txn(event, **kwargs):
        receipt = original_in_txn(event, **kwargs)
        events.append(event)
        return receipt

    monkeypatch.setattr(audit_repository, "insert", append)
    monkeypatch.setattr(audit_repository, "insert_in_transaction", append_in_txn)
    return events


@pytest.fixture()
def store(plane_runtime, audit_repository) -> PushDestinationStore:
    return SessionBoundPushStore(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repository=audit_repository,
    )


@pytest.fixture()
def coordinator(plane_runtime, store, audit_repository):
    def build(adapters, **kwargs):
        options = {
            "audit_repository": audit_repository,
            "clock": lambda: BASE_NOW,
            "delivery_policy": lambda *_: True,
        }
        options.update(kwargs)
        return PushDeliveryCoordinator(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
            destinations=store,
            adapters=adapters,
            **options,
        )

    return build


def _owner_rows(plane_runtime, owner):
    return plane_runtime.fetch_all(
        "SELECT entry_id, topic, status, attempt_count, last_error_code, "
        "available_at FROM astralplane_outbox WHERE topic = %s "
        "AND convert_from(canonical_payload, 'utf8') LIKE %s",
        (pde.OUTBOX_TOPIC, f'%"owner_id":"{owner}"%'),
    )


def _outbox_row(plane_runtime, owner):
    rows = _owner_rows(plane_runtime, owner)
    assert len(rows) == 1
    return rows[0]


def _actions(audit_events):
    return [event.action_type for event in audit_events]


def test_notification_identity_explicit_and_fallback():
    explicit = notification_identity(
        "u", _notification(notification_id="sched-20260101-a"), now_s=BASE_NOW
    )
    assert explicit == "sched-20260101-a"
    derived = notification_identity("u", _notification(), now_s=BASE_NOW)
    again = notification_identity("u", _notification(), now_s=BASE_NOW)
    later = notification_identity("u", _notification(), now_s=BASE_NOW + 61)
    assert derived == again
    assert derived != later
    assert len(derived) == 64
    assert notification_identity("u", {"type": "chat_status"}, now_s=BASE_NOW) is None
    assert notification_identity("u", {"type": "notification"}, now_s=BASE_NOW) is None
    assert (
        notification_identity(
            "u", _notification(notification_id="short"), now_s=BASE_NOW
        )
        is not None
    )


def test_minimal_payload_bounds_and_fields():
    data = pde.minimal_payload(
        _notification(
            title="T" * 300,
            body="B\n\r" + "x" * 400,
            activity_id=42,
            assignment_id="a" * 300,
        ),
        "notif-1",
    )
    assert data == {"notification_id": "notif-1", "title": "AstralDeep",
                    "body": "You have a new notification."}
    empty = pde.minimal_payload({"type": "notification", "title": "  "}, "notif-2")
    assert empty is None
    untitled = pde.minimal_payload(
        {"type": "notification", "notification_id": "notif-3"}, "notif-3"
    )
    assert untitled is None


async def test_enqueue_fans_out_per_destination_and_dedupes(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-enroll-01", platform="android", token="tok-1")
    store.enroll(owner, device_id="device-enroll-02", platform="ios", token="tok-2")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter, "apns": adapter})
    payload = _notification()
    queued = await push.enqueue_notification(owner, payload)
    assert queued == 2
    rows = plane_runtime.fetch_all(
        "SELECT idempotency_key, status FROM astralplane_outbox WHERE topic = %s",
        (pde.OUTBOX_TOPIC,),
    )
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"pending"}
    assert _actions(audit_events).count("push.delivery.queued") == 2
    duplicate = await push.enqueue_notification(owner, payload)
    assert duplicate == 0
    rows = plane_runtime.fetch_all(
        "SELECT idempotency_key FROM astralplane_outbox WHERE topic = %s",
        (pde.OUTBOX_TOPIC,),
    )
    assert len(rows) == 2


async def test_enqueue_skips_non_notification_and_unbound_payloads(
    plane_runtime, store, coordinator, owner
):
    store.enroll(owner, device_id="device-skip-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})
    assert await push.enqueue_notification(owner, {"type": "chat_status"}) == 0
    assert await push.enqueue_notification(owner, {"type": "notification"}) == 0
    assert await push.enqueue_notification(owner, _notification()) == 1


async def test_delivery_success_records_outcome(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-deliver-01", platform="android", token="tok-1")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())
    processed = await push.dispatch_once()
    assert processed == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "succeeded"
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["device_token"] == "tok-1"
    assert adapter.calls[0]["notification_id"]
    assert "push.delivery.delivered" in _actions(audit_events)


async def test_transient_failure_retries_with_backoff_then_recovers(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-retry-01", platform="android", token="tok-1")
    failing = FakeAdapter(
        error=PushProviderError(
            transient=True, code="provider_unavailable", detail="503"
        )
    )
    push = coordinator({"fcm": failing})
    await push.enqueue_notification(owner, _notification())
    from datetime import datetime, timezone

    base = datetime.fromtimestamp(BASE_NOW, tz=timezone.utc)
    assert await push.dispatch_once(now=base) == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "retry"
    assert row["attempt_count"] == 1
    assert row["last_error_code"] == "provider_unavailable"
    delay = pde._retry_delay(1)
    recovered = FakeAdapter()
    push2 = coordinator({"fcm": recovered})
    processed = await push2.dispatch_once(
        now=datetime.fromtimestamp(BASE_NOW + delay + 1, tz=timezone.utc)
    )
    assert processed == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "succeeded"
    assert "push.delivery.unavailable" in _actions(audit_events)
    assert "push.delivery.delivered" in _actions(audit_events)


async def test_transient_failures_exhaust_into_failed_dead_letter(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-exhaust-01", platform="android", token="tok-1")
    failing = FakeAdapter(
        error=PushProviderError(
            transient=True, code="provider_unavailable", detail="503"
        )
    )
    push = coordinator({"fcm": failing})
    await push.enqueue_notification(owner, _notification())
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(BASE_NOW, tz=timezone.utc)
    for _ in range(pde.MAX_DELIVERY_ATTEMPTS):
        await push.dispatch_once(now=moment)
        row = _outbox_row(plane_runtime, owner)
        if row["status"] == "dead_letter":
            break
        moment = row["available_at"]
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == pde.MAX_DELIVERY_ATTEMPTS
    assert _actions(audit_events).count("push.delivery.unavailable") == (
        pde.MAX_DELIVERY_ATTEMPTS - 1
    )
    failed = [event for event in audit_events if event.action_type == "push.delivery.failed"]
    assert failed and failed[-1].inputs_meta["attempts_exhausted"] is True


async def test_permanent_failure_dead_letters_without_retry(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-perm-01", platform="android", token="tok-1")
    failing = FakeAdapter(
        error=PushProviderError(
            transient=False, code="request_rejected", detail="bad payload"
        )
    )
    push = coordinator({"fcm": failing})
    await push.enqueue_notification(owner, _notification())
    assert await push.dispatch_once() == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == 1
    failed = [event for event in audit_events if event.action_type == "push.delivery.failed"]
    assert failed[-1].inputs_meta["attempts_exhausted"] is False


async def test_unregistered_token_revokes_destination(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-unreg-01", platform="android", token="tok-1")
    failing = FakeAdapter(
        error=PushProviderError(
            transient=False, code="token_unregistered", detail="unregistered"
        )
    )
    push = coordinator({"fcm": failing})
    await push.enqueue_notification(owner, _notification())
    await push.dispatch_once()
    assert store.get_destination(owner, "device-unreg-01")["status"] == "revoked"


async def test_revoked_destination_between_enqueue_and_dispatch_is_suppressed(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-stale-01", platform="android", token="tok-1")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())
    store.revoke(owner, "device-stale-01")
    assert await push.dispatch_once() == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "succeeded"
    assert adapter.calls == []
    assert "push.delivery.stale_suppressed" in _actions(audit_events)


async def test_rotated_token_between_enqueue_and_dispatch_is_suppressed(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-rot-01", platform="android", token="tok-1")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())
    store.enroll(owner, device_id="device-rot-01", platform="android", token="tok-2")
    assert await push.dispatch_once() == 1
    assert adapter.calls == []
    assert "push.delivery.stale_suppressed" in _actions(audit_events)


async def test_unconfigured_provider_is_transient_unavailable(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-unconf-01", platform="ios", token="tok-1")
    push = coordinator({"apns": FakeAdapter(configured=False)})
    await push.enqueue_notification(owner, _notification(platform="ios"))
    assert await push.dispatch_once() == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "retry"
    unavailable = [
        event for event in audit_events if event.action_type == "push.delivery.unavailable"
    ]
    assert unavailable[-1].inputs_meta["error_code"] == "provider_unconfigured"


async def test_acknowledge_records_client_ack_and_stale_denials(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-ack-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})
    await push.enqueue_notification(owner, _notification(notification_id="sched-20260101-a"))
    await push.acknowledge(owner, "device-ack-01", "sched-20260101-a")
    assert "push.delivery.client_acked" in _actions(audit_events)
    with pytest.raises(AckInvalid):
        await push.acknowledge(owner, "device-ack-01", "bad id!")
    with pytest.raises(AckRejected):
        await push.acknowledge(owner, "device-other-999", "sched-20260101-a")
    store.revoke(owner, "device-ack-01")
    with pytest.raises(AckDestinationRevoked):
        await push.acknowledge(owner, "device-ack-01", "sched-20260101-a")
    denials = [
        event for event in audit_events if event.action_type == "push.delivery.ack_denied"
    ]
    assert len(denials) == 3


async def test_dispatch_survives_audit_outage_and_leaves_entry_queued(
    plane_runtime, store, coordinator, owner, audit_repository, monkeypatch
):
    store.enroll(owner, device_id="device-audit-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})
    await push.enqueue_notification(owner, _notification())

    def broken(event):
        raise RuntimeError("audit chain unavailable")

    monkeypatch.setattr(audit_repository, "insert", broken)
    processed = await push.dispatch_once()
    assert processed == 0
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "claimed"


async def test_run_loop_sleeps_between_empty_passes(store, coordinator, owner):
    store.enroll(owner, device_id="device-loop-01", platform="android", token="tok-1")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    push = coordinator({"fcm": FakeAdapter()}, sleep=fake_sleep, poll_seconds=0.01)
    with pytest.raises(asyncio.CancelledError):
        await push.run_loop()
    assert sleeps == [0.01, 0.01]


async def test_run_loop_survives_dispatch_failures(store, coordinator, owner, monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    push = coordinator({"fcm": FakeAdapter()}, sleep=fake_sleep, poll_seconds=0.01)
    async def broken_dispatch(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(push, "dispatch_once", broken_dispatch)
    with pytest.raises(asyncio.CancelledError):
        await push.run_loop()
    assert len(sleeps) == 2


def test_notify_user_hook_fans_out_to_push(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    enqueued = []

    class FakeOrch:
        ui_clients = []
        push_delivery = None

        def _get_user_id(self, ws):
            return "u1"

        async def _safe_send(self, ws, data):
            return True

    class FakePush:
        async def enqueue_notification(self, user_id, payload):
            enqueued.append((user_id, payload))
            return 1

    class BrokenPush:
        async def enqueue_notification(self, user_id, payload):
            raise RuntimeError("outbox down")

    orch = FakeOrch()
    orch.push_delivery = FakePush()

    payload = _notification()
    asyncio.run(Orchestrator.notify_user(orch, "u1", payload))
    assert enqueued == []

    import shared.feature_flags as ff

    monkeypatch.setitem(ff.flags._flags, "push_delivery", True)
    monkeypatch.setitem(ff.flags._flags, "notification_routing", True)
    async def route(**_kwargs):
        return {"foreground": False, "push": True}
    orch.notification_routing = type("Routing", (), {"route": staticmethod(route)})()
    orch._owner_surface_active = lambda _owner: False
    asyncio.run(Orchestrator.notify_user(orch, "u1", payload))
    assert enqueued == [("u1", payload)]

    orch.push_delivery = BrokenPush()
    asyncio.run(Orchestrator.notify_user(orch, "u1", payload))


def test_provider_configured_and_delivery_accessors(coordinator):
    push = coordinator({"fcm": FakeAdapter(configured=True), "apns": FakeAdapter(configured=False)})
    assert push.provider_configured("fcm") is True
    assert push.provider_configured("apns") is False
    assert push.provider_configured("unknown") is False
    assert push.audit_repository is not None
    assert push.destinations is not None


async def test_metrics_rejection_is_tolerated(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-metric-01", platform="android", token="tok-1")

    def broken_metrics(name, labels):
        raise RuntimeError("registry down")

    push = coordinator({"fcm": FakeAdapter()}, metrics=broken_metrics)
    await push.enqueue_notification(owner, _notification())
    assert await push.dispatch_once() == 1


async def test_audit_receipt_mismatch_fails_delivery_outcome(
    plane_runtime, store, coordinator, owner, audit_repository, monkeypatch
):
    store.enroll(owner, device_id="device-receipt-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})
    await push.enqueue_notification(owner, _notification())
    original = audit_repository.insert

    def wrong_receipt(event):
        receipt = original(event)
        return receipt.model_copy(update={"action_type": "tampered"})

    monkeypatch.setattr(audit_repository, "insert", wrong_receipt)
    assert await push.dispatch_once() == 0
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "claimed"


async def test_enqueue_transition_receipt_mismatch_rolls_back(
    plane_runtime, store, coordinator, owner, audit_repository, monkeypatch
):
    store.enroll(owner, device_id="device-txn-receipt-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})
    original = audit_repository.insert_in_transaction

    def wrong_receipt(event, **kwargs):
        receipt = original(event, **kwargs)
        return receipt.model_copy(update={"outcome": "interrupted"})

    monkeypatch.setattr(audit_repository, "insert_in_transaction", wrong_receipt)
    with pytest.raises(pde.PushDeliveryError):
        await push.enqueue_notification(owner, _notification())
    assert not _owner_rows(plane_runtime, owner)


async def test_enqueue_drop_when_payload_not_push_eligible(
    plane_runtime, store, coordinator, owner
):
    store.enroll(owner, device_id="device-drop-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})
    queued = await push.enqueue_notification(
        owner,
        {"type": "notification", "notification_id": "explicit-id-0001"},
    )
    assert queued == 0


async def test_enqueue_without_destinations_returns_zero(
    plane_runtime, store, coordinator, owner
):
    push = coordinator({"fcm": FakeAdapter()})
    assert await push.enqueue_notification(owner, _notification()) == 0


async def test_enqueue_unexpected_plane_error_propagates(
    plane_runtime, store, coordinator, owner, monkeypatch
):
    from astralplane.errors import PlaneError

    store.enroll(owner, device_id="device-plane-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})

    def conflicting(transaction, entry):
        raise PlaneError("boom", code="outbox_write_invalid")

    monkeypatch.setattr(push._outbox, "enqueue", conflicting)
    with pytest.raises(PlaneError):
        await push.enqueue_notification(owner, _notification())


async def test_malformed_outbox_payload_dead_letters(
    plane_runtime, store, coordinator, owner, audit_events
):
    from astralplane.contracts import OutboxEntry
    import hashlib
    from datetime import datetime, timezone

    push = coordinator({"fcm": FakeAdapter()})
    entry = OutboxEntry(
        entry_id="push-malformed0000000000000000000000000000",
        topic=pde.OUTBOX_TOPIC,
        canonical_payload=b"{not-json",
        payload_sha256=hashlib.sha256(b"{not-json").hexdigest(),
        idempotency_key="push:malformed:device-malformed-01",
        available_at=datetime.now(timezone.utc),
    )
    with push._runtime.transaction() as transaction:
        push._outbox.enqueue(transaction, entry)
    assert await push.dispatch_once() == 1
    row = plane_runtime.fetch_one(
        "SELECT status FROM astralplane_outbox WHERE entry_id = %s",
        (entry.entry_id,),
    )
    assert row["status"] == "dead_letter"


async def test_missing_adapter_is_transient_unavailable(
    plane_runtime, store, coordinator, owner, audit_events
):
    store.enroll(owner, device_id="device-noadapter-01", platform="android", token="tok-1")
    push = coordinator({})
    await push.enqueue_notification(owner, _notification())
    assert await push.dispatch_once() == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "retry"
    unavailable = [
        event for event in audit_events if event.action_type == "push.delivery.unavailable"
    ]
    assert unavailable[-1].inputs_meta["error_code"] == "provider_unconfigured"


async def test_fence_loss_does_not_crash_delivery(
    plane_runtime, store, coordinator, owner, audit_events, monkeypatch
):
    store.enroll(owner, device_id="device-fence-01", platform="android", token="tok-1")
    push = coordinator({"fcm": FakeAdapter()})
    await push.enqueue_notification(owner, _notification())

    def fenced_ack(transaction, **kwargs):
        raise RuntimeError("fence lost")

    monkeypatch.setattr(push._outbox, "ack", fenced_ack)
    assert await push.dispatch_once() == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "claimed"


def test_push_delivery_metric_helper_without_observability():
    from orchestrator.orchestrator import Orchestrator

    class FakeOrch:
        pass

    Orchestrator._push_delivery_metric(FakeOrch(), "push_delivery_outcome_total", {"outcome": "failed"})

    class FakeObservability:
        def __init__(self):
            self.calls = []

        def record(self, name, *, value=1, labels=None):
            self.calls.append((name, labels))

    orch = FakeOrch()
    orch.runtime_observability = FakeObservability()
    Orchestrator._push_delivery_metric(
        orch, "push_delivery_outcome_total", {"outcome": "failed"}
    )
    assert orch.runtime_observability.calls == [
        ("push_delivery_outcome_total", {"outcome": "failed"})
    ]


async def test_explicit_notification_identity_cannot_collide_across_owners(
    plane_runtime, store, coordinator, owner
):
    other = f"push-owner-{uuid.uuid4().hex[:12]}"
    for principal in (owner, other):
        store.enroll(principal, device_id="device-shared-01", platform="android", token=principal)
    push = coordinator({"fcm": FakeAdapter()})
    payload = _notification(notification_id="same-notification")
    assert await push.enqueue_notification(owner, payload) == 1
    assert await push.enqueue_notification(other, payload) == 1
    assert len(_owner_rows(plane_runtime, owner)) == 1
    assert len(_owner_rows(plane_runtime, other)) == 1


async def test_acknowledgment_requires_a_notification_queued_for_the_owner_and_device(
    store, coordinator, owner
):
    store.enroll(owner, device_id="device-forged-01", platform="android", token="token")
    push = coordinator({"fcm": FakeAdapter()})
    with pytest.raises(AckRejected):
        await push.acknowledge(owner, "device-forged-01", "never-enqueued-id")


async def test_acknowledgment_cannot_borrow_another_owners_or_devices_delivery(
    store, coordinator, owner
):
    other = f"push-owner-{uuid.uuid4().hex[:12]}"
    store.enroll(owner, device_id="device-owner-01", platform="android", token="first")
    push = coordinator({"fcm": FakeAdapter()})
    await push.enqueue_notification(owner, _notification(notification_id="owner-bound-id"))
    store.enroll(owner, device_id="device-owner-02", platform="android", token="second")
    store.enroll(other, device_id="device-owner-01", platform="android", token="third")
    with pytest.raises(AckRejected):
        await push.acknowledge(owner, "device-owner-02", "owner-bound-id")
    with pytest.raises(AckRejected):
        await push.acknowledge(other, "device-owner-01", "owner-bound-id")
    await push.acknowledge(owner, "device-owner-01", "owner-bound-id")
    store.enroll(owner, device_id="device-owner-01", platform="android", token="rotated")
    with pytest.raises(AckRejected):
        await push.acknowledge(owner, "device-owner-01", "owner-bound-id")


@pytest.mark.parametrize("return_to_old_token", [False, True])
async def test_stale_provider_rejection_cannot_revoke_a_rotated_destination(
    store, coordinator, owner, return_to_old_token
):
    device = "device-race-01"
    store.enroll(owner, device_id=device, platform="android", token="old-token")

    class RotatingAdapter(FakeAdapter):
        def send(self, **kwargs):
            store.enroll(owner, device_id=device, platform="android", token="new-token")
            if return_to_old_token:
                store.enroll(owner, device_id=device, platform="android", token="old-token")
            raise PushProviderError(transient=False, code="token_unregistered")

    push = coordinator({"fcm": RotatingAdapter()})
    await push.enqueue_notification(owner, _notification())
    await push.dispatch_once()
    destination = store.delivery_target(owner, device)
    assert destination is not None
    assert destination["push_token"] == ("old-token" if return_to_old_token else "new-token")
    assert destination["token_generation"] == (3 if return_to_old_token else 2)


async def test_exhausted_claim_cannot_contact_the_provider(
    plane_runtime, store, coordinator, owner
):
    store.enroll(owner, device_id="device-limit-01", platform="android", token="token")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())
    plane_runtime.execute(
        "UPDATE astralplane_outbox SET attempt_count = %s WHERE topic = %s",
        (pde.MAX_DELIVERY_ATTEMPTS, pde.OUTBOX_TOPIC),
    )
    await push.dispatch_once()
    assert adapter.calls == []
    assert _outbox_row(plane_runtime, owner)["status"] == "dead_letter"


async def test_success_followed_by_audit_outage_cannot_be_resent_after_lease_expiry(
    plane_runtime, store, coordinator, owner, audit_repository, monkeypatch
):
    from datetime import timedelta

    store.enroll(owner, device_id="device-uncertain-01", platform="android", token="token")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())
    original_insert = audit_repository.insert
    moment = pde._utc_now()

    def fail_after_success(event):
        if event.action_type == "push.delivery.delivered":
            raise RuntimeError("audit chain unavailable after provider acceptance")
        return original_insert(event)

    monkeypatch.setattr(audit_repository, "insert", fail_after_success)
    assert await push.dispatch_once(now=moment) == 0
    assert len(adapter.calls) == 1
    monkeypatch.setattr(audit_repository, "insert", original_insert)
    await push.dispatch_once(now=moment + pde.LEASE_DURATION + timedelta(seconds=1))
    assert len(adapter.calls) == 1
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == "dead_letter"
    assert row["last_error_code"] == "effect_uncertain"


async def test_waiting_deliveries_do_not_hold_a_lease_during_another_send(
    plane_runtime, store, coordinator, owner
):
    for index in range(2):
        store.enroll(owner, device_id=f"device-lease-{index:02d}", platform="android", token=str(index))

    class LeaseObservingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.claim_counts = []

        def send(self, **kwargs):
            rows = _owner_rows(plane_runtime, owner)
            self.claim_counts.append(sum(row["status"] == "claimed" for row in rows))
            return super().send(**kwargs)

    adapter = LeaseObservingAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())
    assert await push.dispatch_once() == 1
    assert adapter.claim_counts == [1]
    assert sorted(row["status"] for row in _owner_rows(plane_runtime, owner)) == ["pending", "succeeded"]
    assert await push.dispatch_once() == 1
    assert adapter.claim_counts == [1, 1]


async def test_durable_provider_success_reconciles_a_lost_ack_without_resending(
    plane_runtime, store, coordinator, owner, monkeypatch
):
    from datetime import timedelta

    store.enroll(owner, device_id="device-reconcile", platform="android", token="token")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())
    original_ack = push._outbox.ack
    moment = pde._utc_now()

    def lost_ack(*args, **kwargs):
        raise RuntimeError("acknowledgment unavailable")

    monkeypatch.setattr(push._outbox, "ack", lost_ack)
    assert await push.dispatch_once(now=moment) == 1
    assert _outbox_row(plane_runtime, owner)["status"] == "claimed"
    monkeypatch.setattr(push._outbox, "ack", original_ack)
    await push.dispatch_once(now=moment + pde.LEASE_DURATION + timedelta(seconds=1))
    assert len(adapter.calls) == 1
    assert _outbox_row(plane_runtime, owner)["status"] == "succeeded"


async def test_returning_to_an_old_token_never_revives_an_old_delivery(coordinator, store, owner, plane_runtime):
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    device = "device-generation-01"
    store.enroll(owner, device_id=device, platform="android", token="generation-a")
    await push.enqueue_notification(owner, _notification(notification_id="generation-old-delivery"))
    store.enroll(owner, device_id=device, platform="android", token="generation-b")
    store.enroll(owner, device_id=device, platform="android", token="generation-a")
    await push.dispatch_once()
    assert adapter.calls == []
    assert _outbox_row(plane_runtime, owner)["status"] == "succeeded"
    with pytest.raises(AckRejected):
        await push.acknowledge(owner, device, "generation-old-delivery")


async def test_persisted_push_payload_never_contains_source_or_notification_content(coordinator, store, owner, plane_runtime):
    store.enroll(owner, device_id="device-privacy-0001", platform="android", token="privacy-token")
    push = coordinator({"fcm": FakeAdapter()})
    assert await push.enqueue_notification(owner, _notification(title="Patient Private",
        body="MRN 123456", source="Private source", chat_id="private-chat")) == 1
    rows = plane_runtime.fetch_all("SELECT canonical_payload FROM astralplane_outbox WHERE topic = %s",
                                   (pde.OUTBOX_TOPIC,))
    assert len(rows) == 1
    raw = bytes(rows[0]["canonical_payload"])
    assert b"Patient" not in raw and b"MRN" not in raw and b"Private" not in raw and b"private-chat" not in raw


@pytest.mark.parametrize("outage", [False, True])
async def test_dispatch_rechecks_current_owner_policy_before_provider_effect(coordinator, store, owner,
                                                                             plane_runtime, outage):
    store.enroll(owner, device_id="device-policy-0001", platform="android", token="policy-token")
    adapter = FakeAdapter()
    push = coordinator({"fcm": adapter})
    await push.enqueue_notification(owner, _notification())

    def policy(*_):
        if outage:
            raise RuntimeError("policy unavailable")
        return False

    push._delivery_policy = policy
    await push.dispatch_once()
    assert adapter.calls == []
    row = _outbox_row(plane_runtime, owner)
    assert row["status"] == ("retry" if outage else "succeeded")


async def test_expired_queued_notification_cannot_contact_provider(coordinator, store, owner, plane_runtime,
                                                                  audit_events):
    store.enroll(owner, device_id="device-expiry-0001", platform="android", token="expiry-token")
    adapter = FakeAdapter()
    clock = {"now": BASE_NOW}
    push = coordinator({"fcm": adapter}, clock=lambda: clock["now"])
    await push.enqueue_notification(owner, _notification(delivery_expires_at=int(BASE_NOW) + 10))
    clock["now"] += 11
    await push.dispatch_once()
    assert adapter.calls == []
    assert _outbox_row(plane_runtime, owner)["status"] == "succeeded"
    suppressed = [event for event in audit_events if event.action_type == "push.delivery.stale_suppressed"]
    assert suppressed[-1].inputs_meta["reason"] == "notification_expired"
    assert suppressed[-1].inputs_meta["delivery_outcome"] == "suppressed"
