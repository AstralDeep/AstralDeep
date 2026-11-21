"""Exercises consumer-selected Plane baseline preparation with real immutable Git trees.
The previous Plane source stays authoritative whether the base held it as a pinned gitlink or in the plane/ folder, and invalid event or source identities fail closed.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import prepare_plane_upgrade_baseline as baseline
from scripts.check_changed_coverage import CoveragePolicyError, select_revisions


def git(repo, *args):
    return (
        subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
        .decode()
        .strip()
    )


def initialize(repo):
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Synthetic CI")
    git(repo, "config", "user.email", "synthetic@example.test")


def commit(repo, message="Synthetic source"):
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def pinned_manifest(sha):
    return {
        "format": "astral.composition/v1",
        "components": {
            "astral-plane": {
                "repository": baseline.PLANE_REPOSITORY,
                "path": baseline.LEGACY_PLANE_PATH,
                "commit": sha,
                "contract_version": "astralplane.contract/v1",
            }
        },
    }


def in_tree_manifest():
    return {
        "format": "astral.composition/v2",
        "components": {},
        "in_tree": {
            "astral-plane": {
                "path": baseline.PLANE_PATH,
                "contract_version": "astralplane.contract/v1",
            }
        },
    }


def write_manifest(deep, document):
    (deep / "config").mkdir(exist_ok=True)
    (deep / baseline.MANIFEST_PATH).write_text(json.dumps(document))


def write_plane(deep, version):
    package = deep / baseline.PLANE_PATH / "src/astralplane"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(f'VERSION = "{version}"\n')


def selection(base, head, event="pull_request"):
    payload = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    if event == "push":
        payload = {"ref": "refs/heads/main", "before": base, "after": head}
    return select_revisions(
        event_name=event, event_payload=payload, base_sha=None, candidate_sha=None
    )


@pytest.fixture
def sources(tmp_path):
    plane = tmp_path / "plane-source"
    initialize(plane)
    package = plane / "src/astralplane"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VERSION = "previous"\n')
    previous = commit(plane)
    deep = tmp_path / "deep"
    initialize(deep)
    git(deep, "fetch", "--no-tags", str(plane), previous)
    write_manifest(deep, pinned_manifest(previous))
    git(deep, "add", baseline.MANIFEST_PATH)
    git(deep, "update-index", "--add", "--cacheinfo",
        f"160000,{previous},{baseline.LEGACY_PLANE_PATH}")
    git(deep, "commit", "-m", "Pre-move consumer pin")
    pre_move_base = git(deep, "rev-parse", "HEAD")
    git(deep, "update-index", "--force-remove", baseline.LEGACY_PLANE_PATH)
    write_manifest(deep, in_tree_manifest())
    write_plane(deep, "previous")
    post_move_base = commit(deep, "Move Plane in tree")
    write_plane(deep, "candidate")
    changed = commit(deep, "Change Plane")
    git(deep, "checkout", "-q", "-b", "same", post_move_base)
    (deep / "change.txt").write_text("consumer change\n")
    unchanged = commit(deep, "Change outside Plane")
    git(deep, "checkout", "-q", "main")
    return deep, plane, pre_move_base, post_move_base, changed, unchanged, previous


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_post_move_base_is_isolated_as_a_synthetic_plane_commit(sources, tmp_path, event):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", changed)
    report = baseline.prepare(deep, selection(base, changed, event), tmp_path / "isolated.git")
    isolated = Path(report["baseline_root"])
    assert report["status"] == "prepared"
    assert report["base_deep_sha"] == base and report["candidate_deep_sha"] == changed
    assert report["baseline_plane_tree"] == git(deep, "rev-parse", f"{base}:plane")
    assert report["candidate_plane_tree"] == git(deep, "rev-parse", f"{changed}:plane")
    assert report["baseline_plane_tree"] != report["candidate_plane_tree"]
    assert report["baseline_source_tree"] == git(deep, "rev-parse", f"{base}:plane/src")
    assert report["candidate_source_tree"] == git(deep, "rev-parse", f"{changed}:plane/src")
    assert git(isolated, "rev-parse", "refs/remotes/origin/main") == report["baseline_plane_sha"]
    assert git(isolated, "rev-parse", f"{report['baseline_plane_sha']}^{{tree}}") == (
        report["baseline_plane_tree"]
    )
    assert git(isolated, "show", f"{report['baseline_plane_sha']}:src/astralplane/__init__.py") == (
        'VERSION = "previous"'
    )
    assert report["baseline_manifest_sha256"] == hashlib.sha256(
        subprocess.check_output(["git", "-C", str(deep), "show", f"{base}:{baseline.MANIFEST_PATH}"])
    ).hexdigest()


def test_the_synthetic_baseline_commit_is_deterministic(sources, tmp_path):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", changed)
    first = baseline.prepare(deep, selection(base, changed), tmp_path / "one.git")
    second = baseline.prepare(deep, selection(base, changed), tmp_path / "two.git")
    assert first["baseline_plane_sha"] == second["baseline_plane_sha"]


def test_an_unchanged_plane_tree_is_reported_as_identical(sources, tmp_path):
    deep, _, _, base, _, unchanged, _ = sources
    git(deep, "checkout", "-q", unchanged)
    report = baseline.prepare(deep, selection(base, unchanged), tmp_path / "isolated.git")
    assert report["baseline_plane_tree"] == report["candidate_plane_tree"]


def test_pre_move_base_binds_its_pinned_commit_from_deeps_own_objects(sources, tmp_path):
    deep, plane, pre_move, _, changed, _, previous = sources
    git(deep, "checkout", "-q", changed)
    report = baseline.prepare(deep, selection(pre_move, changed), tmp_path / "isolated.git")
    isolated = Path(report["baseline_root"])
    assert report["baseline_plane_sha"] == previous
    assert git(isolated, "rev-parse", "refs/remotes/origin/main") == previous
    assert report["baseline_plane_tree"] == git(plane, "rev-parse", f"{previous}^{{tree}}")
    assert git(isolated, "show", f"{previous}:src/astralplane/__init__.py") == 'VERSION = "previous"'


def test_a_pin_missing_from_deeps_objects_is_fetched_from_the_canonical_repository(
    sources, tmp_path, monkeypatch
):
    deep, plane, _, _, changed, _, previous = sources
    other = tmp_path / "other"
    initialize(other)
    git(other, "fetch", "--no-tags", str(deep), changed)
    git(other, "checkout", "-q", changed)
    write_manifest(other, pinned_manifest(previous))
    git(other, "add", baseline.MANIFEST_PATH)
    git(other, "update-index", "--add", "--cacheinfo",
        f"160000,{previous},{baseline.LEGACY_PLANE_PATH}")
    git(other, "rm", "-rq", "--cached", baseline.PLANE_PATH)
    git(other, "commit", "-m", "Pre-move base without the pinned objects")
    pre_move = git(other, "rev-parse", "HEAD")
    write_manifest(other, in_tree_manifest())
    git(other, "update-index", "--force-remove", baseline.LEGACY_PLANE_PATH)
    git(other, "checkout", "-q", changed, "--", baseline.PLANE_PATH)
    head = commit(other, "Candidate")
    assert subprocess.run(["git", "-C", str(other), "cat-file", "-e", previous]).returncode != 0
    monkeypatch.setattr(baseline, "ARCHIVED_SOURCE", str(plane))
    report = baseline.prepare(other, selection(pre_move, head), tmp_path / "isolated.git")
    assert report["baseline_plane_sha"] == previous


def test_committed_manifest_is_used_instead_of_working_copy(sources, tmp_path):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", changed)
    (deep / baseline.MANIFEST_PATH).write_text("malformed uncommitted input")
    git(deep, "checkout", "--", baseline.MANIFEST_PATH)
    report = baseline.prepare(deep, selection(base, changed), tmp_path / "isolated.git")
    assert report["status"] == "prepared"


def test_invalid_previous_consumer_manifest_fails_closed(sources, tmp_path):
    deep, _, _, base, _, _, _ = sources
    git(deep, "checkout", "-q", "-b", "invalid-base", base)
    (deep / baseline.MANIFEST_PATH).write_text("[]")
    broken = commit(deep)
    (deep / "change.txt").write_text("candidate\n")
    head = commit(deep)
    with pytest.raises(ValueError, match="no components"):
        baseline.prepare(deep, selection(broken, head), tmp_path / "isolated.git")


@pytest.mark.parametrize(
    "damage",
    ["repository", "path", "commit", "contract_version", "missing", "malformed", "duplicate",
     "gitlink"],
)
def test_invalid_pinned_base_fails_before_preparation(sources, tmp_path, damage):
    deep, _, pre_move, _, changed, _, previous = sources
    git(deep, "checkout", "-q", "-b", "damaged", pre_move)
    data = pinned_manifest(previous)
    entry = data["components"]["astral-plane"]
    entry.update({"repository": "https://example.test/wrong.git"} if damage == "repository" else {})
    entry.update({"path": "other/Plane"} if damage == "path" else {})
    entry.update({"commit": "main"} if damage == "commit" else {})
    entry.update({"contract_version": "unsupported"} if damage == "contract_version" else {})
    if damage == "missing":
        data = {}
    content = json.dumps(data)
    if damage == "malformed":
        content = "{"
    if damage == "duplicate":
        content = '{"components": {}, "components": {}}'
    (deep / baseline.MANIFEST_PATH).write_text(content)
    if damage == "gitlink":
        git(deep, "update-index", "--cacheinfo", f"160000,{'a' * 40},{baseline.LEGACY_PLANE_PATH}")
    damaged_base = commit(deep, "Damaged base")
    git(deep, "checkout", "-q", changed)
    destination = tmp_path / "isolated.git"
    with pytest.raises((ValueError, CoveragePolicyError)):
        baseline.prepare(deep, selection(damaged_base, changed), destination)
    assert not destination.exists()


@pytest.mark.parametrize("damage", ["identity", "folder", "format"])
def test_invalid_in_tree_base_fails_before_preparation(sources, tmp_path, damage):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", "-b", "damaged", base)
    data = in_tree_manifest()
    if damage == "identity":
        data["in_tree"]["astral-plane"]["path"] = "elsewhere"
    if damage == "format":
        data["format"] = "astral.composition/v3"
    write_manifest(deep, data)
    if damage == "folder":
        git(deep, "rm", "-rq", baseline.PLANE_PATH)
        (deep / "plane").write_text("a file, not a folder\n")
    damaged_base = commit(deep, "Damaged base")
    git(deep, "checkout", "-q", changed)
    destination = tmp_path / "isolated.git"
    with pytest.raises((ValueError, CoveragePolicyError)):
        baseline.prepare(deep, selection(damaged_base, changed), destination)
    assert not destination.exists()


def test_a_candidate_that_still_pins_plane_is_refused(sources, tmp_path):
    deep, _, pre_move, _, _, _, _ = sources
    git(deep, "checkout", "-q", "-b", "still-pinned", pre_move)
    (deep / "change.txt").write_text("consumer change\n")
    head = commit(deep, "Candidate that still pins Plane")
    with pytest.raises(ValueError, match="repository tree"):
        baseline.prepare(deep, selection(pre_move, head), tmp_path / "isolated.git")


@pytest.mark.parametrize(
    "damage",
    ["head", "dirty", "untracked", "zero", "missing", "nonancestor", "destination",
     "inside-component"],
)
def test_invalid_source_or_event_identity_fails_closed(sources, tmp_path, damage):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", changed)
    selected = selection(base, changed)
    destination = tmp_path / "isolated.git"
    if damage == "head":
        git(deep, "checkout", "-q", base)
    elif damage == "dirty":
        (deep / "plane/src/astralplane/__init__.py").write_text("changed = True\n")
    elif damage == "untracked":
        (deep / "plane/src/astralplane/extra.py").write_text("changed = True\n")
    elif damage == "zero":
        selected = selection("0" * 40, changed)
    elif damage == "missing":
        selected = selection("a" * 40, changed)
    elif damage == "nonancestor":
        selected = selection(changed, base)
    elif damage == "destination":
        destination.mkdir()
    elif damage == "inside-component":
        destination = deep / "plane" / "baseline.git"
    with pytest.raises((ValueError, CoveragePolicyError, FileExistsError)):
        baseline.prepare(deep, selected, destination)


def test_main_emits_environment_and_bound_receipt(sources, tmp_path, monkeypatch):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", changed)
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"ref": "refs/heads/main", "before": base, "after": changed}))
    env = tmp_path / "github-env"
    report = tmp_path / "report.json"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload))
    assert baseline.main(["--repo", str(deep), "--baseline-root", str(tmp_path / "isolated.git"),
                          "--github-env", str(env), "--output", str(report)]) == 0
    written = json.loads(report.read_text())
    lines = env.read_text()
    assert f"ASTRALPLANE_UPGRADE_BASELINE_SHA={written['baseline_plane_sha']}\n" in lines
    assert f"PLANE_BASE_DEEP_SHA={base}\n" in lines
    assert f"PLANE_BASE_TREE={written['baseline_plane_tree']}\n" in lines
    assert f"PLANE_CANDIDATE_TREE={written['candidate_plane_tree']}\n" in lines
    assert written["event_name"] == "push"


@pytest.mark.parametrize("content", ["{", "[]", '{"inputs":{},"inputs":{}}'])
def test_main_records_failed_receipt_without_environment_on_invalid_event(
    tmp_path, content, capsys
):
    payload = tmp_path / "event.json"
    payload.write_text(content)
    output = tmp_path / "report.json"
    env = tmp_path / "github-env"
    assert baseline.main(["--event-name", "push", "--event-path", str(payload),
                          "--baseline-root", str(tmp_path / "isolated.git"),
                          "--github-env", str(env), "--output", str(output)]) == 2
    assert json.loads(output.read_text())["status"] == "failed"
    assert not env.exists()
    assert "baseline preparation failed" in capsys.readouterr().err


def test_real_cli_accepts_explicit_manual_identity(sources, tmp_path):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", changed)
    output = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.prepare_plane_upgrade_baseline", "--repo", str(deep),
         "--event-name", "manual", "--base-sha", base, "--candidate-sha", changed,
         "--baseline-root", str(tmp_path / "isolated.git"), "--output", str(output),
         "--github-env", str(tmp_path / "github-env")],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["baseline_plane_tree"] == git(deep, "rev-parse", f"{base}:plane")


def test_main_refuses_environment_line_breaks(sources, tmp_path, monkeypatch):
    deep, _, _, base, changed, _, _ = sources
    git(deep, "checkout", "-q", changed)
    original = baseline.prepare

    def damaged_prepare(*args):
        return {**original(*args), "baseline_root": "unsafe\nextra=value"}

    monkeypatch.setattr(baseline, "prepare", damaged_prepare)
    output, env = tmp_path / "report.json", tmp_path / "github-env"
    assert baseline.main(["--repo", str(deep), "--event-name", "manual", "--base-sha", base,
                          "--candidate-sha", changed, "--baseline-root",
                          str(tmp_path / "isolated.git"), "--output", str(output),
                          "--github-env", str(env)]) == 2
    assert not env.exists()


def test_explicit_candidate_disagreement_with_event_does_not_emit_environment(sources, tmp_path):
    deep, _, _, base, changed, _, _ = sources
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"pull_request": {"base": {"sha": base}, "head": {"sha": changed}}}))
    env = tmp_path / "github-env"
    assert baseline.main(["--repo", str(deep), "--event-name", "pull_request", "--event-path",
                          str(payload), "--candidate-sha", base, "--baseline-root",
                          str(tmp_path / "isolated.git"), "--output",
                          str(tmp_path / "report.json"), "--github-env", str(env)]) == 2
    assert not env.exists()
