"""Pure, dependency-free trajectory-evaluation metrics (exact/in-order/any-order match,
precision, recall) and the pass^k reliability estimator, comparing an agent's
tool-call sequence to a reference. Feeds feedback/quality.py's scoring job.
"""

from __future__ import annotations

import os
from math import comb
from typing import Any, Dict, List, Sequence, Union

ToolCall = Union[str, Dict[str, Any]]


def agent_eval_enabled() -> bool:
    return os.getenv("FF_AGENT_EVAL", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _tool_name(call: ToolCall) -> str:
    if isinstance(call, str):
        return call
    if isinstance(call, dict):
        return str(call.get("tool") or call.get("name") or call.get("tool_name") or "")
    return str(call)


def _names(trajectory: Sequence[ToolCall]) -> List[str]:
    return [_tool_name(c) for c in (trajectory or [])]


def trajectory_exact_match(predicted: Sequence[ToolCall], reference: Sequence[ToolCall]) -> float:
    return 1.0 if _names(predicted) == _names(reference) else 0.0


def trajectory_in_order_match(predicted: Sequence[ToolCall], reference: Sequence[ToolCall]) -> float:
    pred_iter = iter(_names(predicted))
    return 1.0 if all(name in pred_iter for name in _names(reference)) else 0.0


def trajectory_any_order_match(predicted: Sequence[ToolCall], reference: Sequence[ToolCall]) -> float:
    return 1.0 if set(_names(reference)) <= set(_names(predicted)) else 0.0


def trajectory_precision(predicted: Sequence[ToolCall], reference: Sequence[ToolCall]) -> float:
    pred, ref = set(_names(predicted)), set(_names(reference))
    return (len(pred & ref) / len(pred)) if pred else 0.0


def trajectory_recall(predicted: Sequence[ToolCall], reference: Sequence[ToolCall]) -> float:
    pred, ref = set(_names(predicted)), set(_names(reference))
    return (len(pred & ref) / len(ref)) if ref else 0.0


def trajectory_single_tool_use(predicted: Sequence[ToolCall], tool_name: str) -> float:
    return 1.0 if tool_name in set(_names(predicted)) else 0.0


def score_trajectory(predicted: Sequence[ToolCall], reference: Sequence[ToolCall]) -> Dict[str, float]:
    return {
        "exact_match": trajectory_exact_match(predicted, reference),
        "in_order_match": trajectory_in_order_match(predicted, reference),
        "any_order_match": trajectory_any_order_match(predicted, reference),
        "precision": trajectory_precision(predicted, reference),
        "recall": trajectory_recall(predicted, reference),
    }


DEFAULT_QUALITY_WEIGHTS: Dict[str, float] = {
    "in_order_match": 0.4, "recall": 0.35, "precision": 0.25,
}


def aggregate_quality(scores: Dict[str, float], weights: Dict[str, float] = None) -> float:
    w = weights or DEFAULT_QUALITY_WEIGHTS
    present = {k: v for k, v in w.items() if k in scores}
    total = sum(present.values())
    if total <= 0:
        return 0.0
    return round(sum(scores[k] * wt for k, wt in present.items()) / total, 4)


def pass_hat_k(num_trials: int, num_successes: int, k: int) -> float:
    if k < 1:
        raise ValueError("k must be >= 1")
    if num_trials < 0 or num_successes < 0 or num_successes > num_trials:
        raise ValueError("require 0 <= num_successes <= num_trials")
    if num_trials < k or num_successes < k:
        return 0.0
    denom = comb(num_trials, k)
    return round(comb(num_successes, k) / denom, 6) if denom else 0.0


def pass_k_from_outcomes(outcomes: Sequence[bool], k: int) -> float:
    outs = list(outcomes)
    return pass_hat_k(len(outs), sum(1 for o in outs if o), k)


def score_trajectory_batch(
    pairs: Sequence[Sequence[Sequence[ToolCall]]],
    weights: Dict[str, float] = None,
) -> Dict[str, Any]:
    items = [p for p in (pairs or []) if p and len(p) == 2]
    n = len(items)
    if n == 0:
        return {
            "trajectory_count": 0,
            "mean_quality": 0.0,
            "exact_match_rate": 0.0,
            "metric_means": {},
        }

    per_scores: List[Dict[str, float]] = []
    aggregates: List[float] = []
    for predicted, reference in items:
        s = score_trajectory(predicted, reference)
        per_scores.append(s)
        aggregates.append(aggregate_quality(s, weights))

    metric_keys = sorted({k for s in per_scores for k in s})
    metric_means = {
        k: round(sum(s.get(k, 0.0) for s in per_scores) / n, 4) for k in metric_keys
    }
    exact = sum(1 for s in per_scores if s.get("exact_match", 0.0) >= 1.0)
    return {
        "trajectory_count": n,
        "mean_quality": round(sum(aggregates) / n, 4),
        "exact_match_rate": round(exact / n, 4),
        "metric_means": metric_means,
    }
