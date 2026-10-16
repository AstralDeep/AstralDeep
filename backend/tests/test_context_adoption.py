"""Checks the candidate-bound context adoption record: plan binding, honest decisions, and
default posture. The frozen paired diagnostic itself stays covered by test_context_evaluation.py.
"""
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from context_evaluation import adoption as adoption_module
from context_evaluation import evaluate as diagnostic
from context_evaluation.adoption import control_defaults, plan_identity, verify, verify_async

CANDIDATE = "a" * 40


def identity(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@pytest.fixture
def plan(tmp_path, monkeypatch):
    root = Path(adoption_module.__file__).resolve().parents[2]
    paths = [f"backend/{path}" for path in diagnostic._IMPLEMENTATION_FILES]
    paths.extend(["backend/context_evaluation/workflows.json", "backend/shared/feature_flags.py",
                  "backend/context_evaluation/adoption.py"])
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / relative).read_bytes())
    path = tmp_path / "plan.md"
    path.write_text("Accepted feature plan bytes for the adoption record.\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Synthetic Test",
                    "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Synthetic source anchor"], check=True)
    candidate = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setattr(sys.modules[__name__], "CANDIDATE", candidate)
    monkeypatch.setattr(adoption_module, "_repository_root", lambda: tmp_path)
    return path


async def test_record_binds_candidate_plan_and_frozen_diagnostic(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert record["format"] == adoption_module.ADOPTION_FORMAT
    assert record["candidate"] == CANDIDATE
    assert record["plan_path"] == "plan.md"
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
    await verify_async(first, plan_path=plan)


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
    await verify_async(record)


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
    await verify_async(record)


async def test_structurally_eligible_inputs_cannot_authorize_adoption(monkeypatch, plan):
    from tests.test_context_evaluation import inputs as evaluation_inputs

    original_identity = diagnostic.implementation_identity
    supplied = evaluation_inputs.__wrapped__(monkeypatch)
    monkeypatch.setattr(diagnostic, "implementation_identity", original_identity)
    for name in ("provider_measurements", "formative", "qualification"):
        supplied[name]["candidate"] = CANDIDATE
        supplied[name]["implementation_digest"] = original_identity()
    for client in supplied["qualification"]["live_clients"]:
        client["candidate"] = CANDIDATE
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan,
        **{key: supplied[key] for key in ("provider_route", "provider_measurements", "formative", "qualification")})
    assert record["promotion"]["eligible"] is True
    assert record["decision"] == "withheld"
    assert record["decision_reasons"] == ["external_evidence_origin_unverified"]
    assert record["inputs"]["provider_measurements"] == supplied["provider_measurements"]
    assert record["defaults_changed"] is False
    assert record["waived_gates"] == []
    await verify_async(record)


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
    await verify_async(record, plan_path=plan)
    changed_plan = plan.parent / "other-plan.md"
    changed_plan.write_text("a different accepted plan", encoding="utf-8")
    with pytest.raises(ValueError, match="adoption_plan_changed"):
        await verify_async(record, plan_path=changed_plan)
    with pytest.raises(ValueError, match="adoption_implementation_changed"):
        await verify_async({**record, "implementation_digest": "b" * 64})
    with pytest.raises(ValueError, match="adoption_workflows_changed"):
        await verify_async({**record, "workflows_digest": "b" * 64})
    with pytest.raises(ValueError, match="adoption_scoring_changed"):
        await verify_async({**record, "scoring_digest": "b" * 64})
    with pytest.raises(ValueError, match="adoption_summary_inconsistent"):
        await verify_async({**record, "run_summary": {**record["run_summary"], "passed": 95}})
    with pytest.raises(ValueError, match="adoption_summary_inconsistent"):
        await verify_async({**record, "runs": [{**record["runs"][0], "quality_passed": False}, *record["runs"][1:]]})
    with pytest.raises(ValueError, match="adoption_decision_inconsistent"):
        await verify_async({**record, "decision": "adopted"})
    with pytest.raises(ValueError, match="adoption_decision_inconsistent"):
        await verify_async({**record, "decision_reasons": []})
    with pytest.raises(ValueError, match="adoption_posture_invalid"):
        await verify_async({**record, "defaults_changed": True})
    with pytest.raises(ValueError, match="adoption_posture_invalid"):
        await verify_async({**record, "waived_gates": ["staging"]})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async({key: value for key, value in record.items() if key != "plan_digest"})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async({**record, "format": "astral.context-adoption/v999"})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async({**record, "candidate": "main"})
    with pytest.raises(ValueError, match="adoption_candidate_source_invalid"):
        await verify_async({**record, "candidate": "b" * 40})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async({**record, "controls_default_off": {"safe_compaction": "false"}})
    with pytest.raises(ValueError, match="adoption_receipt_invalid"):
        await verify_async({**record, "receipt_digest": "0" * 64})
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async([])


async def test_verify_rejects_drifted_implementation_identity(monkeypatch, plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    monkeypatch.setattr(diagnostic, "implementation_identity", lambda: "b" * 64)
    with pytest.raises(ValueError, match="adoption_implementation_changed"):
        await verify_async(record, plan_path=plan)


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
    runpy.run_path(str(plan.parent / "backend/context_evaluation/adoption.py"), run_name="__main__")
    verify(json.loads(output.read_text(encoding="utf-8")), plan_path=plan)


def reseal(record):
    record['receipt_digest'] = identity({key: value for key, value in record.items() if key != 'receipt_digest'})
    return record


async def test_rehashed_empty_runs_are_not_verifiable(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record['runs'] = []
    record['run_summary'] = adoption_module.run_summary([])
    with pytest.raises(ValueError, match='adoption_runs_invalid'):
        await verify_async(reseal(record), plan_path=plan)


async def test_rehashed_changed_acceptance_rules_are_not_verifiable(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record['acceptance_rules'] = {**record['acceptance_rules'], 'complete_cost_target_percent': 0}
    with pytest.raises(ValueError, match='adoption_rules_changed'):
        await verify_async(reseal(record), plan_path=plan)


async def test_rehashed_forged_promotion_is_recomputed(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record['promotion'] = {**record['promotion'], 'eligible': True, 'failed': [], 'missing': []}
    record['decision'] = 'adopted'
    record['decision_reasons'] = []
    with pytest.raises(ValueError, match='adoption_promotion_inconsistent'):
        await verify_async(reseal(record), plan_path=plan)


async def test_rehashed_promotion_cost_summary_is_recomputed(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record['promotion']['cost_comparison'] = {'target_passed': True, 'saving_ratio': '1'}
    with pytest.raises(ValueError, match='adoption_promotion_inconsistent'):
        await verify_async(reseal(record), plan_path=plan)


async def test_rehashed_duplicate_run_inventory_is_rejected(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record['runs'][-1] = dict(record['runs'][0])
    record['run_summary'] = adoption_module.run_summary(record['runs'])
    with pytest.raises(ValueError, match='adoption_runs_invalid'):
        await verify_async(reseal(record), plan_path=plan)


async def test_rehashed_paired_failure_cannot_keep_successful_promotion(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record['runs'][0]['quality_passed'] = False
    record['run_summary'] = adoption_module.run_summary(record['runs'])
    with pytest.raises(ValueError, match='adoption_promotion_inconsistent'):
        await verify_async(reseal(record), plan_path=plan)


async def test_expected_candidate_is_an_independent_binding(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    await verify_async(record, expected_candidate=CANDIDATE)
    with pytest.raises(ValueError, match="adoption_candidate_mismatch"):
        await verify_async(record, expected_candidate="f" * 40)


async def test_candidate_must_exist_and_match_the_executed_sources(plan):
    with pytest.raises(ValueError, match="adoption_candidate_source_invalid"):
        await adoption_module.adoption(candidate="f" * 40, plan_path=plan)
    root = plan.parent
    source = root / "backend/orchestrator/context_budget.py"
    source.write_bytes(source.read_bytes() + b"\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Synthetic Test",
                    "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Changed source anchor"], check=True)
    changed = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    with pytest.raises(ValueError, match="adoption_candidate_source_changed"):
        await adoption_module.adoption(candidate=changed, plan_path=plan)
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record["candidate"] = changed
    with pytest.raises(ValueError, match="adoption_candidate_source_changed"):
        await verify_async(reseal(record), plan_path=plan)


async def test_git_candidate_binds_plan_and_declared_flag_source(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    plan.write_text("Modified accepted plan bytes", encoding="utf-8")
    with pytest.raises(ValueError, match="adoption_candidate_source_changed"):
        await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record["plan_digest"] = plan_identity(plan)
    with pytest.raises(ValueError, match="adoption_candidate_source_changed"):
        await verify_async(reseal(record), plan_path=plan)
    plan.write_text("Accepted feature plan bytes for the adoption record.\n", encoding="utf-8")
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    flags = plan.parent / "backend/shared/feature_flags.py"
    flags.write_bytes(flags.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="adoption_candidate_source_changed"):
        await verify_async(record)


async def test_external_inputs_are_retained_without_mutable_aliases(plan):
    provider = {"candidate": "b" * 40, "records": [{"synthetic": "unqualified"}]}
    formative = {"participants": []}
    qualification = {"checks": {}}
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan,
        provider_measurements=provider, formative=formative, qualification=qualification)
    assert record["inputs"]["provider_measurements"] == provider
    assert record["inputs"]["formative"] == formative
    assert record["inputs"]["qualification"] == qualification
    assert set(record["promotion"]["failed"]) == {"real_provider_measurements_invalid",
        "five_user_formative_evidence_invalid", "product_and_client_qualification_invalid"}
    provider["records"].clear()
    formative["participants"].append("later mutation")
    qualification["checks"]["capability"] = True
    assert record["inputs"]["provider_measurements"]["records"]
    assert record["inputs"]["formative"]["participants"] == []
    assert record["inputs"]["qualification"]["checks"] == {}
    await verify_async(record, expected_candidate=CANDIDATE)


async def test_rehashed_raw_evidence_change_cannot_keep_a_missing_class(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record["inputs"]["provider_measurements"] = {"candidate": "b" * 40}
    with pytest.raises(ValueError, match="adoption_promotion_inconsistent"):
        await verify_async(reseal(record))


@pytest.mark.parametrize("replacement", [[], {}, {"unexpected": None}])
async def test_rehashed_input_envelope_must_remain_complete(plan, replacement):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record["inputs"] = replacement
    with pytest.raises(ValueError, match="adoption_inputs_invalid"):
        await verify_async(reseal(record))


@pytest.mark.parametrize(("field", "replacement"), [
    ("workflow", []), ("split", "unknown"), ("source_identity", "0" * 64),
    ("task_identity", "0" * 64), ("control", "unknown"), ("quality_passed", 1),
    ("exact_reconstruction", 0), ("source_operations", True), ("source_operations", 0),
    ("context_bytes_upper_bound_before", -1), ("context_bytes_upper_bound_after", "1"),
    ("auxiliary_model_attempts", False), ("context_status", "unknown"), ("context_reason", ""),
    ("source_unavailable_reason", []), ("reported_provider_cost", 0), ("complete_cost_known", True),
])
async def test_rehashed_malformed_run_cannot_claim_capability(plan, field, replacement):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record["runs"][0][field] = replacement
    with pytest.raises(ValueError, match="adoption_runs_invalid"):
        await verify_async(reseal(record))


@pytest.mark.parametrize("replacement", [None, {}, [None] * 96])
async def test_rehashed_run_shape_is_checked_before_summary(plan, replacement):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record["runs"] = replacement
    with pytest.raises(ValueError, match="adoption_runs_invalid"):
        await verify_async(reseal(record))


@pytest.mark.parametrize(("field", "replacement"), [
    ("measurement_kind", "real_provider"), ("provider_route", "claimed-live"),
    ("measurement_provider_route", {}), ("external_evidence_origin", "verified"),
])
async def test_rehashed_diagnostic_cannot_claim_provider_or_external_origin(plan, field, replacement):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record[field] = replacement
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async(reseal(record))


async def test_rehashed_declared_defaults_are_rechecked(plan, monkeypatch):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    monkeypatch.setattr(adoption_module, "control_defaults", lambda: dict.fromkeys(adoption_module.CONTROL_FLAGS, True))
    with pytest.raises(ValueError, match="adoption_defaults_changed"):
        await verify_async(record)


@pytest.mark.parametrize("value", [object(), float("nan"), "\ud800"])
async def test_non_json_inputs_are_rejected_without_an_output(plan, value):
    with pytest.raises(ValueError, match="adoption_inputs_invalid"):
        await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan, provider_measurements=value)


async def test_input_and_record_bounds_are_enforced(plan, monkeypatch):
    monkeypatch.setattr(adoption_module, "_MAX_INPUT_BYTES", 2 * 1024 * 1024)
    with pytest.raises(ValueError, match="adoption_inputs_invalid"):
        await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan, provider_measurements="x" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async({"oversized": "x" * (3 * 1024 * 1024)})


def test_git_binding_rejects_non_commit_and_non_blob_sources(plan, monkeypatch):
    real_git = adoption_module._git

    def wrong_commit(root, *arguments, input=None):
        if arguments[0] == "rev-parse":
            return b"0" * 40 + b"\n"
        return real_git(root, *arguments, input=input)

    monkeypatch.setattr(adoption_module, "_git", wrong_commit)
    with pytest.raises(ValueError, match="adoption_candidate_source_invalid"):
        adoption_module._candidate_binding(CANDIDATE, plan)
    monkeypatch.setattr(adoption_module, "_git", real_git)
    with pytest.raises(ValueError, match="adoption_plan_invalid"):
        adoption_module._candidate_binding(CANDIDATE, plan.parent.parent / "outside.md")
    for response in (b"", b"missing\n", b"0 blob 0\n", b"0 tree 2\n", b"0 blob 999999999\n"):
        def invalid_metadata(root, *arguments, input=None):
            if arguments == ("cat-file", "--batch-check"):
                return response * len(input.splitlines())
            return real_git(root, *arguments, input=input)
        monkeypatch.setattr(adoption_module, "_git", invalid_metadata)
        with pytest.raises(ValueError, match="adoption_candidate_source_invalid"):
            adoption_module._candidate_binding(CANDIDATE, plan)
    def excessive_inventory(root, *arguments, input=None):
        if arguments == ("cat-file", "--batch-check"):
            return (b"0 blob 5242880\n") * len(input.splitlines())
        return real_git(root, *arguments, input=input)
    monkeypatch.setattr(adoption_module, "_git", excessive_inventory)
    with pytest.raises(ValueError, match="adoption_candidate_source_invalid"):
        adoption_module._candidate_binding(CANDIDATE, plan)


@pytest.mark.parametrize("change", ["header", "separator", "trailing"])
def test_git_binding_checks_blob_framing(plan, monkeypatch, change):
    real_git = adoption_module._git

    def corrupt(root, *arguments, input=None):
        content = real_git(root, *arguments, input=input)
        if arguments == ("cat-file", "--batch"):
            if change == "header":
                return b"X" + content[1:]
            if change == "trailing":
                return content + b"X"
            return content[:-1] + b"X"
        return content

    monkeypatch.setattr(adoption_module, "_git", corrupt)
    with pytest.raises(ValueError, match="adoption_candidate_source_invalid"):
        adoption_module._candidate_binding(CANDIDATE, plan)


@pytest.mark.parametrize("scenario", ["corrupt", "cancelled"])
async def test_rehashed_frozen_scenario_forgery_is_replayed(plan, scenario):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    expected = next(item for item in diagnostic.load_workflows() if item["kind"] == scenario)
    run = next(item for item in record["runs"] if item["workflow"] == expected["id"] and item["control"] == "combined")
    run.update(dict.fromkeys(adoption_module.PASS_FIELDS, True))
    run.update({"integrity_failure_verified": False, "source_unavailable": False,
                "source_unavailable_reason": None, "context_status": "accepted", "context_reason": "compacted",
                "auxiliary_model_attempts": 0})
    record["run_summary"] = adoption_module.run_summary(record["runs"])
    record["promotion"] = diagnostic.promotion(record["runs"], candidate=CANDIDATE, **record["inputs"])
    record["decision_reasons"] = adoption_module._decision_reasons(record["promotion"], record["controls_default_off"])
    with pytest.raises(ValueError, match="adoption_runs_inconsistent"):
        await verify_async(reseal(record))


async def test_rehashed_known_boundary_failure_cannot_claim_passing_replay(plan, monkeypatch):
    original_read = diagnostic.EvidenceArchive.read
    def bypass(self, reference, **kwargs):
        kwargs["owner_id"] = "synthetic-owner"
        return original_read(self, reference, **kwargs)
    monkeypatch.setattr(diagnostic.EvidenceArchive, "read", bypass)
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    assert any(run["boundary_passed"] is False for run in record["runs"])
    for run in record["runs"]:
        run.update(dict.fromkeys(adoption_module.PASS_FIELDS, True))
    record["run_summary"] = adoption_module.run_summary(record["runs"])
    record["promotion"] = diagnostic.promotion(record["runs"], candidate=CANDIDATE, **record["inputs"])
    record["decision_reasons"] = adoption_module._decision_reasons(record["promotion"], record["controls_default_off"])
    with pytest.raises(ValueError, match="adoption_runs_inconsistent"):
        await verify_async(reseal(record))


async def test_sync_verifier_refuses_an_existing_event_loop(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    with pytest.raises(ValueError, match="adoption_async_verification_required"):
        verify(record)


async def test_non_string_plan_identity_is_rejected_before_path_use(plan):
    record = await adoption_module.adoption(candidate=CANDIDATE, plan_path=plan)
    record["plan_path"] = {}
    with pytest.raises(ValueError, match="adoption_record_invalid"):
        await verify_async(reseal(record))


def test_cli_without_git_metadata_fails_closed(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("unversioned-source")
    plan = root / "plan.md"
    plan.write_text("Unversioned accepted plan", encoding="utf-8")
    output = root / "adoption.json"
    monkeypatch.setattr(adoption_module, "_repository_root", lambda: root)
    monkeypatch.setattr("sys.argv", ["adoption", "--candidate", "a" * 40, "--plan", str(plan),
                                     "--output", str(output)])
    with pytest.raises(ValueError, match="adoption_candidate_source_invalid"):
        adoption_module.main()
    assert not output.exists()
