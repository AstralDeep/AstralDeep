"""Bounded budget accounting for workbench model calls: a conservative per-call token
reserve admitted before every reflection or evaluation call, post-call reconciliation of
actual usage, and a complete call inventory in which denials, failures, cancellations,
and unknown or incomplete accounting stay visible and can never yield a qualified
success.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional

CALL_KINDS = ("reflection", "evaluation")
OUTCOMES = ("succeeded", "denied", "failed", "cancelled")


class BudgetExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetLimits:
    max_reflection_calls: int
    max_evaluation_calls: int
    max_total_tokens: int
    max_retries_per_call: int = 1
    max_output_chars: int = 4000
    max_candidates_per_round: int = 4
    max_rounds: int = 4
    concurrency: int = 1

    def __post_init__(self) -> None:
        positive = (
            self.max_reflection_calls,
            self.max_evaluation_calls,
            self.max_total_tokens,
            self.max_output_chars,
            self.max_candidates_per_round,
            self.max_rounds,
        )
        if any(type(value) is not int or value < 1 for value in positive):
            raise ValueError("budget limits must be positive integers")
        if type(self.max_retries_per_call) is not int or not 0 <= self.max_retries_per_call <= 2:
            raise ValueError("max_retries_per_call must be between 0 and 2")
        if self.concurrency != 1:
            raise ValueError("workbench concurrency is fixed at 1")

    def to_dict(self) -> dict:
        return {
            "max_reflection_calls": self.max_reflection_calls,
            "max_evaluation_calls": self.max_evaluation_calls,
            "max_total_tokens": self.max_total_tokens,
            "max_retries_per_call": self.max_retries_per_call,
            "max_output_chars": self.max_output_chars,
            "max_candidates_per_round": self.max_candidates_per_round,
            "max_rounds": self.max_rounds,
            "concurrency": self.concurrency,
        }


@dataclass
class CallRecord:
    sequence: int
    kind: str
    phase: str
    subject: str
    outcome: str
    attempts: int
    reserved_tokens: int
    actual_tokens: Optional[int]
    usage_complete: bool
    error_class: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "phase": self.phase,
            "subject": self.subject,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "reserved_tokens": self.reserved_tokens,
            "actual_tokens": self.actual_tokens,
            "usage_complete": self.usage_complete,
            "error_class": self.error_class,
        }


def estimate_tokens(text: str) -> int:
    return len(text or "") // 2 + 64


@dataclass
class _Reservation:
    kind: str
    phase: str
    subject: str
    reserved_tokens: int
    released: bool = False


@dataclass
class Budget:
    limits: BudgetLimits
    reserved_tokens: int = 0
    spent_tokens: int = 0
    reflection_calls: int = 0
    evaluation_calls: int = 0
    usage_complete: bool = True
    records: List[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _sequence: int = 0

    def remaining(self) -> int:
        return self.limits.max_total_tokens - self.reserved_tokens

    def reserve(self, kind: str, phase: str, subject: str, estimated_tokens: int) -> _Reservation:
        if kind not in CALL_KINDS:
            raise ValueError(f"unknown call kind {kind!r}")
        if type(estimated_tokens) is not int or estimated_tokens < 1:
            raise ValueError("estimated tokens must be a positive integer")
        with self._lock:
            calls = (
                self.reflection_calls if kind == "reflection" else self.evaluation_calls
            )
            cap = (
                self.limits.max_reflection_calls
                if kind == "reflection"
                else self.limits.max_evaluation_calls
            )
            if calls >= cap:
                raise BudgetExhausted(
                    f"{kind} call budget exhausted ({calls}/{cap})"
                )
            if self.reserved_tokens + estimated_tokens > self.limits.max_total_tokens:
                raise BudgetExhausted(
                    "token budget exhausted: "
                    f"reserved {self.reserved_tokens}+{estimated_tokens} "
                    f"exceeds {self.limits.max_total_tokens}"
                )
            self.reserved_tokens += estimated_tokens
            self._sequence += 1
            return _Reservation(
                kind=kind,
                phase=phase,
                subject=subject,
                reserved_tokens=estimated_tokens,
            )

    def reconcile(
        self,
        reservation: _Reservation,
        *,
        outcome: str,
        attempts: int,
        actual_tokens: Optional[int],
        usage_complete: bool,
        error_class: Optional[str] = None,
    ) -> CallRecord:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown call outcome {outcome!r}")
        with self._lock:
            if reservation.released:
                raise ValueError("reservation already reconciled")
            reservation.released = True
            self.reserved_tokens -= reservation.reserved_tokens
            charge = (
                actual_tokens
                if type(actual_tokens) is int and actual_tokens >= 0
                else reservation.reserved_tokens
            )
            self.spent_tokens += charge
            if outcome == "succeeded":
                if reservation.kind == "reflection":
                    self.reflection_calls += 1
                else:
                    self.evaluation_calls += 1
            if not usage_complete:
                self.usage_complete = False
            self._sequence += 1
            record = CallRecord(
                sequence=self._sequence,
                kind=reservation.kind,
                phase=reservation.phase,
                subject=reservation.subject,
                outcome=outcome,
                attempts=attempts,
                reserved_tokens=reservation.reserved_tokens,
                actual_tokens=actual_tokens,
                usage_complete=usage_complete,
                error_class=error_class,
            )
            self.records.append(record)
            if self.spent_tokens > self.limits.max_total_tokens:
                raise BudgetExhausted(
                    "token budget exceeded after reconciliation: "
                    f"{self.spent_tokens} > {self.limits.max_total_tokens}"
                )
            return record

    def failures(self) -> List[CallRecord]:
        return [
            record
            for record in self.records
            if record.outcome in ("denied", "failed", "cancelled")
        ]

    def to_dict(self) -> dict:
        return {
            "limits": self.limits.to_dict(),
            "reserved_tokens": self.reserved_tokens,
            "spent_tokens": self.spent_tokens,
            "reflection_calls": self.reflection_calls,
            "evaluation_calls": self.evaluation_calls,
            "usage_complete": self.usage_complete,
            "calls": [record.to_dict() for record in self.records],
        }
