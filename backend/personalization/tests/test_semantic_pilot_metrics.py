"""Tests for the pilot metrics: hand-computed recall@K and graded nDCG@K, forbidden-row
accounting, and mean aggregation.
"""

from __future__ import annotations

import pytest

from personalization.semantic_pilot.metrics import (
    forbidden_hits,
    mean,
    ndcg_at_k,
    recall_at_k,
)


def test_recall_at_k_perfect_and_partial():
    assert recall_at_k(["a", "b", "c"], ["a"], 3) == 1.0
    assert recall_at_k(["a", "b"], ["a", "c"], 2) == 0.5
    assert recall_at_k(["x", "y"], ["a"], 2) == 0.0


def test_recall_at_k_edge_cases():
    assert recall_at_k(["a"], ["a"], 0) == 0.0
    assert recall_at_k(["a"], [], 3) == 0.0
    assert recall_at_k([], ["a"], 3) == 0.0
    assert recall_at_k(["a", "a", "b"], ["a", "b"], 3) == 1.0


def test_ndcg_at_k_perfect_ordering_is_one():
    grades = {"a": 3, "b": 2, "c": 1}
    assert ndcg_at_k(["a", "b", "c"], grades, 3) == 1.0
    assert ndcg_at_k(["a", "b", "c", "d"], grades, 3) == 1.0


def test_ndcg_at_k_known_value():
    import math

    grades = {"a": 3, "b": 1}
    ranked = ["b", "a"]
    dcg = (2 ** 1 - 1) / math.log2(2) + (2 ** 3 - 1) / math.log2(3)
    idcg = (2 ** 3 - 1) / math.log2(2) + (2 ** 1 - 1) / math.log2(3)
    assert ndcg_at_k(ranked, grades, 2) == pytest.approx(dcg / idcg, abs=1e-6)


def test_ndcg_at_k_ordering_matters():
    grades = {"a": 3, "b": 1}
    good = ndcg_at_k(["a", "b"], grades, 2)
    bad = ndcg_at_k(["b", "a"], grades, 2)
    assert good == 1.0
    assert 0.0 < bad < good


def test_ndcg_at_k_empty_and_ungraded():
    assert ndcg_at_k(["a"], {}, 3) == 0.0
    assert ndcg_at_k(["a"], {"a": 0}, 3) == 0.0
    assert ndcg_at_k([], {"a": 3}, 3) == 0.0


def test_ndcg_ignores_rows_beyond_k():
    grades = {"a": 3}
    assert ndcg_at_k(["z", "a"], grades, 1) == 0.0
    assert ndcg_at_k(["a", "z"], grades, 1) == 1.0


def test_forbidden_hits_and_mean():
    assert forbidden_hits(["a", "b", "c"], ["b"]) == ["b"]
    assert forbidden_hits(["a"], ["x"]) == []
    assert mean([1.0, 2.0]) == 1.5
    assert mean([]) is None
