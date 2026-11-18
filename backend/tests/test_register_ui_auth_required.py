"""Tests that orchestrator.py's register_ui answers an invalid, missing, expired or
rejected token with a recoverable auth_required signal (shared/protocol.py) instead
of a dead-end chat alert; a signed token still registers normally.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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
