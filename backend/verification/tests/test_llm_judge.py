"""Tests for LLM-as-judge enrichment (backend/verification/llm_judge.py): resolves to
n/a without a real LLM, judge response interpretation, and that judge errors are
swallowed rather than failing the check.
"""

from __future__ import annotations

import types

from verification.checks.base import Check, ok
from verification.evidence import CapturedEvidence
from verification.llm_judge import interpret_judge_response, make_llm_judge
from verification.tests.conftest import run_async
from verification.verdict import Outcome


def test_interpret_judge_response():
    assert interpret_judge_response('{"verdict": "pass"}') == Outcome.PASS
    assert interpret_judge_response('{"verdict": "fail", "why": "x"}') == Outcome.FAIL
    assert interpret_judge_response("pass") == Outcome.PASS
    assert interpret_judge_response("this should fail") == Outcome.FAIL
    assert interpret_judge_response("") is None
    assert interpret_judge_response("¯\\_(ツ)_/¯") is None


def test_make_llm_judge_na_without_llm():
    judge = make_llm_judge(None)
    ev = CapturedEvidence(evidence_id="e", scenario_id="s", run_mode="mock_inprocess")
    check = Check("c", "tangible_ui", lambda e, i: ok("c"))
    assert run_async(judge(check, ev, {})) is None


def test_make_llm_judge_with_fake_llm():
    async def _fake_call_llm(ws, messages, tools_desc=None, temperature=None, feature=""):
        return types.SimpleNamespace(content='{"verdict": "fail"}'), {}

    judge = make_llm_judge(_fake_call_llm)
    ev = CapturedEvidence(evidence_id="e", scenario_id="s", run_mode="mock_inprocess",
                          components=[{"type": "table"}])
    check = Check("c", "tangible_ui", lambda e, i: ok("c"))
    assert run_async(judge(check, ev, {})) == Outcome.FAIL


def test_make_llm_judge_swallows_errors():
    async def _boom(*a, **k):
        raise RuntimeError("llm down")

    judge = make_llm_judge(_boom)
    ev = CapturedEvidence(evidence_id="e", scenario_id="s", run_mode="mock_inprocess")
    check = Check("c", "tangible_ui", lambda e, i: ok("c"))
    assert run_async(judge(check, ev, {})) is None
