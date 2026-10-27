"""Integration tests for the bounded optimization run: deterministic search and
selection, hold-out isolation and leakage guards, budget exhaustion, cancellation,
denials, malformed proposals, failure recovery, and the hold-out reuse seal.
"""

from __future__ import annotations

import json
from typing import List, Tuple

import pytest

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io
from prompt_optimization import optimizer as optimizer_module
from prompt_optimization import proposals as proposals_module
from prompt_optimization import task as task_module
from prompt_optimization.fixture import PlannerCase, build_fixture
from prompt_optimization.fixture import render_case_block

REPO_ROOT = task_module.Path(task_module.__file__).resolve().parents[2]

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


@pytest.fixture(autouse=True)
def _fresh_holdout_ledger():
    optimizer_module.reset_consumed_holdouts()
    yield
    optimizer_module.reset_consumed_holdouts()


def _limits(**overrides) -> budget_module.BudgetLimits:
    defaults = dict(
        max_reflection_calls=6,
        max_evaluation_calls=96,
        max_total_tokens=400000,
        max_retries_per_call=1,
        max_output_chars=4000,
        max_candidates_per_round=4,
        max_rounds=4,
    )
    defaults.update(overrides)
    return budget_module.BudgetLimits(**defaults)


def _task_prompt() -> task_module.TaskPrompt:
    return task_module.load_task_prompt(REPO_ROOT)


def _scripted_run(**overrides):
    limits = overrides.pop("limits", None) or _limits(**{
        key: value for key, value in overrides.items() if key in
        ("max_reflection_calls", "max_evaluation_calls", "max_total_tokens",
         "max_retries_per_call", "max_candidates_per_round", "max_rounds")
    })
    fixture = overrides.pop("fixture", None) or build_fixture()
    controls_flag = {"cancelled": False}
    controls = model_io.Controls(lambda: controls_flag["cancelled"])
    variants = proposals_module.scripted_variants(BASELINE_RULES)
    adapter = model_io.ProviderDouble(
        variants=variants, faults=overrides.pop("faults", None),
        usage_mode=overrides.pop("usage_mode", "complete"),
    )
    run = optimizer_module.OptimizationRun(
        fixture=fixture,
        task_prompt=_task_prompt(),
        adapter=adapter,
        limits=limits,
        controls=controls,
        model_identity="scripted:test-double",
    )
    run.proposal_source = proposals_module.ReflectionProposalSource(
        adapter=adapter,
        budget=run.budget,
        controls=controls,
        max_candidates=limits.max_candidates_per_round,
        max_output_chars=limits.max_output_chars,
        max_attempts=limits.max_retries_per_call + 1,
    )
    return run, adapter, controls_flag


def test_deterministic_scripted_run_adopts_improved_candidate():
    first, _, _ = _scripted_run()
    result = first.execute()

    second, _, _ = _scripted_run()
    optimizer_module.reset_consumed_holdouts()
    again = second.execute()

    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is True
    assert result.recommendation == optimizer_module.RECOMMEND_ADOPT
    assert result.selected_digest is not None
    assert result.selected_digest != result.task["baseline_instruction_digest"]
    assert again.selected_digest == result.selected_digest
    assert [c["instruction_digest"] for c in again.candidates] == [
        c["instruction_digest"] for c in result.candidates
    ]
    assert result.baseline_holdout.mean()["combined"] < (
        result.selected_holdout.mean()["combined"]
    )
    assert result.budget["usage_complete"] is True
    assert result.budget["reserved_tokens"] == 0
    assert result.budget["reflection_calls"] >= 1
    assert result.budget["evaluation_calls"] > (
        result.budget["reflection_calls"]
    )
    assert result.next_holdout_required is True


def test_selection_uses_validation_not_train_scores():
    run, _, _ = _scripted_run()
    result = run.execute()
    train_ranked = sorted(
        result.candidates,
        key=lambda c: (-c["train_mean"]["combined"], c["instruction_digest"]),
    )
    selected = next(
        c for c in result.candidates
        if c["instruction_digest"] == result.selected_digest
    )
    best_train = train_ranked[0]
    assert best_train["instruction_digest"] in {
        c["instruction_digest"] for c in result.candidates
    }
    assert selected["validation_mean"]["combined"] >= max(
        c["validation_mean"]["combined"] for c in result.candidates
    )


class RecordingAdapter:
    def __init__(self, inner):
        self.inner = inner
        self.calls: List[Tuple[str, str, List[dict]]] = []

    def complete(self, kind, subject, messages, max_output_chars, max_attempts):
        self.calls.append((kind, subject, messages))
        return self.inner.complete(
            kind, subject, messages, max_output_chars, max_attempts
        )


def test_no_holdout_content_in_reflection_or_selection_traffic():
    canary = "ZZHOLDOUT-CANARY-QQ"
    holdout_cases = (
        PlannerCase(
            case_id="h-canary",
            request=f"Check the schedule for {canary} tomorrow.",
            available_tools=("check_schedule", "list_projects", "fetch_record"),
            min_subtasks=1,
            max_subtasks=1,
            expected_tools=frozenset({"check_schedule"}),
            simple=True,
        ),
    ) + build_fixture().cases("holdout")[1:]
    fixture = build_fixture(holdout=holdout_cases)
    run, _, _ = _scripted_run(fixture=fixture)
    recorder = RecordingAdapter(run.adapter)
    run.adapter = recorder
    run.proposal_source.adapter = recorder
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED

    holdout_ids = {case.case_id for case in fixture.cases("holdout")}
    for kind, subject, messages in recorder.calls:
        if kind == "evaluation" and subject in holdout_ids:
            continue
        assert canary not in json.dumps(messages), (kind, subject)
    assert any(
        kind == "evaluation" and subject in holdout_ids
        for kind, subject, _ in recorder.calls
    )


def test_reflection_feedback_contains_only_train_outputs():
    run, _, _ = _scripted_run()
    source = run.proposal_source
    captured = []
    original = source.reflection_messages

    def spy(baseline, feedback, count):
        captured.append(feedback)
        return original(baseline, feedback, count)

    source.reflection_messages = spy
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert captured
    train_ids = {case.case_id for case in build_fixture().cases("train")}
    validation_ids = {case.case_id for case in build_fixture().cases("validation")}
    for feedback in captured:
        for case_id in validation_ids:
            assert case_id not in feedback
        for case_id in train_ids:
            assert case_id in feedback


def test_negative_result_retains_baseline_and_stays_qualified():
    limits = _limits(max_candidates_per_round=1, max_rounds=1)
    fixture = build_fixture()
    controls = model_io.Controls(lambda: False)
    adapter = model_io.ProviderDouble(variants=())
    run = optimizer_module.OptimizationRun(
        fixture=fixture,
        task_prompt=_task_prompt(),
        adapter=adapter,
        limits=limits,
        controls=controls,
        model_identity="scripted:test-double",
    )
    run.proposal_source = proposals_module.ScriptedProposalSource(())
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is True
    assert result.recommendation == optimizer_module.RECOMMEND_REJECT
    assert result.selected_digest == result.task["baseline_instruction_digest"]
    assert result.candidates == []
    assert result.baseline_holdout.mean() is not None


def test_holdout_reuse_is_refused_without_new_designation():
    first, _, _ = _scripted_run()
    first.execute()
    second, _, _ = _scripted_run()
    with pytest.raises(optimizer_module.HoldoutReuseError):
        second.execute()


def test_new_holdout_digest_permits_further_tuning():
    first, _, _ = _scripted_run()
    first.execute()
    replaced = build_fixture(
        holdout=(
            PlannerCase(
                case_id="h-new",
                request="Fetch the record for the audit.",
                available_tools=("fetch_record", "list_projects"),
                min_subtasks=1,
                max_subtasks=1,
                expected_tools=frozenset({"fetch_record"}),
            ),
            PlannerCase(
                case_id="h-new-2",
                request="Render a chart and build a report.",
                available_tools=("render_chart", "build_report"),
                min_subtasks=2,
                max_subtasks=2,
                expected_tools=frozenset({"render_chart", "build_report"}),
            ),
        )
    )
    second, _, _ = _scripted_run(fixture=replaced)
    result = second.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED


def test_execute_twice_rejected():
    run, _, _ = _scripted_run()
    run.execute()
    with pytest.raises(optimizer_module.RunStateError):
        run.execute()


def test_budget_exhaustion_by_call_cap_stays_visible():
    run, _, _ = _scripted_run(limits=_limits(max_evaluation_calls=3))
    result = run.execute()
    assert result.status == optimizer_module.STATUS_BUDGET_EXHAUSTED
    assert result.qualified is False
    assert result.recommendation == optimizer_module.RECOMMEND_INCONCLUSIVE
    evaluation_records = [
        record for record in result.budget["calls"]
        if record["kind"] == "evaluation"
    ]
    assert len(evaluation_records) == 3
    assert all(record["outcome"] == "succeeded" for record in evaluation_records)
    assert result.baseline_holdout is None
    assert result.next_holdout_required is True


def test_budget_exhaustion_by_token_cap_stays_visible():
    run, _, _ = _scripted_run(limits=_limits(max_total_tokens=200))
    result = run.execute()
    assert result.status == optimizer_module.STATUS_BUDGET_EXHAUSTED
    assert result.qualified is False
    assert result.budget["calls"] == []


def test_denied_evaluation_case_is_recorded_and_blocks_qualified_claim():
    run, _, _ = _scripted_run(
        faults={"evaluation:t-simple-projects": "deny"}
    )
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is False
    assert result.recommendation == optimizer_module.RECOMMEND_INCONCLUSIVE
    denied = [
        c for c in result.baseline_train.case_results if c.outcome == "denied"
    ]
    assert [c.case_id for c in denied] == ["t-simple-projects"]
    assert denied[0].error_class == "policy_denied"
    assert any(
        record.outcome == "denied" for record in run.budget.records
    )


def test_denied_reflection_stops_search_and_blocks_qualified_claim():
    run, _, _ = _scripted_run(faults={"reflection:round-0": "deny"})
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is False
    assert result.recommendation == optimizer_module.RECOMMEND_INCONCLUSIVE
    assert result.proposal_errors
    assert result.candidates == []


def test_malformed_reflection_output_blocks_qualified_claim():
    run, _, _ = _scripted_run(faults={"reflection:round-0": "malformed"})
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is False
    assert result.recommendation == optimizer_module.RECOMMEND_INCONCLUSIVE
    assert any(
        error["error_class"] == "ProposalError"
        for error in result.proposal_errors
    )


def test_malformed_evaluation_output_scores_zero_visibly():
    run, _, _ = _scripted_run(
        faults={"evaluation:t-simple-chart": "malformed"}
    )
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    failed_cases = [
        c for c in result.baseline_train.case_results
        if c.case_id == "t-simple-chart"
    ]
    assert failed_cases[0].outcome == "succeeded"
    assert failed_cases[0].metrics["schema"] == 0.0
    assert "plan_not_json_object" in failed_cases[0].components["schema"]


def test_transient_failure_recovers_within_retry_bound():
    run, _, _ = _scripted_run(
        faults={"evaluation:t-simple-projects": "transient"},
        limits=_limits(max_retries_per_call=1),
    )
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    recovered = next(
        record for record in run.budget.records
        if record.subject == "t-simple-projects"
        and record.phase == "search"
    )
    assert recovered.attempts == 2
    assert recovered.outcome == "succeeded"


def test_unknown_usage_accounting_blocks_qualified_claim():
    run, _, _ = _scripted_run(usage_mode="incomplete")
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is False
    assert result.recommendation == optimizer_module.RECOMMEND_INCONCLUSIVE
    assert result.budget["usage_complete"] is False


def test_cancellation_mid_run_is_visible_and_unqualified():
    state = {"checks": 0}

    def cancelled() -> bool:
        state["checks"] += 1
        return state["checks"] > 9

    run, _, _ = _scripted_run()
    run.controls = model_io.Controls(cancelled)
    run.proposal_source.controls = run.controls
    result = run.execute()
    assert result.status == optimizer_module.STATUS_CANCELLED
    assert result.qualified is False
    assert result.recommendation == optimizer_module.RECOMMEND_INCONCLUSIVE
    assert result.next_holdout_required is True
    assert result.baseline_holdout is None


def test_search_phase_failure_does_not_leak_into_holdout_phase():
    run, _, _ = _scripted_run(
        faults={"evaluation:t-simple-projects": "deny"}
    )
    result = run.execute()
    holdout_cases = result.baseline_holdout.case_results
    assert all(c.outcome == "succeeded" for c in holdout_cases)
    assert all(c.error_class is None for c in holdout_cases)


def test_baseline_train_failure_visible_in_feedback():
    run, _, _ = _scripted_run(
        faults={"evaluation:t-simple-projects": "deny"}
    )
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    feedback = optimizer_module._feedback_text(run._baseline.train)
    assert "t-simple-projects: denied (policy_denied)" in feedback


def test_rendered_case_block_used_for_evaluation():
    run, _, _ = _scripted_run()
    prompt_text = run.task_prompt.render(run.task_prompt.rules)
    messages = model_io.evaluation_messages(prompt_text, build_fixture().cases("train")[0])
    assert render_case_block(build_fixture().cases("train")[0]) == messages[1]["content"]
