"""Tests for the run report and redaction (backend/verification/config.py, evidence.py,
report.py): secret masking, near-exposure flagging, and that differentiation claims
are grounded only in passing verdicts.
"""

from __future__ import annotations

import json
import os

from verification.config import RunConfig
from verification.evidence import CapturedEvidence, redact
from verification.report import build_record, write_report
from verification.verdict import Outcome, Verdict


def test_redact_masks_known_secret_and_flags():
    obj = {"authorization": "Bearer abcdef123456", "note": "topsecretvalue"}
    cleaned, hit = redact(obj, ["topsecretvalue"])
    assert hit is True
    blob = json.dumps(cleaned)
    assert "topsecretvalue" not in blob
    assert "abcdef123456" not in blob


def test_redact_clean_when_no_secrets():
    cleaned, hit = redact({"a": "hello", "b": [1, 2, 3]}, ["nope-not-present"])
    assert hit is False
    assert cleaned == {"a": "hello", "b": [1, 2, 3]}


def test_evidence_redacted_sets_near_exposure():
    ev = CapturedEvidence(evidence_id="e", scenario_id="s", run_mode="mock_inprocess",
                          messages=[{"t": "Bearer zzzzzzzzzzzz"}])
    red = ev.redacted(secret_values=[])
    assert red.near_exposure is True


def _verdict(outcome, check="us2.cross_user_refused"):
    return Verdict(verdict_id="v", scope="check", outcome=outcome,
                   run_mode="mock_inprocess",
                   refs={"persona": "xuser", "scenario": "xuser:a",
                         "check": check, "property": "delegated_authority"},
                   reason="r")


def test_build_record_and_dual_report(tmp_path):
    cfg = RunConfig(mode="in_process", run_id="__verif__rep", out_dir=str(tmp_path))
    ev = CapturedEvidence(
        evidence_id="xuser:a:ev", scenario_id="xuser:a", run_mode="mock_inprocess",
    )
    verdicts = [
        _verdict(Outcome.PASS, "us2.cross_user_refused"),
        _verdict(Outcome.PASS, "us2.delegation_attribution"),
        _verdict(Outcome.PASS, "us2.audit_chain_unbroken"),
        _verdict(Outcome.PASS, "us3.no_client_construction"),
        _verdict(Outcome.UNCERTAIN, "us2.denials_audited"),
    ]
    record = build_record(cfg, verdicts, {"xuser:a": ev}, auth_mode="mock_inprocess")
    assert 0.0 < record["uncertain_ratio"] < 1.0
    assert len(record["differentiation"]) == 3
    assert any("cross-user" in d for d in record["differentiation"])

    paths = write_report(record, cfg.run_dir)
    assert os.path.exists(paths["json"]) and os.path.exists(paths["markdown"])
    md = open(paths["markdown"], encoding="utf-8").read()
    assert "__verif__rep" in md and "mock_inprocess" in md
    assert "NOT a real-realm" in md


def test_near_exposure_flag_in_record(tmp_path):
    cfg = RunConfig(mode="in_process", run_id="__verif__flag", out_dir=str(tmp_path))
    ev = CapturedEvidence(evidence_id="e", scenario_id="s", run_mode="mock_inprocess",
                          near_exposure=True)
    record = build_record(cfg, [_verdict(Outcome.PASS)], {"s": ev})
    assert "credential_near_exposure" in record["flags"]
