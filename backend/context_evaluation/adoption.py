"""Records the candidate-bound context adoption decision from the frozen paired diagnostic.
The record binds the evaluated candidate and the accepted feature plan, retains failed and
unknown results, and never changes defaults or waives implementation gates.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from context_evaluation import evaluate as diagnostic

ADOPTION_FORMAT = "astral.context-adoption/v1"
DECISION_ADOPTED = "adopted"
DECISION_WITHHELD = "withheld"
CONTROL_FLAGS = {"message_compaction": "FF_MESSAGE_COMPACTION",
                 "observation_packing": "FF_OBSERVATION_PACKING",
                 "safe_compaction": "FF_SAFE_COMPACTION"}
PASS_FIELDS = ("quality_passed", "boundary_passed", "failure_preservation_passed", "scenario_passed")
DECISION_RULES = {
    "adopted_requires": ("every-paired-run-passes, all-supplied-evidence-classes-valid-and-passing,"
                         " and-context-controls-default-off"),
    "complete_cost_target_percent": diagnostic.SCORING_RULES["minimum_saving_percent"],
    "context_defaults": "observation-packing-safe-compaction-message-compaction-remain-off",
    "evidence_classes": ["five_user_formative_evidence", "product_and_client_qualification",
                         "real_provider_measurements"],
    "missing_evidence": "recorded-as-missing-never-counted-as-passing-or-zero-cost",
    "paired_oracle": diagnostic.SCORING,
    "record_scope": "candidate-and-accepted-feature-plan-with-separate-development-held-out-fixtures",
    "retained_results": "failed-runs-source-identities-and-unknown-accounting-stay-in-the-record",
}
_RECORD_FIELDS = {"acceptance_rules", "candidate", "controls_default_off", "decision", "decision_reasons",
                  "defaults_changed", "format", "implementation_digest", "measurement_kind",
                  "measurement_provider_route", "plan_digest", "plan_path", "promotion", "provider_route",
                  "receipt_digest", "run_summary", "runs", "scoring", "scoring_digest", "waived_gates",
                  "workflows_digest"}


def _candidate_valid(candidate) -> bool:
    return type(candidate) is str and re.fullmatch(r"[a-f0-9]{40}", candidate) is not None


def plan_identity(plan_path) -> str:
    limit = 1024 * 1024
    try:
        descriptor = os.open(plan_path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0 or metadata.st_size > limit:
                raise ValueError
            content = source.read(limit + 1)
        if len(content) > limit:
            raise ValueError
        content.decode("utf-8")
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ValueError("adoption_plan_invalid") from None
    return hashlib.sha256(content).hexdigest()


def _plan_label(plan_path) -> str:
    label = str(plan_path)
    if not label or len(label) > 512 or any(character in label for character in "\x00\n\r"):
        raise ValueError("adoption_plan_invalid")
    return label


def control_defaults() -> dict[str, bool]:
    saved = {variable: os.environ.pop(variable, None) for variable in CONTROL_FLAGS.values()}
    try:
        from shared.feature_flags import FeatureFlags

        flags = FeatureFlags()
        return {name: flags.is_enabled(name) for name in sorted(CONTROL_FLAGS)}
    finally:
        for variable, value in saved.items():
            if value is not None:
                os.environ[variable] = value


def _run_passed(run) -> bool:
    return all(run.get(field) is True for field in PASS_FIELDS) and run.get("exact_reconstruction") is not False


def run_summary(runs) -> dict:
    controls = sorted({run.get("control") for run in runs})
    splits = sorted({run.get("split") for run in runs})
    return {
        "total": len(runs),
        "passed": sum(1 for run in runs if _run_passed(run)),
        "by_control": {control: {"passed": sum(1 for run in runs if run.get("control") == control and _run_passed(run)),
                                 "total": sum(1 for run in runs if run.get("control") == control)}
                       for control in controls},
        "by_split": {split: {"passed": sum(1 for run in runs if run.get("split") == split and _run_passed(run)),
                             "total": sum(1 for run in runs if run.get("split") == split)}
                     for split in splits},
        "failed_workflows": sorted({run.get("workflow") for run in runs if not _run_passed(run)}),
        "unknown_complete_cost_runs": sum(1 for run in runs if run.get("complete_cost_known") is not True),
    }


def _decision_reasons(promotion, flags) -> list[str]:
    reasons = [*promotion["failed"], *promotion["missing"]]
    if any(flags.values()):
        reasons.append("context_defaults_not_off")
    return list(dict.fromkeys(reasons))


async def adoption(*, candidate, plan_path, provider_measurements=None, formative=None,
                   qualification=None, promoted_control="combined"):
    if not _candidate_valid(candidate):
        raise ValueError("adoption_candidate_invalid")
    plan_digest = plan_identity(plan_path)
    report = await diagnostic.evaluate(candidate=candidate, provider_measurements=provider_measurements,
                                       formative=formative, qualification=qualification,
                                       promoted_control=promoted_control)
    flags = control_defaults()
    reasons = _decision_reasons(report["promotion"], flags)
    record = {
        "format": ADOPTION_FORMAT,
        "candidate": candidate,
        "implementation_digest": report["implementation_digest"],
        "plan_path": _plan_label(plan_path),
        "plan_digest": plan_digest,
        "workflows_digest": report["workflows_digest"],
        "scoring": report["scoring"],
        "scoring_digest": report["scoring_digest"],
        "provider_route": report["provider_route"],
        "measurement_provider_route": report["measurement_provider_route"],
        "measurement_kind": report["measurement_kind"],
        "controls_default_off": flags,
        "acceptance_rules": DECISION_RULES,
        "run_summary": run_summary(report["runs"]),
        "runs": report["runs"],
        "promotion": report["promotion"],
        "decision": DECISION_ADOPTED if not reasons else DECISION_WITHHELD,
        "decision_reasons": reasons,
        "defaults_changed": False,
        "waived_gates": [],
        "receipt_digest": "",
    }
    record["receipt_digest"] = diagnostic._identity({key: value for key, value in record.items()
                                                     if key != "receipt_digest"})
    return record


def verify(record, *, plan_path=None) -> None:
    if type(record) is not dict or set(record) != _RECORD_FIELDS:
        raise ValueError("adoption_record_invalid")
    if record["format"] != ADOPTION_FORMAT or not _candidate_valid(record["candidate"]):
        raise ValueError("adoption_record_invalid")
    if record["implementation_digest"] != diagnostic.implementation_identity():
        raise ValueError("adoption_implementation_changed")
    if record["workflows_digest"] != diagnostic._identity(diagnostic.load_workflows()):
        raise ValueError("adoption_workflows_changed")
    if (record["scoring"] != diagnostic.SCORING
            or record["scoring_digest"] != diagnostic._identity(diagnostic.SCORING_RULES)):
        raise ValueError("adoption_scoring_changed")
    if plan_path is not None and record["plan_digest"] != plan_identity(plan_path):
        raise ValueError("adoption_plan_changed")
    if run_summary(record["runs"]) != record["run_summary"]:
        raise ValueError("adoption_summary_inconsistent")
    flags = record["controls_default_off"]
    if (type(flags) is not dict or set(flags) != set(CONTROL_FLAGS)
            or any(type(enabled) is not bool for enabled in flags.values())):
        raise ValueError("adoption_record_invalid")
    if record["decision_reasons"] != _decision_reasons(record["promotion"], flags):
        raise ValueError("adoption_decision_inconsistent")
    expected = DECISION_ADOPTED if not record["decision_reasons"] else DECISION_WITHHELD
    if record["decision"] != expected:
        raise ValueError("adoption_decision_inconsistent")
    if record["defaults_changed"] is not False or record["waived_gates"] != []:
        raise ValueError("adoption_posture_invalid")
    receipt = diagnostic._identity({key: value for key, value in record.items() if key != "receipt_digest"})
    if record["receipt_digest"] != receipt:
        raise ValueError("adoption_receipt_invalid")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--measurements", type=Path)
    args = parser.parse_args()
    evidence = diagnostic._read_measurements(args.measurements) if args.measurements else {}
    record = asyncio.run(adoption(candidate=args.candidate, plan_path=args.plan, **evidence))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
