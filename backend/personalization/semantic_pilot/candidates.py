"""Bounded semantic candidate generation and optional reranking over an already
authorized owner/project snapshot: live, tamper, and temporal filters run before any
inference, provider indices are validated against exactly that snapshot, and every
selected identity is revalidated against the store before return, so no model response
can add a foreign, stale, deleted, or otherwise unauthorized row.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from personalization import living_memory as lm
from personalization import memory_guard
from personalization import project_scope as ps

from .bounds import Bounds
from .cache import EphemeralEmbeddingCache
from .providers import ProviderFault, ProviderUsage, RunCancelled, VectorGateway

Revalidate = Tuple[bool, str]


def _read_scope(project_id: Optional[str]) -> Optional[str]:
    if not ps.project_scope_enabled():
        return None
    return ps.normalize_project(project_id)


@dataclass
class CandidateRun:
    variant: str
    ranked: List[Tuple[Dict[str, Any], float]]
    snapshot_digest: str
    usage: ProviderUsage
    faults: List[Dict[str, Any]] = field(default_factory=list)
    revalidation_drops: List[Dict[str, Any]] = field(default_factory=list)
    cache_hits: int = 0
    snapshot_rows: int = 0
    snapshot_bytes: int = 0

    @property
    def ranked_ids(self) -> List[str]:
        return [row["id"] for row, _ in self.ranked]


def snapshot_digest(rows: List[Dict[str, Any]]) -> str:
    material = [
        {
            "id": row.get("id"),
            "user_id": row.get("user_id"),
            "project_id": row.get("project_id"),
            "value": row.get("value"),
            "valid_from": row.get("valid_from"),
            "valid_to": row.get("valid_to"),
            "signature": row.get("signature"),
            "updated_at": row.get("updated_at"),
            "category": row.get("category"),
            "source": row.get("source"),
            "superseded_by": row.get("superseded_by"),
            "superseded_at": row.get("superseded_at"),
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(0.0, min(1.0, dot))


def revalidate_row(store: Any, user_id: str, scope: Optional[str], row: Dict[str, Any]) -> Revalidate:
    row_id = row.get("id")
    fresh = store.get_memory(user_id, str(row_id))
    if fresh is None:
        return False, "row_absent"
    if fresh.get("user_id") != user_id:
        return False, "owner_mismatch"
    if scope is not None and not ps.visible_in(fresh, scope):
        return False, "project_mismatch"
    if memory_guard.guard_enabled() and memory_guard.trust_of(fresh) == "tampered":
        return False, "tampered"
    if lm.temporal_enabled() and not lm.is_valid_at(fresh, int(time.time() * 1000)):
        return False, "validity_expired"
    if snapshot_digest([fresh]) != snapshot_digest([row]):
        return False, "row_changed"
    return True, "ok"


def _vectors_for_texts(gateway: VectorGateway, cache: Optional[EphemeralEmbeddingCache],
                       identity_digest: str, user_id: str, project_scope: Optional[str],
                       source_digest: str, texts: List[str], bounds: Bounds,
                       usage: ProviderUsage, cancel_check) -> List[List[float]]:
    vectors: List[Optional[List[float]]] = [None] * len(texts)
    missing: List[Tuple[int, str]] = []
    for index, text in enumerate(texts):
        if cancel_check is not None and cancel_check():
            raise RunCancelled("cancelled during cache lookup")
        if cache is not None:
            cached = cache.get(identity_digest, user_id, project_scope,
                               source_digest, text)
            if cached is not None:
                vectors[index] = cached
                continue
        missing.append((index, text))
    if missing:
        computed = gateway.embed([text for _, text in missing], bounds, usage, cancel_check)
        if len(computed) != len(missing):
            raise ProviderFault(
                "row_count_mismatch",
                f"gateway returned {len(computed)} vectors for {len(missing)} texts")
        for (index, text), vector in zip(missing, computed):
            vectors[index] = vector
            if cache is not None:
                cache.put(identity_digest, user_id, project_scope,
                          source_digest, text, vector)
    return [v for v in vectors if v is not None]


def semantic_candidates(tools: Any, store: Any, gateway: VectorGateway,
                        *, user_id: str, project_id: Optional[str], query: str,
                        top_k: int, bounds: Bounds, usage: ProviderUsage,
                        cache: Optional[EphemeralEmbeddingCache] = None,
                        cancel_check=None) -> CandidateRun:
    scope = _read_scope(project_id)
    rows = tools.live_memory_snapshot(user_id, project_id=project_id)
    bounds.validate_query(query)
    snapshot_bytes = bounds.validate_snapshot(rows)
    bounds.validate_candidates(top_k)
    digest = snapshot_digest(rows)
    run = CandidateRun(
        variant="semantic",
        ranked=[],
        snapshot_digest=digest,
        usage=usage,
        snapshot_rows=len(rows),
        snapshot_bytes=snapshot_bytes,
    )
    if not rows:
        return run
    texts = [str(row.get("value") or "") for row in rows]
    vectors = _vectors_for_texts(
        gateway, cache, gateway.identity.digest, user_id, scope, digest,
        [query] + texts, bounds, usage, cancel_check)
    query_vector = vectors[0]
    row_vectors = vectors[1:]
    scored: List[Tuple[float, int]] = []
    for index, row_vector in enumerate(row_vectors):
        scored.append((_cosine(query_vector, row_vector), index))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    selected = scored[:top_k]
    for score, index in selected:
        row = rows[index]
        ok, reason = revalidate_row(store, user_id, scope, row)
        if not ok:
            run.revalidation_drops.append({"id": row.get("id"), "reason": reason})
            continue
        run.ranked.append((row, round(score, 6)))
    return run


def rerank_candidates(tools: Any, store: Any, gateway: VectorGateway,
                      candidates: CandidateRun, *, user_id: str, project_id: Optional[str],
                      query: str, bounds: Bounds, usage: ProviderUsage,
                      cancel_check=None) -> CandidateRun:
    scope = _read_scope(project_id)
    run = CandidateRun(
        variant="reranked",
        ranked=[],
        snapshot_digest=candidates.snapshot_digest,
        usage=usage,
        snapshot_rows=candidates.snapshot_rows,
        snapshot_bytes=candidates.snapshot_bytes,
    )
    current = []
    for row, score in candidates.ranked:
        ok, reason = revalidate_row(store, user_id, scope, row)
        if not ok:
            run.revalidation_drops.append({"id": row.get("id"), "reason": reason})
            continue
        current.append((row, score))
    if not current:
        return run
    texts = [str(row.get("value") or "") for row, _ in current]
    bounds.validate_candidates(len(texts))
    order = gateway.rerank(query, texts, bounds, usage, cancel_check)
    for index, score in order:
        row = current[index][0]
        ok, reason = revalidate_row(store, user_id, scope, row)
        if not ok:
            run.revalidation_drops.append({"id": row.get("id"), "reason": reason})
            continue
        run.ranked.append((row, round(score, 6)))
    return run
