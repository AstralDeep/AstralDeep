"""Pure recency×importance×relevance composite scoring for memory rows, normalizing each
signal to [0,1] with renormalized weights; used by memory_tools.py's memory_search to
rank durable memory without a vector DB.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

DEFAULT_WEIGHTS: Dict[str, float] = {"recency": 0.34, "importance": 0.33, "relevance": 0.33}


def multisignal_enabled() -> bool:
    return os.getenv("FF_MEMORY_MULTISIGNAL", "true").strip().lower() not in ("0", "false", "no", "off")


def _clamp01(x: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def recency_from_rank(index: int, total: int) -> float:
    if total <= 1:
        return 1.0
    return round(1.0 - (index / (total - 1)), 6)


def relevance_from_overlap(overlap: int, query_size: int) -> float:
    if query_size <= 0:
        return 0.0
    return round(min(1.0, overlap / query_size), 6)


def importance_signal(salience: float = 0.0, source: Optional[str] = None) -> float:
    s = _clamp01(salience)
    if s > 0:
        return s
    src = str(source or "").lower()
    if src == "explicit":
        return 0.7
    if src == "promoted":
        return 0.5
    return 0.5


def multi_signal_score(
    *,
    recency: float,
    importance: float,
    relevance: float,
    weights: Dict[str, float] = None,
) -> float:
    w = weights or DEFAULT_WEIGHTS
    signals = {
        "recency": _clamp01(recency),
        "importance": _clamp01(importance),
        "relevance": _clamp01(relevance),
    }
    total = sum(w.get(k, 0.0) for k in signals)
    if total <= 0:
        return 0.0
    return round(sum(signals[k] * w.get(k, 0.0) for k in signals) / total, 6)


def score_memory_row(row: Dict[str, Any], *, index: int, total: int,
                     overlap: int, query_size: int, weights: Dict[str, float] = None) -> float:
    return multi_signal_score(
        recency=recency_from_rank(index, total),
        importance=importance_signal(row.get("salience", 0.0), row.get("source")),
        relevance=relevance_from_overlap(overlap, query_size),
        weights=weights,
    )
