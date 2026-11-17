"""Turns diff-cover's JSON report for the committed range BASE_SHA..HEAD into the changed-line
coverage decision that .github/workflows/ci.yml records for every change. It verifies both
commits with git, accepts only reports that provably measure exactly that committed range, fails
closed on a malformed base SHA or a malformed or mismatched report, and prints and appends to the
GitHub step summary a pass, a fail, or an explicit not-applicable outcome.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SUMMARY_ENVIRONMENT = "GITHUB_STEP_SUMMARY"
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
ZERO_SHA = "0" * 40
COMMITTED_DIFF = "{base}..HEAD"
WORKING_TREE_DIFF_SUFFIX = ", staged and unstaged changes"
REPORT_FIELDS = (
    "diff_name",
    "src_stats",
    "total_num_lines",
    "total_num_violations",
    "num_changed_lines",
)


class CoverageDecisionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Comparison:
    base_sha: str
    candidate_sha: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MeasuredPath:
    path: str
    covered_lines: tuple[int, ...]
    uncovered_lines: tuple[int, ...]


def _line_totals(measured: tuple[MeasuredPath, ...]) -> tuple[int, int]:
    covered = sum(len(item.covered_lines) for item in measured)
    uncovered = sum(len(item.uncovered_lines) for item in measured)
    return covered + uncovered, uncovered


@dataclass(frozen=True, slots=True)
class Decision:
    comparison: Comparison
    fail_under: Decimal
    measured: tuple[MeasuredPath, ...]

    @property
    def status(self) -> str:
        measurable, uncovered = _line_totals(self.measured)
        if not measurable:
            return "not-applicable"
        covered = Decimal(measurable - uncovered)
        return "pass" if covered * 100 >= self.fail_under * measurable else "fail"

    @property
    def covered_percent(self) -> str | None:
        measurable, uncovered = _line_totals(self.measured)
        if not measurable:
            return None
        percent = Decimal(measurable - uncovered) * 100 / measurable
        return str(percent.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))

    def as_dict(self) -> dict[str, Any]:
        measurable, uncovered = _line_totals(self.measured)
        return {
            "status": self.status,
            "fail_under": float(self.fail_under),
            "base_sha": self.comparison.base_sha,
            "candidate_sha": self.comparison.candidate_sha,
            "changed_paths": list(self.comparison.changed_paths),
            "measured_paths": [item.path for item in self.measured],
            "measurable_lines": measurable,
            "uncovered_lines": uncovered,
            "covered_percent": self.covered_percent,
        }

    def markdown(self) -> str:
        comparison = self.comparison
        measurable, uncovered = _line_totals(self.measured)
        revisions = f"base `{comparison.base_sha}` and candidate `{comparison.candidate_sha}`"
        if not measurable:
            lines = [
                "## Changed-line coverage: not applicable",
                "",
                f"No measurable executable lines changed between {revisions}, so the "
                f"{self.fail_under}% threshold does not apply.",
            ]
        else:
            lines = [
                f"## Changed-line coverage: {self.status}",
                "",
                f"{measurable - uncovered} of {measurable} measurable changed lines are covered "
                f"({self.covered_percent}%) between {revisions}; the threshold is "
                f"{self.fail_under}%.",
            ]
        lines += [
            "",
            f"Changed paths considered ({len(comparison.changed_paths)}):",
            "",
            *_block(comparison.changed_paths),
        ]
        if self.measured:
            lines += ["", "Measured paths:", "", *_block(map(_measured_line, self.measured))]
        return "\n".join(lines) + "\n"


def _measured_line(item: MeasuredPath) -> str:
    total = len(item.covered_lines) + len(item.uncovered_lines)
    line = f"{item.path}: {len(item.covered_lines)} of {total} covered"
    if item.uncovered_lines:
        line += "; uncovered " + ", ".join(str(number) for number in item.uncovered_lines)
    return line


def _block(items: Iterable[str]) -> list[str]:
    rendered = [json.dumps(item, ensure_ascii=False)[1:-1] for item in items]
    return ["```text", *rendered, "```"] if rendered else ["none"]


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            encoding="utf-8",
        )
    except OSError as error:
        raise CoverageDecisionError(f"git is unavailable: {error}") from None
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise CoverageDecisionError(f"git {arguments[0]} failed: {detail}")
    return completed.stdout


def _has_uncommitted_changes(repository: Path) -> bool:
    return _git(repository, "status", "--porcelain") != ""


def resolve_comparison(repository: Path, base_sha: str) -> Comparison:
    if COMMIT_SHA.fullmatch(base_sha) is None or base_sha == ZERO_SHA:
        raise CoverageDecisionError(
            f"base SHA {base_sha!r} is not a non-zero 40-character lowercase hexadecimal SHA"
        )
    if _git(repository, "rev-parse", "--verify", f"{base_sha}^{{commit}}").strip() != base_sha:
        raise CoverageDecisionError(f"base SHA {base_sha} names a tag, not a commit")
    candidate_sha = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if candidate_sha == base_sha:
        raise CoverageDecisionError(
            f"base and candidate are both {base_sha}, so the comparison identifies no change"
        )
    listing = _git(repository, "diff", "--name-only", "-z", base_sha, candidate_sha)
    return Comparison(
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        changed_paths=tuple(sorted(path for path in listing.split("\0") if path)),
    )


def _count(document: dict[str, Any], field: str) -> int:
    value = document[field]
    if type(value) is not int or value < 0:
        raise CoverageDecisionError(f"diff-cover {field} must be a non-negative integer")
    return value


def _lines(value: object, path: str) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or any(type(line) is not int or line < 1 for line in value)
        or value != sorted(set(value))
    ):
        raise CoverageDecisionError(
            f"diff-cover line numbers for {path!r} must be ascending positive integers"
        )
    return tuple(value)


def _measured_path(path: str, stats: object, changed: frozenset[str]) -> MeasuredPath:
    if path not in changed:
        raise CoverageDecisionError(
            f"diff-cover measured {path!r}, which the compared revisions do not change"
        )
    if not isinstance(stats, dict):
        raise CoverageDecisionError(f"diff-cover statistics for {path!r} must be an object")
    covered = _lines(stats.get("covered_lines"), path)
    uncovered = _lines(stats.get("violation_lines"), path)
    if (not covered and not uncovered) or set(covered) & set(uncovered):
        raise CoverageDecisionError(
            f"diff-cover statistics for {path!r} must split measured lines into covered and "
            "uncovered"
        )
    return MeasuredPath(path, covered, uncovered)


def load_report(
    path: Path,
    comparison: Comparison,
    repository: Path,
) -> tuple[MeasuredPath, ...]:
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as error:
        raise CoverageDecisionError(f"diff-cover report {path} is unreadable: {error}") from None
    if not isinstance(document, dict) or any(field not in document for field in REPORT_FIELDS):
        raise CoverageDecisionError(
            f"diff-cover report {path} must be an object with {', '.join(REPORT_FIELDS)}"
        )
    expected_diff = COMMITTED_DIFF.format(base=comparison.base_sha)
    working_tree_diff = f"{expected_diff}{WORKING_TREE_DIFF_SUFFIX}"
    if document["diff_name"] not in (expected_diff, working_tree_diff):
        raise CoverageDecisionError(
            f"diff-cover compared {document['diff_name']!r}, not {expected_diff!r} "
            "with staged and unstaged changes ignored"
        )
    if document["diff_name"] == working_tree_diff and _has_uncommitted_changes(repository):
        raise CoverageDecisionError(
            f"diff-cover compared {working_tree_diff!r} while the repository has staged, "
            "unstaged, or untracked changes, so the measured lines are not the committed "
            "range alone; commit them or rerun diff-cover with --ignore-staged --ignore-unstaged"
        )
    stats = document["src_stats"]
    if not isinstance(stats, dict):
        raise CoverageDecisionError("diff-cover src_stats must map paths to line statistics")
    changed = frozenset(comparison.changed_paths)
    measured = tuple(_measured_path(name, stats[name], changed) for name in sorted(stats))
    measurable, uncovered = _line_totals(measured)
    if (
        _count(document, "total_num_lines") != measurable
        or _count(document, "total_num_violations") != uncovered
    ):
        raise CoverageDecisionError("diff-cover totals disagree with its per-path line statistics")
    if _count(document, "num_changed_lines") < measurable:
        raise CoverageDecisionError("diff-cover measured more lines than the diff changed")
    return measured


def _threshold(value: str) -> Decimal:
    try:
        threshold = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{value!r} is not a percentage") from None
    if not threshold.is_finite() or not 0 < threshold <= 100:
        raise argparse.ArgumentTypeError(f"{value!r} is not a percentage above 0 and at most 100")
    return threshold


def _append_summary(path: str | None, markdown: str) -> None:
    if path is None:
        return
    try:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(markdown)
    except OSError as error:
        raise CoverageDecisionError(f"step summary {path} is not writable: {error}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--fail-under", type=_threshold, required=True)
    args = parser.parse_args(argv)
    summary = os.environ.get(SUMMARY_ENVIRONMENT) or None
    try:
        comparison = resolve_comparison(Path.cwd(), args.base_sha)
        decision = Decision(
            comparison, args.fail_under, load_report(args.report, comparison, Path.cwd())
        )
        print(json.dumps(decision.as_dict(), indent=2, sort_keys=True))
        _append_summary(summary, decision.markdown())
    except CoverageDecisionError as error:
        print(f"changed-line coverage could not be decided: {error}", file=sys.stderr)
        with contextlib.suppress(CoverageDecisionError):
            _append_summary(summary, f"## Changed-line coverage: not decided\n\n{error}\n")
        return 2
    return 1 if decision.status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
