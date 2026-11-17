"""Builds the Kernel artifacts pinned by this repository from a clean exact-source checkout.
The artifact manifest and local installer both verify outputs before CI publishes them to the current run."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "kernel-binding.json"
KERNEL_REPOSITORY = "https://github.com/AstralDeep/AstralKernel.git"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
PLATFORMS = {"linux/arm64": "linux-aarch64", "linux/amd64": "linux-x86_64"}
INSTALLER_PATH = ROOT / "scripts" / "install_kernel.py"
KERNEL_REPOSITORY_NAME = "AstralDeep/AstralKernel"
KERNEL_REPOSITORY_ID = 1455890728
# k/integration was the Kernel default branch before main; runs qualified there stay valid.
KERNEL_BRANCHES = frozenset({"main", "k/integration"})
KERNEL_WORKFLOW_PATH = ".github/workflows/ci.yml"
KERNEL_WORKFLOW_NAME = "Kernel qualification"
MAX_PROVIDER_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_PAYLOAD_BYTES = 64 * 1024 * 1024


class BuildError(ValueError):
    pass


def run_command(command: list[str], **kwargs: Any) -> None:
    subprocess.run(command, **kwargs)


def load_binding(path: Path = MANIFEST) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("kernel binding is missing or invalid") from exc
    require(isinstance(value, dict), "kernel binding object is invalid")
    source = value.get("source")
    require(value.get("format") == "astraldeep.kernel-binding/v1", "kernel binding format is invalid")
    require(isinstance(source, dict) and source.get("repository") == KERNEL_REPOSITORY,
            "kernel binding must name the official AstralKernel repository")
    require(isinstance(source.get("commit"), str) and SHA_PATTERN.fullmatch(source["commit"]) is not None,
            "kernel binding must pin a full immutable Kernel commit")
    require(isinstance(value.get("artifacts"), dict), "kernel binding artifact map is invalid")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def source_state(source: Path, expected: str) -> None:
    require(source.is_dir() and not source.is_symlink(), "Kernel source path must be a real checkout")
    try:
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        status = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"], text=True
        )
        remote = subprocess.check_output(["git", "-C", str(source), "remote", "get-url", "origin"],
                                         text=True).strip()
    except subprocess.CalledProcessError as exc:
        raise BuildError("Kernel source is not a complete Git checkout") from exc
    require(head == expected, "Kernel source HEAD differs from the pinned commit")
    require(not status, "Kernel source checkout is dirty")
    require(remote.rstrip("/") in {KERNEL_REPOSITORY, KERNEL_REPOSITORY.removesuffix(".git"),
                                    "git@github.com:AstralDeep/AstralKernel.git"},
            "Kernel source origin is not the official repository")


def checkout_source(destination: Path, binding: dict[str, Any], workspace: Path = ROOT) -> Path:
    require(not destination.is_symlink(), "Kernel checkout destination symlinks are refused")
    destination = destination.resolve()
    require(not destination.exists(), "Kernel checkout destination already exists")
    require(not destination.is_relative_to(workspace.resolve()), "Kernel source must be outside the candidate tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    commit = binding["source"]["commit"]
    run_command(["git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "1",
                 binding["source"]["repository"], str(destination)], check=True)
    run_command(["git", "-C", str(destination), "fetch", "--depth", "1", "origin", commit], check=True)
    run_command(["git", "-C", str(destination), "checkout", "--detach", commit], check=True)
    source_state(destination, commit)
    return destination


def installer_module():
    spec = importlib.util.spec_from_file_location("astralvessel_kernel_installer", INSTALLER_PATH)
    require(spec is not None and spec.loader is not None, "Kernel installer cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def produce(source: Path, output: Path, platform_name: str, binding: dict[str, Any]) -> dict[str, Path]:
    require(platform_name in PLATFORMS, "unsupported Linux artifact platform")
    commit = binding["source"]["commit"]
    require(not source.is_symlink(), "Kernel source path symlinks are refused")
    source = source.resolve()
    require(not source.is_relative_to(ROOT.resolve()), "Kernel source must be outside the candidate tree")
    if not source.exists():
        source = checkout_source(source, binding)
    source_state(source, commit)
    require(not output.is_symlink(), "Kernel artifact output symlinks are refused")
    output = output.resolve()
    require(not output.is_relative_to(ROOT.resolve()) and not output.is_relative_to(source),
            "Kernel artifacts must be written outside both source trees")
    if output.exists():
        require(output.is_dir() and not any(output.iterdir()), "Kernel artifact output must be empty")
    else:
        output.mkdir(parents=True)
    producer = source / "scripts" / "build-python.sh"
    manifest_tool = source / "scripts" / "artifact_manifest.py"
    require(producer.is_file() and manifest_tool.is_file(), "pinned Kernel artifact producer is missing")
    run_command([str(producer), "--platform", platform_name, "--out", str(output)], cwd=source, check=True)
    source_state(source, commit)
    receipt_path = output / "linux-build-manifest.json"
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("Kernel producer did not write a valid Linux artifact receipt") from exc
    require(receipt.get("source_commit") == commit and receipt.get("target") == platform_name,
            "Kernel artifact receipt source or target differs")
    run_command([sys.executable, str(manifest_tool), "verify", "--root", str(output), "--kind", "linux",
                 "--expected-source", commit], cwd=source, check=True)
    paths = installer_module().verify(output, PLATFORMS[platform_name], binding)
    source_state(source, commit)
    return paths


def gh_api(endpoint: str, *, binary: bool = False) -> Any:
    try:
        result = subprocess.run(["gh", "api", endpoint], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError("GitHub Actions artifact API request failed") from exc
    if binary:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError("GitHub Actions API returned invalid JSON") from exc


def _artifact_identity(run: dict[str, Any], artifact: dict[str, Any], run_id: int,
                       artifact_id: int, expected_digest: str, platform_name: str,
                       binding: dict[str, Any]) -> tuple[str, int]:
    require(platform_name in PLATFORMS, "unsupported Linux artifact platform")
    commit = binding["source"]["commit"]
    require(run.get("id") == run_id and run.get("event") == "push" and run.get("status") == "completed"
            and run.get("conclusion") == "success", "Kernel provider run is not a successful push")
    require(run.get("head_branch") in KERNEL_BRANCHES and run.get("head_sha") == commit,
            "Kernel provider run branch or source differs from the binding")
    repository = run.get("repository")
    require(isinstance(repository, dict) and repository.get("full_name") == KERNEL_REPOSITORY_NAME
            and repository.get("id") == KERNEL_REPOSITORY_ID,
            "Kernel provider run is not from the official repository")
    workflow = run.get("path")
    require(workflow == KERNEL_WORKFLOW_PATH and run.get("name") == KERNEL_WORKFLOW_NAME,
            "Kernel provider run used an unexpected workflow")
    artifact_run = artifact.get("workflow_run")
    require(artifact.get("id") == artifact_id and isinstance(artifact_run, dict)
            and artifact_run.get("id") == run_id and artifact_run.get("repository_id") == KERNEL_REPOSITORY_ID
            and artifact_run.get("head_repository_id") == KERNEL_REPOSITORY_ID
            and artifact_run.get("head_sha") == commit and artifact_run.get("head_branch") == run["head_branch"],
            "Kernel provider artifact is not bound to the verified run")
    expected_name = f"kernel-{PLATFORMS[platform_name]}"
    require(artifact.get("name") == expected_name, "Kernel provider artifact name does not match the platform")
    require(artifact.get("expired") is False, "Kernel provider artifact is expired")
    size = artifact.get("size_in_bytes")
    require(type(size) is int and 0 < size <= MAX_PROVIDER_ARCHIVE_BYTES,
            "Kernel provider artifact size is invalid")
    digest = artifact.get("digest")
    require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
            "Kernel provider artifact digest is missing or invalid")
    require(re.fullmatch(r"[0-9a-f]{64}", expected_digest) is not None
            and digest.removeprefix("sha256:") == expected_digest,
            "Kernel provider artifact digest differs from the pinned digest")
    expires_at = artifact.get("expires_at")
    require(isinstance(expires_at, str), "Kernel provider artifact expiry is missing")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BuildError("Kernel provider artifact expiry is invalid") from exc
    require(expiry.tzinfo is not None and expiry > datetime.now(UTC),
            "Kernel provider artifact is expired")
    return digest.removeprefix("sha256:"), size


def _extract_provider_zip(archive: bytes, output: Path, expected_names: set[str]) -> None:
    try:
        with ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            names = [entry.filename for entry in entries]
            require(len(names) == len(expected_names) and len(set(names)) == len(names)
                    and set(names) == expected_names,
                    "Kernel provider archive payload names are unexpected")
            total = 0
            for entry in entries:
                path = Path(entry.filename)
                mode = (entry.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                require(not entry.is_dir() and not path.is_absolute() and len(path.parts) == 1
                        and path.name == entry.filename and file_type in (0, stat.S_IFREG)
                        and not entry.flag_bits & 0x1 and entry.compress_type in (ZIP_STORED, ZIP_DEFLATED),
                        "Kernel provider archive contains an unsafe file")
                total += entry.file_size
                require(entry.file_size > 0 and total <= MAX_PROVIDER_PAYLOAD_BYTES,
                        "Kernel provider archive payload is too large")
            for entry in entries:
                (output / entry.filename).write_bytes(bundle.read(entry))
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise BuildError("Kernel provider artifact is not a valid safe ZIP") from exc


def fetch_provider(run_id: int, artifact_id: int, expected_digest: str, output: Path, platform_name: str,
                   binding: dict[str, Any]) -> dict[str, Path]:
    require(run_id > 0 and artifact_id > 0, "provider run and artifact IDs must be positive")
    require(platform_name in PLATFORMS, "unsupported Linux artifact platform")
    run = gh_api(f"repos/{KERNEL_REPOSITORY_NAME}/actions/runs/{run_id}")
    require(isinstance(run, dict), "Kernel provider run record is invalid")
    artifact = gh_api(f"repos/{KERNEL_REPOSITORY_NAME}/actions/artifacts/{artifact_id}")
    require(isinstance(artifact, dict), "Kernel provider artifact record is invalid")
    expected_digest, expected_size = _artifact_identity(run, artifact, run_id, artifact_id,
                                                        expected_digest, platform_name, binding)
    archive = gh_api(f"repos/{KERNEL_REPOSITORY_NAME}/actions/artifacts/{artifact_id}/zip", binary=True)
    require(isinstance(archive, bytes) and len(archive) == expected_size
            and hashlib.sha256(archive).hexdigest() == expected_digest,
            "downloaded Kernel provider archive differs from its API size or digest")
    require(not output.is_symlink(), "Kernel artifact output symlinks are refused")
    output = output.resolve()
    require(not output.is_relative_to(ROOT.resolve()), "Kernel artifacts must be written outside the candidate tree")
    if output.exists():
        require(output.is_dir() and not any(output.iterdir()), "Kernel artifact output must be empty")
    else:
        output.mkdir(parents=True)
    key = PLATFORMS[platform_name]
    entries = binding["artifacts"].get(key)
    require(isinstance(entries, dict) and isinstance(entries.get("wheel"), dict)
            and isinstance(entries.get("mailbox"), dict), f"kernel binding is incomplete for {key}")
    expected_names = {entries["wheel"].get("file"), entries["mailbox"].get("file"), "linux-build-manifest.json"}
    require(all(isinstance(name, str) and name and Path(name).name == name for name in expected_names),
            "Kernel binding artifact names are invalid")
    _extract_provider_zip(archive, output, expected_names)
    with tempfile.TemporaryDirectory(prefix="astral-kernel-source-", dir=output.parent) as temp:
        source = checkout_source(Path(temp) / "source", binding)
        manifest_tool = source / "scripts" / "artifact_manifest.py"
        require(manifest_tool.is_file(), "pinned Kernel artifact manifest verifier is missing")
        receipt = json.loads((output / "linux-build-manifest.json").read_text())
        require(receipt.get("source_commit") == binding["source"]["commit"]
                and receipt.get("target") == platform_name,
                "Kernel provider manifest source or target differs")
        run_command([sys.executable, str(manifest_tool), "verify", "--root", str(output), "--kind", "linux",
                     "--expected-source", binding["source"]["commit"]], cwd=source, check=True)
        source_state(source, binding["source"]["commit"])
    paths = installer_module().verify(output, key, binding)
    (output / "provider-receipt.json").write_text(json.dumps({
        "run_id": run_id,
        "source_commit": binding["source"]["commit"],
        "workflow": run["path"],
        "repository_id": KERNEL_REPOSITORY_ID,
        "artifact_id": artifact_id,
        "artifact_name": artifact["name"],
        "artifact_digest": f"sha256:{expected_digest}",
        "expires_at": artifact["expires_at"],
        "platform": platform_name,
    }, sort_keys=True, indent=2) + "\n")
    return paths


def write_ci_deployment(template: Path, output: Path, artifacts: Path, platform_name: str,
                        binding: dict[str, Any]) -> Path:
    require(platform_name in PLATFORMS, "unsupported Linux artifact platform")
    require(not artifacts.is_symlink(), "Kernel artifact input symlinks are refused")
    artifacts = artifacts.resolve()
    installer_module().verify(artifacts, PLATFORMS[platform_name], binding)
    require(not output.is_symlink(), "generated CI deployment symlinks are refused")
    require(template.is_file() and output.parent.resolve() == template.parent.resolve(),
            "generated CI deployment must share the template directory")
    require(output.resolve() != template.resolve() and not output.exists(),
            "generated CI deployment already exists or is the template")
    source = template.read_text()
    require("[executor]" not in source, "CI deployment template already declares an executor section")
    output.write_text(f'{source.rstrip()}\n\n[executor]\nkernel_artifacts = {json.dumps(str(artifacts))}\n')
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identity = commands.add_parser("source-info")
    identity.add_argument("--binding", type=Path, default=MANIFEST)
    build = commands.add_parser("produce")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--platform", choices=tuple(PLATFORMS), required=True)
    build.add_argument("--binding", type=Path, default=MANIFEST)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--run-id", type=int, required=True)
    fetch.add_argument("--artifact-id", type=int, required=True)
    fetch.add_argument("--expected-digest", required=True)
    fetch.add_argument("--output", type=Path, required=True)
    fetch.add_argument("--platform", choices=tuple(PLATFORMS), required=True)
    fetch.add_argument("--binding", type=Path, default=MANIFEST)
    deployment = commands.add_parser("prepare-deployment")
    deployment.add_argument("--template", type=Path, default=ROOT / "deploy" / "deployment.toml")
    deployment.add_argument("--output", type=Path, required=True)
    deployment.add_argument("--artifacts", type=Path, required=True)
    deployment.add_argument("--platform", choices=tuple(PLATFORMS), required=True)
    deployment.add_argument("--binding", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    try:
        binding = load_binding(args.binding)
        if args.command == "source-info":
            print(f"commit={binding['source']['commit']}")
        elif args.command == "produce":
            produce(args.source, args.output, args.platform, binding)
        elif args.command == "fetch":
            fetch_provider(args.run_id, args.artifact_id, args.expected_digest, args.output, args.platform, binding)
        else:
            write_ci_deployment(args.template, args.output, args.artifacts, args.platform, binding)
    except (BuildError, OSError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"Kernel artifact build: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
