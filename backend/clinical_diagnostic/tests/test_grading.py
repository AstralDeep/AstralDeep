"""Tests for clinical_diagnostic.grading: deterministic marker grading, strict grader JSON
parsing, bounded model-grader calls, and every invalid-grading failure path.
"""

from __future__ import annotations

import json

import pytest

from clinical_diagnostic.grading import (
    CallableGrader,
    GraderError,
    MarkerGrader,
    grader_messages,
    parse_grader_response,
)
from clinical_diagnostic.models import (
    ClinicalCase,
    ConversationMessage,
    CriterionKind,
    FailureCode,
    Partition,
    RubricItem,
    Setting,
)
from clinical_diagnostic.provider import (
    CallableProvider,
    ProducerError,
    ProviderReply,
    RateLimited,
)


def item(criterion_id, points, kind, terms):
    return RubricItem(
        criterion_id=criterion_id,
        criterion=f"Criterion {criterion_id}.",
        points=points,
        kind=kind,
        evidence_terms=tuple(terms),
    )


def case():
    return ClinicalCase(
        case_id="case-1",
        partition=Partition.DEVELOPMENT,
        setting=Setting.CONSULTATION,
        specialty="primary care",
        conversation=(ConversationMessage("user", "Synthetic question?"),),
        rubric_items=(
            item("required", 1.0, CriterionKind.REQUIRED_FACT, ["required fact"]),
            item("omission", 1.0, CriterionKind.OMISSION, ["safety action"]),
            item("harmful", -2.0, CriterionKind.HARMFUL_CLAIM, ["harmful claim"]),
            item("abstain", 1.0, CriterionKind.APPROPRIATE_ABSTENTION, ["cannot determine"]),
        ),
    )


def response(*pairs):
    return json.dumps({
        "assessments": [
            {"criterion_id": criterion_id, "met": met} for criterion_id, met in pairs
        ]
    })


def test_marker_grader_separates_positive_and_negative_semantics():
    grade = MarkerGrader().assess(
        case(), "Required fact. Safety action. Cannot determine remotely."
    ).grade
    assert grade.by_criterion == {
        "required": True, "omission": True, "harmful": False, "abstain": True
    }
    harmful = MarkerGrader().assess(
        case(), "Required fact. Safety action. Cannot determine. Harmful claim."
    ).grade
    assert harmful.by_criterion["harmful"] is True


def test_marker_grader_requires_evidence_terms():
    missing = ClinicalCase(
        case_id="case-2",
        partition=Partition.DEVELOPMENT,
        setting=Setting.CONSULTATION,
        specialty="primary care",
        conversation=(ConversationMessage("user", "Synthetic question?"),),
        rubric_items=(item("required", 1.0, CriterionKind.REQUIRED_FACT, []),),
    )
    with pytest.raises(GraderError, match="evidence terms"):
        MarkerGrader().assess(missing, "answer")


def test_parse_grader_response_accepts_exact_assessments():
    grade = parse_grader_response(
        response(("required", True), ("omission", False), ("harmful", False),
                 ("abstain", True)),
        case(),
    )
    assert grade.by_criterion["required"] is True
    assert grade.by_criterion["omission"] is False


@pytest.mark.parametrize("payload", [
    "",
    "not json",
    '{"assessments": [], "extra": 1}',
    '{"criteria": []}',
    '{"assessments": {}}',
    '{"assessments": [{"criterion_id": "required", "met": true, "why": "x"}]}',
    '{"assessments": [{"criterion_id": "unknown", "met": true}]}',
    '{"assessments": [{"criterion_id": "required", "met": null}]}',
    '{"assessments": [{"criterion_id": "required", "met": true}, '
    '{"criterion_id": "required", "met": false}]}',
    '{"assessments": [{"criterion_id": "required", "met": true}]}',
    '{"assessments": [{"criterion_id": "required", "met": true, "criterion_id": "omission"}]}',
    '{"assessments": NaN}',
])
def test_parse_grader_response_refuses_invalid_grading(payload):
    with pytest.raises(GraderError):
        parse_grader_response(payload, case())


def test_parse_grader_response_enforces_its_size_bound():
    with pytest.raises(GraderError, match="size bound"):
        parse_grader_response(" " * (64 * 1024 + 1), case())


def test_callable_grader_delegates_and_carries_identity_and_cost():
    calls = []

    def complete(messages):
        calls.append(messages)
        return ProviderReply(
            text=response(("required", True), ("omission", True),
                          ("harmful", False), ("abstain", True)),
            model_identity="grader-model-1",
            cost=0.02,
        )

    grader = CallableGrader(CallableProvider(complete, "grader-provider"))
    reply = grader.assess(case(), "answer")
    assert reply.grade.by_criterion["required"] is True
    assert reply.model_identity == "grader-model-1"
    assert reply.cost == 0.02
    assert grader.identity == "model-grader/grader-provider"
    assert len(calls) == 1


def test_callable_grader_maps_provider_failure_to_grading_failure():
    def complete(_messages):
        raise RateLimited()

    with pytest.raises(GraderError) as caught:
        CallableGrader(CallableProvider(complete, "grader-provider")).assess(
            case(), "answer"
        )
    assert caught.value.code is FailureCode.RATE_LIMITED
    assert caught.value.retryable is True


def test_callable_grader_explicit_identity_and_producer_error():
    provider = CallableProvider(lambda _m: ProviderReply("x", "m"), "p")
    assert CallableGrader(provider, identity="custom").identity == "custom"

    class Broken:
        identity = "broken"

        def complete(self, _messages):
            raise ProducerError("broken")

    with pytest.raises(GraderError) as caught:
        CallableGrader(Broken()).assess(case(), "answer")
    assert caught.value.code is FailureCode.PROVIDER_ERROR


def test_grader_messages_include_criteria_but_not_evidence_terms():
    messages = grader_messages(case(), "Visible answer.")
    prompt = json.dumps(messages)
    assert "Criterion required." in prompt
    assert "required fact" not in prompt
    assert "Visible answer." in prompt
    assert "-2.00 points" in prompt