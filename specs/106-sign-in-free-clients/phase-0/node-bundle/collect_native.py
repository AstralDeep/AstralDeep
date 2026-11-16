"""Stages Homebrew bottles for poppler and libmagic into a relocatable native tree for the node bundle.
It verifies each bottle digest against Homebrew's metadata, copies only the dylibs the tools load, rewrites install names to @loader_path and re-signs ad hoc. build_bundle.sh runs it after the Python layer is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

BOTTLE_TAG = "arm64_golden_gate"
ROOT_FORMULAE = ("poppler", "libmagic")
ROOT_BINARIES = {"poppler": ("pdftoppm", "pdfinfo")}
PLACEHOLDER = re.compile(r"^@@HOMEBREW_(PREFIX|CELLAR)@@/")
FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir>/System/Library/Fonts</dir>
  <dir>/System/Library/Fonts/Supplemental</dir>
  <dir>/Library/Fonts</dir>
  <cachedir prefix="xdg">fontconfig</cachedir>
</fontconfig>
"""


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def brew_json(names: list[str]) -> dict[str, dict]:
    data = json.loads(run("brew", "info", "--json=v2", *names))
    return {formula["name"]: formula for formula in data["formulae"]}


def runtime_closure() -> list[str]:
    closure: set[str] = set(ROOT_FORMULAE)
    pending = list(ROOT_FORMULAE)
    while pending:
        formulae = brew_json(pending)
        pending = []
        for formula in formulae.values():
            for dependency in formula["dependencies"]:
                if dependency not in closure:
                    closure.add(dependency)
                    pending.append(dependency)
    return sorted(closure)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stage_bottles(names: list[str], staging: Path, manifest: list[dict]) -> None:
    metadata = brew_json(names)
    for name in names:
        files = metadata[name]["bottle"]["stable"]["files"]
        tag = BOTTLE_TAG if BOTTLE_TAG in files else "all"
        bottle = files[tag]
        path = Path(run("brew", "--cache", f"--bottle-tag={tag}", name).strip())
        if not path.exists():
            run("brew", "fetch", "--formula", "--force-bottle", name)
        actual = sha256_file(path.resolve())
        if actual != bottle["sha256"]:
            raise SystemExit(f"bottle digest mismatch for {name}: {actual} != {bottle['sha256']}")
        with tarfile.open(path.resolve()) as archive:
            archive.extractall(staging, filter="tar")
        manifest.append({"formula": name, "version": metadata[name]["versions"]["stable"], "bottle_sha256": actual})


def load_commands(path: Path) -> list[str]:
    lines = run("otool", "-L", str(path)).splitlines()[1:]
    return [line.strip().split(" (")[0] for line in lines]


def rpaths(path: Path) -> list[str]:
    output = run("otool", "-l", str(path))
    return re.findall(r"LC_RPATH\n\s+cmdsize \d+\n\s+path (\S+)", output)


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def index_dylibs(staging: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for candidate in staging.glob("*/*/lib/**/*.dylib"):
        if is_macho(candidate):
            found.setdefault(candidate.name, candidate)
    for candidate in staging.glob("*/*/lib/**/*.dylib"):
        if candidate.is_symlink():
            found.setdefault(candidate.name, candidate.resolve())
    return found


def dependency_name(command: str) -> str | None:
    if command.startswith(("/usr/lib/", "/System/")):
        return None
    return os.path.basename(command)


def collect(staging: Path, out: Path) -> list[str]:
    dylibs = index_dylibs(staging)
    (out / "bin").mkdir(parents=True)
    (out / "lib").mkdir()
    queue: list[Path] = []
    for formula, names in ROOT_BINARIES.items():
        for name in names:
            source = next(staging.glob(f"{formula}/*/bin/{name}"))
            shutil.copy2(source, out / "bin" / name)
            queue.append(out / "bin" / name)
    queue.append(_copy_lib(dylibs["libmagic.1.dylib"], "libmagic.1.dylib", out))
    seen: set[str] = {"libmagic.1.dylib"}
    while queue:
        current = queue.pop()
        for command in load_commands(current):
            name = dependency_name(command)
            if name is None or name == current.name or name in seen:
                continue
            seen.add(name)
            if name not in dylibs:
                raise SystemExit(f"unresolved dependency {command} of {current.name}")
            queue.append(_copy_lib(dylibs[name], name, out))
    return sorted(seen)


def _copy_lib(source: Path, name: str, out: Path) -> Path:
    destination = out / "lib" / name
    shutil.copy2(source.resolve(), destination)
    destination.chmod(0o755)
    return destination


def rewrite(out: Path) -> None:
    targets = [*sorted((out / "bin").iterdir()), *sorted((out / "lib").iterdir())]
    libs = {path.name for path in (out / "lib").iterdir()}
    for path in targets:
        relative = "../lib" if path.parent.name == "bin" else "."
        arguments: list[str] = []
        if path.parent.name == "lib":
            arguments += ["-id", f"@loader_path/{path.name}"]
        for command in load_commands(path):
            name = dependency_name(command)
            if name in libs and name != path.name and not command.startswith("@loader_path/"):
                arguments += ["-change", command, f"@loader_path/{relative}/{name}".replace("/./", "/")]
        for rpath in dict.fromkeys(rpaths(path)):
            arguments += ["-delete_rpath", rpath]
        if arguments:
            run("install_name_tool", *arguments, str(path))
        run("codesign", "--force", "--sign", "-", str(path))


def assert_clean(out: Path) -> None:
    offenders = []
    for path in [*(out / "bin").iterdir(), *(out / "lib").iterdir()]:
        for command in load_commands(path):
            if PLACEHOLDER.match(command) or command.startswith(("/opt/homebrew", "@rpath")):
                offenders.append(f"{path.name}: {command}")
    if offenders:
        raise SystemExit("non-relocatable load commands remain:\n" + "\n".join(offenders))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    for directory in (args.out, args.staging):
        if directory.exists():
            shutil.rmtree(directory)
    args.staging.mkdir(parents=True)
    names = runtime_closure()
    manifest: list[dict] = []
    stage_bottles(names, args.staging, manifest)
    libraries = collect(args.staging, args.out)
    rewrite(args.out)
    assert_clean(args.out)
    magic = next(args.staging.glob("libmagic/*/share/misc/magic.mgc"))
    (args.out / "share/misc").mkdir(parents=True)
    shutil.copy2(magic, args.out / "share/misc/magic.mgc")
    (args.out / "etc/fonts").mkdir(parents=True)
    (args.out / "etc/fonts/fonts.conf").write_text(FONTS_CONF)
    args.manifest.write_text(json.dumps({"bottle_tag": BOTTLE_TAG, "bottles": manifest, "libraries": libraries}, indent=2) + "\n")
    print(f"staged {len(libraries)} libraries from {len(manifest)} bottles", file=sys.stderr)


if __name__ == "__main__":
    main()
