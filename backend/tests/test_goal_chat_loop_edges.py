"""Exercises goal-loop failure, recovery, and evidence rules through chat dispatch."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.feature_flags import flags
from tests import test_turn_seams_loop as seams

_last_assistant_text = seams._last_assistant_text
_msg = seams._msg
_register = seams._register
_tc = seams._tc
_usage = seams._usage
_ws = seams._ws
orch = seams.orch


@pytest.fixture
def goal_orch(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    orch._design_turn_post_done = AsyncMock(return_value=None)
    return orch


def _verdict(status, remaining=(), reason=""):
    return _msg(content=json.dumps({"status": status, "remaining": list(remaining), "reason": reason})), _usage()


async def _chat(o):
    chat_id = "goal-edge-" + uuid.uuid4().hex
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id="seam-user")
    return chat_id, _ws(o)


@pytest.mark.asyncio
async def test_repeated_identical_tool_failures_stop_as_incomplete(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"exit_code": 2, "stderr": "tests failed"}},
        error=None, ui_components=[], correlation_id=None,
    ))

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            return _verdict("continue", ["Repair the test failure and run the tests again."])
        return _msg(tool_calls=[_tc("run_command")]), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "Run the tests and make them pass", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "incomplete" in final.lower()
    assert o.execute_single_tool.await_count == 3


@pytest.mark.asyncio
async def test_prose_only_continuations_stop_after_three_reviews(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)
    review_count = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal review_count
        if kwargs.get("feature") == "goal_verification":
            review_count += 1
            return _verdict("continue", ["Run a tool before finishing."])
        return _msg(content="I am still working on it."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "Create a project and verify it", chat_id, user_id="seam-user")
    assert "incomplete" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    assert review_count == 3
    o.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_verifier_exception_is_incomplete(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            raise RuntimeError("review service unavailable")
        return _msg(content="Everything is finished successfully."), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "Build and test the project", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "incomplete" in final.lower()
    assert "finished successfully" not in final


@pytest.mark.asyncio
async def test_background_command_needs_terminal_read_output_evidence(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o, "run_command", "runner")
    _register(o, "read_output", "reader")
    chat_id, ws = await _chat(o)
    planner = 0
    review_evidence = []

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner
        if kwargs.get("feature") == "goal_verification":
            evidence = json.loads(messages[-1]["content"])
            review_evidence.append(evidence["observed_evidence"])
            return _verdict("complete")
        planner += 1
        if planner == 1:
            return _msg(tool_calls=[_tc("run_command", "start")]), _usage()
        if planner == 2:
            return _msg(content="The command was started."), _usage()
        if planner == 3:
            poll = SimpleNamespace(
                id="poll",
                function=SimpleNamespace(name="read_output", arguments=json.dumps({"job_id": "job-17"})),
            )
            return _msg(tool_calls=[poll]), _usage()
        return _msg(content="The tests passed after the command exited."), _usage()

    o._call_llm = provider

    async def execute(websocket, call, *args, **kwargs):
        name = call.function.name
        if name == "run_command":
            return SimpleNamespace(result={"_data": {"job_id": "job-17", "background": True}}, error=None,
                                   ui_components=[], correlation_id=None)
        return SimpleNamespace(result={"_data": {
            "job_id": "job-17", "state": "exited", "exit_code": 0, "stdout": "tests passed",
        }}, error=None, ui_components=[], correlation_id=None)

    o.execute_single_tool = AsyncMock(side_effect=execute)
    await o.handle_chat_message(ws, "Run the background tests and report their result", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "tests passed" in final
    assert o.execute_single_tool.await_count == 2
    assert len(review_evidence) == 2
    assert "job-17" in review_evidence[0]["pending_jobs"]
    assert review_evidence[-1]["tools"][-1]["status"] == "exited"


@pytest.mark.asyncio
async def test_tool_failure_can_be_repaired_and_verified(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    planner = 0
    reviewed = []

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner
        if kwargs.get("feature") == "goal_verification":
            reviewed.append(json.loads(messages[-1]["content"])["observed_evidence"])
            return _verdict("complete")
        planner += 1
        if planner < 3:
            return _msg(tool_calls=[_tc("run_command", f"attempt-{planner}")]), _usage()
        return _msg(content="The repaired test run passed."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(side_effect=[
        SimpleNamespace(result={"_data": {"exit_code": 1, "stderr": "initial failure"}}, error=None,
                        ui_components=[], correlation_id=None),
        SimpleNamespace(result={"_data": {"exit_code": 0, "stdout": "tests passed"}}, error=None,
                        ui_components=[], correlation_id=None),
    ])
    await o.handle_chat_message(ws, "Repair and pass the tests", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "repaired test run passed" in final
    assert len(reviewed) == 1
    assert o.execute_single_tool.await_count == 2
    assert [item["outcome"] for item in reviewed[0]["tools"]] == ["failure", "success"]


@pytest.mark.asyncio
async def test_original_objective_survives_compaction(goal_orch, monkeypatch, user_skills_disabled):
    import orchestrator.orchestrator as orchestrator_module

    o = goal_orch
    _register(o)
    monkeypatch.setitem(flags._flags, "message_compaction", True)
    chat_id, ws = await _chat(o)
    compacted = False

    async def compact(messages, *args, **kwargs):
        nonlocal compacted
        compacted = True
        return [{"role": "system", "content": "Compacted conversation without original user request."}], True

    monkeypatch.setattr(orchestrator_module, "compact_messages", compact)
    objectives = []

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            objectives.append(json.loads(messages[-1]["content"])["objective"])
            return _verdict("complete")
        assert "ORIGINAL objective: produce a verified report" in json.dumps(messages)
        messages[:] = [{"role": "system", "content": "Planner context edited."}]
        return _msg(content="Done."), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "ORIGINAL objective: produce a verified report", chat_id, user_id="seam-user")
    assert compacted
    assert objectives == ["ORIGINAL objective: produce a verified report"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_budget", ["not-a-number", "0", "513"])
async def test_invalid_goal_budget_fails_safely_before_tool_dispatch(goal_orch, monkeypatch, user_skills_disabled, invalid_budget):
    o = goal_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", invalid_budget)
    _register(o, "run_command")
    chat_id, ws = await _chat(o)
    o.execute_single_tool = AsyncMock()

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        return _msg(tool_calls=[_tc("run_command")]), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "Run the command and verify it", chat_id, user_id="seam-user")
    assert "incomplete" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    o.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_verdict_after_cancel_does_not_publish_success(goal_orch, user_skills_disabled):
    o = goal_orch
    _register(o)
    chat_id, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            o.cancelled_sessions[id(ws)] = True
            return _verdict("complete")
        return _msg(content="The work is done."), _usage()

    o._call_llm = provider
    await o.handle_chat_message(ws, "Create and verify a project", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "cancelled" in final.lower()
    assert "work is done" not in final.lower()


@pytest.mark.asyncio
async def test_goal_budget_allows_more_than_24_tool_rounds(goal_orch, monkeypatch, user_skills_disabled):
    o = goal_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "128")
    _register(o, "search_tool")
    chat_id, ws = await _chat(o)
    planner = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner
        if kwargs.get("feature") == "goal_verification":
            return _verdict("complete")
        planner += 1
        if planner <= 25:
            return _msg(tool_calls=[_tc(cid=f"round-{planner}")]), _usage()
        return _msg(content="Completed all 25 research actions."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"items": ["result"]}}, error=None, ui_components=[], correlation_id=None,
    ))
    await o.handle_chat_message(ws, "Complete the requested research and verify it", chat_id, user_id="seam-user")
    assert "completed all 25" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    assert planner == 26
    assert o.execute_single_tool.await_count == 25
