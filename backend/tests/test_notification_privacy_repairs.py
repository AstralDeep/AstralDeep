"""Notification policy outages and lock-screen privacy regressions exercise the
orchestrator dispatch hook and push payload boundary without provider traffic.
"""

import json
from types import SimpleNamespace

import pytest

from orchestrator import hitl_confirmation as hc
from orchestrator.orchestrator import Orchestrator
from orchestrator.push_delivery import minimal_payload
from shared.feature_flags import flags


@pytest.mark.parametrize("state", ["outage", "missing", "disabled", "unclassified", "malformed"])
async def test_push_requires_a_successful_current_owner_policy(monkeypatch, state):
    sent, enqueued = [], []

    async def route(**kwargs):
        if state == "outage":
            raise RuntimeError("policy store unavailable")
        if state == "malformed":
            return {"foreground": True}
        return None

    async def enqueue(owner, payload):
        enqueued.append((owner, payload))

    async def send(ws, data):
        sent.append(json.loads(data))
        return True

    orch = Orchestrator.__new__(Orchestrator)
    orch.ui_clients = ["owned", "other"]
    orch.ui_sessions = {"owned": {"sub": "owner"}, "other": {"sub": "other"}}
    orch._safe_send = send
    orch._get_user_id = lambda ws: orch.ui_sessions[ws]["sub"]
    orch._owner_surface_active = lambda owner: False
    orch.push_delivery = SimpleNamespace(enqueue_notification=enqueue)
    if state != "missing":
        orch.notification_routing = SimpleNamespace(route=route)
    monkeypatch.setitem(flags._flags, "notification_routing", state != "disabled")
    monkeypatch.setitem(flags._flags, "push_delivery", True)
    await orch.notify_user("owner", {"type": "notification", "title": "Finished"})
    assert enqueued == []
    assert sent == ([{"type": "notification", "title": "Finished"}] if state == "disabled" else [])


def test_lock_screen_payload_contains_only_generic_content_and_opaque_identity():
    payload = minimal_payload({"type": "notification", "title": "Patient Alice",
                               "body": "MRN 123-45-6789", "source": "Private patient",
                               "chat_id": "patient-record", "job_id": "confidential"}, "opaque-entry")
    assert payload == {"notification_id": "opaque-entry", "title": "AstralDeep",
                       "body": "You have a new notification."}


@pytest.mark.parametrize("value", ["abcdef123-45-6789", "abcd4111111111111111",
                                   "a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6"])
def test_identifier_shapes_are_still_screened_as_argument_content(monkeypatch, value):
    observed = []

    def contains_phi(text):
        observed.append(text)
        return value in text

    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
                        lambda: SimpleNamespace(contains_phi=contains_phi))
    preview, allowed = hc._review_arguments({"note": value})
    assert not allowed
    assert value not in preview
    assert value in observed[0]
