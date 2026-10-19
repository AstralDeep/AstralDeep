"""Tests for clinical_diagnostic.corpus: frozen synthetic corpus loading, stratification,
review counts, content digests, and fail-closed validation of every required dimension.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from clinical_diagnostic.corpus import (
    PROVENANCE,
    SYNTHETIC_CORPUS_PATH,
    SYNTHETIC_LICENSE,
    canonical_cases,
    conversation_digest,
    load_synthetic_corpus,
    partition_summary,
    validate_corpus,
)
from clinical_diagnostic.healthbench import REVIEWED_ASSET_SHA256, SchemaError
from clinical_diagnostic.models import (
    CriterionKind,
    Partition,
    ReviewRecord,
    RubricItem,
    Setting,
)


def test_frozen_corpus_is_stratified_and_hashes_are_stable():
    corpus = load_synthetic_corpus()
    assert 30 <= len(corpus.cases) <= 50
    assert corpus.license_name == SYNTHETIC_LICENSE
    assert corpus.file_digest and corpus.content_digest
    assert corpus.file_digest == load_synthetic_corpus().file_digest
    assert corpus.content_digest == load_synthetic_corpus().content_digest
    summary = partition_summary(corpus.cases)
    assert set(summary) == {"development", "held_out"}
    assert summary["development"] == {"cases": 18, "settings": 4, "criteria": 72}
    assert summary["held_out"] == {"cases": 18, "settings": 4, "criteria": 72}
    assert corpus.review_counts()["draft"] == 36
    assert corpus.review_counts()["clinician_reviewed"] == 0


def test_frozen_corpus_preserves_reviewed_asset_identity():
    corpus = load_synthetic_corpus()
    assert corpus.provenance["asset"]["sha256"] == REVIEWED_ASSET_SHA256
    assert corpus.provenance["asset"]["url"].startswith("https://openaipublic")
    assert any("simple-evals" in source["url"] for source in corpus.provenance["sources"])
    assert any("medhelm" in source["url"] for source in corpus.provenance["sources"])


def test_partition_selection_and_content_digests_are_deterministic():
    corpus = load_synthetic_corpus()
    development = corpus.for_partition(Partition.DEVELOPMENT)
    held_out = corpus.for_partition(Partition.HELD_OUT)
    assert len(development) == len(held_out) == 18
    assert {conversation_digest(case) for case in development}.isdisjoint(
        {conversation_digest(case) for case in held_out}
    )
    assert canonical_cases(corpus.cases) == canonical_cases(tuple(corpus.cases))
    assert corpus.for_partition(Partition.DEVELOPMENT, limit=3) == development[:3]


def test_validate_refuses_out_of_range_or_duplicate_corpus():
    corpus = load_synthetic_corpus()
    with pytest.raises(SchemaError, match="30..50"):
        validate_corpus(corpus.cases[:10])
    with pytest.raises(SchemaError, match="unique"):
        validate_corpus(tuple([corpus.cases[0]] * 36))
    with pytest.raises(SchemaError, match="development and held-out"):
        validate_corpus(tuple(
            replace(case, partition=Partition.DEVELOPMENT) for case in corpus.cases
        ))


def test_validate_refuses_missing_setting_and_criteria_coverage():
    corpus = load_synthetic_corpus()
    cases = list(corpus.cases)
    for index, case in enumerate(cases):
        if case.partition is Partition.DEVELOPMENT:
            items = tuple(
                replace(item, kind=CriterionKind.OTHER)
                if item.kind is CriterionKind.APPROPRIATE_ABSTENTION else item
                for item in case.rubric_items
            )
            cases[index] = replace(case, rubric_items=items)
    with pytest.raises(SchemaError, match="appropriate_abstention"):
        validate_corpus(tuple(cases))
    cases = list(corpus.cases)
    for index, case in enumerate(cases):
        if case.partition is Partition.HELD_OUT:
            cases[index] = replace(case, setting=Setting.CONSULTATION)
    with pytest.raises(SchemaError, match="every workflow setting"):
        validate_corpus(tuple(cases))


def test_validate_refuses_missing_frozen_answers_and_observations():
    corpus = load_synthetic_corpus()
    cases = list(corpus.cases)
    cases[0] = replace(
        cases[0],
        frozen_answers={"candidate": "answer"},
        review=replace(
            cases[0].review,
            observations=tuple(
                observation for observation in cases[0].review.observations
                if observation.answer_key == "candidate"
            ),
        ),
    )
    with pytest.raises(SchemaError, match="missing frozen baseline"):
        validate_corpus(tuple(cases))
    cases = list(corpus.cases)
    cases[1] = replace(
        cases[1],
        review=ReviewRecord(labels_source=cases[1].review.labels_source),
    )
    with pytest.raises(SchemaError, match="missing candidate review observation"):
        validate_corpus(tuple(cases))


def test_validate_refuses_insufficient_abstention_coverage():
    corpus = load_synthetic_corpus()
    cases = []
    for case in corpus.cases:
        items = tuple(
            replace(item, kind=CriterionKind.REQUIRED_FACT)
            if item.kind is CriterionKind.APPROPRIATE_ABSTENTION else item
            for item in case.rubric_items
        )
        cases.append(replace(case, rubric_items=items))
    with pytest.raises(SchemaError, match="appropriate_abstention"):
        validate_corpus(tuple(cases))


def test_validate_refuses_duplicate_conversations_across_partitions():
    corpus = load_synthetic_corpus()
    development = [case for case in corpus.cases
                   if case.partition is Partition.DEVELOPMENT]
    held = [case for case in corpus.cases if case.partition is Partition.HELD_OUT]
    held[0] = replace(held[0], conversation=development[0].conversation)
    with pytest.raises(SchemaError, match="must be distinct"):
        validate_corpus(tuple(development + held))


def test_validate_refuses_one_partition_only():
    corpus = load_synthetic_corpus()
    with pytest.raises(SchemaError, match="development and held-out"):
        validate_corpus(tuple(
            replace(case, partition=Partition.DEVELOPMENT) for case in corpus.cases
        ))


def test_partition_summary_is_empty_for_no_cases():
    assert partition_summary(()) == {}


def test_corpus_path_is_the_tracked_fixture():
    assert SYNTHETIC_CORPUS_PATH.endswith("fixtures/professional_synthetic.json")
    assert PROVENANCE["schema"] == "healthbench-professional/v1"
    item = RubricItem("c1", "Criterion.", 1.0)
    assert item.criterion_id == "c1"

def _overlay(tmp_path, **over):
    case = load_synthetic_corpus().cases[0]
    payload = {
        "reviews": [{
            "case_id": case.case_id,
            "status": "clinician_reviewed",
            "reviewer_token": "reviewer-1",
            "reviewed_at": "2026-10-20",
            "observations": [{
                "answer_key": "candidate",
                "labels": {case.rubric_items[0].criterion_id: True},
            }],
        }]
    }
    payload.update(over)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, case


def test_review_overlay_marks_a_case_reviewed_and_is_digest_bound(tmp_path):
    from clinical_diagnostic.corpus import load_review_overlay

    path, case = _overlay(tmp_path)
    cases, digest = load_review_overlay(str(path), [case])
    assert cases[0].review.status.value == "clinician_reviewed"
    assert cases[0].review.labels_source.value == "clinician_review"
    assert cases[0].review.reviewer_token == "reviewer-1"
    assert cases[0].review.observations[0].labels[case.rubric_items[0].criterion_id] is True
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutate", [
    lambda p: p.update({"reviews": {}}),
    lambda p: p.update({"extra": 1}),
    lambda p: p["reviews"].append(dict(p["reviews"][0])),
    lambda p: p["reviews"][0].update({"case_id": "missing"}),
    lambda p: p["reviews"][0].update({"status": "approved"}),
    lambda p: p["reviews"][0].update({"reviewer_token": ""}),
    lambda p: p["reviews"][0].update({"unknown": 1}),
    lambda p: p["reviews"][0].update({"observations": {}}),
    lambda p: p["reviews"][0].update({"observations": [{"answer_key": "candidate"}]}),
    lambda p: p["reviews"][0].update({"observations": [
        {"answer_key": "candidate", "labels": {"unknown": True}}
    ]}),
    lambda p: p["reviews"][0].update({"observations": [
        {"answer_key": "candidate", "labels": {"required_fact-1": "yes"}}
    ]}),
])
def test_review_overlay_fails_closed(tmp_path, mutate):
    from clinical_diagnostic.corpus import load_review_overlay

    path, case = _overlay(tmp_path)
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((SchemaError, ValueError)):
        load_review_overlay(str(path), [case])


def test_review_overlay_rejects_oversized_or_invalid_files(tmp_path):
    from clinical_diagnostic.corpus import load_review_overlay

    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SchemaError, match="valid UTF-8 JSON"):
        load_review_overlay(str(path), [load_synthetic_corpus().cases[0]])
    path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(SchemaError, match="reviews list"):
        load_review_overlay(str(path), [load_synthetic_corpus().cases[0]])
