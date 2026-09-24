"""Verdict reconciliation between a deterministic check, its adversarial counter, and an
optional LLM judge (backend/verification/checks/base.py, llm_judge.py): a pass
requires the counter to not refute and the judge to agree or abstain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"


JUDGE_NA = "na"


@dataclass
class Verdict:
    verdict_id: str
    scope: str
    outcome: Outcome
    run_mode: str
    confidence: str = "high"
    evidence_ref: Optional[str] = None
    refs: Dict[str, Any] = field(default_factory=dict)
    adversarial: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict_id": self.verdict_id,
            "scope": self.scope,
            "outcome": self.outcome.value if isinstance(self.outcome, Outcome) else self.outcome,
            "confidence": self.confidence,
            "evidence_ref": self.evidence_ref,
            "refs": self.refs,
            "run_mode": self.run_mode,
            "adversarial": self.adversarial,
            "reason": self.reason,
        }


def reconcile(
    deterministic: Outcome,
    counter_refuted: bool,
    llm_judge: Optional[Outcome] = None,
) -> tuple[Outcome, str, Dict[str, Any]]:
    judge_val = (
        llm_judge.value if isinstance(llm_judge, Outcome) else (llm_judge or JUDGE_NA)
    )
    detail: Dict[str, Any] = {
        "deterministic": deterministic.value,
        "counter_refuted": counter_refuted,
        "llm_judge": judge_val,
    }

    if deterministic == Outcome.FAIL:
        detail["reconciled"] = Outcome.FAIL.value
        return Outcome.FAIL, "high", detail

    if deterministic == Outcome.UNCERTAIN:
        detail["reconciled"] = Outcome.UNCERTAIN.value
        return Outcome.UNCERTAIN, "low", detail

    if counter_refuted:
        detail["reconciled"] = Outcome.UNCERTAIN.value
        return Outcome.UNCERTAIN, "low", detail

    if judge_val not in (Outcome.PASS.value, JUDGE_NA):
        detail["reconciled"] = Outcome.UNCERTAIN.value
        return Outcome.UNCERTAIN, "medium", detail

    confidence = "high" if judge_val == Outcome.PASS.value else "high"
    detail["reconciled"] = Outcome.PASS.value
    return Outcome.PASS, confidence, detail
