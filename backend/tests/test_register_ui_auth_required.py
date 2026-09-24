"""Tests that orchestrator.py's register_ui answers an invalid, missing, expired or
rejected token with a recoverable auth_required signal (shared/protocol.py) instead
of a dead-end chat alert; mock auth still registers normally.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.orchestrator import Orchestrator
from shared.protocol import AuthRequired, Message


class _FakeWS:
    def __init__(self, label: str = ""):
        self.label = label


@pytest.fixture
def auth_audit(monkeypatch):
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_auth_event", _record)
    return events


def _make_fake(*, validate=None):
    from rote.rote import ROTE

    sent = []
    renders = []
    dashboards = []
    profiles = []

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((ws, components, target))

    async def send_dashboard(ws):
        dashboards.append(ws)

    async def llm_configured_for(user_id):
        return True

    async def replay_user_tasks(ws, user_id):
        return None

    fake = types.SimpleNamespace(
        ui_sessions={},
        _registered_events={},
        _ff_llm_first_run=False,
        _ws_active_chat={},
        _ws_welcome={},
        _replay_user_tasks=replay_user_tasks,
        llm_configured_for=llm_configured_for,
        audit_recorder=None,
        rote=ROTE(),
        history=types.SimpleNamespace(db=types.SimpleNamespace(
            get_user_preferences=lambda uid: None)),
        _load_user_preferences=lambda uid: {},
        _save_user_profile=profiles.append,
        _safe_send=_safe_send,
        send_ui_render=send_ui_render,
        send_dashboard=send_dashboard,
        compute_tools_available_for_user=lambda uid: True,
    )
    fake.validate_token = validate if validate is not None else (
        types.MethodType(Orchestrator.validate_token, fake))
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake.speech_server_available = types.MethodType(
        Orchestrator.speech_server_available, fake)
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)
    fake._sent = sent
    fake._renders = renders
    fake._dashboards = dashboards
    fake._profiles = profiles
    return fake


async def _reject_token(token):
    return None


def _fake_jwt(payload: dict) -> str:
    def b64(obj) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(payload)}.c2ln"


def _register_msg(token=None, **extra) -> str:
    body = {"type": "register_ui", "capabilities": [], **extra}
    if token is not None:
        body["token"] = token
    return json.dumps(body)


def _types(fake):
    return [m["type"] for _, m in fake._sent]


# Drains background tasks so asserts don't race them
def _run_and_drain(coro):
    async def _main():
        await coro
        for _ in range(10):
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
    asyncio.run(_main())


@pytest.mark.parametrize("same_owner", [False, True])
def test_verified_registration_retires_only_the_previous_owners_rote_cache(same_owner, auth_audit):
    from orchestrator.orchestrator import ConnectionContext
    from uuid import uuid4

    async def validate(_token):
        return {"sub": "previous" if same_owner else "replacement"}

    fake = _make_fake(validate=validate)
    socket = _FakeWS("reused")
    fake.ui_sessions[socket] = {"sub": "previous"}
    original = [{"type": "text", "component_id": "old-result", "content": "Previous owner result"}]
    fake.rote.adapt(socket, original)
    context = ConnectionContext(socket, uuid4(), time.monotonic() + 30,
        registered=same_owner, work_registrations_pending=1)
    fake._connection_contexts = {id(socket): context}
    send = fake._safe_send
    async def send_when_ready(ws, payload):
        if json.loads(payload).get("type") == "rote_config":
            assert context.registered and context.work_registrations_pending == 0
        await send(ws, payload)
    fake._safe_send = send_when_ready
    async def register():
        ready = await Orchestrator._run_ui_registration(
            fake, context, _register_msg(token="synthetic-valid-token"))
        if not context.registered:
            assert "rote_config" not in _types(fake)
            context.registered = True
            await Orchestrator._publish_registration_ready(fake, context, ready)
    _run_and_drain(register())
    assert fake.ui_sessions[socket]["sub"] == ("previous" if same_owner else "replacement")
    assert "rote_config" in _types(fake)
    assert _types(fake).count("rote_config") == 1
    assert fake.rote.get_cached_components(socket) == (original if same_owner else None)


def test_garbage_token_emits_single_auth_required_invalid(auth_audit, monkeypatch):
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    fake = _make_fake(validate=_reject_token)
    ws = _FakeWS("garbage")
    evt = asyncio.Event()
    fake._registered_events[id(ws)] = evt

    asyncio.run(fake.handle_ui_message(ws, _register_msg(token="total-garbage")))

    assert len(fake._sent) == 1
    sent_ws, payload = fake._sent[0]
    assert sent_ws is ws
    assert payload["type"] == "auth_required"
    assert payload["reason"] in ("invalid", "expired")
    assert payload["reason"] == "invalid"
    assert fake._renders == []
    assert "ui_render" not in _types(fake)
    assert fake.ui_sessions == {}
    assert fake._dashboards == []
    assert fake._profiles == []
    assert evt.is_set()


def test_missing_token_emits_auth_required_invalid(auth_audit, monkeypatch):
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    fake = _make_fake(validate=_reject_token)
    ws = _FakeWS()

    asyncio.run(fake.handle_ui_message(ws, _register_msg()))

    assert len(fake._sent) == 1
    payload = fake._sent[0][1]
    assert payload["type"] == "auth_required"
    assert payload["reason"] == "invalid"
    assert fake._renders == []


def test_expired_jwt_yields_reason_expired(auth_audit, monkeypatch):
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    fake = _make_fake(validate=_reject_token)
    ws = _FakeWS("expired")
    token = _fake_jwt({"sub": "someone", "exp": time.time() - 3600})

    asyncio.run(fake.handle_ui_message(ws, _register_msg(token=token)))

    assert len(fake._sent) == 1
    payload = fake._sent[0][1]
    assert payload["type"] == "auth_required"
    assert payload["reason"] == "expired"
    assert fake._renders == []
    assert fake.ui_sessions == {}


def test_unexpired_but_rejected_jwt_yields_reason_invalid(auth_audit, monkeypatch):
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    fake = _make_fake(validate=_reject_token)
    token = _fake_jwt({"sub": "someone", "exp": time.time() + 3600})

    asyncio.run(fake.handle_ui_message(_FakeWS(), _register_msg(token=token)))

    assert len(fake._sent) == 1
    payload = fake._sent[0][1]
    assert payload["type"] == "auth_required"
    assert payload["reason"] == "invalid"


def test_mock_auth_mode_registers_normally(auth_audit, monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    fake = _make_fake()
    ws = _FakeWS("mock")
    evt = asyncio.Event()
    fake._registered_events[id(ws)] = evt

    _run_and_drain(fake.handle_ui_message(ws, _register_msg(token="dev-token")))

    assert "auth_required" not in _types(fake)
    assert ws in fake.ui_sessions
    claims = fake.ui_sessions[ws]
    assert claims["sub"] == "test_user"
    assert claims["_raw_token"] == "dev-token"
    assert fake._profiles and fake._profiles[0]["sub"] == "test_user"
    assert "rote_config" in _types(fake)
    assert fake._dashboards == [ws]
    assert evt.is_set()
    actions = [e.get("action") for e in auth_audit]
    assert "ws_register" in actions
    assert "login_interactive" in actions


def test_auth_required_dataclass_round_trips():
    wire = AuthRequired(reason="expired").to_json()
    data = json.loads(wire)
    assert data == {"type": "auth_required", "reason": "expired"}

    parsed = Message.from_json(wire)
    assert isinstance(parsed, AuthRequired)
    assert parsed.type == "auth_required"
    assert parsed.reason == "expired"

    assert json.loads(AuthRequired().to_json()) == {
        "type": "auth_required", "reason": "invalid"}
