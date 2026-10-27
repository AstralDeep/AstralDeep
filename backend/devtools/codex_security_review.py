"""Isolated report-only CLI wrapper for opt-in Codex Security source reviews of an approved
checkout: sandboxed provider execution under Bubblewrap, bounded run/call/cost budgets, and
sanitized finding/SARIF/coverage artifacts digest-bound to the exact source and policy.
Developer tooling only — never a runtime, CI, or release gate — the preview surface is
permanently disabled in favor of private CLI report files.
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

REVIEWED_PROVIDER_COMMIT = "f894f1ff64b1a332001f48593c1622b85701f9f3"
DEFAULT_POLICY = Path("tooling/codex-security/review-policy.json")
RESULT_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_DENIED = 2
EXIT_STOPPED = 3
EXIT_INTERRUPTED = 4
EXIT_ERROR = 5
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_INTERRUPTED = "interrupted"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"
RUN_STATUSES = (STATUS_COMPLETE, STATUS_PARTIAL, STATUS_INTERRUPTED, STATUS_STOPPED, STATUS_ERROR)
PROVIDER_STATUSES = frozenset({STATUS_COMPLETE, STATUS_PARTIAL, STATUS_INTERRUPTED, STATUS_ERROR})
ENDPOINT_PATTERN = re.compile(r"^[A-Za-z0-9.-]+:[0-9]{1,5}$")
SIMPLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ARG_PLACEHOLDERS = frozenset({"output_dir"})
PATH_VALUE_KEYS = frozenset({"uri", "path", "file"})
SNIPPET_KEYS = ("snippet", "contextRegion")
POLL_SECONDS = 0.25
KILL_GRACE_SECONDS = 5


class Denial(RuntimeError):
    pass


class ResultError(RuntimeError):
    pass


class SanitizationError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Denial(f"git {arguments[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def resolve_checkout(source_root: Path) -> tuple[Path, str]:
    root = Path(os.path.abspath(source_root))
    if not root.is_dir():
        raise Denial(f"source root is not a directory: {source_root}")
    top = Path(run_git(root, "rev-parse", "--show-toplevel").strip())
    if root != top:
        raise Denial("source root must be the checkout toplevel")
    commit = run_git(top, "rev-parse", "HEAD").strip()
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise Denial("checkout has no exact commit identity")
    return top, commit


def load_policy(path: Path) -> dict:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Denial(f"policy unreadable at {path}: {error}") from error
    validate_policy(policy)
    return policy


def _require_string_list(container: dict, key: str, *, allow_empty: bool = False) -> None:
    value = container.get(key)
    if not isinstance(value, list) or (not allow_empty and not value):
        raise Denial(f"policy {key} must be a non-empty list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise Denial(f"policy {key} must contain only non-empty strings")


def validate_policy(policy: dict) -> None:
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise Denial("policy schema_version must be 1")
    provider = policy.get("provider")
    if not isinstance(provider, dict) or not isinstance(provider.get("tool"), str):
        raise Denial("policy provider.tool must be a string")
    reviewed = provider.get("reviewed_source")
    if (not isinstance(reviewed, dict)
            or not isinstance(reviewed.get("repository"), str)
            or reviewed.get("commit") != REVIEWED_PROVIDER_COMMIT
            or not isinstance(reviewed.get("reviewed_on"), str)):
        raise Denial(f"policy provider.reviewed_source must pin commit {REVIEWED_PROVIDER_COMMIT}")
    _require_string_list(provider, "args")
    for argument in provider["args"]:
        for token in re.findall(r"\{([^}]+)\}", argument):
            if token not in ARG_PLACEHOLDERS:
                raise Denial(f"policy provider.args has unknown placeholder {{{token}}}")
    if not isinstance(provider.get("result_file"), str):
        raise Denial("policy provider.result_file must be a string")
    _require_string_list(provider, "endpoint_allowlist")
    if any(ENDPOINT_PATTERN.fullmatch(item) is None for item in provider["endpoint_allowlist"]):
        raise Denial("policy endpoint_allowlist entries must be host:port")
    sandbox = policy.get("sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("program") != "bwrap":
        raise Denial("policy sandbox.program must be bwrap")
    _require_string_list(sandbox, "mask_files")
    _require_string_list(sandbox, "mask_dirs")
    scope = policy.get("scope")
    if not isinstance(scope, dict):
        raise Denial("policy scope must be an object")
    _require_string_list(scope, "include_roots")
    for root in scope["include_roots"]:
        if root.startswith("/") or ".." in root.split("/"):
            raise Denial(f"policy include root must be inside the checkout: {root}")
    _require_string_list(scope, "exclude_paths", allow_empty=True)
    _require_string_list(scope, "exclude_names", allow_empty=True)
    if not isinstance(scope.get("max_files"), int) or scope["max_files"] <= 0:
        raise Denial("policy scope.max_files must be a positive integer")
    if not isinstance(scope.get("max_bytes"), int) or scope["max_bytes"] <= 0:
        raise Denial("policy scope.max_bytes must be a positive integer")
    budgets = policy.get("budgets")
    if not isinstance(budgets, dict):
        raise Denial("policy budgets must be an object")
    for key in ("max_runs", "max_calls_per_run", "max_seconds", "max_artifact_bytes"):
        if not isinstance(budgets.get(key), int) or budgets[key] <= 0:
            raise Denial(f"policy budgets.{key} must be a positive integer")
    cost = budgets.get("max_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
        raise Denial("policy budgets.max_cost_usd must be a positive number")
    reports = policy.get("reports")
    if not isinstance(reports, dict) or not isinstance(reports.get("directory"), str):
        raise Denial("policy reports.directory must be a string")
    if reports.get("require_untracked") is not True:
        raise Denial("policy reports.require_untracked must be true")
    if policy.get("preview") != {"enabled": False}:
        raise Denial("policy preview must be exactly {\"enabled\": false}")


def excluded_reason(relative: str, name: str, policy: dict) -> str | None:
    scope = policy["scope"]
    for base in scope["exclude_paths"]:
        if relative == base or relative.startswith(base + "/"):
            return f"path:{base}"
    for pattern in scope["exclude_names"]:
        if fnmatch.fnmatch(name, pattern):
            return f"name:{pattern}"
    return None


def source_inventory(repo: Path, policy: dict) -> tuple[list[dict], str, dict]:
    scope = policy["scope"]
    listed = run_git(repo, "ls-files", "-s", "-z", "--", *scope["include_roots"])
    entries: list[dict] = []
    excluded_files = 0
    excluded_bytes = 0
    excluded_by_reason: dict[str, int] = {}
    total_bytes = 0
    for record in listed.split("\0"):
        if not record:
            continue
        metadata, relative = record.split("\t", 1)
        mode = metadata.split(" ", 1)[0]
        if mode == "160000":
            continue
        name = relative.rsplit("/", 1)[-1]
        reason = excluded_reason(relative, name, policy)
        path = repo / relative
        try:
            size = path.stat().st_size
        except OSError as error:
            raise Denial(f"inventory file unreadable: {relative}: {error}") from error
        if reason is not None:
            excluded_files += 1
            excluded_bytes += size
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
            continue
        total_bytes += size
        entries.append({"path": relative, "size": size, "sha256": sha256_file(path)})
    if len(entries) > scope["max_files"]:
        raise Denial(f"scope exceeds max_files budget: {len(entries)} > {scope['max_files']}")
    if total_bytes > scope["max_bytes"]:
        raise Denial(f"scope exceeds max_bytes budget: {total_bytes} > {scope['max_bytes']}")
    entries.sort(key=lambda item: item["path"])
    digest = sha256_bytes("".join(
        f"{item['path']} {item['sha256']}\n" for item in entries).encode())
    excluded = {"files": excluded_files, "bytes": excluded_bytes, "by_reason": excluded_by_reason}
    return entries, digest, excluded


def resolve_sandbox_program(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        located = shutil.which(value)
        if located is None:
            raise Denial(f"sandbox program not found on PATH: {value}")
        candidate = Path(located)
    if not candidate.exists():
        raise Denial(f"sandbox program does not exist: {candidate}")
    return candidate


def sandbox_probe(program: Path) -> tuple[bool, str]:
    argv = [str(program), "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--unshare-all", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
            "--", "/bin/true"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"sandbox probe failed to execute: {error}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or f"sandbox probe exited {result.returncode}"
    return True, ""


def dir_is_excluded(relative: str, policy: dict) -> bool:
    scope = policy["scope"]
    for base in scope["exclude_paths"]:
        if relative == base or relative.startswith(base + "/"):
            return True
    return False


def mask_targets(repo: Path, policy: dict) -> tuple[list[str], list[str]]:
    patterns = policy["sandbox"]["mask_files"]
    masked_files: list[str] = []
    masked_dirs: list[str] = []
    for name in policy["sandbox"]["mask_dirs"]:
        target = repo / name
        if target.is_dir() and not target.is_symlink():
            masked_dirs.append(name)
    for base, directories, files in os.walk(repo, topdown=True, onerror=lambda _error: None):
        relative_base = Path(base).relative_to(repo)
        relative_posix = "" if str(relative_base) == "." else relative_base.as_posix()
        directories[:] = sorted(
            name for name in directories
            if name != ".git" and not dir_is_excluded(f"{relative_posix}/{name}".lstrip("/"), policy)
        )
        for file_name in sorted(files):
            if any(fnmatch.fnmatch(file_name, pattern) for pattern in patterns):
                masked_files.append(f"{relative_posix}/{file_name}".lstrip("/"))
    return masked_files, masked_dirs


def sandbox_argv(program: Path, repo: Path, raw_dir: Path, provider_argv: list[str],
                 policy: dict, env_pairs: list[tuple[str, str]], egress_approved: bool,
                 masked_files: list[str], masked_dirs: list[str]) -> list[str]:
    argv = [str(program), "--unshare-all", "--die-with-parent", "--new-session",
            "--clearenv", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp"]
    if egress_approved:
        argv.append("--share-net")
    for name in masked_dirs:
        argv += ["--tmpfs", str(repo / name)]
    for relative in masked_files:
        argv += ["--ro-bind", "/dev/null", str(repo / relative)]
    argv += ["--bind", str(raw_dir), str(raw_dir)]
    for key, value in env_pairs:
        argv += ["--setenv", key, value]
    argv += ["--", *provider_argv]
    return argv


class StopController:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reason = ""

    def trigger(self, reason: str) -> None:
        if not self.reason:
            self.reason = reason
        self.event.set()


def install_signal_relay(controller: StopController) -> list[tuple[int, Any]]:
    def handler(number, _frame):
        controller.trigger(f"signal:{signal.Signals(number).name}")

    previous = []
    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            previous.append((number, signal.getsignal(number)))
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass
    return previous


def restore_signal_handlers(previous: list[tuple[int, Any]]) -> None:
    for number, handler in previous:
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


def terminate_provider(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    process.wait(timeout=30)


def wait_for_provider(process: subprocess.Popen, controller: StopController
                      ) -> tuple[int | None, bool]:
    while True:
        try:
            return process.wait(timeout=POLL_SECONDS), False
        except subprocess.TimeoutExpired:
            pass
        if controller.event.is_set():
            terminate_provider(process)
            return None, True


def read_result(raw_dir: Path, policy: dict) -> tuple[dict, dict | None, dict | None]:
    result_path = raw_dir / policy["provider"]["result_file"]
    try:
        document = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ResultError("provider result file is missing") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ResultError(f"provider result file is unreadable: {error}") from error
    validate_result(document, policy)
    artifacts = document.get("artifacts") or {}
    limit = policy["budgets"]["max_artifact_bytes"]
    sarif = _load_artifact(raw_dir, artifacts.get("sarif"), limit, "sarif")
    receipt = _load_artifact(raw_dir, artifacts.get("coverage_receipt"), limit, "coverage receipt")
    return document, sarif, receipt


def _load_artifact(raw_dir: Path, name: Any, limit: int, label: str) -> dict | None:
    if name is None:
        return None
    path = raw_dir / str(name)
    try:
        size = path.stat().st_size
        if size > limit:
            raise ResultError(f"provider {label} artifact exceeds {limit} bytes")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ResultError(f"provider {label} artifact is missing: {name}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ResultError(f"provider {label} artifact is unreadable: {error}") from error


def validate_result(document: Any, policy: dict) -> None:
    if not isinstance(document, dict) or document.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ResultError("provider result schema_version must be 1")
    if document.get("status") not in PROVIDER_STATUSES:
        raise ResultError(f"provider result status must be one of {sorted(PROVIDER_STATUSES)}")
    provider = document.get("provider")
    if (not isinstance(provider, dict) or provider.get("tool") != policy["provider"]["tool"]
            or not isinstance(provider.get("version"), str) or not provider["version"]):
        raise ResultError("provider result must identify the pinned tool and a version")
    for key in ("calls_used", "cost_usd"):
        value = document.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ResultError(f"provider result {key} must be a non-negative number")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ResultError("provider result artifacts must be an object")
    for key in ("sarif", "coverage_receipt"):
        value = artifacts.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or SIMPLE_NAME_PATTERN.fullmatch(value) is None:
            raise ResultError(f"provider result artifacts.{key} must be a simple filename")


def relative_uri(value: str, repo: Path) -> str:
    text = value[len("file://"):] if value.startswith("file://") else value
    prefix = str(repo)
    if text == prefix:
        return "."
    if text.startswith(prefix + "/"):
        remainder = text[len(prefix) + 1:]
        return "." if remainder in ("", "/") else remainder
    if text.startswith("/"):
        raise SanitizationError(f"finding path outside the approved checkout: {value}")
    return text


def _sanitize_paths(node: Any, repo: Path, policy: dict, violations: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in PATH_VALUE_KEYS and isinstance(value, str):
                try:
                    relative = relative_uri(value, repo)
                except SanitizationError as error:
                    violations.append(str(error))
                    continue
                node[key] = relative
                if relative not in (".", "./"):
                    reason = excluded_reason(relative, relative.rsplit("/", 1)[-1], policy)
                    if reason is not None:
                        violations.append(f"finding references excluded path {relative} ({reason})")
            _sanitize_paths(value, repo, policy, violations)
    elif isinstance(node, list):
        for item in node:
            _sanitize_paths(item, repo, policy, violations)


def _strip_snippets(node: Any) -> None:
    if isinstance(node, dict):
        for key in SNIPPET_KEYS:
            node.pop(key, None)
        for value in node.values():
            _strip_snippets(value)
    elif isinstance(node, list):
        for item in node:
            _strip_snippets(item)


def sanitize_document(document: Any, repo: Path, policy: dict) -> tuple[Any, int]:
    if not isinstance(document, dict):
        raise SanitizationError("artifact is not a JSON object")
    _strip_snippets(document)
    violations: list[str] = []
    _sanitize_paths(document, repo, policy, violations)
    if violations:
        raise SanitizationError("; ".join(violations[:5]))
    findings = 0
    for run in document.get("runs", []) if isinstance(document.get("runs"), list) else []:
        results = run.get("results")
        if isinstance(results, list):
            findings += len(results)
    return document, findings


def read_ledger(root: Path) -> list[dict]:
    path = root / "ledger.json"
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Denial(f"budget ledger is corrupt at {path}: {error}") from error
    if (not isinstance(document, dict) or document.get("schema_version") != LEDGER_SCHEMA_VERSION
            or not isinstance(document.get("entries"), list)):
        raise Denial(f"budget ledger has an unexpected schema at {path}")
    return document["entries"]


def append_ledger(root: Path, entry: dict) -> None:
    path = root / "ledger.json"
    with open(root / "ledger.lock", "a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        existing = read_ledger(root)
        document = {"schema_version": LEDGER_SCHEMA_VERSION, "entries": [*existing, entry]}
        atomic_write(path, canonical_bytes(document) + b"\n")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def budget_state(entries: list[dict]) -> dict:
    counted = [entry for entry in entries
               if entry.get("kind") == "run" and entry.get("counted") is True]
    return {"runs": len(counted),
            "cost_usd": sum(float(entry.get("cost_usd") or 0.0) for entry in counted)}


def prepare_report_root(repo: Path, policy: dict, override: str | None) -> Path:
    configured = Path(override) if override else Path(policy["reports"]["directory"])
    root = Path(os.path.normpath(configured if configured.is_absolute() else repo / configured))
    try:
        relative = root.relative_to(repo)
    except ValueError as error:
        raise Denial("report root must stay inside the checkout") from error
    if relative.as_posix() == ".":
        raise Denial("report root must not be the checkout toplevel")
    ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--",
                              relative.as_posix()], capture_output=True, check=False)
    if ignored.returncode != 0:
        raise Denial(
            f"report root {relative.as_posix()} is not git-ignored; reports must stay private "
            "outside tracked product data")
    existed = root.exists()
    if existed and not root.is_dir():
        raise Denial(f"report root is not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if not existed:
        root.chmod(0o700)
    return root


def resolve_provider_command(value: str) -> str:
    if "/" in value:
        return str(Path(value).expanduser().resolve())
    return value


def provider_environment(repo: Path, raw_dir: Path, policy: dict,
                         egress_approved: bool) -> list[tuple[str, str]]:
    return [
        ("CODEX_SECURITY_SOURCE_ROOT", str(repo)),
        ("CODEX_SECURITY_OUTPUT_DIR", str(raw_dir)),
        ("CODEX_SECURITY_MAX_CALLS", str(policy["budgets"]["max_calls_per_run"])),
        ("CODEX_SECURITY_EGRESS_ALLOWLIST", ",".join(policy["provider"]["endpoint_allowlist"])),
        ("CODEX_SECURITY_EGRESS_APPROVED", "1" if egress_approved else "0"),
    ]


def provider_argv(policy: dict, raw_dir: Path) -> list[str]:
    arguments = []
    for argument in policy["provider"]["args"]:
        arguments.append(argument.replace("{output_dir}", str(raw_dir)))
    return arguments


def write_run_report(run_dir: Path, report: dict) -> str:
    report["report_digest"] = sha256_bytes(canonical_bytes(report))
    atomic_write(run_dir / "report.json", canonical_bytes(report) + b"\n")
    return report["report_digest"]


def artifact_record(run_dir: Path, name: str) -> dict:
    return {"path": name, "sha256": sha256_file(run_dir / name)}


def refuse(root: Path, reason: str, *, config_digest: str, commit: str,
           exit_code: int = EXIT_STOPPED, stderr_status: str = STATUS_STOPPED) -> int:
    if root is not None:
        append_ledger(root, {
            "schema_version": LEDGER_SCHEMA_VERSION, "kind": "refused", "counted": False,
            "status": STATUS_STOPPED, "reason": reason, "at": utc_timestamp(),
            "config_digest": config_digest, "source_commit": commit,
        })
    print(json.dumps({"status": stderr_status, "refused": reason}, sort_keys=True), file=sys.stderr)
    return exit_code


def _write_sanitized_artifacts(run_dir: Path, repo: Path, policy: dict,
                               sarif: dict | None, receipt: dict | None) -> tuple[dict, int]:
    artifacts: dict = {"sarif": None, "coverage_receipt": None}
    findings = 0
    if sarif is not None:
        document, findings = sanitize_document(sarif, repo, policy)
        atomic_write(run_dir / "findings.sarif", canonical_bytes(document) + b"\n")
        artifacts["sarif"] = artifact_record(run_dir, "findings.sarif")
    if receipt is not None:
        document, _count = sanitize_document(receipt, repo, policy)
        atomic_write(run_dir / "coverage-receipt.json", canonical_bytes(document) + b"\n")
        artifacts["coverage_receipt"] = artifact_record(run_dir, "coverage-receipt.json")
    return artifacts, findings


def execute_run(args: argparse.Namespace) -> int:
    repo, commit = resolve_checkout(Path(args.source_root))
    policy_path = Path(args.policy)
    policy_path = policy_path if policy_path.is_absolute() else repo / policy_path
    policy = load_policy(policy_path)
    config_digest = sha256_bytes(canonical_bytes(policy))
    if not args.approve:
        raise Denial("run requires --approve to opt this checkout into a provider source review")
    report_root = prepare_report_root(repo, policy, args.report_root)
    state = budget_state(read_ledger(report_root))
    budgets = policy["budgets"]
    if state["runs"] >= budgets["max_runs"]:
        return refuse(report_root, "budget:max_runs", config_digest=config_digest, commit=commit)
    if state["cost_usd"] >= budgets["max_cost_usd"]:
        return refuse(report_root, "budget:max_cost_usd", config_digest=config_digest,
                      commit=commit)
    inventory, source_digest, excluded = source_inventory(repo, policy)
    program = resolve_sandbox_program(args.sandbox_program)
    probe_ok, probe_detail = sandbox_probe(program)
    if not probe_ok:
        print(json.dumps({"status": "preflight-failed", "stage": "sandbox-probe",
                          "detail": probe_detail}, sort_keys=True), file=sys.stderr)
        return refuse(report_root, f"sandbox-preflight: {probe_detail}",
                      config_digest=config_digest, commit=commit,
                      exit_code=EXIT_DENIED, stderr_status="preflight-failed")
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    run_dir = report_root / run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    egress_approved = bool(args.egress_approve)
    masked_files, masked_dirs = mask_targets(repo, policy)
    command = resolve_provider_command(args.provider_command)
    env_pairs = provider_environment(repo, raw_dir, policy, egress_approved)
    api_key = os.environ.get("CODEX_SECURITY_PROVIDER_API_KEY")
    if egress_approved and api_key:
        env_pairs.append(("CODEX_SECURITY_PROVIDER_API_KEY", api_key))
    argv = sandbox_argv(program, repo, raw_dir, [command, *provider_argv(policy, raw_dir)],
                        policy, env_pairs, egress_approved, masked_files, masked_dirs)
    started = utc_timestamp()
    monotonic_start = time.monotonic()
    controller = StopController()
    relayed = install_signal_relay(controller)
    max_seconds = budgets["max_seconds"]
    if args.max_seconds is not None:
        if args.max_seconds <= 0:
            raise Denial("--max-seconds must be a positive integer")
        max_seconds = min(max_seconds, args.max_seconds)
    watchdog = threading.Timer(float(max_seconds), controller.trigger, args=("budget:max_seconds",))
    watchdog.daemon = True
    stdout_log = (raw_dir / "provider-stdout.log").open("wb")
    stderr_log = (raw_dir / "provider-stderr.log").open("wb")
    interrupted = False
    returncode: int | None = None
    try:
        watchdog.start()
        process = subprocess.Popen(argv, stdout=stdout_log, stderr=stderr_log, stdin=subprocess.DEVNULL,
                                   cwd=str(repo), start_new_session=True)
        returncode, interrupted = wait_for_provider(process, controller)
    finally:
        watchdog.cancel()
        stdout_log.close()
        stderr_log.close()
        restore_signal_handlers(relayed)
    finished = utc_timestamp()
    elapsed = round(time.monotonic() - monotonic_start, 3)
    calls_used = 0
    cost_used = 0.0
    reported_version = ""
    sarif_document: dict | None = None
    receipt_document: dict | None = None
    result_error = ""
    try:
        result, sarif_document, receipt_document = read_result(raw_dir, policy)
        calls_used = int(result["calls_used"])
        cost_used = float(result["cost_usd"])
        reported_version = result["provider"]["version"]
        provider_status = result["status"]
    except ResultError as error:
        provider_status = STATUS_ERROR
        result_error = str(error)
    if interrupted:
        status = STATUS_INTERRUPTED
    elif returncode not in (0, None):
        status = STATUS_ERROR
    elif provider_status == STATUS_ERROR:
        status = STATUS_ERROR
    elif provider_status == STATUS_INTERRUPTED:
        status = STATUS_INTERRUPTED
    else:
        status = provider_status
    artifacts: dict = {"sarif": None, "coverage_receipt": None}
    findings = 0
    error_detail = result_error
    if status in (STATUS_COMPLETE, STATUS_PARTIAL):
        over_calls = calls_used > budgets["max_calls_per_run"]
        over_cost = state["cost_usd"] + cost_used > budgets["max_cost_usd"]
        if over_calls or over_cost:
            status = STATUS_STOPPED
            error_detail = "budget:max_calls_per_run" if over_calls else "budget:max_cost_usd"
        else:
            try:
                artifacts, findings = _write_sanitized_artifacts(
                    run_dir, repo, policy, sarif_document, receipt_document)
            except SanitizationError as error:
                status = STATUS_ERROR
                error_detail = f"sanitization: {error}"
                artifacts = {"sarif": None, "coverage_receipt": None}
                findings = 0
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "policy_path": str(policy_path),
        "config_digest": config_digest,
        "provider": {
            "tool": policy["provider"]["tool"],
            "command": command,
            "reviewed_commit": REVIEWED_PROVIDER_COMMIT,
            "reported_version": reported_version,
        },
        "source": {
            "root": str(repo), "commit": commit, "digest": source_digest,
            "files": len(inventory), "bytes": sum(item["size"] for item in inventory),
        },
        "scope": {
            "include_roots": policy["scope"]["include_roots"], "excluded": excluded,
            "masked_files": masked_files, "masked_dirs": masked_dirs,
        },
        "budgets": {
            "limits": {key: budgets[key] for key in
                       ("max_runs", "max_calls_per_run", "max_cost_usd", "max_seconds")},
            "used": {"runs": state["runs"] + 1, "calls": calls_used,
                     "cost_usd": cost_used, "seconds": elapsed},
        },
        "egress": {"approved": egress_approved,
                   "allowlist": policy["provider"]["endpoint_allowlist"]},
        "sandbox": {"program": str(program), "probe": "ok", "network_shared": egress_approved},
        "interrupt_reason": controller.reason or None,
        "error_detail": error_detail or None,
        "artifacts": {**artifacts, "raw_dir": "raw"},
        "findings_count": findings,
        "sanitized": True,
        "started": started,
        "finished": finished,
    }
    report_digest = write_run_report(run_dir, report)
    append_ledger(report_root, {
        "schema_version": LEDGER_SCHEMA_VERSION, "kind": "run", "counted": True,
        "run_id": run_id, "status": status, "reason": controller.reason or error_detail or None,
        "started": started, "finished": finished, "calls_used": calls_used,
        "cost_usd": cost_used, "config_digest": config_digest, "source_commit": commit,
        "report_digest": report_digest,
    })
    print(json.dumps({
        "status": status, "run_id": run_id, "run_dir": str(run_dir),
        "config_digest": config_digest, "source_digest": source_digest,
        "findings_count": findings, "egress_approved": egress_approved,
        "interrupt_reason": controller.reason or None,
    }, sort_keys=True))
    return {STATUS_COMPLETE: EXIT_OK, STATUS_PARTIAL: EXIT_OK, STATUS_STOPPED: EXIT_STOPPED,
            STATUS_INTERRUPTED: EXIT_INTERRUPTED, STATUS_ERROR: EXIT_ERROR}[status]


def execute_plan(args: argparse.Namespace) -> int:
    repo, commit = resolve_checkout(Path(args.source_root))
    policy_path = Path(args.policy)
    policy_path = policy_path if policy_path.is_absolute() else repo / policy_path
    policy = load_policy(policy_path)
    inventory, source_digest, excluded = source_inventory(repo, policy)
    print(json.dumps({
        "plan": policy.get("policy", "codex-security-report-only-review"),
        "source_root": str(repo), "commit": commit,
        "policy_path": str(policy_path), "config_digest": sha256_bytes(canonical_bytes(policy)),
        "source_digest": source_digest, "files": len(inventory),
        "bytes": sum(item["size"] for item in inventory), "excluded": excluded,
        "budgets": policy["budgets"], "provider": policy["provider"],
        "provider_command": resolve_provider_command(args.provider_command),
        "sandbox_program": args.sandbox_program, "report_root": str(repo / policy["reports"]["directory"]),
        "preview": "disabled", "egress": {"approved": False,
                                          "allowlist": policy["provider"]["endpoint_allowlist"]},
    }, sort_keys=True))
    return EXIT_OK


def verify_run(args: argparse.Namespace) -> int:
    repo, commit = resolve_checkout(Path(args.source_root))
    policy_path = Path(args.policy)
    policy_path = policy_path if policy_path.is_absolute() else repo / policy_path
    policy = load_policy(policy_path)
    config_digest = sha256_bytes(canonical_bytes(policy))
    run_dir = Path(args.run_dir)
    run_dir = run_dir if run_dir.is_absolute() else Path(os.path.abspath(run_dir))
    report_path = run_dir / "report.json"
    reasons: list[str] = []
    report: dict = {}
    recorded: str | None = None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        reasons.append(f"report unreadable at {report_path}: {error}")
    if report:
        recorded = report.pop("report_digest", None)
        if recorded != sha256_bytes(canonical_bytes(report)):
            reasons.append("report_digest mismatch: report was modified after the run")
        if report.get("config_digest") != config_digest:
            reasons.append("policy changed since the run (config_digest mismatch)")
        if report.get("provider", {}).get("reviewed_commit") != REVIEWED_PROVIDER_COMMIT:
            reasons.append("report does not bind the reviewed provider commit")
        if report.get("source", {}).get("commit") != commit:
            reasons.append("checkout is no longer at the reviewed source commit")
        for label, key in (("sarif", "sarif"), ("coverage receipt", "coverage_receipt")):
            record = report.get("artifacts", {}).get(key)
            if record is None:
                continue
            artifact = run_dir / record["path"]
            if not artifact.exists() or sha256_file(artifact) != record["sha256"]:
                reasons.append(f"sanitized {label} artifact missing or modified")
        raw_dir = run_dir / report.get("artifacts", {}).get("raw_dir", "raw")
        if not raw_dir.is_dir():
            reasons.append("raw provider evidence directory is missing")
    if not reasons:
        try:
            _inventory, source_digest, _excluded = source_inventory(repo, policy)
        except Denial as error:
            reasons.append(f"source inventory failed: {error}")
        else:
            if report.get("source", {}).get("digest") != source_digest:
                reasons.append("source content changed since the run (source_digest mismatch)")
    if not reasons:
        root = prepare_report_root(repo, policy, args.report_root)
        entry = next((item for item in read_ledger(root)
                      if item.get("run_id") == report.get("run_id")), None)
        if entry is None or entry.get("report_digest") != recorded \
                or entry.get("status") != report.get("status"):
            reasons.append("budget ledger has no matching run receipt")
    print(json.dumps({"verified": not reasons, "run_dir": str(run_dir), "reasons": reasons},
                     sort_keys=True))
    return EXIT_OK if not reasons else EXIT_VERIFY_FAILED


def refuse_preview(_args: argparse.Namespace) -> int:
    print("preview is disabled: Codex Security findings must never be exposed over an "
          "unauthenticated server; use the private CLI report files.", file=sys.stderr)
    return EXIT_DENIED


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source-root", default=".",
                        help="approved checkout toplevel to review (default: current directory)")
    common.add_argument("--policy", default=str(DEFAULT_POLICY),
                        help="review policy JSON, resolved against the checkout toplevel")
    common.add_argument("--provider-command", default="codex-security",
                        help="provider CLI; use the absolute path of the isolated venv binary")
    common.add_argument("--sandbox-program", default="bwrap",
                        help="Bubblewrap executable used for the mandatory sandbox")
    parser = argparse.ArgumentParser(
        prog="codex_security_review",
        description="Report-only isolated Codex Security review wrapper (CLI only; "
                    "no preview server, no CI gate).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan", parents=[common],
                          help="print the resolved scope, digests and budgets; no egress")
    run_parser = subparsers.add_parser("run", parents=[common],
                                       help="execute one sandboxed review run")
    run_parser.add_argument("--approve", action="store_true",
                            help="explicitly opt the checkout into this provider review")
    run_parser.add_argument("--egress-approve", action="store_true",
                            help="approve provider egress to the policy endpoint allowlist")
    run_parser.add_argument("--report-root", default=None,
                            help="override the private report root (must be git-ignored)")
    run_parser.add_argument("--max-seconds", type=int, default=None,
                            help="lower the wall-clock cap for this run only")
    verify_parser = subparsers.add_parser(
        "verify", parents=[common],
        help="re-verify a run report against the exact source and policy")
    verify_parser.add_argument("--run-dir", required=True, help="run directory to verify")
    verify_parser.add_argument("--report-root", default=None,
                               help="report root override used when the run was executed")
    subparsers.add_parser("preview",
                          help="refuses: the preview server surface is disabled")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"plan": execute_plan, "run": execute_run, "verify": verify_run,
                "preview": refuse_preview}
    try:
        return handlers[args.command](args)
    except Denial as error:
        print(json.dumps({"status": "denied", "reason": str(error)}, sort_keys=True),
              file=sys.stderr)
        return EXIT_DENIED


if __name__ == "__main__":
    sys.exit(main())
