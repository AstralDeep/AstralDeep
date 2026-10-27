"""Tests for model I/O: scripted double plan behaviors keyed on instruction rules,
fault injection, bounded completion with reserve-then-reconcile, cancellation, and
live-adapter configuration fail-closed paths (no network use).
"""

from __future__ import annotations

import json

import pytest

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io
from prompt_optimization.fixture import PlannerCase


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


def _case(**overrides) -> PlannerCase:
    defaults = dict(
        case_id="t-x",
        request="List the projects for the Atlas team and build one combined report.",
        available_tools=("list_projects", "build_report", "fetch_record"),
        min_subtasks=2,
        max_subtasks=2,
        expected_tools=frozenset({"list_projects", "build_report"}),
    )
    defaults.update(overrides)
    return PlannerCase(**defaults)


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


def _run_double(rules: str, case: PlannerCase, **double_kwargs):
    double = model_io.ProviderDouble(**double_kwargs)
    messages = model_io.evaluation_messages(HEAD + rules, case)
    result = double.complete("evaluation", case.case_id, messages, 4000, 2)
    assert result.outcome == "succeeded"
    assert result.usage_complete
    return json.loads(result.text)


def test_double_single_subtask_rule():
    simple = _case(
        case_id="t-simple",
        request="List the projects for Atlas.",
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"list_projects"}),
        simple=True,
    )
    plan = _run_double(BASELINE_RULES, simple)
    assert len(plan["subtasks"]) == 1
    assert plan["subtasks"][0]["tool_hints"] == ["list_projects"]
    assert plan["synthesis"] == ""


def test_double_without_single_rule_splits_simple_request():
    rules = BASELINE_RULES.replace(
        "- If the request is simple enough for a single tool call, "
        "return a single subtask.\n",
        "",
    )
    simple = _case(
        case_id="t-simple",
        request="List the projects for Atlas.",
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"list_projects"}),
        simple=True,
    )
    plan = _run_double(rules, simple)
    assert len(plan["subtasks"]) == 2


def test_double_respects_instruction_cap():
    rules = BASELINE_RULES + "- Use at most 3 subtasks for any request.\n"
    broad = _case()
    plan = _run_double(rules, broad)
    assert len(plan["subtasks"]) == 2


def test_double_uncapped_defaults_to_four():
    broad = _case(
        request=(
            "Audit the catalog, list the projects, fetch the record, "
            "render the chart, check the schedule, and build the report."
        ),
        available_tools=(
            "list_projects",
            "build_report",
            "fetch_record",
            "render_chart",
            "check_schedule",
            "search_catalog",
        ),
    )
    plan = _run_double(BASELINE_RULES, broad)
    assert len(plan["subtasks"]) == 4


def test_double_grounding_rule():
    broad = _case()
    plan = _run_double(BASELINE_RULES, broad)
    hints = {hint for entry in plan["subtasks"] for hint in entry["tool_hints"]}
    assert hints <= set(broad.available_tools)

    rules = BASELINE_RULES.replace(
        "- tool_hints should reference actual tool names from the available "
        "tools list.\n",
        "- Prefer broadly relevant tool hints for each subtask.\n",
    )
    degraded = _run_double(rules, broad)
    degraded_hints = {
        hint for entry in degraded["subtasks"] for hint in entry["tool_hints"]
    }
    assert "list_projects_pro" in degraded_hints


def test_double_dependency_rule():
    pair = _case(
        case_id="t-pair",
        request="Fetch the record and build a report from it.",
        available_tools=("fetch_record", "build_report"),
        expected_tools=frozenset({"fetch_record", "build_report"}),
        expect_dependency=True,
    )
    plan = _run_double(BASELINE_RULES, pair)
    assert plan["subtasks"][-1]["depends_on"] == ["st_1"]

    rules = BASELINE_RULES.replace("depends_on", "ordering")
    no_dep = _run_double(rules, pair)
    assert all(entry["depends_on"] == [] for entry in no_dep["subtasks"])


def test_double_synthesis_rule():
    rules = BASELINE_RULES + (
        "- Always include a synthesis field describing how subtask results "
        "combine.\n"
    )
    plan = _run_double(rules, _case())
    assert plan["synthesis"] == "Combine the subtask results into one answer."


def test_double_honors_exclusions_only_when_instructed():
    case = _case(
        case_id="t-exclude",
        request=(
            "List the projects and build a report, "
            "but do not build any report twice."
        ),
    )
    instructed = BASELINE_RULES + (
        "- Honor explicit exclusions: never hint an excluded tool.\n"
    )
    plan = _run_double(instructed, case)
    hints = {hint for entry in plan["subtasks"] for hint in entry["tool_hints"]}
    assert "build_report" not in hints

    plan = _run_double(BASELINE_RULES, case)
    hints = {hint for entry in plan["subtasks"] for hint in entry["tool_hints"]}
    assert "build_report" in hints


def test_double_reflection_emits_variant_batches():
    variants = ("v-one", "v-two", "v-three")
    double = model_io.ProviderDouble(variants=variants)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    first = json.loads(
        double.complete("reflection", "round-0", messages, 4000, 1).text
    )
    second = json.loads(
        double.complete("reflection", "round-1", messages, 4000, 1).text
    )
    third = json.loads(
        double.complete("reflection", "round-2", messages, 4000, 1).text
    )
    assert first["candidates"] == ["v-one", "v-two"]
    assert second["candidates"] == ["v-three"]
    assert third["candidates"] == []


def test_double_fault_malformed_output():
    double = model_io.ProviderDouble(faults={"evaluation:t-x": "malformed"})
    result = double.complete(
        "evaluation", "t-x",
        model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()), 4000, 2,
    )
    assert result.outcome == "succeeded"
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.text)
    assert result.text.startswith('{"subtasks"')


def test_double_fault_deny():
    double = model_io.ProviderDouble(faults={"evaluation:t-x": "deny"})
    result = double.complete(
        "evaluation", "t-x",
        model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()), 4000, 2,
    )
    assert result.outcome == "denied"
    assert result.error_class == "policy_denied"


def test_double_fault_empty_output():
    double = model_io.ProviderDouble(faults={"evaluation:t-x": "empty"})
    result = double.complete(
        "evaluation", "t-x",
        model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()), 4000, 2,
    )
    assert result.text == ""


def test_double_fault_transient_recovers_with_retry():
    double = model_io.ProviderDouble(faults={"evaluation:t-x": "transient"})
    result = double.complete(
        "evaluation", "t-x",
        model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()), 4000, 2,
    )
    assert result.outcome == "succeeded"
    assert result.attempts == 2

    strict = model_io.ProviderDouble(faults={"evaluation:t-x": "transient"})
    failed = strict.complete(
        "evaluation", "t-x",
        model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()), 4000, 1,
    )
    assert failed.outcome == "failed"
    assert failed.error_class == "transient_timeout"


def test_double_usage_incomplete_mode():
    double = model_io.ProviderDouble(usage_mode="incomplete")
    result = double.complete(
        "evaluation", "t-x",
        model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()), 4000, 2,
    )
    assert result.total_tokens is None
    assert result.usage_complete is False


def test_double_respects_output_char_bound():
    double = model_io.ProviderDouble()
    result = double.complete(
        "evaluation", "t-x",
        model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()), 30, 1,
    )
    assert len(result.text) <= 30


def test_evaluation_messages_shape():
    case = _case()
    messages = model_io.evaluation_messages("SYSTEM PROMPT", case)
    assert messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert "[available tools]" in messages[1]["content"]
    assert "[request]" in messages[1]["content"]
    assert case.request in messages[1]["content"]


def test_bounded_complete_success_reconciles():
    budget = budget_module.Budget(_limits())
    double = model_io.ProviderDouble()
    text, record = model_io.bounded_complete(
        budget,
        double,
        kind="evaluation",
        phase="search",
        subject="t-x",
        messages=model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()),
        controls=model_io.Controls(lambda: False),
        max_output_chars=4000,
        max_attempts=2,
    )
    assert text
    assert record.outcome == "succeeded"
    assert budget.reserved_tokens == 0
    assert budget.spent_tokens > 0


def test_bounded_complete_denied_raises_with_record():
    budget = budget_module.Budget(_limits())
    double = model_io.ProviderDouble(faults={"evaluation:t-x": "deny"})
    with pytest.raises(model_io.ProviderDenied) as excinfo:
        model_io.bounded_complete(
            budget,
            double,
            kind="evaluation",
            phase="search",
            subject="t-x",
            messages=model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()),
            controls=model_io.Controls(lambda: False),
            max_output_chars=4000,
            max_attempts=1,
        )
    assert excinfo.value.record.outcome == "denied"
    assert budget.reserved_tokens == 0


def test_bounded_complete_failed_raises_with_record():
    budget = budget_module.Budget(_limits())
    double = model_io.ProviderDouble(faults={"evaluation:t-x": "transient"})
    with pytest.raises(model_io.ProviderFailed) as excinfo:
        model_io.bounded_complete(
            budget,
            double,
            kind="evaluation",
            phase="search",
            subject="t-x",
            messages=model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()),
            controls=model_io.Controls(lambda: False),
            max_output_chars=4000,
            max_attempts=1,
        )
    assert excinfo.value.record.error_class == "transient_timeout"
    assert excinfo.value.record.usage_complete is False


def test_bounded_complete_cancellation_before_call():
    budget = budget_module.Budget(_limits())
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 0

    with pytest.raises(model_io.RunCancelled):
        model_io.bounded_complete(
            budget,
            model_io.ProviderDouble(),
            kind="evaluation",
            phase="search",
            subject="t-x",
            messages=model_io.evaluation_messages(HEAD + BASELINE_RULES, _case()),
            controls=model_io.Controls(cancelled),
            max_output_chars=4000,
            max_attempts=1,
        )
    assert budget.records[-1].outcome == "cancelled"


def test_live_completion_rejects_invalid_config(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"model": "m"}', encoding="utf-8")
    with pytest.raises(ValueError):
        model_io.LiveCompletion(str(bad))

    not_json = tmp_path / "not.json"
    not_json.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        model_io.LiveCompletion(str(not_json))


def test_live_completion_config_fields(tmp_path):
    config = tmp_path / "live.json"
    config.write_text(
        json.dumps({"base_url": "http://127.0.0.1:9999/v1", "model": "synthetic"}),
        encoding="utf-8",
    )
    live = model_io.LiveCompletion(str(config))
    assert live.base_url == "http://127.0.0.1:9999/v1"
    assert live.model == "synthetic"
    assert live.api_key == ""
