"""Checks frozen goal requirements and actual local evidence through ordinary chat dispatch."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.feature_flags import flags
from tests import test_turn_seams_loop as seams
from tests.test_turn_seams_loop import _last_assistant_text, _msg, _register, _tc, _usage, _ws

orch = seams.orch

PROJECT = {
    "project_id": "project-a", "computer_id": "host-a", "computer_name": "Test Mac",
    "folder_status": "granted", "workspace_roots": [{"name": "Game", "path": "/work/Game"}],
    "primary_root": "/work/Game",
}
CATEGORIES = ("files", "environment", "dependencies", "tests", "startup")


@pytest.fixture
def acceptance_orch(orch, monkeypatch):
    from orchestrator import goal_project_context

    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    monkeypatch.setattr(goal_project_context, "project_goal_context", AsyncMock(return_value=PROJECT))
    orch._design_turn_post_done = AsyncMock(return_value=None)
    return orch


async def _chat(o):
    chat = "goal-acceptance-" + uuid.uuid4().hex
    await asyncio.to_thread(o.history.create_chat, chat, user_id="seam-user")
    return chat, _ws(o)


def _review(evidence=None):
    value = {"status": "complete", "remaining": [], "reason": ""}
    if evidence is not None:
        value["evidence"] = evidence
    return _msg(content=json.dumps(value)), _usage()


@pytest.mark.asyncio
async def test_local_goal_cannot_finish_on_unsubstantiated_complete(acceptance_orch, user_skills_disabled):
    o = acceptance_orch
    _register(o)
    chat, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_requirements":
            return _msg(content='{"kind":"local_project"}'), _usage()
        if kwargs.get("feature") == "goal_verification":
            return _review()
        return _msg(content="Done; the game and tests are on the canvas."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "Create a game in this project and run its tests", chat, user_id="seam-user")
    final = await _last_assistant_text(o, chat, "seam-user")
    assert "incomplete" in final.lower()
    assert "Done; the game" not in final
    o.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["garbage", "truncated", "cancelled"])
async def test_unusable_contract_stops_before_execution(acceptance_orch, user_skills_disabled, kind):
    o = acceptance_orch
    _register(o)
    chat, ws = await _chat(o)
    planner_calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        if kwargs.get("feature") == "goal_requirements":
            reply = _msg(content='{"kind":"local_project"}' if kind != "garbage" else "invalid")
            if kind == "truncated":
                reply._astral_finish_reason = "length"
            if kind == "cancelled":
                o.cancelled_sessions[id(ws)] = True
            return reply, _usage()
        planner_calls += 1
        return _msg(content="Done."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "Create and test a game", chat, user_id="seam-user")
    final = await _last_assistant_text(o, chat, "seam-user")
    assert "cancelled" in final.lower() if kind == "cancelled" else "incomplete" in final.lower()
    assert planner_calls == 0
    o.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_local_completion_binds_proof_to_actual_computer_agent(acceptance_orch, user_skills_disabled, foreign):
    o = acceptance_orch
    _register(o, "run_command", "other-agent" if foreign else "computer-use-1")
    chat, ws = await _chat(o)
    planner_calls = 0

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        if kwargs.get("feature") == "goal_requirements":
            return _msg(content='{"kind":"local_project"}'), _usage()
        if kwargs.get("feature") == "goal_verification":
            return _review({key: ["verified"] for key in CATEGORIES})
        planner_calls += 1
        if planner_calls == 1:
            call = _tc("run_command", "verified")
            call.function.arguments = json.dumps({"cwd": "/work/Game", "argv": [".venv/bin/python", "verify_project.py"]})
            return _msg(tool_calls=[call]), _usage()
        return _msg(content="The project was installed, tested and launched."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"computer": "Test Mac", "cwd": "/work/Game", "exit_code": 0,
                           "timed_out": False, "stdout": "Project files, environment, dependencies, tests and startup verified"}},
        error=None, ui_components=[], correlation_id=None,
    ))
    await o.handle_chat_message(ws, "Create and verify a local game", chat, user_id="seam-user")
    final = await _last_assistant_text(o, chat, "seam-user")
    if foreign:
        assert "incomplete" in final.lower()
        assert planner_calls == 0
        o.execute_single_tool.assert_not_awaited()
    else:
        assert "installed, tested and launched" in final
        o.execute_single_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_selected_project_information_stays_tool_free(acceptance_orch, user_skills_disabled):
    o = acceptance_orch
    _register(o)
    chat, ws = await _chat(o)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_requirements":
            return _msg(content='{"kind":"informational"}'), _usage()
        if kwargs.get("feature") == "goal_verification":
            return _review()
        return _msg(content="A virtual environment isolates Python dependencies."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "What is a Python virtual environment?", chat, user_id="seam-user")
    assert "isolates Python dependencies" in await _last_assistant_text(o, chat, "seam-user")
    o.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["missing_grant", "authority_changed", "cancelled", "classifier_exception"])
async def test_local_contract_respects_authority_and_cancel_boundaries(acceptance_orch, monkeypatch, user_skills_disabled, boundary):
    from orchestrator import goal_project_context

    o = acceptance_orch
    _register(o, "run_command", "computer-use-1")
    chat, ws = await _chat(o)
    planner_calls = 0
    if boundary == "missing_grant":
        monkeypatch.setattr(goal_project_context, "project_goal_context", AsyncMock(return_value=None))
    elif boundary == "authority_changed":
        monkeypatch.setattr(goal_project_context, "project_goal_context", AsyncMock(side_effect=[PROJECT, None]))
    elif boundary == "cancelled":
        async def context(*args):
            if planner_calls:
                o.cancelled_sessions[id(ws)] = True
            return PROJECT
        monkeypatch.setattr(goal_project_context, "project_goal_context", context)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        if kwargs.get("feature") == "goal_requirements":
            if boundary == "classifier_exception":
                raise RuntimeError("classification unavailable")
            return _msg(content='{"kind":"local_project"}'), _usage()
        if kwargs.get("feature") == "goal_verification":
            return _review({key: ["verified"] for key in CATEGORIES})
        planner_calls += 1
        if planner_calls == 1:
            call = _tc("run_command", "verified")
            call.function.arguments = json.dumps({"cwd": "/work/Game", "argv": ["python", "verify_project.py"]})
            return _msg(tool_calls=[call]), _usage()
        return _msg(content="All installation and testing finished successfully."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"computer": "Test Mac", "cwd": "/work/Game", "exit_code": 0, "timed_out": False}},
        error=None, ui_components=[], correlation_id=None,
    ))
    await o.handle_chat_message(ws, "Build a game and verify it", chat, user_id="seam-user")
    final = await _last_assistant_text(o, chat, "seam-user")
    assert "cancelled" in final.lower() if boundary == "cancelled" else "incomplete" in final.lower()
    assert "finished successfully" not in final
    if boundary in {"missing_grant", "classifier_exception"}:
        assert planner_calls == 0
        o.execute_single_tool.assert_not_awaited()


@pytest.mark.parametrize("text", [None, "x" * 4097, "[]", "invalid", '{"status":"complete"}'])
def test_completion_proof_parser_fails_closed(text):
    from orchestrator.goal_verification import parse_review

    with pytest.raises((ValueError, TypeError)):
        parse_review(text)


@pytest.mark.asyncio
async def test_goal_transport_buffer_is_limited_to_planning(acceptance_orch, user_skills_disabled):
    from orchestrator.orchestrator import _GOAL_BUFFERED_RESPONSE

    o = acceptance_orch
    _register(o)
    chat, ws = await _chat(o)
    features = []

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        feature = kwargs.get("feature")
        features.append(feature)
        assert _GOAL_BUFFERED_RESPONSE.get() is (feature == "tool_dispatch")
        if feature == "goal_requirements":
            return _msg(content='{"kind":"informational"}'), _usage()
        if feature == "goal_verification":
            return _review()
        return _msg(content="A virtual environment isolates Python dependencies."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock()
    await o.handle_chat_message(ws, "What is a virtual environment?", chat, user_id="seam-user")
    assert features == ["goal_requirements", "tool_dispatch", "goal_verification"]
    assert _GOAL_BUFFERED_RESPONSE.get() is False
    o.execute_single_tool.assert_not_awaited()
