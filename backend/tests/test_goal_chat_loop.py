"""Exercises goal completion and cancellation through the ordinary authorized chat loop."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.feature_flags import flags
from tests import test_turn_seams_loop as seams
from tests.test_turn_seams_loop import (
    _last_assistant_text,
    _msg,
    _register,
    _tc,
    _usage,
    _ws,
)

orch = seams.orch


@pytest.fixture
def goal_orch(orch, monkeypatch):
    from orchestrator import goal_verification
    from orchestrator.goal_requirements import build_goal_contract

    monkeypatch.setattr(goal_verification, "classify_goal", AsyncMock(return_value=build_goal_contract("informational")))
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    orch._design_turn_post_done = AsyncMock(return_value=None)
    return orch


def _verdict(status, remaining=(), reason=""):
    return _msg(content=json.dumps({"status": status, "remaining": list(remaining), "reason": reason})), _usage()


async def _chat(o):
    chat_id = "goal-" + uuid.uuid4().hex
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id="seam-user")
    return chat_id, _ws(o)


@pytest.mark.asyncio
async def test_requested_tests_continue_after_canvas_only_proposal(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    planner_calls = 0
    verifier_inputs = []

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        if kwargs.get("feature") == "goal_verification":
            verifier_inputs.append(messages)
            if planner_calls == 1:
                return _verdict("continue", ["Create the environment and run the tests before stopping."])
            return _verdict("complete")
        planner_calls += 1
        if planner_calls == 1:
            return _msg(content="The code is ready; install its dependencies and run the tests."), _usage()
        if planner_calls == 2:
            return _msg(tool_calls=[_tc("run_command", "verified-test")]), _usage()
        return _msg(content="The project is installed and its tests passed."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"exit_code": 0, "stdout": "31 tests passed"}},
        error=None, ui_components=[], correlation_id=None,
    ))
    await o.handle_chat_message(ws, "Create a runnable project and verify it with tests", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "tests passed" in final
    assert planner_calls == 3
    assert len(verifier_inputs) == 2
    assert "verified-test" in json.dumps(verifier_inputs[-1])
    assert o.execute_single_tool.await_count == 1


@pytest.mark.asyncio
async def test_informational_answer_can_finish_without_tools(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            assert not tools_desc
            return _verdict("complete")
        return _msg(content="Four."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "What is two plus two?", chat_id, user_id="seam-user")
    assert "Four" in await _last_assistant_text(o, chat_id, "seam-user")
    o.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("review", ["not JSON", '{"status":"complete"}'])
async def test_invalid_verifier_never_publishes_success(goal_orch, user_skills_disabled, review):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            return _msg(content=review), _usage()
        return _msg(content="Everything is finished successfully."), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "Build and test the project", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "finished successfully" not in final
    assert "incomplete" in final.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("round_budget", [1, 128])
async def test_cancellation_during_verification_prevents_continuation(goal_orch, monkeypatch, user_skills_disabled, round_budget):
    o = goal_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", str(round_budget))
    _register(o)
    chat_id, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            o.cancelled_sessions[id(ws)] = True
            return _verdict("continue", ["Run the command."])
        return _msg(content="I will start now."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "Create a project", chat_id, user_id="seam-user")
    o.execute_single_tool.assert_not_awaited()
    assert "cancelled" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()


@pytest.mark.asyncio
async def test_goal_budget_exhaustion_is_incomplete(goal_orch, monkeypatch, user_skills_disabled):
    o = goal_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "2")
    _register(o)
    chat_id, ws = await _chat(o)
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"items": [1]}, error=None, ui_components=[], correlation_id=None,
    ))

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        return _msg(tool_calls=[_tc(cid=uuid.uuid4().hex)]), _usage()

    o._call_llm = provider
    o._generate_tool_summary = AsyncMock(return_value=[])
    await o.handle_chat_message(ws, "Keep researching until verified", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "incomplete" in final.lower()
    assert o.execute_single_tool.await_count == 2
    o._generate_tool_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_limit_publishes_incomplete_goal_outcome(goal_orch, monkeypatch, user_skills_disabled):
    import orchestrator.orchestrator as orchestrator_module

    o = goal_orch
    from orchestrator.task_state import TaskState

    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    task = SimpleNamespace(_canonical_state=lambda: TaskState.RUNNING, task_id="goal-context-limit")
    monkeypatch.setitem(flags._flags, "task_state_machine", True)
    o.task_manager.admit_task = AsyncMock(return_value=task)
    o.task_manager.transition_task = AsyncMock()
    o._append_conversation_message = AsyncMock(return_value=123)
    o._publish_conversation_snapshot = AsyncMock(return_value={})
    o._call_llm = AsyncMock(side_effect=AssertionError("context limit must stop before model calls"))
    evidence_service = SimpleNamespace(
        prepare=AsyncMock(return_value=SimpleNamespace(status="context_limit")),
    )
    monkeypatch.setattr(orchestrator_module.evidence_context, "enabled", lambda: True)
    monkeypatch.setattr(orchestrator_module.evidence_context, "get_context", lambda _orch: evidence_service)
    monkeypatch.setattr(orchestrator_module.evidence_context, "has_references", lambda _messages: False)

    await o._handle_chat_message_impl(
        ws, "Create and verify a project", chat_id, user_id="seam-user",
        conversation_stage=stage, conversation_request_generation=str(uuid.uuid4()),
        llm_preflight_complete=True,
    )

    evidence_service.prepare.assert_awaited_once()
    assert stage.completion_state == "failed"
    o._call_llm.assert_not_awaited()
    assert o._append_conversation_message.await_count == 2
    o._publish_conversation_snapshot.assert_awaited_once()
    assert o._publish_conversation_snapshot.await_args.kwargs["stage"] is stage
    o.task_manager.transition_task.assert_awaited_once_with(
        task, TaskState.FAILED,
        error="The evidence context limit prevented completion verification.",
    )


@pytest.mark.asyncio
async def test_truncated_tool_arguments_never_reach_execution(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)
    calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal calls
        if kwargs.get("feature") == "goal_verification":
            return _verdict("complete")
        calls += 1
        if calls == 1:
            partial = _msg(tool_calls=[_tc(cid="truncated-call")])
            partial._astral_finish_reason = "length"
            return partial, _usage()
        return _msg(content="No action was needed after inspection."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "Inspect the project safely", chat_id, user_id="seam-user")
    o.execute_single_tool.assert_not_awaited()
    assert calls == 2


@pytest.mark.asyncio
async def test_provider_finish_reason_survives_runner_for_goal_gate(monkeypatch):
    from tests.test_harness_standard_runner import Provider
    from tests.test_llm_streaming import _Resp, _bare_orch

    monkeypatch.setitem(flags._flags, "goal_harness", True)
    response = _Resp("Incomplete response")
    response.choices[0].finish_reason = "length"
    o = _bare_orch(Provider([response]))
    message, _ = await o._call_llm(None, [{"role": "user", "content": "A request"}])
    assert getattr(message, "_astral_finish_reason", None) == "length"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, expected_state", [("blocked", "failed"), ("cancelled", "cancelled"), ("truncated", "failed")])
async def test_goal_outcome_is_set_before_conversation_publication(goal_orch, user_skills_disabled, outcome, expected_state):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)
    stage = SimpleNamespace(publication_role="atomic", completion_state="completed")
    o._append_conversation_message = AsyncMock(return_value=123)
    o._publish_conversation_snapshot = AsyncMock(return_value={})

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            if outcome == "cancelled":
                o.cancelled_sessions[id(ws)] = True
                return _verdict("complete")
            if outcome == "truncated":
                reply, usage = _verdict("complete")
                reply._astral_finish_reason = "length"
                return reply, usage
            return _verdict("blocked", reason="The required capability is unavailable.")
        return _msg(content="All work is complete."), _usage()

    o._call_llm = provider
    await o._handle_chat_message_impl(
        ws, "Create and verify the project", chat_id, user_id="seam-user",
        conversation_stage=stage, conversation_request_generation=str(uuid.uuid4()),
    )
    assert stage.completion_state == expected_state
    o._publish_conversation_snapshot.assert_awaited_once()
    assert all("All work is complete" not in str(call.kwargs.get("content"))
               for call in o._append_conversation_message.await_args_list)


@pytest.mark.asyncio
async def test_repeated_truncation_stops_without_effects(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        partial = _msg(tool_calls=[_tc(cid="partial")])
        partial._astral_finish_reason = "length"
        return partial, _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "Perform a verified action", chat_id, user_id="seam-user")
    assert "incomplete" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    o.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_error", [{"code": "outcome_uncertain", "message": "The acknowledgement was lost."}, None])
async def test_uncertain_write_stops_before_another_planner_or_effect(goal_orch, user_skills_disabled, tool_error):
    o = goal_orch
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    planner_calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        assert kwargs.get("feature") != "goal_verification"
        planner_calls += 1
        return _msg(tool_calls=[_tc("run_command", f"write-{planner_calls}")]), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result=None, error=tool_error,
        ui_components=[], correlation_id=None,
    ))
    await o.handle_chat_message(ws, "Write the project file once and verify it", chat_id, user_id="seam-user")
    assert planner_calls == 1
    assert o.execute_single_tool.await_count == 1
    assert "incomplete" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()
