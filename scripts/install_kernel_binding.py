"""Verifies and installs the pinned kernel Python wheel and the mailbox binary from an artifacts directory, so the node image carries exactly the digests named in config/astral-kernel-binding.json.
The Dockerfile runs it with the artifacts build context, its pin command rewrites the manifest digests from staged files, and moving to a newer kernel needs no code change in the node."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import zipfile
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
    actual = sha256(path)
    if actual != entry["sha256"]:
        raise BindingError(mismatch_report(path, entry["sha256"], actual))
    return path


def record_lines(path: Path) -> List[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
            if not names:
                return ["RECORD: not present in archive"]
            text = archive.read(names[0]).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError) as exc:
        return [f"RECORD: unreadable ({type(exc).__name__}: {exc})"]
    return ["RECORD (path,hash,size):"] + [f"  {line}" for line in text.splitlines() if line.strip()]


def mismatch_report(path: Path, expected: str, actual: str) -> str:
    lines = [f"digest mismatch for {path.name}", f"  expected sha256: {expected}", f"  actual sha256:   {actual}"]
    if path.suffix == ".whl":
        lines.extend(record_lines(path))
    return "\n".join(lines)


def artifacts_for(manifest: Dict[str, Any], key: str) -> Dict[str, Dict[str, str]]:
    found = manifest["artifacts"].get(key)
    if not isinstance(found, dict) or not {"wheel", "mailbox"} <= set(found):
        raise BindingError(f"manifest has no artifacts for {key}")
    return found


def verify(directory: Path, key: str, manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    entries = artifacts_for(manifest or load_manifest(), key)
    return {name: verified(directory, entry) for name, entry in entries.items()}


def pin(
    directory: Path, key: str, wheel: str, mailbox: str, commit: str, manifest: Dict[str, Any]
) -> Dict[str, Any]:
    entries = {}
    for name, file in (("wheel", wheel), ("mailbox", mailbox)):
        path = directory / file
        if not path.is_file():
            raise BindingError(f"missing artifact {file}")
        entries[name] = {"file": file, "sha256": sha256(path)}
    pinned = json.loads(json.dumps(manifest))
    pinned["artifacts"][key] = entries
    pinned.setdefault("source", {})["commit"] = commit
    return pinned


def install(directory: Path, key: str, bin_dir: Path, manifest: Optional[Dict[str, Any]] = None) -> None:
    manifest = manifest or load_manifest()
    paths = verify(directory, key, manifest)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps", "--no-index", "--force-reinstall", str(paths["wheel"])],
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
    parser.add_argument("command", choices=["verify", "install", "pin"])
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    parser.add_argument("--platform")
    parser.add_argument("--wheel")
    parser.add_argument("--mailbox")
    parser.add_argument("--commit")
    args = parser.parse_args(argv)
    try:
        key = args.platform or platform_key()
        if args.command == "pin":
            if not (args.wheel and args.mailbox and args.commit):
                raise BindingError("pin needs --wheel and --mailbox file names and --commit")
            pinned = pin(args.artifacts, key, args.wheel, args.mailbox, args.commit, load_manifest())
            MANIFEST.write_text(json.dumps(pinned, indent=2) + "\n")
        elif args.command == "verify":
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
