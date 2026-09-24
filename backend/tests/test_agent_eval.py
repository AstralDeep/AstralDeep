"""Pure-Python tests for orchestrator/agent_eval.py: trajectory metrics, the weighted
quality aggregate, and the tau-bench pass-hat-k reliability estimator.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import agent_eval as ae  # noqa: E402


def test_tool_name_normalisation():
    assert ae._tool_name("search") == "search"
    assert ae._tool_name({"tool": "a"}) == "a"
    assert ae._tool_name({"name": "b"}) == "b"
    assert ae._tool_name({"tool_name": "c"}) == "c"
    assert ae._tool_name(123) == "123"


def test_mixed_str_and_dict_trajectory():
    traj = ["a", {"tool": "b"}, {"name": "c"}]
    assert ae.trajectory_exact_match(traj, ["a", "b", "c"]) == 1.0


def test_exact_match():
    assert ae.trajectory_exact_match(["a", "b"], ["a", "b"]) == 1.0
    assert ae.trajectory_exact_match(["b", "a"], ["a", "b"]) == 0.0
    assert ae.trajectory_exact_match(["a", "b", "c"], ["a", "b"]) == 0.0


def test_in_order_match_is_subsequence():
    assert ae.trajectory_in_order_match(["a", "x", "b", "y"], ["a", "b"]) == 1.0
    assert ae.trajectory_in_order_match(["b", "a"], ["a", "b"]) == 0.0
    assert ae.trajectory_in_order_match(["a"], []) == 1.0


def test_any_order_match_is_subset():
    assert ae.trajectory_any_order_match(["b", "a", "c"], ["a", "b"]) == 1.0
    assert ae.trajectory_any_order_match(["a"], ["a", "b"]) == 0.0


def test_precision_and_recall():
    pred, ref = ["a", "b", "c"], ["a", "b", "d"]
    assert ae.trajectory_precision(pred, ref) == pytest.approx(2 / 3)
    assert ae.trajectory_recall(pred, ref) == pytest.approx(2 / 3)
    assert ae.trajectory_precision([], ref) == 0.0
    assert ae.trajectory_recall(pred, []) == 0.0


def test_single_tool_use():
    assert ae.trajectory_single_tool_use(["a", "b"], "b") == 1.0
    assert ae.trajectory_single_tool_use(["a", "b"], "z") == 0.0


def test_score_trajectory_dict():
    s = ae.score_trajectory(["a", "b"], ["a", "b"])
    assert set(s) == {"exact_match", "in_order_match", "any_order_match", "precision", "recall"}
    assert s["exact_match"] == 1.0 and s["recall"] == 1.0


def test_aggregate_quality_default_weights():
    s = {"in_order_match": 1.0, "recall": 1.0, "precision": 0.5}
    assert ae.aggregate_quality(s) == 0.875


def test_aggregate_quality_renormalises_partial():
    assert ae.aggregate_quality({"recall": 1.0}) == 1.0
    assert ae.aggregate_quality({"unrelated": 1.0}) == 0.0


def test_pass_hat_k_perfect_and_floor():
    assert ae.pass_hat_k(8, 8, 8) == 1.0
    assert ae.pass_hat_k(8, 4, 8) == 0.0
    assert ae.pass_hat_k(2, 2, 3) == 0.0


def test_pass_hat_k_combinatorial_estimator():
    assert ae.pass_hat_k(4, 2, 2) == pytest.approx(1 / 6, abs=1e-6)
    assert ae.pass_hat_k(10, 10, 3) == 1.0


def test_pass_hat_k_validation():
    with pytest.raises(ValueError):
        ae.pass_hat_k(5, 5, 0)
    with pytest.raises(ValueError):
        ae.pass_hat_k(3, 5, 1)


def test_pass_k_from_outcomes():
    assert ae.pass_k_from_outcomes([True] * 8, 8) == 1.0
    assert ae.pass_k_from_outcomes([True, True, False, False], 2) == pytest.approx(1 / 6, abs=1e-6)
    assert ae.pass_k_from_outcomes([False, False], 1) == 0.0
