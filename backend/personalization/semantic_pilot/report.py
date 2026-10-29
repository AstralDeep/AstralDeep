"""Candidate-bound report export for the pilot: honest qualification statuses, separate
baseline/semantic/reranked metrics, resource inventory, decision rules, and an explicit
adoption/rejection/inconclusive recommendation; reports are data only and never loaded
as code.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from .bounds import Bounds, BoundsExceeded

REPORT_SCHEMA_VERSION = 1
ADOPTION_STATES = ("adopt", "reject", "inconclusive")
PROFILE_QUALIFICATION_STATES = (
    "qualified_manual_run",
    "executed_diagnostic_only",
    "executed_provider_double_only",
    "executed_unattested",
    "unavailable",
    "not_applicable",
)
PRODUCTION_DEFAULTS_CHANGED = False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def profile_qualification(identity: Optional[Dict[str, Any]],
                          status: Dict[str, Any],
                          attestation: Optional[Dict[str, Any]],
                          corpus_digest: str) -> Dict[str, Any]:
    if identity is None or status.get("state") == "unavailable":
        return {
            "state": "unavailable",
            "reason": status.get("reason", "provider not resolved"),
        }
    if identity.get("qualifier") == "diagnostic_only":
        return {
            "state": "executed_diagnostic_only",
            "reason": "deterministic offline diagnostic executed; not model qualification",
        }
    if identity.get("qualifier") == "provider_double_only":
        return {
            "state": "executed_provider_double_only",
            "reason": "scripted double executed for CI; doubles cannot establish "
                      "model/runtime qualification",
        }
    if attestation is None:
        return {
            "state": "executed_unattested",
            "reason": "pilot executed against the configured endpoint, but no operator "
                      "attestation of a bounded manual local run was provided",
        }
    problems = _attestation_problems(attestation, identity, corpus_digest)
    if problems:
        return {
            "state": "executed_unattested",
            "reason": "attestation rejected: " + "; ".join(problems),
        }
    return {
        "state": "qualified_manual_run",
        "reason": "operator-attested bounded manual local run bound to this corpus and "
                  "profile; not independently re-executed by CI",
        "attestation_digest": _sha256(
            json.dumps(attestation, sort_keys=True, separators=(",", ":"))),
    }


def _attestation_problems(attestation: Dict[str, Any], identity: Dict[str, Any],
                          corpus_digest: str) -> List[str]:
    problems: List[str] = []
    for field_name in ("profile", "model_revision", "corpus_digest", "performed_at",
                       "operator", "provider_identity_digest"):
        if not attestation.get(field_name):
            problems.append(f"missing {field_name}")
    if attestation.get("profile") != identity.get("profile"):
        problems.append("attestation profile differs from the executed profile")
    if attestation.get("model_revision") != identity.get("revision"):
        problems.append("attestation model revision differs from the executed revision")
    if attestation.get("corpus_digest") != corpus_digest:
        problems.append("attestation corpus digest differs from the executed corpus")
    identity_digest = _sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    if attestation.get("provider_identity_digest") != identity_digest:
        problems.append("attestation provider identity digest differs from the executed profile")
    return problems


def recommend(final_aggregate: Dict[str, Any],
              case_results: List[Dict[str, Any]],
              embedding_qualification: Dict[str, Any],
              rerank_qualification: Dict[str, Any],
              rerank_available: bool) -> Dict[str, Any]:
    reasons: List[str] = []
    unauthorized = 0
    unsafe_qualified = 0
    unsafe_unqualified = 0
    defense_blocked_only = 0
    qualified = embedding_qualification["state"] == "qualified_manual_run"
    for result in case_results:
        defense_blocked = result.get("status") == "defense_blocked"
        classes = result.get("forbidden_classes") or {}
        for variant, payload in result.get("variants", {}).items():
            if "error" in payload:
                continue
            for hit in payload.get("forbidden_hits") or []:
                hit_class = classes.get(hit, "authorization")
                if defense_blocked:
                    defense_blocked_only += 1
                elif hit_class == "authorization":
                    unauthorized += 1
                elif qualified:
                    unsafe_qualified += 1
                else:
                    unsafe_unqualified += 1
    if unauthorized:
        reasons.append(
            f"{unauthorized} authorization-forbidden rows returned with the relevant "
            "defense active; authorization regression")
    semantic = final_aggregate["semantic"]
    baseline = final_aggregate["baseline"]
    reranked = final_aggregate["reranked"]
    semantic_errors = semantic.get("errors", 0)
    rerank_errors = reranked.get("errors", 0) if rerank_available else 0
    if semantic_errors:
        reasons.append(f"semantic variant errored on {semantic_errors} final cases")
    if rerank_errors:
        reasons.append(f"reranked variant errored on {rerank_errors} final cases")
    if embedding_qualification["state"] not in ("qualified_manual_run",):
        reasons.append(
            f"embedding profile {embedding_qualification['state']}: no qualified "
            "model/runtime evidence")
    blocked_cases = [r["case_id"] for r in case_results if r.get("status") == "defense_blocked"]
    if blocked_cases:
        reasons.append(
            "defense-inactive cases excluded from the verdict: " + ", ".join(blocked_cases))

    if unauthorized:
        return {"recommendation": "reject", "reasons": reasons}
    if unsafe_qualified:
        reasons.append(
            f"{unsafe_qualified} instruction-bearing rows surfaced as candidates with a "
            "qualified model; unsafe for personalization guidance")
        return {"recommendation": "reject", "reasons": reasons}
    if (blocked_cases or not qualified or semantic_errors or rerank_errors
            or unsafe_unqualified):
        if unsafe_unqualified:
            reasons.append(
                f"{unsafe_unqualified} instruction-bearing rows surfaced by an "
                "unqualified profile; not final safety evidence")
        return {"recommendation": "inconclusive", "reasons": reasons}

    semantic_recall = semantic.get("mean_recall_at_k") or 0.0
    baseline_recall = baseline.get("mean_recall_at_k") or 0.0
    if rerank_available:
        ordering = reranked.get("mean_ndcg_at_k") or 0.0
        baseline_ordering = baseline.get("mean_ndcg_at_k") or 0.0
    else:
        ordering = semantic.get("mean_ndcg_at_k") or 0.0
        baseline_ordering = baseline.get("mean_ndcg_at_k") or 0.0
    if semantic_recall + 1e-9 < baseline_recall:
        reasons.append(
            f"semantic candidate recall@k {semantic_recall} is below the lexical "
            f"baseline {baseline_recall}")
    if ordering + 1e-9 < baseline_ordering:
        reasons.append(
            f"final ordering nDCG@k {ordering} is below the lexical baseline "
            f"{baseline_ordering}")
    if reasons:
        return {"recommendation": "reject", "reasons": reasons}
    reasons.append(
        "qualified model profile with candidate recall and ordering at or above the "
        "lexical baseline on the untouched final partition")
    return {"recommendation": "adopt", "reasons": reasons}


def build_report(*, corpus_identity: Dict[str, Any],
                 provider_identity: Optional[Dict[str, Any]],
                 provider_status: Dict[str, Any],
                 defenses: Dict[str, bool],
                 bounds: Dict[str, Any],
                 selection_decision: Dict[str, Any],
                 selection_metrics: Dict[str, Any],
                 selection_case_results: List[Dict[str, Any]],
                 final_case_results: List[Dict[str, Any]],
                 final_aggregate: Dict[str, Any],
                 chosen_k: int,
                 usage: Dict[str, Any],
                 cache_stats: Dict[str, Any],
                 elapsed_seconds: float,
                 attestation: Optional[Dict[str, Any]] = None,
                 rerank_enabled: bool = True) -> Dict[str, Any]:
    corpus_digest = corpus_identity["corpus_digest"]
    embedding_qualification = profile_qualification(
        provider_identity, provider_status, attestation, corpus_digest)
    rerank_available = bool((provider_identity or {}).get("rerank_capable")) and rerank_enabled
    if rerank_available:
        rerank_qualification = embedding_qualification
    else:
        rerank_qualification = {
            "state": "not_applicable",
            "reason": "no rerank route configured for this profile",
        }
    recommendation = recommend(
        final_aggregate, final_case_results, embedding_qualification,
        rerank_qualification, rerank_available)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_kind": "semantic_retrieval_qualification_pilot",
        "run": {
            "run_id": _sha256(f"{corpus_digest}\x1f{provider_status.get('reason', '')}"
                              f"\x1f{chosen_k}\x1f{elapsed_seconds}")[:32],
            "elapsed_seconds": elapsed_seconds,
            "chosen_k": chosen_k,
            "scope": "personalization memory only; distinct from exact tool-observation "
                     "recall (feature-093)",
            "production_defaults_changed": PRODUCTION_DEFAULTS_CHANGED,
            "data_kind": "synthetic non-PHI",
        },
        "corpus": corpus_identity,
        "provider": {
            "identity": provider_identity,
            "status": provider_status,
            "embedding_qualification": embedding_qualification,
            "rerank_qualification": rerank_qualification,
        },
        "defenses": defenses,
        "bounds": bounds,
        "selection": {
            "decision": selection_decision,
            "metrics": selection_metrics,
            "case_results": selection_case_results,
        },
        "final": {
            "aggregate": final_aggregate,
            "case_results": final_case_results,
        },
        "resources": {
            "usage": usage,
            "cache": cache_stats,
        },
        "recommendation": recommendation,
        "adoption_status": "proposed_not_applied",
        "limitations": [
            "Relevance labels are evidence about retrieval ordering, not fact truth, "
            "calibrated medical certainty, or permission.",
            "Provider doubles and the offline diagnostic cannot establish model/runtime "
            "qualification; only an attested bounded manual local run can.",
            "Durable adoption (vector store, indexing, recall policy) belongs in "
            "separately qualified work; this pilot changes no production default.",
        ],
    }


def validate_report(report: Dict[str, Any], bounds: Optional[Bounds] = None) -> List[str]:
    errors: List[str] = []
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        errors.append("unsupported report schema_version")
    if report.get("report_kind") != "semantic_retrieval_qualification_pilot":
        errors.append("unexpected report_kind")
    run = report.get("run") or {}
    if run.get("production_defaults_changed") is not False:
        errors.append("production_defaults_changed must be false")
    if report.get("adoption_status") != "proposed_not_applied":
        errors.append("adoption_status must remain proposed_not_applied")
    recommendation = (report.get("recommendation") or {}).get("recommendation")
    if recommendation not in ADOPTION_STATES:
        errors.append("recommendation must be adopt, reject, or inconclusive")
    provider = report.get("provider") or {}
    if provider.get("embedding_qualification", {}).get("state") not in PROFILE_QUALIFICATION_STATES:
        errors.append("unknown embedding qualification state")
    corpus = report.get("corpus") or {}
    if not corpus.get("corpus_digest"):
        errors.append("report is not bound to a corpus digest")
    for section in ("selection", "final"):
        if section not in report:
            errors.append(f"missing {section} section")
    if bounds is not None:
        try:
            bounds.validate_report_bytes(len(json.dumps(report)))
        except BoundsExceeded as exc:
            errors.append(str(exc))
    return errors


def write_report(report: Dict[str, Any], path: str, *, bounds: Optional[Bounds] = None) -> str:
    errors = validate_report(report, bounds)
    if errors:
        raise ValueError("refusing to write an invalid report: " + "; ".join(errors))
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    encoded = payload.encode("utf-8")
    if bounds is not None:
        bounds.validate_report_bytes(len(encoded))
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    return path
