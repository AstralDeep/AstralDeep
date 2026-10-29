"""Exercises HITL goal rehydration through the normal chat loop."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import goal_project_context, goal_verification, hitl_confirmation
from orchestrator.goal_requirements import PROOF_CATEGORIES, build_goal_contract
from tests import test_goal_chat_loop as goal_loop
from tests.test_goal_chat_loop import _chat, _verdict
from tests.test_turn_seams_loop import _msg, _register, _tc, _usage


orch = goal_loop.orch
goal_orch = goal_loop.goal_orch
PROJECT = {
    "project_id": "project", "computer_id": "mac", "computer_name": "Work Mac",
    "workspace_roots": [{"path": "/work/project"}], "primary_root": "/work/project",
    "folder_status": "granted",
}
ORIGINAL = "Create the local game, install dependencies, run tests, and verify startup"


def _project_writer(o, monkeypatch):
    from orchestrator import tool_visibility

    _register(o, "write_file", "computer-use-1")
    pairs = [("computer-use-1", o.agent_cards["computer-use-1"].skills[0])]
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda *_args, **_kwargs: pairs)


def _approval_result():
    return SimpleNamespace(
        result={"_data": {"status": "confirmation_required", "message": "Review this action."}},
        error=None, ui_components=[{"type": "card", "title": "Review action"}], correlation_id=None,
    )


@pytest.mark.asyncio
async def test_approved_goal_resumes_original_contract_and_cannot_complete_without_proof(
    goal_orch, monkeypatch, user_skills_disabled,
):
    o = goal_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "1")
    monkeypatch.setattr(goal_project_context, "project_goal_context", AsyncMock(return_value=PROJECT))
    monkeypatch.setattr(goal_verification, "classify_goal", AsyncMock(
        return_value=build_goal_contract("local_project", PROJECT)))
    _project_writer(o, monkeypatch)
    chat_id, ws = await _chat(o)
    o._ws_active_chat = {id(ws): chat_id}
    hitl_confirmation.begin_task(o, ws, "seam-user", chat_id)
    mkdir = _tc("write_file", "write-review")
    mkdir.function.arguments = json.dumps({"path": "/work/project/game"})
    planner_calls = 0
    review_payloads = []

    async def provider(_socket, messages, tools_desc=None, **kwargs):
        nonlocal planner_calls
        if kwargs.get("feature") == "goal_verification":
            review_payloads.append(json.loads(messages[-1]["content"]))
            return _verdict("complete")
        planner_calls += 1
        if planner_calls == 1:
            return _msg(tool_calls=[mkdir]), _usage()
        assert "Frozen completion requirements" in str(messages)
        assert "The exact approved action completed" in str(messages)
        return _msg(content="The local game is complete."), _usage()

    o._call_llm = provider
    o.execute_single_tool = AsyncMock(return_value=_approval_result())
    await o.handle_chat_message(ws, ORIGINAL, chat_id, user_id="seam-user")
    binding = o._hitl_goal_contexts[("seam-user", chat_id)]
    assert binding.progress.requires_approval
    token = binding.progress._pending_approvals[next(iter(binding.progress._pending_approvals))]
    assert binding.contract.required_categories == PROOF_CATEGORIES
    assert o.execute_single_tool.await_count == 1

    assert binding.progress.resolve_approval(
        next(iter(binding.progress._pending_approvals)),
        SimpleNamespace(result={"_data": {"computer": "Work Mac", "path": "/work/project/game"}},
                        error=None),
        tool_name="write_file", arguments={"path": "/work/project/game"},
        agent_id="computer-use-1", computer_id="mac", computer_name="Work Mac",
        project_context=PROJECT,
    ) is True
    assert token["tool"] == "write_file"

    with hitl_confirmation._goal_continuation_scope(binding):
        await o.handle_chat_message(
            ws, hitl_confirmation.CONTINUATION_TEXT, chat_id, user_id="seam-user")

    goal_verification.classify_goal.assert_awaited_once()
    assert planner_calls == 2
    assert len(review_payloads) == 1
    assert review_payloads[0]["objective"] == ORIGINAL
    assert tuple(review_payloads[0]["requirements"]["required_categories"]) == PROOF_CATEGORIES
    assert review_payloads[0]["observed_evidence"]["tools"][-1]["tool"] == "write_file"
    assert "incomplete" in (await goal_loop._last_assistant_text(o, chat_id, "seam-user")).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["project", "disabled"])
async def test_changed_project_authority_blocks_resumed_goal_before_planning(
    goal_orch, monkeypatch, user_skills_disabled, boundary,
):
    from shared.feature_flags import flags

    o = goal_orch
    monkeypatch.setenv("GOAL_MAX_TURNS", "1")
    changed = {**PROJECT, "computer_id": "replacement-mac"} if boundary == "project" else PROJECT
    monkeypatch.setattr(goal_project_context, "project_goal_context", AsyncMock(side_effect=[PROJECT, changed]))
    monkeypatch.setattr(goal_verification, "classify_goal", AsyncMock(
        return_value=build_goal_contract("local_project", PROJECT)))
    _project_writer(o, monkeypatch)
    chat_id, ws = await _chat(o)
    o._ws_active_chat = {id(ws): chat_id}
    hitl_confirmation.begin_task(o, ws, "seam-user", chat_id)
    mkdir = _tc("write_file", "write-review-stale-project")
    mkdir.function.arguments = json.dumps({"path": "/work/project/game"})
    o._call_llm = AsyncMock(return_value=(_msg(tool_calls=[mkdir]), _usage()))
    o.execute_single_tool = AsyncMock(return_value=_approval_result())
    await o.handle_chat_message(ws, ORIGINAL, chat_id, user_id="seam-user")
    binding = o._hitl_goal_contexts[("seam-user", chat_id)]
    if boundary == "disabled":
        monkeypatch.setitem(flags._flags, "goal_harness", False)

    with hitl_confirmation._goal_continuation_scope(binding):
        await o.handle_chat_message(
            ws, hitl_confirmation.CONTINUATION_TEXT, chat_id, user_id="seam-user")

    goal_verification.classify_goal.assert_awaited_once()
    o._call_llm.assert_awaited_once()
    o.execute_single_tool.assert_awaited_once()
    expected = "authority changed" if boundary == "project" else "goal execution was disabled"
    assert expected in (await goal_loop._last_assistant_text(o, chat_id, "seam-user")).lower()
