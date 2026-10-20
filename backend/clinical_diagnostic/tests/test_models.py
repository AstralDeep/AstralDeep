"""Tests for clinical_diagnostic.models: constructor bounds, criterion sign rules, review
status requirements, answer/grade validation, and score-result exclusivity.
"""

from __future__ import annotations

import math

import pytest

from clinical_diagnostic.models import (
    Agreement,
    AnswerSource,
    CaseAnswer,
    CaseResult,
    ClinicalCase,
    ConversationMessage,
    CriterionAssessment,
    CriterionKind,
    FailureCode,
    Grade,
    InfrastructureFailure,
    LabelSource,
    Partition,
    ReviewObservation,
    ReviewRecord,
    ReviewStatus,
    RubricItem,
    Score,
    Setting,
)


def _item(**over):
    fields = dict(criterion_id="c1", criterion="States the fact.", points=1.0)
    fields.update(over)
    return RubricItem(**fields)


def _case(**over):
    fields = dict(
        case_id="case-1",
        partition=Partition.DEVELOPMENT,
        setting=Setting.CONSULTATION,
        specialty="primary care",
        conversation=(ConversationMessage("user", "Synthetic question?"),),
        rubric_items=(_item(),),
    )
    fields.update(over)
    return ClinicalCase(**fields)


def test_message_rejects_unknown_role_and_empty_content():
    with pytest.raises(ValueError, match="role"):
        ConversationMessage("tool", "x")
    with pytest.raises(ValueError, match="must not be empty"):
        ConversationMessage("user", "  ")


def test_rubric_sign_rules_are_enforced():
    with pytest.raises(ValueError, match="harmful-claim"):
        _item(kind=CriterionKind.HARMFUL_CLAIM, points=2.0)
    with pytest.raises(ValueError, match="must carry positive"):
        _item(points=-2.0)
    with pytest.raises(ValueError, match="must not be zero"):
        _item(points=0.0)
    with pytest.raises(ValueError, match="finite"):
        _item(points=math.inf)
    assert _item(kind=CriterionKind.HARMFUL_CLAIM, points=-2.0).is_negative


def test_rubric_coerces_string_kind_and_rejects_tag_overflow():
    item = _item(kind="omission")
    assert item.kind is CriterionKind.OMISSION
    with pytest.raises(ValueError, match="tags"):
        _item(tags=tuple(f"t{i}" for i in range(9)))


def test_case_requires_terminal_user_message_and_positive_weight():
    with pytest.raises(ValueError, match="end with a user"):
        _case(conversation=(ConversationMessage("assistant", "answer"),))
    with pytest.raises(ValueError, match="positive weight"):
        _case(rubric_items=(_item(kind=CriterionKind.HARMFUL_CLAIM, points=-2.0),))
    with pytest.raises(ValueError, match="unique"):
        _case(rubric_items=(_item(), _item(criterion="Other.")))


def test_case_rejects_review_labels_for_unknown_answers_and_criteria():
    review = ReviewRecord(
        observations=(ReviewObservation("candidate", {"c1": True}),)
    )
    with pytest.raises(ValueError, match="unknown frozen answer"):
        _case(review=review)
    review = ReviewRecord(
        observations=(ReviewObservation("candidate", {"other": True}),)
    )
    with pytest.raises(ValueError, match="unknown criteria"):
        _case(review=review, frozen_answers={"candidate": "answer"})


def test_clinician_review_requires_reviewer_identity_and_labels():
    with pytest.raises(ValueError, match="clinician labels"):
        ReviewRecord(status=ReviewStatus.CLINICIAN_REVIEWED)
    with pytest.raises(ValueError, match="reviewer_token"):
        ReviewRecord(
            status=ReviewStatus.CLINICIAN_REVIEWED,
            labels_source=LabelSource.CLINICIAN_REVIEW,
        )
    record = ReviewRecord(
        status=ReviewStatus.CLINICIAN_REVIEWED,
        labels_source=LabelSource.CLINICIAN_REVIEW,
        reviewer_token="reviewer-1",
        reviewed_at="2026-10-20",
    )
    assert record.is_clinician_reviewed


def test_case_answer_rejects_empty_text_and_bad_cost():
    with pytest.raises(ValueError, match="must not be empty"):
        CaseAnswer("case-1", AnswerSource.BARE, " ")
    with pytest.raises(ValueError, match="cost"):
        CaseAnswer("case-1", AnswerSource.BARE, "answer", cost=-1)
    answer = CaseAnswer("case-1", "bare_model", "answer", cost=0.1)
    assert answer.source is AnswerSource.BARE


def test_grade_rejects_duplicates_and_non_boolean():
    with pytest.raises(ValueError, match="boolean"):
        CriterionAssessment("c1", "true")
    with pytest.raises(ValueError, match="unique"):
        Grade((CriterionAssessment("c1", True), CriterionAssessment("c1", False)))
    assert Grade((CriterionAssessment("c1", True),)).by_criterion == {"c1": True}


def test_failure_requires_known_code_and_bounded_detail():
    assert InfrastructureFailure("timeout", "slow").code is FailureCode.TIMEOUT
    with pytest.raises(ValueError):
        InfrastructureFailure("not-a-code", "x")
    with pytest.raises(ValueError, match="detail"):
        InfrastructureFailure("timeout", "")


def test_case_result_requires_exactly_one_outcome():
    score = Score(1, 1, 1.0, "excellent", 1, 0, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="exactly one"):
        CaseResult(
            "case-1", Partition.DEVELOPMENT, Setting.CONSULTATION, "primary care",
            ReviewStatus.DRAFT, LabelSource.NONE, AnswerSource.BARE, "d", "scored",
            score=score, failure=InfrastructureFailure("timeout", "x"),
        )
    with pytest.raises(ValueError, match="exactly one"):
        CaseResult(
            "case-1", Partition.DEVELOPMENT, Setting.CONSULTATION, "primary care",
            ReviewStatus.DRAFT, LabelSource.NONE, AnswerSource.BARE, "d", "scored",
        )
    assert score.to_dict()["band"] == "excellent"


def test_agreement_rate_handles_empty_and_decided():
    assert Agreement(0, 0, 3).rate is None
    assert Agreement(2, 1, 0).rate == pytest.approx(2 / 3)


def test_case_to_dict_round_trips_core_fields():
    payload = _case(frozen_answers={"candidate": "answer"}).to_dict()
    assert payload["partition"] == "development"
    assert payload["rubric_items"][0]["points"] == 1.0
    assert payload["frozen_answers"] == {"candidate": "answer"}