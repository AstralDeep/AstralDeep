"""Tests for backend/answer_behavior/corpus.py: the frozen corpus parses, stays within
its declared bounds, carries provenance pins and both identity scopes, and fails closed
on malformed entries.
"""

from __future__ import annotations

import copy

import pytest

from answer_behavior.corpus import MAX_CASES, load_corpus, parse_corpus
from answer_behavior.models import ACTIONS, DIMENSIONS


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def test_frozen_corpus_loads_within_bounds(corpus):
    assert corpus.schema_version == 1
    assert corpus.suite == "answer-behavior"
    assert 0 < len(corpus.cases) <= MAX_CASES


def test_every_dimension_and_both_actors_are_represented(corpus):
    assert {case.dimension for case in corpus.cases} == set(DIMENSIONS)
    for case in corpus.cases:
        assert set(case.frozen_answers) == set(ACTIONS)
        assert set(case.expected_verdicts) == set(ACTIONS)


def test_case_ids_are_unique(corpus):
    ids = [case.case_id for case in corpus.cases]
    assert len(ids) == len(set(ids))


def test_provenance_pins_every_adapted_source(corpus):
    assert corpus.provenance["corpus_license"]
    pins = corpus.provenance["adapted_from"]
    assert pins, "corpus must record its adapted sources"
    for source in pins:
        assert source["name"] and source["pin"] and source["retrieved"]
    names = " ".join(f"{source['name']} {source['pin']}" for source in pins)
    assert "model_spec_dataset" in names
    assert "monitorability" in names
    assert "s41586-026-10549-w" in names
    assert "guan26h" in names


def test_development_and_held_out_identities_are_retained(corpus):
    dev = corpus.cases_for_identity_scope("development")
    held = corpus.cases_for_identity_scope("held_out")
    assert dev and held


def test_rejects_duplicate_case_id():
    raw = _raw_corpus()
    raw["cases"].append(copy.deepcopy(raw["cases"][0]))
    with pytest.raises(ValueError, match="duplicate case_id"):
        parse_corpus(raw)


def test_rejects_unknown_dimension():
    raw = _raw_corpus()
    raw["cases"][0]["dimension"] = "charm"
    with pytest.raises(ValueError, match="unknown dimension"):
        parse_corpus(raw)


def test_rejects_undeclared_identity():
    raw = _raw_corpus()
    raw["cases"][0]["identity"] = "ghost"
    with pytest.raises(ValueError, match="not declared"):
        parse_corpus(raw)


def test_rejects_corpus_without_held_out_identities():
    raw = _raw_corpus()
    for case in raw["cases"]:
        case["identity"] = raw["identities"]["development"][0]
    with pytest.raises(ValueError, match="held_out"):
        parse_corpus(raw)


def test_rejects_missing_frozen_answer():
    raw = _raw_corpus()
    del raw["cases"][0]["frozen_answers"]["candidate"]
    with pytest.raises(ValueError, match="missing frozen answer"):
        parse_corpus(raw)


def test_rejects_case_without_scorable_axis():
    raw = _raw_corpus()
    case = raw["cases"][0]
    case["environment"] = {}
    case["unscorable_reason"] = ""
    with pytest.raises(ValueError, match="no scorable axis"):
        parse_corpus(raw)


def test_rejects_unbounded_corpus():
    raw = _raw_corpus()
    raw["cases"] = [copy.deepcopy(raw["cases"][0]) for _ in range(MAX_CASES + 1)]
    for index, case in enumerate(raw["cases"]):
        case["case_id"] = f"duplicate-{index}"
    with pytest.raises(ValueError, match="bounded"):
        parse_corpus(raw)


def test_rejects_unsupported_schema_version():
    raw = _raw_corpus()
    raw["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        parse_corpus(raw)


def test_rejects_malformed_provenance():
    raw = _raw_corpus()
    raw["provenance"]["adapted_from"] = [{"name": "x"}]
    with pytest.raises(ValueError, match="pin"):
        parse_corpus(raw)


def test_rejects_unknown_expected_verdict():
    raw = _raw_corpus()
    raw["cases"][0]["expected_verdicts"]["candidate"] = "perfect"
    with pytest.raises(ValueError, match="unknown verdict"):
        parse_corpus(raw)


def test_rejects_unknown_actor_fields():
    raw = _raw_corpus()
    raw["cases"][0]["expected_verdicts"]["crowd"] = "truthful_report"
    with pytest.raises(ValueError, match="unknown actor"):
        parse_corpus(raw)


def test_rejects_missing_required_field():
    raw = _raw_corpus()
    del raw["cases"][0]["user_prompt"]
    with pytest.raises(ValueError, match="user_prompt"):
        parse_corpus(raw)


def _raw_corpus():
    from answer_behavior.corpus import DEFAULT_CORPUS_PATH
    import json

    with open(DEFAULT_CORPUS_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_rejects_empty_case_list():
    raw = _raw_corpus()
    raw["cases"] = []
    with pytest.raises(ValueError, match="bounded"):
        parse_corpus(raw)


def test_rejects_malformed_identity_scope():
    raw = _raw_corpus()
    raw["identities"]["development"] = "dev-primary"
    with pytest.raises(ValueError, match="identities"):
        parse_corpus(raw)
