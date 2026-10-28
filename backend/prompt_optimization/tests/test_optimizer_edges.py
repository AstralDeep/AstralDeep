"""Edge-path tests for the optimizer: cancellation placement, duplicate candidates,
failed-evaluation partition branches, and a clean-accounting negative holdout result.
"""

from __future__ import annotations

import pytest

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io
from prompt_optimization import optimizer as optimizer_module
from prompt_optimization.tests.support import holdout_seals, reset_holdouts
from prompt_optimization import proposals as proposals_module
from prompt_optimization import task as task_module
from prompt_optimization.fixture import build_fixture
from prompt_optimization.optimizer import CaseResult, PartitionResult

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


@pytest.fixture(autouse=True)
def _fresh_ledger():
    reset_holdouts()
    yield
    reset_holdouts()


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


def test_partition_mean_is_none_when_every_case_failed():
    partition = PartitionResult(
        partition="train",
        case_results=[
            CaseResult("t-a", "denied", None, None, "", "policy_denied"),
            CaseResult("t-b", "failed", None, None, "", "transient_timeout"),
        ],
    )
    assert partition.mean() is None
    assert partition.error_count() == 2
    assert partition.to_dict()["mean"] is None


def test_feedback_text_handles_missing_partition():
    assert optimizer_module._feedback_text(None) == (
        "no train evaluation completed yet"
    )


def _build_run(controls, *, fixture=None, adapter=None, source=None, limits=None):
    adapter = adapter or model_io.ProviderDouble(
        variants=proposals_module.scripted_variants(BASELINE_RULES)
    )
    run = optimizer_module.OptimizationRun(
        holdout_seals=holdout_seals,
        fixture=fixture or build_fixture(),
        task_prompt=_task_prompt(),
        adapter=adapter,
        limits=limits or _limits(),
        controls=controls,
        model_identity="scripted:test-double",
    )
    run.proposal_source = source or proposals_module.ReflectionProposalSource(
        adapter=adapter,
        budget=run.budget,
        controls=controls,
        max_candidates=limits.max_candidates_per_round if limits else 4,
        max_output_chars=4000,
        max_attempts=2,
    )
    return run


def test_cancellation_at_round_start_is_visible():
    state = {"checks": 0}

    def cancelled() -> bool:
        state["checks"] += 1
        return state["checks"] > 10

    run = _build_run(model_io.Controls(cancelled))
    result = run.execute()
    assert result.status == optimizer_module.STATUS_CANCELLED
    assert result.baseline_train is not None
    assert result.candidates == []
    assert result.baseline_holdout is None


def test_cancellation_during_selection_evaluation_is_visible():
    state = {"checks": 0}

    def cancelled() -> bool:
        state["checks"] += 1
        return state["checks"] > 11

    run = _build_run(
        model_io.Controls(cancelled),
        source=proposals_module.ScriptedProposalSource(
            proposals_module.scripted_variants(BASELINE_RULES)
        ),
    )
    result = run.execute()
    assert result.status == optimizer_module.STATUS_CANCELLED
    assert result.qualified is False
    assert result.baseline_holdout is None
    assert result.candidates == []


def test_duplicate_reflection_candidates_are_deduplicated():
    variants = proposals_module.scripted_variants(BASELINE_RULES)
    duplicated = variants + variants
    adapter = model_io.ProviderDouble(variants=duplicated)
    controls = model_io.Controls(lambda: False)
    run = _build_run(controls, adapter=adapter)
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    digests = [c["instruction_digest"] for c in result.candidates]
    assert len(digests) == len(set(digests)) == 4


def test_failed_evaluation_partition_branch_records_error():
    controls = model_io.Controls(lambda: False)
    limits = _limits(max_retries_per_call=0)
    adapter = model_io.ProviderDouble(
        variants=proposals_module.scripted_variants(BASELINE_RULES),
        faults={"evaluation:t-simple-chart": "transient"},
    )
    run = _build_run(controls, adapter=adapter, limits=limits)
    run.proposal_source.max_attempts = 1
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    failed = [
        c for c in result.baseline_train.case_results
        if c.case_id == "t-simple-chart"
    ]
    assert failed[0].outcome == "failed"
    assert failed[0].metrics is None
    assert failed[0].error_class == "transient_timeout"
    assert result.qualified is False


def test_unclassified_provider_failure_falls_back_to_exception_name():
    class _OpaqueFailAdapter:
        def complete(self, kind, subject, messages, max_output_chars, max_attempts):
            return model_io.ModelCallResult(
                "failed", None, 1, None, False, None
            )

    controls = model_io.Controls(lambda: False)
    run = _build_run(
        controls,
        adapter=_OpaqueFailAdapter(),
        source=proposals_module.ScriptedProposalSource(()),
        limits=_limits(max_total_tokens=2000000),
    )
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    failed = [
        c for c in result.baseline_train.case_results if c.outcome == "failed"
    ]
    assert failed
    assert failed[0].error_class == "ProviderFailed"
    assert result.qualified is False


def test_all_validation_denials_leave_selection_empty():
    controls = model_io.Controls(lambda: False)
    fixture = build_fixture()
    faults = {f"evaluation:{case.case_id}": "deny"
              for case in fixture.cases("validation")}
    adapter = model_io.ProviderDouble(
        variants=proposals_module.scripted_variants(BASELINE_RULES),
        faults=faults,
    )
    run = _build_run(controls, adapter=adapter)
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is False
    assert result.recommendation == optimizer_module.RECOMMEND_INCONCLUSIVE
    assert result.selected_digest is None


def test_clean_negative_holdout_result_rejects_candidate():
    degraded_dependency = BASELINE_RULES.replace("depends_on", "ordering") + (
        "- Honor explicit exclusions: never hint an excluded tool.\n"
    )
    controls = model_io.Controls(lambda: False)
    run = _build_run(
        controls,
        source=proposals_module.ScriptedProposalSource((degraded_dependency,)),
    )
    result = run.execute()
    assert result.status == optimizer_module.STATUS_COMPLETED
    assert result.qualified is True
    assert result.recommendation == optimizer_module.RECOMMEND_REJECT
    assert "retain the baseline instruction" in result.recommendation_basis
    selected = next(
        c for c in result.candidates
        if c["instruction_digest"] == result.selected_digest
    )
    assert (
        selected["validation_mean"]["combined"]
        > result.baseline_validation.mean()["combined"]
    )
    assert (
        result.selected_holdout.mean()["combined"]
        <= result.baseline_holdout.mean()["combined"]
    )
