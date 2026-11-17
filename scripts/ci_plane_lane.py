#!/usr/bin/env python3
"""Chooses which Plane suite ci.yml's plane-postgres job runs: the full suite when the plane/ tree
differs between the base and candidate Deep commits, else SMOKE_TESTS. A base from before the move is
compared through its gitlink commit's root tree, and a zero, malformed, unknown or Plane-less base
fails closed to the full suite.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

PLANE_PATH = "plane"
LEGACY_PLANE_PATH = "components/AstralPlane"
SMOKE_TESTS = ("tests/architecture", "tests/test_api.py", "tests/test_domain.py", "tests/test_bootstrap.py")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ZERO_SHA = "0" * 40


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True,
                          timeout=120, check=False)


def plane_tree(repo: Path, revision: str) -> str | None:
    if SHA.fullmatch(revision) is None or revision == ZERO_SHA:
        return None
    result = _git(repo, "ls-tree", f"{revision}^{{commit}}", "--", PLANE_PATH, LEGACY_PLANE_PATH)
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 1:
        return None
    fields = lines[0].split()
    if len(fields) != 4:
        return None
    if fields[:2] == ["040000", "tree"] and fields[3] == PLANE_PATH:
        return fields[2]
    if fields[:2] == ["160000", "commit"] and fields[3] == LEGACY_PLANE_PATH:
        pinned = _git(repo, "rev-parse", "--verify", "--quiet", f"{fields[2]}^{{tree}}")
        return pinned.stdout.strip() if pinned.returncode == 0 and pinned.stdout.strip() else None
    return None


def choose_lane(repo: Path, base: str, head: str) -> str:
    candidate = plane_tree(repo, head)
    if candidate is None:
        raise ValueError("candidate has no exact Plane tree")
    return "smoke" if plane_tree(repo, base) == candidate else "full"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        selected = choose_lane(args.repo, args.base, args.head)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Plane lane unavailable: {exc}", file=sys.stderr)
        return 2
    with args.github_output.open("a", encoding="utf-8") as stream:
        stream.write(f"lane={selected}\n")
    print(f"Plane lane: {selected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
