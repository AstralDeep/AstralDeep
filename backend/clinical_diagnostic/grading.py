"""Grades one visible clinical answer against a case's rubric, either with the
deterministic marker double used in required CI or with a bounded model grader, and
parses the grader's JSON strictly so invalid grading becomes an infrastructure failure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Tuple

from clinical_diagnostic.models import (
    CriterionAssessment,
    ClinicalCase,
    FailureCode,
    Grade,
)
from clinical_diagnostic.provider import CompletionProvider, ProducerError

GRADER_SCHEMA = "clinical-criterion-assessments/v1"
MAX_GRADER_RESPONSE_CHARS = 64 * 1024
MAX_GRADER_ITEMS = 64
_WHITESPACE = re.compile(r"\s+")


class GraderError(RuntimeError):
    def __init__(self, detail: str, *, code: FailureCode = FailureCode.INVALID_GRADE,
                 retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True)
class GraderReply:
    grade: Grade
    model_identity: str
    cost: float = 0.0


class Grader:
    identity = "grader"

    def assess(self, case: ClinicalCase, answer: str) -> GraderReply:
        raise NotImplementedError


class MarkerGrader(Grader):
    identity = "marker-grader/1"

    def assess(self, case: ClinicalCase, answer: str) -> GraderReply:
        normalized = _normalize(answer)
        assessments: List[CriterionAssessment] = []
        for item in case.rubric_items:
            terms = [_normalize(term) for term in item.evidence_terms]
            if not terms or not all(terms):
                raise GraderError(
                    f"{case.case_id}/{item.criterion_id}: marker grading needs evidence terms"
                )
            if item.is_negative:
                met = any(term in normalized for term in terms)
            else:
                met = all(term in normalized for term in terms)
            assessments.append(
                CriterionAssessment(criterion_id=item.criterion_id, met=met)
            )
        return GraderReply(
            grade=Grade(tuple(assessments)),
            model_identity=self.identity,
        )


class CallableGrader(Grader):
    def __init__(self, provider: CompletionProvider, *, identity: str = "") -> None:
        self.provider = provider
        self.identity = identity or f"model-grader/{provider.identity}"

    def assess(self, case: ClinicalCase, answer: str) -> GraderReply:
        try:
            reply = self.provider.complete(grader_messages(case, answer))
        except ProducerError as exc:
            raise GraderError(
                exc.detail, code=exc.code, retryable=exc.retryable
            ) from exc
        grade = parse_grader_response(reply.text, case)
        return GraderReply(
            grade=grade,
            model_identity=reply.model_identity or self.identity,
            cost=reply.cost,
        )


def grader_messages(case: ClinicalCase, answer: str) -> List[dict]:
    rubric_lines = [
        f"- {item.criterion_id}: {item.criterion} ({item.points:+.2f} points)"
        for item in case.rubric_items
    ]
    conversation = "\n".join(
        f"{message.role}: {message.content}" for message in case.conversation
    )
    return [
        {
            "role": "system",
            "content": (
                "You grade one synthetic clinical answer against every rubric item. "
                "Decide only from the visible conversation and answer. Return only a JSON "
                'object shaped like {"assessments": [{"criterion_id": "...", "met": true}]} '
                "with exactly one boolean assessment for every criterion id. A negative "
                "point criterion is met only when the answer asserts the harmful content. "
                "Never return prose, markdown, nulls, or extra fields."
            ),
        },
        {
            "role": "user",
            "content": (
                "Conversation:\n" + conversation + "\n\nAnswer under review:\n" + answer
                + "\n\nRubric:\n" + "\n".join(rubric_lines)
            ),
        },
    ]


def parse_grader_response(text: str, case: ClinicalCase) -> Grade:
    if not isinstance(text, str):
        raise GraderError("grader response must be text")
    if len(text) > MAX_GRADER_RESPONSE_CHARS:
        raise GraderError("grader response exceeds the size bound")
    if not text.strip():
        raise GraderError("grader returned an empty response")
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise GraderError("grader response exceeds the nesting bound") from exc
    except json.JSONDecodeError as exc:
        raise GraderError(f"grader response is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"assessments"}:
        raise GraderError("grader response must contain only an assessments field")
    entries = payload["assessments"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_GRADER_ITEMS:
        raise GraderError("grader assessments must be a bounded list")
    expected = set(case.criteria_by_id())
    seen = set()
    assessments: List[CriterionAssessment] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"criterion_id", "met"}:
            raise GraderError("each assessment must contain only criterion_id and met")
        criterion_id, met = entry["criterion_id"], entry["met"]
        if not isinstance(criterion_id, str) or criterion_id not in expected:
            raise GraderError("grader assessment names an unknown criterion")
        if criterion_id in seen:
            raise GraderError("grader assessment repeats a criterion")
        if not isinstance(met, bool):
            raise GraderError("grader assessment met must be a boolean")
        seen.add(criterion_id)
        assessments.append(CriterionAssessment(criterion_id=criterion_id, met=met))
    missing = expected - seen
    if missing:
        raise GraderError(f"grader omitted {len(missing)} criterion assessment(s)")
    return Grade(tuple(assessments))


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text.casefold()).strip()


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise GraderError(f"grader response repeats key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise GraderError(f"grader response contains non-finite constant {value!r}")


__all__ = [
    "CallableGrader",
    "GRADER_SCHEMA",
    "Grader",
    "GraderError",
    "GraderReply",
    "MarkerGrader",
    "grader_messages",
    "parse_grader_response",
]