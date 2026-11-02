"""Unit coverage for the goal loop's budget steering text (orchestrator/goal_execution.py)."""

from orchestrator.goal_execution import REPEATED_FAILURES_REASON, budget_notice, budget_summary_prompt, summary_worthy


def test_budget_notice_fires_at_a_quarter_remaining_and_at_two_turns_left():
    assert [turn for turn in range(1, 9) if budget_notice(turn, 8)] == [4, 6]
    assert [turn for turn in range(1, 129) if budget_notice(turn, 128)] == [96, 126]
    assert [turn for turn in range(1, 257) if budget_notice(turn, 256)] == [192, 254]
    assert budget_notice(96, 128).startswith("Execution budget: 32 of 128 turns remain.")


def test_small_budgets_and_exhausted_budgets_get_no_notice():
    assert all(budget_notice(turn, 7) is None for turn in range(1, 8))
    assert budget_notice(8, 8) is None
    assert budget_notice(9, 8) is None


def test_summary_prompt_names_the_budget_and_forbids_tools():
    prompt = budget_summary_prompt(256)
    assert "budget of 256 turns is exhausted" in prompt
    assert prompt.endswith("Do not call tools.")


def test_summary_prompt_names_an_early_stop_reason_instead_of_the_budget():
    prompt = budget_summary_prompt(128, "Repeated tool failures prevented verified completion.")
    assert prompt.startswith("The run stopped early: Repeated tool failures prevented verified completion. and no")
    assert "budget" not in prompt


def test_only_budget_exhaustion_and_repeated_failures_get_a_summary():
    assert summary_worthy(None, 3)
    assert summary_worthy(REPEATED_FAILURES_REASON, 1)
    assert not summary_worthy(None, 0)
    assert not summary_worthy("Waiting for approval of the local computer session.", 5)
    assert not summary_worthy("The model repeatedly selected a different executor for the local project.", 5)
