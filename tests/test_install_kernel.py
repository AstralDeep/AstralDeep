"""Tests architecture-specific Kernel artifact verification and fail-closed image installation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_kernel.py"
spec = importlib.util.spec_from_file_location("install_kernel", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def artifacts(tmp_path):
    directory = tmp_path / "artifacts"
    directory.mkdir()
    payloads = {"wheel": b"wheel", "mailbox": b"mailbox"}
    entries = {name: {"file": f"kernel.{name}", "sha256": digest(content)}
               for name, content in payloads.items()}
    for name, content in payloads.items():
        (directory / entries[name]["file"]).write_bytes(content)
    manifest = {"python": {"import": "astral_kernel"}, "artifacts": {"linux-aarch64": entries}}
    return directory, manifest


def test_platform_key_supports_linux_arm_and_amd64_aliases():
    assert installer.platform_key("arm64", "Linux") == "linux-aarch64"
    assert installer.platform_key("AMD64", "Linux") == "linux-x86_64"


def test_platform_key_fails_closed_outside_linux_artifact_architectures():
    with pytest.raises(ValueError, match="no kernel artifacts"):
        installer.platform_key("arm64", "Darwin")
    with pytest.raises(ValueError, match="no kernel artifacts"):
        installer.platform_key("riscv64", "Linux")


def test_binding_pins_final_kernel_and_both_linux_architectures():
    manifest = json.loads((SCRIPT.parents[1] / "config" / "kernel-binding.json").read_text())
    assert manifest["source"]["commit"] == "8ab7f91e66c27e47db4e6cce7061e78142bc0ba9"
    assert set(manifest["artifacts"]) == {"linux-aarch64", "linux-x86_64"}
    for entries in manifest["artifacts"].values():
        assert set(entries) == {"wheel", "mailbox"}
        assert all(len(entry["sha256"]) == 64 for entry in entries.values())


def test_verify_requires_both_matching_artifacts(artifacts):
    directory, manifest = artifacts
    assert set(installer.verify(directory, "linux-aarch64", manifest)) == {"wheel", "mailbox"}
    (directory / "kernel.mailbox").write_bytes(b"changed")
    with pytest.raises(ValueError, match="sha256"):
        installer.verify(directory, "linux-aarch64", manifest)
    (directory / "kernel.mailbox").unlink()
    with pytest.raises(ValueError, match="missing pinned kernel mailbox"):
        installer.verify(directory, "linux-aarch64", manifest)
    with pytest.raises(ValueError, match="no kernel wheel and mailbox"):
        installer.verify(directory, "linux-x86_64", manifest)


def test_install_installs_wheel_imports_binding_and_copies_mailbox(artifacts, tmp_path, monkeypatch):
    directory, manifest = artifacts
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda command, check: calls.append(command))
    bin_directory = tmp_path / "bin"
    installer.install(directory, "linux-aarch64", bin_directory, manifest)
    assert "--no-index" in calls[0] and "--force-reinstall" in calls[0]
    assert calls[0][-1] == str(directory / "kernel.wheel")
    assert calls[1][-1] == "import astral_kernel"
    mailbox = bin_directory / "astral-mailbox"
    assert mailbox.read_bytes() == b"mailbox"
    assert mailbox.stat().st_mode & 0o777 == 0o755


def test_main_refuses_an_artifact_digest_mismatch(artifacts, tmp_path, monkeypatch, capsys):
    directory, manifest = artifacts
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(manifest))
    (directory / "kernel.wheel").write_bytes(b"tampered")
    monkeypatch.setattr(installer, "platform_key", lambda: "linux-aarch64")
    assert installer.main(str(directory), binding, tmp_path / "bin") == 1
    assert "sha256" in capsys.readouterr().err


def test_main_installs_verified_artifacts(artifacts, tmp_path, monkeypatch):
    directory, manifest = artifacts
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(manifest))
    monkeypatch.setattr(installer, "platform_key", lambda: "linux-aarch64")
    monkeypatch.setattr(installer.subprocess, "run", lambda command, check: None)
    bin_directory = tmp_path / "bin"
    assert installer.main(str(directory), binding, bin_directory) == 0
    assert (bin_directory / "astral-mailbox").read_bytes() == b"mailbox"


def test_main_reports_failed_wheel_install(artifacts, tmp_path, monkeypatch, capsys):
    directory, manifest = artifacts
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(manifest))
    monkeypatch.setattr(installer, "platform_key", lambda: "linux-aarch64")

    def fail(command, check):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(installer.subprocess, "run", fail)
    assert installer.main(str(directory), binding, tmp_path / "bin") == 1
    assert "kernel binding" in capsys.readouterr().err


def test_cli_accepts_explicit_binding_and_bin_directory(monkeypatch):
    calls = []
    monkeypatch.setattr(installer, "main", lambda *args: calls.append(args) or 0)
    assert installer.cli(["artifacts", "--binding", "binding.json", "--bin-dir", "bin"]) == 0
    assert calls == [("artifacts", Path("binding.json"), Path("bin"))]


def test_cli_rejects_missing_artifact_argument():
    result = subprocess.run([installer.sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2 and "usage:" in result.stderr
