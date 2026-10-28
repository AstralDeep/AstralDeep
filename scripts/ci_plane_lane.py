#!/usr/bin/env python3
"""Chooses which Plane suite ci.yml's plane-postgres job runs: the full suite when the
components/AstralPlane gitlink differs between the base and candidate Deep commits, else SMOKE_TESTS.
A zero, malformed, unknown or Plane-less base fails closed to the full suite.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

PLANE_PATH = "components/AstralPlane"
SMOKE_TESTS = ("tests/architecture", "tests/test_api.py", "tests/test_domain.py", "tests/test_bootstrap.py")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ZERO_SHA = "0" * 40


def plane_gitlink(repo: Path, revision: str) -> str | None:
    if SHA.fullmatch(revision) is None or revision == ZERO_SHA:
        return None
    result = subprocess.run(["git", "-C", str(repo), "ls-tree", f"{revision}^{{commit}}", "--", PLANE_PATH],
                            capture_output=True, text=True, timeout=120, check=False)
    fields = result.stdout.split()
    if result.returncode or len(fields) != 4 or fields[:2] != ["160000", "commit"] or fields[3] != PLANE_PATH:
        return None
    return fields[2]


def choose_lane(repo: Path, base: str, head: str) -> str:
    candidate = plane_gitlink(repo, head)
    if candidate is None:
        raise ValueError("candidate has no exact Plane gitlink")
    return "smoke" if plane_gitlink(repo, base) == candidate else "full"


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
