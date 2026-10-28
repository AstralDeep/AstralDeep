"""Tests for budget accounting: conservative reserve-before-call admission, post-call
reconciliation, exhausted call and token budgets, complete failure inventory, and
incomplete-usage visibility.
"""

from __future__ import annotations

import pytest

from prompt_optimization.budget import (
    Budget,
    BudgetExhausted,
    BudgetLimits,
    CallRecord,
    estimate_tokens,
)


def _limits(**overrides) -> BudgetLimits:
    defaults = dict(
        max_reflection_calls=2,
        max_evaluation_calls=3,
        max_total_tokens=1000,
        max_retries_per_call=1,
        max_output_chars=4000,
        max_candidates_per_round=2,
        max_rounds=2,
    )
    defaults.update(overrides)
    return BudgetLimits(**defaults)


def test_limits_validation():
    with pytest.raises(ValueError):
        BudgetLimits(max_reflection_calls=0, max_evaluation_calls=1, max_total_tokens=1)
    with pytest.raises(ValueError):
        BudgetLimits(max_reflection_calls=1, max_evaluation_calls=1, max_total_tokens=-5)
    with pytest.raises(ValueError):
        _limits(max_retries_per_call=3)
    with pytest.raises(ValueError):
        _limits(concurrency=4)
    assert _limits().to_dict()["concurrency"] == 1


def test_estimate_tokens_is_conservative_and_deterministic():
    assert estimate_tokens("") == 64
    assert estimate_tokens("x" * 100) == 64 + 100
    assert estimate_tokens("x" * 100) == estimate_tokens("x" * 100)


def test_reserve_and_reconcile_round_trip():
    budget = Budget(_limits())
    reservation = budget.reserve("evaluation", "search", "case-1", 100)
    assert budget.reserved_tokens == 100
    record = budget.reconcile(
        reservation,
        outcome="succeeded",
        attempts=1,
        actual_tokens=80,
        usage_complete=True,
    )
    assert budget.reserved_tokens == 0
    assert budget.spent_tokens == 80
    assert budget.evaluation_calls == 1
    assert isinstance(record, CallRecord)
    assert record.to_dict()["actual_tokens"] == 80


def test_reconcile_without_usage_charges_reservation():
    budget = Budget(_limits())
    reservation = budget.reserve("reflection", "search", "round-0", 120)
    budget.reconcile(
        reservation,
        outcome="failed",
        attempts=1,
        actual_tokens=None,
        usage_complete=False,
        error_class="transient_timeout",
    )
    assert budget.spent_tokens == 120
    assert budget.usage_complete is False
    assert [f.kind for f in budget.failures()] == ["reflection"]


def test_reflection_call_cap_exhausts():
    budget = Budget(_limits(max_reflection_calls=1))
    first = budget.reserve("reflection", "search", "r0", 10)
    budget.reconcile(first, outcome="succeeded", attempts=1, actual_tokens=10,
                     usage_complete=True)
    with pytest.raises(BudgetExhausted) as excinfo:
        budget.reserve("reflection", "search", "r1", 10)
    assert "reflection call budget exhausted" in str(excinfo.value)


def test_evaluation_call_cap_exhausts():
    budget = Budget(_limits(max_evaluation_calls=1))
    first = budget.reserve("evaluation", "search", "c0", 10)
    budget.reconcile(first, outcome="succeeded", attempts=1, actual_tokens=10,
                     usage_complete=True)
    with pytest.raises(BudgetExhausted):
        budget.reserve("evaluation", "search", "c1", 10)


def test_denied_calls_consume_call_cap_and_stay_visible():
    budget = Budget(_limits(max_evaluation_calls=1))
    reservation = budget.reserve("evaluation", "search", "c0", 10)
    budget.reconcile(
        reservation,
        outcome="denied",
        attempts=0,
        actual_tokens=None,
        usage_complete=True,
        error_class="policy_denied",
    )
    assert budget.evaluation_calls == 1
    with pytest.raises(BudgetExhausted):
        budget.reserve("evaluation", "search", "c1", 10)
    assert len(budget.failures()) == 1


def test_token_budget_exhausts_on_reserve():
    budget = Budget(_limits(max_total_tokens=100))
    with pytest.raises(BudgetExhausted) as excinfo:
        budget.reserve("evaluation", "search", "case-1", 150)
    assert "token budget exhausted" in str(excinfo.value)


def test_pending_reservations_count_against_token_budget():
    budget = Budget(_limits(max_total_tokens=150))
    budget.reserve("evaluation", "search", "c0", 100)
    with pytest.raises(BudgetExhausted):
        budget.reserve("evaluation", "search", "c1", 60)
    assert budget.remaining() == 50


def test_overage_after_reconcile_raises():
    budget = Budget(_limits(max_total_tokens=100))
    reservation = budget.reserve("evaluation", "search", "c0", 80)
    with pytest.raises(BudgetExhausted) as excinfo:
        budget.reconcile(
            reservation,
            outcome="succeeded",
            attempts=1,
            actual_tokens=120,
            usage_complete=True,
        )
    assert "exceeded after reconciliation" in str(excinfo.value)


def test_double_reconcile_rejected():
    budget = Budget(_limits())
    reservation = budget.reserve("evaluation", "search", "c0", 10)
    budget.reconcile(reservation, outcome="succeeded", attempts=1, actual_tokens=5,
                     usage_complete=True)
    with pytest.raises(ValueError):
        budget.reconcile(reservation, outcome="succeeded", attempts=1,
                         actual_tokens=5, usage_complete=True)


def test_unknown_kind_and_outcome_rejected():
    budget = Budget(_limits())
    with pytest.raises(ValueError):
        budget.reserve("summarization", "search", "c0", 10)
    reservation = budget.reserve("evaluation", "search", "c0", 10)
    with pytest.raises(ValueError):
        budget.reconcile(reservation, outcome="mysteriously-fine", attempts=1,
                         actual_tokens=1, usage_complete=True)


def test_invalid_estimate_rejected():
    budget = Budget(_limits())
    with pytest.raises(ValueError):
        budget.reserve("evaluation", "search", "c0", 0)


def test_call_inventory_complete():
    budget = Budget(_limits())
    reservation = budget.reserve("evaluation", "search", "c0", 20)
    budget.reconcile(reservation, outcome="succeeded", attempts=1, actual_tokens=18,
                     usage_complete=True)
    payload = budget.to_dict()
    assert len(payload["calls"]) == 1
    record = payload["calls"][0]
    assert record["kind"] == "evaluation"
    assert record["phase"] == "search"
    assert record["subject"] == "c0"
    assert record["outcome"] == "succeeded"
    assert record["attempts"] == 1
    assert record["reserved_tokens"] == 20
    assert record["actual_tokens"] == 18
    assert record["usage_complete"] is True
    assert payload["usage_complete"] is True
    assert payload["reflection_calls"] == 0
    assert payload["evaluation_calls"] == 1


def test_usage_incomplete_marks_budget():
    budget = Budget(_limits())
    reservation = budget.reserve("evaluation", "search", "c0", 20)
    budget.reconcile(reservation, outcome="succeeded", attempts=1, actual_tokens=None,
                     usage_complete=False)
    assert budget.usage_complete is False


def test_spent_tokens_stay_in_admission_after_reconciliation():
    budget = Budget(_limits(max_total_tokens=100))
    first = budget.reserve("evaluation", "search", "first", 90)
    budget.reconcile(first, outcome="succeeded", attempts=1, actual_tokens=90,
                     usage_complete=True)
    assert budget.remaining() == 10
    with pytest.raises(BudgetExhausted):
        budget.reserve("evaluation", "search", "second", 90)


@pytest.mark.parametrize("outcome", ["failed", "denied", "cancelled"])
def test_every_admitted_attempt_consumes_call_cap(outcome):
    budget = Budget(_limits(max_evaluation_calls=1))
    first = budget.reserve("evaluation", "search", "first", 10)
    budget.reconcile(first, outcome=outcome, attempts=1, actual_tokens=0,
                     usage_complete=True)
    with pytest.raises(BudgetExhausted):
        budget.reserve("evaluation", "search", "second", 10)


def test_outstanding_attempts_consume_call_cap_before_reconciliation():
    budget = Budget(_limits(max_evaluation_calls=1))
    budget.reserve("evaluation", "search", "first", 10)
    with pytest.raises(BudgetExhausted):
        budget.reserve("evaluation", "search", "second", 10)


def test_incomplete_usage_cannot_release_conservative_charge():
    budget = Budget(_limits(max_total_tokens=100))
    first = budget.reserve("evaluation", "search", "first", 90)
    budget.reconcile(first, outcome="succeeded", attempts=1, actual_tokens=1,
                     usage_complete=False)
    assert budget.spent_tokens == 90
    assert budget.usage_complete is False
    with pytest.raises(BudgetExhausted):
        budget.reserve("evaluation", "search", "second", 20)


def test_a_single_reservation_cannot_hide_multiple_attempts():
    budget = Budget(_limits())
    reservation = budget.reserve("evaluation", "search", "first", 100)
    with pytest.raises(ValueError, match="single attempt"):
        budget.reconcile(reservation, outcome="succeeded", attempts=2,
                         actual_tokens=10, usage_complete=True)
    assert budget.reserved_tokens == 100
    assert budget.evaluation_calls == 1
