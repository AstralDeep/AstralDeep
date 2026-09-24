"""Check framework for typed, pure, replayable verification assertions: each Check pairs
a run() assertion with an adversarial counter() that tries to falsify a pass. Used by
every module under verification/checks/ and by verification/runner.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from verification.evidence import CapturedEvidence
from verification.verdict import Outcome


@dataclass
class CheckResult:
    check_id: str
    outcome: Outcome
    observed: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "outcome": self.outcome.value if isinstance(self.outcome, Outcome) else self.outcome,
            "observed": self.observed,
            "reason": self.reason,
        }


@dataclass
class Check:
    check_id: str
    property: str
    run_fn: Callable[[CapturedEvidence, Dict[str, Any]], CheckResult]
    counter_fn: Optional[Callable[[CapturedEvidence, Dict[str, Any]], CheckResult]] = None
    is_deterministic: bool = True

    def run(self, evidence: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
        return self.run_fn(evidence, inputs)

    def counter(self, evidence: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
        if self.counter_fn is None:
            # No counter defined counts as FAIL, i.e. did not refute a pass.
            return CheckResult(self.check_id + ".counter", Outcome.FAIL, reason="no counter-check")
        return self.counter_fn(evidence, inputs)

    def counter_refutes(self, evidence: CapturedEvidence, inputs: Dict[str, Any]) -> bool:
        if self.counter_fn is None:
            return False
        return self.counter(evidence, inputs).outcome == Outcome.PASS


_REGISTRY: Dict[str, Check] = {}


def register(check: Check) -> Check:
    _REGISTRY[check.check_id] = check
    return check


def get(check_id: str) -> Optional[Check]:
    return _REGISTRY.get(check_id)


def all_checks() -> List[Check]:
    return list(_REGISTRY.values())


def by_property(prop: str) -> List[Check]:
    return [c for c in _REGISTRY.values() if c.property == prop]


def ok(check_id: str, reason: str = "", **observed: Any) -> CheckResult:
    return CheckResult(check_id, Outcome.PASS, observed=observed, reason=reason)


def no(check_id: str, reason: str = "", **observed: Any) -> CheckResult:
    return CheckResult(check_id, Outcome.FAIL, observed=observed, reason=reason)


def unsure(check_id: str, reason: str = "", **observed: Any) -> CheckResult:
    return CheckResult(check_id, Outcome.UNCERTAIN, observed=observed, reason=reason)
