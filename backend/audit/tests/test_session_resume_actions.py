"""Tests for the auth.session_resumed / login_interactive / session_resume_failed action
types: schema round-trip, orchestrator branching on msg.resumed, and the REST
fallback endpoint's anonymous vs bearer attribution.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from audit.schemas import AuditEventCreate


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.parametrize(
    "action_type",
    [
        "auth.login_interactive",
        "auth.session_resumed",
        "auth.session_resume_failed",
    ],
)
def test_new_auth_action_types_accepted_under_existing_event_class(action_type):
    ev = AuditEventCreate(
        actor_user_id="u1",
        auth_principal="u1",
        event_class="auth",
        action_type=action_type,
        description="smoke",
        correlation_id="00000000-0000-0000-0000-000000000001",
        outcome="success" if action_type != "auth.session_resume_failed" else "failure",
        started_at=_now(),
    )
    assert ev.event_class == "auth"
    assert ev.action_type == action_type


class _CapturingRecorder:
    def __init__(self):
        self.records = []

    async def record(self, event):
        self.records.append(event)


@pytest.mark.asyncio
async def test_resumed_true_records_session_resumed(monkeypatch):
    cap = _CapturingRecorder()
    monkeypatch.setattr("audit.hooks.get_recorder", lambda: cap)

    from audit.hooks import record_auth_event

    await record_auth_event(
        claims={"sub": "alice", "preferred_username": "alice", "_pl_resumed": True},
        action="session_resumed",
        description="Silent session resumed from stored credential",
    )
    assert len(cap.records) == 1
    ev = cap.records[0]
    assert ev.event_class == "auth"
    assert ev.action_type == "auth.session_resumed"
    assert ev.outcome == "success"
    assert ev.actor_user_id == "alice"


@pytest.mark.asyncio
async def test_resumed_false_records_login_interactive(monkeypatch):
    cap = _CapturingRecorder()
    monkeypatch.setattr("audit.hooks.get_recorder", lambda: cap)

    from audit.hooks import record_auth_event

    await record_auth_event(
        claims={"sub": "bob", "preferred_username": "bob"},
        action="login_interactive",
        description="Interactive login completed",
    )
    assert len(cap.records) == 1
    ev = cap.records[0]
    assert ev.event_class == "auth"
    assert ev.action_type == "auth.login_interactive"
    assert ev.outcome == "success"


@pytest.mark.asyncio
async def test_resumed_true_invalid_jwt_records_resume_failed(monkeypatch):
    cap = _CapturingRecorder()
    monkeypatch.setattr("audit.hooks.get_recorder", lambda: cap)

    from audit.hooks import record_auth_event

    await record_auth_event(
        claims={"sub": "carol"},
        action="session_resume_failed",
        description="Silent session resume rejected (invalid/expired token)",
        outcome="failure",
        outcome_detail="ws_register token rejected",
    )
    assert len(cap.records) == 1
    ev = cap.records[0]
    assert ev.event_class == "auth"
    assert ev.action_type == "auth.session_resume_failed"
    assert ev.outcome == "failure"
    assert ev.outcome_detail == "ws_register token rejected"


def test_resumed_omitted_treated_as_false_for_backward_compat():
    from shared.protocol import RegisterUI

    legacy_payload = json.dumps({
        "type": "register_ui",
        "token": "tok",
        "capabilities": ["render"],
    })
    msg = RegisterUI.from_json(legacy_payload)
    assert msg.resumed is False


@pytest.mark.asyncio
async def test_session_resume_failed_rest_endpoint_records_anonymous_when_unauthenticated():
    from audit.api import post_session_resume_failed, SessionResumeFailedBody

    cap = _CapturingRecorder()

    import audit.api as api_mod
    original = api_mod.get_recorder
    api_mod.get_recorder = lambda: cap

    try:
        class _StubHeaders(dict):
            def get(self, k, default=None):
                return super().get(k.lower(), default)

        class _StubRequest:
            headers = _StubHeaders()

        body = SessionResumeFailedBody(
            reason="retry-budget-exhausted",
            attempts=3,
            last_error="Network request failed after 3 attempts",
        )
        await post_session_resume_failed(_StubRequest(), body)
    finally:
        api_mod.get_recorder = original

    assert len(cap.records) == 1
    ev = cap.records[0]
    assert ev.actor_user_id == "anonymous"
    assert ev.auth_principal == "anonymous"
    assert ev.event_class == "auth"
    assert ev.action_type == "auth.session_resume_failed"
    assert ev.outcome == "failure"
    assert ev.inputs_meta.get("reason") == "retry-budget-exhausted"
    assert ev.inputs_meta.get("attempts") == 3
    assert ev.inputs_meta.get("resumed") is True


@pytest.mark.asyncio
async def test_session_resume_failed_rest_endpoint_attributes_when_bearer_present():
    import base64
    from audit.api import post_session_resume_failed, SessionResumeFailedBody

    cap = _CapturingRecorder()
    import audit.api as api_mod
    original = api_mod.get_recorder
    api_mod.get_recorder = lambda: cap

    try:
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            b'{"sub":"dave","preferred_username":"dave-user"}'
        ).rstrip(b"=").decode()
        signature = "x"
        jwt = f"{header}.{payload}.{signature}"

        class _StubHeaders:
            def __init__(self, h):
                self._h = h

            def get(self, k, default=None):
                return self._h.get(k.lower(), default)

        class _StubRequest:
            def __init__(self, h):
                self.headers = _StubHeaders(h)

        body = SessionResumeFailedBody(
            reason="token-expired", attempts=0, last_error="hard-max"
        )
        await post_session_resume_failed(
            _StubRequest({"authorization": f"Bearer {jwt}"}), body
        )
    finally:
        api_mod.get_recorder = original

    assert len(cap.records) == 1
    ev = cap.records[0]
    assert ev.actor_user_id == "dave"
    assert ev.auth_principal == "dave-user"
