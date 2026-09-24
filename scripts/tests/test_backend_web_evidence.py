"""Tests for scripts/validate_backend_web_evidence.py: bundle completeness, disposition
rules, changed-coverage binding, and protected-provider identity reconstruction.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest


SPEC = importlib.util.spec_from_file_location("backend_web_evidence", Path(__file__).parents[1] / "validate_backend_web_evidence.py")
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)
CANDIDATE = "a" * 40
BASE = validator.REVIEW_BASES["deep"]
IMAGE = "sha256:" + "c" * 64
NOW = datetime(2026, 9, 21, 17, tzinfo=UTC)


def save(root, document):
    (root / "evidence.json").write_text(json.dumps(document), encoding="utf-8")


def member(root, document, name, content):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    data = content.encode()
    (root / name).write_bytes(data)
    document["files"][name] = hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    report = {
        "schema_version": 1, "scope": "backend-web", "candidate_sha": CANDIDATE,
        "base_sha": BASE, "candidate_image": IMAGE,
        "components": {name: "d" * 40 for name in validator.COMPONENTS},
        "schema": {"revision": "089.001", "migration_sha256": "e" * 64},
        "baseline": {"deep_commit": "013a06922759ae737b24b20ab02705e35614789d", "image": IMAGE,
                     "plane_commit": "4a07d59a448c1960ce2ae3f35e605f4d78c4a3f9", "schema_revision": "088.003",
                     "migration_sha256": "a3d3ac43bee48b0ca6832cca1e4a347db0a3f838af908b8545edb11e7e94272a", "inventory_sha256": "f" * 64},
        "source_run_id": "12", "source_run_attempt": 1,
        "started_at": "2026-09-21T16:00:00Z", "finished_at": "2026-09-21T16:30:00Z",
        "environment": {"astral_env": "production", "mock_auth": False, "lets_mode": "enforce",
                        "external_warden": True, "classification": "synthetic", "qualification_id": "f" * 32},
        "voice": {"enabled": True, "backend": "llm_factory", "closure_sha256": "e" * 64,
                  "worker_image": IMAGE, "livekit_image": IMAGE},
        "checks": {}, "files": {}, "coverage": {}, "suite_inventory": {}, "dispositions": [],
        "responsive": [{"width_css_px": width, "text_percent": percent, "status": "passed"}
                       for width, percent in [(320, 200), (390, 100), (768, 200), (1024, 100), (1440, 100)]],
    }
    for name in validator.CHECKS:
        path = f"raw/{name}.xml" if name in validator.SUITES else f"raw/{name}.log"
        content = '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="qualification.synthetic" name="executed"/></testsuite>' if name in validator.SUITES else "executed qualification check\n"
        member(tmp_path, report, path, content)
        report["checks"][name] = {"status": "passed", "exit_code": 0, "command": ["protected-producer", name], "artifacts": [path]}
        if name in validator.SUITES:
            inventory = f"raw/{name}-inventory.json"
            source = "d" * 40 if name in {"plane-postgresql", "projection-python", "browser"} else CANDIDATE
            member(tmp_path, report, inventory, json.dumps({"schema_version": 1, "suite": name,
                                                          "source_commit": source, "tests": ["qualification.synthetic::executed"]}))
            report["checks"][name]["artifacts"].append(inventory)
            report["suite_inventory"][name] = {"inventory": inventory, "junit": [path]}
    for name in ("deep", "plane", "projection"):
        path = f"raw/coverage-{name}.json"
        decision = {"status": "pass", "revisions_validated": True, "candidate_sha": CANDIDATE if name == "deep" else "d" * 40,
                    "base_sha": validator.REVIEW_BASES[name], "failures": [], "fail_under": 90,
                    "combined": {"covered_lines": 90, "executable_lines": 100}, "languages": {}}
        member(tmp_path, report, path, json.dumps(decision))
        report["checks"]["changed-coverage"]["artifacts"].append(path)
        report["coverage"][name] = {"base_sha": validator.REVIEW_BASES[name], "decision": path}
    save(tmp_path, report)
    return tmp_path, report


def validate(bundle, **kwargs):
    root, report = bundle
    save(root, report)
    return validator.validate_bundle(root, candidate=CANDIDATE, base=BASE, image=IMAGE, now=NOW, **kwargs)


def test_complete_backend_web_bundle_and_candidate_composition(bundle):
    _, report = bundle
    composition = {"components": {name: {"commit": commit} for name, commit in report["components"].items()},
                   "compatibility": {"data_plane": {"schema_revision": "089.001", "migration_sha256": "e" * 64}}}
    assert validate(bundle, composition=composition)["scope"] == "backend-web"
    composition["components"]["astral-plane"]["commit"] = "f" * 40
    with pytest.raises(validator.EvidenceError, match="components"):
        validate(bundle, composition=composition)


@pytest.mark.parametrize("mutation", ["candidate", "base", "image", "checks", "native", "skip", "exit", "stale", "future", "timing", "baseline", "mock", "lets", "voice", "responsive", "coverage-closure", "unbound", "extra"])
def test_missing_failed_deferred_or_wrong_scope_cannot_pass(bundle, mutation):
    root, report = bundle
    if mutation in {"candidate", "base", "image"}:
        report[{"candidate": "candidate_sha", "base": "base_sha", "image": "candidate_image"}[mutation]] = ("9" * 40 if mutation != "image" else "sha256:" + "9" * 64)
    elif mutation == "checks":
        report["checks"].pop("lets-enforce-denial")
    elif mutation == "native":
        report["checks"]["windows"] = report["checks"]["chat"]
    elif mutation == "skip":
        report["checks"]["chat"]["status"] = "skipped"
    elif mutation == "exit":
        report["checks"]["chat"]["exit_code"] = 1
    elif mutation == "stale":
        report["finished_at"] = "2026-09-19T16:30:00Z"
    elif mutation == "future":
        report["finished_at"] = "2026-09-22T16:30:00Z"
    elif mutation == "timing":
        report["started_at"] = "2026-09-22T16:00:00Z"
    elif mutation == "baseline":
        report["baseline"]["schema_revision"] = "088.008"
    elif mutation == "mock":
        report["environment"]["mock_auth"] = True
    elif mutation == "lets":
        report["environment"]["lets_mode"] = "off"
    elif mutation == "voice":
        report["voice"]["enabled"] = False
    elif mutation == "responsive":
        report["responsive"][0]["text_percent"] = 100
    elif mutation == "coverage-closure":
        report["coverage"].pop("plane")
    elif mutation == "unbound":
        report["checks"]["chat"]["artifacts"] = ["absent.log"]
    else:
        (root / "extra.txt").write_text("unregistered")
    with pytest.raises((validator.EvidenceError, validator.POLICY.ReleaseEvidenceError)):
        validate(bundle)


@pytest.mark.parametrize("xml", [
    '<testsuite><testcase><skipped/></testcase></testsuite>',
    '<testsuite><testcase><failure/></testcase></testsuite>',
    '<testsuite><testcase><error/></testcase></testsuite>',
    '<testsuite skipped="1"><testcase/></testsuite>', '<testsuite/>', 'broken',
    '<!DOCTYPE x [<!ENTITY y "payload">]><testsuite><testcase/></testsuite>',
])
def test_suite_failure_skip_empty_or_unsafe_xml_is_never_passed(bundle, xml):
    root, report = bundle
    xml = xml.replace("<testcase", '<testcase classname="synthetic" name="case"')
    member(root, report, "raw/backend-complete.xml", xml)
    with pytest.raises(validator.EvidenceError):
        validate(bundle)


def add_disposition(root, report, function):
    classname, source, kind, reason, category, fingerprint = validator.DISPOSITIONS[function]
    nodeid = classname + "::" + function
    document = ET.fromstring((root / "raw/backend-complete.xml").read_bytes())
    document.set("tests", "2")
    document.set("skipped", "1")
    case = ET.SubElement(document, "testcase", classname=classname, name=function)
    ET.SubElement(case, "skipped", type=kind, message=reason)
    member(root, report, "raw/backend-complete.xml", ET.tostring(document, encoding="unicode"))
    inventory_path = report["suite_inventory"]["backend-complete"]["inventory"]
    inventory = json.loads((root / inventory_path).read_bytes())
    inventory["tests"].append(nodeid)
    member(root, report, inventory_path, json.dumps(inventory))
    report["dispositions"].append({"nodeid": nodeid, "source": source, "source_sha256": fingerprint,
                                  "type": kind, "reason": reason, "outcome": category})
    save(root, report)


@pytest.mark.parametrize("function", list(validator.DISPOSITIONS))
def test_exact_existing_dispositions_stay_explicitly_not_passed(bundle, function):
    root, report = bundle
    add_disposition(root, report, function)
    assert validate(bundle)["dispositions"][0]["outcome"] != "passed"
    source = validator.ROOT / report["dispositions"][0]["source"]
    assert validator._function_fingerprint(source.read_bytes(), function) == report["dispositions"][0]["source_sha256"]
    report["dispositions"] = []
    with pytest.raises(validator.EvidenceError, match="reported exactly"):
        validate(bundle)


@pytest.mark.parametrize("field,value", [("source_commit", "f" * 40), ("tests", []), ("tests", ["synthetic::absent"])])
def test_complete_suite_inventory_cannot_be_truncated_or_rebound(bundle, field, value):
    root, report = bundle
    path = report["suite_inventory"]["backend-complete"]["inventory"]
    inventory = json.loads((root / path).read_bytes())
    inventory[field] = value
    member(root, report, path, json.dumps(inventory))
    with pytest.raises(validator.EvidenceError):
        validate(bundle)


def test_unknown_live_service_skip_cannot_take_native_disposition(bundle):
    root, report = bundle
    member(root, report, "raw/backend-modules.xml", '<testsuite tests="1" skipped="1"><testcase classname="agents.ml_services.tests.test_e2e_smoke" name="test_llm_factory_list_models_returns_payload"><skipped type="pytest.skip" message="external credentials unavailable"/></testcase></testsuite>')
    with pytest.raises(validator.EvidenceError, match="unqualified"):
        validate(bundle)


@pytest.mark.parametrize("update", [{"status": "fail"}, {"revisions_validated": False}, {"candidate_sha": "f" * 40}, {"fail_under": 89}, {"combined": {"covered_lines": 89, "executable_lines": 100}}, {"combined": {"covered_lines": True, "executable_lines": 100}}])
def test_changed_coverage_is_measured_and_candidate_bound(bundle, update):
    root, report = bundle
    path = "raw/coverage-deep.json"
    decision = json.loads((root / path).read_text()) | update
    member(root, report, path, json.dumps(decision))
    with pytest.raises(validator.EvidenceError):
        validate(bundle)


def test_raw_digest_member_bounds_duplicate_keys_and_unconsumed_files(bundle):
    root, report = bundle
    (root / "raw/chat.log").write_text("tampered")
    with pytest.raises(validator.EvidenceError, match="digest"):
        validate(bundle)
    for path in ("../outside", "/absolute", "raw/../bad", "raw//bad", "raw\\bad"):
        with pytest.raises(validator.EvidenceError):
            validator._file(root, path)
    with pytest.raises(validator.EvidenceError):
        validator._read(root / "missing")
    (root / "empty").write_bytes(b"")
    with pytest.raises(validator.EvidenceError):
        validator._read(root / "empty")
    (root / "duplicate.json").write_text('{"x":1,"x":2}')
    with pytest.raises(validator.POLICY.ReleaseEvidenceError):
        validator._json(root / "duplicate.json")


@pytest.fixture
def provider():
    run = {"id": 12, "run_attempt": 1, "head_sha": "d" * 40, "path": validator.PRODUCER,
           "head_branch": "main", "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
           "head_repository": {"full_name": "AstralDeep/AstralDeep"}}
    jobs = [{"id": 45, "name": "qualify-backend-web", "status": "completed", "conclusion": "success",
             "run_id": 12, "run_attempt": 1, "runner_name": "isolated-s1"}]
    artifact = {"id": 34, "name": "backend-web-qualification-12-1", "expired": False,
                "workflow_run": {"id": 12, "head_sha": "d" * 40}, "digest": "sha256:" + "e" * 64, "size_in_bytes": 1}
    return run, jobs, artifact


def reconstruct(provider):
    return validator.reconstruct_source(*provider, repository="AstralDeep/AstralDeep", default_branch="main",
                                        producer_sha="d" * 40, runner="isolated-s1", run_id="12", artifact_id="34")


def test_provider_identity_reconstructs_without_uploaded_claims(provider):
    assert reconstruct(provider)["job_id"] == 45


@pytest.mark.parametrize("mutation", ["sha", "fork", "candidate-workflow", "failed", "attempt", "runner", "job", "artifact", "digest", "expired", "wrong-run"])
def test_provider_rejects_replays_forks_untrusted_jobs_and_missing_digest(provider, mutation):
    run, jobs, artifact = provider
    if mutation == "sha":
        run["head_sha"] = "e" * 40
    elif mutation == "fork":
        run["head_repository"]["full_name"] = "other/repo"
    elif mutation == "candidate-workflow":
        run["head_branch"] = "candidate"
    elif mutation == "failed":
        jobs[0]["conclusion"] = "failure"
    elif mutation == "attempt":
        jobs[0]["run_attempt"] = 2
    elif mutation == "runner":
        jobs[0]["runner_name"] = "other"
    elif mutation == "job":
        jobs.append(deepcopy(jobs[0]))
    elif mutation == "artifact":
        artifact["name"] = "backend-web-qualification-12-2"
    elif mutation == "digest":
        artifact["digest"] = None
    elif mutation == "expired":
        artifact["expired"] = True
    else:
        artifact["workflow_run"]["id"] = 13
    with pytest.raises(validator.EvidenceError):
        reconstruct(provider)


def test_diagnostic_cli_never_grants_authority_and_invalid_output_is_absent(bundle, monkeypatch):
    root, _ = bundle
    original = validator.validate_bundle
    monkeypatch.setattr(validator, "validate_bundle", lambda *a, **kw: original(*a, **kw, now=NOW))
    output = root.parent / (root.name + "-result.json")
    args = ["--candidate-sha", CANDIDATE, "--base-sha", BASE, "--candidate-image", IMAGE,
            "--evidence-dir", str(root), "--output", str(output)]
    assert validator.main(args) == 0
    assert json.loads(output.read_text())["protected_provenance_verified"] is False
    assert json.loads(output.read_text())["release_authorized"] is False
    assert validator.main(args) == 1
    output.unlink()
    assert validator.main([*args, "--protected"]) == 1
    assert not output.exists()


def test_protected_context_cannot_be_created_by_a_local_report(monkeypatch, tmp_path):
    for name in ("GITHUB_REPOSITORY", "BACKEND_WEB_POLICY_SHA", "BACKEND_WEB_PRODUCER_SHA"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(validator.EvidenceError, match="installation"):
        validator.protected_validate(argparse.Namespace())
    monkeypatch.setenv("GITHUB_REPOSITORY", "AstralDeep/AstralDeep")
    monkeypatch.setenv("BACKEND_WEB_POLICY_SHA", "c" * 40)
    monkeypatch.setenv("BACKEND_WEB_PRODUCER_SHA", "d" * 40)
    monkeypatch.setenv("BACKEND_WEB_PRODUCER_RUNNER", "isolated-s1")
    monkeypatch.setenv("BACKEND_WEB_PRODUCER_IDENTITY", "installed-identity")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(validator.EvidenceError, match="protected job"):
        validator.protected_validate(argparse.Namespace())


def test_provider_commands_are_bounded_and_fail_closed(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b"secret", stderr=b"secret"))
    with pytest.raises(validator.EvidenceError, match="command failed"):
        validator._run(["gh", "api"])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'{"x":1}'))
    assert validator._api("AstralDeep/AstralDeep", "path") == {"x": 1}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'[{"jobs":[{"id":1}]},{"jobs":[{"id":2}]}]'))
    assert validator._pages("AstralDeep/AstralDeep", "path", "jobs") == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize("disposition", [False, True])
def test_protected_fetch_verify_and_decision_bind_exact_provider_and_candidate(bundle, provider, monkeypatch, disposition):
    root, report = bundle
    if disposition:
        add_disposition(root, report, "test_python_314_candidate_witness_matches_locked_coverage_parser")
    run, jobs, artifact = provider
    artifact.update(size_in_bytes=3, digest="sha256:" + hashlib.sha256(b"zip").hexdigest())
    composition = {"components": {name: {"commit": commit} for name, commit in report["components"].items()},
                   "compatibility": {"data_plane": {"schema_revision": "089.001", "migration_sha256": "e" * 64}}}
    env = {"GITHUB_REPOSITORY": "AstralDeep/AstralDeep", "BACKEND_WEB_POLICY_SHA": "c" * 40,
           "BACKEND_WEB_PRODUCER_SHA": "d" * 40, "BACKEND_WEB_PRODUCER_RUNNER": "isolated-s1",
           "BACKEND_WEB_PRODUCER_IDENTITY": "https://github.com/AstralDeep/AstralDeep/" + validator.PRODUCER + "@refs/heads/main",
           "GITHUB_ACTIONS": "true", "GITHUB_JOB": "protected-backend-web-decision",
           "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_WORKFLOW_SHA": "c" * 40, "GITHUB_REF": "refs/heads/main"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    def api(_repository, suffix):
        if suffix == "":
            return {"default_branch": "main"}
        if suffix.startswith("actions/runs/"):
            return run
        if suffix.startswith("actions/artifacts/"):
            return artifact
        if suffix.startswith("contents/backend/"):
            source = (validator.ROOT / report["dispositions"][0]["source"]).read_bytes()
            return {"encoding": "base64", "type": "file", "content": base64.b64encode(source).decode()}
        return {"encoding": "base64", "type": "file", "content": base64.b64encode(json.dumps(composition).encode()).decode()}
    def command(arguments):
        if arguments[:2] == ["git", "rev-parse"]:
            return ("c" * 40).encode()
        if arguments[1:3] == ["attestation", "verify"]:
            return b'[{"verificationResult":"verified"}]'
        import shutil
        shutil.copytree(root, arguments[-1])
        return b""
    def download(_arguments, **kwargs):
        kwargs["stdout"].write(b"zip")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(validator, "_api", api)
    monkeypatch.setattr(validator, "_pages", lambda *_: jobs)
    monkeypatch.setattr(validator, "_run", command)
    monkeypatch.setattr(subprocess, "run", download)
    original = validator.validate_bundle
    monkeypatch.setattr(validator, "validate_bundle", lambda *a, **kw: original(*a, **kw, now=NOW))
    args = argparse.Namespace(source_run_id="12", artifact_id="34", candidate_sha=CANDIDATE, base_sha=BASE, candidate_image=IMAGE)
    decision = validator.protected_validate(args)
    assert decision["decision"] == "qualified"
    assert decision["release_authorized"] is False
    assert decision["source"]["job_id"] == 45
    assert decision["candidate_sha"] == CANDIDATE
    monkeypatch.setenv("GITHUB_REF", "refs/heads/candidate")
    with pytest.raises(validator.EvidenceError, match="candidate refs"):
        validator.protected_validate(args)
