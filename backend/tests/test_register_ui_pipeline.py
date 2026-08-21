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

from orchestrator.plane_repository_context import (  # noqa: E402
    PlaneRepositoryContext,
    plane_source_from_orchestrator,
)


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


async def _register_and_observe(orch, identity, payload):
    """Run register_ui and observe its two fire-and-forget writes durably."""
    ws = _fresh_socket()
    orch._registered_events[id(ws)] = asyncio.Event()
    await orch.handle_ui_message(ws, json.dumps(payload))

    source = plane_source_from_orchestrator(orch)
    identity_context = PlaneRepositoryContext(
        repository=source.plane_repositories.identity,
        plane_runtime=source.plane_runtime,
    )
    audit_context = PlaneRepositoryContext(
        repository=source.plane_repositories.audit,
        plane_runtime=source.plane_runtime,
    )
    user_id = identity["user_id"]
    terminal_action = (
        "auth.session_resumed" if payload.get("resumed") else "auth.login_interactive"
    )
    deadline = asyncio.get_running_loop().time() + 10.0
    profile = None
    actions = []
    while asyncio.get_running_loop().time() < deadline:
        profile = await identity_context.call_async(
            identity_context.repository.get_identity,
            owner_id=user_id,
        )
        records = await audit_context.call_async(
            audit_context.repository.list_for_chain,
            chain_id=user_id,
            after_sequence=0,
            limit=50,
        )
        actions = [record.event.action_type for record in records]
        if (
            profile is not None
            and "auth.ws_register" in actions
            and terminal_action in actions
        ):
            return ws, profile, actions
        await asyncio.sleep(0.01)
    raise AssertionError(
        "register_ui background writes did not become durable: "
        f"profile={profile is not None}, actions={actions}"
    )


@pytest.fixture()
async def orch(monkeypatch, isolated_mock_identity):
    """A real Orchestrator under mock auth with an isolated DB owner."""
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.orchestrator import Orchestrator
    try:
        o = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    # Feature 054: an UNCONFIGURED user's register pushes the mandatory
    # provider-setup dialog and SUPPRESSES the welcome canvas. These tests
    # cover the configured-user handshake, so seed only this test's unique
    # mock-auth owner and remove only its deletable LLM record at teardown.
    # Identity observations and their append-only audit evidence deliberately
    # remain: Plane exposes no ordinary identity hard-delete API, and every
    # test uses a unique owner inside the disposable CI database.
    user_id = isolated_mock_identity["user_id"]
    try:
        await o._llm_store.set(
            user_id,
            provider="custom",
            base_url="http://test.invalid/v1",
            model="test-model",
            api_key="test-key",
        )
        yield o
    finally:
        cleanup_errors = []
        try:
            await o._llm_store.clear(user_id)
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            await asyncio.wait_for(o._close_started_services(), timeout=15.0)
        except BaseException as exc:
            cleanup_errors.append(exc)
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise BaseExceptionGroup(
                "register_ui fixture cleanup failed",
                cleanup_errors,
            )


async def test_register_ui_delivers_welcome_and_dashboard(
    orch, isolated_mock_identity
):
    """The handshake still delivers rote_config, system_config and welcome."""
    ws, _profile, _actions = await _register_and_observe(
        orch,
        isolated_mock_identity,
        {
            "type": "register_ui",
            "token": isolated_mock_identity["token"],
            "device": {},
        },
    )

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
    orch, isolated_mock_identity
):
    """ws_register then login_interactive/session_resumed, off-path but complete."""
    _ws, _profile, actions = await _register_and_observe(
        orch,
        isolated_mock_identity,
        {
            "type": "register_ui",
            "token": isolated_mock_identity["token"],
            "device": {},
            "resumed": False,
        },
    )

    assert actions.index("auth.ws_register") < actions.index("auth.login_interactive")


async def test_register_ui_persists_user_profile(orch, isolated_mock_identity):
    """The backgrounded profile save still upserts the JWT user row."""
    _ws, profile, _actions = await _register_and_observe(
        orch,
        isolated_mock_identity,
        {
            "type": "register_ui",
            "token": isolated_mock_identity["token"],
            "device": {},
        },
    )

    assert profile.owner_id == isolated_mock_identity["user_id"]
    assert profile.username == isolated_mock_identity["user_id"]
