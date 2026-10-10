from __future__ import annotations

import os
import unittest
from personalization.qualification_pilot import (
    OwnerScopedSemanticReranker,
    QualificationThresholds,
    SemanticScoringWeights,
    compute_lexical_similarity,
    compute_semantic_embedding_similarity,
    qualification_pilot_enabled,
)


class TestOwnerScopedSemanticQualification(unittest.TestCase):
    def setUp(self):
        self.reranker = OwnerScopedSemanticReranker()

    def test_feature_flag(self):
        os.environ["FF_MEMORY_QUALIFICATION_PILOT"] = "true"
        self.assertTrue(qualification_pilot_enabled())
        os.environ["FF_MEMORY_QUALIFICATION_PILOT"] = "false"
        self.assertFalse(qualification_pilot_enabled())

    def test_lexical_and_semantic_similarity(self):
        sim = compute_lexical_similarity("python async fast", "python async programming")
        self.assertGreater(sim, 0.25)

        sem = compute_semantic_embedding_similarity("database migration", "sql schema update")
        self.assertGreater(sem, 0.0)

    def test_owner_scoped_isolation(self):
        items = [
            {"id": "m1", "owner_id": "user_a", "value": "Project alpha roadmap", "salience": 0.5},
            {"id": "m2", "owner_id": "user_b", "value": "Confidential financial data roadmap", "salience": 0.9},
            {"id": "m3", "owner_id": "user_a", "value": "Weekly engineering notes and roadmap updates", "salience": 0.2},
        ]

        ranked_a = self.reranker.rank_memories(owner_id="user_a", query="roadmap", items=items)
        self.assertEqual(len(ranked_a), 2)
        self.assertTrue(all(it["owner_id"] == "user_a" for it in ranked_a))
        self.assertEqual(self.reranker.verify_isolation("user_a", ranked_a), 0.0)

    def test_ranking_weights_and_multisignal_fusion(self):
        items = [
            {"id": "1", "owner_id": "u1", "value": "old unrelated log", "pagerank": 0.0, "salience": 0.0},
            {"id": "2", "owner_id": "u1", "value": "general notes", "pagerank": 0.8, "salience": 0.5},
            {"id": "3", "owner_id": "u1", "value": "exact target match query keywords", "pagerank": 0.9, "salience": 0.8},
        ]

        ranked = self.reranker.rank_memories(owner_id="u1", query="exact target match", items=items)
        self.assertEqual(ranked[0]["id"], "3")

    def test_ndcg_and_mrr_metrics(self):
        ranked_ids = ["doc_3", "doc_1", "doc_2"]
        ground_truth = {"doc_3": 3.0, "doc_2": 2.0, "doc_1": 1.0}
        ndcg = self.reranker.evaluate_ndcg_at_k(ranked_ids, ground_truth, k=3)
        self.assertGreater(ndcg, 0.85)

        mrr = self.reranker.evaluate_mrr(ranked_ids, relevant_ids=["doc_3"])
        self.assertEqual(mrr, 1.0)

    def test_memory_tools_integration_with_qualification(self):
        from unittest.mock import patch
        from personalization.memory_tools import MemoryTools

        class FakeRepo:
            def __init__(self):
                self.items = [
                    {"id": "m1", "user_id": "u1", "category": "preference", "value": "User prefers python async and pydantic"},
                    {"id": "m2", "user_id": "u2", "category": "preference", "value": "Tenant 2 secret: do not leak python"},
                    {"id": "m3", "user_id": "u1", "category": "preference", "value": "User likes dark mode theme"},
                ]
            def list_memory(self, user_id, project_id=None):
                return [it for it in self.items if it["user_id"] == user_id]
            def touch_access(self, user_id, mid):
                pass

        tools = MemoryTools(repo=FakeRepo())
        with patch.dict(os.environ, {"FF_MEMORY_QUALIFICATION_PILOT": "true"}):
            results = tools.memory_search("u1", "python async")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "m1")
            self.assertEqual(results[0]["user_id"], "u1")


if __name__ == "__main__":
    unittest.main()
