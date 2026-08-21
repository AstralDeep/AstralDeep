"""Regression tests for feature-074 repository migration guards."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPOSITORY_ROOT / "scripts" / "migration" / "preflight_074.ps1"
STAGED_GUARD = (
    REPOSITORY_ROOT / "scripts" / "migration" / "check_staged_paths_074.py"
)


def _run(
    arguments: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    completed = _run(["git", "-C", os.fspath(repo), *arguments])
    assert completed.returncode == 0, completed.stderr
    return completed


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Feature 074 Guard Test")
    _git(repo, "config", "user.email", "guard-test@example.invalid")
    (repo / "README.md").write_text("guard fixture\n", encoding="utf-8")
    _git(repo, "add", "--", "README.md")
    _git(repo, "commit", "-m", "Initialize guard fixture")
    return repo


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is required for migration preflight tests")
    return executable


def _run_preflight(
    repo: Path,
    *,
    expected_root: Path | None = None,
    expected_branch: str = "main",
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            os.fspath(PREFLIGHT),
            "-RepositoryRoot",
            os.fspath(repo),
            "-ExpectedRoot",
            os.fspath(expected_root or repo),
            "-ExpectedBranch",
            expected_branch,
        ]
    )


def _run_staged_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, os.fspath(STAGED_GUARD), "--repo", os.fspath(repo)]
    )


def _write_and_stage(repo: Path, relative_path: str) -> None:
    path = repo / Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("non-secret test fixture\n", encoding="utf-8")
    _git(repo, "add", "--", relative_path)


def test_preflight_accepts_exact_root_branch_and_clean_index(git_repo: Path) -> None:
    completed = _run_preflight(git_repo)

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["contract"] == "astral.migration-preflight/074-v1"
    assert receipt["branch"] == "main"
    assert receipt["index_clean"] is True
    assert receipt["reparse_points"] == 0


def test_preflight_refuses_wrong_exact_root(git_repo: Path, tmp_path: Path) -> None:
    wrong_root = tmp_path / "different-root"
    wrong_root.mkdir()

    completed = _run_preflight(git_repo, expected_root=wrong_root)

    assert completed.returncode != 0
    assert "exact-root mismatch" in completed.stderr


def test_preflight_refuses_wrong_branch(git_repo: Path) -> None:
    completed = _run_preflight(git_repo, expected_branch="codex/wrong-branch")

    assert completed.returncode != 0
    assert "expected-branch mismatch" in completed.stderr


def test_preflight_refuses_staged_index(git_repo: Path) -> None:
    _write_and_stage(git_repo, "staged.txt")

    completed = _run_preflight(git_repo)

    assert completed.returncode != 0
    assert "index is not clean" in completed.stderr


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise

    # Directory junctions do not require Developer Mode or symlink privilege.
    completed = _run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "& { param([string]$Link, [string]$Target) "
                "New-Item -ItemType Junction -LiteralPath $Link "
                "-Target $Target -ErrorAction Stop | Out-Null }"
            ),
            os.fspath(link),
            os.fspath(target),
        ]
    )
    if completed.returncode != 0:
        pytest.skip(f"cannot create a test reparse point: {completed.stderr}")


def test_preflight_refuses_reparse_point_beneath_root(
    git_repo: Path, tmp_path: Path
) -> None:
    target = tmp_path / "link-target"
    target.mkdir()
    _create_directory_link(git_repo / "linked-state", target)

    completed = _run_preflight(git_repo)

    assert completed.returncode != 0
    assert "reparse point" in completed.stderr.casefold()


def test_preflight_refuses_reparse_point_as_root(
    git_repo: Path, tmp_path: Path
) -> None:
    linked_root = tmp_path / "linked-repository-root"
    _create_directory_link(linked_root, git_repo)

    completed = _run_preflight(linked_root)

    assert completed.returncode != 0
    assert "repository root is a reparse point" in completed.stderr.casefold()


@pytest.mark.parametrize(
    ("relative_path", "expected_reason"),
    [
        (".env", "credential or private-key path"),
        ("app/app.db", "database or database-dump path"),
        ("app/app.db-wal", "database or database-dump path"),
        ("app/app.db-shm", "database or database-dump path"),
        ("app/app.sqlite-journal", "database or database-dump path"),
        ("app/app.sqlite3-wal", "database or database-dump path"),
        ("logs/server.log", "log path"),
        ("tmp_uploads/payload.bin", "upload path"),
        ("paper/submission/main.tex", "local manuscript or submission path"),
        ("results/run.json", "generated evidence path"),
        (".venv/pyvenv.cfg", "local environment or generated dependency path"),
        ("backend/knowledge/index.json", "runtime or user-state path"),
    ],
)
def test_staged_guard_refuses_sensitive_paths(
    git_repo: Path, relative_path: str, expected_reason: str
) -> None:
    _write_and_stage(git_repo, relative_path)

    completed = _run_staged_guard(git_repo)

    assert completed.returncode == 1
    assert f"DENY {relative_path}:" in completed.stderr
    assert expected_reason in completed.stderr


def test_staged_guard_refuses_generated_agent_directory_with_marker(
    git_repo: Path,
) -> None:
    agent_dir = git_repo / "backend" / "agents" / "generated_example"
    agent_dir.mkdir(parents=True)
    (agent_dir / ".draft").write_text("fixture-draft-id\n", encoding="utf-8")
    _write_and_stage(git_repo, "backend/agents/generated_example/mcp_tools.py")

    completed = _run_staged_guard(git_repo)

    assert completed.returncode == 1
    assert "generated-agent path" in completed.stderr


def test_staged_guard_accepts_source_path(git_repo: Path) -> None:
    _write_and_stage(git_repo, "src/astralplane/api.py")

    completed = _run_staged_guard(git_repo)

    assert completed.returncode == 0, completed.stderr
    assert "checked 1 staged path(s); no denied paths" in completed.stdout


def test_staged_guard_accepts_gradle_dependency_verification_metadata(
    git_repo: Path,
) -> None:
    _write_and_stage(git_repo, "android-client/gradle/verification-metadata.xml")

    completed = _run_staged_guard(git_repo)

    assert completed.returncode == 0, completed.stderr
    assert "checked 1 staged path(s); no denied paths" in completed.stdout


def test_staged_guard_still_refuses_generated_verification_directory(
    git_repo: Path,
) -> None:
    _write_and_stage(git_repo, "verification-run/result.json")

    completed = _run_staged_guard(git_repo)

    assert completed.returncode == 1
    assert "generated evidence path" in completed.stderr
