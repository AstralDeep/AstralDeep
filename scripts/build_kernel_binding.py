"""Builds the Kernel artifacts pinned by this repository from a clean exact-source checkout.
The artifact manifest and local installer both verify outputs before CI publishes them to the current run."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "kernel-binding.json"
KERNEL_REPOSITORY = "https://github.com/AstralDeep/AstralKernel.git"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
PLATFORMS = {"linux/arm64": "linux-aarch64", "linux/amd64": "linux-x86_64"}
INSTALLER_PATH = ROOT / "scripts" / "install_kernel.py"


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
        else:
            write_ci_deployment(args.template, args.output, args.artifacts, args.platform, binding)
    except (BuildError, OSError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"Kernel artifact build: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
