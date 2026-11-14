"""Prepares the exact pinned Linux Kernel wheel and mailbox for Docker and CI builds.
The Kernel's own builder supplies artifacts, and the installer verifies their committed digests before any consumer uses them."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

if __package__:
    from . import install_kernel_binding as binding
else:
    import install_kernel_binding as binding

PLATFORMS = {"linux-aarch64": "linux/arm64", "linux-x86_64": "linux/amd64"}


def prepare(root: Path, output: Path, *, key: Optional[str] = None) -> None:
    root, output = root.resolve(), output.resolve()
    manifest = binding.load_manifest(root / "config/astral-kernel-binding.json")
    key = key or binding.platform_key(system="Linux")
    binding.artifacts_for(manifest, key)
    if key not in PLATFORMS:
        raise binding.BindingError(f"unsupported build platform: {key}")
    expected = manifest.get("source", {}).get("commit", "")
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{40}", expected) is None:
        raise binding.BindingError("manifest needs an immutable source commit")
    source = root / "components/AstralKernel"
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    if head != expected:
        raise binding.BindingError("Kernel source does not match the binding manifest")
    dirty = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise binding.BindingError("Kernel source is dirty")
    if output.exists() and any(output.iterdir()):
        binding.verify(output, key, manifest)
        return
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["bash", str(source / "scripts/build-python.sh"), "--platform", PLATFORMS[key], "--out", str(output)],
        check=True,
    )
    binding.verify(output, key, manifest)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("build/kernel-binding"))
    parser.add_argument("--platform", choices=sorted(PLATFORMS))
    args = parser.parse_args(argv)
    try:
        prepare(args.root, args.output, key=args.platform)
    except (binding.BindingError, OSError, subprocess.CalledProcessError) as exc:
        print(f"kernel binding: {exc}", file=sys.stderr)
        return 1
    print("kernel binding artifacts: verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
