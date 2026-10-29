"""Exercises degenerate scripted inputs and bounded-wrapper failure classification.
The workbench has no live provider adapter.
"""

from __future__ import annotations

import json

import pytest

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io
from prompt_optimization.fixture import PlannerCase

BASELINE_RULES = (
    '- Use "parallel" type when subtasks are independent.\n'
    '- Use "sequential" type and specify "depends_on" when a subtask needs '
    "results from another.\n"
    "- Each subtask should map to 1-2 tool calls.\n"
    "- If the request is simple enough for a single tool call, return a single "
    "subtask.\n"
    "- tool_hints should reference actual tool names from the available tools "
    "list.\n"
)

HEAD = "You are a task planner.\nReturn a JSON object.\n\nRules:\n"


def _limits(**overrides) -> budget_module.BudgetLimits:
    defaults = dict(
        max_reflection_calls=8,
        max_evaluation_calls=64,
        max_total_tokens=200000,
        max_retries_per_call=1,
        max_output_chars=4000,
    )
    defaults.update(overrides)
    return budget_module.BudgetLimits(**defaults)


def _case() -> PlannerCase:
    return PlannerCase(
        case_id="t-x",
        request="List the projects for the Atlas team and build one combined report.",
        available_tools=("list_projects", "build_report", "fetch_record"),
        min_subtasks=2,
        max_subtasks=2,
        expected_tools=frozenset({"list_projects", "build_report"}),
    )


def test_double_degenerate_user_content_produces_bounded_plan():
    double = model_io.ProviderDouble()
    messages = [
        {"role": "system", "content": HEAD + BASELINE_RULES},
        {"role": "user", "content": "no case markers here"},
    ]
    result = double.complete("evaluation", "t-x", messages, 4000, 1)
    plan = json.loads(result.text)
    assert len(plan["subtasks"]) == 1
    assert plan["subtasks"][0]["tool_hints"] == ["unlisted_tool"]


def test_bounded_complete_classifies_unknown_error_class():
    class _FailingAdapter:
        def complete(self, kind, subject, messages, max_output_chars, max_attempts):
            return model_io.ModelCallResult(
                "failed", None, 1, None, False, None
            )

    budget = budget_module.Budget(_limits())
    with pytest.raises(model_io.ProviderFailed) as excinfo:
        model_io.bounded_complete(
            budget,
            _FailingAdapter(),
            kind="evaluation",
            phase="search",
            subject="t-x",
            messages=[{"role": "user", "content": "hi"}],
            controls=model_io.Controls(lambda: False),
            max_output_chars=4000,
            max_attempts=1,
        )
    assert excinfo.value.record.error_class is None
