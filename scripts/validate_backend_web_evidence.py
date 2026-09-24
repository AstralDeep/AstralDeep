#!/usr/bin/env python3
"""Validates a protected backend/web qualification evidence bundle whose real authority
is an installed producer workflow at its exact reviewed commit; reconstructs identity
via the native GitHub job token and never checks out candidate source.
"""

from __future__ import annotations

import argparse
import ast
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "config/backend-web-evidence.schema.json"
CONSUMER = ".github/workflows/backend-web-readiness.yml"
PRODUCER = ".github/workflows/backend-web-qualification-protected.yml"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"(?:[a-zA-Z0-9._:/-]+@)?sha256:[0-9a-f]{64}")
MAX_BYTES = 16 * 1024 * 1024
COMPONENTS = {"astral-plane", "astral-projection", "astral-primitives", "lets"}
SUITES = {"backend-complete", "backend-modules", "plane-postgresql", "projection-python", "browser", "voice-workers"}
REVIEW_BASES = {
    "deep": "b3ae2dc549928aeaee518ad60c9428790f67fc05",
    "plane": "37dfe02ee2df1c96884bef62c7ee2a9f3e44087b",
    "projection": "b9a42384209c482c21afccd72f58ed3c7dc88549",
}
_BRIDGE_REASON = (
    "protected bridge parked at d3cb9a51 (cc15b033 restored the direct tag-push release path; "
    "v0.4.0 shipped on it) — see specs/060-runtime-reliability-hardening/verification/release-trust-bootstrap.md"
)
# Frozen outcomes; any change here needs policy review
DISPOSITIONS = {
    "test_python_314_candidate_witness_matches_locked_coverage_parser": (
        "tests.test_changed_coverage_060", "backend/tests/test_changed_coverage_060.py", "pytest.skip",
        "system Python 3.14 is not available on this runner", "not-applicable-python-version",
        "43ed756644680f9e167a4082be03ae9c4a5fec8ad0565d388514bb1a1255f042",
    ),
    "test_release_windows_bridge_keeps_pinned_identity_with_no_write_authority": (
        "tests.test_release_workflows_060", "backend/tests/test_release_workflows_060.py", "pytest.skip",
        _BRIDGE_REASON, "deferred-native",
        "859876017e75c0050ee0ba06a142b6da589eca9904cfdbaf35b8e01ea89296e0",
    ),
    "test_release_windows_bridge_isolates_candidate_checkout_from_signer_runtime": (
        "tests.test_release_workflows_060", "backend/tests/test_release_workflows_060.py", "pytest.skip",
        _BRIDGE_REASON, "deferred-native",
        "53f6fb9b284e50cbc85fa93048554c69bb0907f981c0cef09414c1d646b2ec29",
    ),
    "test_release_windows_bridge_never_rebuilds_and_never_mutates_releases": (
        "tests.test_release_workflows_060", "backend/tests/test_release_workflows_060.py", "pytest.skip",
        _BRIDGE_REASON, "deferred-native",
        "3845ca506bac7d040082f29a31f0bbfac497b7ff65b01007e58ef32a3bbeb8c7",
    ),
    "test_synonym_evasion": (
        "qual_audit.suites.test_tool_poisoning.TestStaticAnalysisLimitations", "backend/qual_audit/suites/test_tool_poisoning.py",
        "pytest.xfail", "Static regex analysis cannot detect semantically disguised threats", "expected-limitation-not-passed",
        "e4d9174670eb0c50bcd98b07d5c02079306a3129c1da68d4c5217fd7ea5ecc18",
    ),
}
CHECKS = SUITES | {
    "python-javascript-lint", "changed-coverage", "component-integrity", "secret-history",
    "image-build", "production-boot-positive", "production-boot-negative", "real-keycloak-auth",
    "session-renewal", "provider-settings", "chat", "governed-tools", "attachments",
    "history-workspace", "background-work", "owner-authorization-denials", "audit-continuity",
    "lets-enforce-success", "lets-enforce-denial", "lets-recovery", "voice-enabled-browser",
    "responsive-accessibility", "live-baseline-inventory", "baseline-migration",
    "migration-repeat-safe", "retained-data", "backup-restore", "recovery",
}


class EvidenceError(ValueError):
    pass


def _policy_module() -> Any:
    spec = importlib.util.spec_from_file_location("backend_web_base_policy", ROOT / "scripts/validate_release_evidence.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


POLICY = _policy_module()


def _require(condition: object, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise EvidenceError("evidence timestamp is invalid") from None
    _require(parsed.tzinfo is not None, "evidence timestamp needs an offset")
    return parsed.astimezone(UTC)


def _read(path: Path) -> bytes:
    _require(not path.is_symlink() and path.is_file(), "evidence must be a regular file")
    _require(0 < path.stat().st_size <= MAX_BYTES, "evidence file exceeds bounds")
    return path.read_bytes()


def _json(path: Path) -> Any:
    return POLICY.load_json_bytes(_read(path))


def _file(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    _require(re.fullmatch(r"[A-Za-z0-9._/-]+", relative) and not path.is_absolute()
             and all(part not in {".", "..", ""} for part in relative.split("/")), "evidence member path is unsafe")
    target = root.joinpath(*path.parts)
    for parent in [target, *target.parents]:
        _require(not parent.is_symlink(), "evidence path traverses a symlink")
        if parent == root:
            break
    return target


def _function_fingerprint(source: bytes, name: str) -> str:
    text = source.decode("utf-8").replace("\r\n", "\n")
    functions = [node for node in ast.walk(ast.parse(text)) if isinstance(node, ast.FunctionDef) and node.name == name]
    _require(len(functions) == 1, "disposition source function is missing or ambiguous")
    node = functions[0]
    first = min([node.lineno, *(item.lineno for item in node.decorator_list)]) - 1
    return hashlib.sha256("".join(text.splitlines(keepends=True)[first:node.end_lineno]).encode()).hexdigest()


def _suite(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    data = _read(path)
    _require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(), "JUnit entities are forbidden")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise EvidenceError("JUnit evidence is malformed") from None
    _require(root.tag in {"testsuites", "testsuite"}, "JUnit root is invalid")
    cases = list(root.iter("testcase"))
    _require(cases, "complete suite has no test cases")
    observed, dispositions = [], []
    for case in cases:
        _require(case.get("classname") and case.get("name"), "JUnit case lacks an exact identity")
        nodeid = case.get("classname") + "::" + case.get("name")
        observed.append(nodeid)
        _require(not any(list(case.iter(tag)) for tag in ("failure", "error")), "complete suite contains a failure or error")
        skipped = list(case.iter("skipped"))
        if skipped:
            policy = DISPOSITIONS.get(case.get("name"))
            _require(policy is not None and len(skipped) == 1, "in-scope skipped checks remain unqualified")
            classname, source, kind, reason, category, fingerprint = policy
            _require(case.get("classname") in {classname, "backend." + classname}
                     and skipped[0].get("type") == kind and skipped[0].get("message") == reason,
                     "skipped outcome differs from its exact existing characterization")
            dispositions.append({"nodeid": nodeid, "source": source, "source_sha256": fingerprint,
                                 "outcome": category, "type": kind, "reason": reason})
    _require(len(observed) == len(set(observed)) and len(cases) > len(dispositions),
             "complete suite is duplicated or contains no passed in-scope case")
    for suite in root.iter("testsuite"):
        for field in ("failures", "errors", "disabled"):
            _require(suite.get(field, "0") == "0", "complete suite summary contains unsuccessful tests")
        _require(suite.get("skipped", "0") == str(len(list(suite.iter("skipped")))), "JUnit skipped summary differs from cases")
        _require(suite.get("tests", str(len(list(suite.iter("testcase"))))) == str(len(list(suite.iter("testcase")))),
                 "JUnit test summary differs from executed cases")
    return observed, dispositions


def validate_bundle(root: Path, *, candidate: str, base: str, image: str,
                    composition: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    _require(HEX40.fullmatch(candidate) and HEX40.fullmatch(base) and candidate != base,
             "candidate and base require different exact commits")
    _require(base == REVIEW_BASES["deep"], "coverage base differs from the reviewed maintenance base")
    _require(IMAGE.fullmatch(image), "candidate image must be immutable")
    report = _json(root / "evidence.json")
    POLICY.validate_document(report, _json(SCHEMA))
    _require(report["candidate_sha"] == candidate and report["base_sha"] == base
             and report["candidate_image"] == image, "evidence candidate identity differs")
    _require(set(report["components"]) == COMPONENTS, "component closure is incomplete")
    _require(set(report["checks"]) == CHECKS, "required backend/web checks differ")
    current = now or datetime.now(UTC)
    finished = _time(report["finished_at"])
    _require(current - timedelta(hours=24) <= finished <= current, "qualification evidence is stale or future-dated")
    _require(_time(report["started_at"]) <= finished, "qualification timing is reversed")
    if composition is not None:
        _require(report["components"] == {name: value["commit"] for name, value in composition["components"].items()},
                 "evidence components differ from candidate composition")
        plane = composition["compatibility"]["data_plane"]
        _require(report["schema"]["revision"] == plane["schema_revision"]
                 and report["schema"]["migration_sha256"] == plane["migration_sha256"],
                 "evidence schema differs from candidate composition")
    _require(report["baseline"]["schema_revision"] == "088.003"
             and report["baseline"]["migration_sha256"] == "a3d3ac43bee48b0ca6832cca1e4a347db0a3f838af908b8545edb11e7e94272a"
             and report["baseline"]["plane_commit"] == "4a07d59a448c1960ce2ae3f35e605f4d78c4a3f9",
             "qualification must rehearse the inventoried live baseline")
    files = report["files"]
    _require(files and len(files) <= 4096, "raw evidence file inventory is empty or excessive")
    actual = set()
    for path in root.rglob("*"):
        _require(not path.is_symlink(), "evidence tree contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        else:
            _require(path.is_dir(), "evidence tree contains a special file")
    _require(actual == set(files) | {"evidence.json"}, "raw evidence tree differs from its inventory")
    for name, digest in files.items():
        _require(hashlib.sha256(_read(_file(root, name))).hexdigest() == digest, "raw evidence digest differs")
    referenced = set()
    observed_dispositions = []
    _require(set(report["suite_inventory"]) == SUITES, "complete suite inventory is incomplete")
    for name, check in report["checks"].items():
        _require(check["status"] == "passed" and check["exit_code"] == 0,
                 "failed, skipped or unavailable checks cannot qualify")
        members = check["artifacts"]
        _require(members and len(members) == len(set(members)) and set(members) <= set(files),
                 "check lacks exact raw evidence artifacts")
        referenced.update(members)
        if name in SUITES:
            inventory_record = report["suite_inventory"][name]
            inventory_path = inventory_record["inventory"]
            junit = inventory_record["junit"]
            _require(inventory_path in members and junit and set(junit) <= set(members), "suite inventory and JUnit are not gate-bound")
            inventory = _json(_file(root, inventory_path))
            component = "astral-plane" if name == "plane-postgresql" else "astral-projection" if name in {"projection-python", "browser"} else None
            source_sha = report["components"][component] if component else candidate
            _require(set(inventory) == {"schema_version", "suite", "source_commit", "tests"}
                     and inventory["schema_version"] == 1 and inventory["suite"] == name
                     and inventory["source_commit"] == source_sha and isinstance(inventory["tests"], list),
                     "suite discovery is not bound to its exact source")
            cases = []
            for member in junit:
                actual_cases, dispositions = _suite(_file(root, member))
                cases.extend(actual_cases)
                observed_dispositions.extend(dispositions)
            _require(sorted(cases) == sorted(inventory["tests"]), "executed suite differs from complete discovery inventory")
    _require(referenced == set(files), "raw evidence includes unconsumed files")
    _require(sorted(report["dispositions"], key=lambda row: row["nodeid"]) == sorted(observed_dispositions, key=lambda row: row["nodeid"]),
             "non-passed characterization outcomes must be reported exactly")
    coverage = report["coverage"]
    _require(set(coverage) == {"deep", "plane", "projection"}, "changed coverage closure is incomplete")
    for name, item in coverage.items():
        _require(item["decision"] in report["checks"]["changed-coverage"]["artifacts"], "coverage decision is not gate-bound")
        decision = _json(_file(root, item["decision"]))
        expected_candidate = candidate if name == "deep" else report["components"]["astral-" + name]
        _require(decision.get("status") == "pass" and decision.get("revisions_validated") is True
                 and decision.get("candidate_sha") == expected_candidate
                 and decision.get("base_sha") == item["base_sha"]
                 and item["base_sha"] == REVIEW_BASES[name]
                 and (name != "deep" or item["base_sha"] == base)
                 and decision.get("failures") == [], "changed coverage decision is invalid")
        _require(isinstance(decision.get("fail_under"), (int, float)) and decision["fail_under"] >= 90,
                 "changed coverage threshold is below 90 percent")
        for measurement in [decision.get("combined", {}), *decision.get("languages", {}).values()]:
            covered, total = measurement.get("covered_lines"), measurement.get("executable_lines")
            _require(type(covered) is int and type(total) is int and 0 <= covered <= total
                     and covered * 100 >= total * 90, "changed executable lines are below 90 percent")
    responsive = report["responsive"]
    _require({(item["width_css_px"], item["text_percent"]) for item in responsive} >= {
        (320, 200), (390, 100), (768, 200), (1024, 100), (1440, 100),
    }, "responsive phone/tablet/desktop enlarged-text matrix is incomplete")
    return report


def _run(arguments: list[str]) -> bytes:
    result = subprocess.run(arguments, check=False, capture_output=True, timeout=120)
    _require(result.returncode == 0, "protected provider verification command failed")
    return result.stdout


def _api(repository: str, suffix: str) -> Any:
    return POLICY.load_json_bytes(_run(["gh", "api", f"repos/{repository}" + ("/" + suffix if suffix else "")]))


def _pages(repository: str, suffix: str, key: str) -> list[dict[str, Any]]:
    pages = POLICY.load_json_bytes(_run(["gh", "api", f"repos/{repository}/{suffix}?per_page=100", "--paginate", "--slurp"]))
    _require(isinstance(pages, list), "provider pagination is malformed")
    return [item for page in pages for item in page[key]]


def reconstruct_source(run: dict[str, Any], jobs: list[dict[str, Any]], artifact: dict[str, Any], *,
                       repository: str, default_branch: str, producer_sha: str, runner: str,
                       run_id: str, artifact_id: str) -> dict[str, Any]:
    _require(str(run.get("id")) == run_id and run.get("head_sha") == producer_sha
             and run.get("path") == PRODUCER and run.get("head_branch") == default_branch
             and run.get("event") == "workflow_dispatch" and run.get("status") == "completed"
             and run.get("conclusion") == "success"
             and run.get("head_repository", {}).get("full_name") == repository,
             "source run is not the installed protected producer")
    attempt = run.get("run_attempt")
    _require(type(attempt) is int and attempt > 0, "source attempt is invalid")
    matched = [job for job in jobs if job.get("name") == "qualify-backend-web"]
    _require(len(matched) == 1, "protected producer job is missing or ambiguous")
    job = matched[0]
    _require(job.get("status") == "completed" and job.get("conclusion") == "success"
             and job.get("run_id") == int(run_id) and job.get("run_attempt") == attempt
             and job.get("runner_name") == runner and type(job.get("id")) is int,
             "protected producer job or runner identity differs")
    _require(str(artifact.get("id")) == artifact_id
             and artifact.get("name") == f"backend-web-qualification-{run_id}-{attempt}"
             and artifact.get("expired") is False
             and artifact.get("workflow_run", {}).get("id") == int(run_id)
             and artifact.get("workflow_run", {}).get("head_sha") == producer_sha
             and re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest", "")))
             and type(artifact.get("size_in_bytes")) is int
             and 0 < artifact["size_in_bytes"] <= 512 * 1024 * 1024,
             "source artifact identity is invalid")
    return {"run_id": run_id, "run_attempt": attempt, "job_id": job["id"],
            "runner_name": runner, "artifact_id": artifact_id,
            "artifact_sha256": artifact["digest"].split(":", 1)[1], "producer_sha": producer_sha}


def protected_validate(args: argparse.Namespace) -> dict[str, Any]:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    policy_sha = os.environ.get("BACKEND_WEB_POLICY_SHA", "")
    producer_sha = os.environ.get("BACKEND_WEB_PRODUCER_SHA", "")
    runner = os.environ.get("BACKEND_WEB_PRODUCER_RUNNER", "")
    identity = os.environ.get("BACKEND_WEB_PRODUCER_IDENTITY", "")
    _require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
             and HEX40.fullmatch(policy_sha) and HEX40.fullmatch(producer_sha) and runner and identity,
             "protected policy and producer installation is incomplete")
    _require(os.environ.get("GITHUB_ACTIONS") == "true"
             and os.environ.get("GITHUB_JOB") == "protected-backend-web-decision"
             and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
             and os.environ.get("GITHUB_WORKFLOW_SHA") == policy_sha,
             "qualification authority requires the installed protected job")
    _require(HEX40.fullmatch(args.candidate_sha) and args.base_sha == REVIEW_BASES["deep"]
             and IMAGE.fullmatch(args.candidate_image), "protected candidate identities are invalid")
    _require(re.fullmatch(r"[1-9][0-9]*", args.source_run_id or "")
             and re.fullmatch(r"[1-9][0-9]*", args.artifact_id or ""), "source provider ids are invalid")
    branch = _api(repository, "")["default_branch"]
    _require(os.environ.get("GITHUB_REF") == f"refs/heads/{branch}", "candidate refs cannot run the protected consumer")
    _require(identity == f"https://github.com/{repository}/{PRODUCER}@refs/heads/{branch}",
             "producer certificate identity differs from the protected default branch")
    _require(_run(["git", "rev-parse", "HEAD"]).decode().strip() == policy_sha,
             "executed policy checkout is not installed")
    run = _api(repository, f"actions/runs/{args.source_run_id}")
    jobs = _pages(repository, f"actions/runs/{args.source_run_id}/attempts/{run['run_attempt']}/jobs", "jobs")
    artifact = _api(repository, f"actions/artifacts/{args.artifact_id}")
    source = reconstruct_source(run, jobs, artifact, repository=repository, default_branch=branch,
                                producer_sha=producer_sha, runner=runner,
                                run_id=args.source_run_id, artifact_id=args.artifact_id)
    with tempfile.TemporaryDirectory(prefix="astral-protected-backend-web-") as temporary:
        root = Path(temporary)
        archive = root / "artifact.zip"
        with archive.open("xb") as output:
            result = subprocess.run(["gh", "api", f"repos/{repository}/actions/artifacts/{args.artifact_id}/zip"],
                                    check=False, stdout=output, stderr=subprocess.PIPE, timeout=120)
        _require(result.returncode == 0 and archive.stat().st_size == artifact["size_in_bytes"]
                 and hashlib.sha256(archive.read_bytes()).hexdigest() == source["artifact_sha256"],
                 "downloaded artifact differs from provider digest")
        evidence_root = root / "evidence"
        _run([sys.executable, "-I", str(ROOT / "scripts/extract_release_artifact.py"),
              "--archive", str(archive), "--target", str(evidence_root)])
        receipt = _run(["gh", "attestation", "verify", str(evidence_root / "evidence.json"),
                        "--repo", repository, "--signer-workflow", f"{repository}/{PRODUCER}",
                        "--signer-digest", producer_sha, "--cert-identity", identity, "--format", "json"])
        _require(isinstance(POLICY.load_json_bytes(receipt), list) and POLICY.load_json_bytes(receipt),
                 "attestation verification returned no verified statement")
        content = _api(repository, f"contents/config/astral-composition.json?ref={args.candidate_sha}")
        _require(content.get("encoding") == "base64" and content.get("type") == "file", "candidate composition is not a file")
        composition = POLICY.load_json_bytes(base64.b64decode(content["content"], validate=False))
        report = validate_bundle(evidence_root, candidate=args.candidate_sha, base=args.base_sha,
                                 image=args.candidate_image, composition=composition)
        sources = {}
        for disposition in report["dispositions"]:
            path = disposition["source"]
            if path not in sources:
                source_file = _api(repository, f"contents/{path}?ref={args.candidate_sha}")
                _require(source_file.get("encoding") == "base64" and source_file.get("type") == "file", "disposition source is not a file")
                sources[path] = base64.b64decode(source_file["content"], validate=False)
            fingerprint = _function_fingerprint(sources[path], disposition["nodeid"].rsplit("::", 1)[1])
            _require(fingerprint == disposition["source_sha256"], "disposition source changed and requires independent policy review")
        _require(report["source_run_id"] == source["run_id"] and report["source_run_attempt"] == source["run_attempt"],
                 "attested report is replayed from another source run or attempt")
        now = datetime.now(UTC)
        return {"schema_version": 1, "scope": "backend-web", "decision": "qualified",
                "release_authorized": False, "deployment_requires_owner_approval": True,
                "candidate_sha": args.candidate_sha, "base_sha": args.base_sha,
                "candidate_image": args.candidate_image, "components": report["components"],
                "schema": report["schema"], "source": source,
                "non_passed_dispositions": report["dispositions"],
                "evidence_sha256": hashlib.sha256(_read(evidence_root / "evidence.json")).hexdigest(),
                "attestation_verification_sha256": hashlib.sha256(receipt).hexdigest(),
                "policy_commit": policy_sha, "policy_sha256": hashlib.sha256(_read(Path(__file__))).hexdigest(),
                "schema_sha256": hashlib.sha256(_read(SCHEMA)).hexdigest(),
                "generated_at": now.isoformat(), "valid_until": min(now + timedelta(hours=24), _time(report["finished_at"]) + timedelta(hours=24)).isoformat()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--protected", action="store_true")
    parser.add_argument("--source-run-id")
    parser.add_argument("--artifact-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _require(not args.output.exists() and not args.output.is_symlink(), "output already exists")
        if args.protected:
            _require(args.evidence_dir is None, "protected consumer cannot trust a local evidence directory")
            decision = protected_validate(args)
        else:
            _require(args.evidence_dir is not None, "diagnostic parsing needs an evidence directory")
            report = validate_bundle(args.evidence_dir, candidate=args.candidate_sha,
                                     base=args.base_sha, image=args.candidate_image)
            decision = {"schema_version": 1, "scope": "backend-web", "decision": "diagnostic-valid",
                        "release_authorized": False, "protected_provenance_verified": False,
                        "candidate_sha": report["candidate_sha"], "candidate_image": report["candidate_image"]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(decision, indent=2, sort_keys=True) + "\n")
        return 0
    except (EvidenceError, POLICY.ReleaseEvidenceError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("backend/web evidence rejected: incomplete, invalid or untrusted inputs", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
