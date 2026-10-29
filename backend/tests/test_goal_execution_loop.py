"""Exercises goal routing, ordered dispatch, approval barriers, and model repair through chat execution."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import goal_project_context, goal_verification, hitl_confirmation
from orchestrator.goal_requirements import build_goal_contract
from tests import test_goal_chat_loop as goal_loop
from tests.test_goal_chat_loop import _chat, _verdict
from tests.test_turn_seams_loop import _last_assistant_text, _msg, _register, _tc, _usage


orch = goal_loop.orch
goal_orch = goal_loop.goal_orch

PROJECT = {
    "project_id": "project", "computer_id": "mac", "computer_name": "Work Mac",
    "workspace_roots": [{"path": "/work/project"}], "primary_root": "/work/project",
    "folder_status": "granted",
}


@pytest.mark.asyncio
async def test_remote_project_plan_is_repaired_before_any_effect(goal_orch, monkeypatch, user_skills_disabled):
    o = goal_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "2")
    monkeypatch.setattr(goal_project_context, "project_goal_context", AsyncMock(return_value=PROJECT))
    monkeypatch.setattr(goal_verification, "classify_goal", AsyncMock(
        return_value=build_goal_contract("local_project", PROJECT)))
    _register(o, "make_directory", "remote-compute-1")
    _register(o, "run_command", "computer-use-1")
    from orchestrator import tool_visibility

    pairs = [(agent, o.agent_cards[agent].skills[0]) for agent in ("remote-compute-1", "computer-use-1")]
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda *_args, **_kwargs: pairs)
    chat_id, ws = await _chat(o)
    plans = 0

    async def provider(_socket, messages, tools_desc=None, **kwargs):
        nonlocal plans
        plans += 1
        if plans == 1:
            names = [tool["function"]["name"] for tool in tools_desc]
            call = _tc(next(name for name in names if name.endswith("make_directory")), "wrong-route")
            call.function.arguments = json.dumps({"path": "/work/project/game"})
        else:
            assert "Do not dispatch this batch" in str(messages)
            assert "run_command" in str(messages)
            names = [tool["function"]["name"] for tool in tools_desc]
            call = _tc(next(name for name in names if name.endswith("run_command")), "correct-route")
        return _msg(tool_calls=[call]), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"exit_code": 0}}, error=None, ui_components=[], correlation_id=None))
    await o.handle_chat_message(ws, "Create and test the local game", chat_id, user_id="seam-user")

    assert plans == 2
    o.execute_single_tool.assert_awaited_once()
    assert o.execute_single_tool.await_args.args[1].function.name.endswith("run_command")


@pytest.mark.asyncio
async def test_pending_approval_stops_remaining_batch_and_preserves_original_goal(
    goal_orch, monkeypatch, user_skills_disabled,
):
    o = goal_orch
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    o._ws_active_chat = {id(ws): chat_id}
    hitl_confirmation.begin_task(o, ws, "seam-user", chat_id)
    o._call_llm = AsyncMock(return_value=(
        _msg(tool_calls=[_tc("run_command", "parent"), _tc("run_command", "child")]), _usage()))
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"status": "confirmation_required"}}, error=None,
        ui_components=[], correlation_id=None))
    original = "Create the game, install dependencies, run tests, and verify startup"
    await o.handle_chat_message(ws, original, chat_id, user_id="seam-user")

    o.execute_single_tool.assert_awaited_once()
    assert o._call_llm.await_count == 1
    binding = o._hitl_goal_contexts[("seam-user", chat_id)]
    assert binding.progress.request == original
    assert binding.progress.requires_approval
    assert binding.progress.tool_count == 1
    assert "Waiting for approval" in await _last_assistant_text(o, chat_id, "seam-user")


@pytest.mark.asyncio
async def test_no_response_is_repaired_once_without_replaying_a_tool(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    plans = 0

    async def provider(_socket, messages, tools_desc=None, **kwargs):
        nonlocal plans
        if kwargs.get("feature") == "goal_verification":
            return _verdict("complete")
        plans += 1
        if plans == 1:
            return None, _usage()
        if plans == 2:
            assert "Recover the original objective" in str(messages)
            return _msg(tool_calls=[_tc("run_command", "only-effect")]), _usage()
        return _msg(content="The requested check passed."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"exit_code": 0}}, error=None, ui_components=[], correlation_id=None))
    await o.handle_chat_message(ws, "Run the check and verify it", chat_id, user_id="seam-user")

    assert plans == 3
    o.execute_single_tool.assert_awaited_once()
    assert "check passed" in await _last_assistant_text(o, chat_id, "seam-user")


@pytest.mark.asyncio
async def test_repeated_no_response_stops_after_bounded_repair(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)
    o._call_llm = AsyncMock(return_value=(None, _usage()))
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "Original request", chat_id, user_id="seam-user")

    assert o._call_llm.await_count == 2
    o.execute_single_tool.assert_not_awaited()
    assert "model did not return" in await _last_assistant_text(o, chat_id, "seam-user")


@pytest.mark.asyncio
async def test_final_review_repairs_malformed_output_without_new_tool_effect(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)
    reviews = 0

    async def provider(_socket, messages, tools_desc=None, **kwargs):
        nonlocal reviews
        if kwargs.get("feature") == "goal_verification":
            reviews += 1
            assert json.loads(messages[-1]["content"])["objective"] == "What is two plus two?"
            return (_msg(content="invalid"), _usage()) if reviews == 1 else _verdict("complete")
        return _msg(content="Four."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "What is two plus two?", chat_id, user_id="seam-user")

    assert reviews == 2
    o.execute_single_tool.assert_not_awaited()
    assert "Four." in await _last_assistant_text(o, chat_id, "seam-user")


@pytest.mark.asyncio
async def test_failed_first_batch_call_retains_only_executed_calls_in_provider_history(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    plans = 0

    async def provider(_socket, messages, tools_desc=None, **kwargs):
        nonlocal plans
        if kwargs.get("feature") == "goal_verification":
            return _verdict("complete")
        plans += 1
        if plans == 1:
            return _msg(tool_calls=[_tc("run_command", "parent"), _tc("run_command", "undispatched-child")]), _usage()
        if plans == 2:
            requests = [item for item in messages if getattr(item, "tool_calls", None)]
            assert len(requests) == 1
            assert [call.id for call in requests[0].tool_calls] == ["parent"]
            results = [item for item in messages if isinstance(item, dict) and item.get("role") == "tool"]
            assert [item["tool_call_id"] for item in results] == ["parent"]
            return _msg(tool_calls=[_tc("run_command", "retry-parent")]), _usage()
        return _msg(content="The requested check passed."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(side_effect=[SimpleNamespace(
        result={"_data": {"exit_code": status}}, error=None, ui_components=[], correlation_id=None,
    ) for status in (1, 0)])
    await o.handle_chat_message(ws, "Run and verify the requested check", chat_id, user_id="seam-user")
    assert plans == 3
    assert o.execute_single_tool.await_count == 2
    assert "check passed" in await _last_assistant_text(o, chat_id, "seam-user")
