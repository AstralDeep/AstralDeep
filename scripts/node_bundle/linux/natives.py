"""Copies the Debian native tools a Linux node pack ships, with the shared libraries they need, into the pack's native folder and points their search paths at the pack.
assemble.py calls it before sealing, and elfaudit.py then proves nothing resolves outside the pack except glibc.
"""

import os
import shutil
import subprocess
from pathlib import Path

import elfaudit

NATIVE_DIR = "native"
MULTIARCH = {"aarch64": "aarch64-linux-gnu", "x86_64": "x86_64-linux-gnu"}


class NativeError(Exception):
    pass


def library_directories(architecture: str) -> list[Path]:
    triplet = MULTIARCH[architecture]
    return [Path(f"/lib/{triplet}"), Path(f"/usr/lib/{triplet}"), Path("/lib"), Path("/usr/lib")]


def locate(name: str, directories: list[Path]) -> Path:
    for directory in directories:
        candidate = directory / name
        if candidate.is_file():
            return candidate.resolve()
    raise NativeError(f"{name} is not installed in the build image")


def locate_binary(name: str) -> Path:
    for directory in ("/usr/bin", "/bin"):
        candidate = Path(directory) / name
        if candidate.is_file():
            return candidate.resolve()
    raise NativeError(f"{name} is not installed in the build image")


def package_versions(packages: list[str]) -> list[dict]:
    versions = []
    for package in packages:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", package], capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or not result.stdout:
            raise NativeError(f"package {package} is not installed in the build image")
        versions.append({"name": package, "version": result.stdout})
    return versions


def set_search_path(path: Path, value: str) -> None:
    subprocess.run(["patchelf", "--set-rpath", value, str(path)], check=True)


def copy_library(name: str, root: Path, directories: list[Path], pending: list[Path]) -> None:
    target = root / "lib" / name
    if name in elfaudit.SYSTEM_LIBRARIES or target.exists():
        return
    shutil.copy2(locate(name, directories), target)
    set_search_path(target, "$ORIGIN")
    pending.append(target)


def close_over_needs(root: Path, directories: list[Path], pending: list[Path]) -> None:
    while pending:
        for needed in elfaudit.read_dynamic(pending.pop()).needed:
            if not needed.startswith("$ORIGIN"):
                copy_library(needed, root, directories, pending)


def link_system_needs(stage: Path, root: Path, directories: list[Path]) -> None:
    for path in elfaudit.elf_files(stage):
        missing = [name for name in elfaudit.unresolved_needs(path, stage.resolve()) if not name.startswith("$ORIGIN")]
        found = [name for name in missing if any((directory / name).is_file() for directory in directories)]
        if not found:
            continue
        pending: list[Path] = []
        for name in found:
            copy_library(name, root, directories, pending)
        close_over_needs(root, directories, pending)
        existing = elfaudit.read_dynamic(path)
        relative = os.path.relpath(root.resolve() / "lib", path.resolve().parent)
        set_search_path(path, ":".join([*existing.runpath, *existing.rpath, f"$ORIGIN/{relative}"]))


def bundle(stage: Path, spec: dict, architecture: str) -> dict:
    root = stage / NATIVE_DIR
    shutil.rmtree(root, ignore_errors=True)
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir()
    directories = library_directories(architecture)
    versions = package_versions(spec["packages"])
    pending = []
    for name in spec["binaries"]:
        target = root / "bin" / name
        shutil.copy2(locate_binary(name), target)
        set_search_path(target, "$ORIGIN/../lib")
        pending.append(target)
    for name in spec["libraries"]:
        target = root / "lib" / name
        shutil.copy2(locate(name, directories), target)
        set_search_path(target, "$ORIGIN")
        pending.append(target)
    close_over_needs(root, directories, pending)
    link_system_needs(stage, root, directories)
    for name, source in spec["data"].items():
        destination = root / "share" / name
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(Path(source).resolve(), destination)
    licenses = root / "licenses"
    licenses.mkdir()
    for entry in versions:
        notice = Path("/usr/share/doc") / entry["name"] / "copyright"
        if notice.is_file():
            shutil.copy2(notice.resolve(), licenses / f"{entry['name']}.copyright")
    return {"packages": versions, "libraries": sorted(path.name for path in (root / "lib").iterdir())}
