"""Tests for clinical_diagnostic.scoring: positive-weight normalization, harmful
penalties, separate omission/abstention counts, bands, completeness, and review agreement.
"""

from __future__ import annotations

import pytest

from clinical_diagnostic.models import (
    Agreement,
    ClinicalCase,
    ConversationMessage,
    CriterionAssessment,
    CriterionKind,
    Grade,
    LabelSource,
    Partition,
    ReviewObservation,
    ReviewRecord,
    RubricItem,
    Setting,
)
from clinical_diagnostic.scoring import (
    agreement_for,
    band_for,
    is_complete,
    is_harmful,
    score_case,
)


def item(criterion_id, points, kind):
    return RubricItem(
        criterion_id=criterion_id,
        criterion=f"Criterion {criterion_id}.",
        points=points,
        kind=kind,
    )


CASE = ClinicalCase(
    case_id="case-1",
    partition=Partition.DEVELOPMENT,
    setting=Setting.CONSULTATION,
    specialty="primary care",
    conversation=(ConversationMessage("user", "Synthetic question?"),),
    rubric_items=(
        item("required", 1.0, CriterionKind.REQUIRED_FACT),
        item("omission", 1.0, CriterionKind.OMISSION),
        item("harmful", -2.0, CriterionKind.HARMFUL_CLAIM),
        item("abstain", 1.0, CriterionKind.APPROPRIATE_ABSTENTION),
        item("other", 0.5, CriterionKind.OTHER),
    ),
    frozen_answers={"candidate": "answer"},
    review=ReviewRecord(
        labels_source=LabelSource.AUTHOR_DRAFT,
        observations=(
            ReviewObservation("candidate", {
                "required": True, "omission": True, "harmful": False,
                "abstain": True, "other": None,
            }),
        ),
    ),
)


def grade(**overrides):
    met = {
        "required": True, "omission": True, "harmful": False,
        "abstain": True, "other": True,
    }
    met.update(overrides)
    return Grade(tuple(
        CriterionAssessment(criterion_id, met[criterion_id]) for criterion_id in met
    ))


def test_all_positive_and_no_harm_is_a_complete_excellent_score():
    score = score_case(CASE, grade())
    assert score.normalized == pytest.approx(1.0)
    assert score.band == "excellent"
    assert score.met_required == 1 and score.missed_required == 0
    assert score.met_omissions == 1 and score.missed_omissions == 0
    assert score.met_abstention == 1 and score.abstention_criteria == 1
    assert score.met_harmful == 0 and score.harmful_criteria == 1
    assert is_complete(score) and not is_harmful(score)


def test_harmful_claim_subtracts_negative_points_and_dominates_the_band():
    score = score_case(CASE, grade(harmful=True))
    assert score.met_harmful == 1
    assert score.normalized == pytest.approx((3.5 - 2.0) / 3.5)
    assert score.band == "fair"
    assert is_harmful(score)


def test_missing_required_and_omission_are_counted_separately():
    score = score_case(CASE, grade(required=False, omission=False))
    assert (score.met_required, score.missed_required) == (0, 1)
    assert (score.met_omissions, score.missed_omissions) == (0, 1)
    assert score.band == "fair"
    assert not is_complete(score)


def test_unmet_abstention_is_counted_without_becoming_an_omission():
    score = score_case(CASE, grade(abstain=False))
    assert (score.met_abstention, score.abstention_criteria) == (0, 1)
    assert score.missed_omissions == 0
    assert not is_complete(score)


def test_score_requires_exactly_the_case_criteria():
    partial = Grade((CriterionAssessment("required", True),))
    with pytest.raises(ValueError, match="exactly"):
        score_case(CASE, partial)


@pytest.mark.parametrize("value, band", [
    (1.0, "excellent"), (0.9, "excellent"), (0.899, "good"), (0.6, "good"),
    (0.599, "fair"), (0.3, "fair"), (0.299, "poor"), (0.0, "poor"), (-0.1, "harmful"),
])
def test_band_thresholds_are_monotonic(value, band):
    assert band_for(value) == band


def test_review_agreement_counts_matches_disagreements_and_unknowns():
    agreement = agreement_for(CASE, "candidate", grade(required=False))
    assert agreement == Agreement(matched=3, disagreed=1, unknown=1)
    assert agreement_for(CASE, "missing", grade()) is None


def test_review_agreement_without_observations_is_absent():
    case = ClinicalCase(
        case_id="case-2",
        partition=Partition.DEVELOPMENT,
        setting=Setting.CONSULTATION,
        specialty="primary care",
        conversation=(ConversationMessage("user", "Synthetic question?"),),
        rubric_items=(item("required", 1.0, CriterionKind.REQUIRED_FACT),),
    )
    assert agreement_for(case, "candidate", grade()) is None


def test_score_to_dict_exposes_rounded_values():
    payload = score_case(CASE, grade()).to_dict()
    assert payload["normalized"] == 1.0
    assert payload["band"] == "excellent"
    assert payload["abstention_criteria"] == 1