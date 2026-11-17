"""Tests for scripts/ci_plane_lane.py against real Git trees: the Plane lane is full when the plane/
tree changed between base and candidate, smoke otherwise, and full whenever the base cannot be read,
including a base from before the move that held Plane as a gitlink; ci.yml's plane-postgres job
must run exactly that lane.
"""

from pathlib import Path
import re
import subprocess

import pytest

from scripts import ci_plane_lane as lane

ROOT = Path(__file__).resolve().parents[2]
LEGACY_PLANE = "components/AstralPlane"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def commit_all(repo, note):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=CI", "-c", "user.email=ci@invalid", "commit", "-q", "-m", note)
    return git(repo, "rev-parse", "HEAD")


def commit(repo, plane_text, note):
    (repo / "plane").mkdir(exist_ok=True)
    (repo / "plane" / "pyproject.toml").write_text(plane_text, encoding="utf-8")
    (repo / "notes.txt").write_text(note, encoding="utf-8")
    return commit_all(repo, note)


def commit_gitlink(repo, gitlink, note):
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},{LEGACY_PLANE}")
    (repo / "notes.txt").write_text(note, encoding="utf-8")
    git(repo, "add", "notes.txt")
    git(repo, "-c", "user.name=CI", "-c", "user.email=ci@invalid", "commit", "-q", "-m", note)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "deep"
    path.mkdir()
    git(path, "init", "-q")
    return path


def chosen(repo, tmp_path, base, head):
    output = tmp_path / "github-output"
    code = lane.main(["--repo", str(repo), "--base", base, "--head", head, "--github-output", str(output)])
    return code, output.read_text(encoding="utf-8") if output.exists() else None


def test_plane_lane_is_full_when_a_plane_file_changed(repo, tmp_path):
    base = commit(repo, "a", "base")
    head = commit(repo, "b", "candidate")
    assert chosen(repo, tmp_path, base, head) == (0, "lane=full\n")


def test_plane_lane_is_smoke_when_the_plane_tree_is_unchanged(repo, tmp_path):
    base = commit(repo, "a", "base")
    head = commit(repo, "a", "candidate")
    assert chosen(repo, tmp_path, base, head) == (0, "lane=smoke\n")
    assert lane.choose_lane(repo, head, head) == "smoke"


def test_a_base_from_before_the_move_compares_its_gitlink_tree(repo, tmp_path):
    same = commit(repo, "a", "candidate with the same Plane tree")
    pinned = git(repo, "commit-tree", f"{same}:plane", "-m", "pinned Plane commit")
    git(repo, "rm", "-rq", "plane")
    base = commit_gitlink(repo, pinned, "base with a gitlink")
    git(repo, "update-index", "--force-remove", LEGACY_PLANE)
    again = commit(repo, "a", "candidate again")
    changed = commit(repo, "b", "candidate with a changed Plane file")
    assert chosen(repo, tmp_path, base, again) == (0, "lane=smoke\n")
    (tmp_path / "github-output").unlink()
    assert chosen(repo, tmp_path, base, changed) == (0, "lane=full\n")


def test_a_gitlink_commit_missing_from_the_object_store_gives_the_full_lane(repo, tmp_path):
    base = commit_gitlink(repo, "e" * 40, "base pointing at an absent commit")
    head = commit(repo, "a", "candidate")
    assert chosen(repo, tmp_path, base, head) == (0, "lane=full\n")


@pytest.mark.parametrize("base", ["0" * 40, "", "not-a-sha", "c" * 40])
def test_lane_fails_closed_on_a_zero_base_sha(repo, tmp_path, base):
    head = commit(repo, "a", "candidate")
    assert chosen(repo, tmp_path, base, head) == (0, "lane=full\n")


def test_lane_fails_closed_when_the_base_has_no_plane(repo, tmp_path):
    (repo / "notes.txt").write_text("before Plane", encoding="utf-8")
    base = commit_all(repo, "before")
    head = commit(repo, "a", "candidate")
    assert chosen(repo, tmp_path, base, head) == (0, "lane=full\n")


@pytest.mark.parametrize("head", ["0" * 40, "HEAD", "d" * 40])
def test_lane_refuses_a_candidate_without_an_exact_plane_tree(repo, tmp_path, head, capsys):
    base = commit(repo, "a", "base")
    assert chosen(repo, tmp_path, base, head) == (2, None)
    assert "Plane lane" in capsys.readouterr().err


def test_ci_runs_the_chosen_plane_lane_with_the_full_suite_gated_by_coverage():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    plane = re.search(r"(?ms)^  plane-postgres:\n(.*?)(?=^  [a-z][a-z-]*:\n)", workflow).group(1)
    assert "python scripts/ci_plane_lane.py" in plane and '--github-output "$GITHUB_OUTPUT"' in plane
    assert plane.index("prepare_plane_upgrade_baseline") < plane.index("ci_plane_lane.py")
    assert "PLANE_LANE: ${{ steps.lane.outputs.lane }}" in plane
    smoke, _, full = plane.partition('if [[ "$PLANE_LANE" == smoke ]]; then')[2].partition("else")
    full = full.partition("\n          fi\n")[0]
    assert "--cov-fail-under=88.75" in full and "--cov-branch" in full and "--cov" not in smoke
    assert [word for word in smoke.split() if word.startswith("tests/")] == list(lane.SMOKE_TESTS)
    assert "--junitxml=build/backend-web/plane-tests.xml" in smoke
