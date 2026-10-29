"""Covers durable goal outcomes when a model call returns no response."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.feature_flags import flags
from tests import test_turn_seams_loop as seams
from tests.test_turn_seams_loop import _register, _ws

orch = seams.orch


async def _chat(o):
    chat_id = "goal-model-failure-" + uuid.uuid4().hex
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id="seam-user")
    return chat_id, _ws(o)


@pytest.fixture
def goal_orch(orch, monkeypatch):
    from orchestrator import goal_verification
    from orchestrator.goal_requirements import build_goal_contract

    monkeypatch.setattr(
        goal_verification,
        "classify_goal",
        AsyncMock(return_value=build_goal_contract("informational")),
    )
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    orch._design_turn_post_done = AsyncMock(return_value=None)
    return orch


def _disable_chat_step_persistence(monkeypatch):
    from orchestrator.chat_steps import ChatStepRecorder

    monkeypatch.setattr(ChatStepRecorder, "start", AsyncMock(return_value="phase-step"))
    monkeypatch.setattr(ChatStepRecorder, "complete", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_call", [False, True])
async def test_missing_model_response_publishes_goal_failure_or_cancellation(
    goal_orch, monkeypatch, user_skills_disabled, cancel_during_call
):
    from orchestrator.task_state import TaskState

    o = goal_orch
    _disable_chat_step_persistence(monkeypatch)
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    task = SimpleNamespace(
        _canonical_state=lambda: TaskState.RUNNING,
        _execution_fence=None,
        task_id="goal-model-failure",
    )
    monkeypatch.setitem(flags._flags, "task_state_machine", True)
    o.task_manager.admit_task = AsyncMock(return_value=task)
    o.task_manager.assert_current_execution = AsyncMock()
    o.task_manager.transition_task = AsyncMock()
    appended = []

    async def append_message(_stage, **message):
        appended.append(message)
        return len(appended)

    o._append_conversation_message = AsyncMock(side_effect=append_message)
    o._publish_conversation_snapshot = AsyncMock(return_value={})

    async def no_response(*_args, **_kwargs):
        if cancel_during_call:
            o.cancelled_sessions[id(ws)] = True
        return None, None

    o._call_llm = AsyncMock(side_effect=no_response)
    await o._handle_chat_message_impl(
        ws,
        "Create and verify a project",
        chat_id,
        user_id="seam-user",
        conversation_stage=stage,
        conversation_request_generation=str(uuid.uuid4()),
        llm_preflight_complete=True,
    )

    assert appended[0]["role"] == "user"
    assert appended[0]["content"] == "Create and verify a project"
    assert o._call_llm.await_count == (1 if cancel_during_call else 2)
    assert o._call_llm.await_args.kwargs["feature"] == "tool_dispatch"
    o._publish_conversation_snapshot.assert_awaited_once()
    assert o._publish_conversation_snapshot.await_args.kwargs["stage"] is stage
    assert o.task_manager.transition_task.await_count == 1
    if cancel_during_call:
        assert stage.completion_state == "cancelled"
        assert o.task_manager.transition_task.await_args.args == (task, TaskState.CANCELLED)
        assert "cancelled" in str(appended[-1]["content"]).lower()
    else:
        assert stage.completion_state == "failed"
        assert o.task_manager.transition_task.await_args.args == (task, TaskState.FAILED)
        assert "Request incomplete" in str(appended[-1]["content"])
        assert "model did not return a response" in str(appended[-1]["content"]).lower()


@pytest.mark.asyncio
async def test_model_failure_publication_error_does_not_claim_completion(
    goal_orch, monkeypatch, user_skills_disabled
):
    from orchestrator.task_state import TaskState

    o = goal_orch
    _disable_chat_step_persistence(monkeypatch)
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    task = SimpleNamespace(
        _canonical_state=lambda: TaskState.RUNNING,
        _execution_fence=None,
        task_id="goal-model-failure-publication",
    )
    monkeypatch.setitem(flags._flags, "task_state_machine", True)
    o.task_manager.admit_task = AsyncMock(return_value=task)
    o.task_manager.assert_current_execution = AsyncMock()
    o.task_manager.transition_task = AsyncMock()
    o._append_conversation_message = AsyncMock(return_value=1)
    o._publish_conversation_snapshot = AsyncMock(
        side_effect=RuntimeError("atomic publication failed")
    )
    o._call_llm = AsyncMock(return_value=(None, None))

    await o._handle_chat_message_impl(
        ws,
        "Create and verify a project",
        chat_id,
        user_id="seam-user",
        conversation_stage=stage,
        conversation_request_generation=str(uuid.uuid4()),
        llm_preflight_complete=True,
    )

    assert stage.completion_state == "failed"
    assert o._call_llm.await_count == 2
    assert o._call_llm.await_args.kwargs["feature"] == "tool_dispatch"
    o._publish_conversation_snapshot.assert_awaited_once()
    o.task_manager.transition_task.assert_awaited_once_with(
        task,
        TaskState.FAILED,
        error="The model did not return a response after the configured attempts.",
    )
