"""Checks paired qualification evidence, candidate binding, and failure labels.
Unit evidence never substitutes for a registered-turn run.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from security_benchmark.qualification import CASES, CaseEvidence, validate_evidence
from security_benchmark.qualification_report import build_report, validate_report


def _evidence():
    return tuple(CaseEvidence(
        case_id=case.case_id, surface=case.surface,
        registered_turn=True, read_completed=True, effect_attempted=True,
        effect_count=0, audit_chain_ok=True, audit_event_ids=(f"audit-{index}",),
        benign_completed=True)
        for index, case in enumerate(CASES))


def _candidate():
    return {"commit": "a" * 40, "policy_sha256": "b" * 64,
            "composition_sha256": "c" * 64}


def test_complete_paired_evidence_passes_schema():
    evidence = _evidence()
    assert not validate_evidence(CASES, evidence, teardown_verified=True)
    report = build_report(_candidate(), evidence, teardown_verified=True)
    validate_report(report)
    assert report["counts"]["denied"] == 12
    assert report["counts"]["benign_completed"] == 12


@pytest.mark.parametrize("change", [
    {"effect_count": 1}, {"effect_count": None}, {"audit_chain_ok": False},
    {"audit_event_ids": ()}, {"read_completed": False},
    {"effect_attempted": False}, {"registered_turn": False},
    {"benign_completed": False},
])
def test_missing_or_bad_observation_cannot_pass(change):
    evidence = list(_evidence())
    evidence[0] = replace(evidence[0], **change)
    report = build_report(_candidate(), tuple(evidence), teardown_verified=True)
    with pytest.raises(ValueError):
        validate_report(report)


def test_teardown_and_population_must_be_complete():
    report = build_report(_candidate(), _evidence(), teardown_verified=False)
    with pytest.raises(ValueError):
        validate_report(report)
    report = build_report(_candidate(), _evidence()[:-1], teardown_verified=True)
    with pytest.raises(ValueError):
        validate_report(report)


def test_synthetic_label_cannot_be_promoted():
    report = build_report(_candidate(), _evidence(), teardown_verified=True)
    report["evidence_class"] = "synthetic_diagnostic"
    with pytest.raises(ValueError):
        validate_report(report)


def test_interrupted_cli_writes_incomplete_report(monkeypatch, tmp_path):
    from security_benchmark import __main__ as cli
    from security_benchmark import qualification_report

    def unavailable(_root):
        raise ValueError("dirty candidate")

    monkeypatch.setattr(qualification_report, "candidate_identity", unavailable)
    assert cli.main(["--qualify-real-dispatch", "--out", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "real-dispatch-qualification.json").read_text())
    assert report["status"] == "incomplete"
    with pytest.raises(ValueError):
        validate_report(report)


def test_legacy_inprocess_driver_refuses_fake_real_turn():
    from security_benchmark.drivers.inprocess import InProcessDriver

    with pytest.raises(RuntimeError, match="no real turn executor"):
        InProcessDriver("test").setup()


