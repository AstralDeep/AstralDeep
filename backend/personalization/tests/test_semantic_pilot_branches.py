"""Branch-completion tests for the pilot: mid-flight revalidation drops, baseline error
recording, unknown-profile fallback, attestation problem enumeration, recommendation
branches, report write failures, and store/analyzer helpers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personalization.memory_tools import MemoryTools
from personalization.semantic_pilot import corpus as corpus_module
from personalization.semantic_pilot.bounds import Bounds
from personalization.semantic_pilot.candidates import _cosine, rerank_candidates, \
    semantic_candidates
from personalization.semantic_pilot.corpus import load_corpus
from personalization.semantic_pilot.providers import ProviderUsage, VectorGateway, \
    scripted_transport
from personalization.semantic_pilot.report import recommend, write_report
from personalization.semantic_pilot.runner import PilotConfig, run_pilot
from personalization.semantic_pilot.snapshot import (
    CorpusStore,
    _CleanAnalyzer,
    build_tools,
    detect_defenses,
)


@pytest.fixture()
def defenses_on(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "1")
    monkeypatch.setenv("FF_PROJECT_MEMORY", "1")
    monkeypatch.setenv("FF_MEMORY_GUARD", "1")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "branch-test-key")
    yield


def _case(case_id):
    selection, final, _ = load_corpus()
    return next(c for c in selection + final if c.case_id == case_id)


def _store_hiding_deletions(case):
    tools, store = build_tools(case)
    hidden = SimpleNamespace(
        get_memory=lambda user_id, mem_id: None,
        list_memory=store.list_memory,
        list_links=store.list_links,
        linked_ids=store.linked_ids,
    )
    return tools, store, hidden


def test_semantic_candidates_record_midflight_deletions(defenses_on):
    case = _case("sel-02-ambiguity-portal")
    tools, store, hidden = _store_hiding_deletions(case)
    gateway = VectorGateway(_identity(), scripted_transport())
    usage = ProviderUsage()
    run = semantic_candidates(
        tools, hidden, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=3, bounds=Bounds(), usage=usage)
    assert run.ranked == []
    assert len(run.revalidation_drops) == run.snapshot_rows
    assert all(drop["reason"] == "row_absent" for drop in run.revalidation_drops)


def _identity(**overrides):
    from personalization.semantic_pilot.providers import ProviderIdentity

    fields = dict(
        profile="scripted-double", model="m", revision="r", tokenizer_revision="t",
        dimension=256, license="l", notice="n", qualifier="provider_double_only",
        rerank_model="m", rerank_revision="r", rerank_capable=True,
    )
    fields.update(overrides)
    return ProviderIdentity(**fields)


def test_rerank_records_midflight_deletions(defenses_on):
    case = _case("sel-02-ambiguity-portal")
    tools, store, hidden = _store_hiding_deletions(case)
    gateway = VectorGateway(_identity(), scripted_transport())
    usage = ProviderUsage()
    candidates = semantic_candidates(
        tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=2, bounds=Bounds(), usage=usage)
    reranked = rerank_candidates(
        tools, hidden, gateway, candidates, user_id=case.owner_id,
        project_id=case.project_id, query=case.query, bounds=Bounds(), usage=usage)
    assert reranked.ranked == []
    assert len(reranked.revalidation_drops) == len(candidates.ranked)


def test_cosine_guards_mismatched_and_empty_vectors():
    assert _cosine([], [1.0]) == 0.0
    assert _cosine([1.0], []) == 0.0
    assert _cosine([1.0, 0.0], [1.0]) == 0.0
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_baseline_error_recorded_per_case(defenses_on, monkeypatch):
    def broken(self, user_id, query, *, limit=10, project_id=None):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(MemoryTools, "memory_search_ranked", broken)
    report = run_pilot(PilotConfig(profile="offline-diagnostic"))
    for result in report["final"]["case_results"]:
        assert "error" in result["variants"]["baseline"]
    assert report["recommendation"]["recommendation"] == "inconclusive"


def test_unknown_profile_falls_back_to_baseline_only(defenses_on):
    report = run_pilot(PilotConfig(profile="quantum-embedder"))
    assert report["provider"]["status"]["state"] == "unavailable"
    assert "quantum-embedder" in report["provider"]["status"]["reason"]
    assert all("semantic" not in result["variants"]
               for result in report["final"]["case_results"])


def test_rerank_disabled_omits_variant(defenses_on):
    report = run_pilot(PilotConfig(profile="offline-diagnostic", rerank_enabled=False))
    assert report["provider"]["rerank_qualification"]["state"] == "not_applicable"
    for result in report["final"]["case_results"]:
        assert "reranked" not in result["variants"]


def test_store_record_recall_and_analyzer():
    selection, final, _ = load_corpus()
    store = CorpusStore((selection[0],), hmac_key_present=False)
    assert store.record_recall("owner-atlas", "sel01-r1-latte") is True
    assert store.recall_requests == [("owner-atlas", "sel01-r1-latte")]
    assert store.record_recall("owner-other", "sel01-r1-latte") is True
    assert _CleanAnalyzer().analyze("text", "en", [], 0.5) == []
    defenses = detect_defenses()
    assert set(defenses) == {
        "temporal_filter", "memory_guard", "tamper_detection", "project_scope"}


def test_load_corpus_detects_digest_drift(monkeypatch):
    monkeypatch.setattr(corpus_module, "CORPUS_DIGEST", "0" * 64)
    with pytest.raises(corpus_module.CorpusError, match="re-pinning"):
        load_corpus()


def test_load_corpus_detects_duplicate_case_identity(monkeypatch):
    extra_key = dict(corpus_module.CASES_BY_ID)
    extra_key["ghost"] = corpus_module.ALL_CASES[0]
    monkeypatch.setattr(corpus_module, "CASES_BY_ID", extra_key)
    with pytest.raises(corpus_module.CorpusError, match="duplicate case_id"):
        load_corpus()


def test_corpus_validation_rejects_empty_query_and_rows():
    row = corpus_module.MemoryRowSpec(
        row_id="x", owner_id="owner-atlas", category="preference", value="v",
        relevance=1, rationale="r")
    with pytest.raises(corpus_module.CorpusError, match="empty query"):
        corpus_module._validate_case(
            corpus_module.MemoryCase(
                case_id="c", partition="selection", scenarios=("lexical_ambiguity",),
                owner_id="owner-atlas", query="  ", rows=(row,)), {})
    with pytest.raises(corpus_module.CorpusError, match="no rows"):
        corpus_module._validate_case(
            corpus_module.MemoryCase(
                case_id="c", partition="selection", scenarios=("lexical_ambiguity",),
                owner_id="owner-atlas", query="q", rows=()), {})


def test_corpus_validation_rejects_bad_relevance_owner_project_value():
    def row_spec(**overrides):
        fields = dict(row_id="x", owner_id="owner-atlas", category="preference",
                      value="v", relevance=1, rationale="r")
        fields.update(overrides)
        return corpus_module.MemoryRowSpec(**fields)

    with pytest.raises(corpus_module.CorpusError, match="graded 0..3"):
        corpus_module._validate_case(_case_with(row_spec(relevance=9)), {})
    with pytest.raises(corpus_module.CorpusError, match="unknown owner"):
        corpus_module._validate_case(_case_with(row_spec(owner_id="ghost")), {})
    with pytest.raises(corpus_module.CorpusError, match="unknown project"):
        corpus_module._validate_case(_case_with(row_spec(project_id="proj-x")), {})
    with pytest.raises(corpus_module.CorpusError, match="empty value"):
        corpus_module._validate_case(_case_with(row_spec(value=" ")), {})
    with pytest.raises(corpus_module.CorpusError, match="unknown forbidden_class"):
        corpus_module._validate_case(
            _case_with(row_spec(),
                       row_spec(row_id="y", relevance=0, forbidden=True,
                                forbidden_class="magic")), {})


def _case_with(*rows):
    return corpus_module.MemoryCase(
        case_id="c", partition="selection", scenarios=("lexical_ambiguity",),
        owner_id="owner-atlas", query="q", rows=tuple(rows))


def test_corpus_validation_rejects_unknown_link_reference():
    row = corpus_module.MemoryRowSpec(
        row_id="x", owner_id="owner-atlas", category="preference", value="v",
        relevance=1, rationale="r")
    with pytest.raises(corpus_module.CorpusError, match="unknown"):
        corpus_module._validate_case(
            corpus_module.MemoryCase(
                case_id="c", partition="selection", scenarios=("lexical_ambiguity",),
                owner_id="owner-atlas", query="q", rows=(row,),
                links=(("x", "ghost"),)), {})


def test_recommend_flags_missing_attestation_fields():
    cases = [{"case_id": "c", "status": "measured",
              "variants": {"baseline": {"forbidden_hits": []}}}]
    decision = recommend(
        _agg(), cases,
        {"state": "executed_unattested", "reason": "attestation rejected: missing operator"},
        {"state": "executed_unattested", "reason": "x"}, True)
    assert decision["recommendation"] == "inconclusive"


def _agg(**overrides):
    base = {
        "baseline": {"mean_recall_at_k": 0.5, "mean_ndcg_at_k": 0.5, "errors": 0,
                     "forbidden_hits": 0},
        "semantic": {"mean_recall_at_k": 0.5, "mean_ndcg_at_k": 0.5, "errors": 0,
                     "forbidden_hits": 0},
        "reranked": {"mean_recall_at_k": 0.5, "mean_ndcg_at_k": 0.5, "errors": 0,
                     "forbidden_hits": 0},
    }
    for variant, values in overrides.items():
        base[variant].update(values)
    return base


def test_recommend_rejects_unsafe_rows_with_qualified_profile():
    cases = [{
        "case_id": "c", "status": "measured",
        "forbidden_classes": {"bad": "safety"},
        "variants": {"semantic": {"forbidden_hits": ["bad"]},
                     "baseline": {"forbidden_hits": []}},
    }]
    decision = recommend(
        _agg(), cases, {"state": "qualified_manual_run"},
        {"state": "qualified_manual_run"}, True)
    assert decision["recommendation"] == "reject"
    assert "instruction-bearing" in " ".join(decision["reasons"])


def test_recommend_inconclusive_for_unsafe_rows_with_unqualified_profile():
    cases = [{
        "case_id": "c", "status": "measured",
        "forbidden_classes": {"bad": "safety"},
        "variants": {"semantic": {"forbidden_hits": ["bad"]},
                     "baseline": {"forbidden_hits": []}},
    }]
    decision = recommend(
        _agg(), cases, {"state": "executed_diagnostic_only"},
        {"state": "executed_diagnostic_only"}, True)
    assert decision["recommendation"] == "inconclusive"
    assert "not final safety evidence" in " ".join(decision["reasons"])


def test_recommend_rejects_semantic_recall_regression():
    cases = [{"case_id": "c", "status": "measured",
              "variants": {"baseline": {"forbidden_hits": []}}}]
    decision = recommend(
        _agg(semantic={"mean_recall_at_k": 0.1},
             reranked={"mean_ndcg_at_k": 0.9}),
        cases, {"state": "qualified_manual_run"}, {"state": "qualified_manual_run"}, True)
    assert decision["recommendation"] == "reject"
    assert "recall@k" in " ".join(decision["reasons"])


def test_recommend_uses_semantic_ordering_without_reranker():
    cases = [{"case_id": "c", "status": "measured",
              "variants": {"baseline": {"forbidden_hits": []}}}]
    decision = recommend(
        _agg(semantic={"mean_recall_at_k": 0.9, "mean_ndcg_at_k": 0.9}),
        cases, {"state": "qualified_manual_run"}, {"state": "not_applicable"}, False)
    assert decision["recommendation"] == "adopt"


def test_write_report_cleans_up_on_failure(tmp_path):
    from personalization.semantic_pilot.report import build_report

    report = build_report(
        corpus_identity={"corpus_id": "c", "corpus_digest": "d", "schema_version": 1,
                         "case_digests": {}, "selection_case_ids": [],
                         "final_case_ids": []},
        provider_identity=None,
        provider_status={"state": "unavailable", "reason": "r"},
        defenses={}, bounds=Bounds().__dict__.copy(),
        selection_decision={"rule": "forced", "k": 2}, selection_metrics={},
        selection_case_results=[], final_case_results=[],
        final_aggregate=_agg(), chosen_k=2, usage={}, cache_stats={},
        elapsed_seconds=0.1)
    target = tmp_path / "as-directory"
    target.mkdir()
    with pytest.raises(OSError):
        write_report(report, str(target))
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
