"""Coverage for the emergency-stop framework (MCP) path: scope-gated status/stop/resume
operations, tool projection, dispatch wiring, and the orchestrator-side dispatch gates
for chat, scheduled turns, and tool effects.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from orchestrator.emergency_stop import (  # noqa: E402
    EmergencyStopCoordinator,
)
from orchestrator.framework_credentials import FrameworkCaller  # noqa: E402
from orchestrator.mcp_projection import _WORK_TOOL_SPECS  # noqa: E402
from orchestrator.work_operations import (  # noqa: E402
    DISPATCHABLE_TOOL_NAMES,
    FrameworkWorkOperations,
    dispatch_name,
)
from persistent_agents.models import AssignmentError  # noqa: E402


OWNER = "framework-owner-1"


def _caller(scopes=("operations.read", "operations.control")) -> FrameworkCaller:
    return FrameworkCaller(owner_id=OWNER, credential_id="cred-1",
                           scopes=frozenset(scopes), _token_hash="hash")


def _ops(coordinator, scopes=("operations.read", "operations.control")):
    ops = FrameworkWorkOperations(assignments=MagicMock(), credentials=MagicMock())
    ops.emergency = coordinator
    return ops, _caller(scopes)


def test_emergency_tools_are_projected_and_dispatchable():
    for name in ("astral_emergency_status", "astral_emergency_stop",
                 "astral_emergency_resume"):
        assert name in DISPATCHABLE_TOOL_NAMES
        assert name in _WORK_TOOL_SPECS
        assert dispatch_name(name)
    assert dispatch_name("astral_emergency_status") == "emergency_status"


def test_emergency_operations_require_their_scopes():
    coordinator = EmergencyStopCoordinator()
    ops, read_only = _ops(coordinator, scopes=("operations.read",))
    assert asyncio.run(ops.emergency_status(read_only))["state"] == "running"
    with pytest.raises(AssignmentError) as denied:
        asyncio.run(ops.emergency_stop(read_only))
    assert denied.value.code == "framework_scope_required"
    with pytest.raises(AssignmentError) as denied_resume:
        asyncio.run(ops.emergency_resume(read_only, expected_revision=1))
    assert denied_resume.value.code == "framework_scope_required"


def test_framework_stop_cannot_invent_current_owner_resume_authority():
    coordinator = EmergencyStopCoordinator()
    ops, caller = _ops(coordinator)
    engaged = asyncio.run(ops.emergency_stop(caller, reason="drill"))
    assert engaged["engaged"] is True and engaged["state"] == "stopped"
    assert coordinator.admission_allowed(OWNER) is False
    with pytest.raises(AssignmentError, match="emergency_stop_owner_authentication_required"):
        asyncio.run(ops.emergency_resume(caller, expected_revision=engaged["revision"]))
    assert coordinator.admission_allowed(OWNER) is False


def test_emergency_operations_without_a_coordinator_fail_closed():
    ops, caller = _ops(EmergencyStopCoordinator())
    ops.emergency = None
    with pytest.raises(AssignmentError) as unavailable:
        asyncio.run(ops.emergency_status(caller))
    assert unavailable.value.code == "emergency_stop_unavailable"


def test_emergency_resume_through_the_facade_rejects_bad_revisions():
    coordinator = EmergencyStopCoordinator()
    ops, caller = _ops(coordinator)
    for bad in (None, "one", 0, True):
        with pytest.raises(AssignmentError) as invalid:
            asyncio.run(ops.emergency_resume(caller, expected_revision=bad))
        assert invalid.value.code == "emergency_stop_owner_authentication_required"


def test_emergency_facade_surfaces_coordinator_denials():
    coordinator = EmergencyStopCoordinator()
    ops, caller = _ops(coordinator)
    with pytest.raises(AssignmentError) as not_engaged:
        asyncio.run(ops.emergency_resume(caller, expected_revision=1))
    assert not_engaged.value.code == "emergency_stop_owner_authentication_required"
    engaged = asyncio.run(ops.emergency_stop(caller))
    stale = FrameworkCaller(owner_id=OWNER, credential_id="cred-2",
                            scopes=frozenset({"operations.control"}), _token_hash="h2")
    with pytest.raises(AssignmentError) as denied:
        asyncio.run(ops.emergency_resume(stale, expected_revision=engaged["revision"] + 9))
    assert denied.value.code == "emergency_stop_owner_authentication_required"


def _dispatch(orchestrator, tool_name, arguments, claims=None):
    from orchestrator.mcp_server_endpoint import _dispatch_work_tool

    return asyncio.run(_dispatch_work_tool(
        orchestrator, claims or {"_framework_caller": _caller()}, tool_name, arguments))


def _mcp_error(response) -> str:
    return (response.error or {}).get("message", "")


def test_dispatch_routes_emergency_tools_to_the_coordinator():
    coordinator = EmergencyStopCoordinator()
    ops = FrameworkWorkOperations(assignments=MagicMock(), credentials=MagicMock())
    orchestrator = SimpleNamespace(framework_work_operations=ops,
                                   emergency_stop=coordinator)
    engaged = _dispatch(orchestrator, "astral_emergency_stop", {"reason": "drill"})
    assert engaged.result["state"] == "stopped"
    status = _dispatch(orchestrator, "astral_emergency_status", {})
    assert status.result["engaged"] is True
    resumed = _dispatch(orchestrator, "astral_emergency_resume",
                        {"expected_revision": engaged.result["revision"]})
    assert _mcp_error(resumed) == "emergency_stop_owner_authentication_required"
    assert not coordinator.admission_allowed(OWNER)


def test_dispatch_reports_coordinator_denials_as_tool_errors():
    coordinator = EmergencyStopCoordinator()
    ops = FrameworkWorkOperations(assignments=MagicMock(), credentials=MagicMock())
    orchestrator = SimpleNamespace(framework_work_operations=ops,
                                   emergency_stop=coordinator)
    stale = _dispatch(orchestrator, "astral_emergency_resume", {"expected_revision": 7})
    assert _mcp_error(stale) == "emergency_stop_owner_authentication_required"
    broken = SimpleNamespace(framework_work_operations=ops, emergency_stop=None)
    assert _mcp_error(_dispatch(broken, "astral_emergency_stop", {})) == "emergency_stop_unavailable"


def test_dispatch_rejects_malformed_emergency_arguments():
    coordinator = EmergencyStopCoordinator()
    ops = FrameworkWorkOperations(assignments=MagicMock(), credentials=MagicMock())
    orchestrator = SimpleNamespace(framework_work_operations=ops,
                                   emergency_stop=coordinator)
    assert _mcp_error(_dispatch(orchestrator, "astral_emergency_resume", {})) == "emergency_stop_owner_authentication_required"
    non_dict = _dispatch(orchestrator, "astral_emergency_stop", ["not", "a", "dict"])
    assert non_dict.result["engaged"] is True


def _orchestrator_with_stop(coordinator) -> object:
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.emergency_stop = coordinator
    orch.ui_sessions = {}
    return orch


def test_chat_dispatch_refuses_new_turns_while_stopped():
    coordinator = EmergencyStopCoordinator()
    asyncio.run(coordinator.engage(OWNER, sweep=False))
    orch = _orchestrator_with_stop(coordinator)
    with pytest.raises(AssignmentError) as stopped:
        asyncio.run(orch.handle_chat_message(object(), "hello", "chat-1", user_id=OWNER))
    assert stopped.value.code == "emergency_stop_active"
    allowed = EmergencyStopCoordinator()
    orch = _orchestrator_with_stop(allowed)
    with pytest.raises(Exception) as relaxed:
        asyncio.run(orch.handle_chat_message(object(), "hello", "chat-1", user_id=OWNER))
    assert "emergency_stop_active" not in str(getattr(relaxed.value, "code", ""))


def test_scheduled_turns_refuse_to_run_while_stopped():
    coordinator = EmergencyStopCoordinator()
    asyncio.run(coordinator.engage(OWNER, sweep=False))
    orch = _orchestrator_with_stop(coordinator)
    with pytest.raises(AssignmentError) as stopped:
        asyncio.run(orch.run_scheduled_turn(user_id=OWNER, chat_id="chat-1",
                                            instruction="run", agent_id=None,
                                            access_token="token", allowed_scopes=[],
                                            correlation_id="corr"))
    assert stopped.value.code == "emergency_stop_active"


def test_tool_dispatch_refuses_effects_while_stopped():
    from shared.protocol import MCPResponse

    coordinator = EmergencyStopCoordinator()
    asyncio.run(coordinator.engage(OWNER, sweep=False))
    orch = _orchestrator_with_stop(coordinator)
    response = asyncio.run(orch._dispatch_tool_call(
        "agent-1", "some_tool", {}, 1.0, None, protected_owner_id=OWNER))
    assert isinstance(response, MCPResponse)
    assert _mcp_error(response) == "emergency_stop_active"
    assert not getattr(response, "error", {}).get("retryable", True)


def test_tool_dispatch_resolves_the_owner_from_the_socket_session():
    coordinator = EmergencyStopCoordinator()
    asyncio.run(coordinator.engage(OWNER, sweep=False))
    orch = _orchestrator_with_stop(coordinator)
    socket = object()
    orch.ui_sessions = {socket: {"sub": OWNER}}
    response = asyncio.run(orch._dispatch_tool_call(
        "agent-1", "some_tool", {}, 1.0, socket, protected_owner_id=None))
    assert _mcp_error(response) == "emergency_stop_active"


def test_tool_dispatch_ignores_legacy_and_unowned_sockets():
    coordinator = EmergencyStopCoordinator()
    asyncio.run(coordinator.engage(OWNER, sweep=False))
    orch = _orchestrator_with_stop(coordinator)
    with pytest.raises(Exception) as relaxed:
        asyncio.run(orch._dispatch_tool_call("agent-1", "some_tool", {}, 1.0, None,
                                             protected_owner_id=None))
    assert "emergency_stop_active" not in str(getattr(relaxed.value, "code", ""))
