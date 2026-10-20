"""Bounds one diagnostic run's cases, provider and grader calls, retries, elapsed time,
cost, concurrency, and cancellation with an injectable monotonic clock so tests stay
deterministic; every exhausted limit raises instead of yielding a zero score.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from clinical_diagnostic.models import MAX_CASES, FailureCode

MAX_CALLS = 10_000
MAX_RETRIES = 8
MAX_CONCURRENCY = 8
MAX_ELAPSED_SECONDS = 3600.0
MAX_COST = 1000.0


class Clock(Protocol):
    def __call__(self) -> float:
        ...


@dataclass(frozen=True)
class ExecutionLimits:
    max_cases: int = MAX_CASES
    max_calls: int = 400
    max_retries: int = 2
    max_elapsed_seconds: float = 900.0
    max_cost: float = 5.0
    concurrency: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.max_cases, bool) or not isinstance(self.max_cases, int):
            raise ValueError("max_cases must be an integer")
        if not 1 <= self.max_cases <= MAX_CASES:
            raise ValueError(f"max_cases must be within 1..{MAX_CASES}")
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int):
            raise ValueError("max_calls must be an integer")
        if not 1 <= self.max_calls <= MAX_CALLS:
            raise ValueError(f"max_calls must be within 1..{MAX_CALLS}")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise ValueError("max_retries must be an integer")
        if not 0 <= self.max_retries <= MAX_RETRIES:
            raise ValueError(f"max_retries must be within 0..{MAX_RETRIES}")
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int):
            raise ValueError("concurrency must be an integer")
        if not 1 <= self.concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"concurrency must be within 1..{MAX_CONCURRENCY}")
        for name, value in (
            ("max_elapsed_seconds", self.max_elapsed_seconds),
            ("max_cost", self.max_cost),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite number")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_elapsed_seconds > MAX_ELAPSED_SECONDS:
            raise ValueError(f"max_elapsed_seconds must not exceed {MAX_ELAPSED_SECONDS}")
        if self.max_cost > MAX_COST:
            raise ValueError(f"max_cost must not exceed {MAX_COST}")

    def to_dict(self) -> dict:
        return {
            "max_cases": self.max_cases,
            "max_calls": self.max_calls,
            "max_retries": self.max_retries,
            "max_elapsed_seconds": float(self.max_elapsed_seconds),
            "max_cost": float(self.max_cost),
            "concurrency": self.concurrency,
        }


class BudgetExhausted(RuntimeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = FailureCode.BUDGET_EXHAUSTED
        self.detail = detail


class RunCancelled(RuntimeError):
    def __init__(self, detail: str = "run cancelled") -> None:
        super().__init__(detail)
        self.code = FailureCode.CANCELLED
        self.detail = detail


class BudgetTracker:
    def __init__(
        self,
        limits: ExecutionLimits,
        *,
        clock: Optional[Clock] = None,
        sleep: Optional[Callable[[float], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.limits = limits
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._cancel = cancel_event or threading.Event()
        self._lock = threading.Lock()
        self._started = self._clock()
        self._calls = 0
        self._cost = 0.0

    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    @property
    def cost(self) -> float:
        with self._lock:
            return self._cost

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started)

    def _check_completion(self) -> None:
        if self._cancel.is_set():
            raise RunCancelled()
        if self.elapsed_seconds > self.limits.max_elapsed_seconds:
            raise BudgetExhausted("elapsed budget exhausted")
        with self._lock:
            if self._cost > self.limits.max_cost:
                raise BudgetExhausted("cost budget exhausted")

    def check(self) -> None:
        self._check_completion()
        with self._lock:
            if self._calls >= self.limits.max_calls:
                raise BudgetExhausted("call budget exhausted")

    def begin_call(self) -> None:
        if self._cancel.is_set():
            raise RunCancelled()
        with self._lock:
            if self.elapsed_seconds > self.limits.max_elapsed_seconds:
                raise BudgetExhausted("elapsed budget exhausted")
            if self._calls >= self.limits.max_calls:
                raise BudgetExhausted("call budget exhausted")
            if self._cost > self.limits.max_cost:
                raise BudgetExhausted("cost budget exhausted")
            self._calls += 1

    def record_cost(self, cost: float) -> None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ValueError("call cost must be a finite number")
        if not math.isfinite(float(cost)) or float(cost) < 0:
            raise ValueError("call cost must be finite and non-negative")
        with self._lock:
            self._cost += float(cost)
        self._check_completion()

    def retry_delay(self, attempt: int) -> None:
        if attempt < 0:
            raise ValueError("retry attempt must be non-negative")
        self.check()
        self._sleep(min(2.0, 0.1 * (2 ** min(attempt, 5))))
        self.check()