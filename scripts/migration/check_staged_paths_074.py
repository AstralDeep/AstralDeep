#!/usr/bin/env python3
"""Reject sensitive or local-only paths in an exact Git staged set.

The guard intentionally examines path names and generated-agent marker
metadata only. It never opens staged or working-tree file content. Run it from
any repository with ``--repo <exact-worktree-root>`` immediately before a
migration commit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CONTRACT = "astral.staged-path-guard/074-v1"

_CREDENTIAL_DIRECTORIES = {
    ".aws",
    ".azure",
    ".gnupg",
    ".ssh",
    "credentials",
    "secrets",
}
_CREDENTIAL_NAMES = {
    ".env",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "id_ed25519",
    "id_rsa",
    "keystore.properties",
    "local.properties",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "service-account.json",
}
_SAFE_ENV_TEMPLATE_SUFFIXES = (".dist", ".example", ".sample", ".template")
_PRIVATE_KEY_SUFFIXES = {".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
_DATABASE_SUFFIXES = {
    ".accdb",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".dump",
    ".mdb",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
_DATABASE_DIRECTORIES = {".postgres", "pgdata", "postgres-data", "postgres_data"}
_LOG_DIRECTORIES = {"log", "logs"}
_UPLOAD_DIRECTORIES = {
    "tmp_uploads",
    "uploads",
    "user_uploads",
    "user-uploads",
}
_LOCAL_ENVIRONMENT_DIRECTORIES = {
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "env",
    "htmlcov",
    "node_modules",
    "test-results",
    "venv",
}
_GENERATED_EVIDENCE_DIRECTORIES = {
    ".runs",
    "_artifacts",
    "generated-evidence",
    "generated_evidence",
    "results",
}
_GENERATED_AGENT_DIRECTORIES = {
    "_generated",
    "drafts",
    "generated",
    "generated_agents",
    "user_generated",
}
_LOCAL_MANUSCRIPT_DIRECTORIES = {"manuscript"}
_RUNTIME_PREFIXES = {
    ("backend", "data"),
    ("backend", "knowledge"),
    ("backend", "tmp"),
}


@dataclass(frozen=True)
class Violation:
    path: str
    reason: str


class GuardError(RuntimeError):
    """The staged set could not be inspected safely."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        help="Exact Git worktree root whose index will be inspected",
    )
    return parser


def _git(repo: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repo), *arguments],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise GuardError(f"could not execute git: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GuardError(
            f"git {' '.join(arguments)} failed with exit code "
            f"{completed.returncode}: {detail or 'no error output'}"
        )
    return completed.stdout


def exact_repository_root(value: str) -> Path:
    candidate = Path(value).expanduser()
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise GuardError(f"repository root does not resolve: {candidate}: {exc}") from exc
    if not root.is_dir():
        raise GuardError(f"repository root is not a directory: {root}")

    raw_git_root = _git(root, ("rev-parse", "--show-toplevel"))
    try:
        git_root = Path(os.fsdecode(raw_git_root).rstrip("\r\n")).resolve(strict=True)
    except OSError as exc:
        raise GuardError(f"Git worktree root does not resolve: {exc}") from exc
    if git_root != root:
        raise GuardError(
            f"exact-root mismatch: requested '{root}', Git worktree is '{git_root}'"
        )
    return root


def staged_paths(repo: Path) -> list[str]:
    # --no-renames exposes both sides of a rename as independent paths. That
    # prevents a sensitive old or new name from being hidden by rename display.
    raw = _git(
        repo,
        (
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            "--diff-filter=ACDMRTUXB",
            "--",
        ),
    )
    paths = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    return sorted(set(paths), key=lambda item: (item.casefold(), item))


def _has_prefix(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(parts) >= len(prefix) and parts[: len(prefix)] == prefix


def _is_environment_secret(name: str) -> bool:
    if name == ".env" or name == ".envrc":
        return True
    if not name.startswith(".env."):
        return False
    return not name.endswith(_SAFE_ENV_TEMPLATE_SUFFIXES)


def _generated_agent_marker_exists(repo: Path, parts: tuple[str, ...]) -> bool:
    if len(parts) < 3 or parts[:2] != ("backend", "agents"):
        return False
    marker = repo.joinpath("backend", "agents", parts[2], ".draft")
    try:
        marker.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GuardError(f"could not inspect generated-agent marker '{marker}': {exc}") from exc
    return True


def violations_for_path(repo: Path, raw_path: str) -> list[Violation]:
    normalized = raw_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return [Violation(raw_path, "invalid staged path")]

    parts = tuple(part.casefold() for part in pure.parts)
    name = parts[-1]
    suffix = PurePosixPath(name).suffix.casefold()
    reasons: list[str] = []

    if (
        name in _CREDENTIAL_NAMES
        or _is_environment_secret(name)
        or suffix in _PRIVATE_KEY_SUFFIXES
        or any(part in _CREDENTIAL_DIRECTORIES for part in parts[:-1])
    ):
        reasons.append("credential or private-key path")

    if (
        suffix in _DATABASE_SUFFIXES
        or any(part in _DATABASE_DIRECTORIES for part in parts[:-1])
    ):
        reasons.append("database or database-dump path")

    if (
        suffix == ".log"
        or ".log." in name
        or any(part in _LOG_DIRECTORIES for part in parts[:-1])
    ):
        reasons.append("log path")

    if any(part in _UPLOAD_DIRECTORIES for part in parts):
        reasons.append("upload path")

    if (
        name == ".draft"
        or (
            len(parts) >= 3
            and parts[:2] == ("backend", "agents")
            and parts[2] in _GENERATED_AGENT_DIRECTORIES
        )
        or _generated_agent_marker_exists(repo, parts)
    ):
        reasons.append("generated-agent path")

    if (
        any(part in _LOCAL_MANUSCRIPT_DIRECTORIES for part in parts)
        or _has_prefix(parts, ("paper", "submission"))
        or _has_prefix(parts, ("paper", "private"))
        or _has_prefix(parts, ("paper", "local"))
    ):
        reasons.append("local manuscript or submission path")

    if (
        any(part in _GENERATED_EVIDENCE_DIRECTORIES for part in parts)
        or any(part.startswith("verification-") for part in parts[:-1])
    ):
        reasons.append("generated evidence path")

    if any(part in _LOCAL_ENVIRONMENT_DIRECTORIES for part in parts):
        reasons.append("local environment or generated dependency path")

    if any(_has_prefix(parts, prefix) for prefix in _RUNTIME_PREFIXES) or (
        len(parts) >= 4
        and parts[:2] == ("backend", "agents")
        and parts[3] in {"data", "tmp"}
    ):
        reasons.append("runtime or user-state path")

    return [Violation(raw_path, reason) for reason in reasons]


def inspect_staged_paths(repo: Path, paths: Iterable[str]) -> list[Violation]:
    violations: list[Violation] = []
    for path in paths:
        violations.extend(violations_for_path(repo, path))
    return sorted(
        set(violations),
        key=lambda violation: (
            violation.path.casefold(),
            violation.path,
            violation.reason,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo = exact_repository_root(args.repo)
        paths = staged_paths(repo)
        violations = inspect_staged_paths(repo, paths)
    except GuardError as exc:
        print(f"check_staged_paths_074: {exc}", file=sys.stderr)
        return 2

    if violations:
        print(
            f"{CONTRACT}: refused {len(violations)} sensitive staged-path "
            f"match(es) across {len(paths)} staged path(s)",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"DENY {violation.path}: {violation.reason}", file=sys.stderr)
        return 1

    print(f"{CONTRACT}: checked {len(paths)} staged path(s); no denied paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
