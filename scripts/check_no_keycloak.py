#!/usr/bin/env python3
"""Fails when tracked product files mention Keycloak, its host or its endpoint shape, except for paths the config lists as pending removal.
It reads a repository's `git ls-files`, a JSON config of exclusions and pending entries, and is run as each removal task's last command."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

PATTERNS = (
    re.compile(r"keycloak", re.IGNORECASE),
    re.compile(r"iam\.ai\.uky\.edu", re.IGNORECASE),
    re.compile(r"/protocol/openid-connect/", re.IGNORECASE),
)
SNIFF_BYTES = 8192


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    out: List[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _tracked_files(repo: Path) -> List[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [p for p in result.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _first_hit(text: str) -> Optional[str]:
    for pattern in PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


def _content_hits(path: Path) -> List[tuple]:
    try:
        if not path.is_file() or path.is_symlink():
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:SNIFF_BYTES]:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return [(n, line.strip()) for n, line in enumerate(lines, 1) if _first_hit(line)]


def find_hits(repo: Path, excludes: Sequence[str]) -> Dict[str, List[str]]:
    excluded = [_glob_regex(p) for p in excludes]
    hits: Dict[str, List[str]] = {}
    for rel in _tracked_files(repo):
        if any(rx.match(rel) for rx in excluded):
            continue
        found: List[str] = []
        if _first_hit(rel):
            found.append(f"{rel}: path names an identity server")
        for number, line in _content_hits(repo / rel):
            found.append(f"{rel}:{number}: {line[:160]}")
        if found:
            hits[rel] = found
    return hits


def _bound_problem(repo: Path, entry: dict) -> Optional[str]:
    bound = entry.get("bound_by")
    if entry.get("until") != "T8.6":
        return None
    if not bound:
        return f"{entry['path']}: T8.6 entry has no bound_by"
    holder = repo / bound
    name = Path(entry["path"]).name
    stem = Path(entry["path"]).stem
    try:
        text = holder.read_text("utf-8", "replace")
    except OSError:
        return f"{entry['path']}: bound_by {bound} cannot be read"
    if name not in text and stem not in text:
        return f"{entry['path']}: bound_by {bound} does not name {name}"
    return None


def check(repo: Path, config: dict) -> List[str]:
    hits = find_hits(repo, config.get("exclude", []))
    pending = {entry["path"]: entry for entry in config.get("pending", [])}
    failures: List[str] = []
    for rel, lines in sorted(hits.items()):
        if rel not in pending:
            failures.extend(lines)
    for rel, entry in sorted(pending.items()):
        if rel not in hits:
            failures.append(f"{rel}: pending entry has no hit and is stale")
        problem = _bound_problem(repo, entry)
        if problem:
            failures.append(problem)
    return failures


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    config = json.loads(Path(args.config).read_text("utf-8"))
    failures = check(Path(args.repo), config)
    if args.json:
        print(json.dumps({"ok": not failures, "failures": failures}, indent=2))
    else:
        for line in failures:
            print(line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
