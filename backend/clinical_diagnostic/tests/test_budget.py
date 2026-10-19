"""Tests for clinical_diagnostic.budget: bounded limits, call/elapsed/cost exhaustion,
retry backoff, cancellation, and an injected clock that keeps CI deterministic.
"""

from __future__ import annotations

import threading

import pytest

from clinical_diagnostic.budget import (
    BudgetExhausted,
    BudgetTracker,
    ExecutionLimits,
    RunCancelled,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_limits_validate_every_bound():
    with pytest.raises(ValueError, match="max_cases"):
        ExecutionLimits(max_cases=0)
    with pytest.raises(ValueError, match="max_calls"):
        ExecutionLimits(max_calls=0)
    with pytest.raises(ValueError, match="max_retries"):
        ExecutionLimits(max_retries=9)
    with pytest.raises(ValueError, match="concurrency"):
        ExecutionLimits(concurrency=9)
    with pytest.raises(ValueError, match="finite"):
        ExecutionLimits(max_elapsed_seconds=float("inf"))
    with pytest.raises(ValueError, match="must not exceed"):
        ExecutionLimits(max_elapsed_seconds=4000)
    with pytest.raises(ValueError, match="max_cost"):
        ExecutionLimits(max_cost=-1)
    limits = ExecutionLimits(max_cases=4, max_calls=4, max_retries=0,
                             max_elapsed_seconds=10, max_cost=1.0, concurrency=2)
    assert limits.to_dict()["concurrency"] == 2


def test_call_budget_is_exhausted_without_scoring():
    budget = BudgetTracker(ExecutionLimits(max_calls=1))
    budget.begin_call()
    with pytest.raises(BudgetExhausted, match="call budget"):
        budget.begin_call()
    assert budget.calls == 1


def test_concurrent_call_reservations_cannot_overshoot_the_budget():
    budget = BudgetTracker(ExecutionLimits(max_calls=1))
    barrier = threading.Barrier(2)
    outcomes = []

    def call():
        barrier.wait()
        try:
            budget.begin_call()
            outcomes.append("ok")
        except BudgetExhausted:
            outcomes.append("exhausted")

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["exhausted", "ok"]
    assert budget.calls == 1


def test_elapsed_budget_uses_the_injected_clock():
    clock = FakeClock()
    budget = BudgetTracker(ExecutionLimits(max_elapsed_seconds=5), clock=clock)
    clock.advance(5.0)
    budget.check()
    clock.advance(1.0)
    with pytest.raises(BudgetExhausted, match="elapsed"):
        budget.check()


def test_cost_budget_rejects_missing_and_excessive_cost():
    budget = BudgetTracker(ExecutionLimits(max_cost=0.5))
    budget.record_cost(0.25)
    assert budget.cost == 0.25
    budget.record_cost(0.25)
    with pytest.raises(BudgetExhausted, match="cost budget"):
        budget.record_cost(0.01)
    for value in (-1, float("nan"), True):
        with pytest.raises(ValueError):
            budget.record_cost(value)


def test_retry_backoff_is_bounded_and_uses_the_injected_sleep():
    slept = []
    budget = BudgetTracker(ExecutionLimits(max_retries=3), sleep=slept.append)
    for attempt in range(4):
        budget.retry_delay(attempt)
    assert slept == [0.1, 0.2, 0.4, 0.8]
    with pytest.raises(ValueError, match="non-negative"):
        budget.retry_delay(-1)


def test_cancellation_is_checked_before_calls_and_after_sleep():
    event = threading.Event()
    budget = BudgetTracker(ExecutionLimits(), cancel_event=event)
    budget.request_cancel()
    assert budget.cancelled
    with pytest.raises(RunCancelled):
        budget.check()
    with pytest.raises(RunCancelled):
        budget.begin_call()


def test_retry_delay_observes_cancellation():
    event = threading.Event()
    budget = BudgetTracker(ExecutionLimits(max_retries=1), cancel_event=event,
                           sleep=lambda _: event.set())
    with pytest.raises(RunCancelled):
        budget.retry_delay(1)


def test_limits_reject_non_integer_case_and_call_bounds():
    with pytest.raises(ValueError, match="max_cases must be an integer"):
        ExecutionLimits(max_cases=True)
    with pytest.raises(ValueError, match="max_calls must be an integer"):
        ExecutionLimits(max_calls=1.5)
    with pytest.raises(ValueError, match="max_retries must be an integer"):
        ExecutionLimits(max_retries=True)
    with pytest.raises(ValueError, match="concurrency must be an integer"):
        ExecutionLimits(concurrency=False)
    with pytest.raises(ValueError, match="max_cost must be a finite number"):
        ExecutionLimits(max_cost="1")