"""Orchestration for the qualification pilot: loads the digested corpus, runs the actual
baseline through memory_tools.py's non-reinforcing seam, tunes the candidate budget only
on selection cases, evaluates baseline/semantic/reranked variants exactly once on the
untouched final cases, and binds resource usage, defense state, and honest profile
qualification into one report.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


from . import candidates as candidates_module
from .bounds import Bounds, BoundsExceeded
from .cache import EphemeralEmbeddingCache
from .corpus import (
    SCENARIO_PROJECT_BOUNDARY,
    SCENARIO_SUPERSESSION,
    SCENARIO_TAMPERING,
    SCENARIO_TIME_BOUNDARY,
    MemoryCase,
    load_corpus,
)
from .metrics import forbidden_hits, mean, ndcg_at_k, recall_at_k
from .providers import (
    ProviderFault,
    ProviderUsage,
    RunCancelled as GatewayRunCancelled,
    VectorGateway,
    resolve_providers,
)
from .report import build_report
from .snapshot import build_tools, detect_defenses

DEFAULT_K_GRID = (2, 3, 5)
VARIANT_BASELINE = "baseline"
VARIANT_SEMANTIC = "semantic"
VARIANT_RERANKED = "reranked"

SCENARIO_DEFENSE_REQUIREMENTS = {
    SCENARIO_TAMPERING: "tamper_detection",
    SCENARIO_SUPERSESSION: "temporal_filter",
    SCENARIO_TIME_BOUNDARY: "temporal_filter",
    SCENARIO_PROJECT_BOUNDARY: "project_scope",
}

CancelCheck = Optional[Callable[[], bool]]


class PilotError(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


@dataclass
class PilotConfig:
    profile: str = "offline-diagnostic"
    run_config: Optional[Dict[str, Any]] = None
    candidate_k_grid: Tuple[int, ...] = DEFAULT_K_GRID
    forced_k: Optional[int] = None
    bounds: Bounds = field(default_factory=Bounds)
    rerank_enabled: bool = True
    attestation: Optional[Dict[str, Any]] = None


def _deadline_check(deadline: Optional[float], base_cancel: CancelCheck) -> Callable[[], bool]:
    def check() -> bool:
        if base_cancel is not None and base_cancel():
            return True
        if deadline is not None and time.monotonic() > deadline:
            return True
        return False

    return check


def _case_snapshot(case: MemoryCase) -> Dict[str, Any]:
    return {
        "case_id": case.case_id,
        "owner_id": case.owner_id,
        "project_id": case.project_id,
        "query": case.query,
        "scenarios": list(case.scenarios),
    }


def _grade_map(case: MemoryCase) -> Dict[str, int]:
    return {
        row.row_id: row.relevance
        for row in case.rows
        if row.relevance > 0 and not row.forbidden
    }


def _relevant_ids(case: MemoryCase) -> List[str]:
    return sorted(_grade_map(case))


def _forbidden_ids(case: MemoryCase) -> List[str]:
    return [row.row_id for row in case.rows if row.forbidden]


def _forbidden_classes(case: MemoryCase) -> Dict[str, str]:
    return {
        row.row_id: row.forbidden_class
        for row in case.rows if row.forbidden
    }


def _missing_defenses(case: MemoryCase, defenses: Dict[str, bool]) -> List[str]:
    return sorted({
        defense
        for scenario in case.scenarios
        for defense in (SCENARIO_DEFENSE_REQUIREMENTS.get(scenario),)
        if defense is not None and not defenses.get(defense, False)
    })


def _variant_payload(ranked_ids: List[str], scores: List[float],
                     relevant_ids: List[str], grades: Dict[str, int],
                     forbidden_ids: List[str], k: int, **extra: Any) -> Dict[str, Any]:
    payload = {
        "ranked_ids": ranked_ids,
        "scores": scores,
        "recall_at_k": recall_at_k(ranked_ids, relevant_ids, k),
        "ndcg_at_k": ndcg_at_k(ranked_ids, grades, k),
        "forbidden_hits": forbidden_hits(ranked_ids, forbidden_ids),
    }
    payload.update(extra)
    return payload


def _evaluate_case(case: MemoryCase, gateway: Optional[VectorGateway],
                   reranker: Optional[VectorGateway],
                   *, k: int, bounds: Bounds, usage: ProviderUsage,
                   cache: Optional[EphemeralEmbeddingCache], cancel_check,
                   defenses: Dict[str, bool]) -> Dict[str, Any]:
    missing = _missing_defenses(case, defenses)
    tools, store = build_tools(case)
    result: Dict[str, Any] = {
        "case_id": case.case_id,
        "snapshot": _case_snapshot(case),
        "relevant_ids": _relevant_ids(case),
        "forbidden_ids": _forbidden_ids(case),
        "forbidden_classes": _forbidden_classes(case),
        "grades": _grade_map(case),
        "status": "defense_blocked" if missing else "measured",
        "missing_defenses": missing,
        "variants": {},
        "baseline_empty": None,
        "empty_seed_declared": "empty_seed_baseline" in case.scenarios,
    }
    try:
        baseline_ranked = tools.memory_search_ranked(
            case.owner_id, case.query, limit=k, project_id=case.project_id)
    except Exception as exc:
        result["variants"][VARIANT_BASELINE] = {"error": f"{type(exc).__name__}: {exc}"}
        return result
    result["variants"][VARIANT_BASELINE] = _variant_payload(
        [row["id"] for row, _ in baseline_ranked],
        [round(score, 6) for _, score in baseline_ranked],
        _relevant_ids(case), _grade_map(case), _forbidden_ids(case), k,
    )
    result["baseline_empty"] = len(baseline_ranked) == 0
    if gateway is None:
        return result
    try:
        semantic_run = candidates_module.semantic_candidates(
            tools, store, gateway,
            user_id=case.owner_id, project_id=case.project_id, query=case.query,
            top_k=k, bounds=bounds, usage=usage, cache=cache,
            cancel_check=cancel_check)
    except GatewayRunCancelled as exc:
        raise RunCancelled(str(exc)) from None
    except (BoundsExceeded, ProviderFault) as exc:
        result["variants"][VARIANT_SEMANTIC] = {
            "error": f"{type(exc).__name__}: {exc}",
            "kind": getattr(exc, "kind", "bounds"),
        }
        return result
    result["variants"][VARIANT_SEMANTIC] = _variant_payload(
        semantic_run.ranked_ids, [score for _, score in semantic_run.ranked],
        _relevant_ids(case), _grade_map(case), _forbidden_ids(case), k,
        revalidation_drops=semantic_run.revalidation_drops,
        snapshot_rows=semantic_run.snapshot_rows,
        snapshot_bytes=semantic_run.snapshot_bytes,
    )
    if reranker is None:
        return result
    try:
        reranked_run = candidates_module.rerank_candidates(
            tools, store, reranker, semantic_run,
            user_id=case.owner_id, project_id=case.project_id, query=case.query,
            bounds=bounds, usage=usage, cancel_check=cancel_check)
    except GatewayRunCancelled as exc:
        raise RunCancelled(str(exc)) from None
    except (BoundsExceeded, ProviderFault) as exc:
        result["variants"][VARIANT_RERANKED] = {
            "error": f"{type(exc).__name__}: {exc}",
            "kind": getattr(exc, "kind", "bounds"),
        }
        return result
    result["variants"][VARIANT_RERANKED] = _variant_payload(
        reranked_run.ranked_ids, [score for _, score in reranked_run.ranked],
        _relevant_ids(case), _grade_map(case), _forbidden_ids(case), k,
        revalidation_drops=reranked_run.revalidation_drops,
        snapshot_rows=reranked_run.snapshot_rows,
        snapshot_bytes=reranked_run.snapshot_bytes,
    )
    return result


def _aggregate(case_results: List[Dict[str, Any]], k: int) -> Dict[str, Any]:
    aggregate: Dict[str, Any] = {"k": k, "cases": len(case_results)}
    for variant in (VARIANT_BASELINE, VARIANT_SEMANTIC, VARIANT_RERANKED):
        recalls: List[float] = []
        ndcgs: List[float] = []
        forbidden = 0
        errors = 0
        drops = 0
        for result in case_results:
            payload = result["variants"].get(variant)
            if payload is None:
                continue
            if "error" in payload:
                errors += 1
                continue
            recalls.append(payload["recall_at_k"])
            ndcgs.append(payload["ndcg_at_k"])
            forbidden += len(payload["forbidden_hits"])
            drops += len(payload.get("revalidation_drops") or [])
        aggregate[variant] = {
            "mean_recall_at_k": mean(recalls),
            "mean_ndcg_at_k": mean(ndcgs),
            "evaluated": len(recalls),
            "errors": errors,
            "forbidden_hits": forbidden,
            "revalidation_drops": drops,
        }
    return aggregate


def run_pilot(config: PilotConfig, *, cancel_check: CancelCheck = None) -> Dict[str, Any]:
    if not config.candidate_k_grid:
        raise PilotError("candidate_k_grid must not be empty")
    if any(k <= 0 for k in config.candidate_k_grid):
        raise PilotError("candidate_k_grid entries must be positive")
    if config.forced_k is not None and config.forced_k <= 0:
        raise PilotError("forced_k must be positive")
    selection_cases, final_cases, corpus_identity = load_corpus()
    defenses = detect_defenses()

    usage = ProviderUsage()
    cache = EphemeralEmbeddingCache()
    started = time.monotonic()
    deadline = (
        started + config.bounds.runtime_deadline_seconds
        if config.bounds.runtime_deadline_seconds is not None else None
    )
    check = _deadline_check(deadline, cancel_check)

    gateway: Optional[VectorGateway] = None
    reranker: Optional[VectorGateway] = None
    identity = None
    provider_status: Dict[str, Any] = {"state": "unavailable", "reason": "not resolved"}
    try:
        gateway, identity, provider_status = resolve_providers(config.profile, config.run_config)
    except Exception as exc:
        provider_status = {"state": "unavailable", "reason": str(exc)}
        gateway = None
        identity = None
    if provider_status.get("state") == "unavailable":
        gateway = None
    if gateway is not None and identity is not None and identity.rerank_capable \
            and config.rerank_enabled:
        reranker = gateway

    selection_grid = sorted(set(config.candidate_k_grid))
    selection_runs: Dict[int, List[Dict[str, Any]]] = {}
    selection_metrics: Dict[int, Dict[str, Any]] = {}
    for k in selection_grid:
        case_results = [
            _evaluate_case(case, gateway, reranker, k=k,
                           bounds=config.bounds, usage=usage, cache=cache,
                           cancel_check=check, defenses=defenses)
            for case in selection_cases
        ]
        selection_runs[k] = case_results
        selection_metrics[k] = _aggregate(case_results, k)

    def semantic_mean(k: int) -> float:
        return selection_metrics[k]["semantic"].get("mean_recall_at_k") or 0.0

    if config.forced_k is not None:
        chosen_k = config.forced_k
        selection_decision: Dict[str, Any] = {"rule": "forced", "k": chosen_k}
    else:
        chosen_k = max(selection_grid, key=lambda k: (semantic_mean(k), -k))
        selection_decision = {
            "rule": "max semantic mean recall@k on selection partition, ties to smaller k",
            "k": chosen_k,
            "per_k": {
                str(k): {
                    "semantic_mean_recall_at_k": semantic_mean(k),
                    "baseline_mean_recall_at_k":
                        selection_metrics[k]["baseline"].get("mean_recall_at_k"),
                }
                for k in selection_grid
            },
        }

    final_results = [
        _evaluate_case(case, gateway, reranker, k=chosen_k,
                       bounds=config.bounds, usage=usage, cache=cache,
                       cancel_check=check, defenses=defenses)
        for case in final_cases
    ]
    final_aggregate = _aggregate(final_results, chosen_k)

    elapsed = time.monotonic() - started
    return build_report(
        corpus_identity=corpus_identity,
        provider_identity=identity.to_dict() if identity else None,
        provider_status=provider_status,
        defenses=defenses,
        bounds=config.bounds.__dict__.copy(),
        selection_decision=selection_decision,
        selection_metrics={str(k): value for k, value in selection_metrics.items()},
        selection_case_results=selection_runs[chosen_k],
        final_case_results=final_results,
        final_aggregate=final_aggregate,
        chosen_k=chosen_k,
        usage=usage.to_dict(),
        cache_stats={
            "hits": cache.hits,
            "misses": cache.misses,
            "expirations": cache.expirations,
            "evictions": cache.evictions,
            "entries": len(cache),
        },
        elapsed_seconds=round(elapsed, 3),
        attestation=config.attestation,
        rerank_enabled=config.rerank_enabled,
    )
