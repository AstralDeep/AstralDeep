"""Offline deterministic tests for the isolated report-only Codex Security review wrapper:
success, partial, interrupted, stopped, error, denial, and recovery paths against frozen
synthetic provider fixtures and a recording sandbox fake; no network and no provider account.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from devtools import codex_security_review as csr

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex_security"
WRAPPER = REPO_ROOT / "backend" / "devtools" / "codex_security_review.py"
REAL_POLICY = REPO_ROOT / "tooling" / "codex-security" / "review-policy.json"


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def build_checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    for directory in ("backend/orchestrator", "scripts", "backend/data",
                      "backend/tmp", "backend/knowledge", "certs"):
        (repo / directory).mkdir(parents=True)
    (repo / ".gitignore").write_text("backend/tmp/\nbackend/data/\nbackend/knowledge/\n")
    (repo / "backend/orchestrator/example.py").write_text("def guarded():\n    return 1\n")
    (repo / "scripts/tool.py").write_text("VALUE = 1\n")
    (repo / "backend/data/private.txt").write_text("SENSITIVE_DATA\n")
    (repo / "backend/tmp/cache.txt").write_text("CACHED\n")
    (repo / "backend/knowledge/synth.md").write_text("SYNTHESIZED\n")
    (repo / ".env").write_text("TOKEN=synthetic-placeholder\n")
    (repo / "certs/server.pem").write_text("SYNTHETIC CERTIFICATE\n")
    (repo / "backend/legacy.key").write_text("SYNTHETIC KEY MATERIAL\n")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "review@example.invalid")
    git(repo, "config", "user.name", "Review Fixture")
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def write_policy(repo: Path, **overrides) -> Path:
    policy = json.loads(REAL_POLICY.read_text(encoding="utf-8"))
    policy["scope"]["include_roots"] = ["backend", "scripts"]
    policy["budgets"]["max_seconds"] = 120
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(policy.get(key), dict):
            policy[key].update(value)
        else:
            policy[key] = value
    path = repo / "review-policy.json"
    path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    return path


FAKE_PROVIDER = r'''
import os, sys, time

CASE, FIXTURE_DIR, CODE = sys.argv[1], sys.argv[2], int(sys.argv[3])
OUT = os.environ["CODEX_SECURITY_OUTPUT_DIR"]
ROOT = os.environ["CODEX_SECURITY_SOURCE_ROOT"]

def copy(source, destination):
    with open(os.path.join(FIXTURE_DIR, source), encoding="utf-8") as handle:
        text = handle.read()
    with open(os.path.join(OUT, destination), "w", encoding="utf-8") as handle:
        handle.write(text.replace("@SOURCE_ROOT@", ROOT))

PLAN = {
    "complete": [("result_complete.json", "result.json"),
                 ("findings_complete.sarif", "findings.sarif"),
                 ("coverage_complete.json", "coverage-receipt.json")],
    "partial": [("result_partial.json", "result.json"),
                ("findings_complete.sarif", "findings.sarif")],
    "error": [("result_error.json", "result.json")],
    "overcost": [("result_overcost.json", "result.json"),
                 ("findings_complete.sarif", "findings.sarif"),
                 ("coverage_complete.json", "coverage-receipt.json")],
    "excluded": [("result_excluded.json", "result.json"),
                 ("findings_excluded.sarif", "findings-excluded.sarif")],
    "outside": [("result_outside.json", "result.json"),
                ("findings_outside.sarif", "findings-outside.sarif")],
    "interrupted": [("result_interrupted.json", "result.json")],
    "garbage_result": [("garbage.sarif", "result.json")],
    "oldversion": [("result_oldversion.json", "result.json")],
    "badstatus": [("result_badstatus.json", "result.json")],
    "wrongtool": [("result_wrongtool.json", "result.json")],
    "negative": [("result_negative.json", "result.json")],
    "badartifacts": [("result_badartifacts.json", "result.json")],
    "badname": [("result_badname.json", "result.json")],
    "missing_artifact": [("result_missing_artifact.json", "result.json")],
    "garbage_artifact": [("result_garbage_artifact.json", "result.json"),
                         ("garbage.sarif", "findings.sarif")],
    "fail_exit": [("result_complete.json", "result.json"),
                  ("findings_complete.sarif", "findings.sarif"),
                  ("coverage_complete.json", "coverage-receipt.json")],
}
if CASE == "sleep":
    time.sleep(30)
for source, destination in PLAN.get(CASE, []):
    copy(source, destination)
sys.exit(CODE)
'''

FAKE_BWRAP = r'''#!/usr/bin/env python3
import json, os, sys

RECORD = "@RECORD@"
PROVIDER = "@PROVIDER@"
PYTHON = "@PYTHON@"
CASE = "@CASE@"
CODE = "@CODE@"
FAIL_PROBE = @FAILPROBE@
PASSTHROUGH = @PASSTHROUGH@
FIXTURES = "@FIXTURES@"

argv = sys.argv[1:]
command = argv[argv.index("--") + 1:] if "--" in argv else []
if command and command[0].endswith("/true"):
    if FAIL_PROBE:
        sys.stderr.write("bwrap: AppArmor denied userns creation\n")
        sys.exit(1)
    sys.exit(0)
with open(RECORD, "w", encoding="utf-8") as handle:
    json.dump(argv, handle)
environment = {"PATH": "/usr/bin:/bin"}
index = 0
while index < len(argv):
    if argv[index] == "--setenv":
        environment[argv[index + 1]] = argv[index + 2]
        index += 3
    else:
        index += 1
if PASSTHROUGH:
    os.execve(command[0], command, environment)
os.execve(PYTHON, [PYTHON, PROVIDER, CASE, FIXTURES, CODE], environment)
'''


def write_fakes(tmp_path: Path, *, case: str = "complete", code: int = 0,
                fail_probe: bool = False, passthrough: bool = False) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    provider = tmp_path / "fake_provider.py"
    provider.write_text(FAKE_PROVIDER, encoding="utf-8")
    program = tmp_path / "fake_bwrap.py"
    body = (FAKE_BWRAP
            .replace("@RECORD@", str(tmp_path / "bwrap-record.json"))
            .replace("@PROVIDER@", str(provider))
            .replace("@PYTHON@", sys.executable)
            .replace("@CASE@", case)
            .replace("@CODE@", str(code))
            .replace("@FAILPROBE@", "True" if fail_probe else "False")
            .replace("@PASSTHROUGH@", "True" if passthrough else "False")
            .replace("@FIXTURES@", str(FIXTURES)))
    program.write_text(body, encoding="utf-8")
    program.chmod(0o755)
    provider.chmod(0o755)
    return program


def run_args(tmp_path: Path, repo: Path, policy: Path, program: Path, *,
             approve: bool = True, extra: list[str] | None = None) -> list[str]:
    arguments = ["run", "--source-root", str(repo), "--policy", str(policy),
                 "--provider-command", str(tmp_path / "venv" / "codex-security"),
                 "--sandbox-program", str(program)]
    if approve:
        arguments.append("--approve")
    return arguments + (extra or [])


def report_root(repo: Path) -> Path:
    return repo / "backend" / "tmp" / "codex-security"


def run_directories(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir())


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def mask_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[index] == flag and argv[index + 1] == value
               for index in range(len(argv) - 1))


def setenv_value(argv: list[str], key: str) -> str | None:
    for index in range(len(argv) - 2):
        if argv[index] == "--setenv" and argv[index + 1] == key:
            return argv[index + 2]
    return None


def test_plan_reports_scope_digests_and_budgets(capsys):
    exit_code = csr.main(["plan", "--source-root", str(REPO_ROOT)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_OK
    assert payload["commit"] == git(REPO_ROOT, "rev-parse", "HEAD")
    assert payload["files"] > 100
    assert payload["config_digest"] == sha_of_policy(REAL_POLICY)
    assert payload["preview"] == "disabled"
    assert payload["egress"] == {"approved": False, "allowlist": ["api.openai.com:443"]}
    assert payload["budgets"]["max_cost_usd"] == 25.0
    assert payload["excluded"]["files"] >= 0


def sha_of_policy(path: Path) -> str:
    return csr.sha256_bytes(csr.canonical_bytes(read_json(path)))


def read_json_stdout(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_plan_denies_paths_outside_a_toplevel(tmp_path, capsys):
    nested = tmp_path / "not-a-repo"
    nested.mkdir()
    assert csr.main(["plan", "--source-root", str(nested)]) == csr.EXIT_DENIED
    assert "denied" in capsys.readouterr().err


def test_plan_denies_subdirectory_source_root(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path)
    exit_code = csr.main(["plan", "--source-root", str(repo / "backend"),
                          "--policy", str(policy), "--sandbox-program", str(program)])
    assert exit_code == csr.EXIT_DENIED
    assert "toplevel" in capsys.readouterr().err


def test_run_requires_explicit_approval(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program, approve=False))
    captured = capsys.readouterr()
    assert exit_code == csr.EXIT_DENIED
    assert "--approve" in captured.err
    assert not report_root(repo).exists()


def test_run_complete_sanitizes_and_binds_report(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_OK
    assert payload["status"] == "complete"
    assert payload["findings_count"] == 2
    run_dir = Path(payload["run_dir"])
    report = read_json(run_dir / "report.json")
    assert report["status"] == "complete"
    assert report["provider"]["reviewed_commit"] == csr.REVIEWED_PROVIDER_COMMIT
    assert report["source"]["commit"] == git(repo, "rev-parse", "HEAD")
    assert report["source"]["digest"] == payload["source_digest"]
    assert report["config_digest"] == sha_of_policy(policy)
    assert report["egress"] == {"approved": False, "allowlist": ["api.openai.com:443"]}
    assert report["budgets"]["used"]["cost_usd"] == 0.42
    assert report["sanitized"] is True
    sarif_text = (run_dir / "findings.sarif").read_text(encoding="utf-8")
    assert "@SOURCE_ROOT@" not in sarif_text
    assert "SYNTHETIC_SNIPPET_CONTENT_SHOULD_BE_STRIPPED" not in sarif_text
    sarif = read_json(run_dir / "findings.sarif")
    uris = _uris(sarif)
    assert "backend/orchestrator/example.py" in uris
    assert "scripts/tool.py" in uris
    assert sarif["runs"][0]["originalUriBaseIds"]["SRCROOT"]["uri"] == "."
    receipt = read_json(run_dir / "coverage-receipt.json")
    assert receipt["observed"][0]["path"] == "backend/orchestrator/example.py"
    for name in ("report.json", "findings.sarif", "coverage-receipt.json"):
        assert (run_dir / name).stat().st_mode & 0o777 == 0o600
    ledger = read_json(report_root(repo) / "ledger.json")
    assert len(ledger["entries"]) == 1
    entry = ledger["entries"][0]
    assert entry["kind"] == "run" and entry["counted"] is True
    assert entry["status"] == "complete" and entry["cost_usd"] == 0.42
    assert entry["report_digest"] == report["report_digest"]
    argv = read_json(tmp_path / "bwrap-record.json")
    assert "--unshare-all" in argv and "--clearenv" in argv
    assert "--share-net" not in argv
    assert mask_pair(argv, "--tmpfs", str(repo / "backend" / "data"))
    assert mask_pair(argv, "--ro-bind", "/dev/null")
    assert str(repo / ".env") in argv
    assert mask_pair(argv, "--bind", str(run_dir / "raw"))
    assert setenv_value(argv, "CODEX_SECURITY_EGRESS_APPROVED") == "0"
    assert "api.openai.com:443" in setenv_value(argv, "CODEX_SECURITY_EGRESS_ALLOWLIST")
    command_index = argv.index("--")
    assert argv[command_index + 1] == str(tmp_path / "venv" / "codex-security")


def _uris(node):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uri" and isinstance(value, str):
                found.append(value)
            found.extend(_uris(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_uris(item))
    return found


def test_run_denies_tracked_report_root(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program,
                                  extra=["--report-root", "scripts"]))
    assert exit_code == csr.EXIT_DENIED
    assert "git-ignored" in capsys.readouterr().err
    assert not (repo / "scripts" / "ledger.json").exists()


def test_run_refuses_when_run_budget_exhausted(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo, budgets={"max_runs": 1})
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    captured = capsys.readouterr()
    assert exit_code == csr.EXIT_STOPPED
    assert "budget:max_runs" in captured.err
    entries = read_json(report_root(repo) / "ledger.json")["entries"]
    assert [entry["kind"] for entry in entries] == ["run", "refused"]
    assert entries[1]["counted"] is False


def test_run_stops_without_artifacts_when_cost_exceeded(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo, budgets={"max_cost_usd": 0.5})
    program = write_fakes(tmp_path, case="overcost", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_STOPPED
    assert payload["status"] == "stopped"
    run_dir = Path(payload["run_dir"])
    report = read_json(run_dir / "report.json")
    assert report["status"] == "stopped"
    assert report["error_detail"] in ("budget:max_cost_usd", "budget:max_calls_per_run")
    assert report["artifacts"]["sarif"] is None
    assert not (run_dir / "findings.sarif").exists()
    entry = read_json(report_root(repo) / "ledger.json")["entries"][0]
    assert entry["status"] == "stopped" and entry["counted"] is True
    assert entry["cost_usd"] == 999.0


def test_run_refused_before_provider_when_cost_already_over(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    root = report_root(repo)
    root.mkdir(parents=True)
    csr.append_ledger(root, {"kind": "run", "counted": True, "cost_usd": 30.0,
                             "calls_used": 1, "status": "complete"})
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    assert exit_code == csr.EXIT_STOPPED
    assert "budget:max_cost_usd" in capsys.readouterr().err
    assert not (tmp_path / "bwrap-record.json").exists()


def test_run_error_on_provider_failure_preserves_raw(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="fail_exit", code=7)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    assert payload["status"] == "error"
    run_dir = Path(payload["run_dir"])
    assert (run_dir / "raw" / "provider-stderr.log").exists()
    report = read_json(run_dir / "report.json")
    assert report["status"] == "error"
    assert report["artifacts"]["sarif"] is None
    entry = read_json(report_root(repo) / "ledger.json")["entries"][0]
    assert entry["status"] == "error" and entry["calls_used"] == 3


def test_run_error_on_missing_result(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="missing", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    run_dir = Path(payload["run_dir"])
    assert read_json(run_dir / "report.json")["error_detail"].startswith(
        "provider result file is missing")


def test_run_error_on_invalid_result_schema(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="badschema", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    assert read_json(Path(payload["run_dir"]) / "report.json")["status"] == "error"


def test_run_partial_labels_coverage_missing(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="partial", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_OK
    assert payload["status"] == "partial"
    run_dir = Path(payload["run_dir"])
    assert (run_dir / "findings.sarif").exists()
    assert not (run_dir / "coverage-receipt.json").exists()
    report = read_json(run_dir / "report.json")
    assert report["status"] == "partial"
    assert report["artifacts"]["coverage_receipt"] is None


def test_run_error_when_sarif_references_excluded_path(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="excluded", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    run_dir = Path(payload["run_dir"])
    report = read_json(run_dir / "report.json")
    assert report["status"] == "error"
    assert report["error_detail"].startswith("sanitization:")
    assert "backend/tmp/leaked.txt" in report["error_detail"]
    assert not (run_dir / "findings.sarif").exists()


def test_run_recovery_after_error_succeeds(tmp_path):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    failing = write_fakes(tmp_path / "one", case="fail_exit", code=7)
    assert csr.main(run_args(tmp_path, repo, policy, failing)) == csr.EXIT_ERROR
    succeeding = write_fakes(tmp_path / "two", case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, succeeding)) == csr.EXIT_OK
    entries = read_json(report_root(repo) / "ledger.json")["entries"]
    assert [entry["status"] for entry in entries] == ["error", "complete"]


def test_sandbox_probe_failure_is_preserved_and_denies(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, fail_probe=True)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    captured = capsys.readouterr()
    assert exit_code == csr.EXIT_DENIED
    assert "AppArmor denied userns creation" in captured.err
    entry = read_json(report_root(repo) / "ledger.json")["entries"][0]
    assert entry["kind"] == "refused"
    assert "sandbox-preflight" in entry["reason"]


def test_missing_sandbox_program_denies(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    exit_code = csr.main(run_args(tmp_path, repo, policy, tmp_path / "absent" / "bwrap"))
    assert exit_code == csr.EXIT_DENIED
    assert "sandbox program" in capsys.readouterr().err


def test_missing_provider_command_reports_error(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, passthrough=True)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    run_dir = Path(payload["run_dir"])
    stderr_log = (run_dir / "raw" / "provider-stderr.log").read_text(encoding="utf-8")
    assert "codex-security" in stderr_log or "No such file" in stderr_log


def test_egress_approval_shares_network_and_passes_allowlist(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program,
                                  extra=["--egress-approve"]))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_OK
    assert payload["egress_approved"] is True
    argv = read_json(tmp_path / "bwrap-record.json")
    assert "--share-net" in argv
    assert setenv_value(argv, "CODEX_SECURITY_EGRESS_APPROVED") == "1"
    report = read_json(Path(payload["run_dir"]) / "report.json")
    assert report["sandbox"]["network_shared"] is True


def test_interrupted_run_is_distinct_and_preserves_raw(tmp_path):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="sleep", code=0)
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    process = subprocess.Popen(
        [sys.executable, str(WRAPPER), *run_args(tmp_path, repo, policy, program)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, text=True,
        start_new_session=True)
    raw_dir = report_root(repo) / "in-progress"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        candidates = run_directories(report_root(repo)) if report_root(repo).exists() else []
        if candidates:
            raw_dir = candidates[0] / "raw"
            break
        time.sleep(0.05)
    assert raw_dir.parent.exists(), "run directory was not created before the signal"
    time.sleep(0.3)
    os.kill(process.pid, signal.SIGINT)
    stdout, _stderr = process.communicate(timeout=30)
    assert process.returncode == csr.EXIT_INTERRUPTED
    payload = json.loads(stdout)
    assert payload["status"] == "interrupted"
    assert payload["interrupt_reason"] == "signal:SIGINT"
    run_dir = Path(payload["run_dir"])
    assert (run_dir / "raw").is_dir()
    report = read_json(run_dir / "report.json")
    assert report["status"] == "interrupted"
    assert report["interrupt_reason"] == "signal:SIGINT"
    assert report["artifacts"]["sarif"] is None
    entry = read_json(report_root(repo) / "ledger.json")["entries"][0]
    assert entry["status"] == "interrupted" and entry["counted"] is True


def test_wait_for_provider_terminates_process_group():
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    controller = csr.StopController()
    controller.trigger("budget:max_seconds")
    returncode, interrupted = csr.wait_for_provider(process, controller)
    assert interrupted is True and returncode is None
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process.poll() is not None


def test_verify_accepts_unmodified_run(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_OK
    assert payload["verified"] is True and payload["reasons"] == []


def test_verify_fails_on_tampered_report(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    report = read_json(run_dir / "report.json")
    report["findings_count"] = 0
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("report_digest" in reason for reason in payload["reasons"])


def test_verify_fails_after_source_content_changes_without_commit(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    (repo / "scripts" / "tool.py").write_text("VALUE = 2\n")
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("source content changed" in reason for reason in payload["reasons"])


def test_verify_fails_after_source_commit_moves(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    (repo / "scripts" / "extra.py").write_text("NEW = 1\n")
    git(repo, "add", "scripts/extra.py")
    git(repo, "commit", "-q", "-m", "drift")
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("no longer at the reviewed source commit" in reason
               for reason in payload["reasons"])


def test_verify_fails_after_policy_change(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    write_policy(repo, budgets={"max_runs": 99})
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("policy changed" in reason for reason in payload["reasons"])


def test_verify_fails_when_artifact_modified(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    with (run_dir / "findings.sarif").open("a", encoding="utf-8") as handle:
        handle.write(" ")
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("sarif" in reason for reason in payload["reasons"])


def test_preview_always_refuses(capsys):
    assert csr.main(["preview"]) == csr.EXIT_DENIED
    assert "preview is disabled" in capsys.readouterr().err


@pytest.mark.parametrize(("mutation", "fragment"), [
    ({"schema_version": 2}, "schema_version"),
    ({"provider": None}, "provider.tool"),
    ({"provider": {"result_file": 7}}, "result_file"),
    ({"provider": {"args": "scan"}}, "non-empty list"),
    ({"provider": {"args": [""]}}, "non-empty strings"),
    ({"scope": None}, "scope must be"),
    ({"scope": {"max_files": "many"}}, "max_files"),
    ({"scope": {"max_bytes": 0}}, "max_bytes"),
    ({"budgets": None}, "budgets must be"),
    ({"budgets": {"max_seconds": 0}}, "max_seconds"),
    ({"reports": None}, "reports.directory"),
    ({"preview": {"enabled": True}}, "preview"),
    ({"provider": {"reviewed_source": {"commit": "0" * 40}}}, "reviewed_source"),
    ({"provider": {"endpoint_allowlist": ["api.openai.com"]}}, "endpoint_allowlist"),
    ({"budgets": {"max_cost_usd": 0}}, "max_cost_usd"),
    ({"reports": {"require_untracked": False}}, "require_untracked"),
    ({"sandbox": {"program": "unsandboxed"}}, "sandbox"),
    ({"scope": {"include_roots": ["/etc"]}}, "include root"),
    ({"provider": {"args": ["scan", "{bogus}"]}}, "placeholder"),
])
def test_policy_validation_rejects_drift(mutation, fragment):
    policy = json.loads(REAL_POLICY.read_text(encoding="utf-8"))
    for key, value in mutation.items():
        if isinstance(value, dict) and isinstance(policy.get(key), dict):
            policy[key].update(value)
        else:
            policy[key] = value
    with pytest.raises(csr.Denial, match=fragment):
        csr.validate_policy(policy)


def test_relative_uri_rules():
    repo = Path("/reviewed-checkout")
    assert csr.relative_uri("file:///reviewed-checkout/", repo) == "."
    assert csr.relative_uri("file:///reviewed-checkout", repo) == "."
    assert csr.relative_uri("/reviewed-checkout/backend/x.py", repo) == "backend/x.py"
    assert csr.relative_uri("backend/x.py", repo) == "backend/x.py"
    with pytest.raises(csr.SanitizationError):
        csr.relative_uri("/etc/passwd", repo)


def test_mask_targets_finds_sensitive_files_and_dirs(tmp_path):
    repo = build_checkout(tmp_path)
    policy = json.loads(REAL_POLICY.read_text(encoding="utf-8"))
    masked_files, masked_dirs = csr.mask_targets(repo, policy)
    assert ".env" in masked_files
    assert "certs/server.pem" in masked_files
    assert "backend/data" in masked_dirs
    assert "backend/tmp" in masked_dirs
    assert not any(name == ".git" or name.startswith(".git/") for name in masked_files)


def test_ledger_append_is_atomic_and_ordered(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    csr.append_ledger(root, {"kind": "run", "counted": True, "status": "complete"})
    csr.append_ledger(root, {"kind": "run", "counted": True, "status": "error"})
    entries = csr.read_ledger(root)
    assert [entry["status"] for entry in entries] == ["complete", "error"]
    assert csr.budget_state(entries) == {"runs": 2, "cost_usd": 0.0}


RESULT_CONTRACT_CASES = {
    "garbage_result": "result file is unreadable",
    "badstatus": "status must be one of",
    "wrongtool": "pinned tool",
    "negative": "non-negative number",
    "badartifacts": "artifacts must be an object",
    "badname": "simple filename",
    "missing_artifact": "artifact is missing",
    "garbage_artifact": "artifact is unreadable",
    "oldversion": "schema_version must be 1",
}


@pytest.mark.parametrize("case", sorted(RESULT_CONTRACT_CASES))
def test_run_rejects_provider_contract_violations(tmp_path, capsys, case):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case=case, code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    run_dir = Path(payload["run_dir"])
    report = read_json(run_dir / "report.json")
    assert report["status"] == "error"
    assert RESULT_CONTRACT_CASES[case] in report["error_detail"]
    assert report["artifacts"]["sarif"] is None
    entry = read_json(report_root(repo) / "ledger.json")["entries"][0]
    assert entry["status"] == "error" and entry["counted"] is True


def test_run_reports_provider_self_interrupted_status(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="interrupted", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_INTERRUPTED
    assert payload["status"] == "interrupted"
    report = read_json(Path(payload["run_dir"]) / "report.json")
    assert report["status"] == "interrupted"
    assert report["provider"]["reported_version"] == "0.1.0-synthetic"


def test_run_error_when_finding_escapes_the_checkout(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="outside", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    report = read_json(Path(payload["run_dir"]) / "report.json")
    assert "outside the approved checkout" in report["error_detail"]


def test_run_error_when_artifact_exceeds_declared_limit(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo, budgets={"max_artifact_bytes": 10})
    program = write_fakes(tmp_path, case="complete", code=0)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_ERROR
    report = read_json(Path(payload["run_dir"]) / "report.json")
    assert "exceeds 10 bytes" in report["error_detail"]


@pytest.mark.parametrize(("budget", "fragment"), [
    ({"max_files": 1}, "max_files"),
    ({"max_bytes": 1}, "max_bytes"),
])
def test_plan_denies_scope_over_budget(tmp_path, capsys, budget, fragment):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo, scope=budget)
    exit_code = csr.main(["plan", "--source-root", str(repo), "--policy", str(policy)])
    assert exit_code == csr.EXIT_DENIED
    assert fragment in capsys.readouterr().err


def test_run_denies_corrupt_or_foreign_ledger(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    root = report_root(repo)
    root.mkdir(parents=True)
    (root / "ledger.json").write_text("not json", encoding="utf-8")
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_DENIED
    assert "ledger is corrupt" in capsys.readouterr().err
    (root / "ledger.json").write_text('{"schema_version": 9, "entries": []}', encoding="utf-8")
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_DENIED
    assert "unexpected schema" in capsys.readouterr().err


def test_run_denies_unusable_report_roots(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    outside = tmp_path / "elsewhere"
    assert csr.main(run_args(tmp_path, repo, policy, program,
                             extra=["--report-root", str(outside)])) == csr.EXIT_DENIED
    assert "inside the checkout" in capsys.readouterr().err
    assert csr.main(run_args(tmp_path, repo, policy, program,
                             extra=["--report-root", "."])) == csr.EXIT_DENIED
    assert "toplevel" in capsys.readouterr().err
    blocker = repo / "backend" / "tmp" / "blocker"
    blocker.write_text("not a directory\n")
    assert csr.main(run_args(tmp_path, repo, policy, program,
                             extra=["--report-root", "backend/tmp/blocker"])) == csr.EXIT_DENIED
    assert "not a directory" in capsys.readouterr().err


def test_provider_api_key_forwarded_only_with_egress_approval(tmp_path, capsys, monkeypatch):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    monkeypatch.setenv("CODEX_SECURITY_PROVIDER_API_KEY", "synthetic-operator-key")
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    argv = read_json(tmp_path / "bwrap-record.json")
    assert setenv_value(argv, "CODEX_SECURITY_PROVIDER_API_KEY") is None
    assert csr.main(run_args(tmp_path, repo, policy, program,
                             extra=["--egress-approve"])) == csr.EXIT_OK
    argv = read_json(tmp_path / "bwrap-record.json")
    assert setenv_value(argv, "CODEX_SECURITY_PROVIDER_API_KEY") == "synthetic-operator-key"


def test_max_seconds_guard_and_override(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program,
                             extra=["--max-seconds", "0"])) == csr.EXIT_DENIED
    assert "max-seconds" in capsys.readouterr().err
    assert csr.main(run_args(tmp_path, repo, policy, program,
                             extra=["--max-seconds", "100"])) == csr.EXIT_OK


def test_wrapper_marks_interrupted_when_provider_wait_reports_interruption(
        tmp_path, capsys, monkeypatch):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)

    def interrupted_wait(_process, _controller):
        return None, True

    monkeypatch.setattr(csr, "wait_for_provider", interrupted_wait)
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_INTERRUPTED
    report = read_json(Path(payload["run_dir"]) / "report.json")
    assert report["status"] == "interrupted"
    assert report["artifacts"]["sarif"] is None
    entry = read_json(report_root(repo) / "ledger.json")["entries"][0]
    assert entry["status"] == "interrupted" and entry["calls_used"] == 0


def test_verify_reports_unreadable_report(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(tmp_path / "absent-run")])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("report unreadable" in reason for reason in payload["reasons"])


def test_verify_flags_wrong_provider_binding(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    report = read_json(run_dir / "report.json")
    report["provider"]["reviewed_commit"] = "0" * 40
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("reviewed provider commit" in reason for reason in payload["reasons"])


def test_verify_accepts_partial_run_without_receipt(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="partial", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_OK
    assert payload["verified"] is True


def test_verify_flags_missing_raw_evidence(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    (run_dir / "raw").rmdir() if (run_dir / "raw").is_dir() and not any((run_dir / "raw").iterdir()) \
        else shutil.rmtree(run_dir / "raw")
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("raw provider evidence" in reason for reason in payload["reasons"])


def test_verify_flags_inventory_failure(tmp_path, capsys, monkeypatch):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]

    def failing_inventory(_repo, _policy):
        raise csr.Denial("inventory exploded")

    monkeypatch.setattr(csr, "source_inventory", failing_inventory)
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("inventory failed" in reason for reason in payload["reasons"])


def test_verify_flags_missing_ledger_receipt(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = write_fakes(tmp_path, case="complete", code=0)
    assert csr.main(run_args(tmp_path, repo, policy, program)) == csr.EXIT_OK
    run_dir = run_directories(report_root(repo))[0]
    (report_root(repo) / "ledger.json").unlink()
    exit_code = csr.main(["verify", "--source-root", str(repo), "--policy", str(policy),
                          "--run-dir", str(run_dir)])
    payload = read_json_stdout(capsys)
    assert exit_code == csr.EXIT_VERIFY_FAILED
    assert any("budget ledger" in reason for reason in payload["reasons"])


def test_signal_relay_records_reason_in_process():
    controller = csr.StopController()
    previous = csr.install_signal_relay(controller)
    try:
        os.kill(os.getpid(), signal.SIGINT)
        assert controller.event.is_set()
        assert controller.reason == "signal:SIGINT"
    finally:
        csr.restore_signal_handlers(previous)


def test_signal_handlers_are_thread_safe_noops():
    outcome = {}

    def target():
        controller = csr.StopController()
        previous = csr.install_signal_relay(controller)
        csr.restore_signal_handlers(previous)
        outcome["done"] = True

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=10)
    assert outcome.get("done") is True


def test_terminate_provider_escalates_to_sigkill(tmp_path):
    ready = tmp_path / "ready"
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, sys, time; "
         "open(sys.argv[1], 'w').write('ready'); "
         "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
         str(ready)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "child never signalled readiness"
    started = time.monotonic()
    csr.terminate_provider(process)
    assert time.monotonic() - started >= 4
    assert process.poll() is not None


def test_sandbox_program_is_located_on_path(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    assert csr.main(run_args(tmp_path, repo, policy, tmp_path / "absent" / "bwrap")) \
        == csr.EXIT_DENIED
    assert "does not exist" in capsys.readouterr().err
    exit_code = csr.main(run_args(tmp_path, repo, policy, "no-such-sandbox-anywhere"))
    assert exit_code == csr.EXIT_DENIED
    assert "not found on PATH" in capsys.readouterr().err
    exit_code = csr.main(run_args(tmp_path, repo, policy, "python3"))
    assert exit_code == csr.EXIT_DENIED
    assert "preflight" in capsys.readouterr().err


def test_probe_execution_failure_is_preserved(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    policy = write_policy(repo)
    program = tmp_path / "sandbox-directory"
    program.mkdir()
    exit_code = csr.main(run_args(tmp_path, repo, policy, program))
    captured = capsys.readouterr()
    assert exit_code == csr.EXIT_DENIED
    assert "preflight" in captured.err
    entry = read_json(report_root(repo) / "ledger.json")["entries"][0]
    assert "sandbox-preflight" in entry["reason"]


def test_plan_denies_missing_source_root_and_policy(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    assert csr.main(["plan", "--source-root", str(tmp_path / "absent")]) == csr.EXIT_DENIED
    assert "not a directory" in capsys.readouterr().err
    exit_code = csr.main(["plan", "--source-root", str(repo),
                          "--policy", str(tmp_path / "missing-policy.json")])
    assert exit_code == csr.EXIT_DENIED
    assert "policy unreadable" in capsys.readouterr().err


def test_inventory_applies_name_exclusions(tmp_path):
    repo = build_checkout(tmp_path)
    policy = csr.load_policy(write_policy(repo))
    entries, _digest, excluded = csr.source_inventory(repo, policy)
    paths = {entry["path"] for entry in entries}
    assert "backend/legacy.key" not in paths
    assert "backend/orchestrator/example.py" in paths
    assert excluded["by_reason"]["name:*.key"] == 1
    assert excluded["by_reason"]["path:backend/data"] == 1


def test_sanitize_document_requires_object():
    with pytest.raises(csr.SanitizationError):
        csr.sanitize_document([], Path("/repo"), json.loads(REAL_POLICY.read_text()))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission denials")
def test_inventory_denies_unreadable_file(tmp_path, capsys):
    repo = build_checkout(tmp_path)
    write_policy(repo)
    sealed = repo / "backend" / "sealed"
    sealed.mkdir()
    (sealed / "code.py").write_text("VALUE = 1\n")
    git(repo, "add", "-f", "backend/sealed/code.py")
    git(repo, "commit", "-q", "-m", "sealed")
    sealed.chmod(0o000)
    try:
        exit_code = csr.main(["plan", "--source-root", str(repo),
                              "--policy", str(repo / "review-policy.json")])
    finally:
        sealed.chmod(0o755)
    assert exit_code == csr.EXIT_DENIED
    assert "inventory file unreadable" in capsys.readouterr().err
