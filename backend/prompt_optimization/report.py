"""Candidate-bound artifact export: schema-validates and writes the inert prompt-text
candidate JSON and the full optimization report containing baseline/candidate hashes,
fixture/source/optimizer/model identities, partition results, the complete call and
failure inventory, and the adoption/rejection recommendation. Exports are data only and
are never loaded as code.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

from prompt_optimization.optimizer import OptimizationResult

CANDIDATE_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 256 * 1024
ADOPTION_STATES = ("adopt", "reject", "inconclusive")
RUN_STATUSES = ("completed", "cancelled", "budget_exhausted", "failed")


class ArtifactError(RuntimeError):
    pass


def candidate_artifact(result: OptimizationResult) -> dict:
    baseline = result.task["baseline_instruction"]
    selected = _selected_instruction(result)
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "artifact_kind": "prompt_candidate",
        "run_id": result.run_id,
        "task_id": result.task["task_id"],
        "allowlisted_field": "rules",
        "baseline_instruction": baseline,
        "candidate_instruction": selected if selected is not None else "",
        "baseline_instruction_sha256": hashlib.sha256(
            baseline.encode("utf-8")
        ).hexdigest(),
        "candidate_instruction_sha256": (
            hashlib.sha256(selected.encode("utf-8")).hexdigest()
            if selected is not None
            else ""
        ),
        "adoption_status": "proposed_not_applied",
        "recommendation": result.recommendation,
    }


def _selected_instruction(result: OptimizationResult) -> Optional[str]:
    if not result.selected_digest:
        return None
    for candidate in result.candidates:
        if candidate["instruction_digest"] == result.selected_digest:
            return candidate["instruction"]
    return None


def validate_candidate(artifact: dict) -> List[str]:
    errors: List[str] = []
    required = {
        "schema_version",
        "artifact_kind",
        "run_id",
        "task_id",
        "allowlisted_field",
        "baseline_instruction",
        "candidate_instruction",
        "baseline_instruction_sha256",
        "candidate_instruction_sha256",
        "adoption_status",
        "recommendation",
    }
    missing = required - set(artifact)
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
        return errors
    if artifact["schema_version"] != CANDIDATE_SCHEMA_VERSION:
        errors.append("unsupported candidate schema_version")
    if artifact["artifact_kind"] != "prompt_candidate":
        errors.append("artifact_kind must be prompt_candidate")
    for field in ("run_id", "task_id", "allowlisted_field", "adoption_status"):
        if not isinstance(artifact[field], str) or not artifact[field]:
            errors.append(f"{field} must be a non-empty string")
    if artifact["allowlisted_field"] != "rules":
        errors.append("allowlisted_field must be the declared rules field")
    if artifact["adoption_status"] != "proposed_not_applied":
        errors.append("adoption_status must remain proposed_not_applied")
    if artifact["recommendation"] not in ADOPTION_STATES:
        errors.append("recommendation must be adopt, reject, or inconclusive")
    for field in ("baseline_instruction", "candidate_instruction"):
        if not isinstance(artifact[field], str):
            errors.append(f"{field} must be a string")
    for field in (
        "baseline_instruction_sha256",
        "candidate_instruction_sha256",
    ):
        value = artifact[field]
        if not isinstance(value, str) or (value and len(value) != 64):
            errors.append(f"{field} must be a 64-character digest or empty")
    if (
        isinstance(artifact["baseline_instruction"], str)
        and artifact["baseline_instruction_sha256"]
        and hashlib.sha256(
            artifact["baseline_instruction"].encode("utf-8")
        ).hexdigest()
        != artifact["baseline_instruction_sha256"]
    ):
        errors.append("baseline_instruction digest mismatch")
    if (
        isinstance(artifact["candidate_instruction"], str)
        and artifact["candidate_instruction"]
        and artifact["candidate_instruction_sha256"]
        and hashlib.sha256(
            artifact["candidate_instruction"].encode("utf-8")
        ).hexdigest()
        != artifact["candidate_instruction_sha256"]
    ):
        errors.append("candidate_instruction digest mismatch")
    return errors


def validate_report(report: dict) -> List[str]:
    errors: List[str] = []
    required = {
        "schema_version",
        "run_id",
        "created_at",
        "suite_version",
        "status",
        "qualified",
        "recommendation",
        "recommendation_basis",
        "task",
        "fixture",
        "optimizer",
        "model",
        "budget",
        "baseline_train",
        "baseline_validation",
        "baseline_holdout",
        "selected_holdout",
        "candidates",
        "selected_digest",
        "selection_pool",
        "proposal_errors",
        "next_holdout_required",
    }
    missing = required - set(report)
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
        return errors
    if report["schema_version"] != REPORT_SCHEMA_VERSION:
        errors.append("unsupported report schema_version")
    if report["status"] not in RUN_STATUSES:
        errors.append("unknown run status")
    if report["recommendation"] not in ADOPTION_STATES:
        errors.append("recommendation must be adopt, reject, or inconclusive")
    if not isinstance(report["qualified"], bool):
        errors.append("qualified must be a boolean")
    if report["recommendation"] == "adopt" and (
        not report["qualified"] or report["status"] != "completed"
    ):
        errors.append("adopt requires a qualified completed run")
    if not isinstance(report["candidates"], list) or not all(
        isinstance(entry, dict) for entry in report["candidates"]
    ):
        errors.append("candidates must be a list of objects")
    if not isinstance(report["proposal_errors"], list):
        errors.append("proposal_errors must be a list")
    budget = report.get("budget")
    if not isinstance(budget, dict) or not isinstance(budget.get("calls"), list):
        errors.append("budget inventory with call records is required")
    else:
        for record in budget["calls"]:
            if not isinstance(record, dict) or not {
                "sequence",
                "kind",
                "phase",
                "subject",
                "outcome",
                "attempts",
                "reserved_tokens",
                "actual_tokens",
                "usage_complete",
            } <= set(record):
                errors.append("call inventory record is incomplete")
                break
    task = report.get("task")
    if not isinstance(task, dict) or not isinstance(
        task.get("allowlisted_fields"), list
    ):
        errors.append("task identity with allowlisted fields is required")
    fixture = report.get("fixture")
    if not isinstance(fixture, dict) or not fixture.get("digest"):
        errors.append("fixture identity with digest is required")
    return errors


def build_report(result: OptimizationResult) -> dict:
    report = result.to_dict()
    report["schema_version"] = REPORT_SCHEMA_VERSION
    return report


def export_artifacts(result: OptimizationResult, out_dir: Path) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    candidate = candidate_artifact(result)
    report = build_report(result)
    candidate_errors = validate_candidate(candidate)
    report_errors = validate_report(report)
    if candidate_errors or report_errors:
        raise ArtifactError(
            "artifact validation failed: "
            + "; ".join(candidate_errors + report_errors)
        )
    candidate_bytes = _serialize(candidate)
    report_bytes = _serialize(report)
    for name, payload in (
        ("candidate_prompt.json", candidate_bytes),
        ("optimization_report.json", report_bytes),
    ):
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ArtifactError(f"{name} exceeds {MAX_ARTIFACT_BYTES} bytes")
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = out_dir / "candidate_prompt.json"
    report_path = out_dir / "optimization_report.json"
    candidate_path.write_bytes(candidate_bytes)
    report["candidate_artifact_sha256"] = hashlib.sha256(candidate_bytes).hexdigest()
    report_path.write_bytes(_serialize(report))
    return {"candidate": candidate_path, "report": report_path}


def _serialize(payload: dict) -> bytes:
    return json.dumps(
        payload, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
