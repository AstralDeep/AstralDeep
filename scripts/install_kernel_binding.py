"""Verifies and installs the pinned kernel Python wheel and the mailbox binary from an artifacts directory, so the node image carries exactly the digests named in config/astral-kernel-binding.json.
The Dockerfile runs it with the artifacts build context, and moving to a newer kernel is a change to that manifest and the artifacts, with no code change in the node."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

MANIFEST = Path(__file__).resolve().parent.parent / "config" / "astral-kernel-binding.json"
FORMAT = "astraldeep.kernel-binding/v1"
DEFAULT_BIN_DIR = Path("/opt/astral-kernel/bin")
MAILBOX_NAME = "astral-mailbox"
ARCHITECTURES = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}


class BindingError(Exception):
    pass


def load_manifest(path: Path = MANIFEST) -> Dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BindingError(f"manifest unreadable: {exc}") from exc
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("artifacts"), dict):
        raise BindingError("manifest format is not recognized")
    return manifest


def platform_key(machine: Optional[str] = None, system: Optional[str] = None) -> str:
    system = (system or platform.system()).lower()
    architecture = ARCHITECTURES.get((machine or platform.machine()).lower())
    if system != "linux" or architecture is None:
        raise BindingError(f"no kernel artifacts for {system}/{machine or platform.machine()}")
    return f"linux-{architecture}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(directory: Path, entry: Dict[str, str]) -> Path:
    path = directory / entry["file"]
    if not path.is_file():
        raise BindingError(f"missing artifact {entry['file']}")
    if sha256(path) != entry["sha256"]:
        raise BindingError(f"digest mismatch for {entry['file']}")
    return path


def artifacts_for(manifest: Dict[str, Any], key: str) -> Dict[str, Dict[str, str]]:
    found = manifest["artifacts"].get(key)
    if not isinstance(found, dict) or not {"wheel", "mailbox"} <= set(found):
        raise BindingError(f"manifest has no artifacts for {key}")
    return found


def verify(directory: Path, key: str, manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    entries = artifacts_for(manifest or load_manifest(), key)
    return {name: verified(directory, entry) for name, entry in entries.items()}


def install(directory: Path, key: str, bin_dir: Path, manifest: Optional[Dict[str, Any]] = None) -> None:
    manifest = manifest or load_manifest()
    paths = verify(directory, key, manifest)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps", "--no-index", str(paths["wheel"])],
        check=True,
    )
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / MAILBOX_NAME
    shutil.copyfile(paths["mailbox"], target)
    target.chmod(0o755)
    module = manifest["python"]["import"]
    subprocess.run([sys.executable, "-c", f"import {module}"], check=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify", "install"])
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args(argv)
    if args.allow_missing and not (args.artifacts.is_dir() and any(args.artifacts.iterdir())):
        print("kernel binding artifacts not provided, the node runs without the mesh peer")
        return 0
    try:
        key = platform_key()
        if args.command == "verify":
            verify(args.artifacts, key)
        else:
            install(args.artifacts, key, args.bin_dir)
    except (BindingError, subprocess.CalledProcessError) as exc:
        print(f"kernel binding: {exc}", file=sys.stderr)
        return 1
    print(f"kernel binding {args.command}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
