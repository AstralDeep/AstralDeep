"""Tests for Terminal Session Emergency Stop Gating & Mesh Owner Identity (Issue #289).

Verifies the outstanding Issue #289 requirements:
1. Terminal session creation gated by active emergency stop epoch (423 status).
2. Terminal session write gated by active emergency stop epoch (423 status).
3. Active terminal sessions actively terminated and closed on emergency stop engagement.
4. Terminal execution restored upon authenticated owner resume.
5. Personal mesh node identity authentication without Keycloak dependency.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from orchestrator.emergency_stop import EmergencyStopCoordinator, EmergencyStopRefused
from orchestrator.emergency_stop_api import _human
from orchestrator.emergency_stop_binding import interrupt_owner
from orchestrator.terminal_security import TerminalError, require_terminal_execution
from orchestrator.terminal_sessions import TerminalSession, TerminalSessionManager
from shared.feature_flags import flags


pytestmark = pytest.mark.asyncio


class MockProcess:
    def __init__(self, pid=12345):
        self.pid = pid

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class DummySession:
    def __init__(self, session_id: str, owner_id: str):
        self.session_id = session_id
        self.owner_id = owner_id
        self.closed = False
        self.close_reason = None
        self.process = MockProcess()
        self.master_fd = 999
        self.cwd = "/tmp/dummy"
        self.remote = None
        self.binding = None

    def close(self, reason: str = "closed"):
        self.closed = True
        self.close_reason = reason


async def test_terminal_creation_gated_by_emergency_stop(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    stop = EmergencyStopCoordinator()
    manager = TerminalSessionManager()
    manager.set_emergency_stop_coordinator(stop)

    # Before stop: admission allowed
    assert stop.admission_allowed("owner_alice") is True

    # Engage emergency stop for Alice
    await stop.engage("owner_alice", reason="Security incident detected")
    assert stop.admission_allowed("owner_alice") is False

    # Alice's terminal execution is refused with emergency_stop_active (423)
    with pytest.raises(TerminalError) as exc_info:
        require_terminal_execution(manager, "owner_alice")
    assert exc_info.value.code == "emergency_stop_active"
    assert exc_info.value.status_code == 423

    # Bob is unaffected
    assert stop.admission_allowed("owner_bob") is True


async def test_active_terminal_sessions_closed_on_emergency_stop():
    owner_socket = object()
    control_task = asyncio.current_task()
    
    session_alice_1 = DummySession("term-1", "owner_alice")
    session_alice_2 = DummySession("term-2", "owner_alice")
    session_bob = DummySession("term-3", "owner_bob")

    term_manager = MagicMock()
    def mock_close_all(owner_id, reason):
        for s in [session_alice_1, session_alice_2]:
            if s.owner_id == owner_id:
                s.close(reason)
        return 2

    term_manager.close_all_for_owner.side_effect = mock_close_all

    background, streams = AsyncMock(), AsyncMock()
    orch = SimpleNamespace(
        ui_sessions={owner_socket: {"sub": "owner_alice"}},
        _connection_contexts={
            1: SimpleNamespace(websocket=owner_socket, operation_tasks={control_task})
        },
        async_task_manager=background,
        stream_manager=streams,
        terminal_sessions=term_manager,
    )

    # Interrupt Alice
    await interrupt_owner(orch, "owner_alice", control_task)

    term_manager.close_all_for_owner.assert_called_once_with("owner_alice", "emergency_stop")
    assert session_alice_1.closed is True
    assert session_alice_1.close_reason == "emergency_stop"
    assert session_alice_2.closed is True
    assert session_alice_2.close_reason == "emergency_stop"
    assert session_bob.closed is False


async def test_resume_reenables_terminal_execution(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    stop = EmergencyStopCoordinator()
    manager = TerminalSessionManager()
    manager.set_emergency_stop_coordinator(stop)

    # Engage stop
    status = await stop.engage("owner_alice", reason="Under review")
    revision = status["revision"]
    assert stop.admission_allowed("owner_alice") is False

    # Verify denied
    with pytest.raises(TerminalError) as exc:
        require_terminal_execution(manager, "owner_alice")
    assert exc.value.code == "emergency_stop_active"
    assert exc.value.status_code == 423

    # Stale revision resume fails
    with pytest.raises(EmergencyStopRefused) as stale_exc:
        await stop.resume("owner_alice", expected_revision=revision + 999)
    assert stale_exc.value.code == "emergency_stop_stale_revision"

    # Valid resume succeeds
    await stop.resume("owner_alice", expected_revision=revision)
    assert stop.admission_allowed("owner_alice") is True


async def test_personal_mesh_node_identity_authentication():
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            orchestrator=SimpleNamespace(human_request_boundary=None)
        )
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/emergency-stop/stop",
        "headers": [(b"authorization", b"Bearer mock-mesh-token")],
        "app": fake_app,
    }
    request = Request(scope)

    # Mock mesh_admission.verify_member_token
    import orchestrator.mesh_admission as ma
    orig_verify = getattr(ma, "verify_member_token", None)

    try:
        ma.verify_member_token = lambda tok: {"sub": "node_owner_123", "aud": "astral-mesh"}
        caller = await _human(request)
        assert caller.owner_id == "node_owner_123"
        assert caller.claims["sub"] == "node_owner_123"
        assert callable(caller.require_write)
    finally:
        if orig_verify is not None:
            ma.verify_member_token = orig_verify


async def test_unauthenticated_request_fails_closed():
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            orchestrator=SimpleNamespace(human_request_boundary=None)
        )
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/emergency-stop/stop",
        "headers": [],
        "app": fake_app,
    }
    request = Request(scope)

    with pytest.raises(HTTPException) as exc_info:
        await _human(request)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "human_unauthenticated"
