#!/usr/bin/env python3
"""Validate local targets in Git-tracked Markdown without network access."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit


_INLINE_LINK = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?P<target><[^>\n]+>|[^\s)]*)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
_REFERENCE_TARGET = re.compile(
    r"(?m)^ {0,3}\[[^\]\n]+\]:\s*(?P<target><[^>\n]+>|\S+)"
)
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(?P<heading>.+?)\s*#*\s*$")
_HTML_ID = re.compile(r"\b(?:id|name)=[\"'](?P<id>[^\"']+)[\"']")
_VALID_PERCENT = re.compile(r"%(?:[0-9A-Fa-f]{2})")
_EXTERNAL_SCHEMES = frozenset(
    {"app", "data", "gh", "http", "https", "mailto", "skill", "tel"}
)


class GitInventoryError(RuntimeError):
    """Raised when the tracked-file inventory cannot be read safely."""


@dataclass(frozen=True, order=True)
class LinkIssue:
    """One invalid local Markdown target."""

    source: str
    line: int
    target: str
    reason: str

    def render(self) -> str:
        """Return a stable compiler-style diagnostic."""

        return f"{self.source}:{self.line}: {self.reason}: {self.target}"


def git_tracked_files(repo_root: Path) -> tuple[PurePosixPath, ...]:
    """Return the repository's tracked files using Git's NUL-safe format."""

    completed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitInventoryError(message or "git ls-files failed")
    try:
        values = completed.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise GitInventoryError("git ls-files returned non-UTF-8 paths") from exc
    paths = tuple(
        sorted(PurePosixPath(value) for value in values if value)
    )
    return paths


def git_candidate_files(repo_root: Path) -> tuple[PurePosixPath, ...]:
    """Return tracked plus non-ignored candidate files for pre-commit checks.

    Sources remain restricted to tracked Markdown. Including non-ignored
    worktree targets lets a feature validate a newly added target before its
    eventual commit; CI's clean checkout then proves that target was actually
    included in the candidate.
    """

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitInventoryError(message or "git candidate inventory failed")
    try:
        values = completed.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise GitInventoryError("git candidate inventory returned non-UTF-8 paths") from exc
    candidates = tuple(sorted(PurePosixPath(value) for value in values if value))
    # ``--cached`` includes tracked paths deleted by an uncommitted cutover.
    # Validate the live candidate tree: removed sources disappear, while links
    # from surviving documents to those removed targets still fail normally.
    return tuple(
        path for path in candidates if repo_root.joinpath(*path.parts).exists()
    )


def tracked_markdown_files(
    repo_root: Path,
    requested: Sequence[str] = (),
) -> tuple[PurePosixPath, ...]:
    """Select tracked Markdown sources, optionally restricted by path prefix."""

    tracked = git_tracked_files(repo_root)
    markdown = tuple(path for path in tracked if path.suffix.lower() == ".md")
    if not requested:
        return markdown
    prefixes = tuple(PurePosixPath(value) for value in requested)
    selected = tuple(
        path
        for path in markdown
        if any(path == prefix or prefix in path.parents for prefix in prefixes)
    )
    missing = [
        str(prefix)
        for prefix in prefixes
        if not any(path == prefix or prefix in path.parents for path in selected)
    ]
    if missing:
        raise GitInventoryError(
            "requested Markdown path is not tracked: " + ", ".join(missing)
        )
    return selected


def maintained_markdown_files(
    tracked_files: Iterable[PurePosixPath],
) -> tuple[PurePosixPath, ...]:
    """Select current product/operator docs from a tracked-file inventory.

    Numbered ``specs/`` are immutable design history and intentionally retain
    links to files that existed at that feature's point in time. Generated
    agent/Spec-Kit instructions and ``CLAUDE.md`` are likewise not current
    operator documentation. ``--all`` remains available for an explicit
    historical audit.
    """

    excluded_roots = frozenset({".agents", ".specify", "specs"})
    return tuple(
        path
        for path in tracked_files
        if path.suffix.lower() == ".md"
        and path.name != "CLAUDE.md"
        and (not path.parts or path.parts[0] not in excluded_roots)
    )


def extract_markdown_targets(text: str) -> tuple[tuple[int, str], ...]:
    """Extract inline/image and reference-definition targets outside fences."""

    found: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        marker_match = re.match(r"(`{3,}|~{3,})", stripped)
        if marker_match:
            marker = marker_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        for pattern in (_INLINE_LINK, _REFERENCE_TARGET):
            for match in pattern.finditer(line):
                target = match.group("target")
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                found.append((line_number, target))
    return tuple(found)


def markdown_anchors(text: str) -> frozenset[str]:
    """Return GitHub-style heading anchors and explicit HTML IDs."""

    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        marker_match = re.match(r"(`{3,}|~{3,})", stripped)
        if marker_match:
            marker = marker_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        heading_match = _HEADING.match(line)
        if heading_match:
            heading = re.sub(r"<[^>]+>", "", heading_match.group("heading"))
            heading = re.sub(r"[`*_~]", "", heading).strip().lower()
            slug = "".join(
                character
                for character in heading
                if character.isalnum() or character in {" ", "-", "_"}
            )
            slug = re.sub(r"\s+", "-", slug)
            count = occurrences.get(slug, 0)
            occurrences[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
        for id_match in _HTML_ID.finditer(line):
            anchors.add(id_match.group("id"))
    return frozenset(anchors)


def validate_markdown_links(
    repo_root: Path,
    sources: Iterable[PurePosixPath],
    tracked_files: Iterable[PurePosixPath] | None = None,
) -> tuple[LinkIssue, ...]:
    """Validate local file/directory/anchor targets for selected sources.

    HTTP and other explicitly external schemes are intentionally not contacted.
    A local target must stay inside ``repo_root`` and exist in Git's tracked
    inventory, which makes the same link reproducible in a clean checkout.
    """

    root = repo_root.resolve()
    tracked = frozenset(
        tracked_files if tracked_files is not None else git_tracked_files(root)
    )
    tracked_strings = frozenset(path.as_posix() for path in tracked)
    issues: list[LinkIssue] = []

    @lru_cache(maxsize=None)
    def anchors_for(relative: str) -> frozenset[str]:
        return markdown_anchors((root / relative).read_text(encoding="utf-8"))

    for source in sources:
        source_text = source.as_posix()
        source_path = root / source_text
        try:
            text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            issues.append(LinkIssue(source_text, 1, source_text, str(exc)))
            continue
        for line, raw_target in extract_markdown_targets(text):
            issue = _validate_target(
                root=root,
                source=source,
                line=line,
                raw_target=raw_target,
                tracked=tracked_strings,
                anchors_for=anchors_for,
            )
            if issue is not None:
                issues.append(issue)
    return tuple(sorted(set(issues)))


def _validate_target(
    *,
    root: Path,
    source: PurePosixPath,
    line: int,
    raw_target: str,
    tracked: frozenset[str],
    anchors_for,
) -> LinkIssue | None:
    source_text = source.as_posix()
    if not raw_target:
        return LinkIssue(source_text, line, raw_target, "empty target")
    if raw_target.startswith("/"):
        return None  # application route, resolved by the deployed origin
    parsed = urlsplit(raw_target)
    scheme = parsed.scheme.lower()
    if scheme:
        if scheme in _EXTERNAL_SCHEMES:
            return None
        return LinkIssue(source_text, line, raw_target, "unsupported URI scheme")
    percent_without_escape = _VALID_PERCENT.sub("", raw_target)
    if "%" in percent_without_escape:
        return LinkIssue(source_text, line, raw_target, "invalid percent escape")
    path_text = unquote(parsed.path)
    fragment = unquote(parsed.fragment)
    relative_target = source if not path_text else source.parent / path_text
    resolved = (root / relative_target.as_posix()).resolve()
    try:
        repo_relative = PurePosixPath(resolved.relative_to(root).as_posix())
    except ValueError:
        return LinkIssue(source_text, line, raw_target, "target escapes repository")

    # Some historical project documents use repository-root-relative paths
    # without a leading slash. Accept that form only when the normal Markdown
    # relative target does not exist.
    if path_text and not resolved.exists():
        root_candidate = (root / path_text).resolve()
        if root_candidate.is_relative_to(root) and root_candidate.exists():
            resolved = root_candidate
            repo_relative = PurePosixPath(root_candidate.relative_to(root).as_posix())

    relative_text = repo_relative.as_posix()
    if not resolved.exists():
        return LinkIssue(source_text, line, raw_target, "target does not exist")
    if resolved.is_dir():
        prefix = relative_text.rstrip("/") + "/"
        if not any(value.startswith(prefix) for value in tracked):
            return LinkIssue(
                source_text, line, raw_target, "target directory has no tracked files"
            )
    elif relative_text not in tracked:
        return LinkIssue(source_text, line, raw_target, "target is not tracked")
    if fragment and resolved.is_file() and resolved.suffix.lower() == ".md":
        if fragment not in anchors_for(relative_text):
            return LinkIssue(source_text, line, raw_target, "anchor does not exist")
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Git worktree root (defaults to the script's repository)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include historical specs and generated instruction Markdown",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional tracked Markdown file or directory prefixes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run tracked-Markdown validation and return a process exit code."""

    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        tracked = git_tracked_files(repo_root)
        candidate_files = git_candidate_files(repo_root)
        if args.paths:
            sources = tracked_markdown_files(repo_root, args.paths)
        elif args.all:
            sources = tuple(
                path for path in tracked if path.suffix.lower() == ".md"
            )
        else:
            # Include non-ignored candidates so a newly authored guide is
            # validated before its first commit. CI runs from a clean checkout,
            # where the same files necessarily come from the tracked inventory.
            sources = maintained_markdown_files(candidate_files)
        issues = validate_markdown_links(repo_root, sources, candidate_files)
    except GitInventoryError as exc:
        print(f"documentation link check could not run: {exc}", file=sys.stderr)
        return 2
    if issues:
        for issue in issues:
            print(issue.render(), file=sys.stderr)
        print(
            f"documentation link check failed: {len(issues)} invalid target(s)",
            file=sys.stderr,
        )
        return 1
    print(f"documentation link check passed: {len(sources)} Markdown file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
