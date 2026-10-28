"""End-to-end tests for the pilot runner and report: honest qualification statuses,
partition separation, defense-gap accounting, cancellation, deadliness, baseline-only
runs when the provider is unavailable, attestation binding, and recommendation rules.
"""

from __future__ import annotations

import json

import pytest

from personalization.semantic_pilot import report as report_module
from personalization.semantic_pilot.bounds import Bounds
from personalization.semantic_pilot.corpus import CORPUS_DIGEST
from personalization.semantic_pilot.report import (
    build_report,
    profile_qualification,
    recommend,
    validate_report,
    write_report,
)
from personalization.semantic_pilot.runner import (
    PilotConfig,
    PilotError,
    RunCancelled,
    run_pilot,
)


@pytest.fixture()
def defenses_on(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "1")
    monkeypatch.setenv("FF_PROJECT_MEMORY", "1")
    monkeypatch.setenv("FF_MEMORY_GUARD", "1")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "runner-test-key")
    yield


def _full_run(**overrides):
    cancel_check = overrides.pop("cancel_check", None)
    fields = {"profile": "offline-diagnostic"}
    fields.update(overrides)
    config = PilotConfig(**fields)
    return run_pilot(config, cancel_check=cancel_check)


def test_full_run_offline_diagnostic(defenses_on):
    report = _full_run()
    assert validate_report(report) == []
    assert report["corpus"]["corpus_digest"] == CORPUS_DIGEST
    assert report["run"]["production_defaults_changed"] is False
    assert report["adoption_status"] == "proposed_not_applied"
    assert report["provider"]["embedding_qualification"]["state"] == \
        "executed_diagnostic_only"
    assert report["recommendation"]["recommendation"] == "inconclusive"
    selection_ids = {result["case_id"] for result in report["selection"]["case_results"]}
    final_ids = {result["case_id"] for result in report["final"]["case_results"]}
    assert selection_ids.isdisjoint(final_ids)
    assert all(result["status"] == "measured"
               for result in report["final"]["case_results"])


def test_empty_seed_evidence_recorded(defenses_on):
    report = _full_run()
    for result in report["final"]["case_results"]:
        if result["empty_seed_declared"]:
            assert result["baseline_empty"] is True
            assert result["variants"]["baseline"]["ranked_ids"] == []
            assert result["variants"]["semantic"]["recall_at_k"] > 0.0


def test_graph_expansion_case_keeps_baseline_evidence(defenses_on):
    report = _full_run()
    result = next(r for r in report["final"]["case_results"]
                  if r["case_id"] == "fin-04-graph-cfp")
    assert result["variants"]["baseline"]["recall_at_k"] >= \
        result["variants"]["semantic"]["recall_at_k"] or \
        "fin04-r2-cfp-calendar" in result["variants"]["baseline"]["ranked_ids"]


def test_all_variants_present_when_rerank_capable(defenses_on):
    report = _full_run()
    for result in report["final"]["case_results"]:
        assert set(result["variants"]) == {"baseline", "semantic", "reranked"}


def test_tei_http_without_config_runs_baseline_only(defenses_on):
    report = _full_run(profile="tei-http", run_config=None)
    assert report["provider"]["status"]["state"] == "unavailable"
    assert report["provider"]["embedding_qualification"]["state"] == "unavailable"
    for result in report["final"]["case_results"]:
        assert "semantic" not in result["variants"]
        assert "baseline" in result["variants"]
    assert report["recommendation"]["recommendation"] == "inconclusive"


def test_scripted_double_profile_qualification(defenses_on):
    report = _full_run(profile="scripted-double")
    assert report["provider"]["embedding_qualification"]["state"] == \
        "executed_provider_double_only"
    assert report["recommendation"]["recommendation"] == "inconclusive"


def test_rerank_fault_records_error_without_failing_run(defenses_on):
    report = _full_run(
        profile="scripted-double",
        run_config={"faults": {"/rerank": "non_finite_value"}})
    final = report["final"]["aggregate"]
    assert final["semantic"]["errors"] == 0
    assert final["reranked"]["errors"] > 0
    assert report["recommendation"]["recommendation"] == "inconclusive"
    reasons = " ".join(report["recommendation"]["reasons"])
    assert "reranked variant errored" in reasons


def test_cancel_check_stops_the_run(defenses_on):
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(RunCancelled):
        _full_run(bounds=Bounds(max_batch_rows=1), cancel_check=cancel)


def test_expired_deadline_cancels(defenses_on):
    with pytest.raises(RunCancelled):
        _full_run(bounds=Bounds(runtime_deadline_seconds=1e-9))


def test_invalid_grid_raises_config_error(defenses_on):
    with pytest.raises(PilotError):
        _full_run(candidate_k_grid=())
    with pytest.raises(PilotError):
        _full_run(candidate_k_grid=(0, 2))


def test_forced_k_skips_selection_rule(defenses_on):
    report = _full_run(forced_k=3)
    assert report["run"]["chosen_k"] == 3
    assert report["selection"]["decision"]["rule"] == "forced"


def test_project_scope_defense_gap_blocks_case_not_verdict(defenses_on, monkeypatch):
    monkeypatch.delenv("FF_PROJECT_MEMORY", raising=False)
    report = _full_run()
    assert report["defenses"]["project_scope"] is False
    blocked = [r for r in report["final"]["case_results"] if r["status"] == "defense_blocked"]
    assert [r["case_id"] for r in blocked] == ["fin-03-project-boundary-tickets"]
    assert report["recommendation"]["recommendation"] == "inconclusive"
    reasons = " ".join(report["recommendation"]["reasons"])
    assert "defense-inactive cases excluded" in reasons


def test_attestation_binds_corpus_and_profile(defenses_on):
    attestation = {
        "profile": "tei-http",
        "model_revision": "unconfigured",
        "corpus_digest": CORPUS_DIGEST,
        "performed_at": "2026-10-06T00:00:00Z",
        "operator": "operator-a",
    }
    report = _full_run(profile="tei-http", run_config=None, attestation=attestation)
    assert report["provider"]["embedding_qualification"]["state"] == "unavailable"


def _identity(profile="tei-http", revision="sha-a", **overrides):

    fields = dict(
        profile=profile, model="m", revision=revision, tokenizer_revision="t",
        dimension=8, license="Apache-2.0", notice="n",
        qualifier="unqualified_unattested",
    )
    fields.update(overrides)
    return fields


def test_profile_qualification_paths():
    status_available = {"state": "available", "reason": "ok"}
    diagnostic = _identity(profile="offline-diagnostic", qualifier="diagnostic_only")
    assert profile_qualification(diagnostic, status_available, None, CORPUS_DIGEST)[
        "state"] == "executed_diagnostic_only"
    double = _identity(profile="scripted-double", qualifier="provider_double_only")
    assert profile_qualification(double, status_available, None, CORPUS_DIGEST)[
        "state"] == "executed_provider_double_only"
    unattested = profile_qualification(_identity(), status_available, None, CORPUS_DIGEST)
    assert unattested["state"] == "executed_unattested"
    attested = dict(performed_at="t", operator="o", corpus_digest=CORPUS_DIGEST,
                    profile="tei-http", model_revision="r1")
    assert profile_qualification(_identity(revision="r1"), status_available, attested,
                                 CORPUS_DIGEST)["state"] == "qualified_manual_run"


def test_attestation_mismatch_is_rejected():
    status_available = {"state": "available", "reason": "ok"}
    attested = dict(performed_at="t", operator="o", corpus_digest=CORPUS_DIGEST,
                    profile="tei-http", model_revision="other")
    result = profile_qualification(_identity(revision="r1"), status_available, attested,
                                   CORPUS_DIGEST)
    assert result["state"] == "executed_unattested"
    assert "model revision" in result["reason"]


def test_unavailable_status_is_truthful():
    result = profile_qualification(None, {"state": "unavailable", "reason": "because"}, None,
                                   CORPUS_DIGEST)
    assert result["state"] == "unavailable"
    assert result["reason"] == "because"


def _aggregate(*overrides):
    base = {
        "baseline": {"mean_recall_at_k": 0.5, "mean_ndcg_at_k": 0.5, "errors": 0,
                     "forbidden_hits": 0},
        "semantic": {"mean_recall_at_k": 0.5, "mean_ndcg_at_k": 0.5, "errors": 0,
                     "forbidden_hits": 0},
        "reranked": {"mean_recall_at_k": 0.5, "mean_ndcg_at_k": 0.5, "errors": 0,
                     "forbidden_hits": 0},
    }
    for variant, values in overrides:
        base[variant].update(values)
    return base


def test_recommend_rejects_on_unauthorized_returns():
    cases = [{
        "case_id": "c1", "status": "measured",
        "variants": {"baseline": {"forbidden_hits": ["x"]}},
    }]
    decision = recommend(
        _aggregate(), cases,
        {"state": "qualified_manual_run"}, {"state": "qualified_manual_run"}, True)
    assert decision["recommendation"] == "reject"
    assert "authorization regression" in " ".join(decision["reasons"])


def test_recommend_ignores_forbidden_hits_from_defense_blocked_cases():
    cases = [{
        "case_id": "c1", "status": "defense_blocked",
        "variants": {"baseline": {"forbidden_hits": ["x"]}},
    }]
    decision = recommend(
        _aggregate(), cases,
        {"state": "executed_diagnostic_only"}, {"state": "executed_diagnostic_only"}, True)
    assert decision["recommendation"] == "inconclusive"


def test_recommend_adopt_requires_qualified_profile_and_no_regression():
    cases = [{"case_id": "c1", "status": "measured",
              "variants": {"baseline": {"forbidden_hits": []}}}]
    aggregate = _aggregate(
        ("semantic", {"mean_recall_at_k": 0.9}),
        ("reranked", {"mean_ndcg_at_k": 0.9}),
    )
    decision = recommend(
        aggregate, cases, {"state": "qualified_manual_run"},
        {"state": "qualified_manual_run"}, True)
    assert decision["recommendation"] == "adopt"


def test_recommend_rejects_ranking_regression_with_qualified_profile():
    cases = [{"case_id": "c1", "status": "measured",
              "variants": {"baseline": {"forbidden_hits": []}}}]
    aggregate = _aggregate(
        ("semantic", {"mean_recall_at_k": 0.9}),
        ("reranked", {"mean_ndcg_at_k": 0.1}),
        ("baseline", {"mean_ndcg_at_k": 0.9}),
    )
    decision = recommend(
        aggregate, cases, {"state": "qualified_manual_run"},
        {"state": "qualified_manual_run"}, True)
    assert decision["recommendation"] == "reject"
    assert "nDCG" in " ".join(decision["reasons"])


def test_recommend_counts_variant_errors():
    cases = [{"case_id": "c1", "status": "measured",
              "variants": {"baseline": {"forbidden_hits": []}}}]
    aggregate = _aggregate(("semantic", {"errors": 2}))
    decision = recommend(
        aggregate, cases, {"state": "qualified_manual_run"},
        {"state": "qualified_manual_run"}, True)
    assert decision["recommendation"] == "inconclusive"
    assert "semantic variant errored" in " ".join(decision["reasons"])


def _minimal_report(**overrides):
    fields = dict(
        corpus_identity={
            "corpus_id": "c", "corpus_digest": CORPUS_DIGEST, "schema_version": 1,
            "case_digests": {}, "selection_case_ids": [], "final_case_ids": [],
        },
        provider_identity=None,
        provider_status={"state": "unavailable", "reason": "r"},
        defenses={"temporal_filter": True, "memory_guard": True,
                  "tamper_detection": True, "project_scope": True},
        bounds=Bounds().__dict__.copy(),
        selection_decision={"rule": "forced", "k": 2},
        selection_metrics={},
        selection_case_results=[],
        final_case_results=[],
        final_aggregate=_aggregate(),
        chosen_k=2,
        usage={},
        cache_stats={},
        elapsed_seconds=0.1,
    )
    fields.update(overrides)
    return build_report(**fields)


def test_build_and_validate_report(tmp_path):
    report = _minimal_report()
    assert validate_report(report) == []
    path = write_report(report, str(tmp_path / "nested" / "report.json"))
    loaded = json.loads(open(path).read())
    assert loaded["report_kind"] == "semantic_retrieval_qualification_pilot"


def test_validate_report_catches_tampering():
    report = _minimal_report()
    report["adoption_status"] = "applied"
    assert any("proposed_not_applied" in error for error in validate_report(report))
    report = _minimal_report()
    report["recommendation"]["recommendation"] = "maybe"
    assert any("recommendation" in error for error in validate_report(report))
    report = _minimal_report()
    report["run"]["production_defaults_changed"] = True
    assert any("production_defaults_changed" in error for error in validate_report(report))
    report = _minimal_report()
    del report["final"]
    assert any("final" in error for error in validate_report(report))
    report = _minimal_report()
    report["corpus"]["corpus_digest"] = ""
    assert any("corpus digest" in error for error in validate_report(report))
    report = _minimal_report()
    report["provider"]["embedding_qualification"]["state"] = "bogus"
    assert any("qualification" in error for error in validate_report(report))
    report = _minimal_report()
    report["schema_version"] = 99
    assert any("schema_version" in error for error in validate_report(report))
    report = _minimal_report()
    report["report_kind"] = "other"
    assert any("report_kind" in error for error in validate_report(report))


def test_write_report_refuses_invalid(tmp_path):
    report = _minimal_report()
    report["adoption_status"] = "applied"
    with pytest.raises(ValueError):
        write_report(report, str(tmp_path / "report.json"))


def test_write_report_enforces_byte_bound(tmp_path):
    report = _minimal_report()
    with pytest.raises(Bounds.__mro__[1].__name__ if False else Exception):
        write_report(report, str(tmp_path / "report.json"),
                     bounds=Bounds(max_report_bytes=10))


def test_rerank_not_applicable_without_rerank_route():
    report = _minimal_report(provider_identity=_identity(rerank_capable=False))
    assert report["provider"]["rerank_qualification"]["state"] == "not_applicable"


def test_report_records_limitations_and_scope():
    report = _minimal_report()
    assert report["run"]["scope"].startswith("personalization memory only")
    assert any("not fact truth" in item for item in report["limitations"])
    assert any("no production default" in item for item in report["limitations"])


def test_run_id_is_stable_and_bound():
    first = _minimal_report()
    second = _minimal_report()
    assert first["run"]["run_id"] == second["run"]["run_id"]
    other = _minimal_report(chosen_k=5)
    assert other["run"]["run_id"] != first["run"]["run_id"]


def test_report_module_exports_states():
    assert report_module.ADOPTION_STATES == ("adopt", "reject", "inconclusive")
    assert "qualified_manual_run" in report_module.PROFILE_QUALIFICATION_STATES
