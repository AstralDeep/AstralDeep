"""Exercise the offline review wrapper with real filesystem effects and frozen upstream mock artifacts. Recording sandbox tests establish argument and lifecycle behavior, never Linux containment."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from devtools import codex_security_review as csr

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures/codex_security"
REAL_POLICY = REPO_ROOT / "tooling/codex-security/review-policy.json"


def git(repo, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], text=True
    ).strip()


def build_checkout(tmp_path):
    repo = tmp_path / "checkout"
    for directory in (
        "backend/orchestrator",
        "scripts",
        "backend/data",
        "backend/tmp",
        "backend/knowledge",
        "backend/attachments",
        "backend/agents/generated",
    ):
        (repo / directory).mkdir(parents=True)
    (repo / ".gitignore").write_text(
        "backend/tmp/\nbackend/data/\nbackend/knowledge/\n"
    )
    for relative, content in {
        "backend/orchestrator/example.py": "VALUE = 1\n",
        "scripts/tool.py": "VALUE = 2\n",
        "backend/data/private.txt": "PRIVATE\n",
        "backend/attachments/clinical.txt": "CLINICAL\n",
        "backend/knowledge/synth.md": "SYNTH\n",
        "backend/tmp/cache.txt": "CACHE\n",
        "backend/legacy.key": "KEY\n",
        ".env": "TOKEN=synthetic\n",
    }.items():
        (repo / relative).write_text(content)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "review@example.invalid")
    git(repo, "config", "user.name", "Review Fixture")
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "fixture")
    (repo / "backend/agents/generated/private.py").write_text("UNTRACKED_GENERATED\n")
    return repo


def write_policy(repo, **overrides):
    policy = json.loads(REAL_POLICY.read_text())
    policy["scope"]["include_roots"] = ["backend", "scripts"]
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(policy.get(key), dict):
            policy[key].update(value)
        else:
            policy[key] = value
    path = repo / "review-policy.json"
    path.write_text(json.dumps(policy))
    return path


@pytest.fixture(autouse=True)
def fixture_runtime_lock(tmp_path, monkeypatch):
    lock = {
        "scanner_version": "0.2.0",
        "files": {
            "scanner/entry": hashlib.sha256(b"trusted fixture runtime\n").hexdigest()
        },
    }
    path = tmp_path / "runtime-lock.json"
    path.write_text(json.dumps(lock))
    monkeypatch.setattr(csr, "RUNTIME_LOCK", path)


def runtime_fixture(tmp_path):
    runtime = tmp_path / "runtime"
    if not runtime.exists():
        for name in (
            "scanner/entry",
            "usr/local/bin/node",
            "usr/bin/python3",
            "usr/bin/git",
            "usr/bin/true",
        ):
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("trusted fixture runtime\n")
            path.chmod(0o755)
        shutil.copyfile(csr.RUNTIME_LOCK, runtime / "runtime-lock.json")
    return runtime


FAKE_SANDBOX = """#!@PYTHON@
import json, os, pathlib, shutil, sys, time
args = sys.argv[1:]
command = args[args.index("--") + 1:]
if command == ["/usr/bin/true"]:
    sys.stderr.write("AppArmor denied userns creation" if @FAIL@ else "")
    sys.exit(1 if @FAIL@ else 0)
raw = pathlib.Path(args[args.index("--bind") + 1])
mounts = [args[i+1:i+3] for i in range(len(args)) if args[i] == "--ro-bind"]
source = next(pathlib.Path(a) for a,b in mounts if b == "/source")
record = {"argv": args, "environment": dict(os.environ),
          "snapshot_files": sorted(str(p.relative_to(source)) for p in source.rglob("*") if p.is_file()),
          "snapshot_content": (source / "scripts/tool.py").read_text()}
pathlib.Path(@RECORD@).write_text(json.dumps(record))
case = @CASE@
if case == "sleep":
    time.sleep(60)
if case == "exit":
    sys.exit(7)
scan = raw / "scan"
scan.mkdir(mode=0o700)
for source_name, destination in [("upstream-manifest.json", "scan-manifest.json"),
                                  ("upstream-findings.json", "findings.json"),
                                  ("upstream-coverage.json", "coverage.json"),
                                  ("upstream-results.sarif", "exports/results.sarif")]:
    path = scan / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pathlib.Path(@FIXTURES@) / source_name, path)
if case == "missing":
    (scan / "scan-manifest.json").unlink()
if case == "outside":
    p = scan / "exports/results.sarif"
    p.write_text(p.read_text().replace("mock/path-traversal.ts", "../private.txt"))
if case in ("partial", "interrupted"):
    import hashlib
    p = scan / "coverage.json"
    doc = json.loads(p.read_text())
    doc["completeness"] = "partial"
    p.write_text(json.dumps(doc))
    manifest = scan / "scan-manifest.json"
    doc = json.loads(manifest.read_text())
    for item in doc["scan"]["artifacts"]:
        if item["path"] == "coverage.json":
            item["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    if case == "interrupted":
        doc["scan"]["status"] = "interrupted"
    manifest.write_text(json.dumps(doc))
"""


def write_fakes(
    tmp_path, *, case="complete", code=0, fail_probe=False, passthrough=False
):
    body = (
        FAKE_SANDBOX.replace("@PYTHON@", sys.executable)
        .replace("@FAIL@", repr(fail_probe))
        .replace("@CASE@", repr(case))
        .replace("@RECORD@", repr(str(tmp_path / "bwrap-record.json")))
        .replace("@FIXTURES@", repr(str(FIXTURES)))
    )
    program = tmp_path / "fake_bwrap.py"
    program.write_text(body)
    program.chmod(0o755)
    return program


def run_args(tmp_path, repo, policy, program, *, approve=True, extra=None):
    runtime = runtime_fixture(tmp_path)
    result = [
        "run",
        "--source-root",
        str(repo),
        "--policy",
        str(policy),
        "--runtime-root",
        str(runtime),
        "--runtime-sha256",
        csr.tree_digest(runtime),
        "--sandbox-program",
        str(program),
    ]
    return result + (["--approve"] if approve else []) + (extra or [])


def report_root(repo):
    return repo / "backend/tmp/codex-security"


def read_json(path):
    return json.loads(path.read_text())


def stdout_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def run_fixture(tmp_path, capsys, *, case="complete", **policy_overrides):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo, **policy_overrides)
    program = write_fakes(tmp_path, case=case)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    output = stdout_json(capsys)
    run = Path(output["run_dir"])
    return repo, policy, run, exit_code, output


def test_plan_is_read_only_and_discloses_only_synthetic_profile(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    assert csr.main(["plan", "--source-root", str(repo), "--policy", str(policy)]) == 0
    result = stdout_json(capsys)
    assert result["files"] == 2
    assert result["commit"] == git(repo, "rev-parse", "HEAD")
    assert result["security_analysis_performed"] is False
    assert not report_root(repo).exists()


def test_success_binds_source_policy_runtime_and_synthetic_artifacts(tmp_path, capsys):
    repo, policy, run, code, output = run_fixture(tmp_path, capsys)
    assert code == csr.EXIT_OK and output["status"] == "complete"
    report = read_json(run / "report.json")
    assert report["synthetic"] and report["security_analysis_performed"] is False
    assert report["source"]["commit"] == git(repo, "rev-parse", "HEAD")
    assert report["config_digest"] == csr.sha256_bytes(
        csr.canonical_bytes(read_json(policy))
    )
    assert report["provider"]["runtime"]["sha256"] == csr.tree_digest(
        tmp_path / "runtime"
    )
    assert report["budgets"]["admitted"] == {"model_calls": 0, "cost_usd": 0}
    assert (
        report["budgets"]["used"]["calls"] == report["budgets"]["used"]["cost_usd"] == 0
    )
    assert report["sandbox"]["network_shared"] is False
    assert report["findings_count"] == 12
    assert read_json(run / "findings.sarif")["properties"]["synthetic"] is True
    assert read_json(run / "coverage-receipt.json")["synthetic"] is True
    assert "snippet" not in (run / "findings.sarif").read_text()
    for path in (
        run / "report.json",
        run / "findings.sarif",
        run / "raw/scan/findings.json",
    ):
        assert path.stat().st_mode & 0o777 == 0o600
    entries = read_json(report_root(repo) / "ledger.json")["entries"]
    assert [entry["kind"] for entry in entries] == ["run", "receipt"]
    assert entries[-1]["report_digest"] == report["report_digest"]


def test_child_receives_only_materialized_tracked_source_and_allowlisted_environment(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_PARENT_SECRET")
    monkeypatch.setenv("NODE_OPTIONS", "--require malicious-parent-hook")
    monkeypatch.setenv("CODEX_SECURITY_PROVIDER_API_KEY", "SYNTHETIC_LEGACY_SECRET")
    repo, _policy, run, _code, _output = run_fixture(tmp_path, capsys)
    record = read_json(tmp_path / "bwrap-record.json")
    assert record["snapshot_files"] == [
        "backend/orchestrator/example.py",
        "scripts/tool.py",
    ]
    assert record["snapshot_content"] == "VALUE = 2\n"
    assert set(record["environment"]) <= {"PATH", "LANG", "__CF_USER_TEXT_ENCODING"}
    assert "OPENAI_API_KEY" not in record["environment"]
    assert "NODE_OPTIONS" not in record["environment"]
    argv = record["argv"]
    assert (
        "--clearenv" in argv and "--unshare-all" in argv and "--share-net" not in argv
    )
    assert str(repo) not in argv
    assert ["--ro-bind", "/", "/"] not in [argv[i : i + 3] for i in range(len(argv))]
    command = argv[argv.index("--") + 1 :]
    assert command[:4] == [
        "/usr/local/bin/node",
        "/scanner/sdk/typescript/bin/codex-security.mjs",
        "scan",
        "/source",
    ]
    assert "--mock" in command and "/output/scan" in command
    assert "SYNTHETIC_PARENT_SECRET" not in (run / "report.json").read_text()
    snapshot = Path(argv[argv.index("--ro-bind", argv.index("--ro-bind") + 1) + 1])
    assert not snapshot.exists()


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("partial", "partial", 0),
        ("interrupted", "interrupted", csr.EXIT_INTERRUPTED),
        ("exit", "error", csr.EXIT_ERROR),
        ("missing", "error", csr.EXIT_ERROR),
        ("outside", "error", csr.EXIT_ERROR),
    ],
)
def test_partial_interrupted_failure_and_sanitization_are_distinct(
    tmp_path, capsys, case, status, code
):
    _repo, _policy, run, actual, _output = run_fixture(tmp_path, capsys, case=case)
    report = read_json(run / "report.json")
    assert actual == code and report["status"] == status
    assert (report["artifacts"]["sarif"] is not None) == (status == "partial")
    assert report["budgets"]["used"]["cost_usd"] == 0


def test_missing_approval_cannot_create_reports_or_invoke_scanner(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    code = csr.main(
        run_args(
            tmp_path, repo, write_policy(repo), write_fakes(tmp_path), approve=False
        )
    )
    assert code == csr.EXIT_DENIED
    assert "--approve" in capsys.readouterr().err
    assert (
        not report_root(repo).exists() and not (tmp_path / "bwrap-record.json").exists()
    )


def test_paid_profile_is_denied_even_when_operator_has_approved_egress(
    tmp_path, capsys
):
    repo = build_checkout(tmp_path)
    code = csr.main(
        run_args(
            tmp_path,
            repo,
            write_policy(repo),
            write_fakes(tmp_path),
            extra=["--egress-approve"],
        )
    )
    assert code == csr.EXIT_DENIED
    assert "unavailable" in capsys.readouterr().err
    assert not report_root(repo).exists()


def test_run_limit_is_reserved_before_start_and_cannot_be_reused_after_failure(
    tmp_path, capsys
):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo, budgets={"max_runs": 1})
    program = write_fakes(tmp_path, case="exit")
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_ERROR
    capsys.readouterr()
    (tmp_path / "bwrap-record.json").unlink()
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_STOPPED
    assert "max_runs" in capsys.readouterr().err
    assert not (tmp_path / "bwrap-record.json").exists()


def test_pending_reservation_is_counted_after_crash(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo, budgets={"max_runs": 1})
    root = report_root(repo)
    root.mkdir(parents=True, mode=0o700)
    csr.append_ledger(root, {"kind": "run", "counted": True, "status": "reserved"})
    assert (
        csr.main(run_args(tmp_path, repo, policy, write_fakes(tmp_path)))
        == csr.EXIT_STOPPED
    )
    assert not (tmp_path / "bwrap-record.json").exists()


def test_concurrent_admission_cannot_oversubscribe_run_budget(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    root = report_root(repo)
    root.mkdir(parents=True, mode=0o700)
    with (root / "admission.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert (
            csr.main(run_args(tmp_path, repo, policy, write_fakes(tmp_path)))
            == csr.EXIT_DENIED
        )
    assert "admission" in capsys.readouterr().err
    assert not (tmp_path / "bwrap-record.json").exists()


def test_sandbox_preflight_retains_denial_without_running_scanner(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, fail_probe=True)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_DENIED
    assert "AppArmor denied userns creation" in capsys.readouterr().err
    assert not (tmp_path / "bwrap-record.json").exists()
    assert csr.budget_state(csr.read_ledger(report_root(repo)))["runs"] == 0


def test_watchdog_stops_process_and_retains_reserved_run(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    args = run_args(
        tmp_path,
        repo,
        policy,
        write_fakes(tmp_path, case="sleep"),
        extra=["--max-seconds", "1"],
    )
    assert csr.main(args) == csr.EXIT_INTERRUPTED
    run = Path(stdout_json(capsys)["run_dir"])
    report = read_json(run / "report.json")
    assert report["interrupt_reason"] == "budget:max_seconds"
    assert report["artifacts"]["sarif"] is None
    assert csr.budget_state(csr.read_ledger(report_root(repo)))["runs"] == 1


def test_source_snapshot_refuses_changes_after_inventory(tmp_path):
    repo = build_checkout(tmp_path)
    inventory, _, _ = csr.source_inventory(repo, csr.load_policy(write_policy(repo)))
    (repo / "scripts/tool.py").write_text("VALUE = 3\n")
    with pytest.raises(csr.Denial, match="changed"):
        csr.materialize_snapshot(repo, tmp_path / "snapshot", inventory)


def test_inventory_does_not_execute_repository_fsmonitor(tmp_path):
    repo = build_checkout(tmp_path)
    marker = tmp_path / "hook-executed"
    hook = tmp_path / "fsmonitor"
    hook.write_text(f"#!/bin/sh\n/usr/bin/touch '{marker}'\nprintf '2\\000'\n")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))
    csr.source_inventory(repo, csr.load_policy(write_policy(repo)))
    assert not marker.exists()


def test_existing_public_report_directory_is_refused(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    root = report_root(repo)
    root.mkdir(parents=True, mode=0o755)
    code = csr.main(run_args(tmp_path, repo, write_policy(repo), write_fakes(tmp_path)))
    assert code == csr.EXIT_DENIED
    assert not (tmp_path / "bwrap-record.json").exists()


def test_reports_refuse_force_tracked_files_beneath_ignored_root(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    root = report_root(repo)
    root.mkdir(parents=True, mode=0o700)
    (root / "tracked-note.json").write_text("{}")
    git(repo, "add", "-f", "backend/tmp/codex-security/tracked-note.json")
    code = csr.main(run_args(tmp_path, repo, write_policy(repo), write_fakes(tmp_path)))
    assert code == csr.EXIT_DENIED
    assert "tracked" in capsys.readouterr().err
    assert not (tmp_path / "bwrap-record.json").exists()


@pytest.mark.parametrize("kind", ["parent_symlink", "hardlink", "fifo"])
def test_inventory_never_reads_special_or_redirected_files(tmp_path, kind):
    repo = build_checkout(tmp_path)
    if kind == "parent_symlink":
        scripts = repo / "scripts"
        scripts.rename(tmp_path / "redirected")
        scripts.symlink_to(tmp_path / "redirected", target_is_directory=True)
    elif kind == "hardlink":
        os.link(repo / "scripts/tool.py", tmp_path / "hardlinked")
    else:
        (repo / "scripts/tool.py").unlink()
        os.mkfifo(repo / "scripts/tool.py")
    with pytest.raises(csr.Denial, match="regular|symlink"):
        csr.source_inventory(repo, csr.load_policy(write_policy(repo)))


@pytest.mark.parametrize("change", ["runtime_file", "lock", "approval", "missing_tool"])
def test_runtime_is_bound_to_explicit_artifact_and_reviewed_dependency_lock(
    tmp_path, change
):
    runtime = runtime_fixture(tmp_path)
    approved = csr.tree_digest(runtime)
    if change == "runtime_file":
        (runtime / "scanner/entry").write_text("substituted executable")
    elif change == "lock":
        (runtime / "runtime-lock.json").write_text("{}")
    elif change == "missing_tool":
        (runtime / "usr/bin/python3").unlink()
    else:
        approved = "0" * 64
    with pytest.raises(csr.Denial):
        csr.verify_runtime(runtime, approved, tmp_path / "source")


def test_runtime_rejects_source_or_host_root(tmp_path):
    with pytest.raises(csr.Denial):
        csr.verify_runtime(Path("/"), "0" * 64, tmp_path)
    with pytest.raises(csr.Denial):
        csr.verify_runtime(tmp_path, "0" * 64, tmp_path / "source")


def test_verify_accepts_and_rejects_artifact_tampering(tmp_path, capsys):
    repo, policy, run, _code, _output = run_fixture(tmp_path, capsys)
    args = [
        "verify",
        "--source-root",
        str(repo),
        "--policy",
        str(policy),
        "--run-dir",
        str(run),
    ]
    assert csr.main(args) == csr.EXIT_OK
    assert stdout_json(capsys)["verified"] is True
    (run / "findings.sarif").write_text("{}")
    assert csr.main(args) == csr.EXIT_VERIFY_FAILED
    assert "artifact missing or modified" in " ".join(stdout_json(capsys)["reasons"])


@pytest.mark.parametrize(
    "change,reason",
    [
        ("source", "source content"),
        ("commit", "source commit"),
        ("policy", "policy changed"),
        ("report", "report_digest"),
        ("raw", "raw provider"),
        ("ledger", "budget ledger"),
        ("provider", "reviewed provider"),
    ],
)
def test_verify_rejects_source_policy_report_or_receipt_drift(
    tmp_path, capsys, change, reason
):
    repo, policy, run, _code, _output = run_fixture(tmp_path, capsys)
    if change == "source":
        (repo / "scripts/tool.py").write_text("VALUE = 9")
    elif change == "commit":
        git(repo, "commit", "--allow-empty", "-q", "-m", "move")
    elif change == "policy":
        document = read_json(policy)
        document["budgets"]["max_runs"] += 1
        policy.write_text(json.dumps(document))
    elif change in ("report", "provider"):
        document = read_json(run / "report.json")
        document["provider"]["reviewed_commit"] = "0" * 40
        (run / "report.json").write_text(json.dumps(document))
    elif change == "raw":
        shutil.rmtree(run / "raw")
    else:
        (report_root(repo) / "ledger.json").unlink()
    code = csr.main(
        [
            "verify",
            "--source-root",
            str(repo),
            "--policy",
            str(policy),
            "--run-dir",
            str(run),
        ]
    )
    assert code == csr.EXIT_VERIFY_FAILED
    assert reason in " ".join(stdout_json(capsys)["reasons"])


@pytest.mark.parametrize(
    "document,change",
    [
        ("scan-manifest.json", "schema"),
        ("scan-manifest.json", "mock"),
        ("scan-manifest.json", "producer"),
        ("coverage.json", "identity"),
        ("findings.json", "shape"),
        ("findings.json", "digest"),
        ("coverage.json", "completeness"),
        ("exports/results.sarif", "shape"),
    ],
)
def test_adapter_rejects_unpinned_nonmock_or_malformed_upstream_artifacts(
    tmp_path, document, change
):
    copy_upstream(tmp_path)
    path = tmp_path / document
    data = read_json(path)
    if change == "schema":
        data["schemaVersion"] = "2.0"
    elif change == "mock":
        data["scan"]["extensions"]["mock"] = False
    elif change == "producer":
        data["scan"]["producer"]["version"] = "unreviewed"
    elif change == "identity":
        data["scanId"] = "another scan"
    elif change == "shape":
        data["findings" if "findings" in document else "runs"] = None
    elif change == "completeness":
        data["completeness"] = "invented"
    else:
        data["extension"] = "unsealed data"
    path.write_text(json.dumps(data))
    with pytest.raises(csr.ResultError):
        csr.read_result(tmp_path, json.loads(REAL_POLICY.read_text()))


def copy_upstream(destination):
    for original, name in [
        ("upstream-manifest.json", "scan-manifest.json"),
        ("upstream-findings.json", "findings.json"),
        ("upstream-coverage.json", "coverage.json"),
        ("upstream-results.sarif", "exports/results.sarif"),
    ]:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / original, path)


@pytest.mark.parametrize(
    "change", ["missing", "invalid_json", "list", "symlink", "oversized"]
)
def test_adapter_preserves_missing_and_unreadable_evidence_failures(tmp_path, change):
    copy_upstream(tmp_path)
    path = tmp_path / "coverage.json"
    path.unlink()
    if change == "invalid_json":
        path.write_text("{")
    elif change == "list":
        path.write_text("[]")
    elif change == "symlink":
        path.symlink_to(FIXTURES / "upstream-coverage.json")
    elif change == "oversized":
        path.write_bytes(b"x" * 2048)
    policy = json.loads(REAL_POLICY.read_text())
    if change == "oversized":
        policy["budgets"]["max_artifact_bytes"] = 1024
    with pytest.raises(csr.ResultError):
        csr.read_result(tmp_path, policy)


@pytest.mark.parametrize("field", ["extensions", "scope", "artifacts"])
def test_adapter_rejects_wrong_nested_manifest_types(tmp_path, field):
    copy_upstream(tmp_path)
    path = tmp_path / "scan-manifest.json"
    document = read_json(path)
    document["scan"][field] = None
    path.write_text(json.dumps(document))
    with pytest.raises(csr.ResultError):
        csr.read_result(tmp_path, json.loads(REAL_POLICY.read_text()))


def test_adapter_classifies_absent_output_directory_as_result_failure(tmp_path):
    with pytest.raises(csr.ResultError):
        csr.read_result(tmp_path / "absent", json.loads(REAL_POLICY.read_text()))


@pytest.mark.parametrize("report", ["[]", "not-json", "null"])
def test_verify_rejects_corrupt_report_without_crashing(tmp_path, capsys, report):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    run = tmp_path / "bad-run"
    run.mkdir()
    (run / "report.json").write_text(report)
    assert (
        csr.main(
            [
                "verify",
                "--source-root",
                str(repo),
                "--policy",
                str(policy),
                "--run-dir",
                str(run),
            ]
        )
        == csr.EXIT_VERIFY_FAILED
    )
    assert stdout_json(capsys)["verified"] is False


def test_source_scope_has_a_pre_copy_byte_budget(tmp_path):
    repo = build_checkout(tmp_path)
    policy = csr.load_policy(write_policy(repo, scope={"max_bytes": 1}))
    with pytest.raises(csr.Denial, match="max_bytes"):
        csr.source_inventory(repo, policy)


def test_missing_runtime_approval_is_refused_before_scanner(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    args = run_args(tmp_path, repo, write_policy(repo), write_fakes(tmp_path))
    del args[args.index("--runtime-sha256") : args.index("--runtime-sha256") + 2]
    assert csr.main(args) == csr.EXIT_DENIED
    assert "approve" in capsys.readouterr().err
    assert not (tmp_path / "bwrap-record.json").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "profile",
        "model",
        "endpoint",
        "budget",
        "pin",
        "preview",
        "scope",
        "schema",
        "sandbox",
        "reports",
    ],
)
def test_policy_cannot_enable_paid_or_unbounded_or_unreviewed_execution(mutation):
    policy = json.loads(REAL_POLICY.read_text())
    if mutation == "profile":
        policy["provider"]["profile"] = "paid"
    elif mutation == "model":
        policy["provider"]["model"] = "arbitrary"
    elif mutation == "endpoint":
        policy["provider"]["endpoint_allowlist"] = ["api.openai.com:443"]
    elif mutation == "budget":
        policy["budgets"]["max_cost_usd"] = 1
    elif mutation == "pin":
        policy["provider"]["reviewed_source"]["commit"] = "0" * 40
    elif mutation == "preview":
        policy["preview"]["enabled"] = True
    elif mutation == "scope":
        policy["scope"]["include_roots"] = ["../private"]
    elif mutation == "schema":
        policy["schema_version"] = 99
    elif mutation == "sandbox":
        policy["sandbox"]["program"] = "sh"
    else:
        policy["reports"]["require_untracked"] = False
    with pytest.raises(csr.Denial):
        csr.validate_policy(policy)


@pytest.mark.parametrize(
    "target", ["scripts", "../outside", "backend/tmp/link/reports"]
)
def test_reports_cannot_use_tracked_external_or_symlinked_locations(
    tmp_path, capsys, target
):
    repo = build_checkout(tmp_path)
    if "link" in target:
        outside = tmp_path / "private-outside"
        outside.mkdir()
        (repo / "backend/tmp/link").symlink_to(outside, target_is_directory=True)
    code = csr.main(
        run_args(
            tmp_path,
            repo,
            write_policy(repo),
            write_fakes(tmp_path),
            extra=["--report-root", target],
        )
    )
    assert code == csr.EXIT_DENIED
    assert not (tmp_path / "bwrap-record.json").exists()


def test_preview_is_always_refused(capsys):
    assert csr.main(["preview"]) == csr.EXIT_DENIED
    assert "disabled" in capsys.readouterr().err


def test_signal_relay_and_process_group_cancellation():
    controller = csr.StopController()
    handlers = csr.install_signal_relay(controller)
    try:
        os.kill(os.getpid(), signal.SIGINT)
        assert controller.reason == "signal:SIGINT"
    finally:
        csr.restore_signal_handlers(handlers)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    assert csr.wait_for_provider(process, controller) == (None, True)
    assert process.poll() is not None


def test_signal_handlers_are_safe_from_worker_thread():
    results = []

    def run():
        results.extend(csr.install_signal_relay(csr.StopController()))
        csr.restore_signal_handlers(results)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert results == []
