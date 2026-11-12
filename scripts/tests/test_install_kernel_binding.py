"""Tests for scripts/install_kernel_binding.py: manifest checks, platform selection, digest verification, optional absence and the install steps with pip stubbed out."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install_kernel_binding.py"
spec = importlib.util.spec_from_file_location("install_kernel_binding", SCRIPT)
ikb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ikb)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def staged(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "k.whl").write_bytes(b"wheel")
    (artifacts / "mb").write_bytes(b"mailbox")
    manifest = {
        "format": ikb.FORMAT,
        "python": {"import": "astral_kernel"},
        "artifacts": {"linux-aarch64": {"wheel": {"file": "k.whl", "sha256": digest(b"wheel")}, "mailbox": {"file": "mb", "sha256": digest(b"mailbox")}}},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return artifacts, manifest, path


def test_the_committed_manifest_is_well_formed():
    manifest = ikb.load_manifest()
    entries = ikb.artifacts_for(manifest, "linux-aarch64")
    for entry in entries.values():
        assert len(entry["sha256"]) == 64 and entry["file"]
    assert manifest["python"]["import"] == "astral_kernel"


def test_manifest_errors(tmp_path):
    with pytest.raises(ikb.BindingError, match="unreadable"):
        ikb.load_manifest(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "other"}')
    with pytest.raises(ikb.BindingError, match="not recognized"):
        ikb.load_manifest(bad)
    with pytest.raises(ikb.BindingError, match="no artifacts"):
        ikb.artifacts_for({"artifacts": {}}, "linux-aarch64")


@pytest.mark.parametrize("machine,expected", [("aarch64", "linux-aarch64"), ("arm64", "linux-aarch64"), ("AMD64", "linux-x86_64")])
def test_platform_key(machine, expected):
    assert ikb.platform_key(machine, "Linux") == expected


def test_platform_key_refuses_other_platforms():
    with pytest.raises(ikb.BindingError):
        ikb.platform_key("aarch64", "Darwin")
    with pytest.raises(ikb.BindingError):
        ikb.platform_key("riscv64", "Linux")


def test_verify_accepts_matching_digests_and_refuses_changes(staged):
    artifacts, manifest, _ = staged
    assert set(ikb.verify(artifacts, "linux-aarch64", manifest)) == {"wheel", "mailbox"}
    (artifacts / "mb").write_bytes(b"tampered")
    with pytest.raises(ikb.BindingError, match="digest mismatch"):
        ikb.verify(artifacts, "linux-aarch64", manifest)
    (artifacts / "mb").unlink()
    with pytest.raises(ikb.BindingError, match="missing artifact"):
        ikb.verify(artifacts, "linux-aarch64", manifest)


def test_install_runs_pip_copies_the_mailbox_and_imports(staged, tmp_path, monkeypatch):
    artifacts, manifest, _ = staged
    calls = []
    monkeypatch.setattr(ikb.subprocess, "run", lambda command, check: calls.append(command))
    bin_dir = tmp_path / "bin"
    ikb.install(artifacts, "linux-aarch64", bin_dir, manifest)
    assert "--no-index" in calls[0] and calls[0][-1] == str(artifacts / "k.whl")
    assert calls[1][-1] == "import astral_kernel"
    target = bin_dir / "astral-mailbox"
    assert target.read_bytes() == b"mailbox" and target.stat().st_mode & 0o777 == 0o755


def test_main_verify_install_and_optional_absence(staged, tmp_path, monkeypatch, capsys):
    artifacts, manifest, path = staged
    monkeypatch.setattr(ikb, "MANIFEST", path)
    monkeypatch.setattr(ikb, "load_manifest", lambda: manifest)
    monkeypatch.setattr(ikb, "platform_key", lambda: "linux-aarch64")
    monkeypatch.setattr(ikb.subprocess, "run", lambda command, check: None)
    assert ikb.main(["verify", "--artifacts", str(artifacts)]) == 0
    assert ikb.main(["install", "--artifacts", str(artifacts), "--bin-dir", str(tmp_path / "b")]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ikb.main(["install", "--artifacts", str(empty), "--allow-missing"]) == 0
    assert "without the mesh peer" in capsys.readouterr().out
    assert ikb.main(["install", "--artifacts", str(tmp_path / "absent"), "--allow-missing"]) == 0
    assert ikb.main(["verify", "--artifacts", str(empty)]) == 1
    (artifacts / "k.whl").write_bytes(b"other")
    assert ikb.main(["verify", "--artifacts", str(artifacts)]) == 1
    assert "digest mismatch" in capsys.readouterr().err


def test_main_reports_a_failed_pip_install(staged, monkeypatch, capsys):
    artifacts, manifest, _ = staged
    monkeypatch.setattr(ikb, "load_manifest", lambda: manifest)
    monkeypatch.setattr(ikb, "platform_key", lambda: "linux-aarch64")

    def failing(command, check):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(ikb.subprocess, "run", failing)
    assert ikb.main(["install", "--artifacts", str(artifacts)]) == 1
    assert "kernel binding" in capsys.readouterr().err
