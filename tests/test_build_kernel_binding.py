"""Tests immutable Kernel source checks, artifact production and CI deployment generation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_kernel_binding.py"
spec = importlib.util.spec_from_file_location("build_kernel_binding", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def git(directory: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


@pytest.fixture
def kernel_checkout(tmp_path):
    source = tmp_path / "kernel"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts" / "build-python.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "scripts" / "artifact_manifest.py").write_text("raise SystemExit(0)\n")
    (source / "scripts" / "build-python.sh").chmod(0o755)
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.org")
    git(source, "add", ".")
    subprocess.run(["git", "-C", str(source), "commit", "-m", "fixture"], check=True,
                   capture_output=True)
    git(source, "remote", "add", "origin", builder.KERNEL_REPOSITORY)
    return source


@pytest.fixture
def binding(kernel_checkout):
    commit = git(kernel_checkout, "rev-parse", "HEAD")
    payloads = {"wheel": b"wheel", "mailbox": b"mailbox"}
    entries = {name: {"file": f"kernel.{name}", "sha256": hashlib.sha256(content).hexdigest()}
               for name, content in payloads.items()}
    return {
        "format": "astraldeep.kernel-binding/v1",
        "source": {"repository": builder.KERNEL_REPOSITORY, "commit": commit},
        "artifacts": {"linux-x86_64": entries},
    }


def test_load_binding_accepts_only_official_repo_and_full_commit(binding, tmp_path):
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding))
    assert builder.load_binding(path)["source"]["commit"] == binding["source"]["commit"]
    binding["source"]["repository"] = "https://example.org/kernel.git"
    path.write_text(json.dumps(binding))
    with pytest.raises(builder.BuildError, match="official AstralKernel"):
        builder.load_binding(path)
    binding["source"]["repository"] = builder.KERNEL_REPOSITORY
    binding["source"]["commit"] = "abc123"
    path.write_text(json.dumps(binding))
    with pytest.raises(builder.BuildError, match="full immutable"):
        builder.load_binding(path)


def test_load_binding_rejects_unreadable_and_invalid_documents(tmp_path):
    with pytest.raises(builder.BuildError, match="missing or invalid"):
        builder.load_binding(tmp_path / "missing.json")
    path = tmp_path / "binding.json"
    valid_source = {"repository": builder.KERNEL_REPOSITORY, "commit": "a" * 40}
    documents = (
        ("[]", "object is invalid"),
        ('{"format":"other"}', "format is invalid"),
        (json.dumps({"format": "astraldeep.kernel-binding/v1", "source": valid_source}),
         "artifact map is invalid"),
    )
    for content, message in documents:
        path.write_text(content)
        with pytest.raises(builder.BuildError, match=message):
            builder.load_binding(path)


def test_producer_builds_and_verifies_golden_exact_source_artifacts(kernel_checkout, binding, tmp_path, monkeypatch):
    output = tmp_path / "artifacts"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == str(kernel_checkout / "scripts" / "build-python.sh"):
            output.mkdir(exist_ok=True)
            (output / "kernel.wheel").write_bytes(b"wheel")
            (output / "kernel.mailbox").write_bytes(b"mailbox")
            (output / "linux-build-manifest.json").write_text(json.dumps({
                "source_commit": binding["source"]["commit"], "target": "linux/amd64",
            }))

    monkeypatch.setattr(builder, "run_command", run)
    paths = builder.produce(kernel_checkout, output, "linux/amd64", binding)
    assert set(paths) == {"wheel", "mailbox"}
    assert calls[0][1:] == ["--platform", "linux/amd64", "--out", str(output)]
    assert calls[1][2:7] == ["verify", "--root", str(output), "--kind", "linux"]
    assert git(kernel_checkout, "status", "--porcelain") == ""


def test_producer_rejects_dirty_or_mismatched_source_before_running(kernel_checkout, binding, tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "run_command", lambda *args, **kwargs: pytest.fail("producer must not run"))
    (kernel_checkout / "untracked.txt").write_text("dirty")
    with pytest.raises(builder.BuildError, match="dirty"):
        builder.produce(kernel_checkout, tmp_path / "artifacts", "linux/amd64", binding)
    (kernel_checkout / "untracked.txt").unlink()
    binding["source"]["commit"] = "0" * 40
    with pytest.raises(builder.BuildError, match="HEAD differs"):
        builder.produce(kernel_checkout, tmp_path / "artifacts", "linux/amd64", binding)


def test_producer_clones_missing_source_to_a_verified_checkout(kernel_checkout, binding, tmp_path, monkeypatch):
    destination = tmp_path / "kernel-source"
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[1] == "clone":
            subprocess.run(["git", "clone", "--no-checkout", "--depth", "1", str(kernel_checkout),
                            str(destination)], check=True, capture_output=True)
            git(destination, "remote", "set-url", "origin", builder.KERNEL_REPOSITORY)
        elif args[1:3] == ["-C", str(destination)] and args[3] == "checkout":
            subprocess.run(args, check=True, capture_output=True)

    monkeypatch.setattr(builder, "run_command", command)
    cloned = builder.checkout_source(destination, binding, workspace=tmp_path / "candidate")
    assert cloned == destination
    assert calls[0][6] == builder.KERNEL_REPOSITORY
    assert calls[1][-1] == binding["source"]["commit"]
    assert git(cloned, "rev-parse", "HEAD") == binding["source"]["commit"]


def test_source_check_rejects_an_unofficial_remote(kernel_checkout, binding):
    git(kernel_checkout, "remote", "set-url", "origin", "git@github.com:AstralDeep/AstralKernel.git")
    builder.source_state(kernel_checkout, binding["source"]["commit"])
    git(kernel_checkout, "remote", "set-url", "origin", "https://example.org/kernel.git")
    with pytest.raises(builder.BuildError, match="origin is not the official"):
        builder.source_state(kernel_checkout, binding["source"]["commit"])


def test_producer_rejects_missing_artifacts_and_builder_failure(kernel_checkout, binding, tmp_path, monkeypatch):
    output = tmp_path / "missing"

    def missing(command, **kwargs):
        if command[0] == str(kernel_checkout / "scripts" / "build-python.sh"):
            output.mkdir(exist_ok=True)
            (output / "kernel.wheel").write_bytes(b"wheel")
            (output / "linux-build-manifest.json").write_text(json.dumps({
                "source_commit": binding["source"]["commit"], "target": "linux/amd64",
            }))

    monkeypatch.setattr(builder, "run_command", missing)
    with pytest.raises(ValueError, match="missing pinned kernel mailbox"):
        builder.produce(kernel_checkout, output, "linux/amd64", binding)
    output = tmp_path / "failed"

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(builder, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        builder.produce(kernel_checkout, output, "linux/amd64", binding)


def test_producer_rejects_nonempty_output_and_unknown_platform(kernel_checkout, binding, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "old.whl").write_text("stale")
    with pytest.raises(builder.BuildError, match="must be empty"):
        builder.produce(kernel_checkout, output, "linux/amd64", binding)
    with pytest.raises(builder.BuildError, match="unsupported Linux"):
        builder.produce(kernel_checkout, tmp_path / "other", "linux/ppc64", binding)


def test_prepare_deployment_is_task_scoped_and_preserves_template(kernel_checkout, binding, tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "kernel.wheel").write_bytes(b"wheel")
    (artifacts / "kernel.mailbox").write_bytes(b"mailbox")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    template = deploy / "deployment.toml"
    template.write_text('[node]\nimage = "astralvessel-node:local"\n')
    generated = deploy / "deployment.ci.toml"
    original = template.read_text()
    builder.write_ci_deployment(template, generated, artifacts, "linux/amd64", binding)
    parsed = tomllib.loads(generated.read_text())
    assert parsed["executor"]["kernel_artifacts"] == str(artifacts)
    assert template.read_text() == original
    with pytest.raises(builder.BuildError, match="already exists"):
        builder.write_ci_deployment(template, generated, artifacts, "linux/amd64", binding)


def test_prepare_deployment_rejects_existing_executor_section(kernel_checkout, binding, tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "kernel.wheel").write_bytes(b"wheel")
    (artifacts / "kernel.mailbox").write_bytes(b"mailbox")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    template = deploy / "deployment.toml"
    template.write_text("[executor]\n")
    with pytest.raises(builder.BuildError, match="already declares"):
        builder.write_ci_deployment(template, deploy / "ci.toml", artifacts, "linux/amd64", binding)


def test_cli_reads_the_binding_for_each_command(binding, tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "binding.json"
    manifest.write_text(json.dumps(binding))
    assert builder.main(["source-info", "--binding", str(manifest)]) == 0
    assert capsys.readouterr().out.strip() == f"commit={binding['source']['commit']}"
    monkeypatch.setattr(builder, "produce", lambda *args: {})
    assert builder.main(["produce", "--source", str(tmp_path / "source"), "--output", str(tmp_path / "out"),
                         "--platform", "linux/amd64", "--binding", str(manifest)]) == 0
    monkeypatch.setattr(builder, "write_ci_deployment", lambda *args: args[1])
    assert builder.main(["prepare-deployment", "--template", str(tmp_path / "deployment.toml"),
                         "--output", str(tmp_path / "deployment.ci.toml"), "--artifacts", str(tmp_path / "out"),
                         "--platform", "linux/amd64", "--binding", str(manifest)]) == 0
    assert builder.main(["source-info", "--binding", str(tmp_path / "missing.json")]) == 1
