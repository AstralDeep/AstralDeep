from __future__ import annotations

import unittest
from unittest.mock import patch

from personalization.qualification_pilot import (
    OwnerScopedSemanticReranker,
    QualificationThresholds,
    ScoredCandidate,
    SemanticScoringWeights,
    compute_lexical_similarity,
    compute_semantic_embedding_similarity,
    qualification_pilot_enabled,
)


class TestQualificationPilotExtended(unittest.TestCase):
    def test_feature_gate(self):
        with patch.dict("os.environ", {"FF_MEMORY_QUALIFICATION_PILOT": "true"}):
            self.assertTrue(qualification_pilot_enabled())
        with patch.dict("os.environ", {"FF_MEMORY_QUALIFICATION_PILOT": "false"}):
            self.assertFalse(qualification_pilot_enabled())

    def test_weights_and_thresholds(self):
        weights = SemanticScoringWeights(w_lexical=1.0, w_semantic=1.0, w_recency=1.0, w_pagerank=1.0, w_salience=1.0)
        norm = weights.normalized()
        self.assertAlmostEqual(norm["w_lexical"], 0.2)

        zero_weights = SemanticScoringWeights(0, 0, 0, 0, 0)
        self.assertEqual(zero_weights.normalized()["w_lexical"], 0.35)

        thresh = QualificationThresholds()
        self.assertEqual(thresh.max_isolation_leakage_rate, 0.0)

    def test_lexical_and_semantic_similarity(self):
        self.assertEqual(compute_lexical_similarity("", "abc"), 0.0)
        self.assertEqual(compute_lexical_similarity("abc", ""), 0.0)
        self.assertEqual(compute_lexical_similarity("   ", "   "), 0.0)
        sim = compute_lexical_similarity("python async fast", "python async programming")
        self.assertGreater(sim, 0.25)

        self.assertEqual(compute_semantic_embedding_similarity("", "abc"), 0.0)
        self.assertEqual(compute_semantic_embedding_similarity("abc", ""), 0.0)
        self.assertEqual(compute_semantic_embedding_similarity("   ", "   "), 0.0)
        sem = compute_semantic_embedding_similarity("database migration", "sql schema update")
        self.assertGreater(sem, 0.0)

        # Mock custom embedder
        mock_embedder = lambda s: [1.0, 0.0] if "database" in s else [0.0, 1.0]
        custom_sim = compute_semantic_embedding_similarity("database test", "database prod", embedder=mock_embedder)
        self.assertAlmostEqual(custom_sim, 1.0)
        zero_sim = compute_semantic_embedding_similarity("zero", "zero", embedder=lambda s: [0.0, 0.0])
        self.assertEqual(zero_sim, 0.0)

    def test_owner_scoped_isolation_and_evaluation(self):
        reranker = OwnerScopedSemanticReranker()
        items = [
            {"id": "m1", "user_id": "user_a", "content": "user A memory 1"},
            {"id": "m2", "owner_id": "user_b", "content": "user B memory 2"},
            {"id": "m3", "owner": "user_a", "content": "user A memory 3"},
            {"id": "m4", "user_id": "user_c", "content": "user C memory 4"},
            {"id": "m5", "content": "unscoped memory"},
        ]

        # Scope for user_a
        ranked_a = reranker.rank_memories("user_a", "memory", items)
        for it in ranked_a:
            owner = it.get("user_id") or it.get("owner_id") or it.get("owner")
            self.assertEqual(owner, "user_a")

        # Isolation helper
        self.assertEqual(reranker.verify_isolation("user_a", ranked_a), 0.0)
        self.assertGreater(reranker.verify_isolation("user_a", items), 0.0)
        self.assertEqual(reranker.verify_isolation("user_a", []), 0.0)

        # Unenforced isolation branch
        unscoped_ranked = reranker.rank_memories("user_a", "memory", items, enforce_owner_isolation=False)
        self.assertGreater(len(unscoped_ranked), len(ranked_a))

        # Empty returns
        self.assertEqual(reranker.rank_memories("non_existent_user", "query", items), [])
        self.assertEqual(reranker.rank_memories("user_a", "query", []), [])

    def test_ndcg_and_mrr_metrics(self):
        reranker = OwnerScopedSemanticReranker()

        # NDCG@K
        ground_truth = {"doc1": 3.0, "doc2": 2.0, "doc3": 1.0}
        ranked_ids = ["doc1", "doc2", "doc3"]
        ndcg_perfect = reranker.evaluate_ndcg_at_k(ranked_ids, ground_truth, k=3)
        self.assertAlmostEqual(ndcg_perfect, 1.0)

        ndcg_suboptimal = reranker.evaluate_ndcg_at_k(["doc3", "doc2", "doc1"], ground_truth, k=3)
        self.assertLess(ndcg_suboptimal, 1.0)
        self.assertGreater(ndcg_suboptimal, 0.0)

        # NDCG edge cases
        self.assertEqual(reranker.evaluate_ndcg_at_k([], ground_truth, k=3), 0.0)
        self.assertEqual(reranker.evaluate_ndcg_at_k(ranked_ids, {}, k=3), 0.0)
        self.assertEqual(reranker.evaluate_ndcg_at_k(ranked_ids, {"doc1": 0.0}, k=3), 0.0)

        # MRR
        mrr_first = reranker.evaluate_mrr(["doc1", "doc2"], ["doc1"])
        self.assertEqual(mrr_first, 1.0)
        mrr_second = reranker.evaluate_mrr(["doc2", "doc1"], ["doc1"])
        self.assertEqual(mrr_second, 0.5)
        mrr_none = reranker.evaluate_mrr(["doc2", "doc3"], ["doc1"])
        self.assertEqual(mrr_none, 0.0)

    def test_scored_candidate_and_empty_query(self):
        reranker = OwnerScopedSemanticReranker()
        items = [
            {"id": "m1", "user_id": "u1", "content": "kubernetes docker container deployment", "pagerank": 0.8, "salience": 0.9},
            {"id": "m2", "user_id": "u1", "content": "unrelated gardening flowers", "pagerank": 0.1, "salience": 0.1},
        ]
        ranked_no_query = reranker.rank_memories("u1", "", items)
        self.assertEqual(len(ranked_no_query), 2)

        c = ScoredCandidate("1", "u1", 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, items[0])
        self.assertEqual(c.score, 0.9)
