"""Data model shared by the answer-behavior suite: frozen cases with their environment
(receipts, denials, resources, untrusted content), observed answer transcripts, and the
rubric verdict taxonomy scored separately per failure class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

DIMENSIONS = ("instruction_precedence", "abstention", "truthful_completion")

SUCCESS_VERDICTS = frozenset({"warranted_abstention", "truthful_report"})
VIOLATION_VERDICTS = frozenset({
    "wrong_claim", "omission", "fabricated_completion", "untrusted_followed",
})

ACTIONS = ("baseline", "candidate")

COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"


class Verdict(str, Enum):
    WRONG_CLAIM = "wrong_claim"
    OMISSION = "omission"
    FABRICATED_COMPLETION = "fabricated_completion"
    UNTRUSTED_FOLLOWED = "untrusted_followed"
    WARRANTED_ABSTENTION = "warranted_abstention"
    TRUTHFUL_REPORT = "truthful_report"
    UNSCOREABLE = "unscoreable"

    @property
    def is_success(self) -> bool:
        return self.value in SUCCESS_VERDICTS

    @property
    def is_violation(self) -> bool:
        return self.value in VIOLATION_VERDICTS


@dataclass(frozen=True)
class Receipt:
    action_id: str
    tool: str
    dispatch: str
    outcome: Optional[str] = None
    artifact: Optional[str] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.dispatch not in ("attempted", "denied"):
            raise ValueError(f"receipt dispatch must be attempted|denied: {self.dispatch!r}")
        if self.dispatch == "attempted" and self.outcome not in (COMPLETED, FAILED, CANCELLED):
            raise ValueError(f"attempted receipt needs terminal outcome: {self.outcome!r}")
        if self.dispatch == "denied" and self.outcome is not None:
            raise ValueError("denied receipt must not claim a terminal outcome")


@dataclass(frozen=True)
class PermissionDenial:
    tool: str
    reason_marker: str


@dataclass(frozen=True)
class UntrustedContent:
    source: str
    embedded_instruction: str
    effect_markers: List[str] = field(default_factory=list)
    disclosure_required: bool = True


@dataclass(frozen=True)
class Question:
    answerable: bool


@dataclass(frozen=True)
class Resource:
    resource_id: str
    text: str


@dataclass(frozen=True)
class Environment:
    receipts: List[Receipt] = field(default_factory=list)
    permission_denials: List[PermissionDenial] = field(default_factory=list)
    dispatch_attempted: Dict[str, bool] = field(default_factory=dict)
    resources_returned: List[Resource] = field(default_factory=list)
    untrusted_content: Optional[UntrustedContent] = None
    question: Optional[Question] = None


@dataclass(frozen=True)
class ActionClaim:
    action_id: str
    tool: str
    claimed_outcome: str

    def __post_init__(self) -> None:
        if self.claimed_outcome not in (COMPLETED, FAILED, CANCELLED):
            raise ValueError(f"unknown claimed outcome: {self.claimed_outcome!r}")


@dataclass(frozen=True)
class AnswerTranscript:
    actor: str
    statements: List[str] = field(default_factory=list)
    resource_citations: List[str] = field(default_factory=list)
    action_claims: List[ActionClaim] = field(default_factory=list)
    abstained: bool = False
    abstention_reason: str = ""
    untrusted_instruction_followed: bool = False
    untrusted_content_disclosed: bool = False

    @property
    def statement_text(self) -> str:
        return " ".join(self.statements)


@dataclass(frozen=True)
class BehaviorCase:
    case_id: str
    dimension: str
    category: str
    identity: str
    authority: str
    user_prompt: str
    environment: Environment
    warrant_markers: List[str] = field(default_factory=list)
    fabrication_markers: List[str] = field(default_factory=list)
    evidence_markers: List[str] = field(default_factory=list)
    report_required: List[str] = field(default_factory=list)
    unscorable_reason: str = ""
    frozen_answers: Dict[str, AnswerTranscript] = field(default_factory=dict)
    expected_verdicts: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.dimension not in DIMENSIONS:
            raise ValueError(f"unknown dimension: {self.dimension!r}")
        for actor, verdict in self.expected_verdicts.items():
            if actor not in ACTIONS:
                raise ValueError(f"unknown actor: {actor!r}")
            try:
                Verdict(verdict)
            except ValueError as exc:
                raise ValueError(f"unknown verdict for {self.case_id}: {verdict!r}") from exc
        for actor in self.frozen_answers:
            if actor not in ACTIONS:
                raise ValueError(f"unknown frozen actor: {actor!r}")
