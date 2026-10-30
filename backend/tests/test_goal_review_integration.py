"""Checks independent review and preview ordering in the real goal chat loop."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from orchestrator import (
    goal_execution,
    goal_project_context,
    goal_requirements,
    goal_review,
    goal_verification,
)
from orchestrator.goal_progress import CompletionDecision
from orchestrator.goal_requirements import ProofAssessment, build_goal_contract
from orchestrator.goal_review import ReviewResult
from shared.feature_flags import flags
from shared.protocol import AgentSkill

from tests import test_turn_seams_loop as seams
from tests.test_goal_chat_loop import _chat
from tests.test_turn_seams_loop import (
    _last_assistant_text,
    _msg,
    _register,
    _tc,
    _usage,
)

orch = seams.orch

PROJECT = {"project_id": "project", "computer_id": "mac", "computer_name": "Mac",
           "folder_status": "granted", "primary_root": "/work/demo", "workspace_roots": [{"path": "/work"}]}


@pytest.fixture
def reviewed_orch(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "goal_harness", True)
    monkeypatch.setitem(flags._flags, "typesafe_routing", False)
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    monkeypatch.setattr(goal_project_context, "project_goal_context", AsyncMock(return_value=PROJECT))
    monkeypatch.setattr(goal_verification, "classify_goal", AsyncMock(return_value=build_goal_contract("local_project", PROJECT)))
    monkeypatch.setattr(goal_execution, "focus_project_catalog", lambda tools, owners, names, *a, **k: (tools, owners, names))
    monkeypatch.setattr(goal_requirements, "select_goal_proof_references", lambda *a: {"tests": ["prior-proof"]})
    monkeypatch.setattr(goal_requirements, "validate_goal_proofs", lambda *a: ProofAssessment(True))
    monkeypatch.setattr(goal_verification, "evaluate_completion", AsyncMock(return_value=(CompletionDecision("complete"), {"tests": ["prior-proof"]})))
    orch._design_turn_post_done = AsyncMock(return_value=None)
    _register(orch, "run_command", "computer-use-1")
    orch.agent_cards["computer-use-1"].skills.append(AgentSkill(
        name="start", description="Start project session", id="start_session", input_schema={"type": "object"}))
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("review_status", ["passed", "blocked"])
@pytest.mark.parametrize("stale_proof", [False, True])
async def test_completion_requires_review_in_worker_and_fallback_paths(
    reviewed_orch, monkeypatch, user_skills_disabled, fallback, review_status, stale_proof,
):
    from orchestrator import goal_preview, goal_worker

    o = reviewed_orch
    chat_id, ws = await _chat(o)
    events = []
    if stale_proof and review_status == "passed":
        validations = iter([ProofAssessment(False, missing=("tests",)), ProofAssessment(True)])
        monkeypatch.setattr(goal_requirements, "validate_goal_proofs", lambda *a: next(validations))
    if fallback:
        monkeypatch.setattr(goal_worker, "build_worker_command", lambda *a, **k: (_ for _ in ()).throw(ValueError("worker unavailable")))

    async def execute(*args, **kwargs):
        events.append("tool")
        return SimpleNamespace(result={"_data": {"exit_code": 0, "stdout": "verified"}}, error=None, ui_components=[], correlation_id=None)

    async def review(*args, **kwargs):
        events.append("review")
        return ReviewResult(review_status, "Review exhausted with inert project button.", 3,
                            {"tests": ["reviewed-proof"]}, True)

    async def preview(*args, **kwargs):
        assert events[-1] == "review"
        events.append("preview")
        return {"status": "running", "url": "http://127.0.0.1:4567",
                "ui_components": [{"type": "Text", "text": "Preview controls"}],
                "_result": SimpleNamespace(correlation_id="preview-correlation"),
                "_tool_name": "project_preview", "_tool_arguments": {"job_id": "preview-job"}}

    planner_calls = 0

    async def provider(*args, **kwargs):
        nonlocal planner_calls
        planner_calls += 1
        if fallback and planner_calls == 1:
            return _msg(tool_calls=[_tc("run_command", "fallback-proof")]), _usage()
        return _msg(content="Builder claims complete."), _usage()

    o._moa_panel = AsyncMock(side_effect=AssertionError("reviewed completion must not be rewritten"))
    o._call_llm = provider
    o.execute_single_tool = AsyncMock(side_effect=execute)
    reviewer = AsyncMock(side_effect=review)
    previewer = AsyncMock(side_effect=preview)
    monkeypatch.setattr(goal_review, "review_local_project", reviewer)
    monkeypatch.setattr(goal_preview, "start_local_preview", previewer)
    await o.handle_chat_message(ws, "Build and test the app", chat_id, user_id="seam-user")
    final = await _last_assistant_text(o, chat_id, "seam-user")
    o._moa_panel.assert_not_awaited()
    assert reviewer.await_count == (2 if stale_proof and review_status == "passed" else 1)
    assert "tool" in events and events.index("tool") < events.index("review")
    if review_status == "passed":
        previewer.assert_awaited_once()
        assert events[-2:] == ["review", "preview"]
        assert "http://127.0.0.1:4567" in final
        assert "Independent review" in final
        o._deliver_round_components.assert_awaited()
        delivered = o._deliver_round_components.await_args.args[1]
        assert delivered[0]["text"] == "Preview controls"
        assert "project_preview" in str(delivered)
    else:
        previewer.assert_not_awaited()
        assert "incomplete" in final.lower()
        assert "Builder claims complete" not in final
        assert "The project is complete" not in final
