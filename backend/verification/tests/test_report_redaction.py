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


def _verdict(outcome, check="us1.component_from_file"):
    return Verdict(verdict_id="v", scope="check", outcome=outcome,
                   run_mode="mock_inprocess",
                   refs={"persona": "everyday", "scenario": "everyday:primary",
                         "check": check, "property": "tangible_ui"},
                   reason="r")


def test_build_record_and_dual_report(tmp_path):
    cfg = RunConfig(mode="in_process", run_id="__verif__rep", out_dir=str(tmp_path))
    ev = CapturedEvidence(
        evidence_id="everyday:primary:ev", scenario_id="everyday:primary",
        run_mode="mock_inprocess", components=[{"type": "table"}],
        extra={"file_category": "spreadsheet"},
    )
    verdicts = [
        _verdict(Outcome.PASS, "us1.component_from_file"),
        _verdict(Outcome.PASS, "us1.persisted_with_identity"),
        _verdict(Outcome.PASS, "us1.re_executable"),
        _verdict(Outcome.UNCERTAIN, "us2.denials_audited"),
    ]
    record = build_record(cfg, verdicts, {"everyday:primary": ev},
                          auth_mode="mock_inprocess", personas=["everyday"])
    assert record["coverage"]["file_categories"] == ["spreadsheet"]
    assert record["coverage"]["component_types"] == ["table"]
    assert 0.0 < record["uncertain_ratio"] < 1.0
    assert any("real contents" in d for d in record["differentiation"])

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
