"""Exercises consumer-selected Plane baseline preparation with real immutable Git trees.
The previous consumed pin remains authoritative when upstream advances, and invalid event or source identities fail closed.
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


def commit(repo):
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Synthetic source")
    return git(repo, "rev-parse", "HEAD")


def manifest(sha):
    return {
        "components": {
            "astral-plane": {
                "repository": baseline.PLANE_REPOSITORY,
                "path": baseline.PLANE_PATH,
                "commit": sha,
                "contract_version": "astralplane.contract/v1",
            }
        }
    }


@pytest.fixture
def sources(tmp_path):
    plane = tmp_path / "plane"
    initialize(plane)
    package = plane / "src/astralplane"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VERSION = "previous"\n')
    previous = commit(plane)
    (package / "__init__.py").write_text('VERSION = "candidate"\n')
    candidate = commit(plane)
    (package / "__init__.py").write_text('VERSION = "future"\n')
    future = commit(plane)
    git(plane, "update-ref", "refs/remotes/origin/main", future)
    deep = tmp_path / "deep"
    initialize(deep)
    (deep / "config").mkdir()
    (deep / baseline.MANIFEST_PATH).write_text(json.dumps(manifest(previous)))
    git(deep, "add", baseline.MANIFEST_PATH)
    git(
        deep,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{previous},{baseline.PLANE_PATH}",
    )
    git(deep, "commit", "-m", "Previous consumer pin")
    base = git(deep, "rev-parse", "HEAD")
    component = deep / baseline.PLANE_PATH
    component.parent.mkdir()
    git(deep, "clone", str(plane), str(component))
    git(component, "checkout", "--detach", previous)
    (deep / "change.txt").write_text("consumer change\n")
    head = commit(deep)
    return deep, component, base, head, previous, candidate, future


def selection(base, head, event="pull_request"):
    payload = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    if event == "push":
        payload = {"ref": "refs/heads/main", "before": base, "after": head}
    return select_revisions(
        event_name=event, event_payload=payload, base_sha=None, candidate_sha=None
    )


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("changed", [False, True])
def test_previous_consumed_pin_is_bound_even_when_upstream_is_newer(
    sources, tmp_path, event, changed
):
    deep, component, base, head, previous, candidate, future = sources
    if changed:
        git(component, "checkout", "--detach", candidate)
        (deep / baseline.MANIFEST_PATH).write_text(json.dumps(manifest(candidate)))
        head = commit(deep)
    report = baseline.prepare(
        deep, selection(base, head, event), tmp_path / "isolated.git"
    )
    assert report["status"] == "prepared"
    assert report["base_deep_sha"] == base
    assert report["candidate_deep_sha"] == head
    assert report["baseline_plane_sha"] == previous
    assert report["candidate_plane_sha"] == (candidate if changed else previous)
    assert report["baseline_source_tree"] == git(
        component, "rev-parse", f"{previous}:src"
    )
    assert report["candidate_source_tree"] == git(
        component, "rev-parse", f"{report['candidate_plane_sha']}:src"
    )
    assert (
        report["baseline_manifest_sha256"]
        == hashlib.sha256(
            subprocess.check_output(
                ["git", "-C", str(deep), "show", f"{base}:{baseline.MANIFEST_PATH}"]
            )
        ).hexdigest()
    )
    isolated = Path(report["baseline_root"])
    assert git(isolated, "rev-parse", "refs/remotes/origin/main") == previous
    assert git(component, "rev-parse", "refs/remotes/origin/main") == future
    assert (
        git(isolated, "show", f"{previous}:src/astralplane/__init__.py")
        == 'VERSION = "previous"'
    )


def test_committed_manifest_is_used_instead_of_working_copy(sources, tmp_path):
    deep, _, base, head, previous, _, _ = sources
    (deep / baseline.MANIFEST_PATH).write_text("malformed uncommitted input")
    assert (
        baseline.prepare(deep, selection(base, head), tmp_path / "isolated.git")[
            "baseline_plane_sha"
        ]
        == previous
    )


def test_invalid_previous_consumer_manifest_fails_closed(sources, tmp_path):
    deep, _, base, _, _, _, _ = sources
    git(deep, "checkout", "-b", "invalid-base", base)
    (deep / baseline.MANIFEST_PATH).write_text("[]")
    base = commit(deep)
    (deep / "change.txt").write_text("candidate\n")
    head = commit(deep)
    with pytest.raises(ValueError, match="no components"):
        baseline.prepare(deep, selection(base, head), tmp_path / "isolated.git")


@pytest.mark.parametrize(
    "damage",
    [
        "repository",
        "path",
        "commit",
        "contract_version",
        "missing",
        "malformed",
        "duplicate",
        "gitlink",
    ],
)
def test_invalid_committed_pin_fails_before_preparation(sources, tmp_path, damage):
    deep, _, base, _, previous, _, _ = sources
    data = manifest(previous)
    entry = data["components"]["astral-plane"]
    entry.update(
        {"repository": "https://example.test/wrong.git"}
        if damage == "repository"
        else {}
    )
    entry.update({"path": "other/Plane"} if damage == "path" else {})
    entry.update({"commit": "main"} if damage == "commit" else {})
    entry.update(
        {"contract_version": "unsupported"} if damage == "contract_version" else {}
    )
    if damage == "missing":
        data = {}
    content = json.dumps(data)
    if damage == "malformed":
        content = "{"
    if damage == "duplicate":
        content = '{"components": {}, "components": {}}'
    (deep / baseline.MANIFEST_PATH).write_text(content)
    if damage == "gitlink":
        git(
            deep,
            "update-index",
            "--cacheinfo",
            f"160000,{'a' * 40},{baseline.PLANE_PATH}",
        )
        git(deep, "add", baseline.MANIFEST_PATH)
        git(deep, "commit", "-m", "Invalid gitlink")
        head = git(deep, "rev-parse", "HEAD")
    else:
        head = commit(deep)
    destination = tmp_path / "isolated.git"
    with pytest.raises((ValueError, CoveragePolicyError)):
        baseline.prepare(deep, selection(base, head), destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "head",
        "component",
        "dirty",
        "untracked",
        "zero",
        "missing",
        "nonancestor",
        "destination",
        "inside-component",
    ],
)
def test_invalid_source_or_event_identity_fails_closed(sources, tmp_path, damage):
    deep, component, base, head, _, candidate, _ = sources
    selected = selection(base, head)
    destination = tmp_path / "isolated.git"
    if damage == "head":
        git(deep, "checkout", "--detach", base)
    elif damage == "component":
        git(component, "checkout", "--detach", candidate)
    elif damage == "dirty":
        (component / "src/astralplane/__init__.py").write_text("changed = True\n")
    elif damage == "untracked":
        (component / "src/astralplane/extra.py").write_text("changed = True\n")
    elif damage == "zero":
        selected = selection("0" * 40, head)
    elif damage == "missing":
        selected = selection("a" * 40, head)
    elif damage == "nonancestor":
        selected = selection(head, base)
    elif damage == "destination":
        destination.mkdir()
    elif damage == "inside-component":
        destination = component / "baseline.git"
    with pytest.raises((ValueError, CoveragePolicyError, FileExistsError)):
        baseline.prepare(deep, selected, destination)


@pytest.mark.parametrize("canonical", [False, True])
def test_missing_previous_source_fetches_only_its_exact_commit_from_canonical_origin(
    sources, tmp_path, monkeypatch, canonical
):
    deep, component, base, _, previous, candidate, _ = sources
    remote = tmp_path / "plane"
    moved = tmp_path / "old-checkout"
    component.rename(moved)
    git(deep, "clone", "--depth=1", "--no-local", f"file://{remote}", str(component))
    git(component, "fetch", "--depth=1", "origin", candidate)
    git(component, "checkout", "--detach", candidate)
    (deep / baseline.MANIFEST_PATH).write_text(json.dumps(manifest(candidate)))
    head = commit(deep)
    original = baseline.git
    fetched = []

    def controlled_git(repo, *args):
        if canonical and args == ("remote", "get-url", "origin"):
            return baseline.PLANE_REPOSITORY.encode()
        if args[:3] == ("fetch", "--no-tags", "origin"):
            fetched.append(args[3])
        return original(repo, *args)

    monkeypatch.setattr(baseline, "git", controlled_git)
    if canonical:
        report = baseline.prepare(
            deep, selection(base, head), tmp_path / "isolated.git"
        )
        assert fetched == [previous]
        assert report["baseline_plane_sha"] == previous
    else:
        with pytest.raises(ValueError, match="canonical repository"):
            baseline.prepare(deep, selection(base, head), tmp_path / "isolated.git")
        assert fetched == []


def test_isolated_source_hash_mismatch_fails_closed(sources, tmp_path, monkeypatch):
    deep, _, base, head, _, _, _ = sources
    destination = tmp_path / "isolated.git"
    original = baseline.git

    def damaged_git(repo, *args):
        if repo == destination and args[0] == "rev-parse":
            return b"0" * 40
        return original(repo, *args)

    monkeypatch.setattr(baseline, "git", damaged_git)
    with pytest.raises(ValueError, match="source identity differs"):
        baseline.prepare(deep, selection(base, head), destination)


def test_main_emits_environment_and_bound_receipt(sources, tmp_path, monkeypatch):
    deep, _, base, head, previous, _, _ = sources
    payload = tmp_path / "event.json"
    payload.write_text(
        json.dumps({"ref": "refs/heads/main", "before": base, "after": head})
    )
    env = tmp_path / "github-env"
    report = tmp_path / "report.json"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload))
    assert (
        baseline.main(
            [
                "--repo",
                str(deep),
                "--baseline-root",
                str(tmp_path / "isolated.git"),
                "--github-env",
                str(env),
                "--output",
                str(report),
            ]
        )
        == 0
    )
    assert f"ASTRALPLANE_UPGRADE_BASELINE_SHA={previous}\n" in env.read_text()
    assert f"PLANE_BASE_COMPONENT_SHA={previous}\n" in env.read_text()
    assert json.loads(report.read_text())["event_name"] == "push"


@pytest.mark.parametrize("content", ["{", "[]", '{"inputs":{},"inputs":{}}'])
def test_main_records_failed_receipt_without_environment_on_invalid_event(
    tmp_path, content, capsys
):
    payload = tmp_path / "event.json"
    payload.write_text(content)
    output = tmp_path / "report.json"
    env = tmp_path / "github-env"
    assert (
        baseline.main(
            [
                "--event-name",
                "push",
                "--event-path",
                str(payload),
                "--baseline-root",
                str(tmp_path / "isolated.git"),
                "--github-env",
                str(env),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert json.loads(output.read_text())["status"] == "failed"
    assert not env.exists()
    assert "baseline preparation failed" in capsys.readouterr().err


def test_real_cli_accepts_explicit_manual_identity(sources, tmp_path):
    deep, _, base, head, previous, _, _ = sources
    output = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.prepare_plane_upgrade_baseline",
            "--repo",
            str(deep),
            "--event-name",
            "manual",
            "--base-sha",
            base,
            "--candidate-sha",
            head,
            "--baseline-root",
            str(tmp_path / "isolated.git"),
            "--output",
            str(output),
            "--github-env",
            str(tmp_path / "github-env"),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["baseline_plane_sha"] == previous


def test_main_refuses_environment_line_breaks(sources, tmp_path, monkeypatch):
    deep, _, base, head, _, _, _ = sources
    original = baseline.prepare

    def damaged_prepare(*args):
        return {**original(*args), "baseline_root": "unsafe\nextra=value"}

    monkeypatch.setattr(baseline, "prepare", damaged_prepare)
    output, env = tmp_path / "report.json", tmp_path / "github-env"
    assert (
        baseline.main(
            [
                "--repo",
                str(deep),
                "--event-name",
                "manual",
                "--base-sha",
                base,
                "--candidate-sha",
                head,
                "--baseline-root",
                str(tmp_path / "isolated.git"),
                "--output",
                str(output),
                "--github-env",
                str(env),
            ]
        )
        == 2
    )
    assert not env.exists()


def test_explicit_candidate_disagreement_with_event_does_not_emit_environment(
    sources, tmp_path
):
    deep, _, base, head, _, _, _ = sources
    payload = tmp_path / "event.json"
    payload.write_text(
        json.dumps({"pull_request": {"base": {"sha": base}, "head": {"sha": head}}})
    )
    env = tmp_path / "github-env"
    assert (
        baseline.main(
            [
                "--repo",
                str(deep),
                "--event-name",
                "pull_request",
                "--event-path",
                str(payload),
                "--candidate-sha",
                base,
                "--baseline-root",
                str(tmp_path / "isolated.git"),
                "--output",
                str(tmp_path / "report.json"),
                "--github-env",
                str(env),
            ]
        )
        == 2
    )
    assert not env.exists()
