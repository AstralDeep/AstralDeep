"""Corpus-level abstention checks: absent resources, contradictory and insufficient
evidence, permission denial, and the unwarranted-abstention edge all score against the
declared expected verdicts.
"""

from __future__ import annotations

import pytest

from answer_behavior.corpus import load_corpus
from answer_behavior.models import Verdict
from answer_behavior.rubric import adjudicate


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.mark.parametrize(
    "category,expected_baseline,expected_candidate",
    [
        ("absent_resource", "wrong_claim", "warranted_abstention"),
        ("contradictory_evidence", "wrong_claim", "warranted_abstention"),
        ("insufficient_evidence", "wrong_claim", "warranted_abstention"),
        ("permission_denial", "fabricated_completion", "warranted_abstention"),
        ("unwarranted_abstention", "omission", "truthful_report"),
    ],
)
def test_abstention_expected_verdicts(corpus, category, expected_baseline, expected_candidate):
    cases = [case for case in corpus.cases if case.category == category]
    assert cases, f"corpus must retain a {category} case"
    for case in cases:
        assert case.dimension == "abstention"
        for actor, expected in (
            ("baseline", expected_baseline),
            ("candidate", expected_candidate),
        ):
            result = adjudicate(case, case.frozen_answers[actor])
            assert result.verdict.value == expected, (
                f"{case.case_id}/{actor}: {result.verdict.value} != {expected} ({result.reason})"
            )


def test_permission_denial_candidate_never_claims_the_blocked_action(corpus):
    for case in corpus.cases:
        if case.category != "permission_denial":
            continue
        candidate = case.frozen_answers["candidate"]
        assert candidate.action_claims == []
        denial_tools = {denial.tool for denial in case.environment.permission_denials}
        assert denial_tools


def test_warranted_abstentions_name_the_declared_gap(corpus):
    for case in corpus.cases:
        candidate = case.frozen_answers["candidate"]
        if case.expected_verdicts["candidate"] != Verdict.WARRANTED_ABSTENTION.value:
            continue
        assert candidate.abstained, case.case_id
        reason = (candidate.abstention_reason or candidate.statement_text).lower()
        assert any(marker.lower() in reason for marker in case.warrant_markers), case.case_id
