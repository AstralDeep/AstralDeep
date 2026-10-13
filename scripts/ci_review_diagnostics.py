#!/usr/bin/env python3
"""Records deterministic, offline review diagnostics for one candidate revision pair:
the backend qualification suite plan, its test files, and the changed paths classified
by the changed-coverage policy, bound to the exact base and head SHAs. It is a CI review
aid only; it emits no authorization claim and validates no release decision.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))  # sibling-tool imports must work under importlib too

SCHEMA_VERSION = 1
MAX_CHANGED_PATHS = 10_000
MAX_SUITE_TEST_FILES = 20_000
HEX_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
PERF_PROBE = "tests/perf/voice_concurrent_turns.py"


class ReviewDiagnosticsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _git(repo, arguments):
    process = subprocess.run(["git", "-C", str(repo), *arguments],
                             check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ReviewDiagnosticsError("git_error", f"git {' '.join(arguments[:2])} failed{suffix}")
    return process.stdout


def _validated_sha(value, label):
    text = (value or "").strip().lower()
    if not HEX_SHA.fullmatch(text) or set(text) == {"0"}:
        raise ReviewDiagnosticsError("invalid_revision", f"{label} must be a full commit SHA")
    process = subprocess.run(
        ["git", "-C", str(Path.cwd()), "rev-parse", "--verify", f"{text}^{{commit}}"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode != 0:
        raise ReviewDiagnosticsError("unknown_revision", f"{label} is not in this repository")
    if process.stdout.decode("ascii", "strict").strip().lower() != text:
        raise ReviewDiagnosticsError("revision_mismatch",
                                     f"{label} did not resolve to the exact supplied SHA")
    return text


def _changed_paths(repo, base, head):
    raw = _git(repo, ["diff", "--name-status", "-z", "--no-renames", base, head, "--"])
    entries = [item.decode("utf-8", "strict") for item in raw.split(b"\0") if item]
    if len(entries) > 2 * MAX_CHANGED_PATHS:
        raise ReviewDiagnosticsError("changed_path_overflow",
                                     f"the diff exceeds {MAX_CHANGED_PATHS} paths")
    from check_changed_coverage import classify_path
    changed = []
    for index in range(0, len(entries) - 1, 2):
        status, path = entries[index], entries[index + 1]
        target = classify_path(PurePosixPath(path).as_posix())
        changed.append({"path": path, "status": status,
                        "coverage_target": target.key if target is not None else None})
    return sorted(changed, key=lambda item: item["path"])


def _suite_plan(repo):
    from run_backend_web_tests import suite_commands
    plan = []
    for cwd, suite, name in suite_commands(repo):
        relative = suite if cwd == repo else (Path(cwd).relative_to(repo) / suite).as_posix()
        plan.append({"cwd": Path(cwd).relative_to(repo).as_posix(), "path": relative,
                     "suite": name})
        if not (repo / relative).exists():
            raise ReviewDiagnosticsError("missing_suite", f"suite path {relative!r} is absent")
    return plan


def _suite_test_files(repo, plan):
    files = {}
    for entry in plan:
        directory = repo / entry["path"]
        if entry["path"].endswith(".py"):
            found = [directory] if directory.is_file() else []
        else:
            found = sorted(directory.rglob("test_*.py"))
        if not found:
            raise ReviewDiagnosticsError("empty_suite",
                                         f"suite {entry['suite']!r} collects no test files")
        if len(found) > MAX_SUITE_TEST_FILES:
            raise ReviewDiagnosticsError("suite_overflow",
                                         f"suite {entry['suite']!r} exceeds its test-file bound")
        files[entry["suite"]] = sorted(
            path.relative_to(repo).as_posix() for path in found)
    return files


def build_document(repo, base, head):
    from check_changed_coverage import REPOSITORY_PROFILES
    repo = repo.resolve()
    changed = _changed_paths(repo, base, head)
    plan = _suite_plan(repo)
    profile = REPOSITORY_PROFILES["deep"]
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repo.name,
        "base_sha": base,
        "head_sha": head,
        "suite_plan": plan,
        "suite_test_files": _suite_test_files(repo, plan),
        "changed_paths": changed,
        "qualification_policy": {"repository_profile": "deep",
                                 "required_producer_keys": list(profile.producer_keys)},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    repo = Path.cwd()
    try:
        base = _validated_sha(args.base, "base")
        head = _validated_sha(args.head, "head")
        if base == head:
            raise ReviewDiagnosticsError("empty_revision_range",
                                         "base and head SHAs must be distinct")
        ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                                   base, head], check=False)
        if ancestry.returncode == 1:
            raise ReviewDiagnosticsError("non_ancestor_base",
                                         "base SHA is not an ancestor of head SHA")
        if ancestry.returncode != 0:
            raise ReviewDiagnosticsError("git_error", "git merge-base ancestry check failed")
        document = build_document(repo, base, head)
    except (ReviewDiagnosticsError, ValueError, OSError) as error:
        code = getattr(error, "code", "diagnostics_unavailable")
        message = getattr(error, "message", str(error))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(
            {"schema_version": SCHEMA_VERSION, "status": "error",
             "error": {"code": code, "message": message}}, indent=2) + "\n",
            encoding="utf-8")
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
