"""Drives the real goal chat loop with goal steering on: update_plan and update_goal are answered by the
harness, the plan renders as a stepper, idle turns get the continuation prompt before the verifier is
used, premature and blocked claims behave, and the in-progress review is replaced by plan reminders.
"""

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
_usage = seams._usage
_ws = seams._ws
orch = seams.orch


def _call(name, arguments=None, cid=None):
    return SimpleNamespace(id=cid or f"{name}-{uuid.uuid4().hex[:8]}",
                           function=SimpleNamespace(name=name, arguments=json.dumps(arguments or {})))


def _plan(*steps):
    return _call("update_plan", {"plan": [{"step": step, "status": status} for step, status in steps]})


def _goal(status, reason=None):
    return _call("update_goal", {"status": status, **({"reason": reason} if reason else {})})


def _texts(messages):
    return [str(m.get("content")) for m in messages if isinstance(m, dict)]


@pytest.fixture
def steering_orch(orch, monkeypatch):
    from orchestrator import goal_verification
    from orchestrator.goal_requirements import build_goal_contract

    monkeypatch.setattr(goal_verification, "classify_goal", AsyncMock(return_value=build_goal_contract("action")))
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "goal_steering", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    orch._design_turn_post_done = AsyncMock(return_value=None)
    orch._send_or_replace_components = AsyncMock(return_value=[])
    _register(orch, "run_command")
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"_data": {"exit_code": 0, "stdout": "total 4"}}, error=None, ui_components=[], correlation_id=None))
    return orch


async def _chat(o):
    chat_id = "goal-steer-" + uuid.uuid4().hex
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id="seam-user")
    return chat_id, _ws(o)


def _scripted(replies, seen, verifier=None):
    queue = list(replies)

    async def provider(websocket, messages, tools_desc=None, **kwargs):
        if kwargs.get("feature") == "goal_verification":
            if verifier is None:
                raise AssertionError("the verifier must not run")
            return verifier()
        seen.append({"messages": list(messages), "tools": [t["function"]["name"] for t in tools_desc or []]})
        if tools_desc is None:
            return _msg(content="Summary of the run."), _usage()
        reply = queue.pop(0)
        return (_msg(content=reply) if isinstance(reply, str) else _msg(tool_calls=reply)), _usage()

    return provider


@pytest.mark.asyncio
async def test_plan_work_and_claim_complete_without_the_verifier(steering_orch, user_skills_disabled):
    o = steering_orch
    chat_id, ws = await _chat(o)
    seen = []
    o._call_llm = _scripted([
        [_plan(("List the project root", "in_progress"), ("Report the output", "pending"))],
        [_call("run_command", {"command": "ls -la"})],
        [_plan(("List the project root", "completed"), ("Report the output", "completed"))],
        [_goal("complete")],
        "Listed the project root: total 4.",
    ], seen)
    await o.handle_chat_message(ws, "List the project root and show me the output", chat_id, user_id="seam-user")
    final = (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    assert "listed the project root" in final and "incomplete" not in final
    o.execute_single_tool.assert_awaited_once()
    assert o.execute_single_tool.await_args.args[1].function.name == "run_command"
    assert {"update_plan", "update_goal"} <= set(seen[-1]["tools"])
    steppers = [call.args[1][0] for call in o._send_or_replace_components.await_args_list
                if call.args[1] and call.args[1][0].get("type") == "pipeline_stepper"]
    assert [[s["status"] for s in stepper["steps"]] for stepper in steppers] == [["active", "pending"], ["done", "done"]]
    assert len({stepper["id"] for stepper in steppers}) == 1
    pinned = [text for text in _texts(seen[-1]["messages"]) if text.startswith("[goal plan]")]
    assert len(pinned) == 1 and '"status": "completed"' in pinned[0]
    assert "Plan updated" in _texts(seen[1]["messages"])


@pytest.mark.asyncio
async def test_idle_turns_are_steered_twice_before_the_verifier_decides(steering_orch, user_skills_disabled):
    from tests.test_goal_chat_loop_edges import _verdict

    o = steering_orch
    chat_id, ws = await _chat(o)
    seen = []
    verdicts = []

    def verifier():
        verdicts.append("complete")
        return _verdict("complete")

    o._call_llm = _scripted([
        [_call("run_command", {"command": "ls -la"})],
        "It is probably done.",
        "Still done.",
        "Done: total 4.",
    ], seen, verifier)
    await o.handle_chat_message(ws, "List the project root", chat_id, user_id="seam-user")
    final = (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    assert "done: total 4" in final
    assert verdicts == ["complete"]
    steering = [text for text in _texts(seen[-1]["messages"]) if text.startswith("The goal is still open")]
    assert len(steering) == 2 and "No-progress check" in steering[0]


@pytest.mark.asyncio
async def test_a_premature_claim_is_refused_until_the_plan_is_finished(steering_orch, user_skills_disabled):
    o = steering_orch
    chat_id, ws = await _chat(o)
    seen = []
    o._call_llm = _scripted([
        [_plan(("Run the listing", "in_progress"), ("Check the output", "pending"))],
        [_call("run_command", {"command": "ls -la"})],
        [_goal("complete")],
        [_plan(("Run the listing", "completed"), ("Check the output", "completed"))],
        [_goal("complete")],
        "Listing verified.",
    ], seen)
    await o.handle_chat_message(ws, "List and check the project root", chat_id, user_id="seam-user")
    assert "listing verified" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    refusal = [text for text in _texts(seen[3]["messages"]) if text.startswith("update_goal was not applied")]
    assert refusal and "Check the output" in refusal[0]


@pytest.mark.asyncio
async def test_a_blocked_claim_stops_with_the_models_explanation(steering_orch, user_skills_disabled):
    o = steering_orch
    chat_id, ws = await _chat(o)
    o._call_llm = _scripted([
        [_call("run_command", {"command": "ls -la"})],
        [_goal("blocked", "The deploy key is missing")],
        "I cannot continue without the deploy key.",
    ], [])
    await o.handle_chat_message(ws, "Deploy the project", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "I cannot continue without the deploy key." in final
    assert "Request incomplete" in final and "The deploy key is missing" in final


@pytest.mark.asyncio
async def test_plan_reminders_replace_the_in_progress_review(steering_orch, monkeypatch, user_skills_disabled):
    from orchestrator import goal_verification

    o = steering_orch
    monkeypatch.setattr(goal_verification, "evaluate_progress", AsyncMock(side_effect=AssertionError("no review")))
    chat_id, ws = await _chat(o)
    seen = []
    o._call_llm = _scripted(
        [[_call("run_command", {"command": f"echo {i}"})] for i in range(9)]
        + [[_plan(("Echo nine times", "completed"))], [_goal("complete")], "Echoed nine times."],
        seen,
    )
    await o.handle_chat_message(ws, "Echo nine numbers", chat_id, user_id="seam-user")
    assert "echoed nine times" in (await _last_assistant_text(o, chat_id, "seam-user")).lower()
    reminders = [text for turn in seen for text in _texts(turn["messages"])
                 if text.startswith("[goal plan]") and "Call update_plan" in text]
    assert reminders


@pytest.mark.asyncio
async def test_steering_off_keeps_the_harness_tools_out_of_the_catalog(steering_orch, monkeypatch, user_skills_disabled):
    from tests.test_goal_chat_loop_edges import _verdict

    o = steering_orch
    monkeypatch.setitem(flags._flags, "goal_steering", False)
    chat_id, ws = await _chat(o)
    seen = []
    o._call_llm = _scripted([[_call("run_command", {"command": "ls"})], "Done."], seen, lambda: _verdict("complete"))
    await o.handle_chat_message(ws, "List the project root", chat_id, user_id="seam-user")
    assert all("update_plan" not in turn["tools"] for turn in seen)
    assert not any(text.startswith("[goal plan]") for turn in seen for text in _texts(turn["messages"]))


@pytest.mark.asyncio
async def test_a_failing_command_can_finish_after_the_model_audits_it(steering_orch, user_skills_disabled):
    o = steering_orch
    o.execute_single_tool = AsyncMock(side_effect=[
        SimpleNamespace(result={"_data": {"exit_code": 0, "stdout": "improve-core.mjs"}},
                        error=None, ui_components=[], correlation_id=None),
        SimpleNamespace(result={"_data": {"exit_code": 1, "stdout": "", "stderr": "SyntaxError: no export main"}},
                        error=None, ui_components=[], correlation_id=None),
    ])
    chat_id, ws = await _chat(o)
    seen = []
    o._call_llm = _scripted([
        [_call("run_command", {"command": "ls"})],
        [_call("run_command", {"command": "node improve-core.mjs"})],
        [_goal("complete")],
        [_call("update_goal", {"status": "complete", "confirm_failures": True})],
        "Ran it: it exits 1 with SyntaxError: no export main.",
    ], seen)
    await o.handle_chat_message(ws, "Run node improve-core.mjs and show me its raw output", chat_id,
                                user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    assert "exits 1 with SyntaxError" in final and "incomplete" not in final.lower()
    audit = [text for text in _texts(seen[3]["messages"]) if text.startswith("update_goal needs your audit")]
    assert audit and "run_command call" in audit[0]
