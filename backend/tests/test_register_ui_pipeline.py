"""Feature 052 (T027) — register_ui handshake pipeline against the live DB.

Drives a real register_ui through Orchestrator.handle_ui_message on an
in-process VirtualWebSocket (mock-auth dev token): the welcome canvas and
dashboard both arrive, rote_config still precedes the dashboard frame, and
the off-critical-path writes (profile save, the two login audit events in
order) still complete (FR-012 — reads parallelized, writes backgrounded,
audit completeness preserved).
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def isolated_mock_identity():
    """Return a per-test mock-auth subject and JWT-like bearer.

    ``test_user`` is also the interactive development identity when
    ``USE_MOCK_AUTH=true``.  A live-DB test must therefore never seed its
    persisted LLM record: doing so replaces the provider configured through
    the running web client.  The mock validator accepts a decoded JWT payload,
    which lets this suite exercise the real mock-auth branch with an isolated
    owner instead.
    """
    user_id = f"pytest-register-ui-{uuid.uuid4().hex}"
    claims = {
        "sub": user_id,
        "preferred_username": user_id,
        "email": f"{user_id}@invalid.example",
        "realm_access": {"roles": ["admin", "user"]},
        "resource_access": {
            "astral-frontend": {"roles": ["admin", "user"]}
        },
    }
    payload = base64.b64encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return {"user_id": user_id, "token": f"mock.{payload}.signature"}


def _fresh_socket():
    """A VirtualWebSocket capturing every frame the handshake sends."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
    return VirtualWebSocket(task)


async def _drain_background_tasks():
    """Give the handshake's create_task work (profile/audit) time to finish."""
    for _ in range(20):
        await asyncio.sleep(0.02)


@pytest.fixture()
def orch(monkeypatch, isolated_mock_identity):
    """A real Orchestrator under mock auth with an isolated DB owner."""
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.orchestrator import Orchestrator
    try:
        o = Orchestrator()
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    # Feature 054: an UNCONFIGURED user's register pushes the mandatory
    # provider-setup dialog and SUPPRESSES the welcome canvas. These tests
    # cover the configured-user handshake, so seed only this test's unique
    # mock-auth owner and remove that record at teardown.
    user_id = isolated_mock_identity["user_id"]
    o._llm_store.set_sync(user_id, provider="custom",
                          base_url="http://test.invalid/v1",
                          model="test-model", api_key="test-key")
    try:
        yield o
    finally:
        o._llm_store.clear_sync(user_id)
        o.history.db.execute("DELETE FROM users WHERE id = ?", (user_id,))


async def test_register_ui_delivers_welcome_and_dashboard(
    orch, isolated_mock_identity
):
    """The handshake still delivers rote_config, system_config and welcome."""
    ws = _fresh_socket()
    orch._registered_events[id(ws)] = asyncio.Event()
    await orch.handle_ui_message(ws, json.dumps(
        {
            "type": "register_ui",
            "token": isolated_mock_identity["token"],
            "device": {},
        }))
    await _drain_background_tasks()

    frame_types = [f.get("type") for f in ws.task.outputs]
    assert "rote_config" in frame_types
    assert "system_config" in frame_types, "dashboard must still arrive"
    renders = [f for f in ws.task.outputs
               if f.get("type") == "ui_render" and f.get("target") != "chat"]
    assert renders, "welcome canvas ui_render must arrive"
    assert orch._registered_events[id(ws)].is_set()
    assert orch._ws_welcome.get(id(ws)) is True

    # rote_config still precedes the dashboard payload — native clients learn
    # their device profile before any adapted content lands.
    assert frame_types.index("rote_config") < frame_types.index("system_config")


async def test_register_ui_audit_events_recorded_in_order(
    orch, monkeypatch, isolated_mock_identity
):
    """ws_register then login_interactive/session_resumed, off-path but complete."""
    from audit import hooks as audit_hooks
    recorded = []

    async def _capture(*, claims, action, description, **kw):
        recorded.append(action)

    monkeypatch.setattr(audit_hooks, "record_auth_event", _capture)
    ws = _fresh_socket()
    orch._registered_events[id(ws)] = asyncio.Event()
    await orch.handle_ui_message(ws, json.dumps(
        {
            "type": "register_ui",
            "token": isolated_mock_identity["token"],
            "device": {},
            "resumed": False,
        }))
    await _drain_background_tasks()

    assert "ws_register" in recorded
    assert "login_interactive" in recorded
    assert recorded.index("ws_register") < recorded.index("login_interactive")


async def test_register_ui_persists_user_profile(orch, isolated_mock_identity):
    """The backgrounded profile save still upserts the JWT user row."""
    ws = _fresh_socket()
    orch._registered_events[id(ws)] = asyncio.Event()
    await orch.handle_ui_message(ws, json.dumps(
        {
            "type": "register_ui",
            "token": isolated_mock_identity["token"],
            "device": {},
        }))
    await _drain_background_tasks()

    row = await orch.history.db.afetch_one(
        "SELECT id FROM users WHERE id = ?",
        (isolated_mock_identity["user_id"],),
    )
    assert row is not None, "profile save must still complete (audit-complete writes)"
