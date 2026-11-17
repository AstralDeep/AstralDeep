"""Verifies and installs the pinned kernel wheel and mailbox from the artifacts build context.
The executor image can verify tickets only when both artifacts match config/kernel-binding.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "kernel-binding.json"
ARCHITECTURES = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}
HINT = "set [executor] kernel_artifacts in deployment.toml, or pass --build-context kernel-artifacts=<folder>"
MAILBOX_NAME = "astral-mailbox"
DEFAULT_BIN_DIR = Path("/usr/local/bin")


def platform_key(machine: str | None = None, system: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    system = (system or platform.system()).lower()
    architecture = ARCHITECTURES.get(machine)
    if system != "linux" or architecture is None:
        raise ValueError(f"no kernel artifacts for {system}/{machine}")
    return f"linux-{architecture}"


def verify(directory: Path, key: str, manifest: dict) -> dict[str, Path]:
    entries = manifest["artifacts"].get(key)
    if not isinstance(entries, dict) or not {"wheel", "mailbox"} <= entries.keys():
        raise ValueError(f"kernel-binding.json pins no kernel wheel and mailbox for {key}")
    verified = {}
    for name in ("wheel", "mailbox"):
        entry = entries[name]
        path = directory / entry["file"]
        if not path.is_file():
            raise ValueError(f"missing pinned kernel {name} {entry['file']} in {directory}; {HINT}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"{path.name} has sha256 {digest}, but kernel-binding.json pins {entry['sha256']}")
        verified[name] = path
    return verified


def install(directory: Path, key: str, bin_directory: Path, manifest: dict) -> None:
    verified = verify(directory, key, manifest)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps", "--no-index",
                    "--force-reinstall", str(verified["wheel"])], check=True)
    subprocess.run([sys.executable, "-c", f"import {manifest['python']['import']}"], check=True)
    bin_directory.mkdir(parents=True, exist_ok=True)
    target = bin_directory / MAILBOX_NAME
    shutil.copyfile(verified["mailbox"], target)
    target.chmod(0o755)


def main(directory: str, binding: Path = MANIFEST, bin_directory: Path = DEFAULT_BIN_DIR) -> int:
    try:
        manifest = json.loads(binding.read_text())
        install(Path(directory), platform_key(), bin_directory, manifest)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"kernel binding: {exc}", file=sys.stderr)
        return 1
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--binding", type=Path, default=MANIFEST)
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    args = parser.parse_args(argv)
    return main(str(args.artifacts), args.binding, args.bin_dir)


if __name__ == "__main__":
    sys.exit(cli())
