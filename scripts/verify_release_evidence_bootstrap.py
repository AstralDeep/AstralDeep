#!/usr/bin/env python3
"""Create, verify, and lease-push a fail-closed evidence bootstrap candidate.

The verifier is intentionally loaded from a separate clean checkout of the
provider's current default-branch commit.  It never authorizes merge or release:
it permits one exact, lead-approved, draft-only diagnostic push when canonical
provider evidence cannot exist before the candidate SHA is remotely addressable.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit


SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
RFC3339_UTC_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?Z"
)
APPROVAL_MARKER_RE = re.compile(
    r"<!--\s*astraldeep-release-evidence-bootstrap-v1\s*\n"
    r"(?P<payload>\{.*?\})\s*\n-->",
    re.DOTALL,
)
MAX_WINDOW = timedelta(hours=168)
EVIDENCE_TOOL = "scripts/prepare_release_evidence.py"
EVIDENCE_INPUTS = {
    "backend_evidence": "build/060/release-evidence/backend.json",
    "web_evidence": "build/060/release-evidence/web.json",
    "windows_evidence": "build/060/release-evidence/windows.json",
    "android_evidence": "build/060/release-evidence/android.json",
    "macos_evidence": "build/060/release-evidence/macos.json",
    "ios_evidence": "build/060/release-evidence/ios.json",
    "watchos_evidence": "build/060/release-evidence/watchos.json",
    "docs_evidence": "build/060/release-evidence/docs.json",
    "backend_python": "build/060/coverage/backend.xml",
    "voice_worker_python": "build/065/coverage/voice-worker.xml",
    "tooling_python": "build/060/coverage/tooling-python.xml",
    "windows_python": "build/060/coverage/windows.xml",
    "javascript": "build/060/coverage/web-istanbul.json",
    "android_app": "build/060/coverage/android-app.xml",
    "android_core": "build/060/coverage/android-core.xml",
    "ios": "build/060/coverage/apple-ios-xccov.json",
    "macos": "build/060/coverage/apple-macos-xccov.json",
    "watchos": "build/060/coverage/apple-watchos-xccov.json",
}
PROVIDER_EVIDENCE_INPUTS = {
    "backend_evidence",
    "web_evidence",
    "windows_evidence",
    "android_evidence",
    "macos_evidence",
    "ios_evidence",
    "watchos_evidence",
    "docs_evidence",
}
LOCAL_COVERAGE_BY_HOST = {
    "Darwin": {
        "backend_python",
        "voice_worker_python",
        "tooling_python",
        "javascript",
        "android_app",
        "android_core",
        "ios",
        "macos",
        "watchos",
    },
    "Linux": {
        "backend_python",
        "voice_worker_python",
        "tooling_python",
        "javascript",
        "android_app",
        "android_core",
    },
    "Windows": {
        "backend_python",
        "voice_worker_python",
        "tooling_python",
        "windows_python",
        "javascript",
        "android_app",
        "android_core",
    },
}
REQUIRED_PR_WORKFLOWS = {
    ".github/workflows/ci.yml",
}
INVENTORY_KEYS = {
    "document_type",
    "schema_version",
    "repository",
    "pr_number",
    "feature",
    "branch",
    "base_branch",
    "base_sha",
    "previous_head",
    "candidate_sha",
    "changed_paths",
    "generated_at",
    "status",
    "protected_release_authorization",
    "host_platform",
    "evidence_command",
    "evidence_command_exit_code",
    "evidence_failure",
    "evidence_stdout_sha256",
    "evidence_stderr_sha256",
    "inputs",
    "missing_inputs",
    "provider_bound_missing_inputs",
    "workflow_files",
    "policy",
}
APPROVAL_KEYS = {
    "schema_version",
    "document_type",
    "repository",
    "pr_number",
    "feature",
    "branch",
    "base_branch",
    "base_sha",
    "previous_head",
    "candidate_sha",
    "approved_paths",
    "provider_bound_missing_inputs",
    "inventory_sha256",
    "policy_commit",
    "local_gate_attestation",
    "structural_blocker",
    "purpose",
    "expires_at",
}


class BootstrapError(RuntimeError):
    """A fail-closed bootstrap inventory, verification, or push error."""


@dataclass(frozen=True)
class ProviderState:
    repository: str
    default_branch: str
    default_sha: str
    branch: str
    previous_head: str
    candidate_sha: str
    changed_paths: tuple[str, ...]
    provider_now: datetime
    workflow_files: Mapping[str, str]
    pull: Mapping[str, Any]


@dataclass(frozen=True)
class EvidenceRun:
    returncode: int
    stdout: str
    stderr: str
    failure: Mapping[str, Any] | None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bytes_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or RFC3339_UTC_RE.fullmatch(value) is None:
        raise BootstrapError(f"{field} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BootstrapError(f"{field} is not a valid RFC 3339 timestamp") from exc
    return parsed.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        env=dict(env) if env is not None else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise BootstrapError(f"{args[0]} failed: {detail}")
    return completed


def _git(repo: Path, *args: str) -> str:
    return _run(("git", *args), cwd=repo).stdout.strip()


def _provider_json(policy_root: Path, endpoint: str) -> tuple[Any, datetime]:
    result = _run(
        ("gh", "api", "--hostname", "github.com", "--include", endpoint),
        cwd=policy_root,
    )
    normalized = result.stdout.replace("\r\n", "\n")
    headers, separator, body = normalized.partition("\n\n")
    if not separator:
        raise BootstrapError(f"GitHub API response lacks headers for {endpoint}")
    date_match = re.search(r"(?im)^Date:\s*(?P<value>.+?)\s*$", headers)
    if date_match is None:
        raise BootstrapError(f"GitHub API response lacks provider time for {endpoint}")
    try:
        provider_now = parsedate_to_datetime(date_match.group("value")).astimezone(UTC)
        value = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"GitHub API returned invalid data for {endpoint}") from exc
    return value, provider_now


def _require_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise BootstrapError(f"{field} must be an exact lowercase Git SHA")
    return value


def _require_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise BootstrapError(f"{field} must be an exact lowercase SHA-256 digest")
    return value


def _ensure_clean(repo: Path, candidate_sha: str) -> None:
    if _git(repo, "rev-parse", "HEAD") != candidate_sha:
        raise BootstrapError("candidate SHA differs from the clean local HEAD")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise BootstrapError("candidate working tree is not clean")


def _outside_candidate(path: Path, candidate_root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(candidate_root.resolve())
    except ValueError:
        return resolved
    raise BootstrapError("bootstrap records must be written outside the candidate tree")


def _require_new_output(path: Path) -> None:
    if path.exists():
        raise BootstrapError(f"bootstrap record already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _require_new_output(path)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=path.parent) as temp_dir:
        temporary = Path(temp_dir) / "record.json"
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise BootstrapError(f"bootstrap record already exists: {path}") from exc
        except OSError as exc:
            raise BootstrapError(f"cannot create bootstrap record: {path}") from exc


def _origin_repository(repo: Path) -> str:
    url = _git(repo, "remote", "get-url", "origin")
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(url)
        if parsed.hostname != "github.com":
            raise BootstrapError("origin must be a github.com repository")
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        raise BootstrapError("origin does not identify one GitHub owner/repository")
    return "/".join(parts)


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = _run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant),
        cwd=repo,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise BootstrapError("git could not evaluate candidate ancestry")
    return result.returncode == 0


def _reject_ambiguous_workflow_yaml(workflow: str) -> None:
    if "\ufeff" in workflow:
        raise BootstrapError("workflow uses a forbidden byte-order mark")
    if re.search(
        r"\\(?:x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})",
        workflow,
    ):
        raise BootstrapError("workflow uses forbidden YAML character escapes")

    block_scalar_indent: int | None = None
    in_double_quote = False
    for line in workflow.splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if block_scalar_indent is not None:
            if not stripped or indent > block_scalar_indent:
                continue
            block_scalar_indent = None
        if not stripped or stripped.startswith("#"):
            continue
        if re.search(
            r'''(?:^|[ \t:{\[,])[&*](?![&*])[^\s,\[\]{}'\"]+''', line
        ):
            raise BootstrapError("bootstrap forbids YAML anchors or aliases")
        if stripped.startswith(("{", "[")):
            raise BootstrapError("workflow uses unsupported flow-collection syntax")
        if indent == 0 and re.match(r"---\s*[{[]", stripped):
            raise BootstrapError("workflow uses unsupported flow-collection syntax")
        if stripped == "?" or stripped.startswith("? "):
            raise BootstrapError("workflow uses unsupported explicit mapping keys")
        if re.search(r"(?:^|:\s+|-\s+)!(?:!|<|[A-Za-z]|[ \t])", stripped):
            raise BootstrapError("workflow uses unsupported YAML tags")

        index = 0
        while index < len(line):
            char = line[index]
            if char == '"':
                preceding = 0
                cursor = index - 1
                while cursor >= 0 and line[cursor] == "\\":
                    preceding += 1
                    cursor -= 1
                if preceding % 2 == 0:
                    in_double_quote = not in_double_quote
            if char == "\\" and in_double_quote and not line[index + 1 :].strip():
                raise BootstrapError(
                    "workflow uses forbidden YAML escaped line continuation"
                )
            index += 1

        if not in_double_quote and re.search(
            r"(?:^|:\s+|-\s+)[>|][+-]?\s*(?:#.*)?$", line
        ):
            block_scalar_indent = indent


def _event_block(workflow: str, event: str) -> str | None:
    lines = workflow.splitlines()
    on_keys: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = re.match(
            r"^(?P<indent>[ \t]*)(?:on|['\"]on['\"])(?P<spacing>[ \t]*):"
            r"(?P<inline>.*)$",
            line,
        )
        if match is not None:
            if match.group("indent") or match.group("spacing"):
                raise BootstrapError("workflow uses unsupported trigger-key syntax")
            on_keys.append((index, match))
    if len(on_keys) > 1:
        raise BootstrapError("workflow defines duplicate trigger keys")
    for index, match in on_keys:
        inline = match.group("inline")
        if inline.strip() and not inline.lstrip().startswith("#"):
            return "" if re.search(rf"\b{event}\b", inline) else None
        block: list[str] = []
        for nested in lines[index + 1 :]:
            if "\t" in nested[: len(nested) - len(nested.lstrip())]:
                raise BootstrapError("workflow trigger indentation cannot use tabs")
            if nested and not nested.startswith((" ", "#")):
                break
            block.append(nested)
        direct_indent: int | None = None
        for nested in block:
            if not nested.strip() or nested.lstrip().startswith("#"):
                continue
            direct_indent = len(nested) - len(nested.lstrip(" "))
            break
        if direct_indent is None:
            return None
        for nested_index, nested in enumerate(block):
            if nested.startswith(" " * direct_indent + "-"):
                raise BootstrapError("workflow uses unsupported event-sequence syntax")
            event_match = re.match(
                rf"^ {{{direct_indent}}}(?:{event}|['\"]{event}['\"]):"
                rf"(?P<inline>.*)$",
                nested,
            )
            if event_match is None:
                loose_event = re.match(
                    rf"^ {{{direct_indent}}}(?:{event}|['\"]{event}['\"])[ \t]+:",
                    nested,
                )
                if loose_event is not None:
                    raise BootstrapError("workflow uses unsupported event-key syntax")
                continue
            event_lines = [event_match.group("inline")]
            for detail in block[nested_index + 1 :]:
                if detail.strip() and not detail.lstrip().startswith("#"):
                    detail_indent = len(detail) - len(detail.lstrip(" "))
                    if detail_indent <= direct_indent:
                        break
                if re.match(rf"^ {{{direct_indent}}}\S", detail):
                    break
                event_lines.append(detail)
            return "\n".join(event_lines)
        return None
    return None


def _pull_events(workflow: str) -> set[str]:
    return {
        event
        for event in ("pull_request", "pull_request_target")
        if _event_block(workflow, event) is not None
    }


def _push_reaches_branch(workflow: str, branch: str) -> bool:
    block = _event_block(workflow, "push")
    if block is None:
        return False
    loose_branches = re.search(
        r"(?m)^\s*['\"]?branches['\"]?\s*:\s*(?P<value>.*)$", block
    )
    branches = re.search(r"(?m)^\s*branches:\s*(?P<value>.*)$", block)
    if loose_branches is not None and branches is None:
        raise BootstrapError("push branch filter uses unsupported key syntax")
    if branches is not None:
        inline = branches.group("value").strip()
        if "#" in inline:
            raise BootstrapError("push branch filter uses unsupported inline comments")
        if inline.startswith("[") and inline.endswith("]"):
            values = [item.strip().strip("'\"") for item in inline[1:-1].split(",")]
        elif not inline:
            tail = block[branches.end() :]
            values = []
            for match in re.finditer(r"(?m)^\s+-\s*(?P<value>.+?)\s*$", tail):
                value = match.group("value")
                if "#" in value:
                    raise BootstrapError(
                        "push branch filter uses unsupported inline comments"
                    )
                values.append(value.strip().strip("'\""))
        else:
            values = [inline.strip("'\"")]
        if not values or any(re.search(r"[*?!\[\]]", value) for value in values):
            return True
        return branch in values
    if re.search(r"(?m)^\s*tags:\s*", block):
        return False
    return True


def _job_blocks(workflow: str) -> tuple[str, dict[str, str]]:
    prefix, marker, jobs = workflow.partition("\njobs:\n")
    if not marker:
        raise BootstrapError("pull-request workflow does not define jobs")
    job_key = re.compile(
        r"(?m)^  (?P<quote>['\"]?)(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?P=quote):\n"
    )
    for line in jobs.splitlines():
        if (
            re.match(r"^  \S", line)
            and not line.lstrip().startswith("#")
            and job_key.fullmatch(line + "\n") is None
        ):
            raise BootstrapError(f"pull-request workflow has unsupported job syntax: {line}")
    matches = list(job_key.finditer(jobs))
    if not matches:
        raise BootstrapError("pull-request workflow has no jobs")
    blocks: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(jobs)
        blocks[match.group("name")] = jobs[match.end() : end]
    return prefix, blocks


def _job_if_expression(job: str) -> str | None:
    match = re.search(r"(?m)^    if:\s*(?P<value>.+?)\s*$", job)
    if match is None:
        return None
    expression = " ".join(match.group("value").split()).strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    return expression


def _job_is_push_only(job: str) -> bool:
    expression = _job_if_expression(job)
    if expression is None:
        return False
    return re.fullmatch(
        r"github\.event_name == 'push'"
        r"(?: && github\.ref == 'refs/heads/[A-Za-z0-9._/-]+')?",
        expression,
    ) is not None


def _job_excludes_draft(job: str) -> bool:
    expression = _job_if_expression(job)
    if expression is None:
        return False
    guard = (
        "github.event_name != 'pull_request' || "
        "github.event.pull_request.draft == false"
    )
    return re.fullmatch(
        rf"(?:vars\.[A-Z][A-Z0-9_]* == 'true' && )?\({re.escape(guard)}\)",
        expression,
    ) is not None


def _fully_parenthesized(expression: str) -> bool:
    if not expression.startswith("(") or not expression.endswith(")"):
        return False
    depth = 0
    quote_char: str | None = None
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote_char is not None:
            if char == quote_char:
                if quote_char == "'" and index + 1 < len(expression) and (
                    expression[index + 1] == "'"
                ):
                    index += 1
                else:
                    quote_char = None
        elif char in ("'", '"'):
            quote_char = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(expression) - 1:
                return False
            if depth < 0:
                return False
        index += 1
    return depth == 0 and quote_char is None


def _job_excludes_feature_dispatch(job: str, default_branch: str) -> bool:
    expression = _job_if_expression(job)
    if expression is None:
        return False
    guard = (
        "(github.event_name != 'workflow_dispatch' || "
        f"github.ref == 'refs/heads/{default_branch}')"
    )
    if expression == guard:
        return True
    prefix = guard + " && "
    return expression.startswith(prefix) and _fully_parenthesized(
        expression.removeprefix(prefix)
    )


def _job_uses_provider_runner(job: str) -> bool:
    match = re.search(r"(?m)^    runs-on:\s*(?P<value>.+?)\s*$", job)
    if match is None:
        return False
    value = match.group("value").strip().strip("'\"")
    return re.fullmatch(
        r"(?:ubuntu|windows|macos)-(?:latest|[0-9][A-Za-z0-9.-]*)", value
    ) is not None


def _dangerous_job_capability(job: str) -> bool:
    patterns = (
        r"(?s)\$\{\{(?:(?!\}\}).)*\bsecrets\b(?:(?!\}\}).)*\}\}",
        r"(?m)^\s+['\"]?secrets['\"]?\s*:\s*['\"]?inherit['\"]?\s*$",
        r"(?m)^\s+['\"]?permissions['\"]?\s*:",
        r"(?m)^\s+['\"]?environment['\"]?\s*:",
    )
    return any(re.search(pattern, job) for pattern in patterns)


def _require_read_only_global_permissions(workflow: str) -> None:
    keys = list(
        re.finditer(
            r"(?m)^(?P<indent>[ \t]*)['\"]?permissions['\"]?"
            r"(?P<spacing>[ \t]*):(?P<inline>.*)$",
            workflow,
        )
    )
    top_level = [match for match in keys if not match.group("indent")]
    if len(top_level) != 1:
        raise BootstrapError(
            "bootstrap-reachable workflow requires one explicit global permissions block"
        )
    match = top_level[0]
    if match.group(0).splitlines()[0] != "permissions:":
        raise BootstrapError(
            "global secret/write grant or unsupported permissions syntax"
        )
    lines = workflow[match.end() :].splitlines()
    permission_lines: list[str] = []
    for line in lines:
        if line and not line.startswith((" ", "#")):
            break
        if line.strip() and not line.lstrip().startswith("#"):
            permission_lines.append(line)
    if permission_lines != ["  contents: read"]:
        raise BootstrapError(
            "global secret/write grant: bootstrap permissions must be exactly "
            "contents: read"
        )


def _validate_bootstrap_workflows(
    repo: Path, branch: str, *, default_branch: str = "main"
) -> dict[str, str]:
    workflow_root = repo / ".github" / "workflows"
    if workflow_root.is_symlink() or not workflow_root.is_dir():
        raise BootstrapError("workflow root must be a real directory")
    validated: dict[str, str] = {}
    pull_workflows: set[str] = set()
    for path in sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))):
        if path.is_symlink() or not path.is_file():
            raise BootstrapError(f"workflow must be a regular file: {path.name}")
        text = path.read_text(encoding="utf-8")
        _reject_ambiguous_workflow_yaml(text)
        events = _pull_events(text)
        relative = path.relative_to(repo).as_posix()
        if re.search(
            r"(?m)^\s*(?:[A-Za-z0-9_'\"-]+:\s*|-[ ]*)[&*][A-Za-z0-9_-]+",
            text,
        ):
            raise BootstrapError(f"bootstrap forbids YAML anchors or aliases: {relative}")
        if "pull_request_target" in events:
            raise BootstrapError(f"bootstrap forbids pull_request_target: {relative}")
        pull_reachable = "pull_request" in events
        if pull_reachable:
            pull_workflows.add(relative)
        push_reachable = _push_reaches_branch(text, branch)
        dispatch_reachable = _event_block(text, "workflow_dispatch") is not None
        if not pull_reachable and not push_reachable and not dispatch_reachable:
            validated[relative] = _sha256(path)
            continue
        prefix, jobs = _job_blocks(text)
        reachable_jobs: dict[str, str] = {}
        for name, job in jobs.items():
            reachable = (
                pull_reachable
                and not _job_excludes_draft(job)
                and not _job_is_push_only(job)
            ) or push_reachable or (
                dispatch_reachable
                and not _job_excludes_feature_dispatch(job, default_branch)
            )
            if reachable:
                reachable_jobs[name] = job
        if reachable_jobs:
            _require_read_only_global_permissions(text)
            if re.search(
                r"(?s)\$\{\{(?:(?!\}\}).)*\bsecrets\b(?:(?!\}\}).)*\}\}",
                prefix,
            ):
                raise BootstrapError(
                    f"bootstrap-reachable workflow has a global secret: {relative}"
                )
        for name, job in reachable_jobs.items():
            if not _job_uses_provider_runner(job):
                raise BootstrapError(
                    f"bootstrap-reachable job lacks a provider runner {relative}:{name}"
                )
            if _dangerous_job_capability(job):
                raise BootstrapError(
                    f"draft bootstrap could reach privileged job {relative}:{name}"
                )
        validated[relative] = _sha256(path)
    missing = sorted(REQUIRED_PR_WORKFLOWS - pull_workflows)
    if missing:
        raise BootstrapError(
            f"required pull-request workflows are missing: {', '.join(missing)}"
        )
    return validated


def _changed_paths(repo: Path, previous_head: str, candidate_sha: str) -> tuple[str, ...]:
    changed = _git(
        repo,
        "diff",
        "--no-renames",
        "--name-only",
        "--diff-filter=ACDMRTUXB",
        f"{previous_head}..{candidate_sha}",
    )
    paths = tuple(sorted(path for path in changed.splitlines() if path))
    if not paths:
        raise BootstrapError("bootstrap candidate contains no changed paths")
    return paths


def _provider_state(
    repo: Path,
    policy_root: Path,
    *,
    github_repository: str,
    pr_number: int,
    candidate_sha: str,
) -> ProviderState:
    if policy_root.resolve() == repo.resolve():
        raise BootstrapError("bootstrap policy must be loaded from a separate checkout")
    if _origin_repository(repo).casefold() != github_repository.casefold() or (
        _origin_repository(policy_root).casefold() != github_repository.casefold()
    ):
        raise BootstrapError("candidate, policy, and requested GitHub repository differ")

    metadata, metadata_time = _provider_json(
        policy_root, f"repos/{github_repository}"
    )
    if not isinstance(metadata, dict) or metadata.get("full_name") != github_repository:
        raise BootstrapError("GitHub repository metadata differs from requested repository")
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise BootstrapError("GitHub repository lacks a default branch")
    default_ref, ref_time = _provider_json(
        policy_root,
        f"repos/{github_repository}/git/ref/heads/{quote(default_branch, safe='')}",
    )
    ref_object = default_ref.get("object") if isinstance(default_ref, dict) else None
    default_sha = _require_sha(
        ref_object.get("sha") if isinstance(ref_object, dict) else None,
        field="provider default-branch SHA",
    )
    policy_sha = _git(policy_root, "rev-parse", "HEAD")
    if policy_sha != default_sha or _git(repo, "rev-parse", f"origin/{default_branch}") != default_sha:
        raise BootstrapError("policy checkout and origin tracking ref must match provider default")
    _ensure_clean(policy_root, policy_sha)

    pull, pull_time = _provider_json(
        policy_root, f"repos/{github_repository}/pulls/{pr_number}"
    )
    if not isinstance(pull, dict) or pull.get("state") != "open" or pull.get("draft") is not True:
        raise BootstrapError("bootstrap pull request must be open and draft")
    head = pull.get("head")
    base = pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise BootstrapError("pull request lacks head/base identity")
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict) or (
        head_repo.get("full_name") != github_repository
        or base_repo.get("full_name") != github_repository
    ):
        raise BootstrapError("bootstrap requires same-repository head and base")
    branch = head.get("ref")
    if not isinstance(branch, str) or not branch:
        raise BootstrapError("pull request lacks a head branch")
    previous_head = _require_sha(head.get("sha"), field="pull request head SHA")
    if base.get("ref") != default_branch or base.get("sha") != default_sha:
        raise BootstrapError("pull request base must equal the current provider default")
    branch_info, branch_time = _provider_json(
        policy_root, f"repos/{github_repository}/branches/{quote(branch, safe='')}"
    )
    if not isinstance(branch_info, dict) or branch_info.get("protected") is not False:
        raise BootstrapError("bootstrap head branch must exist and be non-protected")
    if _git(repo, "branch", "--show-current") != branch:
        raise BootstrapError("candidate checkout branch differs from pull request head")
    if candidate_sha == previous_head:
        raise BootstrapError("bootstrap candidate must advance the pull request head")
    if not _is_ancestor(repo, default_sha, candidate_sha):
        raise BootstrapError("provider default branch is not an ancestor of candidate")
    if not _is_ancestor(repo, previous_head, candidate_sha):
        raise BootstrapError("bootstrap candidate must fast-forward the pull request head")
    workflows = _validate_bootstrap_workflows(
        repo, branch, default_branch=default_branch
    )
    return ProviderState(
        repository=github_repository,
        default_branch=default_branch,
        default_sha=default_sha,
        branch=branch,
        previous_head=previous_head,
        candidate_sha=candidate_sha,
        changed_paths=_changed_paths(repo, previous_head, candidate_sha),
        provider_now=max(metadata_time, ref_time, pull_time, branch_time),
        workflow_files=workflows,
        pull=pull,
    )


def _input_classification() -> tuple[str, set[str], set[str]]:
    host = platform.system()
    local = LOCAL_COVERAGE_BY_HOST.get(host)
    if local is None:
        raise BootstrapError(f"unsupported bootstrap host platform: {host}")
    provider = set(EVIDENCE_INPUTS) - local
    if not PROVIDER_EVIDENCE_INPUTS.issubset(provider):
        raise BootstrapError("internal provider-input classification is invalid")
    return host, set(local), provider


def _validate_local_coverage(path: Path, name: str) -> None:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BootstrapError(f"cannot read locally required coverage input: {name}") from exc
    if not payload:
        raise BootstrapError(f"locally required coverage input is empty: {name}")
    try:
        if path.suffix == ".xml":
            root = ET.fromstring(payload)
            root_name = root.tag.rsplit("}", 1)[-1]
            if root_name not in {"coverage", "report"} or not (
                root.attrib or len(root)
            ):
                raise ValueError("coverage XML lacks report structure")
        else:
            document = json.loads(payload)
            if not isinstance(document, (dict, list)) or not document:
                raise ValueError("coverage JSON must be a non-empty object or array")
    except (ET.ParseError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BootstrapError(
            f"locally required coverage input is not a parseable report: {name}"
        ) from exc


def _snapshot_inputs(repo: Path) -> tuple[str, dict[str, dict[str, Any]], list[str], list[str]]:
    host, local, provider = _input_classification()
    inputs: dict[str, dict[str, Any]] = {}
    for name, relative in EVIDENCE_INPUTS.items():
        path = repo / relative
        entry: dict[str, Any] = {
            "path": relative,
            "classification": "locally_required" if name in local else "provider_bound",
            "present": path.is_file(),
        }
        if path.is_file():
            if name in local:
                _validate_local_coverage(path, name)
            entry.update({"sha256": _sha256(path), "size_bytes": path.stat().st_size})
        inputs[name] = entry
    missing = sorted(name for name, entry in inputs.items() if not entry["present"])
    missing_local = sorted(set(missing) & local)
    if missing_local:
        raise BootstrapError(
            f"locally required evidence inputs are missing: {', '.join(missing_local)}"
        )
    provider_missing = sorted(set(missing) & provider)
    if provider_missing != sorted(provider):
        raise BootstrapError(
            "bootstrap requires every provider-bound input to be absent for this SHA"
        )
    return host, inputs, missing, provider_missing


def _evidence_command_record(base_sha: str, candidate_sha: str) -> dict[str, Any]:
    return {
        "argv": [
            "python3",
            EVIDENCE_TOOL,
            "--evidence-dir",
            "build/060/release-evidence",
            "--coverage-dir",
            "build/060/coverage",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            candidate_sha,
            "--failure-output",
            "<external-bootstrap-failure>",
        ],
        "policy_source": "current provider default branch",
    }


def _run_evidence(
    repo: Path, policy_root: Path, base_sha: str, candidate_sha: str
) -> EvidenceRun:
    policy_tool = policy_root / EVIDENCE_TOOL
    if not policy_tool.is_file():
        raise BootstrapError("default-branch evidence parser is missing")
    with tempfile.TemporaryDirectory(prefix="astral-bootstrap-evidence-") as temp_dir:
        failure_path = Path(temp_dir) / "failure.json"
        completed = _run(
            (
                sys.executable,
                str(policy_tool),
                "--evidence-dir",
                str(repo / "build/060/release-evidence"),
                "--coverage-dir",
                str(repo / "build/060/coverage"),
                "--base-sha",
                base_sha,
                "--candidate-sha",
                candidate_sha,
                "--output",
                str(Path(temp_dir) / "local-diagnostic.json"),
                "--failure-output",
                str(failure_path),
            ),
            cwd=policy_root,
            check=False,
        )
        failure: Mapping[str, Any] | None = None
        if failure_path.is_file():
            try:
                loaded = json.loads(failure_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BootstrapError("evidence parser wrote an invalid failure receipt") from exc
            if not isinstance(loaded, dict):
                raise BootstrapError("evidence parser failure receipt must be an object")
            failure = loaded
        return EvidenceRun(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            failure=failure,
        )


def _validate_missing_provider_failure(
    failure: Mapping[str, Any] | None, *, base_sha: str, candidate_sha: str
) -> None:
    if not isinstance(failure, Mapping) or set(failure) != {
        "document_type",
        "schema_version",
        "error_code",
        "error_message_sha256",
        "base_sha",
        "candidate_sha",
        "protected_release_authorization",
    }:
        raise BootstrapError("evidence parser did not emit the exact failure contract")
    expected = {
        "document_type": "release_evidence_local_failure",
        "schema_version": 1,
        "error_code": "missing_provider_inputs",
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "protected_release_authorization": False,
    }
    for field, value in expected.items():
        if failure.get(field) != value:
            raise BootstrapError(
                f"evidence parser failure {field} is not bootstrap-eligible"
            )
    _require_digest(
        failure.get("error_message_sha256"), field="evidence failure message digest"
    )


def _policy_identity(policy_root: Path, state: ProviderState) -> dict[str, str]:
    allowlist_path = policy_root / ".github" / "release-evidence-leads.json"
    if not allowlist_path.is_file():
        raise BootstrapError("default-branch lead allowlist is missing")
    return {
        "policy_commit": state.default_sha,
        "verifier_sha256": _sha256(Path(__file__).resolve()),
        "lead_allowlist_sha256": _sha256(allowlist_path),
    }


def build_inventory(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the parser and build one non-authorizing missing-input inventory."""

    repo = Path(args.repo).resolve()
    policy_root = Path(__file__).resolve().parents[1]
    candidate_sha = _require_sha(args.candidate_sha, field="candidate_sha")
    _ensure_clean(repo, candidate_sha)
    state = _provider_state(
        repo,
        policy_root,
        github_repository=args.github_repository,
        pr_number=args.pr_number,
        candidate_sha=candidate_sha,
    )
    host, inputs, missing, provider_missing = _snapshot_inputs(repo)
    completed = _run_evidence(repo, policy_root, state.default_sha, candidate_sha)
    if completed.returncode != 2:
        raise BootstrapError("only a handled evidence-policy failure is bootstrap-eligible")
    _validate_missing_provider_failure(
        completed.failure,
        base_sha=state.default_sha,
        candidate_sha=candidate_sha,
    )
    return {
        "document_type": "release_evidence_bootstrap_inventory",
        "schema_version": 1,
        "repository": args.github_repository,
        "pr_number": args.pr_number,
        "feature": args.feature,
        "branch": state.branch,
        "base_branch": state.default_branch,
        "base_sha": state.default_sha,
        "previous_head": state.previous_head,
        "candidate_sha": candidate_sha,
        "changed_paths": list(state.changed_paths),
        "generated_at": _format_time(state.provider_now),
        "status": "bootstrap_missing_inputs",
        "protected_release_authorization": False,
        "host_platform": host,
        "evidence_command": _evidence_command_record(state.default_sha, candidate_sha),
        "evidence_command_exit_code": completed.returncode,
        "evidence_failure": dict(completed.failure),
        "evidence_stdout_sha256": _bytes_sha256(completed.stdout),
        "evidence_stderr_sha256": _bytes_sha256(completed.stderr),
        "inputs": inputs,
        "missing_inputs": missing,
        "provider_bound_missing_inputs": provider_missing,
        "workflow_files": dict(state.workflow_files),
        "policy": _policy_identity(policy_root, state),
    }


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"{label} must be a JSON object")
    return value


def _load_leads(path: Path) -> set[str]:
    value = _load_json(path, label="lead allowlist")
    if set(value) != {"schema_version", "lead_logins"} or value.get("schema_version") != 1:
        raise BootstrapError("lead allowlist differs from the v1 contract")
    logins = value.get("lead_logins")
    if not isinstance(logins, list) or not logins or not all(
        isinstance(item, str) and item for item in logins
    ):
        raise BootstrapError("lead allowlist must contain non-empty lead_logins")
    if len(logins) != len(set(logins)):
        raise BootstrapError("lead allowlist contains duplicate logins")
    return set(logins)


def _all_comments(
    policy_root: Path, github_repository: str, pr_number: int
) -> tuple[list[Any], datetime]:
    comments: list[Any] = []
    provider_times: list[datetime] = []
    for page in range(1, 101):
        value, provider_time = _provider_json(
            policy_root,
            f"repos/{github_repository}/issues/{pr_number}/comments?per_page=100&page={page}",
        )
        if not isinstance(value, list):
            raise BootstrapError("GitHub comments response must be a list")
        comments.extend(value)
        provider_times.append(provider_time)
        if len(value) < 100:
            return comments, max(provider_times)
    raise BootstrapError("GitHub comments exceed the bounded pagination limit")


def _approval_from_comments(
    comments: Sequence[Any], *, candidate_sha: str, leads: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
            continue
        marker = APPROVAL_MARKER_RE.search(comment["body"])
        if marker is None:
            continue
        user = comment.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if login not in leads:
            continue
        try:
            approval = json.loads(marker.group("payload"))
        except json.JSONDecodeError as exc:
            raise BootstrapError("bootstrap approval marker contains invalid JSON") from exc
        if not isinstance(approval, dict):
            raise BootstrapError("bootstrap approval marker must contain a JSON object")
        if approval.get("candidate_sha") != candidate_sha:
            continue
        if comment.get("created_at") != comment.get("updated_at"):
            raise BootstrapError("bootstrap approval comment was edited")
        matches.append((approval, comment))
    if len(matches) != 1:
        raise BootstrapError(
            f"expected exactly one unedited approval for candidate, found {len(matches)}"
        )
    return matches[0]


def _validate_inventory(
    inventory: Mapping[str, Any],
    *,
    inventory_path: Path,
    repo: Path,
    policy_root: Path,
    state: ProviderState,
    args: argparse.Namespace,
) -> tuple[str, list[str]]:
    if set(inventory) != INVENTORY_KEYS:
        raise BootstrapError("bootstrap inventory fields differ from the v1 contract")
    expected = {
        "document_type": "release_evidence_bootstrap_inventory",
        "schema_version": 1,
        "repository": args.github_repository,
        "pr_number": args.pr_number,
        "branch": state.branch,
        "base_branch": state.default_branch,
        "base_sha": state.default_sha,
        "previous_head": state.previous_head,
        "candidate_sha": state.candidate_sha,
        "changed_paths": list(state.changed_paths),
        "status": "bootstrap_missing_inputs",
        "protected_release_authorization": False,
        "workflow_files": dict(state.workflow_files),
        "policy": _policy_identity(policy_root, state),
    }
    for field, value in expected.items():
        if inventory.get(field) != value:
            raise BootstrapError(f"bootstrap inventory {field} differs from live candidate")
    if not isinstance(inventory.get("feature"), str) or not inventory["feature"].strip():
        raise BootstrapError("bootstrap inventory feature must be non-empty")
    generated = _parse_time(inventory.get("generated_at"), field="inventory.generated_at")
    if generated > state.provider_now:
        raise BootstrapError("bootstrap inventory generation time is in the future")
    host, inputs, missing, provider_missing = _snapshot_inputs(repo)
    for field, value in {
        "host_platform": host,
        "inputs": inputs,
        "missing_inputs": missing,
        "provider_bound_missing_inputs": provider_missing,
        "evidence_command": _evidence_command_record(
            state.default_sha, state.candidate_sha
        ),
    }.items():
        if inventory.get(field) != value:
            raise BootstrapError(f"bootstrap inventory {field} is not reproducible")
    exit_code = inventory.get("evidence_command_exit_code")
    if exit_code != 2:
        raise BootstrapError("bootstrap inventory must retain handled evidence exit 2")
    failure = inventory.get("evidence_failure")
    _validate_missing_provider_failure(
        failure if isinstance(failure, Mapping) else None,
        base_sha=state.default_sha,
        candidate_sha=state.candidate_sha,
    )
    stdout_digest = _require_digest(
        inventory.get("evidence_stdout_sha256"), field="evidence stdout digest"
    )
    stderr_digest = _require_digest(
        inventory.get("evidence_stderr_sha256"), field="evidence stderr digest"
    )
    completed = _run_evidence(
        repo, policy_root, state.default_sha, state.candidate_sha
    )
    if (
        completed.returncode != exit_code
        or completed.failure != failure
        or _bytes_sha256(completed.stdout) != stdout_digest
        or _bytes_sha256(completed.stderr) != stderr_digest
    ):
        raise BootstrapError("bootstrap evidence failure is not reproducible")
    return _sha256(inventory_path), provider_missing


def _validate_approval(
    approval: Mapping[str, Any],
    comment: Mapping[str, Any],
    inventory: Mapping[str, Any],
    *,
    inventory_sha256: str,
    provider_missing: list[str],
    state: ProviderState,
    github_repository: str,
    pr_number: int,
    provider_now: datetime,
) -> None:
    if set(approval) != APPROVAL_KEYS:
        raise BootstrapError("bootstrap approval fields differ from the v1 contract")
    expected = {
        "schema_version": 1,
        "document_type": "release_evidence_bootstrap_approval",
        "repository": github_repository,
        "pr_number": pr_number,
        "feature": inventory.get("feature"),
        "branch": state.branch,
        "base_branch": state.default_branch,
        "base_sha": state.default_sha,
        "previous_head": state.previous_head,
        "candidate_sha": state.candidate_sha,
        "approved_paths": list(state.changed_paths),
        "provider_bound_missing_inputs": provider_missing,
        "inventory_sha256": inventory_sha256,
        "policy_commit": state.default_sha,
    }
    for field, value in expected.items():
        if approval.get(field) != value:
            raise BootstrapError(f"bootstrap approval {field} differs from live candidate")
    attestation = approval.get("local_gate_attestation")
    if not isinstance(attestation, Mapping) or set(attestation) != {
        "status",
        "candidate_sha",
        "commands",
        "evidence_input_sha256",
    }:
        raise BootstrapError("local gate attestation differs from the v1 contract")
    inputs = inventory.get("inputs")
    if not isinstance(inputs, Mapping):
        raise BootstrapError("bootstrap inventory inputs are unavailable")
    expected_digests = {
        name: entry.get("sha256")
        for name, entry in inputs.items()
        if isinstance(entry, Mapping) and entry.get("classification") == "locally_required"
    }
    commands = attestation.get("commands")
    if (
        attestation.get("status") != "passed"
        or attestation.get("candidate_sha") != state.candidate_sha
        or not isinstance(commands, list)
        or not commands
        or not all(isinstance(command, str) and command.strip() for command in commands)
        or len(commands) != len(set(commands))
        or attestation.get("evidence_input_sha256") != expected_digests
    ):
        raise BootstrapError(
            "local gate attestation must bind passed commands and exact report digests"
        )
    for field in ("structural_blocker", "purpose"):
        if not isinstance(approval.get(field), str) or not approval[field].strip():
            raise BootstrapError(f"bootstrap approval {field} must be non-empty")
    created = _parse_time(comment.get("created_at"), field="comment.created_at")
    expires = _parse_time(approval.get("expires_at"), field="expires_at")
    if expires <= created or expires - created > MAX_WINDOW:
        raise BootstrapError("bootstrap approval expiry must be within 168 hours")
    if provider_now >= expires:
        raise BootstrapError("bootstrap approval has expired")


def verify_bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    """Reproduce all inputs and verify provider approval for one candidate."""

    repo = Path(args.repo).resolve()
    policy_root = Path(__file__).resolve().parents[1]
    candidate_sha = _require_sha(args.candidate_sha, field="candidate_sha")
    _ensure_clean(repo, candidate_sha)
    state = _provider_state(
        repo,
        policy_root,
        github_repository=args.github_repository,
        pr_number=args.pr_number,
        candidate_sha=candidate_sha,
    )
    allowlist_path = policy_root / ".github" / "release-evidence-leads.json"
    leads = _load_leads(allowlist_path)
    inventory_path = _outside_candidate(Path(args.inventory), repo)
    inventory = _load_json(inventory_path, label="bootstrap inventory")
    inventory_sha256, provider_missing = _validate_inventory(
        inventory,
        inventory_path=inventory_path,
        repo=repo,
        policy_root=policy_root,
        state=state,
        args=args,
    )
    comments, comments_time = _all_comments(
        policy_root, args.github_repository, args.pr_number
    )
    approval, comment = _approval_from_comments(
        comments, candidate_sha=candidate_sha, leads=leads
    )
    provider_now = max(state.provider_now, comments_time)
    _validate_approval(
        approval,
        comment,
        inventory,
        inventory_sha256=inventory_sha256,
        provider_missing=provider_missing,
        state=state,
        github_repository=args.github_repository,
        pr_number=args.pr_number,
        provider_now=provider_now,
    )
    return {
        "document_type": "release_evidence_bootstrap_verification",
        "schema_version": 1,
        "repository": args.github_repository,
        "pr_number": args.pr_number,
        "branch": state.branch,
        "candidate_sha": candidate_sha,
        "previous_head": state.previous_head,
        "verified_at": _format_time(provider_now),
        "status": "bootstrap_push_verified",
        "protected_release_authorization": False,
        "approval_comment_id": comment.get("id"),
        "approval_comment_url": comment.get("html_url"),
        "approval_sha256": _canonical_sha256(dict(approval)),
        "inventory_sha256": inventory_sha256,
        "changed_paths": list(state.changed_paths),
        "policy": _policy_identity(policy_root, state),
    }


def _push_candidate(
    repo: Path, *, candidate_sha: str, branch: str, previous_head: str
) -> None:
    _run(
        (
            "git",
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/{branch}:{previous_head}",
            "origin",
            f"{candidate_sha}:refs/heads/{branch}",
        ),
        cwd=repo,
    )


def _post_push_receipt(
    args: argparse.Namespace, verification: Mapping[str, Any]
) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    policy_root = Path(__file__).resolve().parents[1]
    provider_now: datetime | None = None
    for attempt in range(5):
        pull, provider_now = _provider_json(
            policy_root, f"repos/{args.github_repository}/pulls/{args.pr_number}"
        )
        head = pull.get("head") if isinstance(pull, dict) else None
        if (
            not isinstance(pull, dict)
            or pull.get("state") != "open"
            or pull.get("draft") is not True
        ):
            raise BootstrapError("provider did not retain an open draft pull request")
        if isinstance(head, dict) and head.get("sha") == args.candidate_sha:
            break
        if attempt < 4:
            time.sleep(min(2**attempt, 4))
    else:
        raise BootstrapError("provider did not retain the exact pushed SHA")
    assert provider_now is not None
    return {
        "document_type": "release_evidence_bootstrap_push_receipt",
        "schema_version": 1,
        "repository": args.github_repository,
        "pr_number": args.pr_number,
        "candidate_sha": args.candidate_sha,
        "previous_head": verification["previous_head"],
        "pushed_at": _format_time(provider_now),
        "status": "bootstrap_push_completed",
        "protected_release_authorization": False,
        "verification_sha256": _canonical_sha256(dict(verification)),
        "remote_repository": _origin_repository(repo),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=".")
    common.add_argument("--github-repository", required=True)
    common.add_argument("--pr-number", type=int, required=True)
    common.add_argument("--candidate-sha", required=True)

    inventory = subparsers.add_parser("inventory", parents=[common])
    inventory.add_argument("--feature", required=True)
    inventory.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify", parents=[common])
    verify.add_argument("--inventory", required=True)
    verify.add_argument("--output", required=True)

    push = subparsers.add_parser("push", parents=[common])
    push.add_argument("--inventory", required=True)
    push.add_argument("--preflight-output", required=True)
    push.add_argument("--receipt-output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one bootstrap inventory, verification, or lease-bound push."""

    args = _parser().parse_args(argv)
    try:
        repo = Path(args.repo).resolve()
        if args.command == "inventory":
            output = _outside_candidate(Path(args.output), repo)
            result = build_inventory(args)
            _atomic_json(output, result)
        elif args.command == "verify":
            output = _outside_candidate(Path(args.output), repo)
            result = verify_bootstrap(args)
            _atomic_json(output, result)
        else:
            preflight = _outside_candidate(Path(args.preflight_output), repo)
            receipt = _outside_candidate(Path(args.receipt_output), repo)
            if preflight == receipt:
                raise BootstrapError("preflight and receipt outputs must be distinct")
            _require_new_output(preflight)
            _require_new_output(receipt)
            result = verify_bootstrap(args)
            _atomic_json(preflight, result)
            _push_candidate(
                repo,
                candidate_sha=args.candidate_sha,
                branch=result["branch"],
                previous_head=result["previous_head"],
            )
            receipt_value = _post_push_receipt(args, result)
            _atomic_json(receipt, receipt_value)
            result = receipt_value
        print(json.dumps(result, sort_keys=True))
        return 0
    except BootstrapError as exc:
        print(f"release-evidence bootstrap rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
