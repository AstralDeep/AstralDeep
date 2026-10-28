"""Tests for the frozen pilot corpus: digest pinning, partition separation, scenario
coverage, structural invariants, and fail-closed validation of tampered fixtures.
"""

from __future__ import annotations

import pytest

from personalization.semantic_pilot import corpus as corpus_module
from personalization.semantic_pilot.corpus import (
    CORPUS_DIGEST,
    PARTITION_FINAL,
    PARTITION_SELECTION,
    MemoryCase,
    MemoryRowSpec,
    load_corpus,
    tokens,
)


def test_load_corpus_matches_pinned_digest():
    selection, final, identity = load_corpus()
    assert identity["corpus_digest"] == CORPUS_DIGEST
    assert identity["corpus_digest"] == corpus_module.corpus_digest()
    assert selection and final
    assert identity["selection_case_ids"] == [case.case_id for case in selection]
    assert identity["final_case_ids"] == [case.case_id for case in final]


def test_selection_and_final_partitions_are_disjoint():
    selection, final, _ = load_corpus()
    selection_ids = {case.case_id for case in selection}
    final_ids = {case.case_id for case in final}
    assert selection_ids.isdisjoint(final_ids)
    assert all(case.partition == PARTITION_SELECTION for case in selection)
    assert all(case.partition == PARTITION_FINAL for case in final)


def test_every_declared_scenario_is_covered():
    _, _, _ = load_corpus()
    covered = {
        scenario
        for case in corpus_module.ALL_CASES
        for scenario in case.scenarios
    }
    assert covered == set(corpus_module.KNOWN_SCENARIOS)


def test_both_partitions_cover_paraphrase_and_empty_seed():
    selection, final, _ = load_corpus()
    for partition_cases in (selection, final):
        scenarios = {s for case in partition_cases for s in case.scenarios}
        assert corpus_module.SCENARIO_PARAPHRASE in scenarios
        assert corpus_module.SCENARIO_EMPTY_SEED in scenarios


def test_row_identities_are_globally_unique():
    load_corpus()
    seen = set()
    for case in corpus_module.ALL_CASES:
        for row in case.rows:
            assert row.row_id not in seen
            seen.add(row.row_id)


def test_forbidden_rows_are_never_relevant():
    load_corpus()
    for case in corpus_module.ALL_CASES:
        for row in case.rows:
            if row.forbidden:
                assert row.relevance == 0
                assert row.rationale


def test_zero_overlap_labels_are_machine_checked():
    for case in corpus_module.ALL_CASES:
        if corpus_module.SCENARIO_PARAPHRASE not in case.scenarios:
            continue
        for row in case.rows:
            if row.relevance > 0 and not row.forbidden:
                surface = tokens(f"{row.category} {row.value} {row.keywords or ''}")
                assert not (tokens(case.query) & surface)


def test_boundary_cases_declare_twins():
    load_corpus()
    by_id = {case.case_id: case for case in corpus_module.ALL_CASES}
    owner_case = by_id["fin-02-owner-boundary-desk"]
    foreign = [r for r in owner_case.rows if r.owner_id != owner_case.owner_id]
    assert len(foreign) == 1
    own = [r for r in owner_case.rows
           if r.row_id == "fin02-r1-elbow" and r.owner_id == owner_case.owner_id]
    assert own[0].value == foreign[0].value

    project_case = by_id["fin-03-project-boundary-tickets"]
    assert project_case.project_id == "proj-aurora"
    offscope = [r for r in project_case.rows if r.project_id == "proj-borealis"]
    assert offscope and offscope[0].forbidden


def test_tampered_rows_declare_invalid_signature_path():
    load_corpus()
    case = next(c for c in corpus_module.ALL_CASES if c.case_id == "fin-05-tampered-injection")
    tampered = [r for r in case.rows if r.tampered]
    assert len(tampered) == 1
    assert tampered[0].forbidden
    instructions = [r for r in case.rows
                    if corpus_module.SCENARIO_INSTRUCTION_ROW in case.scenarios
                    and not r.tampered and r.forbidden]
    assert instructions


def _base_row(**overrides):
    fields = dict(
        row_id="x1", owner_id="owner-atlas", category="preference", value="likes tea",
        relevance=3, rationale="test",
    )
    fields.update(overrides)
    return MemoryRowSpec(**fields)


def _bad_case(rows, **overrides):
    fields = dict(
        case_id="bad", partition=PARTITION_SELECTION, scenarios=("lexical_ambiguity",),
        owner_id="owner-atlas", query="tea", rows=tuple(rows),
    )
    fields.update(overrides)
    return MemoryCase(**fields)


def test_validation_rejects_case_without_relevant_row():
    selection, _, _ = load_corpus()
    with pytest.raises(corpus_module.CorpusError, match="no relevant row"):
        corpus_module._validate_case(
            _bad_case([_base_row(relevance=0, forbidden=False)]), {})


def test_validation_rejects_forbidden_row_with_positive_relevance():
    with pytest.raises(corpus_module.CorpusError, match="relevance 0"):
        corpus_module._validate_case(
            _bad_case([
                _base_row(row_id="a"),
                _base_row(row_id="b", relevance=2, forbidden=True),
            ]), {})


def test_validation_rejects_tampered_row_that_is_not_forbidden():
    with pytest.raises(corpus_module.CorpusError, match="forbidden"):
        corpus_module._validate_case(
            _bad_case([
                _base_row(row_id="a"),
                _base_row(row_id="b", relevance=0, tampered=True),
            ]), {})


def test_validation_rejects_duplicate_row_identity():
    with pytest.raises(corpus_module.CorpusError, match="duplicate row identity"):
        corpus_module._validate_case(
            _bad_case([_base_row(), _base_row(relevance=0)]), {})


def test_validation_rejects_paraphrase_case_with_token_overlap():
    with pytest.raises(corpus_module.CorpusError, match="zero-overlap"):
        corpus_module._validate_case(
            _bad_case(
                [_base_row(row_id="a", value="likes hot tea with honey")],
                scenarios=("paraphrase_zero_overlap",),
                query="hot tea",
            ), {})


def test_validation_rejects_unknown_partition_and_scenario():
    with pytest.raises(corpus_module.CorpusError, match="partition"):
        corpus_module._validate_case(
            _bad_case([_base_row()], partition="holdout"), {})
    with pytest.raises(corpus_module.CorpusError, match="scenario"):
        corpus_module._validate_case(
            _bad_case([_base_row()], scenarios=("nonexistent",)), {})


def test_validation_rejects_links_crossing_owners():
    with pytest.raises(corpus_module.CorpusError, match="link crosses owners"):
        corpus_module._validate_case(
            _bad_case(
                [
                    _base_row(row_id="a"),
                    _base_row(row_id="b", owner_id="owner-other", relevance=0),
                ],
                links=(("a", "b"),),
            ), {})


def test_validation_rejects_temporal_case_without_closed_validity():
    with pytest.raises(corpus_module.CorpusError, match="closed validity"):
        corpus_module._validate_case(
            _bad_case([_base_row()], scenarios=("contradiction_supersession",)), {})


def test_validation_rejects_owner_boundary_without_foreign_row():
    with pytest.raises(corpus_module.CorpusError, match="foreign row"):
        corpus_module._validate_case(
            _bad_case([_base_row()], scenarios=("owner_boundary",)), {})


def test_validation_rejects_project_boundary_without_scope():
    with pytest.raises(corpus_module.CorpusError, match="needs a scope"):
        corpus_module._validate_case(
            _bad_case([_base_row()], scenarios=("project_boundary",)), {})


def test_case_digest_is_stable_per_case():
    _, _, identity = load_corpus()
    case = corpus_module.ALL_CASES[0]
    assert identity["case_digests"][case.case_id] == corpus_module._case_digest(case)
