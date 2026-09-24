"""Tests for backend/security_benchmark/runner.py and report.py: synthetic end-to-end
ASR numbers, that every envelope layer's marginal reduction is attributable, and that
adjudication is reproducible given the same inputs.
"""

from __future__ import annotations

import math

from security_benchmark.adjudicator import Outcome
from security_benchmark.config import RunConfig
from security_benchmark.report import marginal_reductions, stats_by_envelope
from security_benchmark.envelope import LADDER
from security_benchmark.runner import run


def _agentdojo_record(tmp_path):
    cfg = RunConfig(mode="synthetic", benchmarks=["agentdojo"],
                    artifacts_root=str(tmp_path), run_id="__bench__test")
    records, _ = run(cfg, stamp="test")
    return records[0]


def test_baseline_and_full_asr(tmp_path):
    rec = _agentdojo_record(tmp_path)
    stats = {s.envelope_label: s for s in stats_by_envelope(rec, LADDER)}
    base = stats["none"]
    assert base.succeeded == 5
    assert base.not_attempted == 1
    assert base.out_of_corpus == 1
    assert base.in_corpus == 6
    assert math.isclose(base.asr, 5 / 6, rel_tol=1e-6)

    full = stats["DAF+PHI+RT+LLM"]
    assert full.succeeded == 1
    assert math.isclose(full.asr, 1 / 6, rel_tol=1e-6)


def test_each_layer_attributable(tmp_path):
    rec = _agentdojo_record(tmp_path)
    stats = stats_by_envelope(rec, LADDER)
    deltas = marginal_reductions(stats)
    assert math.isclose(deltas[1], 2 / 6, rel_tol=1e-6)
    assert math.isclose(deltas[2], 1 / 6, rel_tol=1e-6)
    assert math.isclose(deltas[3], 1 / 6, rel_tol=1e-6)
    assert math.isclose(deltas[4], 0.0, abs_tol=1e-9)
    total = stats[0].asr - stats[-1].asr
    assert math.isclose(sum(d for d in deltas if d is not None), total, rel_tol=1e-6)


def test_llm_judge_column_present_but_not_implemented(tmp_path):
    rec = _agentdojo_record(tmp_path)
    labels = list(rec.adjudications.keys())
    assert any("LLM" in lbl for lbl in labels)
    full = rec.adjudications[labels[-1]]
    semantic = [a for a in full if a.category == "semantic_manipulation"][0]
    assert semantic.outcome is Outcome.SUCCEEDED


def test_reproducible(tmp_path):
    r1 = _agentdojo_record(tmp_path)
    r2 = _agentdojo_record(tmp_path)
    s1 = [s.to_dict() for s in stats_by_envelope(r1, LADDER)]
    s2 = [s.to_dict() for s in stats_by_envelope(r2, LADDER)]
    assert s1 == s2
