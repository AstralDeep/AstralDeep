"""Checks goal cancellation rejects late planner tool calls before dispatch."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.feature_flags import flags
from tests.test_turn_seams_loop import _msg, _register, _tc, _usage, _ws

pytest_plugins = ("tests.test_turn_seams_loop",)


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
    monkeypatch.setitem(flags._flags, "task_state_machine", False)
    orch._design_turn_post_done = AsyncMock(return_value=None)
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_point", ["planner", "dispatch_notice"])
async def test_cancel_during_goal_planner_discards_late_tool_call(
    goal_orch, user_skills_disabled, cancel_point
):
    o = goal_orch
    _register(o, "run_command")
    chat_id = "goal-planner-cancel-" + uuid.uuid4().hex
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id="seam-user")
    ws = _ws(o)
    planner_entered = asyncio.Event()
    release_planner = asyncio.Event()
    planner_call = _msg(tool_calls=[_tc("run_command", "late-call")])
    planner_usage = SimpleNamespace(
        prompt_tokens=7, completion_tokens=3, total_tokens=10,
    )
    o.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"exit_code": 0}}, error=None,
        ui_components=[], correlation_id=None,
    ))

    async def provider(_websocket, _messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_requirements":
            return _msg(content='{"kind":"informational"}'), _usage()
        if kwargs.get("feature") == "tool_dispatch":
            if cancel_point == "planner":
                planner_entered.set()
                await release_planner.wait()
            return planner_call, planner_usage
        raise AssertionError("cancelled planner must not reach verification")

    o._call_llm = provider

    if cancel_point == "dispatch_notice":
        async def cancel_before_dispatch(_websocket, payload):
            if json.loads(payload).get("status") == "executing":
                await o._cancel_turn(ws, "seam-user", chat_id)

        o._safe_send = AsyncMock(side_effect=cancel_before_dispatch)

    turn = asyncio.create_task(o.handle_chat_message(
        ws, "Create and verify a project", chat_id, user_id="seam-user",
    ))
    try:
        if cancel_point == "planner":
            await asyncio.wait_for(planner_entered.wait(), timeout=5)
            await o._cancel_turn(ws, "seam-user", chat_id)
    finally:
        release_planner.set()
    await asyncio.wait_for(turn, timeout=10)

    o.execute_single_tool.assert_not_awaited()
    assert o.token_usage[chat_id] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }
    chat = await asyncio.to_thread(o.history.get_chat, chat_id, user_id="seam-user")
    assert chat["messages"][-1]["role"] == "assistant"
    assert "cancelled" in str(chat["messages"][-1]["content"]).lower()
