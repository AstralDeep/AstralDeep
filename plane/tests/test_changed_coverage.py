"""Tests for scripts/check_changed_coverage.py against real git repositories and real
diff-cover reports of BASE_SHA..HEAD: pass, fail and not-applicable decisions with the SHAs and
paths they record, fail-closed refusal of bad base SHAs and malformed or mismatched reports, and
working-tree-included reports accepted only on a clean repository.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import check_changed_coverage as checker

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_changed_coverage.py"
GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Synthetic Author",
    "GIT_AUTHOR_EMAIL": "author@example.test",
    "GIT_COMMITTER_NAME": "Synthetic Author",
    "GIT_COMMITTER_EMAIL": "author@example.test",
}
CI_OPTIONS = ("--diff-range-notation", "..", "--ignore-staged", "--ignore-unstaged")
MODULE = "package/module.py"
BASE_MODULE = "first = 1\nsecond = 2\n"
EXTENDED_MODULE = BASE_MODULE + "".join(f"value_{line} = {line}\n" for line in range(3, 13))
STATS = ("src_stats", MODULE)


def _environment() -> dict[str, str]:
    return {**os.environ, **GIT_ENVIRONMENT}


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch=main")
        self.commit({MODULE: BASE_MODULE, "README.md": "Synthetic project.\n"}, "Base")

    def git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            capture_output=True,
            check=True,
            encoding="utf-8",
            env=_environment(),
        )
        return completed.stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", message)
        return self.git("rev-parse", "HEAD")

    def diff_cover(
        self, hits: dict[int, int], base: str, options: tuple[str, ...] = CI_OPTIONS
    ) -> Path:
        lines = "".join(f'<line number="{line}" hits="{count}"/>' for line, count in hits.items())
        coverage = self.root.parent / "coverage.xml"
        coverage.write_text(
            f'<?xml version="1.0" ?>\n<coverage><sources><source>{self.root}</source></sources>'
            '<packages><package name="package"><classes>'
            f'<class name="module.py" filename="{MODULE}"><lines>{lines}</lines></class>'
            "</classes></package></packages></coverage>\n",
            encoding="utf-8",
        )
        report = self.root.parent / "diff-cover.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "diff_cover.diff_cover_tool",
                str(coverage),
                "--compare-branch",
                base,
                *options,
                "--format",
                f"json:{report}",
            ],
            cwd=self.root,
            capture_output=True,
            check=True,
            env=_environment(),
        )
        return report


def _arguments(report: Path, base: str) -> list[str]:
    return ["--report", str(report), f"--base-sha={base}", "--fail-under", "90"]


def _extended_hits(uncovered: tuple[int, ...]) -> dict[int, int]:
    return {line: 0 if line in uncovered else 1 for line in range(1, 13)}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GIT_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(checker.SUMMARY_ENVIRONMENT, raising=False)


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    repository = Repository(tmp_path / "repository")
    monkeypatch.chdir(repository.root)
    return repository


@pytest.fixture
def summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "summary.md"
    path.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv(checker.SUMMARY_ENVIRONMENT, str(path))
    return path


@pytest.fixture(scope="module")
def measured_report(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, dict[str, Any]]:
    repository = Repository(tmp_path_factory.mktemp("measured") / "repository")
    base = repository.git("rev-parse", "HEAD")
    repository.commit({MODULE: EXTENDED_MODULE}, "Extend module")
    report = repository.diff_cover(_extended_hits((12,)), base)
    return repository.root, base, json.loads(report.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "change",
    (
        {"README.md": "Synthetic project, revised.\n", "docs/guide.md": "Guide.\n"},
        {MODULE: BASE_MODULE + "# explanatory note\n"},
    ),
    ids=("documentation-only", "unmeasured-python-line"),
)
def test_pull_request_merge_without_measurable_lines_is_recorded_as_not_applicable(
    repository: Repository,
    summary: Path,
    change: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository.git("checkout", "--quiet", "-b", "feature")
    repository.commit(change, "Change")
    repository.git("checkout", "--quiet", "main")
    base = repository.commit({"NOTICE.md": "Main moved on.\n"}, "Advance main")
    repository.git("checkout", "--quiet", "--detach")
    repository.git("merge", "--quiet", "--no-ff", "--no-edit", "feature")
    candidate = repository.git("rev-parse", "HEAD")
    report = repository.diff_cover({1: 1, 2: 1}, base)

    assert checker.main(_arguments(report, base)) == 0

    assert json.loads(capsys.readouterr().out) == {
        "status": "not-applicable",
        "fail_under": 90.0,
        "base_sha": base,
        "candidate_sha": candidate,
        "changed_paths": sorted(change),
        "measured_paths": [],
        "measurable_lines": 0,
        "uncovered_lines": 0,
        "covered_percent": None,
    }
    recorded = summary.read_text(encoding="utf-8")
    assert recorded.startswith("earlier step\n## Changed-line coverage: not applicable\n")
    assert (
        f"between base `{base}` and candidate `{candidate}`, so the 90% threshold does not apply."
    ) in recorded
    assert f"Changed paths considered ({len(change)}):" in recorded
    assert "\n".join(("```text", *sorted(change), "```")) in recorded
    assert "Measured paths" not in recorded


@pytest.mark.parametrize(
    ("uncovered", "status", "exit_code", "percent"),
    (((12,), "pass", 0, "90.00"), ((11, 12), "fail", 1, "80.00")),
    ids=("at-threshold", "below-threshold"),
)
def test_pushed_range_is_held_to_the_threshold(
    repository: Repository,
    summary: Path,
    uncovered: tuple[int, ...],
    status: str,
    exit_code: int,
    percent: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = repository.git("rev-parse", "HEAD")
    candidate = repository.commit({MODULE: EXTENDED_MODULE}, "Extend module")
    report = repository.diff_cover(_extended_hits(uncovered), base)

    assert checker.main(_arguments(report, base)) == exit_code

    decision = json.loads(capsys.readouterr().out)
    assert decision["status"] == status
    assert (decision["base_sha"], decision["candidate_sha"]) == (base, candidate)
    assert decision["changed_paths"] == decision["measured_paths"] == [MODULE]
    assert (decision["measurable_lines"], decision["uncovered_lines"]) == (10, len(uncovered))
    assert decision["covered_percent"] == percent
    covered = 10 - len(uncovered)
    recorded = summary.read_text(encoding="utf-8")
    assert recorded.startswith(f"earlier step\n## Changed-line coverage: {status}\n")
    assert f"{covered} of 10 measurable changed lines are covered ({percent}%)" in recorded
    assert f"between base `{base}` and candidate `{candidate}`; the threshold is 90%." in recorded
    assert (
        f"{MODULE}: {covered} of 10 covered; uncovered {', '.join(map(str, uncovered))}" in recorded
    )


def test_fully_covered_change_lists_measured_paths_without_uncovered_lines(
    repository: Repository, summary: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repository.git("rev-parse", "HEAD")
    repository.commit({MODULE: EXTENDED_MODULE}, "Extend module")
    report = repository.diff_cover(_extended_hits(()), base)

    assert checker.main(_arguments(report, base)) == 0

    assert json.loads(capsys.readouterr().out)["covered_percent"] == "100.00"
    assert f"{MODULE}: 10 of 10 covered\n" in summary.read_text(encoding="utf-8")


def test_command_line_prints_the_decision_without_a_step_summary(repository: Repository) -> None:
    base = repository.git("rev-parse", "HEAD")
    repository.commit({"README.md": "Revised.\n"}, "Revise documentation")
    report = repository.diff_cover({1: 1, 2: 1}, base)

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *_arguments(report, base)],
        cwd=repository.root,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=_environment(),
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "not-applicable"


def test_base_equal_to_the_candidate_fails_closed(
    repository: Repository,
    summary: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    head = repository.git("rev-parse", "HEAD")

    assert checker.main(_arguments(tmp_path / "absent.json", head)) == 2

    assert "so the comparison identifies no change" in capsys.readouterr().err
    recorded = summary.read_text(encoding="utf-8")
    assert recorded.startswith("earlier step\n## Changed-line coverage: not decided\n")
    assert f"base and candidate are both {head}" in recorded


@pytest.mark.parametrize(
    ("base", "message"),
    (
        ("", "is not a non-zero 40-character lowercase hexadecimal SHA"),
        ("0" * 40, "is not a non-zero 40-character lowercase hexadecimal SHA"),
        ("abc1234", "is not a non-zero 40-character lowercase hexadecimal SHA"),
        ("A" * 40, "is not a non-zero 40-character lowercase hexadecimal SHA"),
        ("-q", "is not a non-zero 40-character lowercase hexadecimal SHA"),
        ("1" * 40, "git rev-parse failed"),
    ),
    ids=("empty", "all-zero", "abbreviated", "uppercase", "option-like", "absent-commit"),
)
def test_unusable_base_sha_fails_closed(
    repository: Repository,
    base: str,
    message: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository.commit({"README.md": "Revised.\n"}, "Revise documentation")

    assert checker.main(_arguments(tmp_path / "absent.json", base)) == 2

    assert message in capsys.readouterr().err


def test_tag_object_is_not_accepted_as_the_base_commit(
    repository: Repository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.git("tag", "--annotate", "--message", "Base release", "base-release")
    tag = repository.git("rev-parse", "base-release")
    repository.commit({"README.md": "Revised.\n"}, "Revise documentation")

    assert checker.main(_arguments(tmp_path / "absent.json", tag)) == 2

    assert f"base SHA {tag} names a tag, not a commit" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("report_base", "options", "uncommitted", "message"),
    (
        pytest.param(
            "base",
            ("--ignore-staged", "--ignore-unstaged"),
            False,
            "not '{base}..HEAD' with staged and unstaged changes ignored",
            id="three-dot-range",
        ),
        pytest.param(
            "base",
            ("--diff-range-notation", ".."),
            True,
            "while the repository has staged, unstaged, or untracked changes",
            id="uncommitted-working-tree",
        ),
        pytest.param(
            "previous",
            CI_OPTIONS,
            False,
            "not '{base}..HEAD' with staged and unstaged changes ignored",
            id="different-base",
        ),
    ),
)
def test_report_must_describe_the_same_committed_range(
    repository: Repository,
    report_base: str,
    options: tuple[str, ...],
    uncommitted: bool,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = repository.git("rev-parse", "HEAD")
    previous = repository.commit({"README.md": "Revised.\n"}, "Revise documentation")
    repository.commit({"docs/guide.md": "Guide.\n"}, "Add guide")
    if uncommitted:
        (repository.root / "docs" / "guide.md").write_text(
            "Uncommitted edit.\n", encoding="utf-8"
        )
    report = repository.diff_cover(
        {1: 1, 2: 1}, {"base": base, "previous": previous}[report_base], options
    )

    assert checker.main(_arguments(report, base)) == 2

    error = capsys.readouterr().err
    assert message.format(base=base, previous=previous) in error


def test_working_tree_included_report_on_a_clean_repository_is_accepted(
    repository: Repository,
    summary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = repository.git("rev-parse", "HEAD")
    candidate = repository.commit({MODULE: EXTENDED_MODULE}, "Extend module")
    report = repository.diff_cover(_extended_hits((12,)), base, ("--diff-range-notation", ".."))

    assert checker.main(_arguments(report, base)) == 0

    decision = json.loads(capsys.readouterr().out)
    assert (decision["status"], decision["base_sha"], decision["candidate_sha"]) == (
        "pass",
        base,
        candidate,
    )
    assert decision["measured_paths"] == [MODULE]
    assert decision["covered_percent"] == "90.00"


def _replace(*keys: str, value: object) -> Callable[[dict[str, Any]], object]:
    def mutate(document: dict[str, Any]) -> object:
        target = document
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
        return document

    return mutate


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        pytest.param(lambda document: [document], "must be an object with", id="not-an-object"),
        pytest.param(
            lambda document: {
                key: value for key, value in document.items() if key != "num_changed_lines"
            },
            "must be an object with",
            id="missing-field",
        ),
        pytest.param(_replace("diff_name", value=None), "compared None", id="unnamed-diff"),
        pytest.param(_replace("src_stats", value=[]), "must map paths", id="stats-not-mapping"),
        pytest.param(
            lambda document: {**document, "src_stats": {"package/other.py": {}}},
            "which the compared revisions do not change",
            id="unchanged-path",
        ),
        pytest.param(
            _replace(*STATS, value=[3]),
            f"statistics for {MODULE!r} must be an object",
            id="path-not-object",
        ),
        pytest.param(
            _replace(*STATS, "covered_lines", value="3"),
            "ascending positive integers",
            id="lines-not-list",
        ),
        pytest.param(
            _replace(*STATS, "covered_lines", value=[0, *range(4, 12)]),
            "ascending positive integers",
            id="line-zero",
        ),
        pytest.param(
            _replace(*STATS, "covered_lines", value=[True, *range(4, 12)]),
            "ascending positive integers",
            id="line-boolean",
        ),
        pytest.param(
            _replace(*STATS, "covered_lines", value=[4, 3, *range(5, 12)]),
            "ascending positive integers",
            id="lines-unsorted",
        ),
        pytest.param(
            _replace(*STATS, "covered_lines", value=[3, 3, *range(5, 12)]),
            "ascending positive integers",
            id="lines-duplicated",
        ),
        pytest.param(
            _replace(*STATS, "violation_lines", value=[11]),
            "must split measured lines",
            id="lines-overlap",
        ),
        pytest.param(
            _replace(*STATS, value={"covered_lines": [], "violation_lines": []}),
            "must split measured lines",
            id="path-without-lines",
        ),
        pytest.param(_replace("total_num_lines", value=11), "totals disagree", id="lines-total"),
        pytest.param(
            _replace("total_num_violations", value=2), "totals disagree", id="violations-total"
        ),
        pytest.param(
            _replace("total_num_lines", value=True),
            "total_num_lines must be a non-negative integer",
            id="total-boolean",
        ),
        pytest.param(
            _replace("total_num_violations", value=-1),
            "total_num_violations must be a non-negative integer",
            id="total-negative",
        ),
        pytest.param(
            _replace("num_changed_lines", value=9),
            "measured more lines than the diff changed",
            id="changed-lines-below-measured",
        ),
        pytest.param(
            _replace("num_changed_lines", value=10.0),
            "num_changed_lines must be a non-negative integer",
            id="changed-lines-float",
        ),
    ),
)
def test_malformed_report_fails_closed(
    measured_report: tuple[Path, str, dict[str, Any]],
    mutate: Callable[[dict[str, Any]], object],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base, document = measured_report
    report = tmp_path / "diff-cover.json"
    report.write_text(json.dumps(mutate(copy.deepcopy(document))), encoding="utf-8")
    monkeypatch.chdir(root)

    assert checker.main(_arguments(report, base)) == 2

    assert message in capsys.readouterr().err


def test_unmodified_measured_report_is_accepted(
    measured_report: tuple[Path, str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base, document = measured_report
    report = tmp_path / "diff-cover.json"
    report.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.chdir(root)

    assert checker.main(_arguments(report, base)) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "pass"


@pytest.mark.parametrize(
    "content", (None, b"{", b"\xff"), ids=("missing", "truncated-json", "not-utf-8")
)
def test_unreadable_report_fails_closed(
    measured_report: tuple[Path, str, dict[str, Any]],
    content: bytes | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base, _ = measured_report
    report = tmp_path / "diff-cover.json"
    if content is not None:
        report.write_bytes(content)
    monkeypatch.chdir(root)

    assert checker.main(_arguments(report, base)) == 2

    assert f"diff-cover report {report} is unreadable" in capsys.readouterr().err


def test_unwritable_step_summary_fails_closed(
    measured_report: tuple[Path, str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base, document = measured_report
    report = tmp_path / "diff-cover.json"
    report.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setenv(checker.SUMMARY_ENVIRONMENT, str(tmp_path))

    assert checker.main(_arguments(report, base)) == 2

    assert f"step summary {tmp_path} is not writable" in capsys.readouterr().err


def test_missing_git_fails_closed(
    measured_report: tuple[Path, str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base, _ = measured_report
    monkeypatch.chdir(root)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert checker.main(_arguments(tmp_path / "diff-cover.json", base)) == 2

    assert "git is unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("value", ("0", "100.01", "-5", "ninety", "NaN", "Infinity"))
def test_threshold_must_be_a_percentage(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        checker.main(["--report", "r.json", "--base-sha", "1" * 40, "--fail-under", value])

    assert raised.value.code == 2
    assert "argument --fail-under" in capsys.readouterr().err
