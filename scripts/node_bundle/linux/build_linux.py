"""Builds and checks a Linux node pack in Docker: it verifies the Kernel artifacts against the repository binding, assembles the pack in the pinned bookworm builder image and then runs the relocation check in a clean bookworm-slim container.
It drives assemble.py, relocation_check.py and install_kernel_binding.py, and writes the pack and a build receipt into one output directory.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECIPE = HERE.parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import install_kernel_binding as binding  # noqa: E402

PLATFORMS = {"linux-aarch64": "linux/arm64", "linux-x86_64": "linux/amd64"}
DOCKERFILE = HERE / "Dockerfile"
SOURCE_ENV_PREFIX = "NODE_BUNDLE_SOURCE_"
BUILD_ROOT = "/astral-node-build"
KERNEL_ARTIFACTS_ENV = "ASTRAL_KERNEL_ARTIFACTS"
NAME_PREFIX_ENV = "NODE_PACK_NAME_PREFIX"
DEFAULT_KERNEL_DIR = REPO / "build" / "kernel-binding"
RECEIPT_NAME = "node-pack-build.json"
ARG_PATTERN = re.compile(r"^ARG (\w+)=(\S+)$", re.MULTILINE)
RELOCATION_SCRIPT = r"""
set -eu
for name in python python3 python3.11; do
  if command -v "$name" >/dev/null 2>&1; then echo "$name is on PATH in the clean image" >&2; exit 1; fi
done
mkdir -p /opt/runner
tar -xzf "/pack/$ARCHIVE" -C /opt/runner
exec /opt/runner/python/bin/python3.11 -B /recipe/relocation_check.py /pack
"""


class BuildError(Exception):
    pass


def dockerfile_args(path: Path = DOCKERFILE) -> dict[str, str]:
    return dict(ARG_PATTERN.findall(path.read_text()))


def docker(*arguments: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *arguments], check=True, timeout=timeout)


def docker_output(*arguments: str) -> str:
    return subprocess.check_output(["docker", *arguments], text=True).strip()


def clean_commit(repo: Path) -> str:
    status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"], text=True,
    )
    if status.strip():
        raise BuildError("the Deep checkout must be clean and committed: the pack embeds its commit")
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def clone_source(repo: Path, commit: str, destination: Path) -> None:
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "checkout", "--quiet", "--detach", commit], check=True)


def verify_kernel_artifacts(kernel_dir: Path, platform: str, repo: Path = REPO) -> dict:
    manifest = binding.load_manifest(repo / "config" / "astral-kernel-binding.json")
    try:
        paths = binding.verify(kernel_dir, platform, manifest)
    except binding.BindingError as error:
        raise BuildError(
            f"the Kernel artifacts in {kernel_dir} do not match config/astral-kernel-binding.json: {error}"
        ) from error
    return {name: path.name for name, path in paths.items()}


def source_mounts(environment: dict[str, str]) -> tuple[list[str], list[str]]:
    mounts, variables = [], []
    for name, value in sorted(environment.items()):
        if name.startswith(SOURCE_ENV_PREFIX) and value:
            target = f"{BUILD_ROOT}/sources/{name[len(SOURCE_ENV_PREFIX):].lower()}"
            mounts += ["-v", f"{Path(value).resolve()}:{target}:ro"]
            variables += ["-e", f"{name}={target}"]
    return mounts, variables


def build_image(platform: str, tag: str) -> str:
    docker("build", "--quiet", "--platform", PLATFORMS[platform], "--target", "builder", "-t", tag,
           "-f", str(DOCKERFILE), str(HERE))
    return docker_output("image", "inspect", "--format", "{{.Id}}", tag)


def assemble_in_container(platform: str, tag: str, names: str, source: Path, kernel_dir: Path, out: Path,
                          environment: dict[str, str], timeout: int) -> None:
    mounts, variables = source_mounts(environment)
    docker(
        "run", "--rm", "--name", f"{names}build-{platform}", "--platform", PLATFORMS[platform],
        "-v", f"{source}:{BUILD_ROOT}/src:ro", "-v", f"{kernel_dir.resolve()}:{BUILD_ROOT}/kernel:ro",
        "-v", f"{out.resolve()}:{BUILD_ROOT}/out",
        *mounts, *variables, tag,
        "python3", f"{BUILD_ROOT}/src/scripts/node_bundle/assemble.py", "--platform", platform,
        "--out", f"{BUILD_ROOT}/out", "--work", f"{BUILD_ROOT}/work", "--kernel-dir", f"{BUILD_ROOT}/kernel",
        timeout=timeout,
    )


def check_relocation(platform: str, names: str, slim: str, out: Path, timeout: int) -> None:
    descriptor = json.loads((out / "node-pack.json").read_text())
    docker(
        "run", "--rm", "--network", "none", "--name", f"{names}relocation-{platform}",
        "--platform", PLATFORMS[platform], "-e", f"ARCHIVE={descriptor['archive']['file']}",
        "-v", f"{out.resolve()}:/pack:ro", "-v", f"{RECIPE}:/recipe:ro",
        slim, "sh", "-c", RELOCATION_SCRIPT,
        timeout=timeout,
    )


def build(platform: str, out: Path, kernel_dir: Path, names: str, timeout: int,
          environment: dict[str, str] | None = None, repo: Path = REPO) -> Path:
    if platform not in PLATFORMS:
        raise BuildError(f"{platform} is not a Linux pack platform")
    environment = os.environ if environment is None else environment
    verified = verify_kernel_artifacts(kernel_dir, platform, repo)
    commit = clean_commit(repo)
    args = dockerfile_args()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="node-pack-linux-") as scratch:
        source = Path(scratch) / "deep"
        clone_source(repo, commit, source)
        tag = f"{names}builder:{platform.removeprefix('linux-')}"
        image_id = build_image(platform, tag)
        assemble_in_container(platform, tag, names, source, kernel_dir, out, dict(environment), timeout)
    check_relocation(platform, names, args["BOOKWORM_SLIM"], out, timeout)
    receipt = {
        "platform": platform, "deep_commit": commit, "kernel_artifacts": verified,
        "builder_base": args["BOOKWORM"], "relocation_image": args["BOOKWORM_SLIM"], "apt_snapshot": args["SNAPSHOT"],
        "builder_image_id": image_id,
    }
    (out / RECEIPT_NAME).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    return out / "node-pack.json"


def default_kernel_dir() -> Path | None:
    if KERNEL_ARTIFACTS_ENV in os.environ:
        return Path(os.environ[KERNEL_ARTIFACTS_ENV])
    return DEFAULT_KERNEL_DIR if DEFAULT_KERNEL_DIR.is_dir() else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--kernel-dir", type=Path, default=default_kernel_dir(),
                        help=f"Kernel artifacts folder holding the bound wheel, mailbox and receipt; defaults to {KERNEL_ARTIFACTS_ENV} or build/kernel-binding")
    parser.add_argument("--name-prefix", default=os.environ.get(NAME_PREFIX_ENV, "node-pack-"),
                        help=f"prefix for the image tag and container names, or set {NAME_PREFIX_ENV}")
    parser.add_argument("--timeout", type=int, default=2400, help="seconds allowed for each container run")
    arguments = parser.parse_args(argv)
    if arguments.kernel_dir is None:
        sys.exit(f"linux pack: pass --kernel-dir, set {KERNEL_ARTIFACTS_ENV}, or run make kernel-artifacts for build/kernel-binding")
    try:
        descriptor = build(arguments.platform, arguments.out, arguments.kernel_dir, arguments.name_prefix,
                           arguments.timeout)
    except (BuildError, subprocess.SubprocessError, OSError) as error:
        sys.exit(f"linux pack: {error}")
    summary = json.loads(descriptor.read_text())
    print(f"{descriptor}: archive {summary['archive']['size']} bytes, installed {summary['installed_bytes']} bytes")


if __name__ == "__main__":
    shutil.which("docker") or sys.exit("linux pack: docker is required")
    main()
