"""Tests for verdict reconciliation (backend/verification/verdict.py): counter refutation
and judge disagreement force an uncertain verdict.
"""

from __future__ import annotations

from verification.verdict import Outcome, reconcile


def test_reconcile_rules():
    assert reconcile(Outcome.PASS, False, None)[0] == Outcome.PASS
    assert reconcile(Outcome.PASS, True, None)[0] == Outcome.UNCERTAIN
    assert reconcile(Outcome.PASS, False, Outcome.FAIL)[0] == Outcome.UNCERTAIN
    assert reconcile(Outcome.PASS, False, Outcome.PASS)[0] == Outcome.PASS
    assert reconcile(Outcome.FAIL, False, None)[0] == Outcome.FAIL
    assert reconcile(Outcome.UNCERTAIN, False, None)[0] == Outcome.UNCERTAIN
