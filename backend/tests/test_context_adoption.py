"""Checks the candidate-bound context adoption record: plan binding, honest decisions, and
default posture. The frozen paired diagnostic itself stays covered by test_context_evaluation.py.
"""
import hashlib
import json
import os
from pathlib import Path
import runpy

import pytest

from context_evaluation import adoption as adoption_module
from context_evaluation import evaluate as diagnostic
from context_evaluation.adoption import control_defaults, plan_identity, verify

CANDIDATE = "a" * 40


def identity(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("Accepted feature plan bytes for the adoption record.\n", encoding="utf-8")
    return path


async def test_record_binds_candidate_plan_and_frozen_diagnostic(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert record["format"] == adoption_module.ADOPTION_FORMAT
    assert record["candidate"] == CANDIDATE
    assert record["plan_path"] == str(plan)
    assert record["plan_digest"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert record["implementation_digest"] == diagnostic.implementation_identity()
    assert record["workflows_digest"] == identity(diagnostic.load_workflows())
    assert record["scoring"] == diagnostic.SCORING
    assert record["scoring_digest"] == identity(diagnostic.SCORING_RULES)
    assert record["controls_default_off"] == {"message_compaction": False, "observation_packing": False,
                                              "safe_compaction": False}
    assert record["defaults_changed"] is False
    assert record["waived_gates"] == []


async def test_record_reports_paired_runs_and_honest_withheld_decision(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert len(record["runs"]) == 96
    assert record["run_summary"] == {"total": 96, "passed": 96,
                                     "by_control": {control: {"passed": 24, "total": 24}
                                                    for control in ("baseline", "combined", "compaction", "packing")},
                                     "by_split": {"development": {"passed": 48, "total": 48},
                                                  "held_out": {"passed": 48, "total": 48}},
                                     "failed_workflows": [],
                                     "unknown_complete_cost_runs": 96}
    assert record["decision"] == "withheld"
    assert record["decision_reasons"] == ["real_provider_measurements", "five_user_formative_evidence",
                                          "product_and_client_qualification"]
    assert record["promotion"]["eligible"] is False
    assert record["promotion"]["defaults_changed"] is False
    for run in record["runs"]:
        assert run["complete_cost_known"] is False
        assert run["reported_provider_cost"] is None
    assert {run["source_identity"] for run in record["runs"]} == {identity(item["source"])
                                                                  for item in diagnostic.load_workflows()}
    assert {run["task_identity"] for run in record["runs"]} == {identity(item["task"])
                                                                for item in diagnostic.load_workflows()}


async def test_record_is_deterministic_with_a_verifiable_receipt(plan):
    first = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    second = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["receipt_digest"] == identity({key: value for key, value in first.items()
                                                if key != "receipt_digest"})
    verify(first, plan_path=plan)


async def test_failed_runs_are_retained_and_withhold_the_decision(monkeypatch, plan):
    original_read = diagnostic.EvidenceArchive.read

    def bypass(self, reference, **kwargs):
        kwargs["owner_id"] = "synthetic-owner"
        return original_read(self, reference, **kwargs)

    monkeypatch.setattr(diagnostic.EvidenceArchive, "read", bypass)
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert record["run_summary"]["passed"] < record["run_summary"]["total"]
    assert record["run_summary"]["failed_workflows"]
    assert record["decision"] == "withheld"
    assert "boundary_regression" in record["decision_reasons"]
    failing = [run for run in record["runs"] if not run["boundary_passed"]]
    assert failing and all(run["workflow"] in record["run_summary"]["failed_workflows"] for run in failing)
    verify(record)


async def test_enabled_context_default_blocks_the_decision(monkeypatch, plan):
    class Enabled:
        def is_enabled(self, flag):
            return flag == "safe_compaction"

    monkeypatch.setattr("shared.feature_flags.FeatureFlags", Enabled)
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert record["controls_default_off"]["safe_compaction"] is True
    assert record["controls_default_off"]["observation_packing"] is False
    assert record["decision"] == "withheld"
    assert "context_defaults_not_off" in record["decision_reasons"]
    verify(record)


async def test_eligible_bound_evidence_records_adopted_without_changing_defaults(monkeypatch, plan):
    report = {"implementation_digest": diagnostic.implementation_identity(),
              "workflows_digest": identity(diagnostic.load_workflows()),
              "scoring": diagnostic.SCORING, "scoring_digest": identity(diagnostic.SCORING_RULES),
              "measurement_kind": "synthetic_capability", "provider_route": diagnostic.PROVIDER_ROUTE,
              "measurement_provider_route": None, "runs": [],
              "promotion": {"eligible": True, "failed": [], "missing": [], "promoted_control": "combined",
                            "cost_comparison": {"target_passed": True},
                            "formative_summary": {"target_passed": True}, "defaults_changed": False}}

    async def eligible(**kwargs):
        return report

    monkeypatch.setattr(diagnostic, "evaluate", eligible)
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert record["decision"] == "adopted"
    assert record["decision_reasons"] == []
    assert record["defaults_changed"] is False
    assert record["waived_gates"] == []
    verify(record)


async def test_foreign_candidate_evidence_is_retained_as_failed_not_missing(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan,
                            provider_measurements={"candidate": "b" * 40})
    assert record["decision"] == "withheld"
    assert "real_provider_measurements_invalid" in record["decision_reasons"]
    assert "five_user_formative_evidence" in record["decision_reasons"]
    assert record["measurement_provider_route"] is None


def test_control_defaults_scrub_and_restore_the_environment():
    os.environ["FF_OBSERVATION_PACKING"] = "true"
    try:
        assert control_defaults() == {"message_compaction": False, "observation_packing": False,
                                      "safe_compaction": False}
        assert os.environ["FF_OBSERVATION_PACKING"] == "true"
    finally:
        del os.environ["FF_OBSERVATION_PACKING"]


async def test_invalid_candidates_and_plan_labels_fail_closed(plan):
    with pytest.raises(ValueError, match="adoption_candidate_invalid"):
        await adoption_module.adoption(candidate="main", plan_path=plan)
    with pytest.raises(ValueError, match="adoption_candidate_invalid"):
        await adoption_module.adoption(candidate="A" * 40, plan_path=plan)
    with pytest.raises(ValueError, match="adoption_plan_invalid"):
        await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan.parent / "missing.md")
    hostile = plan.parent / "bad\nlabel"
    hostile.write_text("accepted plan", encoding="utf-8")
    with pytest.raises(ValueError, match="adoption_plan_invalid"):
        await adoption_module.adoption(candidate=CANDIDATE, plan_path=hostile)


def test_plan_reader_fails_closed(tmp_path):
    digest = plan_identity(_write(tmp_path / "plan.md", "accepted plan bytes"))
    assert digest == hashlib.sha256(b"accepted plan bytes").hexdigest()
    for path in (tmp_path / "missing", tmp_path, _write(tmp_path / "empty", ""),
                 _write(tmp_path / "huge", "x" * (1024 * 1024 + 1)),
                 _write_bytes(tmp_path / "binary", b"\xff\xfe\x00")):
        with pytest.raises(ValueError, match="adoption_plan_invalid"):
            plan_identity(path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(ValueError, match="adoption_plan_invalid"):
        plan_identity(pipe)


def test_plan_reader_rejects_growth_between_stat_and_read(tmp_path, monkeypatch):
    path = _write_bytes(tmp_path / "growing", b"x" * (1024 * 1024 + 1))
    original_stat = os.fstat

    def smaller_metadata(descriptor):
        value = list(original_stat(descriptor))
        value[6] = 1
        return os.stat_result(value)

    monkeypatch.setattr(adoption_module.os, "fstat", smaller_metadata)
    with pytest.raises(ValueError, match="adoption_plan_invalid"):
        plan_identity(path)


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _write_bytes(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


async def test_verify_detects_drift(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    verify(record, plan_path=plan)
    changed_plan = plan.parent / "other-plan.md"
    changed_plan.write_text("a different accepted plan", encoding="utf-8")
    with pytest.raises(ValueError, match="adoption_plan_changed"):
        verify(record, plan_path=changed_plan)
    with pytest.raises(ValueError, match="adoption_implementation_changed"):
        verify({**record, "implementation_digest": "b" * 64})
    with pytest.raises(ValueError, match="adoption_workflows_changed"):
        verify({**record, "workflows_digest": "b" * 64})
    with pytest.raises(ValueError, match="adoption_scoring_changed"):
        verify({**record, "scoring_digest": "b" * 64})
    with pytest.raises(ValueError, match="adoption_summary_inconsistent"):
        verify({**record, "run_summary": {**record["run_summary"], "passed": 95}})
    with pytest.raises(ValueError, match="adoption_summary_inconsistent"):
        verify({**record, "runs": [{**record["runs"][0], "quality_passed": False}, *record["runs"][1:]]})
    with pytest.raises(ValueError, match="adoption_decision_inconsistent"):
        verify({**record, "decision": "adopted"})
    with pytest.raises(ValueError, match="adoption_decision_inconsistent"):
        verify({**record, "decision_reasons": []})
    with pytest.raises(ValueError, match="adoption_posture_invalid"):
        verify({**record, "defaults_changed": True})
    with pytest.raises(ValueError, match="adoption_posture_invalid"):
        verify({**record, "waived_gates": ["staging"]})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        verify({key: value for key, value in record.items() if key != "plan_digest"})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        verify({**record, "format": "astral.context-adoption/v2"})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        verify({**record, "candidate": "main"})
    with pytest.raises(ValueError, match="adoption_receipt_invalid"):
        verify({**record, "candidate": "b" * 40})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        verify({**record, "controls_default_off": {"safe_compaction": "false"}})
    with pytest.raises(ValueError, match="adoption_receipt_invalid"):
        verify({**record, "receipt_digest": "0" * 64})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        verify([])


async def test_verify_rejects_drifted_implementation_identity(monkeypatch, plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    monkeypatch.setattr(diagnostic, "implementation_identity", lambda: "b" * 64)
    with pytest.raises(ValueError, match="adoption_implementation_changed"):
        verify(record, plan_path=plan)


def test_cli_writes_a_verifiable_record(tmp_path, monkeypatch, plan):
    output = tmp_path / "evidence" / "adoption.json"
    monkeypatch.setattr("sys.argv", ["adoption", "--candidate", CANDIDATE, "--plan", str(plan),
                                     "--output", str(output)])
    adoption_module.main()
    record = json.loads(output.read_text(encoding="utf-8"))
    verify(record, plan_path=plan)
    assert record["decision"] == "withheld"
    assert record["defaults_changed"] is False


def test_cli_rejects_unbounded_measurements(tmp_path, monkeypatch, plan):
    measurements = tmp_path / "measurements.json"
    measurements.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["adoption", "--candidate", CANDIDATE, "--plan", str(plan),
                                     "--output", str(tmp_path / "out.json"),
                                     "--measurements", str(measurements)])
    with pytest.raises(ValueError, match="evaluation_measurements_invalid"):
        adoption_module.main()
    assert not (tmp_path / "out.json").exists()


def test_module_entry_point_runs_the_same_fail_closed_record(tmp_path, monkeypatch, plan):
    output = tmp_path / "record.json"
    monkeypatch.setattr("sys.argv", ["adoption", "--candidate", CANDIDATE, "--plan", str(plan),
                                     "--output", str(output)])
    runpy.run_path(adoption_module.__file__, run_name="__main__")
    verify(json.loads(output.read_text(encoding="utf-8")), plan_path=plan)
