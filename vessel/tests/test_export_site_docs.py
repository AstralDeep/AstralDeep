"""Tests the docs exporter on a fixture repository with the site build replaced by a fixed page set."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_site_docs.py"
spec = importlib.util.spec_from_file_location("export_site_docs", SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

PAGES = {"index.html": b"<h1>home</h1>", "run/up/index.html": b"<h1>up</h1>", "sitemap.xml": b"<urlset/>"}


def run(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "vessel" / "docs").mkdir(parents=True)
    (root / "vessel" / "mkdocs.yml").write_text("site_name: fixture\n")
    (root / "vessel" / "docs" / "requirements.txt").write_text("# tools\nmkdocs==1.6.1\n\npydantic==2.14.3\n")
    run(root, "init", "-q")
    run(root, "config", "user.email", "t@example.test")
    run(root, "config", "user.name", "t")
    run(root, "add", "-A")
    run(root, "commit", "-q", "-m", "docs")
    return root


@pytest.fixture(autouse=True)
def fake_build(monkeypatch):
    def build(root, pins, out, scratch):
        assert (root / "mkdocs.yml").is_file()
        for name, content in PAGES.items():
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            (out / name).write_bytes(content)

    monkeypatch.setattr(exporter, "build_site", build)


def test_the_manifest_lists_every_file_with_size_and_digest(repository, tmp_path):
    out = tmp_path / "site"
    manifest = exporter.export(repository, "HEAD", "vessel", out)
    assert manifest["commit"] == run(repository, "rev-parse", "HEAD")
    assert manifest["subdirectory"] == "vessel"
    assert manifest["requirements"] == ["mkdocs==1.6.1", "pydantic==2.14.3"]
    assert {entry["path"] for entry in manifest["files"]} == set(PAGES)
    for entry in manifest["files"]:
        assert entry["size"] == len(PAGES[entry["path"]])
        assert entry["sha256"] == hashlib.sha256(PAGES[entry["path"]]).hexdigest()
    assert json.loads((out / "astral-docs.json").read_text()) == manifest


def test_a_rerun_on_the_same_commit_gives_the_same_file_list(repository, tmp_path):
    first = exporter.export(repository, "HEAD", "vessel", tmp_path / "a")
    second = exporter.export(repository, "HEAD", "vessel", tmp_path / "b")
    assert first == second


def test_a_dirty_tree_is_refused(repository, tmp_path):
    (repository / "vessel" / "docs" / "new.md").write_text("draft")
    with pytest.raises(exporter.ExportError, match="uncommitted"):
        exporter.export(repository, "HEAD", "vessel", tmp_path / "site")


def test_a_filled_output_directory_is_refused(repository, tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "stale.html").write_text("old")
    with pytest.raises(exporter.ExportError, match="not empty"):
        exporter.export(repository, "HEAD", "vessel", out)


@pytest.mark.parametrize("subdir", ["../x", "/etc"])
def test_a_subdirectory_outside_the_repository_is_refused(repository, tmp_path, subdir):
    with pytest.raises(exporter.ExportError, match="inside the repository"):
        exporter.export(repository, "HEAD", subdir, tmp_path / "site")


def test_an_unknown_commit_or_missing_docs_is_refused(repository, tmp_path):
    with pytest.raises(exporter.ExportError):
        exporter.export(repository, "0" * 40, "vessel", tmp_path / "a")
    with pytest.raises(exporter.ExportError, match="pathspec"):
        exporter.export(repository, "HEAD", "missing", tmp_path / "b")


def test_the_command_reports_a_refusal_with_a_nonzero_status(repository, tmp_path, capsys):
    status = exporter.main(
        ["--commit", "0" * 40, "--subdir", "vessel", "--out", str(tmp_path / "s"), "--repository", str(repository)]
    )
    assert status == 1
    assert "export refused" in capsys.readouterr().err
    assert exporter.main(
        ["--commit", "HEAD", "--subdir", "vessel", "--out", str(tmp_path / "ok"), "--repository", str(repository)]
    ) == 0
