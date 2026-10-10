"""Exercise owner interruption across active streams and local execution entries.
Real coordinator fences prevent physical invocation and preserve other owners.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.emergency_stop import EmergencyStopCoordinator, EmergencyStopRefused
from orchestrator.emergency_stop_binding import interrupt_owner
from orchestrator.orchestrator import Orchestrator
from orchestrator.stream_manager import StreamManager, StreamState, StreamSubscription
from orchestrator.async_tasks import BackgroundTaskManager
from persistent_agents.runner import AssignmentRunner
from scheduler.runner import JobRunner


pytestmark = pytest.mark.asyncio


async def test_stop_interrupts_only_current_owner_and_keeps_control_task_alive():
    owner_socket, other_socket = object(), object()
    release = asyncio.Event()
    owned = asyncio.create_task(release.wait())
    unrelated = asyncio.create_task(release.wait())
    control = asyncio.current_task()
    background, streams = AsyncMock(), AsyncMock()
    orch = SimpleNamespace(ui_sessions={owner_socket: {"sub": "owner"}, other_socket: {"sub": "other"}},
        _connection_contexts={1: SimpleNamespace(websocket=owner_socket, operation_tasks={owned, control}),
                              2: SimpleNamespace(websocket=other_socket, operation_tasks={unrelated})},
        async_task_manager=background, stream_manager=streams)
    await interrupt_owner(orch, "owner", control)
    with pytest.raises(asyncio.CancelledError):
        await owned
    assert not unrelated.done() and not control.cancelling()
    background.cancel_for_owner.assert_awaited_once_with("owner")
    streams.cancel_for_owner.assert_awaited_once_with("owner")
    release.set()
    await unrelated


async def test_local_acknowledgment_waits_for_cancellation_resistant_effect_unwind():
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stop = EmergencyStopCoordinator()

    async def effect():
        async with stop.effect("owner"):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

    task = asyncio.create_task(effect())
    await entered.wait()
    stopped = asyncio.create_task(stop.engage("owner", sweep=False))
    await cancelled.wait()
    assert not stopped.done() and not task.done()
    assert stop.status("owner")["responders"][0]["state"] == "pending"
    assert not stop.admission_allowed("owner")
    release.set()
    status = await stopped
    assert task.done() and status["state"] == "stopped"
    assert status["responders"][0]["state"] == "acknowledged"
    await task


async def test_unfinished_effect_remains_unreachable_and_fenced(monkeypatch):
    from orchestrator import emergency_stop

    monkeypatch.setattr(emergency_stop, "_INTERRUPTION_TIMEOUT_SECONDS", 0)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stop = EmergencyStopCoordinator()

    async def effect():
        async with stop.effect("owner"):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

    task = asyncio.create_task(effect())
    await entered.wait()
    try:
        with pytest.raises(EmergencyStopRefused, match="unavailable"):
            await stop.engage("owner", sweep=False)
        await cancelled.wait()
        assert not task.done() and not stop.admission_allowed("owner")
        assert stop.status("owner")["state"] == "unreachable"
        assert stop.status("owner")["responders"][0]["state"] == "unreachable"
    finally:
        release.set()
        await task


async def test_connection_cancellation_is_joined_before_owner_interruption_completes():
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def effect():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(effect())
    await entered.wait()
    socket = object()
    orch = SimpleNamespace(ui_sessions={socket: {"sub": "owner"}},
        _connection_contexts={1: SimpleNamespace(websocket=socket, operation_tasks={task})})
    stopped = asyncio.create_task(interrupt_owner(orch, "owner"))
    await cancelled.wait()
    assert not stopped.done()
    release.set()
    await stopped
    assert task.done()


async def test_cancellation_resistant_manager_cannot_make_owner_interruption_hang(monkeypatch):
    from orchestrator import emergency_stop

    monkeypatch.setattr(emergency_stop, "_INTERRUPTION_TIMEOUT_SECONDS", 0)
    entered, cancelled, release, completed = (asyncio.Event() for _ in range(4))

    async def cancel(owner):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        completed.set()

    orch = SimpleNamespace(async_task_manager=SimpleNamespace(cancel_for_owner=cancel))
    try:
        with pytest.raises(EmergencyStopRefused, match="unavailable"):
            await interrupt_owner(orch, "owner")
        await entered.wait()
        await cancelled.wait()
        assert not completed.is_set()
    finally:
        release.set()
        await completed.wait()


async def test_stream_stop_cancels_active_and_dormant_without_retrying_or_touching_other_owner():
    cancel = AsyncMock()
    manager = StreamManager(Mock(), AsyncMock(), Mock(), agent_canceller=cancel)
    terminal = AsyncMock()
    manager.terminal_hook = terminal
    rows = [StreamSubscription(f"s{i}", owner, "chat", "tool", "agent", {}, "digest", f"c{i}",
                request_id=f"request-{i}") for i, owner in enumerate(["owner", "owner", "other"])]
    rows[0]._retry_handle = asyncio.get_running_loop().call_later(30, lambda: None)
    rows[0].task = asyncio.create_task(asyncio.Event().wait())
    owned_task = rows[0].task
    manager._active = {rows[0].key: rows[0], rows[2].key: rows[2]}
    manager._dormant = {("owner", "chat"): {"s1": rows[1]}}
    await manager.cancel_for_owner("owner")
    with pytest.raises(asyncio.CancelledError):
        await owned_task
    assert all(row.state is StreamState.STOPPED and row.state_reason == "emergency_stop" for row in rows[:2])
    assert rows[0]._retry_handle is None and ("owner", "chat") not in manager._dormant
    assert manager._active == {rows[2].key: rows[2]}
    assert cancel.await_count == terminal.await_count == 2


async def test_background_owner_cancel_uses_existing_durable_cancellation_path():
    manager = object.__new__(BackgroundTaskManager)
    manager._lock = asyncio.Lock()
    manager._tasks = {"1": SimpleNamespace(task_id="1", user_id="owner"),
                      "2": SimpleNamespace(task_id="2", user_id="other")}
    manager.cancel = AsyncMock()
    await manager.cancel_for_owner("owner")
    manager.cancel.assert_awaited_once_with("1")


async def test_scheduled_and_persistent_entries_refuse_effects_when_owner_stopped():
    stop = EmergencyStopCoordinator()
    await stop.engage("owner", sweep=False)
    orch = SimpleNamespace(emergency_stop=stop)
    scheduled = object.__new__(JobRunner)
    scheduled.orch = orch
    scheduled._run_occurrence = AsyncMock(return_value="invoked")
    scheduled._run_job = AsyncMock(return_value="invoked")
    attempt = SimpleNamespace(job={"user_id": "owner"}, operation_id="operation")
    refused = await scheduled.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert refused.outcome == "failure" and refused.retryable is False
    with pytest.raises(EmergencyStopRefused):
        await scheduled.run_job(attempt.job)
    scheduled._run_occurrence.assert_not_awaited()
    scheduled._run_job.assert_not_awaited()
    persistent = object.__new__(AssignmentRunner)
    persistent.orch = orch
    persistent._run_claim = AsyncMock()
    with pytest.raises(EmergencyStopRefused):
        await persistent.run_claim(SimpleNamespace(assignment=SimpleNamespace(owner_id="owner")))
    persistent._run_claim.assert_not_awaited()


async def test_final_governed_attempt_rechecks_stop_after_authorization(monkeypatch):
    from orchestrator import hitl_confirmation

    stop = EmergencyStopCoordinator()
    invoke = AsyncMock()

    async def execute(**kwargs):
        await stop.engage("owner", sweep=False)
        return await kwargs["invoke"]({})

    adapter = SimpleNamespace(mode="off", execute=execute)
    websocket = object()
    orch = SimpleNamespace(emergency_stop=stop, _governed_dispatch_adapter=lambda: adapter,
                           ui_sessions={websocket: {"sub": "owner"}},
                           tool_permissions=SimpleNamespace(get_tool_scope=lambda *args: "tools:read"))
    monkeypatch.setattr(hitl_confirmation, "effect_refusal", lambda *args, **kwargs: None)
    result = await Orchestrator._execute_governed_attempt(orch, websocket, "agent", "tool", {},
        user_id="owner", channel="ui", audit_correlation_id=None, actor_user_id="owner",
        auth_principal="owner", conversation_id=None, invoke=invoke)
    assert result.error["message"] == "emergency_stop_active"
    invoke.assert_not_awaited()


async def test_safety_controls_and_open_remain_available_without_llm_configuration():
    from orchestrator.chrome_events import _llm_gate_refusal

    socket = object()
    orch = SimpleNamespace(ui_sessions={socket: {"sub": "owner"}}, llm_configured_for=AsyncMock(return_value=False))
    for action in ("chrome_safety_stop", "chrome_safety_resume", "chrome_safety_verify"):
        assert not await _llm_gate_refusal(orch, socket, action, "owner", payload={"surface": "safety"})
    assert not await _llm_gate_refusal(orch, socket, "chrome_open", "owner", payload={"surface": "safety"})


async def test_admitted_chat_and_runner_tasks_are_registered_until_interrupted(monkeypatch):
    from orchestrator import user_skills, evidence_context

    stop = EmergencyStopCoordinator()
    entered, release = asyncio.Event(), asyncio.Event()

    async def physical(*args, **kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(user_skills, "enabled", lambda: False)
    monkeypatch.setattr(evidence_context, "needs_authority", lambda orch: False)
    orch = SimpleNamespace(emergency_stop=stop, _handle_chat_message_with_guidance=physical)
    task = asyncio.create_task(Orchestrator.handle_chat_message(orch, object(), "hello", "chat", user_id="owner"))
    await entered.wait()
    await stop.engage("owner", sweep=False)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stop._effects == {}
    await stop.resume("owner", expected_revision=1, actor_id="owner")
    runner = object.__new__(AssignmentRunner)
    runner.orch = orch
    runner._run_claim = AsyncMock(return_value="new work")
    assert await runner.run_claim(SimpleNamespace(assignment=SimpleNamespace(owner_id="owner"))) == "new work"


@pytest.mark.parametrize("stop_after_authorization", [False, True])
async def test_stream_physical_open_rechecks_owner_stop_after_authorization(monkeypatch, stop_after_authorization):
    from contextlib import asynccontextmanager
    from audit import hooks

    stop = EmergencyStopCoordinator()
    socket = object()
    remote = SimpleNamespace(send=AsyncMock())
    orch = SimpleNamespace(agents={"agent": remote}, local_agents={}, agent_cards={},
        ui_sessions={socket: {"sub": "owner"}}, emergency_stop=stop,
        _authorize_and_prepare=AsyncMock(return_value=SimpleNamespace(args={})),
        tool_permissions=SimpleNamespace(get_tool_scope=lambda *args: "tools:read"))

    @asynccontextmanager
    async def audit(**kwargs):
        yield SimpleNamespace(correlation_id="synthetic-correlation", set_outputs_meta=lambda *args: None)

    async def execute(**kwargs):
        if stop_after_authorization:
            await stop.engage("owner", sweep=False)
        return await kwargs["invoke"]({})

    orch._governed_dispatch_adapter = lambda: SimpleNamespace(execute=execute)
    monkeypatch.setattr(hooks, "ToolDispatchAudit", audit)
    if stop_after_authorization:
        with pytest.raises(EmergencyStopRefused):
            await Orchestrator._dispatch_stream_request(orch, "agent", "tool", {}, "stream", "owner", socket, "chat")
        remote.send.assert_not_awaited()
    else:
        request = await Orchestrator._dispatch_stream_request(orch, "agent", "tool", {}, "stream", "owner", socket, "chat")
        assert request.startswith("stream_tool_")
        remote.send.assert_awaited_once()
