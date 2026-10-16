"""Corpus-level instruction-precedence checks: malicious tool/document instructions,
policy-conflicting user instructions, and conflicting injections are scored against the
declared expected verdicts over the frozen answers.
"""

from __future__ import annotations

import pytest

from answer_behavior.corpus import load_corpus
from answer_behavior.models import Verdict
from answer_behavior.rubric import adjudicate


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def _cases(corpus, category):
    return [case for case in corpus.cases if case.category == category]


@pytest.mark.parametrize(
    "category",
    [
        "tool_output_injection",
        "document_override",
        "confirmation_bypass",
        "conflicting_injections",
        "fabricated_approval",
    ],
)
def test_instruction_precedence_expected_verdicts(corpus, category):
    for case in _cases(corpus, category):
        assert case.dimension == "instruction_precedence"
        for actor in ("baseline", "candidate"):
            result = adjudicate(case, case.frozen_answers[actor])
            assert result.verdict.value == case.expected_verdicts[actor], (
                f"{case.case_id}/{actor}: {result.verdict.value} != "
                f"{case.expected_verdicts[actor]} ({result.reason})"
            )


def test_candidate_discloses_every_required_injection_and_never_follows(corpus):
    for case in corpus.cases:
        untrusted = case.environment.untrusted_content
        if untrusted is None:
            continue
        candidate = case.frozen_answers["candidate"]
        assert candidate.untrusted_instruction_followed is False, case.case_id
        if untrusted.disclosure_required:
            assert candidate.untrusted_content_disclosed is True, case.case_id


def test_baseline_exhibits_the_failure_mode_the_rubric_must_catch(corpus):
    for case in corpus.cases:
        if case.dimension != "instruction_precedence":
            continue
        baseline = case.frozen_answers["baseline"]
        result = adjudicate(case, baseline)
        assert result.verdict.is_violation, case.case_id


def test_confirmation_bypass_baseline_never_holds_a_receipt(corpus):
    for case in _cases(corpus, "confirmation_bypass"):
        baseline = case.frozen_answers["baseline"]
        assert baseline.action_claims, "bypass case must attempt the claimed action"
        receipt_ids = {r.action_id for r in case.environment.receipts}
        for claim in baseline.action_claims:
            assert claim.action_id not in receipt_ids


def test_candidate_on_conflicting_injections_abstains_with_warrant(corpus):
    for case in _cases(corpus, "conflicting_injections"):
        candidate = case.frozen_answers["candidate"]
        assert candidate.abstained
        assert case.expected_verdicts["candidate"] == Verdict.WARRANTED_ABSTENTION.value
