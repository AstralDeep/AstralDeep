"""Tests for candidate-bound artifact export: schema validation of the inert candidate
prompt JSON and the full report, digest binding, size bounds, and fail-closed export.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from prompt_optimization import report as report_module
from prompt_optimization import task as task_module

REPO_ROOT = Path(task_module.__file__).resolve().parents[2]


@pytest.fixture()
def result():
    from prompt_optimization import budget as budget_module
    from prompt_optimization import model_io
    from prompt_optimization import optimizer as optimizer_module
    from prompt_optimization import proposals as proposals_module
    from prompt_optimization.fixture import build_fixture

    optimizer_module.reset_consumed_holdouts()
    variants = proposals_module.scripted_variants(
        "- tool_hints should reference actual tool names from the available tools "
        "list.\n"
    )
    adapter = model_io.ProviderDouble(variants=variants)
    prompt = task_module.load_task_prompt(REPO_ROOT)
    run = optimizer_module.OptimizationRun(
        fixture=build_fixture(),
        task_prompt=prompt,
        adapter=adapter,
        limits=budget_module.BudgetLimits(
            max_reflection_calls=6,
            max_evaluation_calls=96,
            max_total_tokens=400000,
        ),
        controls=model_io.Controls(lambda: False),
        model_identity="scripted:test-double",
        run_id="fixed-run-id",
    )
    run.proposal_source = proposals_module.ReflectionProposalSource(
        adapter=adapter,
        budget=run.budget,
        controls=run.controls,
        max_candidates=4,
        max_output_chars=4000,
        max_attempts=1,
    )
    outcome = run.execute()
    optimizer_module.reset_consumed_holdouts()
    return outcome


def test_candidate_artifact_is_inert_text(result):
    artifact = report_module.candidate_artifact(result)
    assert artifact["artifact_kind"] == "prompt_candidate"
    assert artifact["adoption_status"] == "proposed_not_applied"
    assert artifact["allowlisted_field"] == "rules"
    assert artifact["recommendation"] == "adopt"
    assert artifact["baseline_instruction_sha256"] == hashlib.sha256(
        artifact["baseline_instruction"].encode("utf-8")
    ).hexdigest()
    assert artifact["candidate_instruction_sha256"] == hashlib.sha256(
        artifact["candidate_instruction"].encode("utf-8")
    ).hexdigest()
    assert report_module.validate_candidate(artifact) == []
    encoded = json.dumps(artifact)
    assert "__import__" not in encoded
    assert artifact["schema_version"] == report_module.CANDIDATE_SCHEMA_VERSION


def test_candidate_validation_rejects_tampering(result):
    artifact = report_module.candidate_artifact(result)
    artifact["candidate_instruction"] = artifact[
        "candidate_instruction"
    ] + " tampered"
    errors = report_module.validate_candidate(artifact)
    assert "candidate_instruction digest mismatch" in errors


def test_candidate_validation_missing_fields():
    errors = report_module.validate_candidate({"schema_version": 1})
    assert any("missing fields" in error for error in errors)


def test_candidate_validation_rejects_wrong_field_and_status(result):
    artifact = report_module.candidate_artifact(result)
    artifact["allowlisted_field"] = "head"
    artifact["adoption_status"] = "auto_applied"
    errors = report_module.validate_candidate(artifact)
    assert "allowlisted_field must be the declared rules field" in errors
    assert "adoption_status must remain proposed_not_applied" in errors


def test_report_contains_candidate_bound_evidence(result):
    report = report_module.build_report(result)
    assert report["schema_version"] == report_module.REPORT_SCHEMA_VERSION
    assert report["task"]["source_path"] == "backend/orchestrator/coordinator.py"
    assert report["task"]["allowlisted_fields"] == ["rules"]
    assert report["fixture"]["digest"]
    assert set(report["fixture"]["partition_digests"]) == {
        "train", "validation", "holdout"
    }
    assert report["optimizer"]["name"] == "bounded-reflection-v1"
    assert report["model"]["mode"] == "scripted:test-double"
    assert report["model"]["data_scope"] == "synthetic-fixture-only"
    assert report["model"]["egress"] == "none"
    assert report["budget"]["calls"]
    assert report["selected_digest"]
    assert report["baseline_holdout"]["mean"] is not None
    assert report["selected_holdout"]["mean"] is not None
    assert report["proposal_errors"] == []
    assert report_module.validate_report(report) == []


def test_report_adopt_requires_qualified_completed_run(result):
    report = report_module.build_report(result)
    report["qualified"] = False
    errors = report_module.validate_report(report)
    assert "adopt requires a qualified completed run" in errors
    report["status"] = "cancelled"
    errors = report_module.validate_report(report)
    assert "adopt requires a qualified completed run" in errors


def test_report_validation_missing_fields():
    errors = report_module.validate_report({})
    assert any("missing fields" in error for error in errors)


def test_report_validation_rejects_incomplete_call_inventory(result):
    report = report_module.build_report(result)
    report["budget"]["calls"] = [{"sequence": 1}]
    errors = report_module.validate_report(report)
    assert "call inventory record is incomplete" in errors


def test_export_writes_bound_artifacts(result, tmp_path):
    out_dir = tmp_path / "artifacts"
    paths = report_module.export_artifacts(result, out_dir)
    candidate_bytes = paths["candidate"].read_bytes()
    report_payload = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert paths["candidate"].name == "candidate_prompt.json"
    assert paths["report"].name == "optimization_report.json"
    assert report_payload["candidate_artifact_sha256"] == hashlib.sha256(
        candidate_bytes
    ).hexdigest()
    assert report_payload["run_id"] == result.run_id
    reloaded = json.loads(candidate_bytes.decode("utf-8"))
    assert reloaded["task_id"] == result.task["task_id"]
    assert report_module.validate_candidate(reloaded) == []


def test_export_is_deterministic_per_result(result, tmp_path):
    first = report_module.export_artifacts(result, tmp_path / "a")
    second = report_module.export_artifacts(result, tmp_path / "b")
    report_a = json.loads(first["report"].read_text(encoding="utf-8"))
    report_b = json.loads(second["report"].read_text(encoding="utf-8"))
    report_a.pop("created_at")
    report_b.pop("created_at")
    assert report_a == report_b


def test_report_validation_rejects_malformed_sections(result):
    report = report_module.build_report(result)
    report["schema_version"] = 99
    report["status"] = "mysterious"
    report["recommendation"] = "maybe"
    report["qualified"] = "yes"
    report["candidates"] = "nope"
    report["proposal_errors"] = "nope"
    report["budget"] = {"calls": "nope"}
    report["task"] = {}
    report["fixture"] = {}
    errors = report_module.validate_report(report)
    joined = "; ".join(errors)
    assert "unsupported report schema_version" in joined
    assert "unknown run status" in joined
    assert "recommendation must be adopt, reject, or inconclusive" in joined
    assert "qualified must be a boolean" in joined
    assert "candidates must be a list of objects" in joined
    assert "proposal_errors must be a list" in joined
    assert "budget inventory with call records is required" in joined
    assert "task identity with allowlisted fields is required" in joined
    assert "fixture identity with digest is required" in joined


def test_report_validation_rejects_bad_candidate_entries(result):
    report = report_module.build_report(result)
    report["candidates"] = ["not-a-dict"]
    report["budget"]["calls"] = [{"sequence": 1}, "junk"]
    errors = report_module.validate_report(report)
    joined = "; ".join(errors)
    assert "candidates must be a list of objects" in joined
    assert "call inventory record is incomplete" in joined


def test_candidate_validation_rejects_malformed_fields(result):
    artifact = report_module.candidate_artifact(result)
    artifact["schema_version"] = 99
    artifact["artifact_kind"] = "executable_payload"
    artifact["run_id"] = ""
    artifact["task_id"] = None
    artifact["baseline_instruction"] = 5
    artifact["baseline_instruction_sha256"] = "short"
    artifact["candidate_instruction_sha256"] = 7
    errors = report_module.validate_candidate(artifact)
    joined = "; ".join(errors)
    assert "unsupported candidate schema_version" in joined
    assert "artifact_kind must be prompt_candidate" in joined
    assert "must be a non-empty string" in joined
    assert "baseline_instruction must be a string" in joined
    assert "must be a 64-character digest or empty" in joined


def test_candidate_validation_accepts_empty_candidate(result):
    artifact = report_module.candidate_artifact(result)
    artifact["candidate_instruction"] = ""
    artifact["candidate_instruction_sha256"] = ""
    assert report_module.validate_candidate(artifact) == []


def test_candidate_validation_both_digest_mismatches(result):
    artifact = report_module.candidate_artifact(result)
    artifact["baseline_instruction"] = (
        artifact["baseline_instruction"] + " tampered"
    )
    artifact["candidate_instruction"] = (
        artifact["candidate_instruction"] + " tampered"
    )
    errors = report_module.validate_candidate(artifact)
    assert "baseline_instruction digest mismatch" in errors
    assert "candidate_instruction digest mismatch" in errors


def test_export_enforces_size_bound(result, tmp_path, monkeypatch):
    monkeypatch.setattr(report_module, "MAX_ARTIFACT_BYTES", 32)
    with pytest.raises(report_module.ArtifactError) as excinfo:
        report_module.export_artifacts(result, tmp_path / "small")
    assert "exceeds" in str(excinfo.value)
    assert not (tmp_path / "small" / "candidate_prompt.json").exists()


def test_export_fails_closed_on_invalid_result(result, tmp_path):
    result.recommendation = "unheard-of"
    with pytest.raises(report_module.ArtifactError):
        report_module.export_artifacts(result, tmp_path / "x")
    assert not (tmp_path / "x" / "candidate_prompt.json").exists()


def test_selected_instruction_lookup(result):
    assert report_module._selected_instruction(result) is not None
    missing = report_module._selected_instruction(
        type(result)(
            run_id="r",
            created_at=0.0,
            suite_version="t",
            status="completed",
            qualified=True,
            recommendation="reject",
            recommendation_basis="b",
            task=result.task,
            fixture=result.fixture,
            optimizer=result.optimizer,
            model=result.model,
            budget=result.budget,
            baseline_train=None,
            baseline_validation=None,
            baseline_holdout=None,
            selected_holdout=None,
            candidates=result.candidates,
            selected_digest=None,
            selection_pool=[],
            proposal_errors=[],
            next_holdout_required=True,
        )
    )
    assert missing is None
    unmatched = report_module._selected_instruction(
        type(result)(
            run_id="r",
            created_at=0.0,
            suite_version="t",
            status="completed",
            qualified=True,
            recommendation="reject",
            recommendation_basis="b",
            task=result.task,
            fixture=result.fixture,
            optimizer=result.optimizer,
            model=result.model,
            budget=result.budget,
            baseline_train=None,
            baseline_validation=None,
            baseline_holdout=None,
            selected_holdout=None,
            candidates=[],
            selected_digest="unmatched-digest",
            selection_pool=[],
            proposal_errors=[],
            next_holdout_required=True,
        )
    )
    assert unmatched is None
