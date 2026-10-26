"""Renders AstralDeep Mac release notes from the pull requests merged along main's first-parent history since the previous release.
The release-macos workflow writes them for release_macos.py, which embeds them in the appcast and the GitHub release.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

LIMIT = 64 * 1024
COMMIT = re.compile(r"[a-f0-9]{40}")
VERSION = re.compile(r"[1-9]\d*\.\d+(?:\.\d+)?")
MERGE = re.compile(r"Merge pull request #([1-9]\d*) from ")
MORE = "- …and more."
MAINTENANCE = "- Maintenance release."


class NotesError(RuntimeError):
    pass


def render_notes(version: str, build: int, entries: list[tuple[int | None, str]]) -> str:
    header = f"## AstralDeep {version} ({build})\n\n"
    lines: list[str] = []
    for number, title in entries:
        text = html.escape(" ".join(title.split()), quote=False)
        line = f"- {text} (#{number})" if number is not None else f"- {text}"
        if text and line not in lines:
            lines.append(line)
    if not lines:
        return f"{header}{MAINTENANCE}\n"
    body = "".join(f"{line}\n" for line in lines)
    if len((header + body).encode()) <= LIMIT:
        return header + body
    budget = LIMIT - len(header.encode()) - len(f"{MORE}\n".encode())
    kept = []
    for line in lines:
        budget -= len(f"{line}\n".encode())
        if budget < 0:
            break
        kept.append(f"{line}\n")
    return f"{header}{''.join(kept)}{MORE}\n"


def entries_from_compare(commits: list[dict], head: str | None) -> list[tuple[int | None, str]]:
    if not commits:
        return []
    by_sha = {commit["sha"]: commit for commit in commits}
    chain = []
    current = head if head in by_sha else commits[-1]["sha"]
    while current in by_sha and len(chain) < len(by_sha):
        commit = by_sha[current]
        chain.append(commit)
        parents = commit["parents"]
        current = parents[0]["sha"] if parents else None
    entries: list[tuple[int | None, str]] = []
    for commit in reversed(chain):
        lines = commit["commit"]["message"].splitlines()
        first = lines[0] if lines else ""
        merged = MERGE.match(first)
        if merged:
            title = lines[2] if len(lines) > 2 and lines[2].strip() else first
            entries.append((int(merged.group(1)), title))
        elif not first.startswith("Merge branch"):
            entries.append((None, first))
    return entries


def compare(repo: str, base: str, head: str) -> list[dict]:
    path = f"repos/{repo}/compare/{base}...{head}?per_page=100"
    done = subprocess.run(["gh", "api", "--paginate", "--slurp", path], capture_output=True, text=True, check=False)
    if done.returncode:
        raise NotesError(f"gh api {path} failed: {done.stderr.strip()[-500:]}")
    try:
        return [commit for page in json.loads(done.stdout) for commit in page["commits"]]
    except (ValueError, KeyError, TypeError) as error:
        raise NotesError(f"GitHub compare for {base}...{head} is unreadable") from error


def run(args: argparse.Namespace) -> str:
    if (args.base and not COMMIT.fullmatch(args.base)) or not COMMIT.fullmatch(args.head):
        raise NotesError("--base and --head must be full lowercase commit hashes")
    if not VERSION.fullmatch(args.version):
        raise NotesError("--version must look like 2.0 or 2.0.1")
    if args.build < 1:
        raise NotesError("--build must be a positive integer")
    if not args.base:
        return render_notes(args.version, args.build, [])
    commits = compare(args.repo, args.base, args.head)
    try:
        entries = entries_from_compare(commits, args.head)
    except (KeyError, TypeError, IndexError) as error:
        raise NotesError(f"GitHub compare for {args.base}...{args.head} is unreadable") from error
    return render_notes(args.version, args.build, entries)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = run(args)
    except NotesError as error:
        print(f"Mac release notes failed: {error}", file=sys.stderr)
        return 1
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
