"""Defines the clinical diagnostic's frozen case, rubric, review, answer, grade, and
infrastructure-failure records shared by the schema adapter, scorer, runner, and report.
Every constructor validates its own bounds so malformed fixtures fail closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional, Tuple

MAX_CASES = 50
MIN_CASES = 30
MAX_MESSAGES = 64
MAX_MESSAGE_CHARS = 32 * 1024
MAX_CONVERSATION_CHARS = 128 * 1024
MAX_RUBRIC_ITEMS = 64
MAX_CRITERION_CHARS = 2000
MAX_TAGS = 8
MAX_TAG_CHARS = 64
MAX_POINTS = 100.0
MAX_CASE_ID_CHARS = 120

ROLES = ("system", "user", "assistant")
PARTITIONS = ("development", "held_out")
SETTINGS = ("consultation", "documentation", "research", "administration")
CRITERION_KINDS = (
    "required_fact",
    "omission",
    "harmful_claim",
    "appropriate_abstention",
    "other",
)
REVIEW_STATUSES = ("draft", "clinician_reviewed", "disputed", "retired")
LABEL_SOURCES = ("none", "author_draft", "clinician_review")
BANDS = ("excellent", "good", "fair", "poor", "harmful", "unscorable")
FAILURE_CODES = (
    "invalid_grade",
    "rate_limited",
    "timeout",
    "cancelled",
    "budget_exhausted",
    "provider_error",
    "protocol_error",
    "asset_identity",
    "dispatcher_denied",
    "malformed_answer",
    "internal_error",
)


class Partition(str, Enum):
    DEVELOPMENT = "development"
    HELD_OUT = "held_out"


class Setting(str, Enum):
    UNKNOWN = "unknown"
    CONSULTATION = "consultation"
    DOCUMENTATION = "documentation"
    RESEARCH = "research"
    ADMINISTRATION = "administration"


class CriterionKind(str, Enum):
    REQUIRED_FACT = "required_fact"
    OMISSION = "omission"
    HARMFUL_CLAIM = "harmful_claim"
    APPROPRIATE_ABSTENTION = "appropriate_abstention"
    OTHER = "other"


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    CLINICIAN_REVIEWED = "clinician_reviewed"
    DISPUTED = "disputed"
    RETIRED = "retired"


class LabelSource(str, Enum):
    NONE = "none"
    AUTHOR_DRAFT = "author_draft"
    CLINICIAN_REVIEW = "clinician_review"


class FailureCode(str, Enum):
    INTERNAL_ERROR = "internal_error"
    INVALID_GRADE = "invalid_grade"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROVIDER_ERROR = "provider_error"
    PROTOCOL_ERROR = "protocol_error"
    ASSET_IDENTITY = "asset_identity"
    DISPATCHER_DENIED = "dispatcher_denied"
    MALFORMED_ANSWER = "malformed_answer"


class AnswerSource(str, Enum):
    BARE = "bare_model"
    ASTRAL = "astral_dispatcher"
    FROZEN_BASELINE = "frozen_baseline"
    FROZEN_CANDIDATE = "frozen_candidate"


def _require_text(value: object, name: str, *, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _require_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or abs(number) > MAX_POINTS:
        raise ValueError(f"{name} must be finite and within +/-{MAX_POINTS}")
    return number


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"conversation role must be one of {ROLES}: {self.role!r}")
        _require_text(self.content, "conversation content", limit=MAX_MESSAGE_CHARS)

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class RubricItem:
    criterion_id: str
    criterion: str
    points: float
    tags: Tuple[str, ...] = ()
    kind: CriterionKind = CriterionKind.REQUIRED_FACT
    evidence_terms: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.criterion_id, "criterion_id", limit=MAX_CASE_ID_CHARS)
        _require_text(self.criterion, "criterion", limit=MAX_CRITERION_CHARS)
        points = _require_finite(self.points, "rubric points")
        if points == 0:
            raise ValueError("rubric points must not be zero")
        if not isinstance(self.kind, CriterionKind):
            object.__setattr__(self, "kind", CriterionKind(str(self.kind)))
        if self.kind is CriterionKind.HARMFUL_CLAIM and points > 0:
            raise ValueError("harmful-claim criteria must carry negative points")
        if self.kind is not CriterionKind.HARMFUL_CLAIM and points < 0:
            raise ValueError(f"{self.kind.value} criteria must carry positive points")
        if len(self.tags) > MAX_TAGS:
            raise ValueError(f"rubric item exceeds {MAX_TAGS} tags")
        for tag in self.tags:
            _require_text(tag, "rubric tag", limit=MAX_TAG_CHARS)
        for term in self.evidence_terms:
            _require_text(term, "evidence term", limit=MAX_CRITERION_CHARS)

    @property
    def is_negative(self) -> bool:
        return self.points < 0

    def to_dict(self) -> dict:
        return {
            "criterion_id": self.criterion_id,
            "criterion": self.criterion,
            "points": self.points,
            "tags": list(self.tags),
            "kind": self.kind.value,
            "evidence_terms": list(self.evidence_terms),
        }


@dataclass(frozen=True)
class ReviewObservation:
    answer_key: str
    labels: Mapping[str, Optional[bool]]

    def __post_init__(self) -> None:
        _require_text(self.answer_key, "review answer_key", limit=MAX_CASE_ID_CHARS)
        for criterion_id, label in self.labels.items():
            _require_text(criterion_id, "review criterion_id", limit=MAX_CASE_ID_CHARS)
            if label is not None and not isinstance(label, bool):
                raise ValueError("review labels must be true, false, or null")


@dataclass(frozen=True)
class ReviewRecord:
    status: ReviewStatus = ReviewStatus.DRAFT
    labels_source: LabelSource = LabelSource.NONE
    reviewer_token: str = ""
    reviewed_at: str = ""
    observations: Tuple[ReviewObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReviewStatus):
            object.__setattr__(self, "status", ReviewStatus(str(self.status)))
        if not isinstance(self.labels_source, LabelSource):
            object.__setattr__(self, "labels_source", LabelSource(str(self.labels_source)))
        if self.reviewed_at:
            _require_text(self.reviewed_at, "reviewed_at", limit=MAX_TAG_CHARS)
        if self.status is ReviewStatus.CLINICIAN_REVIEWED:
            if self.labels_source is not LabelSource.CLINICIAN_REVIEW:
                raise ValueError("clinician-reviewed cases must cite clinician labels")
            _require_text(self.reviewer_token, "reviewer_token", limit=MAX_CASE_ID_CHARS)
            if not self.reviewed_at:
                raise ValueError("clinician-reviewed cases must record reviewed_at")

    @property
    def is_clinician_reviewed(self) -> bool:
        return self.status is ReviewStatus.CLINICIAN_REVIEWED


@dataclass(frozen=True)
class ClinicalCase:
    case_id: str
    partition: Partition
    setting: Setting
    specialty: str
    conversation: Tuple[ConversationMessage, ...]
    rubric_items: Tuple[RubricItem, ...]
    review: ReviewRecord = field(default_factory=ReviewRecord)
    frozen_answers: Mapping[str, str] = field(default_factory=dict)
    example_tags: Tuple[str, ...] = ()
    official_use_case: str = ""

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id", limit=MAX_CASE_ID_CHARS)
        _require_text(self.specialty, "specialty", limit=MAX_TAG_CHARS)
        _require_text(self.official_use_case, "official_use_case", limit=2000, allow_empty=True)
        if not isinstance(self.partition, Partition):
            object.__setattr__(self, "partition", Partition(str(self.partition)))
        if not isinstance(self.setting, Setting):
            object.__setattr__(self, "setting", Setting(str(self.setting)))
        if not 1 <= len(self.conversation) <= MAX_MESSAGES:
            raise ValueError(f"conversation must hold 1..{MAX_MESSAGES} messages")
        if self.conversation[-1].role != "user":
            raise ValueError("conversation must end with a user message")
        total_chars = sum(len(message.content) for message in self.conversation)
        if total_chars > MAX_CONVERSATION_CHARS:
            raise ValueError(f"conversation exceeds {MAX_CONVERSATION_CHARS} characters")
        if not 1 <= len(self.rubric_items) <= MAX_RUBRIC_ITEMS:
            raise ValueError(f"rubric must hold 1..{MAX_RUBRIC_ITEMS} items")
        ids = [item.criterion_id for item in self.rubric_items]
        if len(ids) != len(set(ids)):
            raise ValueError("rubric criterion_id values must be unique")
        if sum(item.points for item in self.rubric_items if item.points > 0) <= 0:
            raise ValueError("rubric must carry positive weight")
        if len(self.example_tags) > MAX_TAGS:
            raise ValueError(f"example exceeds {MAX_TAGS} tags")
        for key, answer in self.frozen_answers.items():
            _require_text(key, "frozen answer key", limit=MAX_TAG_CHARS)
            _require_text(answer, "frozen answer", limit=MAX_CONVERSATION_CHARS)
        for observation in self.review.observations:
            if observation.answer_key not in self.frozen_answers:
                raise ValueError(
                    f"review observation names unknown frozen answer {observation.answer_key!r}"
                )
            unknown = set(observation.labels) - set(ids)
            if unknown:
                raise ValueError(
                    f"review labels name unknown criteria: {sorted(unknown)!r}"
                )

    @property
    def positive_weight(self) -> float:
        return sum(item.points for item in self.rubric_items if item.points > 0)

    def prompt_messages(self) -> List[dict]:
        return [message.to_dict() for message in self.conversation]

    def criteria_by_id(self) -> Dict[str, RubricItem]:
        return {item.criterion_id: item for item in self.rubric_items}

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "partition": self.partition.value,
            "setting": self.setting.value,
            "specialty": self.specialty,
            "conversation": [message.to_dict() for message in self.conversation],
            "rubric_items": [item.to_dict() for item in self.rubric_items],
            "review": {
                "status": self.review.status.value,
                "labels_source": self.review.labels_source.value,
                "reviewer_token": self.review.reviewer_token,
                "reviewed_at": self.review.reviewed_at,
                "observations": [
                    {"answer_key": observation.answer_key, "labels": dict(observation.labels)}
                    for observation in self.review.observations
                ],
            },
            "frozen_answers": dict(self.frozen_answers),
            "example_tags": list(self.example_tags),
            "official_use_case": self.official_use_case,
        }


@dataclass(frozen=True)
class CaseAnswer:
    case_id: str
    source: AnswerSource
    text: str
    model_identity: str = ""
    cost: float = 0.0
    audit_event_ids: Tuple[str, ...] = ()
    tool_denials: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.case_id, "answer case_id", limit=MAX_CASE_ID_CHARS)
        if not isinstance(self.source, AnswerSource):
            object.__setattr__(self, "source", AnswerSource(str(self.source)))
        _require_text(self.text, "answer text", limit=MAX_CONVERSATION_CHARS)
        if self.model_identity:
            _require_text(self.model_identity, "model_identity", limit=MAX_TAG_CHARS)
        if isinstance(self.cost, bool) or not isinstance(self.cost, (int, float)):
            raise ValueError("answer cost must be a finite number")
        if not math.isfinite(float(self.cost)) or float(self.cost) < 0:
            raise ValueError("answer cost must be finite and non-negative")
        for identifier in (*self.audit_event_ids, *self.tool_denials):
            _require_text(identifier, "answer evidence id", limit=MAX_CASE_ID_CHARS)


@dataclass(frozen=True)
class CriterionAssessment:
    criterion_id: str
    met: bool

    def __post_init__(self) -> None:
        _require_text(self.criterion_id, "assessment criterion_id", limit=MAX_CASE_ID_CHARS)
        if not isinstance(self.met, bool):
            raise ValueError("assessment met must be a boolean")


@dataclass(frozen=True)
class Grade:
    assessments: Tuple[CriterionAssessment, ...]

    def __post_init__(self) -> None:
        ids = [assessment.criterion_id for assessment in self.assessments]
        if len(ids) != len(set(ids)):
            raise ValueError("grade assessments must be unique per criterion")

    @property
    def by_criterion(self) -> Dict[str, bool]:
        return {assessment.criterion_id: assessment.met for assessment in self.assessments}


@dataclass(frozen=True)
class Score:
    total_points: float
    possible_points: float
    normalized: Optional[float]
    band: str
    met_required: int
    missed_required: int
    met_harmful: int
    harmful_criteria: int
    met_abstention: int
    abstention_criteria: int
    met_omissions: int
    missed_omissions: int

    def to_dict(self) -> dict:
        return {
            "total_points": round(self.total_points, 6),
            "possible_points": round(self.possible_points, 6),
            "normalized": None if self.normalized is None else round(self.normalized, 6),
            "band": self.band,
            "met_required": self.met_required,
            "missed_required": self.missed_required,
            "met_harmful": self.met_harmful,
            "harmful_criteria": self.harmful_criteria,
            "met_abstention": self.met_abstention,
            "abstention_criteria": self.abstention_criteria,
            "met_omissions": self.met_omissions,
            "missed_omissions": self.missed_omissions,
        }


@dataclass(frozen=True)
class InfrastructureFailure:
    code: FailureCode
    detail: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, FailureCode):
            object.__setattr__(self, "code", FailureCode(str(self.code)))
        _require_text(self.detail, "failure detail", limit=MAX_CRITERION_CHARS)

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "detail": self.detail,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    partition: Partition
    setting: Setting
    specialty: str
    review_status: ReviewStatus
    labels_source: LabelSource
    answer_source: AnswerSource
    answer_digest: str
    status: str
    score: Optional[Score] = None
    failure: Optional[InfrastructureFailure] = None
    agreement: Optional["Agreement"] = None
    model_identity: str = "unknown"
    grader_identity: str = "unknown"
    official_use_case: str = ""

    def __post_init__(self) -> None:
        _require_text(self.model_identity, "model_identity", limit=MAX_TAG_CHARS)
        _require_text(self.grader_identity, "grader_identity", limit=MAX_TAG_CHARS)
        _require_text(self.official_use_case, "official_use_case", limit=2000, allow_empty=True)
        if self.status not in ("scored", "infrastructure_failure"):
            raise ValueError(f"unknown case result status {self.status!r}")
        if (self.score is None) == (self.failure is None):
            raise ValueError("case result must carry exactly one of score or failure")

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "partition": self.partition.value,
            "setting": self.setting.value,
            "specialty": self.specialty,
            "review_status": self.review_status.value,
            "labels_source": self.labels_source.value,
            "answer_source": self.answer_source.value,
            "answer_digest": self.answer_digest,
            "status": self.status,
            "score": None if self.score is None else self.score.to_dict(),
            "failure": None if self.failure is None else self.failure.to_dict(),
            "agreement": None if self.agreement is None else self.agreement.to_dict(),
            "model_identity": self.model_identity,
            "grader_identity": self.grader_identity,
            "official_use_case": self.official_use_case,
        }


@dataclass(frozen=True)
class Agreement:
    matched: int
    disagreed: int
    unknown: int

    @property
    def decided(self) -> int:
        return self.matched + self.disagreed

    @property
    def rate(self) -> Optional[float]:
        return self.matched / self.decided if self.decided else None

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "disagreed": self.disagreed,
            "unknown": self.unknown,
            "decided": self.decided,
            "rate": None if self.rate is None else round(self.rate, 6),
        }
