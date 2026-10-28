"""Run the pinned Codex Security synthetic profile on a filtered source snapshot.
Private reports bind source, policy and an explicitly approved runtime artifact; paid scans remain unavailable.
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
import stat
import tempfile
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

REVIEWED_PROVIDER_COMMIT = "f894f1ff64b1a332001f48593c1622b85701f9f3"
DEFAULT_POLICY = Path("tooling/codex-security/review-policy.json")
RUNTIME_LOCK = (
    Path(__file__).resolve().parents[2] / "tooling/codex-security/runtime-lock.json"
)
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
RUN_STATUSES = (
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    STATUS_INTERRUPTED,
    STATUS_STOPPED,
    STATUS_ERROR,
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PATH_VALUE_KEYS = frozenset({"uri", "path", "file"})
SNIPPET_KEYS = ("snippet", "contextRegion", "code", "codeEvidence")
POLL_SECONDS = 0.25
KILL_GRACE_SECONDS = 5


class Denial(RuntimeError):
    pass


class ResultError(RuntimeError):
    pass


class SanitizationError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


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
    environment = {
        **host_environment(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            *arguments,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise Denial(
            f"git {arguments[0]} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def resolve_checkout(source_root: Path) -> tuple[Path, str]:
    root = source_root.resolve()
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


def _require_string_list(
    container: dict, key: str, *, allow_empty: bool = False
) -> None:
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
    if (
        not isinstance(reviewed, dict)
        or not isinstance(reviewed.get("repository"), str)
        or reviewed.get("commit") != REVIEWED_PROVIDER_COMMIT
        or not isinstance(reviewed.get("reviewed_on"), str)
    ):
        raise Denial(
            f"policy provider.reviewed_source must pin commit {REVIEWED_PROVIDER_COMMIT}"
        )
    if (
        provider.get("profile") != "upstream-mock"
        or provider.get("model") != "gpt-5.6-sol"
    ):
        raise Denial("only the pinned upstream-mock model profile is available")
    if provider.get("endpoint_allowlist") != []:
        raise Denial(
            "network/paid profiles are unavailable; endpoint_allowlist must be empty"
        )
    if policy.get("sandbox") != {"program": "bwrap"}:
        raise Denial(
            "policy sandbox.program must be bwrap with no host mount overrides"
        )
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
    for key in ("max_runs", "max_seconds", "max_artifact_bytes"):
        if not isinstance(budgets.get(key), int) or budgets[key] <= 0:
            raise Denial(f"policy budgets.{key} must be a positive integer")
    if (
        budgets.get("max_calls_per_run") != 0
        or budgets.get("max_cost_usd") != 0
        or isinstance(budgets.get("max_calls_per_run"), bool)
        or isinstance(budgets.get("max_cost_usd"), bool)
    ):
        raise Denial(
            "the offline profile requires zero call and cost admission budgets"
        )
    reports = policy.get("reports")
    if not isinstance(reports, dict) or not isinstance(reports.get("directory"), str):
        raise Denial("policy reports.directory must be a string")
    if reports.get("require_untracked") is not True:
        raise Denial("policy reports.require_untracked must be true")
    if policy.get("preview") != {"enabled": False}:
        raise Denial('policy preview must be exactly {"enabled": false}')


def excluded_reason(relative: str, name: str, policy: dict) -> str | None:
    scope = policy["scope"]
    for base in scope["exclude_paths"]:
        if relative == base or relative.startswith(base + "/"):
            return f"path:{base}"
    for pattern in scope["exclude_names"]:
        if fnmatch.fnmatch(name, pattern):
            return f"name:{pattern}"
    return None


def read_source_file(repo: Path, relative: str, limit: int) -> bytes:
    parts = relative.split("/")
    if any(part in ("", ".", "..", ".git") for part in parts) or "\\" in relative:
        raise Denial(f"unsafe source path: {relative}")
    descriptor = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        child = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(child, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise Denial(f"source must be a singly linked regular file: {relative}")
            if metadata.st_size > limit:
                raise Denial(f"scope exceeds max_bytes budget: {relative}")
            data = handle.read(limit + 1)
            if len(data) > limit:
                raise Denial(f"scope exceeds max_bytes budget: {relative}")
            return data
    except OSError as error:
        raise Denial(
            f"inventory file unreadable or symlink: {relative}: {error}"
        ) from error
    finally:
        os.close(descriptor)


def source_inventory(repo: Path, policy: dict) -> tuple[list[dict], str, dict]:
    scope = policy["scope"]
    listed = run_git(repo, "ls-files", "-s", "-z", "--", *scope["include_roots"])
    entries: list[dict] = []
    excluded_by_reason: dict[str, int] = {}
    total_bytes = 0
    for record in listed.split("\0"):
        if not record:
            continue
        metadata, relative = record.split("\t", 1)
        mode, _object, stage = metadata.split()
        if stage != "0":
            raise Denial("source index has unresolved merges")
        if mode == "160000":
            continue
        reason = excluded_reason(relative, relative.rsplit("/", 1)[-1], policy)
        if reason is not None:
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
            continue
        if mode not in ("100644", "100755"):
            raise Denial(f"source must be a regular file, never a symlink: {relative}")
        data = read_source_file(repo, relative, scope["max_bytes"] - total_bytes)
        total_bytes += len(data)
        entries.append(
            {"path": relative, "size": len(data), "sha256": sha256_bytes(data)}
        )
        if len(entries) > scope["max_files"]:
            raise Denial("scope exceeds max_files budget")
    entries.sort(key=lambda item: item["path"])
    digest = sha256_bytes(canonical_bytes(entries))
    return (
        entries,
        digest,
        {"files": sum(excluded_by_reason.values()), "by_reason": excluded_by_reason},
    )


def materialize_snapshot(repo: Path, destination: Path, inventory: list[dict]) -> None:
    destination.mkdir(mode=0o700)
    for entry in inventory:
        data = read_source_file(repo, entry["path"], entry["size"])
        if len(data) != entry["size"] or sha256_bytes(data) != entry["sha256"]:
            raise Denial("source changed while materializing the approved snapshot")
        target = destination / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(0o400)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for base, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted([*directories, *files]):
            path = Path(base) / name
            metadata = path.lstat()
            record = {
                "path": path.relative_to(root).as_posix(),
                "mode": stat.S_IMODE(metadata.st_mode),
            }
            if path.is_symlink():
                record["link"] = os.readlink(path)
            elif path.is_file():
                record["sha256"] = sha256_file(path)
            elif not path.is_dir():
                raise Denial("runtime artifact contains a special file")
            digest.update(canonical_bytes(record) + b"\n")
    return digest.hexdigest()


def verify_runtime(root: Path, approved_digest: str, repo: Path) -> dict:
    root = root.resolve()
    if (
        not root.is_dir()
        or root == Path("/")
        or repo.is_relative_to(root)
        or root.is_relative_to(repo)
    ):
        raise Denial("runtime must be a separate dedicated root filesystem")
    if re.fullmatch(r"[0-9a-f]{64}", approved_digest or "") is None:
        raise Denial("--runtime-sha256 must approve an exact runtime artifact")
    try:
        lock = json.loads(RUNTIME_LOCK.read_text())
        if json.loads((root / "runtime-lock.json").read_text()) != lock:
            raise Denial(
                "runtime dependency lock does not match the reviewed installation"
            )
        for relative, expected in lock["files"].items():
            if (
                sha256_bytes(read_source_file(root, relative, 16 * 1024 * 1024))
                != expected
            ):
                raise Denial(f"runtime reviewed file mismatch: {relative}")
        for executable in (
            "usr/local/bin/node",
            "usr/bin/python3",
            "usr/bin/git",
            "usr/bin/true",
        ):
            if not (root / executable).is_file():
                raise Denial(f"runtime executable missing: {executable}")
        actual = tree_digest(root)
    except (OSError, ValueError, KeyError) as error:
        raise Denial(f"runtime artifact unreadable: {error}") from error
    if actual != approved_digest:
        raise Denial("runtime artifact digest differs from --runtime-sha256 approval")
    return {
        "sha256": actual,
        "lock_sha256": sha256_bytes(canonical_bytes(lock)),
        "source_commit": REVIEWED_PROVIDER_COMMIT,
        "version": lock["scanner_version"],
    }


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


def sandbox_argv(
    program: Path,
    runtime: Path,
    snapshot: Path,
    raw_dir: Path,
    command: list[str],
    env_pairs: list[tuple[str, str]],
) -> list[str]:
    argv = [
        str(program),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        str(runtime),
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/root",
        "--tmpfs",
        "/run",
        "--ro-bind",
        str(snapshot),
        "/source",
        "--bind",
        str(raw_dir),
        "/output",
        "--chdir",
        "/source",
    ]
    for key, value in env_pairs:
        argv += ["--setenv", key, value]
    return [*argv, "--", *command]


def host_environment() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def sandbox_probe(program: Path, runtime: Path) -> tuple[bool, str]:
    argv = [
        str(program),
        "--ro-bind",
        str(runtime),
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--unshare-all",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--",
        "/usr/bin/true",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            env=host_environment(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"sandbox probe failed to execute: {error}"
    return (
        (True, "")
        if result.returncode == 0
        else (
            False,
            (result.stderr or result.stdout).strip()
            or f"sandbox probe exited {result.returncode}",
        )
    )


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
            prior = signal.getsignal(number)
            signal.signal(number, handler)
            previous.append((number, prior))
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


def wait_for_provider(
    process: subprocess.Popen, controller: StopController
) -> tuple[int | None, bool]:
    while True:
        try:
            return process.wait(timeout=POLL_SECONDS), False
        except subprocess.TimeoutExpired:
            pass
        if controller.event.is_set():
            terminate_provider(process)
            return None, True


def _load_artifact(raw_dir: Path, name: str, limit: int, label: str) -> dict:
    try:
        data = read_source_file(raw_dir, name, limit)
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ResultError(f"provider {label} must be a JSON object")
        return result
    except (Denial, OSError, ValueError) as error:
        raise ResultError(
            f"provider {label} missing, unreadable or exceeds artifact limit: {error}"
        ) from error


def read_result(raw_dir: Path, policy: dict) -> tuple[dict, dict, dict]:
    limit = policy["budgets"]["max_artifact_bytes"]
    manifest = _load_artifact(raw_dir, "scan-manifest.json", limit, "scan manifest")
    findings = _load_artifact(raw_dir, "findings.json", limit, "findings")
    receipt = _load_artifact(raw_dir, "coverage.json", limit, "coverage")
    sarif = _load_artifact(raw_dir, "exports/results.sarif", limit, "SARIF")
    scan = manifest.get("scan", {})
    if (
        manifest.get("documentType") != "codex-security.scan-manifest"
        or manifest.get("schemaVersion") != "1.0"
        or not isinstance(scan, dict)
        or not isinstance(scan.get("extensions"), dict)
        or not isinstance(scan.get("scope"), dict)
        or not isinstance(scan.get("artifacts"), list)
        or scan.get("extensions", {}).get("mock") is not True
        or scan.get("scope", {}).get("runtimeStatus") != "mock"
        or scan.get("producer")
        != {"name": "codex-security-plugin", "version": "0.1.95"}
    ):
        raise ResultError(
            "upstream manifest does not identify the pinned synthetic profile"
        )
    for document, kind in ((findings, "findings"), (receipt, "coverage")):
        if (
            document.get("documentType") != f"codex-security.{kind}"
            or document.get("schemaVersion") != "1.0"
            or not scan.get("id")
            or document.get("scanId") != scan["id"]
        ):
            raise ResultError(f"upstream {kind} identity/schema mismatch")
    if scan.get("status") not in ("completed", "failed", "interrupted"):
        raise ResultError("upstream scan status is invalid")
    if receipt.get("completeness") not in ("complete", "partial", "unknown"):
        raise ResultError("upstream coverage completeness is invalid")
    if not isinstance(findings.get("findings"), list) or not isinstance(
        sarif.get("runs"), list
    ):
        raise ResultError("upstream findings/SARIF shape is invalid")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("extensions"), dict)
        or item["extensions"].get("mock") is not True
        for item in findings["findings"]
    ):
        raise ResultError("upstream findings must be explicitly synthetic")
    records = {
        item.get("path"): item.get("sha256")
        for item in scan.get("artifacts", [])
        if isinstance(item, dict)
    }
    for name in ("findings.json", "coverage.json"):
        if records.get(name) != sha256_file(raw_dir / name):
            raise ResultError(f"upstream manifest artifact digest mismatch: {name}")
    status = {"failed": STATUS_ERROR, "interrupted": STATUS_INTERRUPTED}.get(
        scan["status"],
        STATUS_COMPLETE if receipt["completeness"] == "complete" else STATUS_PARTIAL,
    )
    return (
        {"status": status, "calls_used": 0, "cost_usd": 0.0, "synthetic": True},
        sarif,
        receipt,
    )


def relative_uri(value: str, repo: Path) -> str:
    text = unquote(value[len("file://") :] if value.startswith("file://") else value)
    prefix = str(repo)
    if text == prefix or text == prefix + "/":
        return "."
    if text.startswith(prefix + "/"):
        text = text[len(prefix) + 1 :]
    if (
        text.startswith("/")
        or "\\" in text
        or ":" in text
        or "\0" in text
        or any(part == ".." for part in text.split("/"))
    ):
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
                    reason = excluded_reason(
                        relative, relative.rsplit("/", 1)[-1], policy
                    )
                    if reason is not None:
                        violations.append(
                            f"finding references excluded path {relative} ({reason})"
                        )
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
    for run in (
        document.get("runs", []) if isinstance(document.get("runs"), list) else []
    ):
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
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != LEDGER_SCHEMA_VERSION
        or not isinstance(document.get("entries"), list)
    ):
        raise Denial(f"budget ledger has an unexpected schema at {path}")
    return document["entries"]


def append_ledger(root: Path, entry: dict) -> None:
    path = root / "ledger.json"
    with open(root / "ledger.lock", "a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        existing = read_ledger(root)
        document = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "entries": [*existing, entry],
        }
        atomic_write(path, canonical_bytes(document) + b"\n")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def budget_state(entries: list[dict]) -> dict:
    counted = [
        entry
        for entry in entries
        if entry.get("kind") == "run" and entry.get("counted") is True
    ]
    return {
        "runs": len(counted),
        "cost_usd": sum(float(entry.get("cost_usd") or 0.0) for entry in counted),
    }


def prepare_report_root(repo: Path, policy: dict, override: str | None) -> Path:
    configured = Path(override) if override else Path(policy["reports"]["directory"])
    root = Path(
        os.path.normpath(configured if configured.is_absolute() else repo / configured)
    )
    try:
        relative = root.relative_to(repo)
    except ValueError as error:
        raise Denial("report root must stay inside the checkout") from error
    if relative.as_posix() == ".":
        raise Denial("report root must not be the checkout toplevel")
    current = repo
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise Denial("report root must not traverse symlinks")
    ignored = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo),
            "check-ignore",
            "-q",
            "--",
            relative.as_posix(),
        ],
        env=host_environment(),
        capture_output=True,
        check=False,
    )
    if ignored.returncode != 0:
        raise Denial(
            f"report root {relative.as_posix()} is not git-ignored; reports must stay private "
            "outside tracked product data"
        )
    existed = root.exists()
    if existed and not root.is_dir():
        raise Denial(f"report root is not a directory: {root}")
    if existed and (root.stat().st_mode & 0o077 or root.stat().st_uid != os.geteuid()):
        raise Denial("report root must be owner-private (chmod 700)")
    root.mkdir(parents=True, exist_ok=True)
    if not existed:
        root.chmod(0o700)
    return root


def provider_environment() -> list[tuple[str, str]]:
    return [
        ("PATH", "/usr/local/bin:/usr/bin:/bin"),
        ("HOME", "/tmp/home"),
        ("TMPDIR", "/tmp"),
        ("CODEX_HOME", "/tmp/codex-home"),
        ("CODEX_SECURITY_STATE_DIR", "/output/state"),
        ("PYTHON", "/usr/bin/python3"),
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
        ("GIT_TERMINAL_PROMPT", "0"),
        ("LANG", "C.UTF-8"),
    ]


def provider_argv() -> list[str]:
    return [
        "/usr/local/bin/node",
        "/scanner/sdk/typescript/bin/codex-security.mjs",
        "scan",
        "/source",
        "--mock",
        "--headless",
        "--format",
        "json",
        "--model",
        "gpt-5.6-sol",
        "--plugin-path",
        "/scanner/plugins/codex-security",
        "--output-dir",
        "/output/scan",
    ]


def write_run_report(run_dir: Path, report: dict) -> str:
    report["report_digest"] = sha256_bytes(canonical_bytes(report))
    atomic_write(run_dir / "report.json", canonical_bytes(report) + b"\n")
    return report["report_digest"]


def artifact_record(run_dir: Path, name: str) -> dict:
    return {"path": name, "sha256": sha256_file(run_dir / name)}


def refuse(
    root: Path,
    reason: str,
    *,
    config_digest: str,
    commit: str,
    exit_code: int = EXIT_STOPPED,
    stderr_status: str = STATUS_STOPPED,
) -> int:
    if root is not None:
        append_ledger(
            root,
            {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "kind": "refused",
                "counted": False,
                "status": STATUS_STOPPED,
                "reason": reason,
                "at": utc_timestamp(),
                "config_digest": config_digest,
                "source_commit": commit,
            },
        )
    print(
        json.dumps({"status": stderr_status, "refused": reason}, sort_keys=True),
        file=sys.stderr,
    )
    return exit_code


def _write_sanitized_artifacts(
    run_dir: Path, repo: Path, policy: dict, sarif: dict | None, receipt: dict | None
) -> tuple[dict, int]:
    artifacts: dict = {"sarif": None, "coverage_receipt": None}
    findings = 0
    if sarif is not None:
        document, findings = sanitize_document(sarif, repo, policy)
        atomic_write(run_dir / "findings.sarif", canonical_bytes(document) + b"\n")
        artifacts["sarif"] = artifact_record(run_dir, "findings.sarif")
    if receipt is not None:
        document, _count = sanitize_document(receipt, repo, policy)
        atomic_write(
            run_dir / "coverage-receipt.json", canonical_bytes(document) + b"\n"
        )
        artifacts["coverage_receipt"] = artifact_record(
            run_dir, "coverage-receipt.json"
        )
    return artifacts, findings


def execute_run(args: argparse.Namespace) -> int:
    repo, commit = resolve_checkout(Path(args.source_root))
    policy_path = Path(args.policy)
    policy_path = policy_path if policy_path.is_absolute() else repo / policy_path
    policy = load_policy(policy_path)
    config_digest = sha256_bytes(canonical_bytes(policy))
    if not args.approve:
        raise Denial(
            "run requires --approve to opt this checkout into a synthetic workflow test"
        )
    if args.egress_approve:
        raise Denial(
            "paid/egress profiles are unavailable: upstream has no hard call/cost admission"
        )
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise Denial("--max-seconds must be a positive integer")
    runtime = Path(args.runtime_root or "").resolve()
    if not args.runtime_root:
        raise Denial("--runtime-root and --runtime-sha256 are required")
    identity = verify_runtime(runtime, args.runtime_sha256, repo)
    inventory, source_digest, excluded = source_inventory(repo, policy)
    program = resolve_sandbox_program(args.sandbox_program)
    root = prepare_report_root(repo, policy, args.report_root)
    with open(root / "admission.lock", "a+b") as admission:
        try:
            fcntl.flock(admission.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Denial("another run holds budget admission") from error
        state = budget_state(read_ledger(root))
        if state["runs"] >= policy["budgets"]["max_runs"]:
            return refuse(
                root, "budget:max_runs", config_digest=config_digest, commit=commit
            )
        probe_ok, detail = sandbox_probe(program, runtime)
        if not probe_ok:
            return refuse(
                root,
                f"sandbox-preflight: {detail}",
                config_digest=config_digest,
                commit=commit,
                exit_code=EXIT_DENIED,
                stderr_status="preflight-failed",
            )
        with tempfile.TemporaryDirectory(prefix="codex-security-source-") as temporary:
            snapshot = Path(temporary) / "source"
            materialize_snapshot(repo, snapshot, inventory)
            run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
            run_dir = root / run_id
            raw_dir = run_dir / "raw"
            raw_dir.mkdir(parents=True, mode=0o700)
            run_dir.chmod(0o700)
            started = utc_timestamp()
            append_ledger(
                root,
                {
                    "kind": "run",
                    "counted": True,
                    "run_id": run_id,
                    "status": "reserved",
                    "cost_usd": 0,
                    "calls_used": 0,
                    "source_commit": commit,
                    "config_digest": config_digest,
                },
            )
            argv = sandbox_argv(
                program,
                runtime,
                snapshot,
                raw_dir,
                provider_argv(),
                provider_environment(),
            )
            return run_provider(
                args,
                argv,
                repo,
                commit,
                policy,
                identity,
                inventory,
                source_digest,
                excluded,
                config_digest,
                run_dir,
                started,
                state,
            )


def run_provider(
    args: argparse.Namespace,
    argv: list[str],
    repo: Path,
    commit: str,
    policy: dict,
    identity: dict,
    inventory: list[dict],
    source_digest: str,
    excluded: dict,
    config_digest: str,
    run_dir: Path,
    started: str,
    state: dict,
) -> int:
    raw_dir = run_dir / "raw"
    controller = StopController()
    relayed = install_signal_relay(controller)
    budgets = policy["budgets"]
    max_seconds = min(
        budgets["max_seconds"], args.max_seconds or budgets["max_seconds"]
    )
    watchdog = threading.Timer(
        float(max_seconds), controller.trigger, args=("budget:max_seconds",)
    )
    watchdog.daemon = True
    monotonic_start = time.monotonic()
    status = STATUS_ERROR
    detail = ""
    artifacts = {"sarif": None, "coverage_receipt": None}
    findings = 0
    try:
        with (
            (raw_dir / "provider-stdout.log").open("wb") as stdout,
            (raw_dir / "provider-stderr.log").open("wb") as stderr,
        ):
            watchdog.start()
            process = subprocess.Popen(
                argv,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                cwd=str(run_dir),
                env=host_environment(),
                start_new_session=True,
            )
            returncode, interrupted = wait_for_provider(process, controller)
        if interrupted:
            status = STATUS_INTERRUPTED
        elif returncode != 0:
            detail = f"provider exited {returncode}"
        else:
            result, sarif, receipt = read_result(raw_dir / "scan", policy)
            status = result["status"]
            if status in (STATUS_COMPLETE, STATUS_PARTIAL):
                sarif.setdefault("properties", {})["synthetic"] = True
                receipt["synthetic"] = True
                artifacts, findings = _write_sanitized_artifacts(
                    run_dir, Path("/source"), policy, sarif, receipt
                )
    except (OSError, ResultError, SanitizationError) as error:
        status = STATUS_ERROR
        detail = str(error)
    finally:
        watchdog.cancel()
        restore_signal_handlers(relayed)
    for base, _directories, files in os.walk(raw_dir):
        for name in files:
            path = Path(base) / name
            if not path.is_symlink():
                path.chmod(0o600)
    finished = utc_timestamp()
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "status": status,
        "synthetic": True,
        "security_analysis_performed": False,
        "config_digest": config_digest,
        "policy_path": str(Path(args.policy)),
        "provider": {
            "tool": "codex-security",
            "reviewed_commit": REVIEWED_PROVIDER_COMMIT,
            "profile": "upstream-mock",
            "runtime": identity,
        },
        "source": {
            "root": str(repo),
            "commit": commit,
            "digest": source_digest,
            "files": len(inventory),
            "bytes": sum(item["size"] for item in inventory),
        },
        "scope": {
            "include_roots": policy["scope"]["include_roots"],
            "excluded": excluded,
        },
        "budgets": {
            "limits": budgets,
            "admitted": {"model_calls": 0, "cost_usd": 0},
            "used": {
                "runs": state["runs"] + 1,
                "calls": 0,
                "cost_usd": 0,
                "seconds": round(time.monotonic() - monotonic_start, 3),
            },
        },
        "egress": {
            "approved": False,
            "allowlist": [],
            "enforcement": "unshared-network",
        },
        "sandbox": {
            "program": argv[0],
            "probe": "ok",
            "network_shared": False,
            "filesystem": "approved-runtime-and-filtered-snapshot",
        },
        "interrupt_reason": controller.reason or None,
        "error_detail": detail or None,
        "artifacts": {**artifacts, "raw_dir": "raw"},
        "findings_count": findings,
        "sanitized": status in (STATUS_COMPLETE, STATUS_PARTIAL),
        "started": started,
        "finished": finished,
    }
    digest = write_run_report(run_dir, report)
    append_ledger(
        run_dir.parent,
        {
            "kind": "receipt",
            "counted": False,
            "run_id": run_dir.name,
            "status": status,
            "report_digest": digest,
            "calls_used": 0,
            "cost_usd": 0,
        },
    )
    print(
        json.dumps(
            {
                "status": status,
                "synthetic": True,
                "security_analysis_performed": False,
                "run_dir": str(run_dir),
                "source_digest": source_digest,
                "findings_count": findings,
            },
            sort_keys=True,
        )
    )
    return {
        STATUS_COMPLETE: EXIT_OK,
        STATUS_PARTIAL: EXIT_OK,
        STATUS_INTERRUPTED: EXIT_INTERRUPTED,
        STATUS_ERROR: EXIT_ERROR,
    }[status]


def execute_plan(args: argparse.Namespace) -> int:
    repo, commit = resolve_checkout(Path(args.source_root))
    policy_path = Path(args.policy)
    policy_path = policy_path if policy_path.is_absolute() else repo / policy_path
    policy = load_policy(policy_path)
    inventory, source_digest, excluded = source_inventory(repo, policy)
    print(
        json.dumps(
            {
                "plan": policy.get("policy", "codex-security-report-only-review"),
                "source_root": str(repo),
                "commit": commit,
                "policy_path": str(policy_path),
                "config_digest": sha256_bytes(canonical_bytes(policy)),
                "source_digest": source_digest,
                "files": len(inventory),
                "bytes": sum(item["size"] for item in inventory),
                "excluded": excluded,
                "budgets": policy["budgets"],
                "provider": policy["provider"],
                "profile": "upstream-mock",
                "security_analysis_performed": False,
                "sandbox_program": args.sandbox_program,
                "report_root": str(repo / policy["reports"]["directory"]),
                "preview": "disabled",
                "egress": {
                    "approved": False,
                    "allowlist": policy["provider"]["endpoint_allowlist"],
                },
            },
            sort_keys=True,
        )
    )
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
        if (
            not isinstance(report, dict)
            or report.get("schema_version") != REPORT_SCHEMA_VERSION
            or any(
                not isinstance(report.get(key), dict)
                for key in ("provider", "source", "artifacts")
            )
        ):
            reasons.append("report schema is invalid")
            report = {}
    except (OSError, json.JSONDecodeError) as error:
        reasons.append(f"report unreadable at {report_path}: {error}")
    if report:
        recorded = report.pop("report_digest", None)
        if recorded != sha256_bytes(canonical_bytes(report)):
            reasons.append("report_digest mismatch: report was modified after the run")
        if report.get("config_digest") != config_digest:
            reasons.append("policy changed since the run (config_digest mismatch)")
        if (
            report.get("provider", {}).get("reviewed_commit")
            != REVIEWED_PROVIDER_COMMIT
        ):
            reasons.append("report does not bind the reviewed provider commit")
        if report.get("source", {}).get("commit") != commit:
            reasons.append("checkout is no longer at the reviewed source commit")
        for label, key in (
            ("sarif", "sarif"),
            ("coverage receipt", "coverage_receipt"),
        ):
            record = report.get("artifacts", {}).get(key)
            if record is None:
                continue
            name = "findings.sarif" if key == "sarif" else "coverage-receipt.json"
            if not isinstance(record, dict) or record.get("path") != name:
                reasons.append(f"sanitized {label} record is invalid")
                continue
            try:
                actual = sha256_bytes(
                    read_source_file(
                        run_dir, name, policy["budgets"]["max_artifact_bytes"]
                    )
                )
            except (Denial, OSError):
                actual = None
            if actual is None or actual != record.get("sha256"):
                reasons.append(f"sanitized {label} artifact missing or modified")
        if (
            report["artifacts"].get("raw_dir") != "raw"
            or not (run_dir / "raw").is_dir()
        ):
            reasons.append("raw provider evidence directory is missing")
    if not reasons:
        try:
            _inventory, source_digest, _excluded = source_inventory(repo, policy)
        except Denial as error:
            reasons.append(f"source inventory failed: {error}")
        else:
            if report.get("source", {}).get("digest") != source_digest:
                reasons.append(
                    "source content changed since the run (source_digest mismatch)"
                )
    if not reasons:
        root = prepare_report_root(repo, policy, args.report_root)
        entry = next(
            (
                item
                for item in reversed(read_ledger(root))
                if item.get("kind") == "receipt"
                and item.get("run_id") == report.get("run_id")
            ),
            None,
        )
        if (
            entry is None
            or entry.get("report_digest") != recorded
            or entry.get("status") != report.get("status")
        ):
            reasons.append("budget ledger has no matching run receipt")
    print(
        json.dumps(
            {"verified": not reasons, "run_dir": str(run_dir), "reasons": reasons},
            sort_keys=True,
        )
    )
    return EXIT_OK if not reasons else EXIT_VERIFY_FAILED


def refuse_preview(_args: argparse.Namespace) -> int:
    print(
        "preview is disabled: Codex Security findings must never be exposed over an "
        "unauthenticated server; use the private CLI report files.",
        file=sys.stderr,
    )
    return EXIT_DENIED


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--source-root",
        default=".",
        help="approved checkout toplevel to review (default: current directory)",
    )
    common.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY),
        help="review policy JSON, resolved against the checkout toplevel",
    )
    common.add_argument(
        "--runtime-root", help="dedicated rootfs built from the locked runtime recipe"
    )
    common.add_argument("--runtime-sha256", help="approved exact runtime tree digest")
    common.add_argument(
        "--sandbox-program",
        default="bwrap",
        help="Bubblewrap executable used for the mandatory sandbox",
    )
    parser = argparse.ArgumentParser(
        prog="codex_security_review",
        description="Report-only isolated Codex Security review wrapper (CLI only; "
        "no preview server, no CI gate).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "plan",
        parents=[common],
        help="print the resolved scope, digests and budgets; no egress",
    )
    run_parser = subparsers.add_parser(
        "run", parents=[common], help="execute one sandboxed review run"
    )
    run_parser.add_argument(
        "--approve",
        action="store_true",
        help="explicitly opt the checkout into this provider review",
    )
    run_parser.add_argument(
        "--egress-approve",
        action="store_true",
        help="refused: paid/egress profiles have no hard admission boundary",
    )
    run_parser.add_argument(
        "--report-root",
        default=None,
        help="override the private report root (must be git-ignored)",
    )
    run_parser.add_argument(
        "--max-seconds",
        type=int,
        default=None,
        help="lower the wall-clock cap for this run only",
    )
    verify_parser = subparsers.add_parser(
        "verify",
        parents=[common],
        help="re-verify a run report against the exact source and policy",
    )
    verify_parser.add_argument(
        "--run-dir", required=True, help="run directory to verify"
    )
    verify_parser.add_argument(
        "--report-root",
        default=None,
        help="report root override used when the run was executed",
    )
    subparsers.add_parser(
        "preview", help="refuses: the preview server surface is disabled"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "plan": execute_plan,
        "run": execute_run,
        "verify": verify_run,
        "preview": refuse_preview,
    }
    try:
        return handlers[args.command](args)
    except Denial as error:
        print(
            json.dumps({"status": "denied", "reason": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return EXIT_DENIED


if __name__ == "__main__":
    sys.exit(main())
