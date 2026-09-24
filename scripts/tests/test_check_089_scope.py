"""Tests for the repository migration scope guard: rejects native-client directory and
workflow changes across the five component repositories, using throwaway git
fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "scripts" / "verification" / "check_089_scope.py"
SPEC = importlib.util.spec_from_file_location("check_089_scope", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scope_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scope_tool
SPEC.loader.exec_module(scope_tool)

REPO_NAMES = sorted(scope_tool.DEFAULT_BASELINES)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    _write(path, "README.md", "baseline\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "baseline")
    return _git(path, "rev-parse", "HEAD")


def _commit(repo: Path, relative: str, text: str) -> None:
    _write(repo, relative, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "change " + relative)


@pytest.fixture()
def fleet(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    parent = tmp_path / "fleet"
    baselines = {name: _init_repo(parent / name) for name in REPO_NAMES}
    return parent, baselines


def _argv(parent: Path, baselines: dict[str, str], *extra: str) -> list[str]:
    arguments = ["--repo-parent", os.fspath(parent)]
    for name, sha in baselines.items():
        arguments += ["--baseline", name + "=" + sha]
    return arguments + list(extra)


def test_clean_fleet_passes(fleet: tuple[Path, dict[str, str]], capsys) -> None:
    parent, baselines = fleet
    assert scope_tool.main(_argv(parent, baselines)) == 0
    assert "RESULT: PASS" in capsys.readouterr().out


def test_ordinary_change_passes(fleet: tuple[Path, dict[str, str]], capsys) -> None:
    parent, baselines = fleet
    _commit(parent / "AstralDeep", "backend/orchestrator/typesafe_routing/client.py", "x\n")
    assert scope_tool.main(_argv(parent, baselines)) == 0
    assert "RESULT: PASS" in capsys.readouterr().out


@pytest.mark.parametrize(
    "relative",
    [
        "windows-client/App.xaml.cs",
        "android-client/app/build.gradle",
        "apple-clients/macOS/Info.plist",
        "components/AstralProjection/windows-client/App.xaml.cs",
    ],
)
def test_client_directory_change_fails(
    fleet: tuple[Path, dict[str, str]], relative: str, capsys
) -> None:
    parent, baselines = fleet
    _commit(parent / "AstralProjection", relative, "x\n")
    assert scope_tool.main(_argv(parent, baselines)) == 1
    output = capsys.readouterr().out
    assert "client-directory change: " + relative in output
    assert "RESULT: FAIL" in output


@pytest.mark.parametrize(
    "relative",
    [".github/workflows/ci.yml", "tooling/.github/workflows/nested.yml"],
)
def test_workflow_change_fails(
    fleet: tuple[Path, dict[str, str]], relative: str, capsys
) -> None:
    parent, baselines = fleet
    _commit(parent / "AstralDeep", relative, "on: push\n")
    assert scope_tool.main(_argv(parent, baselines)) == 1
    output = capsys.readouterr().out
    assert "workflow change: " + relative in output


def test_github_file_outside_workflows_passes(fleet: tuple[Path, dict[str, str]]) -> None:
    parent, baselines = fleet
    _commit(parent / "AstralDeep", ".github/CODEOWNERS", "* @owner\n")
    assert scope_tool.main(_argv(parent, baselines)) == 0


def test_uncommitted_change_needs_include_worktree(
    fleet: tuple[Path, dict[str, str]]
) -> None:
    parent, baselines = fleet
    _write(parent / "AstralProjection", "windows-client/App.xaml.cs", "x\n")
    assert scope_tool.main(_argv(parent, baselines)) == 0
    assert scope_tool.main(_argv(parent, baselines, "--include-worktree")) == 1


def test_missing_repository_reports_error(
    fleet: tuple[Path, dict[str, str]], capsys
) -> None:
    parent, baselines = fleet
    import shutil

    shutil.rmtree(parent / "LETS")
    assert scope_tool.main(_argv(parent, baselines)) == 2
    assert "ERROR  LETS" in capsys.readouterr().out


def test_unknown_baseline_reports_error(
    fleet: tuple[Path, dict[str, str]], capsys
) -> None:
    parent, baselines = fleet
    broken = dict(baselines)
    broken["LETS"] = "0" * 40
    assert scope_tool.main(_argv(parent, broken)) == 2
    assert "ERROR  LETS" in capsys.readouterr().out


def test_json_output_is_machine_readable(
    fleet: tuple[Path, dict[str, str]], capsys
) -> None:
    parent, baselines = fleet
    _commit(parent / "AstralProjection", "apple-clients/macOS/Info.plist", "x\n")
    assert scope_tool.main(_argv(parent, baselines, "--json")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert {entry["repository"] for entry in payload} == set(REPO_NAMES)
    projection = next(e for e in payload if e["repository"] == "AstralProjection")
    assert projection["ok"] is False
    assert projection["client_violations"] == ["apple-clients/macOS/Info.plist"]


def test_repo_path_override(fleet: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    parent, baselines = fleet
    relocated = tmp_path / "elsewhere" / "LETS"
    relocated.parent.mkdir(parents=True, exist_ok=True)
    (parent / "LETS").rename(relocated)
    arguments = _argv(parent, baselines, "--repo-path", "LETS=" + os.fspath(relocated))
    assert scope_tool.main(arguments) == 0


def test_rejects_malformed_and_unknown_assignments(
    fleet: tuple[Path, dict[str, str]]
) -> None:
    parent, baselines = fleet
    with pytest.raises(SystemExit):
        scope_tool.main(_argv(parent, baselines, "--baseline", "AstralDeep"))
    with pytest.raises(SystemExit):
        scope_tool.main(_argv(parent, baselines, "--baseline", "Nope=abc"))


def test_recorded_baselines_cover_the_five_repositories() -> None:
    assert REPO_NAMES == [
        "AstralDeep",
        "AstralPlane",
        "AstralPrimitives",
        "AstralProjection",
        "LETS",
    ]
