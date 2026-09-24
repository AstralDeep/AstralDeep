"""Shared contract and data model (BenchmarkAdapter, BenchmarkCase,
AdversarialObjective, CaseTrace) every adapter and driver normalizes onto, so
adjudicator.py and report.py serve any benchmark alike.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Dict, List, Optional

OBJECTIVE_KINDS = (
    "forbidden_tool",
    "out_of_scope_action",
    "privilege_escalation",
    "phi_egress",
    "exfil_egress",
    "prompt_injection_semantic",
    "confused_deputy",
    "cross_hop_escalation",
    "depth_violation",
    "actor_chain_forgery",
    "chained_consent_replay",
)

CHAINED_OBJECTIVE_KINDS = frozenset({
    "confused_deputy", "cross_hop_escalation", "depth_violation",
    "actor_chain_forgery", "chained_consent_replay",
})


@dataclass(frozen=True)
class AdversarialObjective:
    kind: str
    target_tool: Optional[str] = None
    required_scope: Optional[str] = None
    egress_sink: Optional[str] = None
    effect_point: str = "execution"
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in OBJECTIVE_KINDS:
            raise ValueError(f"unknown objective kind: {self.kind!r}")


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    benchmark: str
    category: str
    objective: AdversarialObjective
    user_prompt: str = ""
    injection: str = ""
    scenario: Optional[str] = None
    out_of_corpus: bool = False
    out_of_corpus_reason: str = ""
    meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class ToolCallObservation:
    tool: str
    required_scope: Optional[str] = None
    in_scope: bool = True
    executed: bool = False
    blocked_by: Optional[str] = None
    egress_fired: bool = False
    phi_crossed_boundary: bool = False
    is_adversarial: bool = False


@dataclass
class CaseTrace:
    case_id: str
    envelope_label: str
    bait_taken: bool = False
    tool_calls: List[ToolCallObservation] = field(default_factory=list)
    audit_event_ids: List[str] = field(default_factory=list)
    notes: str = ""

    @property
    def adversarial_calls(self) -> List[ToolCallObservation]:
        return [c for c in self.tool_calls if c.is_adversarial]


class BenchmarkAdapter(abc.ABC):
    name: str = ""
    corpus_version: str = "unknown"

    @abc.abstractmethod
    def load_cases(self, limit: Optional[int] = None) -> List[BenchmarkCase]:
        raise NotImplementedError

    def describe(self) -> Dict[str, str]:
        return {"name": self.name, "corpus_version": self.corpus_version}
