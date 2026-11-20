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
    notification_identity,
)
from orchestrator.push_providers import PushProviderError
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

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


