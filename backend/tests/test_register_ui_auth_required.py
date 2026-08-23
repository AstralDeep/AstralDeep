"""Feature 028 — register_ui auth failure emits AuthRequired (EC-1/FR-009/T013).

Pre-028, a ``register_ui`` with an invalid/expired token produced a dead-end
in-chat error Alert. Now the orchestrator answers with a recoverable
``auth_required`` signal (the client re-fetches ``/auth/session`` and retries,
or redirects to ``/auth/login``).

Exercises the REAL, unbound ``Orchestrator.handle_ui_message`` over a fake
``self`` (the full Orchestrator needs the whole stack), per the pattern in
tests/test_component_action.py.
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
    """Hashable, identity-compared stand-in for a websocket (SimpleNamespace
    is unhashable and compares by __dict__, which breaks the ui_sessions map
    and ROTE's per-socket profile registry)."""

    def __init__(self, label: str = ""):
        self.label = label


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_audit(monkeypatch):
    """Capture audit.hooks.record_auth_event (imported at call time inside
    handle_ui_message, so patching the module attribute is enough) and keep
    the live recorder out of the picture."""
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_auth_event", _record)
    return events


def _make_fake(*, validate=None):
    """Fake orchestrator ``self`` carrying ONLY what the register_ui branch
    of handle_ui_message touches, with the real implementation bound on.

    ``validate``: async stub for validate_token; when None the REAL
    ``Orchestrator.validate_token`` is bound (mock-auth test).
    """
    from rote.rote import ROTE

    sent = []        # (ws, parsed-json) for every _safe_send
    renders = []     # (ws, components, target) for every send_ui_render
    dashboards = []  # ws for every send_dashboard
    profiles = []    # user_data for every _save_user_profile

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((ws, components, target))

    async def send_dashboard(ws):
        dashboards.append(ws)

    async def llm_configured_for(user_id):
        # Feature 054: the register-time first-run gate predicate. These
        # tests cover the auth branch, so report "configured" and let the
        # success path deliver the welcome as before.
        return True

    fake = types.SimpleNamespace(
        ui_sessions={},
        _registered_events={},
        _ff_llm_first_run=False,
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
    # The rote_config handshake hint reads the voice runtime (absent here, so
    # it honestly reports False) through the real fail-closed predicate.
    fake.speech_server_available = types.MethodType(
        Orchestrator.speech_server_available, fake)
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)
    fake._sent = sent
    fake._renders = renders
    fake._dashboards = dashboards
    fake._profiles = profiles
    return fake


async def _reject_token(token):
    """validate_token stub: every token is rejected (Keycloak said no)."""
    return None


def _fake_jwt(payload: dict) -> str:
    """Unsigned-but-well-formed JWT (3 dot-separated base64url segments) —
    enough for the orchestrator's best-effort exp sniffing."""
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


def _run_and_drain(coro):
    """Run the handler, then drain its background tasks before asserting.

    Feature 052 (FR-012) moved the profile save and the ws_register/
    login_interactive audit pair off the handshake's critical path into
    tasks that complete shortly after — the success-path assertions must
    wait for them rather than race ``asyncio.run``'s loop shutdown.
    """
    async def _main():
        await coro
        for _ in range(10):
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
    asyncio.run(_main())


# ---------------------------------------------------------------------------
# EC-1 / FR-009: failure branch emits auth_required, not a dead-end Alert
# ---------------------------------------------------------------------------

def test_garbage_token_emits_single_auth_required_invalid(auth_audit, monkeypatch):
    """A register_ui carrying a garbage (non-JWT) token yields EXACTLY one
    sent payload: type=auth_required with reason 'invalid' — and no ui_render
    error Alert, no session, no dashboard. Waiting tasks are ungated."""
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
    # The pre-028 dead-end Alert is gone: nothing rendered at all.
    assert fake._renders == []
    assert "ui_render" not in _types(fake)
    # No session established, no dashboard, no profile persisted.
    assert fake.ui_sessions == {}
    assert fake._dashboards == []
    assert fake._profiles == []
    # Queued non-register messages are ungated so they hit auth naturally.
    assert evt.is_set()


def test_missing_token_emits_auth_required_invalid(auth_audit, monkeypatch):
    """A register_ui with NO token at all is the same recoverable signal."""
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
    """A well-formed JWT whose exp is in the past is classified 'expired' so
    the client can silently refresh instead of forcing a full re-login."""
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
    """A well-formed JWT with a FUTURE exp that the validator still rejects
    (bad signature, wrong issuer, …) reports 'invalid', not 'expired'."""
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    fake = _make_fake(validate=_reject_token)
    token = _fake_jwt({"sub": "someone", "exp": time.time() + 3600})

    asyncio.run(fake.handle_ui_message(_FakeWS(), _register_msg(token=token)))

    assert len(fake._sent) == 1
    payload = fake._sent[0][1]
    assert payload["type"] == "auth_required"
    assert payload["reason"] == "invalid"


# ---------------------------------------------------------------------------
# Mock-auth mode still registers normally (no auth_required)
# ---------------------------------------------------------------------------

def test_mock_auth_mode_registers_normally(auth_audit, monkeypatch):
    """With USE_MOCK_AUTH on, the real validate_token accepts dev-token and
    the success branch runs: session established, rote_config sent, dashboard
    pushed — and NO auth_required anywhere."""
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    fake = _make_fake()  # binds the REAL Orchestrator.validate_token
    ws = _FakeWS("mock")
    evt = asyncio.Event()
    fake._registered_events[id(ws)] = evt

    _run_and_drain(fake.handle_ui_message(ws, _register_msg(token="dev-token")))

    assert "auth_required" not in _types(fake)
    # Session established for the mock user, raw token retained.
    assert ws in fake.ui_sessions
    claims = fake.ui_sessions[ws]
    assert claims["sub"] == "test_user"
    assert claims["_raw_token"] == "dev-token"
    # Success-path side effects: profile saved, ROTE config sent, dashboard.
    assert fake._profiles and fake._profiles[0]["sub"] == "test_user"
    assert "rote_config" in _types(fake)
    assert fake._dashboards == [ws]
    assert evt.is_set()
    # Auth audit recorded the registration (ws_register + login_interactive).
    actions = [e.get("action") for e in auth_audit]
    assert "ws_register" in actions
    assert "login_interactive" in actions


# ---------------------------------------------------------------------------
# Protocol: AuthRequired dataclass wire shape
# ---------------------------------------------------------------------------

def test_auth_required_dataclass_round_trips():
    """shared.protocol.AuthRequired serializes with type 'auth_required' and
    Message.from_json parses it back to an AuthRequired instance."""
    wire = AuthRequired(reason="expired").to_json()
    data = json.loads(wire)
    assert data == {"type": "auth_required", "reason": "expired"}

    parsed = Message.from_json(wire)
    assert isinstance(parsed, AuthRequired)
    assert parsed.type == "auth_required"
    assert parsed.reason == "expired"

    # Default construction matches what the orchestrator sends for bad tokens.
    assert json.loads(AuthRequired().to_json()) == {
        "type": "auth_required", "reason": "invalid"}
