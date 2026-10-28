"""Runs approval decision frames through real socket admission on a socket registered the way the
native clients register (register_ui with a bearer token, no cookies) and proves the decision is a
human read the approval continuation can capture guidance from.
"""

import json
from uuid import uuid4

import pytest

from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from tests.test_human_socket_ingress_088 import (
    metadata as metadata, terminal, ingress as ingress, surface as surface, command as command,
    context as context, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, registered,
)

pytestmark = pytest.mark.asyncio


def _native(state):
    state.socket.scope["headers"] = [(key, value) for key, value in state.socket.scope["headers"]
                                     if key.lower() not in {b"cookie", b"origin"}]


def _decide(state, action, payload):
    state.socket.feed(json.dumps({"type": "ui_event", "action": action, "submission_id": str(uuid4()),
        "request_generation": str(uuid4()), "connection_generation": state.connection_generation,
        "payload": payload}))


@pytest.mark.parametrize("action,payload", [
    ("authorize_action", {"hitl_request_id": "synthetic-request", "decision": "approve"}),
    ("remote_op_decision", {"proposal_id": "0" * 32, "decision": "approve"}),
])
async def test_native_bearer_decision_carries_a_human_read_for_the_continuation(
        metadata, monkeypatch, fixture, action, payload):
    state, _calls, _audits = metadata
    _native(state)
    await registered(state)
    captured = []

    async def probe(orch, websocket, owner, decision, **_kwargs):
        origin, ready = await hc.continuation_guidance(orch, websocket)
        captured.append((owner, origin, ready))
        if origin is not None:
            origin.close()

    monkeypatch.setattr(hc, "handle_decision", probe)
    monkeypatch.setattr(rc, "handle_decision", probe)
    _decide(state, action, payload)
    assert (await terminal(state))["state"] == "completed"
    (owner, origin, ready), = captured
    assert ready is True and origin is not None and origin.owner_id == owner == fixture[1]


async def test_native_decision_without_skills_skips_the_human_read(metadata, monkeypatch, fixture):
    from shared.feature_flags import flags

    state, _calls, _audits = metadata
    monkeypatch.setitem(flags._flags, "user_skills", False)
    _native(state)
    await registered(state)
    captured = []

    async def probe(orch, websocket, owner, decision, **_kwargs):
        captured.append(await hc.continuation_guidance(orch, websocket))

    monkeypatch.setattr(hc, "handle_decision", probe)
    _decide(state, "authorize_action", {"hitl_request_id": "synthetic-request", "decision": "approve"})
    assert (await terminal(state))["state"] == "completed"
    assert captured == [(None, True)]
