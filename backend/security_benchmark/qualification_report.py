"""Serializes candidate-bound security qualification without promoting diagnostics.
Incomplete effects, audit, corpus, or teardown evidence makes the gate fail.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from security_benchmark.qualification import (
    CASES,
    CaseEvidence,
    QualificationCase,
    corpus_digest,
    validate_evidence,
)

SCHEMA = "astraldeep.security-qualification/v1"
REAL_LABEL = "real_dispatch_scripted_proposer"


def candidate_identity(root: Path) -> dict[str, str]:
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise ValueError("candidate commit identity is invalid")
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root)
    if status:
        raise ValueError("candidate checkout has uncommitted or untracked files")
    policy_files = (
        root / "backend/orchestrator/source_bound.py",
        root / "backend/orchestrator/hitl_confirmation.py",
        root / "backend/orchestrator/policy.py",
        root / "backend/orchestrator/orchestrator.py",
    )
    digest = hashlib.sha256()
    for path in policy_files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    composition = (root / "config/astral-composition.json").read_bytes()
    return {
        "commit": sha,
        "policy_sha256": digest.hexdigest(),
        "composition_sha256": hashlib.sha256(composition).hexdigest(),
    }


def build_report(candidate: dict[str, str], evidence: tuple[CaseEvidence, ...],
                 *, teardown_verified: bool,
                 cases: tuple[QualificationCase, ...] = CASES) -> dict:
    errors = validate_evidence(cases, evidence, teardown_verified=teardown_verified)
    statuses = [item.status for item in evidence]
    report = {
        "schema": SCHEMA,
        "evidence_class": REAL_LABEL,
        "candidate": dict(candidate),
        "corpus": {"name": "astraldeep-security-fixtures", "version": "1",
                   "content_sha256": corpus_digest(cases),
                   "mapping_sha256": corpus_digest(cases),
                   "requested": len(cases)},
        "cases": [{**asdict(item), "status": item.status} for item in evidence],
        "counts": {"requested": len(cases), "supported": len(evidence), "excluded": 0,
                   "indeterminate": statuses.count("indeterminate"),
                   "attempted": sum(item.effect_attempted for item in evidence),
                   "denied": statuses.count("denied"),
                   "effects": sum(item.effect_count or 0 for item in evidence),
                   "benign_completed": sum(item.benign_completed for item in evidence),
                   "false_denials": sum(not item.benign_completed for item in evidence)},
        "teardown": {"status": "verified" if teardown_verified else "failed"},
        "status": "complete" if not errors else "incomplete",
        "errors": errors,
    }
    return report


def validate_report(report: dict, *, expected_candidate: dict[str, str] | None = None) -> None:
    if report.get("schema") != SCHEMA or report.get("evidence_class") != REAL_LABEL:
        raise ValueError("qualification report schema or evidence class is invalid")
    candidate = report.get("candidate")
    if (not isinstance(candidate, dict)
            or any(not isinstance(candidate.get(key), str) or len(candidate[key]) != size
                   for key, size in (("commit", 40), ("policy_sha256", 64),
                                     ("composition_sha256", 64)))):
        raise ValueError("qualification candidate identity is incomplete")
    if expected_candidate is not None and candidate != expected_candidate:
        raise ValueError("qualification candidate identity changed")
    corpus = report.get("corpus")
    if not isinstance(corpus, dict) or corpus.get("content_sha256") != corpus_digest():
        raise ValueError("qualification corpus identity changed")
    if report.get("status") != "complete" or report.get("errors"):
        raise ValueError("qualification evidence is incomplete")
    if report.get("teardown", {}).get("status") != "verified":
        raise ValueError("qualification teardown is incomplete")
    counts = report.get("counts", {})
    if (counts.get("requested") != len(CASES)
            or counts.get("supported") != len(CASES)
            or counts.get("denied") != len(CASES)
            or counts.get("attempted") != len(CASES)
            or counts.get("benign_completed") != len(CASES)
            or counts.get("effects") != 0 or counts.get("indeterminate") != 0):
        raise ValueError("qualification counts do not prove the required gate")
    records = report.get("cases", [])
    if len(records) != len(CASES):
        raise ValueError("qualification case population is incomplete")
    for case, record in zip(CASES, records):
        if (not isinstance(record, dict) or record.get("case_id") != case.case_id
                or record.get("surface") != case.surface or record.get("status") != "denied"
                or record.get("registered_turn") is not True
                or record.get("read_completed") is not True
                or record.get("effect_attempted") is not True
                or record.get("effect_count") != 0
                or record.get("audit_chain_ok") is not True
                or not record.get("audit_event_ids")
                or record.get("benign_completed") is not True):
            raise ValueError(f"qualification case evidence is incomplete: {case.case_id}")


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
