"""Runs failed goal turns through admitted connection work and real publication."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest_plugins = ("tests.test_connection_publication_owner",)


@pytest.mark.asyncio
@pytest.mark.parametrize("task_state_enabled", [False, True])
@pytest.mark.parametrize("failure_mode", ["no_response", "budget", "budget_ui"])
async def test_admitted_goal_failure_commits_incomplete_assistant(
    orchestrator_factory,
    database,
    monkeypatch,
    user_skills_disabled,
    task_state_enabled,
    failure_mode,
):
    from astralprims import Card, Text
    from orchestrator import goal_verification
    from orchestrator.goal_requirements import build_goal_contract
    from orchestrator.orchestrator import _ConnectionOperation
    from orchestrator.task_state import TaskManager, TaskState
    from orchestrator.work_admission import OperationState
    from shared.feature_flags import flags
    from tests import test_connection_publication_owner as connection_helpers
    from tests import test_turn_seams_loop as seams

    host = orchestrator_factory()
    host._llm_store.set_sync(
        connection_helpers.OWNER,
        provider="custom",
        base_url="http://test.invalid/v1",
        model="test-model",
        api_key="test-key",
    )
    connection_helpers.attach_plane(host, database)
    if task_state_enabled:
        host.task_manager = TaskManager(host.work_admission)
    socket = connection_helpers.Socket()
    host.ui_sessions[socket] = {
        "sub": connection_helpers.OWNER,
        "preferred_username": connection_helpers.OWNER,
    }
    host.ui_clients = [socket]
    host._ws_active_chat = {}
    host._conversation_scopes = {}
    host._workspace_locks = {}
    context = connection_helpers.ConnectionContext(
        socket,
        uuid.uuid4(),
        999999,
        connection_generation=uuid.uuid4(),
        registered=True,
    )
    host._connection_contexts = {id(socket): context}
    chat_id = await asyncio.to_thread(
        host.history.create_chat, user_id=connection_helpers.OWNER
    )
    from orchestrator.chat_steps import ChatStepRecorder

    monkeypatch.setattr(ChatStepRecorder, "start", AsyncMock(return_value="phase-step"))
    monkeypatch.setattr(ChatStepRecorder, "complete", AsyncMock())
    host.summarize_chat_title = AsyncMock()
    host._design_turn_post_done = AsyncMock(return_value=None)
    monkeypatch.setattr(
        goal_verification,
        "classify_goal",
        AsyncMock(return_value=build_goal_contract("informational")),
    )
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "task_state_machine", task_state_enabled)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    max_turns = 8 if failure_mode == "budget_ui" else 1
    monkeypatch.setenv("GOAL_MAX_TURNS", str(max_turns))
    seams._register(host, "run_command")
    planner_calls = 0

    async def provider(_websocket, _messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        if kwargs.get("feature") != "tool_dispatch":
            return seams._msg(content=json.dumps({
                "status": "continue",
                "remaining": ["Continue bounded verification."],
                "reason": "The execution budget remains bounded.",
            })), seams._usage()
        planner_calls += 1
        if failure_mode == "no_response":
            return None, None
        call = seams._tc("run_command", f"goal-{planner_calls}-{uuid.uuid4()}")
        call.function.arguments = json.dumps({"command": f"echo {planner_calls}"})
        return seams._msg(tool_calls=[call]), seams._usage()

    async def execute_tool(*_args, **_kwargs):
        component = (
            [Card(title="Command result", content=[Text(content="Finished")]).to_dict()]
            if failure_mode == "budget_ui"
            else []
        )
        return SimpleNamespace(
            result={"_data": {"exit_code": 0, "stdout": "ok"}},
            error=None,
            ui_components=component,
            correlation_id=None,
        )

    host._call_llm = provider
    host.execute_single_tool = AsyncMock(side_effect=execute_tool)
    ingress = connection_helpers.frame(
        host,
        context,
        action="chat_message",
        chat_id=chat_id,
        message="Create a runnable project and verify it with tests",
    )
    results = await asyncio.to_thread(
        host._submit_connection_batch, context, [ingress]
    )
    _, owner, accepted, _ = results[0]
    assert accepted.accepted
    work = _ConnectionOperation(
        frame=ingress,
        owner=owner,
        operation_id=accepted.operation_id,
    )
    context.operations[work.operation_id] = work

    await host._run_connection_operation(context, work)

    transcript = await asyncio.to_thread(
        host.history.get_chat, chat_id, user_id=connection_helpers.OWNER
    )
    assert transcript["messages"][0]["role"] == "user"
    assert transcript["messages"][-1]["role"] == "assistant"
    assert "Request incomplete" in json.dumps(transcript["messages"][-1]["content"])
    if failure_mode == "budget_ui":
        assert any(
            "card" in json.dumps(message["content"]).lower()
            for message in transcript["messages"]
            if message["role"] == "assistant"
        )
        assert planner_calls == max_turns
    terminal = await asyncio.to_thread(
        host.work_admission.query_operation,
        owner=owner,
        operation_id=work.operation_id,
    )
    assert terminal.state is OperationState.FAILED
    assert terminal.terminal_code == "goal_incomplete"
    assert not any(
        frame.get("type") == "operation_terminal"
        and frame.get("state") == OperationState.COMPLETED.value
        for frame in socket.frames
    )
    assert any(frame.get("type") == "conversation_snapshot" for frame in socket.frames)
    if task_state_enabled:
        task = host.task_manager.get_task(str(work.operation_id))
        assert task is not None
        assert task.max_turns == 10
        assert task._canonical_state() is TaskState.FAILED
    if failure_mode == "no_response":
        assert planner_calls == 1
        host.execute_single_tool.assert_not_awaited()
    else:
        assert host.execute_single_tool.await_count == max_turns
