"""Retrieval-quality measures for the pilot: graded recall@K and nDCG@K with explicit
relevant/forbidden accounting, kept separate so an ordering gain can never conceal a
candidate miss or an authorization regression.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: Sequence[str], k: int) -> float:
    if k <= 0:
        return 0.0
    if not relevant_ids:
        return 0.0
    top = set(ranked_ids[:k])
    hits = sum(1 for rid in relevant_ids if rid in top)
    return round(hits / len(relevant_ids), 6)


def ndcg_at_k(ranked_ids: Sequence[str], grades: Dict[str, int], k: int) -> float:
    if k <= 0 or not grades:
        return 0.0
    dcg = 0.0
    for position, rid in enumerate(ranked_ids[:k]):
        gain = grades.get(rid, 0)
        if gain > 0:
            dcg += (2 ** gain - 1) / _log2(position + 2)
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / _log2(i + 2) for i, g in enumerate(ideal) if g > 0)
    if idcg <= 0:
        return 0.0
    return round(dcg / idcg, 6)


def _log2(value: int) -> float:
    if value <= 1:
        return 0.0
    approx = 0
    v = value
    while v > 1:
        v //= 2
        approx += 1
    if 2 ** approx == value:
        return float(approx)
    return approx + math.log2(value / (2 ** approx))


def forbidden_hits(ranked_ids: Sequence[str], forbidden_ids: Sequence[str]) -> List[str]:
    forbidden = set(forbidden_ids)
    return [rid for rid in ranked_ids if rid in forbidden]


def mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 6)
