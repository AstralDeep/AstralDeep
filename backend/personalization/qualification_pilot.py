from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


def qualification_pilot_enabled() -> bool:
    """Check if the semantic memory qualification pilot flag is active."""
    return os.getenv("FF_MEMORY_QUALIFICATION_PILOT", "false").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class QualificationThresholds:
    min_ndcg_at_k: float = 0.60
    min_mrr: float = 0.50
    min_cross_entropy_gain: float = 0.05
    max_isolation_leakage_rate: float = 0.0


@dataclass
class SemanticScoringWeights:
    w_lexical: float = 0.35
    w_semantic: float = 0.35
    w_recency: float = 0.15
    w_pagerank: float = 0.10
    w_salience: float = 0.05

    def normalized(self) -> dict[str, float]:
        total = (
            self.w_lexical
            + self.w_semantic
            + self.w_recency
            + self.w_pagerank
            + self.w_salience
        )
        if total <= 0:
            return {
                "w_lexical": 0.35,
                "w_semantic": 0.35,
                "w_recency": 0.15,
                "w_pagerank": 0.10,
                "w_salience": 0.05,
            }
        return {
            "w_lexical": self.w_lexical / total,
            "w_semantic": self.w_semantic / total,
            "w_recency": self.w_recency / total,
            "w_pagerank": self.w_pagerank / total,
            "w_salience": self.w_salience / total,
        }


def compute_lexical_similarity(query: str, target: str) -> float:
    """Compute token-level Jaccard + substring containment similarity."""
    if not query or not target:
        return 0.0
    q_tokens = set(re.findall(r"\w+", query.lower()))
    t_tokens = set(re.findall(r"\w+", target.lower()))
    if not q_tokens or not t_tokens:
        return 0.0
    
    jaccard = len(q_tokens & t_tokens) / len(q_tokens | t_tokens)
    q_lower = query.lower().strip()
    t_lower = target.lower().strip()
    containment = 1.0 if q_lower in t_lower or t_lower in q_lower else 0.0
    return 0.6 * jaccard + 0.4 * containment


def compute_semantic_embedding_similarity(
    query_text: str,
    target_text: str,
    embedder: Callable[[str], Sequence[float]] | None = None,
) -> float:
    """Compute cosine similarity between query and memory item."""
    if not query_text or not target_text:
        return 0.0
    if embedder is None:
        # High-signal char n-gram cosine fallback for offline qualification
        def ngrams(text: str, n: int = 3) -> dict[str, int]:
            clean = re.sub(r"\s+", " ", text.lower().strip())
            return {clean[i:i+n]: clean.count(clean[i:i+n]) for i in range(max(0, len(clean) - n + 1))}
        
        q_ngrams = ngrams(query_text)
        t_ngrams = ngrams(target_text)
        all_keys = set(q_ngrams.keys()) | set(t_ngrams.keys())
        if not all_keys:
            return 0.0
        dot = sum(q_ngrams.get(k, 0) * t_ngrams.get(k, 0) for k in all_keys)
        norm_q = math.sqrt(sum(v * v for v in q_ngrams.values()))
        norm_t = math.sqrt(sum(v * v for v in t_ngrams.values()))
        if norm_q * norm_t == 0:
            return 0.0
        return dot / (norm_q * norm_t)

    vec_q = embedder(query_text)
    vec_t = embedder(target_text)
    dot = sum(a * b for a, b in zip(vec_q, vec_t))
    norm_a = math.sqrt(sum(a * a for a in vec_q))
    norm_b = math.sqrt(sum(b * b for b in vec_t))
    if norm_a * norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


@dataclass
class ScoredCandidate:
    item_id: str
    owner_id: str
    score: float
    lexical_score: float
    semantic_score: float
    recency_score: float
    pagerank_score: float
    salience_score: float
    item: dict[str, Any]


class OwnerScopedSemanticReranker:
    """
    Owner-scoped reranking engine enforcing hard multi-tenant isolation,
    multi-signal fusion (lexical + semantic + recency + pagerank + salience),
    and top-k qualification metrics.
    """

    def __init__(
        self,
        weights: SemanticScoringWeights | None = None,
        embedder: Callable[[str], Sequence[float]] | None = None,
        thresholds: QualificationThresholds | None = None,
    ):
        self.weights = weights or SemanticScoringWeights()
        self.embedder = embedder
        self.thresholds = thresholds or QualificationThresholds()

    def rank_memories(
        self,
        owner_id: str,
        query: str,
        items: Sequence[dict[str, Any]],
        limit: int = 10,
        enforce_owner_isolation: bool = True,
    ) -> list[dict[str, Any]]:
        """Filter by owner and rank items using composite multi-signal weights."""
        if not items:
            return []

        # 1. Enforce strict owner scoping
        if enforce_owner_isolation:
            scoped_items = [
                it for it in items
                if it.get("owner_id") == owner_id or it.get("user_id") == owner_id or it.get("owner") == owner_id
            ]
        else:
            scoped_items = list(items)

        if not scoped_items:
            return []

        total_count = len(scoped_items)
        w = self.weights.normalized()
        scored: list[ScoredCandidate] = []

        for idx, item in enumerate(scoped_items):
            item_text = str(item.get("value") or item.get("content") or item.get("text") or "")
            lexical = compute_lexical_similarity(query, item_text) if query else 0.0
            semantic = (
                compute_semantic_embedding_similarity(query, item_text, self.embedder)
                if query
                else 0.0
            )
            # Recency rank score (0 to 1)
            recency = 1.0 - (idx / (total_count - 1)) if total_count > 1 else 1.0
            pagerank = float(item.get("pagerank", item.get("rank", 0.0)))
            salience = float(item.get("salience", 0.0))

            composite = (
                w["w_lexical"] * lexical
                + w["w_semantic"] * semantic
                + w["w_recency"] * recency
                + w["w_pagerank"] * pagerank
                + w["w_salience"] * salience
            )

            scored.append(
                ScoredCandidate(
                    item_id=str(item.get("id", idx)),
                    owner_id=owner_id,
                    score=composite,
                    lexical_score=lexical,
                    semantic_score=semantic,
                    recency_score=recency,
                    pagerank_score=pagerank,
                    salience_score=salience,
                    item=item,
                )
            )

        scored.sort(key=lambda x: x.score, reverse=True)
        # If query is non-empty, filter out candidates that have 0 lexical and 0 semantic match
        if query:
            qualified_candidates = [
                c.item for c in scored
                if c.lexical_score > 0.0 or c.semantic_score > 0.05
            ]
        else:
            qualified_candidates = [c.item for c in scored]
        return qualified_candidates[:limit]

    @staticmethod
    def evaluate_ndcg_at_k(
        ranked_ids: Sequence[str],
        ground_truth_relevance: dict[str, float],
        k: int = 5,
    ) -> float:
        """Compute Normalized Discounted Cumulative Gain at K."""
        if not ranked_ids or not ground_truth_relevance or k <= 0:
            return 0.0
        
        dcg = 0.0
        for i, item_id in enumerate(ranked_ids[:k]):
            rel = ground_truth_relevance.get(item_id, 0.0)
            dcg += (2.0**rel - 1.0) / math.log2(i + 2)

        ideal_rels = sorted(ground_truth_relevance.values(), reverse=True)[:k]
        idcg = sum((2.0**rel - 1.0) / math.log2(i + 2) for i, rel in enumerate(ideal_rels))

        if idcg == 0.0:
            return 0.0
        return min(1.0, dcg / idcg)

    @staticmethod
    def evaluate_mrr(
        ranked_ids: Sequence[str],
        relevant_ids: Sequence[str],
    ) -> float:
        """Compute Mean Reciprocal Rank."""
        rel_set = set(relevant_ids)
        for rank, item_id in enumerate(ranked_ids, start=1):
            if item_id in rel_set:
                return 1.0 / rank
        return 0.0

    @staticmethod
    def verify_isolation(
        owner_id: str,
        returned_items: Sequence[dict[str, Any]],
    ) -> float:
        """Calculate owner leakage rate (must be 0.0)."""
        if not returned_items:
            return 0.0
        leaks = [
            it for it in returned_items
            if it.get("owner_id", it.get("user_id", it.get("owner", owner_id))) != owner_id
        ]
        return len(leaks) / len(returned_items)
