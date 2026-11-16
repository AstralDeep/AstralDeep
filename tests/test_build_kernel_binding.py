"""Tests immutable Kernel source checks, artifact production and CI deployment generation."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

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
    fetch_args = []
    monkeypatch.setattr(builder, "fetch_provider", lambda *args: fetch_args.append(args) or {})
    assert builder.main(["fetch", "--run-id", "17", "--artifact-id", "29", "--expected-digest", "a" * 64,
                         "--output", str(tmp_path / "provider"), "--platform", "linux/arm64",
                         "--binding", str(manifest)]) == 0
    assert fetch_args[0][0:3] == (17, 29, "a" * 64)
    monkeypatch.setattr(builder, "write_ci_deployment", lambda *args: args[1])
    assert builder.main(["prepare-deployment", "--template", str(tmp_path / "deployment.toml"),
                         "--output", str(tmp_path / "deployment.ci.toml"), "--artifacts", str(tmp_path / "out"),
                         "--platform", "linux/amd64", "--binding", str(manifest)]) == 0
    assert builder.main(["source-info", "--binding", str(tmp_path / "missing.json")]) == 1


def _provider_fixture(binding):
    payloads = {
        "wheel": ("astral_kernel-0.1.0-py3-none-manylinux_2_36_aarch64.whl", b"arm-wheel"),
        "mailbox": ("astral-mailbox-linux-aarch64", b"arm-mailbox"),
    }
    binding["artifacts"]["linux-aarch64"] = {
        name: {"file": filename, "sha256": hashlib.sha256(content).hexdigest()}
        for name, (filename, content) in payloads.items()
    }
    manifest = json.dumps({"source_commit": binding["source"]["commit"], "target": "linux/arm64"}).encode()
    archive_stream = io.BytesIO()
    with ZipFile(archive_stream, "w") as archive:
        for filename, content in payloads.values():
            archive.writestr(filename, content)
        archive.writestr("linux-build-manifest.json", manifest)
    archive_bytes = archive_stream.getvalue()
    repo = {"full_name": builder.KERNEL_REPOSITORY_NAME, "id": builder.KERNEL_REPOSITORY_ID}
    run = {
        "id": 17, "event": "push", "status": "completed", "conclusion": "success",
        "head_branch": "main", "head_sha": binding["source"]["commit"],
        "repository": repo, "path": builder.KERNEL_WORKFLOW_PATH, "name": builder.KERNEL_WORKFLOW_NAME,
    }
    artifact = {
        "id": 29, "name": "kernel-linux-aarch64", "expired": False,
        "size_in_bytes": len(archive_bytes), "digest": "sha256:" + hashlib.sha256(archive_bytes).hexdigest(),
        "expires_at": "2999-01-01T00:00:00Z",
        "workflow_run": {
            "id": 17, "repository_id": builder.KERNEL_REPOSITORY_ID,
            "head_repository_id": builder.KERNEL_REPOSITORY_ID,
            "head_sha": binding["source"]["commit"], "head_branch": "main",
        },
    }
    return run, artifact, archive_bytes


def test_fetch_downloads_verified_provider_artifact(kernel_checkout, binding, tmp_path, monkeypatch):
    run, artifact, archive = _provider_fixture(binding)
    expected_digest = artifact["digest"].removeprefix("sha256:")
    responses = iter((run, artifact, archive))
    endpoints = []
    monkeypatch.setattr(builder, "gh_api", lambda endpoint, binary=False: endpoints.append((endpoint, binary))
                        or next(responses))
    monkeypatch.setattr(builder, "checkout_source", lambda destination, supplied: kernel_checkout)
    calls = []
    monkeypatch.setattr(builder, "run_command", lambda command, **kwargs: calls.append((command, kwargs)))
    output = tmp_path / "provider"
    paths = builder.fetch_provider(17, 29, expected_digest, output, "linux/arm64", binding)
    assert paths["wheel"].read_bytes() == b"arm-wheel"
    assert paths["mailbox"].read_bytes() == b"arm-mailbox"
    receipt = json.loads((output / "provider-receipt.json").read_text())
    assert receipt == {
        "run_id": 17,
        "source_commit": binding["source"]["commit"],
        "workflow": builder.KERNEL_WORKFLOW_PATH,
        "repository_id": builder.KERNEL_REPOSITORY_ID,
        "artifact_id": 29,
        "artifact_name": "kernel-linux-aarch64",
        "artifact_digest": artifact["digest"],
        "expires_at": artifact["expires_at"],
        "platform": "linux/arm64",
    }
    assert [endpoint for endpoint, _ in endpoints] == [
        "repos/AstralDeep/AstralKernel/actions/runs/17",
        "repos/AstralDeep/AstralKernel/actions/artifacts/29",
        "repos/AstralDeep/AstralKernel/actions/artifacts/29/zip",
    ]
    assert endpoints[-1][1] is True
    assert calls[0][1]["cwd"] == kernel_checkout
    assert calls[0][0][2:6] == ["verify", "--root", str(output), "--kind"]


@pytest.mark.parametrize("branch", ["main", "k/integration"])
def test_provider_identity_accepts_the_kernel_default_branch_and_its_retired_name(binding, branch):
    run, artifact, _ = _provider_fixture(binding)
    run["head_branch"] = artifact["workflow_run"]["head_branch"] = branch
    expected_digest = artifact["digest"].removeprefix("sha256:")
    assert builder._artifact_identity(run, artifact, 17, 29, expected_digest, "linux/arm64", binding) == (
        expected_digest, artifact["size_in_bytes"])


def test_provider_identity_requires_the_artifact_branch_to_match_its_run(binding):
    run, artifact, _ = _provider_fixture(binding)
    artifact["workflow_run"]["head_branch"] = "k/integration"
    expected_digest = artifact["digest"].removeprefix("sha256:")
    with pytest.raises(builder.BuildError, match="not bound to the verified run"):
        builder._artifact_identity(run, artifact, 17, 29, expected_digest, "linux/arm64", binding)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("conclusion", "failure", "successful push"),
    ("head_sha", "0" * 40, "branch or source"),
    ("head_branch", "candidate", "branch or source"),
    ("path", "other.yml", "unexpected workflow"),
    ("id", 18, "successful push"),
])
def test_fetch_rejects_unqualified_provider_run(binding, tmp_path, monkeypatch, field, value, message):
    run, artifact, _ = _provider_fixture(binding)
    run[field] = value
    expected_digest = artifact["digest"].removeprefix("sha256:")
    responses = iter((run, artifact))
    monkeypatch.setattr(builder, "gh_api", lambda *args, **kwargs: next(responses))
    with pytest.raises(builder.BuildError, match=message):
        builder.fetch_provider(17, 29, expected_digest, tmp_path / "out", "linux/arm64", binding)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("name", "kernel-linux-x86_64", "name does not match"),
    ("expired", True, "expired"),
    ("digest", "sha256:bad", "digest is missing or invalid"),
    ("expires_at", "not-a-date", "expiry is invalid"),
])
def test_fetch_rejects_unqualified_provider_artifact(binding, field, value, message):
    run, artifact, _ = _provider_fixture(binding)
    artifact[field] = value
    expected_digest = artifact["digest"].removeprefix("sha256:")
    with pytest.raises(builder.BuildError, match=message):
        builder._artifact_identity(run, artifact, 17, 29, expected_digest, "linux/arm64", binding)


def test_fetch_rejects_artifact_linkage_and_download_digest_mismatch(binding, tmp_path, monkeypatch):
    run, artifact, archive = _provider_fixture(binding)
    expected_digest = artifact["digest"].removeprefix("sha256:")
    artifact["workflow_run"]["head_branch"] = "other"
    with pytest.raises(builder.BuildError, match="not bound to the verified run"):
        builder._artifact_identity(run, artifact, 17, 29, expected_digest, "linux/arm64", binding)
    artifact["workflow_run"]["head_branch"] = "main"
    responses = iter((run, artifact, archive + b"bad"))
    monkeypatch.setattr(builder, "gh_api", lambda *args, **kwargs: next(responses))
    with pytest.raises(builder.BuildError, match="differs from its API size or digest"):
        builder.fetch_provider(17, 29, expected_digest, tmp_path / "out", "linux/arm64", binding)
    with pytest.raises(builder.BuildError, match="differs from the pinned digest"):
        builder._artifact_identity(run, artifact, 17, 29, "0" * 64, "linux/arm64", binding)


def test_gh_api_reads_json_and_binary_and_redacts_failures(monkeypatch):
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b'{"ok":true}'))
    assert builder.gh_api("repos/example") == {"ok": True}
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b"zip"))
    assert builder.gh_api("repos/example/archive", binary=True) == b"zip"
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b"no json"))
    with pytest.raises(builder.BuildError, match="invalid JSON"):
        builder.gh_api("repos/example")
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    with pytest.raises(builder.BuildError, match="API request failed"):
        builder.gh_api("repos/example")


def test_fetch_rejects_nonempty_output_before_extracting(binding, tmp_path, monkeypatch):
    run, artifact, archive = _provider_fixture(binding)
    responses = iter((run, artifact, archive))
    monkeypatch.setattr(builder, "gh_api", lambda *args, **kwargs: next(responses))
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("existing")
    with pytest.raises(builder.BuildError, match="must be empty"):
        builder.fetch_provider(17, 29, artifact["digest"].removeprefix("sha256:"), output,
                              "linux/arm64", binding)


@pytest.mark.parametrize("filename", ["../escape", "/absolute", "subdir/file", "astral-mailbox-linux-aarch64"])
def test_safe_provider_zip_rejects_unexpected_or_unsafe_members(tmp_path, filename):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(filename, b"x")
    with pytest.raises(builder.BuildError):
        builder._extract_provider_zip(stream.getvalue(), tmp_path, {"safe"})


def test_safe_provider_zip_rejects_symlink_and_oversized_members(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        info = __import__("zipfile").ZipInfo("safe")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"target")
    with pytest.raises(builder.BuildError, match="unsafe file"):
        builder._extract_provider_zip(stream.getvalue(), tmp_path, {"safe"})
    monkeypatch.setattr(builder, "MAX_PROVIDER_PAYLOAD_BYTES", 1)
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("safe", b"xx")
    with pytest.raises(builder.BuildError, match="too large"):
        builder._extract_provider_zip(stream.getvalue(), tmp_path, {"safe"})
