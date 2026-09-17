#!/usr/bin/env python3
"""Enforce Feature 089's scope guards across the five Astral repositories.

Feature 089 is web-only and CI-exempt by owner directive (spec FR-039,
SC-011). Two classes of path are therefore forbidden to change:

* native client directories -- ``windows-client/``, ``android-client/`` and
  ``apple-clients/`` in AstralProjection, and their equivalents anywhere else;
* any ``.github/workflows/`` file in any of the five repositories.

This checker diffs each repository between its recorded 089 baseline commit
and its current ``HEAD`` and fails when a changed path matches a guard. It is
a local diagnostic: it authorizes nothing and reads no credential.

The baselines live in ``specs/089-typesafe-a8p-integration/verification.md``
and are duplicated here as ``DEFAULT_BASELINES`` so the check is runnable
without parsing prose. Override any of them with ``--baseline NAME=SHA``.

Exit codes: ``0`` clean, ``1`` a guard was violated, ``2`` the check could not
run (missing repository, unknown commit).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable, Sequence

# Recorded 2026-09-17. Keep in step with verification.md section 1.
DEFAULT_BASELINES: dict[str, str] = {
    "AstralDeep": "e92db75d95719602228b5b41f179cfcbeeadc3bd",
    "AstralPlane": "65cbaedbbda4c5f9adfcb05becf02377a29caeee",
    "AstralPrimitives": "4056df95acd992a9f84d883e572760f6da24c88e",
    "AstralProjection": "dd93dfb30b89b9966682e117a76952f8429a24b0",
    "LETS": "f53f3f329541ad34161ec9aaaad075e80ccb01d1",
}

# Sibling checkouts of the five repositories, resolved relative to AstralDeep.
DEFAULT_REPO_PARENT = Path(__file__).resolve().parents[2]

# A changed path fails when any of these names one of its components. Matching
# on components rather than a prefix catches a nested or vendored client tree.
CLIENT_DIRECTORIES = (
    "windows-client",
    "android-client",
    "apple-clients",
)

WORKFLOW_PREFIX = (".github", "workflows")


@dataclass
class RepoResult:
    """The outcome of checking one repository."""

    name: str
    path: Path
    baseline: str
    head: str = ""
    changed: list[str] = field(default_factory=list)
    client_violations: list[str] = field(default_factory=list)
    workflow_violations: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not (self.error or self.client_violations or self.workflow_violations)

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": self.name,
            "path": str(self.path),
            "baseline": self.baseline,
            "head": self.head,
            "changed_file_count": len(self.changed),
            "client_violations": self.client_violations,
            "workflow_violations": self.workflow_violations,
            "error": self.error,
            "ok": self.ok,
        }


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return its stripped stdout."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git " + " ".join(args) + " failed")
    return completed.stdout.strip()


def _is_client_path(parts: Sequence[str]) -> bool:
    return any(part in CLIENT_DIRECTORIES for part in parts)


def _is_workflow_path(parts: Sequence[str]) -> bool:
    for index in range(len(parts) - 1):
        if (parts[index], parts[index + 1]) == WORKFLOW_PREFIX:
            return True
    return False


def check_repo(
    name: str, path: Path, baseline: str, *, include_worktree: bool
) -> RepoResult:
    """Diff one repository against its baseline and classify the changes."""
    result = RepoResult(name=name, path=path, baseline=baseline)
    if not (path / ".git").exists():
        result.error = "not a git repository: " + str(path)
        return result
    try:
        result.head = _git(path, "rev-parse", "HEAD")
        # The trailing "--" keeps a SHA that also names a file from being
        # interpreted as a pathspec.
        changed = set(
            _git(path, "diff", "--name-only", baseline + "..HEAD", "--").splitlines()
        )
        if include_worktree:
            changed |= set(_git(path, "diff", "--name-only", baseline, "--").splitlines())
            porcelain = _git(
                path, "status", "--porcelain", "--untracked-files=all"
            ).splitlines()
            changed |= {line[3:] for line in porcelain if len(line) > 3}
    except RuntimeError as exc:  # unknown commit, corrupt repo, git missing
        result.error = str(exc)
        return result

    result.changed = sorted(entry for entry in changed if entry)
    for entry in result.changed:
        parts = Path(entry).parts
        if _is_client_path(parts):
            result.client_violations.append(entry)
        if _is_workflow_path(parts):
            result.workflow_violations.append(entry)
    return result


def _resolve_repo_path(name: str, parent: Path, overrides: dict[str, Path]) -> Path:
    return overrides.get(name, parent / name)


def _parse_assignments(values: Iterable[str], label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit("--" + label + " expects NAME=VALUE, got " + repr(value))
        key, _, rest = value.partition("=")
        if key not in DEFAULT_BASELINES:
            raise SystemExit(
                "unknown repository "
                + repr(key)
                + "; expected one of "
                + str(sorted(DEFAULT_BASELINES))
            )
        parsed[key] = rest
    return parsed


def _render_text(results: list[RepoResult]) -> str:
    lines = ["Feature 089 scope check", ""]
    for result in results:
        if result.error:
            lines.append("  ERROR  " + result.name + ": " + result.error)
            continue
        status = "ok" if result.ok else "VIOLATION"
        lines.append(
            "  {status:<9} {name}  {base}..{head}  {count} changed file(s)".format(
                status=status,
                name=result.name,
                base=result.baseline[:12],
                head=result.head[:12],
                count=len(result.changed),
            )
        )
        for entry in result.client_violations:
            lines.append("             client-directory change: " + entry)
        for entry in result.workflow_violations:
            lines.append("             workflow change: " + entry)
    lines.append("")
    violations = sum(
        len(r.client_violations) + len(r.workflow_violations) for r in results
    )
    errors = sum(1 for r in results if r.error)
    if errors:
        lines.append("RESULT: could not check " + str(errors) + " repository(ies)")
    elif violations:
        lines.append("RESULT: FAIL -- " + str(violations) + " forbidden change(s)")
    else:
        lines.append("RESULT: PASS -- no client-directory or workflow changes (SC-011)")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Feature 089 scope guard across the five Astral repositories."
    )
    parser.add_argument(
        "--repo-parent",
        type=Path,
        default=DEFAULT_REPO_PARENT,
        help="directory holding the five sibling checkouts (default: AstralDeep's parent)",
    )
    parser.add_argument(
        "--repo-path",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="override one repository's checkout path",
    )
    parser.add_argument(
        "--baseline",
        action="append",
        default=[],
        metavar="NAME=SHA",
        help="override one repository's 089 baseline commit",
    )
    parser.add_argument(
        "--include-worktree",
        action="store_true",
        help="also treat uncommitted and untracked files as changes",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    baselines = dict(DEFAULT_BASELINES)
    baselines.update(_parse_assignments(args.baseline, "baseline"))
    path_overrides = {
        name: Path(value)
        for name, value in _parse_assignments(args.repo_path, "repo-path").items()
    }

    results = [
        check_repo(
            name,
            _resolve_repo_path(name, args.repo_parent, path_overrides),
            baselines[name],
            include_worktree=args.include_worktree,
        )
        for name in sorted(baselines)
    ]

    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print(_render_text(results))

    if any(r.error for r in results):
        return 2
    if any(not r.ok for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
