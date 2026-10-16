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
import subprocess

from context_evaluation import evaluate as diagnostic

ADOPTION_FORMAT = "astral.context-adoption/v2"
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
    "external_evidence_origin": "independent-verification-required-never-established-by-this-diagnostic",
    "synthetic_results": "replayed-with-the-source-bound-frozen-diagnostic",
}
_RECORD_FIELDS = {"acceptance_rules", "acceptance_rules_digest", "candidate", "controls_default_off", "decision", "decision_reasons",
                  "defaults_changed", "format", "implementation_digest", "measurement_kind",
                  "measurement_provider_route", "plan_digest", "plan_path", "promotion", "provider_route",
                  "receipt_digest", "run_summary", "runs", "scoring", "scoring_digest", "waived_gates",
                  "workflows_digest", "inputs", "external_evidence_origin", "defaults_digest"}
_INPUT_FIELDS = {"provider_route", "promoted_control", "provider_measurements", "formative", "qualification"}
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_RUN_FIELDS = {"workflow", "split", "control", "source_identity", "task_identity", "source_operations",
               *PASS_FIELDS, "exact_reconstruction", "integrity_failure_verified", "source_unavailable",
               "source_unavailable_reason", "context_status", "context_reason", "context_bytes_upper_bound_before",
               "context_bytes_upper_bound_after", "auxiliary_model_attempts", "reported_provider_cost", "complete_cost_known"}


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


def _repository_root():
    return Path(__file__).resolve().parents[2]


def _git(root, *arguments, input=None):
    try:
        return subprocess.run(["git", "-C", str(root), *arguments], input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError("adoption_candidate_source_invalid") from None


def _candidate_binding(candidate, plan_path):
    root = _repository_root().resolve()
    try:
        relative_plan = Path(plan_path).resolve().relative_to(root).as_posix()
    except (TypeError, ValueError, OSError):
        raise ValueError("adoption_plan_invalid") from None
    _plan_label(relative_plan)
    if _git(root, "rev-parse", "--verify", f"{candidate}^{{commit}}").strip().decode() != candidate:
        raise ValueError("adoption_candidate_source_invalid")
    paths = [f"backend/{path}" for path in diagnostic._IMPLEMENTATION_FILES]
    paths.extend(["backend/context_evaluation/workflows.json", "backend/shared/feature_flags.py", relative_plan])
    requests = "".join(f"{candidate}:{path}\n" for path in paths).encode()
    metadata = _git(root, "cat-file", "--batch-check", input=requests).splitlines()
    if len(metadata) != len(paths):
        raise ValueError("adoption_candidate_source_invalid")
    objects = []
    for line in metadata:
        parts = line.split()
        if (len(parts) != 3 or parts[1] != b"blob" or not parts[2].isdigit()
                or not 0 < int(parts[2]) <= 5 * 1024 * 1024):
            raise ValueError("adoption_candidate_source_invalid")
        objects.append((parts[0], int(parts[2])))
    if sum(size for _, size in objects) > _MAX_INPUT_BYTES:
        raise ValueError("adoption_candidate_source_invalid")
    contents = _git(root, "cat-file", "--batch", input=b"".join(oid + b"\n" for oid, _ in objects))
    blobs, position = {}, 0
    for path, (oid, size) in zip(paths, objects):
        header = oid + b" blob " + str(size).encode() + b"\n"
        if contents[position:position + len(header)] != header:
            raise ValueError("adoption_candidate_source_invalid")
        position += len(header)
        blobs[path] = contents[position:position + size]
        position += size
        if contents[position:position + 1] != b"\n":
            raise ValueError("adoption_candidate_source_invalid")
        position += 1
    if position != len(contents):
        raise ValueError("adoption_candidate_source_invalid")
    return {"implementation_digest": diagnostic._identity({path: hashlib.sha256(blobs[f"backend/{path}"]).hexdigest()
                                                          for path in diagnostic._IMPLEMENTATION_FILES}),
            "workflows_digest": diagnostic._identity(json.loads(blobs["backend/context_evaluation/workflows.json"])),
            "defaults_digest": hashlib.sha256(blobs["backend/shared/feature_flags.py"]).hexdigest(),
            "plan_path": relative_plan, "plan_digest": hashlib.sha256(blobs[relative_plan]).hexdigest()}


def _bounded_document(value, limit, reason):
    try:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(content) > limit:
            raise ValueError
        return json.loads(content)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError(reason) from None


def _validate_runs(runs):
    workflows = diagnostic.load_workflows()
    expected = {(item["id"], control): item for item in workflows for control in diagnostic.CONTROLS}
    if type(runs) is not list or len(runs) != len(expected):
        raise ValueError("adoption_runs_invalid")
    observed = set()
    for run in runs:
        try:
            if type(run) is not dict or set(run) != _RUN_FIELDS:
                raise ValueError
            pair = run["workflow"], run["control"]
            workflow = expected[pair]
            if pair in observed:
                raise ValueError
            observed.add(pair)
            if (run["split"] != workflow["split"] or run["source_identity"] != diagnostic._identity(workflow["source"])
                    or run["task_identity"] != diagnostic._identity(workflow["task"])
                    or any(type(run[field]) is not bool for field in (*PASS_FIELDS, "integrity_failure_verified", "source_unavailable"))
                    or (run["exact_reconstruction"] is not None and type(run["exact_reconstruction"]) is not bool)
                    or type(run["source_operations"]) is not int or run["source_operations"] != 1
                    or any(not diagnostic._number(run[field]) for field in
                           ("context_bytes_upper_bound_before", "context_bytes_upper_bound_after", "auxiliary_model_attempts"))
                    or not diagnostic._label(run["context_status"]) or run["context_status"] not in {"unchanged", "accepted", "rejected", "context_limit"}
                    or not diagnostic._label(run["context_reason"])
                    or (run["source_unavailable_reason"] is not None and not diagnostic._label(run["source_unavailable_reason"]))
                    or run["reported_provider_cost"] is not None or run["complete_cost_known"] is not False):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("adoption_runs_invalid") from None


def _measurement_route(inputs):
    try:
        return dict(diagnostic._route(inputs["provider_route"]))
    except ValueError:
        return None


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
    if promotion["eligible"]:
        reasons.append("external_evidence_origin_unverified")
    return list(dict.fromkeys(reasons))


async def adoption(*, candidate, plan_path, provider_measurements=None, formative=None,
                   qualification=None, promoted_control="combined", provider_route=None):
    if not _candidate_valid(candidate):
        raise ValueError("adoption_candidate_invalid")
    plan_digest = plan_identity(plan_path)
    binding = _candidate_binding(candidate, plan_path)
    if (binding["implementation_digest"] != diagnostic.implementation_identity()
            or binding["workflows_digest"] != diagnostic._identity(diagnostic.load_workflows())
            or binding["defaults_digest"] != plan_identity(_repository_root() / "backend/shared/feature_flags.py")
            or binding["plan_digest"] != plan_digest):
        raise ValueError("adoption_candidate_source_changed")
    inputs = _bounded_document({"provider_measurements": provider_measurements, "formative": formative,
                               "qualification": qualification, "promoted_control": promoted_control,
                               "provider_route": provider_route}, _MAX_INPUT_BYTES, "adoption_inputs_invalid")
    report = await diagnostic.evaluate(candidate=candidate, **inputs)
    flags = control_defaults()
    reasons = _decision_reasons(report["promotion"], flags)
    record = {
        "format": ADOPTION_FORMAT,
        "candidate": candidate,
        "implementation_digest": report["implementation_digest"],
        "plan_path": binding["plan_path"],
        "plan_digest": plan_digest,
        "defaults_digest": binding["defaults_digest"],
        "workflows_digest": report["workflows_digest"],
        "scoring": report["scoring"],
        "scoring_digest": report["scoring_digest"],
        "provider_route": report["provider_route"],
        "measurement_provider_route": report["measurement_provider_route"],
        "measurement_kind": report["measurement_kind"],
        "controls_default_off": flags,
        "acceptance_rules": _bounded_document(DECISION_RULES, 4096, "adoption_rules_changed"),
        "acceptance_rules_digest": diagnostic._identity(DECISION_RULES),
        "inputs": inputs,
        "external_evidence_origin": "unverified",
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
    await verify_async(record, plan_path=plan_path, expected_candidate=candidate)
    return record


def _verify_record(record, *, plan_path=None, expected_candidate=None):
    record = _bounded_document(record, _MAX_INPUT_BYTES + 1024 * 1024, "adoption_record_invalid")
    if type(record) is not dict or set(record) != _RECORD_FIELDS:
        raise ValueError("adoption_record_invalid")
    if record["format"] != ADOPTION_FORMAT or not _candidate_valid(record["candidate"]):
        raise ValueError("adoption_record_invalid")
    if type(record["plan_path"]) is not str:
        raise ValueError("adoption_record_invalid")
    _plan_label(record["plan_path"])
    if expected_candidate is not None and record["candidate"] != expected_candidate:
        raise ValueError("adoption_candidate_mismatch")
    if (record["acceptance_rules"] != DECISION_RULES
            or record["acceptance_rules_digest"] != diagnostic._identity(DECISION_RULES)):
        raise ValueError("adoption_rules_changed")
    if record["implementation_digest"] != diagnostic.implementation_identity():
        raise ValueError("adoption_implementation_changed")
    if record["workflows_digest"] != diagnostic._identity(diagnostic.load_workflows()):
        raise ValueError("adoption_workflows_changed")
    if (record["scoring"] != diagnostic.SCORING
            or record["scoring_digest"] != diagnostic._identity(diagnostic.SCORING_RULES)):
        raise ValueError("adoption_scoring_changed")
    if plan_path is not None and record["plan_digest"] != plan_identity(plan_path):
        raise ValueError("adoption_plan_changed")
    binding = _candidate_binding(record["candidate"], plan_path or _repository_root() / record["plan_path"])
    if any(record[field] != value for field, value in binding.items()):
        raise ValueError("adoption_candidate_source_changed")
    if record["defaults_digest"] != plan_identity(_repository_root() / "backend/shared/feature_flags.py"):
        raise ValueError("adoption_candidate_source_changed")
    _validate_runs(record["runs"])
    if run_summary(record["runs"]) != record["run_summary"]:
        raise ValueError("adoption_summary_inconsistent")
    flags = record["controls_default_off"]
    if (type(flags) is not dict or set(flags) != set(CONTROL_FLAGS)
            or any(type(enabled) is not bool for enabled in flags.values())):
        raise ValueError("adoption_record_invalid")
    if flags != control_defaults():
        raise ValueError("adoption_defaults_changed")
    inputs = record["inputs"]
    if type(inputs) is not dict or set(inputs) != _INPUT_FIELDS:
        raise ValueError("adoption_inputs_invalid")
    _bounded_document(inputs, _MAX_INPUT_BYTES, "adoption_inputs_invalid")
    if (record["measurement_kind"] != "synthetic_capability" or record["provider_route"] != diagnostic.PROVIDER_ROUTE
            or record["measurement_provider_route"] != _measurement_route(inputs)
            or record["external_evidence_origin"] != "unverified"):
        raise ValueError("adoption_record_invalid")
    promotion = diagnostic.promotion(record["runs"], candidate=record["candidate"], **inputs)
    if record["promotion"] != promotion:
        raise ValueError("adoption_promotion_inconsistent")
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
    return record


async def verify_async(record, *, plan_path=None, expected_candidate=None) -> None:
    checked = _verify_record(record, plan_path=plan_path, expected_candidate=expected_candidate)
    replay = await diagnostic.evaluate(candidate=checked["candidate"], **checked["inputs"])
    if checked["runs"] != replay["runs"]:
        raise ValueError("adoption_runs_inconsistent")
    _verify_record(checked, plan_path=plan_path, expected_candidate=expected_candidate)


def verify(record, *, plan_path=None, expected_candidate=None) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(verify_async(record, plan_path=plan_path, expected_candidate=expected_candidate))
        return
    raise ValueError("adoption_async_verification_required")


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
