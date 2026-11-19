"""Checks the authenticated source selection, source-build fallback and bounded restoration used by macOS releases."""

import hashlib
import json
import subprocess
import zipfile

import pytest

from scripts import release_apple_framework as apple

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def provider(monkeypatch):
    repo = {"id": apple.REPO_ID, "full_name": apple.REPO}
    run = {"id": 77, "event": "push", "head_sha": SHA, "head_branch": "main", "status": "completed",
           "conclusion": "success", "path": ".github/workflows/ci.yml", "name": "Kernel qualification",
           "repository": repo.copy(), "head_repository": repo.copy()}
    archive = {"id": 88, "name": "kernel-apple", "expired": False, "digest": DIGEST, "size_in_bytes": 1234,
               "workflow_run": {"id": run["id"], "head_sha": SHA, "head_branch": "main",
                                "repository_id": apple.REPO_ID, "head_repository_id": apple.REPO_ID}}
    runs, archives, calls = [run], [archive], []

    def query(args, *, text):
        assert args[:2] == ["gh", "api"] and text
        calls.append(args[2])
        if args[2] == (f"repos/{apple.REPO}/actions/workflows/ci.yml/runs?"
                       f"head_sha={SHA}&event=push&branch=main&status=success&per_page=100"):
            return json.dumps({"workflow_runs": runs})
        assert args[2] == f"repos/{apple.REPO}/actions/runs/77/artifacts?name=kernel-apple&per_page=100"
        return json.dumps({"artifacts": archives})

    def probe(args, *, check):
        assert check and args == ["gh", "api", "--method", "HEAD", "--silent",
                                  f"repos/{apple.REPO}/actions/artifacts/88/zip"]
        calls.append(args[-1])

    monkeypatch.setattr(apple.subprocess, "check_output", query)
    monkeypatch.setattr(apple.subprocess, "run", probe)
    return runs, archives, calls


def selection(tmp_path):
    output = tmp_path / "output"
    apple.main(["plan", SHA, str(output)])
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def test_selects_immutable_qualified_archive(provider, tmp_path):
    assert selection(tmp_path) == {
        "apple_artifact": "88", "apple_bytes": "1234", "apple_digest": DIGEST, "apple_inputs": '["qualified"]'}
    assert len(provider[2]) == 3


@pytest.mark.parametrize("reason", ["run missing", "archive expired", "archive missing", "access denied"])
def test_falls_back_to_all_pinned_source_targets(provider, tmp_path, monkeypatch, reason):
    runs, archives, _ = provider
    if reason == "run missing":
        runs.clear()
    elif reason == "archive expired":
        archives[0]["expired"] = True
    elif reason == "archive missing":
        archives.clear()
    else:
        def denied(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "gh")
        monkeypatch.setattr(apple.subprocess, "run", denied)
    assert selection(tmp_path) == {
        "apple_artifact": "", "apple_bytes": "", "apple_digest": "", "apple_inputs": json.dumps(apple.TARGETS)}


@pytest.mark.parametrize("field,value", [
    ("event", "pull_request"), ("head_sha", "c" * 40), ("head_branch", "feature"), ("status", "in_progress"),
    ("conclusion", "failure"), ("path", ".github/workflows/other.yml"), ("name", "Different workflow"),
    ("repository", {"id": 2, "full_name": apple.REPO}),
    ("head_repository", {"id": apple.REPO_ID, "full_name": "elsewhere/kernel"}),
])
def test_refuses_mismatched_qualification(provider, field, value):
    provider[0][0][field] = value
    with pytest.raises(ValueError, match="qualification identity"):
        apple.select(SHA)


@pytest.mark.parametrize("field,value", [
    ("id", 0), ("size_in_bytes", 0), ("size_in_bytes", apple.DEFAULT_MAX_ARCHIVE_BYTES + 1),
    ("digest", "sha256:xyz"), ("workflow_run.id", 1), ("workflow_run.head_sha", "c" * 40),
    ("workflow_run.head_branch", "candidate"), ("workflow_run.repository_id", 1),
    ("workflow_run.head_repository_id", 2),
])
def test_refuses_mismatched_provider_artifact(provider, field, value):
    obj = provider[1][0]
    if field.startswith("workflow_run."):
        obj = obj["workflow_run"]
        field = field.split(".")[1]
    obj[field] = value
    with pytest.raises(ValueError, match="archive identity"):
        apple.select(SHA)


def test_refuses_ambiguous_archive_and_invalid_gitlink(provider):
    provider[1].append(provider[1][0].copy())
    with pytest.raises(ValueError, match="ambiguous"):
        apple.select(SHA)
    with pytest.raises(ValueError, match="gitlink"):
        apple.select("main")


@pytest.mark.parametrize("change,reason", [
    ("", None), ("size", "ZIP size"), ("digest", "ZIP digest"), ("target", "target or compiler"),
    ("xcode", "target or compiler"), ("rustc", "target or compiler"), ("unsafe path", "not canonical"),
])
def test_restore_checks_provider_bytes_paths_and_compilers(tmp_path, change, reason):
    archive, dest = tmp_path / "archive.zip", tmp_path / "Artifacts"
    manifest = {"target": "universal", "toolchain": {"xcode": "Xcode 26.6\nBuild version 17F113",
                                                     "rustc": "rustc 1.99.1 (58cf8f9c3 2026-10-13)"}}
    if change in {"rustc", "xcode"}:
        manifest["toolchain"][change] = "unexpected compiler"
    elif change == "target":
        manifest["target"] = "unqualified"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("apple-build-manifest.json", json.dumps(manifest))
        zipped.writestr("../lib" if change == "unsafe path" else "Bindings/data", "framework")
    digest = DIGEST if change == "digest" else "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    size = archive.stat().st_size + (change == "size")
    args = ["restore", str(archive), str(dest), str(size), digest]
    if reason:
        with pytest.raises(ValueError, match=reason):
            apple.main(args)
        assert not (tmp_path / "lib").exists()
    else:
        apple.main(args)
        assert (dest / "Bindings/data").read_text() == "framework"


def test_cli_rejects_missing_or_unknown_mode():
    with pytest.raises(SystemExit, match="plan or restore"):
        apple.main([])
