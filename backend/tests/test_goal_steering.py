"""Covers orchestrator/goal_steering.py: update_plan validation and rendering, update_goal claim gates,
pinned-plan reminders and the continuation prompt, independent of the chat loop.
"""

import json

from orchestrator.goal_steering import (
    GOAL_TOOL,
    PIN_PREFIX,
    PLAN_TOOL,
    GoalPlan,
    answer,
    continuation_prompt,
    plan_for,
    strip_pins,
    tool_definitions,
    turn_context,
)


def _plan_args(*steps, explanation=None):
    value = {"plan": [{"step": step, "status": status} for step, status in steps]}
    if explanation is not None:
        value["explanation"] = explanation
    return json.dumps(value)


def _update(plan, *steps, tool_count=0):
    return answer(plan, PLAN_TOOL, _plan_args(*steps), tool_count=tool_count, successes=0, other_calls=0)


def test_tool_definitions_match_the_plan_and_goal_contract():
    plan_tool, goal_tool = tool_definitions()
    assert plan_tool["function"]["name"] == PLAN_TOOL
    item = plan_tool["function"]["parameters"]["properties"]["plan"]["items"]
    assert item["properties"]["status"]["enum"] == ["pending", "in_progress", "completed"]
    assert item["required"] == ["step", "status"]
    assert goal_tool["function"]["name"] == GOAL_TOOL
    assert goal_tool["function"]["parameters"]["properties"]["status"]["enum"] == ["complete", "blocked"]


def test_a_valid_plan_replaces_the_list_and_records_when_it_changed():
    plan = GoalPlan()
    text, changed = _update(plan, ("Read the project", "completed"), ("Write the loop", "in_progress"), tool_count=4)
    assert (text, changed) == ("Plan updated", True)
    assert plan.steps == [("Read the project", "completed"), ("Write the loop", "in_progress")]
    assert plan.calls_at_update == 4 and plan.updates == 1
    assert plan.unfinished() == ["Write the loop"]
    text, changed = _update(plan, tool_count=5)
    assert changed and plan.steps == []


def test_invalid_plans_are_rejected_without_changing_the_current_plan():
    plan = GoalPlan(steps=[("Keep me", "in_progress")])
    cases = [
        _plan_args(("a", "in_progress"), ("b", "in_progress")),
        _plan_args(("a", "done")),
        _plan_args(("  ", "pending")),
        json.dumps({"plan": [{"step": "a", "status": "pending"}], "id": 3}),
        json.dumps({"plan": "a"}),
        json.dumps({"plan": [{"step": f"s{i}", "status": "pending"} for i in range(13)]}),
        "not json",
        json.dumps({"plan": [], "explanation": 4}),
    ]
    for raw in cases:
        text, changed = answer(plan, PLAN_TOOL, raw, tool_count=1, successes=0, other_calls=0)
        assert text.startswith("update_plan rejected:"), raw
        assert changed is False
    assert plan.steps == [("Keep me", "in_progress")]


def test_long_steps_are_trimmed_and_whitespace_collapsed():
    plan = GoalPlan()
    _update(plan, ("word\n  " * 80, "pending"))
    assert len(plan.steps[0][0]) == 160 and "\n" not in plan.steps[0][0]


def test_completion_claims_are_gated_on_evidence_plan_and_a_solo_call():
    plan = GoalPlan(steps=[("Run the listing", "in_progress")])
    complete = json.dumps({"status": "complete"})
    assert "finish the other tool calls" in answer(plan, GOAL_TOOL, complete, tool_count=1, successes=1, other_calls=1)[0]
    assert "no successful tool result" in answer(plan, GOAL_TOOL, complete, tool_count=0, successes=0, other_calls=0)[0]
    text, _ = answer(plan, GOAL_TOOL, complete, tool_count=1, successes=1, other_calls=0)
    assert "not completed" in text and "Run the listing" in text
    assert plan.claim is None
    _update(plan, ("Run the listing", "completed"))
    text, changed = answer(plan, GOAL_TOOL, complete, tool_count=1, successes=1, other_calls=0)
    assert text.startswith("Goal marked complete") and changed is False
    assert plan.claim == "complete"


def test_blocked_claims_need_a_reason_and_bad_status_is_rejected():
    plan = GoalPlan()
    assert "needs a reason" in answer(plan, GOAL_TOOL, json.dumps({"status": "blocked"}), tool_count=0,
                                      successes=0, other_calls=0)[0]
    assert "status must be" in answer(plan, GOAL_TOOL, json.dumps({"status": "paused"}), tool_count=0,
                                      successes=0, other_calls=0)[0]
    text, _ = answer(plan, GOAL_TOOL, json.dumps({"status": "blocked", "reason": "Need the API key"}),
                     tool_count=0, successes=0, other_calls=1)
    assert text.startswith("Goal marked blocked")
    assert (plan.claim, plan.claim_reason) == ("blocked", "Need the API key")
    assert "not a goal harness tool" in answer(plan, "run_command", "{}", tool_count=0, successes=0, other_calls=0)[0]


def test_turn_context_pins_the_plan_and_fires_each_reminder_once():
    plan = GoalPlan()
    assert turn_context(plan, tool_count=2) is None
    first = turn_context(plan, tool_count=3)
    assert first.startswith(PIN_PREFIX) and "Call update_plan" in first
    assert turn_context(plan, tool_count=4) is None
    _update(plan, ("Inspect", "completed"), ("Build", "in_progress"), tool_count=4)
    pinned = turn_context(plan, tool_count=5)
    assert '"step": "Build", "status": "in_progress"' in pinned and "Reminder" not in pinned
    stale = turn_context(plan, tool_count=12)
    assert "has not changed in 8 tool calls" in stale
    assert "Reminder" not in turn_context(plan, tool_count=13)
    assert "has not changed in 16 tool calls" in turn_context(plan, tool_count=20)
    _update(plan, ("Inspect", "completed"), ("Build", "completed"), tool_count=21)
    assert "Reminder" not in turn_context(plan, tool_count=22)
    assert "Every plan step is completed" in turn_context(plan, tool_count=23)
    assert "Reminder" not in turn_context(plan, tool_count=24)


def test_strip_pins_removes_only_pinned_plan_messages():
    messages = [
        {"role": "system", "content": "anchor"},
        {"role": "system", "content": PIN_PREFIX + "\nold"},
        {"role": "user", "content": PIN_PREFIX},
        {"role": "assistant", "content": None, "tool_calls": []},
    ]
    assert strip_pins(messages) == [messages[0], messages[2], messages[3]]


def test_the_stepper_maps_statuses_and_keeps_one_component_id():
    plan = GoalPlan()
    _update(plan, ("Read", "completed"), ("Write", "in_progress"), ("Test", "pending"))
    first = plan.stepper()
    assert first["type"] == "pipeline_stepper" and first["orientation"] == "vertical"
    assert [step["status"] for step in first["steps"]] == ["done", "active", "pending"]
    assert first["id"].startswith("wc_goalplan_")
    assert plan.stepper()["id"] == first["id"]


def test_plan_for_attaches_one_plan_to_the_goal_progress():
    class Progress:
        pass

    progress = Progress()
    assert plan_for(progress) is plan_for(progress)


def test_continuation_prompt_quotes_the_objective_and_states_the_budget():
    plan = GoalPlan(steps=[("Write the loop", "in_progress")])
    prompt = continuation_prompt("Build <x> & test", plan, turn=7, max_turns=40)
    assert '{"objective": "Build <x> & test"}' in prompt
    assert "turn 7 of 40; 33 turns remain" in prompt
    assert "Write the loop" in prompt
    assert "update_goal with status complete" in prompt
    assert "none yet" in continuation_prompt("x", GoalPlan(), turn=50, max_turns=40)


def test_a_claim_with_failed_calls_needs_an_explicit_audit():
    plan = GoalPlan()
    failures = ["run_command call c1 (failed)"]
    text, _ = answer(plan, GOAL_TOOL, json.dumps({"status": "complete"}), tool_count=2, successes=1,
                     other_calls=0, failures=failures)
    assert text.startswith("update_goal needs your audit first") and "run_command call c1" in text
    assert plan.claim is None and plan.failures_confirmed is False
    text, _ = answer(plan, GOAL_TOOL, json.dumps({"status": "complete", "confirm_failures": True}), tool_count=2,
                     successes=1, other_calls=0, failures=failures)
    assert text.startswith("Goal marked complete")
    assert plan.claim == "complete" and plan.failures_confirmed is True
    clean = GoalPlan()
    answer(clean, GOAL_TOOL, json.dumps({"status": "complete"}), tool_count=1, successes=1, other_calls=0, failures=[])
    assert clean.claim == "complete" and clean.failures_confirmed is False
