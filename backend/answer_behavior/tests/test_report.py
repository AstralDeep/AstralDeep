"""Tests for backend/answer_behavior/report.py: dimension stats, separately-counted
failure classes, unscorable exclusion, identity scopes, and markdown rendering.
"""

from __future__ import annotations

import pytest

from answer_behavior.corpus import load_corpus
from answer_behavior.report import (
    baseline_comparison,
    class_totals,
    dimension_stats,
    render_markdown,
    scorable_success_rate,
    unscorable_scores,
)
from answer_behavior.rubric import adjudicate
from answer_behavior.run_record import CaseScore, RunKey, RunRecord


@pytest.fixture(scope="module")
def record():
    corpus = load_corpus()
    scope_by_identity = {
        name: scope
        for scope, names in corpus.identities.items()
        for name in names
    }
    built = RunRecord(
        key=RunKey(model="test-model", corpus_version=corpus.suite_version),
        run_id="test-run",
        mode="frozen",
    )
    for case in corpus.cases:
        for actor in ("baseline", "candidate"):
            built.scores.append(
                CaseScore(
                    case_id=case.case_id,
                    dimension=case.dimension,
                    category=case.category,
                    identity=case.identity,
                    identity_scope=scope_by_identity[case.identity],
                    actor=actor,
                    result=adjudicate(case, case.frozen_answers[actor]),
                    expected=case.expected_verdicts[actor],
                )
            )
    return built


def test_dimension_stats_exclude_unscorable_from_denominator(record):
    stats = dimension_stats(record.scores, "baseline", "truthful_completion")
    assert stats.scorable == 4
    assert stats.class_counts["unscoreable"] == 0
    assert stats.success_rate == 0.0


def test_candidate_dimensions_are_perfect_on_the_frozen_corpus(record):
    for dimension in ("instruction_precedence", "abstention", "truthful_completion"):
        stats = dimension_stats(record.scores, "candidate", dimension)
        assert stats.success_rate == 1.0, dimension
        assert stats.successes == stats.scorable


def test_class_totals_count_each_class_once_per_case(record):
    totals = class_totals(record.scores, "baseline")
    assert totals["fabricated_completion"] >= 4
    assert totals["wrong_claim"] >= 4
    assert totals["untrusted_followed"] >= 4
    assert totals["omission"] >= 2
    assert totals["warranted_abstention"] == 0
    candidate_totals = class_totals(record.scores, "candidate")
    assert candidate_totals["warranted_abstention"] == 7
    assert candidate_totals["truthful_report"] == 8


def test_unscorable_scores_are_listed(record):
    unscorable = unscorable_scores(record.scores)
    assert {score.case_id for score in unscorable} == {"tc-unscorable-async-001"}


def test_baseline_comparison_deltas_are_positive(record):
    for dimension in ("instruction_precedence", "abstention", "truthful_completion"):
        comparison = baseline_comparison(record.scores, dimension)
        assert comparison is not None
        assert comparison["delta"] > 0


def test_scorable_success_rate_none_when_nothing_scorable():
    assert scorable_success_rate([]) is None


def test_dimension_stats_empty_dimension():
    stats = dimension_stats([], "candidate", "abstention")
    assert stats.scorable == 0
    assert stats.success_rate is None


def test_render_markdown_contains_required_sections(record):
    text = render_markdown(record, cases_count=15)
    assert "# Answer-Behavior Diagnostic Report" in text
    assert "## Per-dimension success rates" in text
    assert "## Failure and success classes" in text
    assert "## Identity scopes" in text
    assert "## Unscorable outcomes" in text
    assert "excluded from every denominator" in text
    assert "dev" in text and "held_out" in text
    assert "tc-unscorable-async-001" in text
    for verdict in ("wrong_claim", "omission", "fabricated_completion",
                    "untrusted_followed", "warranted_abstention", "truthful_report"):
        assert verdict in text


def test_render_markdown_with_empty_record():
    empty = RunRecord(key=RunKey(model="m", corpus_version="0"), run_id="r", mode="frozen")
    text = render_markdown(empty, cases_count=0)
    assert "—" in text


def test_dimension_stats_to_dict_roundtrip(record):
    stats = dimension_stats(record.scores, "baseline", "abstention")
    payload = stats.to_dict()
    assert payload["actor"] == "baseline"
    assert payload["dimension"] == "abstention"
    assert payload["success_rate"] == 0.0
    assert payload["class_counts"]["wrong_claim"] >= 3
