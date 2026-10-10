"""Dispatch-path coverage for the emergency stop: chat and scheduled turn refusal,
tool-effect gating across owner resolution paths, delegated-token mint refusal,
user-agent tunnel registration refusal, and the background dispatcher's claim-time
race gate.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.emergency_stop import (  # noqa: E402
    EmergencyStopCoordinator,
)
from orchestrator.work_admission import (  # noqa: E402
    _TERMINAL_STATES as _TERMINAL_OPERATION_STATES,
    OperationState,
)
from persistent_agents.models import AssignmentError  # noqa: E402
from shared.protocol import MCPResponse  # noqa: E402


OWNER = "dispatch-owner-1"


def _stopped_coordinator() -> EmergencyStopCoordinator:
    coordinator = EmergencyStopCoordinator(
        persist=lambda owner, action, detail: None,
        load_durable=lambda owner: {"engaged": False, "revision": 0})
    asyncio.run(coordinator.engage(OWNER))
    return coordinator


def _orchestrator(coordinator) -> object:
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.emergency_stop = coordinator
    orch.ui_sessions = {}
    return orch


def test_chat_dispatch_refuses_new_turns_while_stopped():
    orch = _orchestrator(_stopped_coordinator())
    with pytest.raises(AssignmentError) as stopped:
        asyncio.run(orch.handle_chat_message(object(), "hello", "chat-1", user_id=OWNER))
    assert stopped.value.code == "emergency_stop_active"
    assert stopped.value.status_code == 423


def test_chat_dispatch_resolves_owner_from_the_socket_session():
    coordinator = _stopped_coordinator()
    orch = _orchestrator(coordinator)
    socket = object()
    orch.ui_sessions = {socket: {"sub": OWNER}}
    with pytest.raises(AssignmentError) as stopped:
        asyncio.run(orch.handle_chat_message(socket, "hello", "chat-1"))
    assert stopped.value.code == "emergency_stop_active"


def test_chat_dispatch_proceeds_when_running_or_unowned():
    coordinator = EmergencyStopCoordinator(
        persist=lambda owner, action, detail: None,
        load_durable=lambda owner: {"engaged": False, "revision": 0})
    orch = _orchestrator(coordinator)
    with pytest.raises(Exception) as relaxed:
        asyncio.run(orch.handle_chat_message(object(), "hello", "chat-1", user_id=OWNER))
    assert getattr(relaxed.value, "code", "") != "emergency_stop_active"
    with pytest.raises(Exception) as legacy:
        asyncio.run(orch.handle_chat_message(object(), "hello", "chat-1", user_id="legacy"))
    assert getattr(legacy.value, "code", "") != "emergency_stop_active"


def test_scheduled_turns_refuse_to_run_while_stopped():
    orch = _orchestrator(_stopped_coordinator())
    with pytest.raises(AssignmentError) as stopped:
        asyncio.run(orch.run_scheduled_turn(
            user_id=OWNER, chat_id="chat-1", instruction="run", agent_id=None,
            access_token="token", allowed_scopes=[], correlation_id="corr-1"))
    assert stopped.value.code == "emergency_stop_active"


def test_scheduled_turns_proceed_for_other_owners():
    orch = _orchestrator(_stopped_coordinator())
    with pytest.raises(Exception) as relaxed:
        asyncio.run(orch.run_scheduled_turn(
            user_id="dispatch-owner-2", chat_id="chat-1", instruction="run",
            agent_id=None, access_token="token", allowed_scopes=[],
            correlation_id="corr-1"))
    assert getattr(relaxed.value, "code", "") != "emergency_stop_active"


def test_tool_dispatch_refuses_effects_while_stopped():
    orch = _orchestrator(_stopped_coordinator())
    response = asyncio.run(orch._dispatch_tool_call(
        "agent-1", "some_tool", {}, 1.0, None, protected_owner_id=OWNER))
    assert isinstance(response, MCPResponse)
    assert response.error == {"message": "emergency_stop_active", "retryable": False}
    assert response.request_id.startswith("req_some_tool_")


def test_tool_dispatch_resolves_the_owner_from_the_socket_session():
    coordinator = _stopped_coordinator()
    orch = _orchestrator(coordinator)
    socket = object()
    orch.ui_sessions = {socket: {"sub": OWNER}}
    response = asyncio.run(orch._dispatch_tool_call(
        "agent-1", "some_tool", {}, 1.0, socket, protected_owner_id=None))
    assert response.error == {"message": "emergency_stop_active", "retryable": False}


def test_tool_dispatch_ignores_unowned_and_legacy_dispatches():
    orch = _orchestrator(_stopped_coordinator())
    with pytest.raises(Exception) as unowned:
        asyncio.run(orch._dispatch_tool_call("agent-1", "some_tool", {}, 1.0, None))
    assert getattr(unowned.value, "code", "") != "emergency_stop_active"
    socket = object()
    orch.ui_sessions = {socket: {}}
    with pytest.raises(Exception) as empty_session:
        asyncio.run(orch._dispatch_tool_call("agent-1", "some_tool", {}, 1.0, socket))
    assert getattr(empty_session.value, "code", "") != "emergency_stop_active"


def test_tool_dispatch_allows_other_owners_while_one_is_stopped():
    orch = _orchestrator(_stopped_coordinator())
    with pytest.raises(Exception) as relaxed:
        asyncio.run(orch._dispatch_tool_call(
            "agent-1", "some_tool", {}, 1.0, None,
            protected_owner_id="dispatch-owner-2"))
    assert getattr(relaxed.value, "code", "") != "emergency_stop_active"


def test_user_agent_tunnel_registration_refuses_while_stopped():
    coordinator = _stopped_coordinator()
    orch = _orchestrator(coordinator)
    closed: list[tuple[int, str]] = []
    audits: list[tuple] = []

    async def audit(owner_sub, action, description, agent_id, outcome=None):
        audits.append((owner_sub, action, outcome))

    orch._audit_user_agent = audit
    socket = SimpleNamespace(
        is_user_agent_tunnel=True, owner_sub=OWNER,
        close=lambda code, reason: closed.append((code, reason)) or asyncio.sleep(0))
    card = SimpleNamespace(agent_id="ua-external")
    asyncio.run(orch.register_agent(socket, SimpleNamespace(agent_card=card)))
    assert closed and closed[0][0] == 1008
    assert audits == [(OWNER, "agent.registration_refused", "failure")]


def test_user_agent_tunnel_registration_proceeds_when_running():
    coordinator = EmergencyStopCoordinator(
        persist=lambda owner, action, detail: None,
        load_durable=lambda owner: {"engaged": False, "revision": 0})
    orch = _orchestrator(coordinator)
    closed: list = []
    socket = SimpleNamespace(
        is_user_agent_tunnel=True, owner_sub=OWNER,
        close=lambda code, reason: closed.append((code, reason)) or asyncio.sleep(0))
    card = SimpleNamespace(agent_id="ua-external")
    with pytest.raises(Exception) as relaxed:
        asyncio.run(orch.register_agent(socket, SimpleNamespace(agent_card=card)))
    assert not closed
    assert getattr(relaxed.value, "code", "") != "emergency_stop_active"


def _delegation_orchestrator(coordinator):
    orch = _orchestrator(coordinator)
    socket = object()
    orch.ui_sessions = {socket: {"_raw_token": "raw-token"}}
    orch.agent_cards = {"agent-1": SimpleNamespace(skills=[SimpleNamespace(id="t1")])}
    orch.security_flags = {}
    exchanges: list[tuple] = []

    async def exchange(user_token, agent_id, allowed_tools, user_id, enabled_scopes):
        exchanges.append((agent_id, user_id, tuple(enabled_scopes)))
        return {"access_token": "delegated-token"}

    orch.delegation = SimpleNamespace(exchange_token_for_agent=exchange)
    orch.tool_permissions = SimpleNamespace(
        is_tool_allowed=lambda user, agent, tool: True,
        get_enabled_scope_names=lambda user, agent: ["tools:execute"],
    )
    return orch, socket, exchanges


def test_delegated_token_minting_refuses_while_stopped():
    orch, socket, exchanges = _delegation_orchestrator(_stopped_coordinator())
    token = asyncio.run(orch._get_delegation_token(socket, "agent-1", OWNER))
    assert token is None
    assert exchanges == []


def test_delegated_token_minting_proceeds_when_running():
    coordinator = EmergencyStopCoordinator(
        persist=lambda owner, action, detail: None,
        load_durable=lambda owner: {"engaged": False, "revision": 0})
    orch, socket, exchanges = _delegation_orchestrator(coordinator)
    token = asyncio.run(orch._get_delegation_token(socket, "agent-1", OWNER))
    assert token == "delegated-token"
    assert exchanges == [("agent-1", OWNER, ("tools:execute",))]


def _background_manager(*, active_limit=5):
    from orchestrator.async_tasks import BackgroundTaskManager
    from orchestrator.work_admission import (
        AdmissionClass,
        AdmissionClassConfig,
        InMemoryWorkAdmissionRepository,
        WorkAdmissionCoordinator,
    )

    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=5,
                max_wait_ms=30_000,
                config_revision="stop-test",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
        slot_lease=timedelta(seconds=30),
    )
    return BackgroundTaskManager(coordinator=coordinator), coordinator


def test_background_claim_time_gate_cancels_admitted_work():
    manager, coordinator = _background_manager()
    state = {"stopped": False, "entered": asyncio.Event()}
    release = asyncio.Event()

    async def guard(user_id: str) -> str | None:
        state["entered"].set()
        await release.wait()
        return "emergency_stop_active" if state["stopped"] else None

    manager.set_admission_guard(guard)
    executed: list[str] = []

    async def work(vws, **kwargs):
        executed.append("ran")

    async def engager():
        await state["entered"].wait()
        state["stopped"] = True
        release.set()

    async def scenario():
        engager_task = asyncio.create_task(engager())
        task = await asyncio.wait_for(
            manager.submit("chat-1", OWNER, work), timeout=5.0)
        await asyncio.wait_for(engager_task, timeout=5.0)
        for _ in range(100):
            projection = coordinator.query_operation(
                owner=task._owner,
                operation_id=uuid.UUID(task.task_id),
            )
            if projection.state in _TERMINAL_OPERATION_STATES:
                break
            await asyncio.sleep(0.01)
        return task, projection

    task, projection = asyncio.run(scenario())
    assert executed == []
    assert projection.state is OperationState.CANCELLED
    assert projection.terminal_code == "emergency_stop_active"
    assert task.asyncio_task is None


def test_background_dispatcher_claims_refuse_after_the_stop_engages():
    manager, coordinator = _background_manager(active_limit=1)
    state = {"stopped": False}

    async def guard(user_id: str) -> str | None:
        return "emergency_stop_active" if state["stopped"] else None

    manager.set_admission_guard(guard)
    executed: list[str] = []

    def work_factory(index):
        async def work(vws, **kwargs):
            executed.append(f"ran-{index}")
            await asyncio.sleep(0.05)
        return work

    async def scenario():
        first = await manager.submit("chat-1", OWNER, work_factory(1))
        for _ in range(100):
            if executed:
                break
            await asyncio.sleep(0.01)
        second = await manager.submit("chat-2", OWNER, work_factory(2))
        state["stopped"] = True
        for _ in range(300):
            projection = coordinator.query_operation(
                owner=second._owner,
                operation_id=uuid.UUID(second.task_id),
            )
            if projection.state in _TERMINAL_OPERATION_STATES:
                break
            await asyncio.sleep(0.01)
        return first, second, projection

    first, second, projection = asyncio.run(scenario())
    assert executed == ["ran-1"]
    assert projection.state is OperationState.CANCELLED
    assert projection.terminal_code == "emergency_stop_active"
    assert second.asyncio_task is None


def test_background_claim_time_gate_allows_work_when_running():
    from orchestrator.async_tasks import BackgroundTaskManager
    from orchestrator.work_admission import (
        AdmissionClass,
        AdmissionClassConfig,
        InMemoryWorkAdmissionRepository,
        WorkAdmissionCoordinator,
    )

    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=None,
                active_limit=5,
                queue_limit=5,
                max_wait_ms=30_000,
                config_revision="stop-test",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
        slot_lease=timedelta(seconds=30),
    )
    manager = BackgroundTaskManager(coordinator=coordinator)
    manager.set_admission_guard(lambda user_id: async_none())
    executed: list[str] = []

    async def work(vws, **kwargs):
        executed.append("ran")

    async def scenario():
        task = await manager.submit("chat-1", OWNER, work)
        for _ in range(200):
            if executed:
                break
            await asyncio.sleep(0.01)
        return task

    asyncio.run(scenario())
    assert executed == ["ran"]


async def async_none():
    return None


def test_background_admission_guard_failure_fails_closed():
    from orchestrator.async_tasks import BackgroundTaskManager

    manager = BackgroundTaskManager(coordinator=object())
    manager.set_admission_guard(None)
    task = SimpleNamespace(task_id="t-1", user_id=OWNER)

    async def check():
        assert await manager._admission_refusal(task) is None

    asyncio.run(check())

    def broken(user_id):
        raise RuntimeError("guard crashed")

    manager.set_admission_guard(broken)
    executed: list[str] = []

    async def check_refusal():
        refusal = await manager._admission_refusal(task)
        executed.append(refusal)

    asyncio.run(check_refusal())
    assert executed == ["emergency_stop_unavailable"]


def test_user_agent_tunnel_registration_handles_socket_close_failure():
    coordinator = _stopped_coordinator()
    orch = _orchestrator(coordinator)
    audits: list[tuple] = []

    async def audit(owner_sub, action, description, agent_id, outcome=None):
        audits.append((owner_sub, action, outcome))

    async def broken_close(code, reason):
        raise RuntimeError("socket already gone")

    orch._audit_user_agent = audit
    socket = SimpleNamespace(
        is_user_agent_tunnel=True, owner_sub=OWNER, close=broken_close)
    card = SimpleNamespace(agent_id="ua-external")
    asyncio.run(orch.register_agent(socket, SimpleNamespace(agent_card=card)))
    assert audits == [(OWNER, "agent.registration_refused", "failure")]


def test_background_dispatcher_refusal_survives_a_stale_fence():
    from orchestrator.work_admission import (
        StaleExecutionFenceError,
    )

    manager, coordinator = _background_manager(active_limit=1)
    state = {"stopped": False}

    async def guard(user_id: str) -> str | None:
        state.setdefault("guard", []).append((user_id, state["stopped"]))
        return "emergency_stop_active" if state["stopped"] else None

    manager.set_admission_guard(guard)
    executed: list[str] = []

    def work_factory(index):
        async def work(vws, **kwargs):
            executed.append(f"ran-{index}")
            await asyncio.sleep(0.05)
        return work

    async def scenario():
        real_terminalize = coordinator.terminalize

        def pass_through_then_stale(fence, **kwargs):
            state["calls"] = state.get("calls", 0) + 1
            if state["calls"] == 1:
                return real_terminalize(fence, **kwargs)
            state["stale"] = True
            raise StaleExecutionFenceError("fence expired")

        first = await manager.submit("chat-1", OWNER, work_factory(1))
        second = await manager.submit("chat-2", OWNER, work_factory(2))
        for _ in range(300):
            if executed:
                break
            await asyncio.sleep(0.01)
        state["stopped"] = True
        coordinator.terminalize = pass_through_then_stale
        for _ in range(300):
            if state.get("stale"):
                break
            await asyncio.sleep(0.01)
        return first, second

    asyncio.run(scenario())
    assert executed == ["ran-1"]
    assert state.get("stale") is True


def test_background_submit_refusal_survives_a_stale_fence():
    from orchestrator.work_admission import StaleExecutionFenceError

    manager, coordinator = _background_manager()
    state = {"stopped": False}

    async def guard(user_id: str) -> str | None:
        return "emergency_stop_active" if state["stopped"] else None

    manager.set_admission_guard(guard)
    executed: list[str] = []

    async def work(vws, **kwargs):
        executed.append("ran")

    def stale_terminalize(fence, **kwargs):
        state["stale"] = True
        raise StaleExecutionFenceError("fence expired")

    async def scenario():
        state["stopped"] = True
        coordinator.terminalize = stale_terminalize
        return await asyncio.wait_for(
            manager.submit("chat-1", OWNER, work), timeout=5.0)

    task = asyncio.run(scenario())
    assert executed == []
    assert state.get("stale") is True
    assert task.asyncio_task is None
