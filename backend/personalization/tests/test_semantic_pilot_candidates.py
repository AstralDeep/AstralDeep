"""Tests for bounded candidate generation: authorization before and after inference,
revalidation of every selected identity, determinism, cache staleness/expiry, and
bound refusals.
"""

from __future__ import annotations


import pytest

from types import SimpleNamespace

from personalization.semantic_pilot.bounds import Bounds, BoundsExceeded
from personalization.semantic_pilot.cache import EphemeralEmbeddingCache
from personalization.semantic_pilot.candidates import (
    revalidate_row,
    rerank_candidates,
    semantic_candidates,
    snapshot_digest,
)
from personalization.semantic_pilot.corpus import load_corpus
from personalization.semantic_pilot.providers import (
    ProviderFault,
    ProviderIdentity,
    ProviderUsage,
    VectorGateway,
    scripted_transport,
)
from personalization.semantic_pilot.snapshot import build_tools

BOUNDS = Bounds()


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


def _identity(dimension=256, **overrides):
    fields = dict(
        profile="scripted-double", model="m", revision="r", tokenizer_revision="t",
        dimension=dimension, license="l", notice="n", qualifier="provider_double_only",
    )
    fields.update(overrides)
    return ProviderIdentity(**fields)


class _StubGateway:
    def __init__(self, vectors, identity=None):
        self.vectors = vectors
        self.identity = identity or _identity()
        self.calls = 0

    def embed(self, texts, bounds, usage, cancel_check=None):
        self.calls += 1
        return list(self.vectors)[: len(texts)]


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("MEMORY_HMAC_KEY", "candidates-test-key")
    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "1")
    monkeypatch.setenv("FF_PROJECT_MEMORY", "1")
    yield


def _case_tools(case_id):
    selection, final, _ = load_corpus()
    case = next(c for c in selection + final if c.case_id == case_id)
    tools, store = build_tools(case)
    return case, tools, store


def test_semantic_candidates_golden_deterministic(env):
    case, tools, store = _case_tools("sel-02-ambiguity-portal")
    gateway = VectorGateway(_identity(), scripted_transport(dimension=256))
    usage = ProviderUsage()
    first = semantic_candidates(
        tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=2, bounds=BOUNDS, usage=usage)
    second = semantic_candidates(
        tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=2, bounds=BOUNDS, usage=usage)
    assert first.ranked_ids == second.ranked_ids
    assert all(0.0 <= score <= 1.0 for _, score in first.ranked)
    assert set(first.ranked_ids) <= {row.row_id for row in case.rows
                                     if row.owner_id == case.owner_id}
    assert first.snapshot_rows == len([r for r in case.rows
                                       if r.owner_id == case.owner_id])


def test_snapshot_digest_changes_with_content(env):
    case, tools, store = _case_tools("sel-02-ambiguity-portal")
    rows = tools.live_memory_snapshot(case.owner_id, project_id=case.project_id)
    digest = snapshot_digest(rows)
    mutated = [dict(row) for row in rows]
    mutated[0]["value"] = mutated[0]["value"] + " changed"
    assert snapshot_digest(mutated) != digest


def test_revalidation_drops_deleted_row(env):
    case, tools, store = _case_tools("sel-02-ambiguity-portal")
    rows = tools.live_memory_snapshot(case.owner_id, project_id=case.project_id)
    store.delete_row(case.owner_id, rows[0]["id"])
    ok, reason = revalidate_row(store, case.owner_id, "proj-aurora" if case.project_id else None,
                                rows[0])
    assert not ok and reason == "row_absent"


def _store_returning(fresh):
    return SimpleNamespace(get_memory=lambda user_id, mem_id: fresh)


def test_revalidation_drops_foreign_owner(env):
    ok, reason = revalidate_row(
        _store_returning({"id": "x", "user_id": "someone-else"}),
        "owner-atlas", None, {"id": "x"})
    assert not ok and reason == "owner_mismatch"


def test_revalidation_drops_project_mismatch(env):
    fresh = {"id": "x", "user_id": "u", "project_id": "proj-borealis"}
    ok, reason = revalidate_row(_store_returning(fresh), "u", "proj-aurora", {"id": "x"})
    assert not ok and reason == "project_mismatch"


def test_revalidation_drops_tampered(env):
    fresh = {"id": "x", "user_id": "u", "signature": "f" * 64, "source": "explicit"}
    ok, reason = revalidate_row(_store_returning(fresh), "u", None, {"id": "x"})
    assert not ok and reason == "tampered"


def test_revalidation_drops_expired_validity(env):
    fresh = {"id": "x", "user_id": "u", "valid_from": 0, "valid_to": 1}
    ok, reason = revalidate_row(_store_returning(fresh), "u", None, {"id": "x"})
    assert not ok and reason == "validity_expired"


def test_revalidation_accepts_current_row(env):
    fresh = {"id": "x", "user_id": "u", "source": "explicit",
             "valid_from": 0, "valid_to": None, "project_id": None}
    ok, reason = revalidate_row(_store_returning(fresh), "u", None, {"id": "x"})
    assert ok and reason == "ok"


def test_bounds_refuse_oversized_query_and_snapshot(env):
    case, tools, store = _case_tools("sel-02-ambiguity-portal")
    gateway = VectorGateway(_identity(), scripted_transport())
    usage = ProviderUsage()
    with pytest.raises(BoundsExceeded, match="max_query_chars"):
        semantic_candidates(
            tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
            query="x" * 600, top_k=2, bounds=BOUNDS, usage=usage)
    tiny = Bounds(max_snapshot_rows=1, max_batch_rows=1, max_candidates=1)
    with pytest.raises(BoundsExceeded, match="max_snapshot_rows"):
        semantic_candidates(
            tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
            query=case.query, top_k=1, bounds=tiny, usage=usage)


def test_bounds_refuse_long_row(env):
    case, tools, store = _case_tools("sel-02-ambiguity-portal")
    gateway = VectorGateway(_identity(), scripted_transport())
    usage = ProviderUsage()
    tiny = Bounds(max_row_chars=10)
    with pytest.raises(BoundsExceeded, match="max_row_chars"):
        semantic_candidates(
            tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
            query=case.query, top_k=1, bounds=tiny, usage=usage)


def test_cache_hit_avoids_second_embed_call(env):
    case, tools, store = _case_tools("sel-01-paraphrase-drinks")
    gateway = VectorGateway(_identity(), scripted_transport())
    cache = EphemeralEmbeddingCache()
    usage = ProviderUsage()
    semantic_candidates(tools, store, gateway, user_id=case.owner_id,
                        project_id=case.project_id, query=case.query, top_k=2,
                        bounds=BOUNDS, usage=usage, cache=cache)
    calls_after_first = usage.embedding_calls
    assert cache.hits == 0
    semantic_candidates(tools, store, gateway, user_id=case.owner_id,
                        project_id=case.project_id, query=case.query, top_k=2,
                        bounds=BOUNDS, usage=usage, cache=cache)
    assert cache.hits > 0
    assert usage.embedding_calls == calls_after_first


def test_cache_expires_stale_entries(env):
    case, tools, store = _case_tools("sel-01-paraphrase-drinks")
    gateway = VectorGateway(_identity(), scripted_transport())
    now = {"t": 1000.0}
    cache = EphemeralEmbeddingCache(ttl_seconds=10.0, clock=lambda: now["t"])
    usage = ProviderUsage()
    semantic_candidates(tools, store, gateway, user_id=case.owner_id,
                        project_id=case.project_id, query=case.query, top_k=2,
                        bounds=BOUNDS, usage=usage, cache=cache)
    now["t"] = 2000.0
    cache_hits_before = cache.hits
    semantic_candidates(tools, store, gateway, user_id=case.owner_id,
                        project_id=case.project_id, query=case.query, top_k=2,
                        bounds=BOUNDS, usage=usage, cache=cache)
    assert cache.expirations > 0
    assert cache.hits == cache_hits_before


def test_cache_invalidates_by_owner_and_project():
    cache = EphemeralEmbeddingCache()
    cache.put("p", "u1", "proj-a", "src", "text", [1.0])
    cache.put("p", "u1", "proj-b", "src", "text", [2.0])
    cache.put("p", "u2", "proj-a", "src", "text", [3.0])
    assert cache.invalidate_owner("u1", "proj-a") == 1
    assert cache.get("p", "u1", "proj-a", "src", "text") is None
    assert cache.get("p", "u1", "proj-b", "src", "text") == [2.0]
    assert cache.get("p", "u2", "proj-a", "src", "text") == [3.0]
    assert cache.invalidate_owner("u1") == 1
    assert len(cache) == 1


def test_cache_evicts_oldest_when_full():
    cache = EphemeralEmbeddingCache(max_entries=2)
    cache.put("p", "u", None, "s", "t1", [1.0])
    cache.put("p", "u", None, "s", "t2", [2.0])
    cache.put("p", "u", None, "s", "t3", [3.0])
    assert cache.evictions == 1
    assert cache.get("p", "u", None, "s", "t1") is None
    assert cache.get("p", "u", None, "s", "t3") == [3.0]


def test_cache_rejects_bad_construction():
    with pytest.raises(ValueError):
        EphemeralEmbeddingCache(max_entries=0)
    with pytest.raises(ValueError):
        EphemeralEmbeddingCache(ttl_seconds=0)


def test_source_digest_change_misses_cache(env):
    case, tools, store = _case_tools("sel-01-paraphrase-drinks")
    gateway = VectorGateway(_identity(), scripted_transport())
    cache = EphemeralEmbeddingCache()
    usage = ProviderUsage()
    semantic_candidates(tools, store, gateway, user_id=case.owner_id,
                        project_id=case.project_id, query=case.query, top_k=2,
                        bounds=BOUNDS, usage=usage, cache=cache)
    rows = tools.live_memory_snapshot(case.owner_id, project_id=case.project_id)
    rows[0]["value"] += " (edited)"
    fresh_digest = snapshot_digest(rows)
    miss = cache.get(gateway.identity.digest, case.owner_id, case.project_id,
                     fresh_digest, rows[0]["value"])
    assert miss is None


def test_empty_snapshot_returns_empty_run(env):
    case, tools, store = _case_tools("sel-01-paraphrase-drinks")
    for row in list(store._rows_by_id):
        store.delete_row(case.owner_id, row)
    gateway = VectorGateway(_identity(), scripted_transport())
    usage = ProviderUsage()
    run = semantic_candidates(tools, store, gateway, user_id=case.owner_id,
                              project_id=case.project_id, query=case.query, top_k=2,
                              bounds=BOUNDS, usage=usage)
    assert run.ranked == []
    assert usage.embedding_calls == 0


def test_stub_gateway_row_count_mismatch_is_a_fault(env):
    case, tools, store = _case_tools("sel-01-paraphrase-drinks")
    gateway = _StubGateway([])
    usage = ProviderUsage()
    with pytest.raises(ProviderFault, match="row_count_mismatch"):
        semantic_candidates(tools, store, gateway, user_id=case.owner_id,
                            project_id=case.project_id, query=case.query, top_k=1,
                            bounds=BOUNDS, usage=usage)


def test_rerank_orders_only_the_authorized_candidate_set(env):
    case, tools, store = _case_tools("sel-02-ambiguity-portal")
    gateway = VectorGateway(_identity(rerank_model="m", rerank_revision="r",
                                      rerank_capable=True),
                            scripted_transport(dimension=256))
    usage = ProviderUsage()
    candidates = semantic_candidates(
        tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=2, bounds=BOUNDS, usage=usage)
    reranked = rerank_candidates(
        tools, store, gateway, candidates, user_id=case.owner_id,
        project_id=case.project_id, query=case.query, bounds=BOUNDS, usage=usage)
    assert set(reranked.ranked_ids) == set(candidates.ranked_ids)
    assert reranked.snapshot_digest == candidates.snapshot_digest


def test_rerank_cannot_inject_foreign_index(env):
    case, tools, store = _case_tools("sel-02-ambiguity-portal")
    gateway = VectorGateway(_identity(rerank_model="m", rerank_revision="r",
                                      rerank_capable=True),
                            scripted_transport(
                                dimension=256,
                                faults={"/rerank": "out_of_range_index"}))
    usage = ProviderUsage()
    candidates = semantic_candidates(
        tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=2, bounds=BOUNDS, usage=usage)
    with pytest.raises(ProviderFault, match="out_of_range_index"):
        rerank_candidates(tools, store, gateway, candidates, user_id=case.owner_id,
                          project_id=case.project_id, query=case.query,
                          bounds=BOUNDS, usage=usage)


def test_rerank_of_empty_candidates_makes_no_call(env):
    case, tools, store = _case_tools("sel-01-paraphrase-drinks")
    gateway = VectorGateway(_identity(rerank_model="m", rerank_revision="r",
                                      rerank_capable=True),
                            scripted_transport())
    usage = ProviderUsage()
    candidates = semantic_candidates(
        tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=2, bounds=BOUNDS, usage=usage)
    for row in list(store._rows_by_id):
        store.delete_row(case.owner_id, row)
    candidates.ranked = []
    reranked = rerank_candidates(tools, store, gateway, candidates,
                                 user_id=case.owner_id, project_id=case.project_id,
                                 query=case.query, bounds=BOUNDS, usage=usage)
    assert reranked.ranked == []
    assert usage.rerank_calls == 0
