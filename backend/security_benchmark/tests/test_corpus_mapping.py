"""Ensures external corpus requests never silently use bundled diagnostic samples.
The local qualification corpus has twelve distinct paired cases.
"""

from __future__ import annotations

import pytest

from security_benchmark.adapters.agentdojo import AgentDojoAdapter
from security_benchmark.adapters.asb import ASBAdapter
from security_benchmark.adapters.injecagent import InjecAgentAdapter
from security_benchmark.qualification import CASES, corpus_digest


@pytest.mark.parametrize("adapter", [AgentDojoAdapter, ASBAdapter, InjecAgentAdapter])
def test_real_request_cannot_fall_back_to_samples(monkeypatch, adapter):
    monkeypatch.setenv("ASTRAL_BENCH_LOAD_REAL", "1")
    with pytest.raises(RuntimeError, match="no qualified AstralDeep"):
        adapter().load_cases()


def test_local_corpus_is_distinct_and_pinned():
    assert len(CASES) == 12
    assert len({case.case_id for case in CASES}) == 12
    assert {case.surface for case in CASES} == {
        "tool_result", "remote_reply", "retained_context"}
    assert len(corpus_digest()) == 64
