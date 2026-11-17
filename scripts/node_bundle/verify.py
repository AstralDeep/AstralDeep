"""Fetches a node pack descriptor, checks its archive and every file against the manifest the way the Mac app's installer does, and unpacks the tree into a directory.
It imports nothing from the pack, so the publisher, the node installer and the qualification run can all call it before they trust a pack.
"""

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

import manifest as pack_manifest

MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "[::1]"}
PLATFORMS = {"macos-arm64", "linux-x86_64", "linux-aarch64"}
DIGEST = re.compile(r"[0-9a-f]{64}")
PROBLEM_LIMIT = 20


class VerifyError(Exception):
    pass


def permitted(url: urllib.parse.ParseResult) -> bool:
    if url.scheme == "https":
        return bool(url.hostname)
    if url.scheme == "http":
        return (url.hostname or "").lower() in LOOPBACK_HOSTS
    return url.scheme in ("", "file")


def fetch(location: str, limit: int | None = None) -> bytes:
    url = urllib.parse.urlparse(location)
    if not permitted(url):
        raise VerifyError(f"unsupported address {location}")
    if url.scheme in ("http", "https"):
        with urllib.request.urlopen(location, timeout=60) as response:
            data = response.read(limit + 1 if limit else -1)
    else:
        path = Path(urllib.request.url2pathname(url.path) if url.scheme == "file" else location)
        with path.open("rb") as handle:
            data = handle.read(limit + 1 if limit else -1)
    if limit is not None and len(data) > limit:
        raise VerifyError(f"{location} is larger than {limit} bytes")
    return data


def sibling(location: str, name: str) -> str:
    url = urllib.parse.urlparse(location)
    if url.scheme in ("http", "https", "file"):
        return urllib.parse.urljoin(location, name)
    return str(Path(location).resolve().parent / name)


def parse_descriptor(raw: bytes, pinned: str | None) -> dict:
    digest = pack_manifest.sha256_bytes(raw)
    if pinned and pinned.lower() != digest:
        raise VerifyError("the descriptor does not match its pinned SHA-256")
    try:
        descriptor = json.loads(raw)
        archive = descriptor["archive"]
        fields = (descriptor["version"], descriptor["platform"], archive["file"], archive["size"],
                  archive["sha256"], descriptor["installed_bytes"], descriptor["manifest_sha256"])
    except (ValueError, KeyError, TypeError) as error:
        raise VerifyError(f"the descriptor is unreadable: {error}") from error
    if descriptor.get("format") != pack_manifest.DESCRIPTOR_FORMAT:
        raise VerifyError("the descriptor format is not recognized")
    version, platform, name, size, archive_sha, installed, manifest_sha = fields
    if platform not in PLATFORMS:
        raise VerifyError(f"unsupported platform {platform}")
    valid = (
        isinstance(name, str) and name not in ("", ".", "..") and "/" not in name
        and isinstance(size, int) and size > 0 and isinstance(installed, int) and installed > 0
        and isinstance(archive_sha, str) and DIGEST.fullmatch(archive_sha)
        and isinstance(manifest_sha, str) and DIGEST.fullmatch(manifest_sha)
        and isinstance(version, str) and pack_manifest.VERSION_PATTERN.fullmatch(version)
    )
    if not valid:
        raise VerifyError("the descriptor fields are out of range")
    descriptor["descriptor_sha256"] = digest
    return descriptor


def tree_problems(root: Path, manifest: dict) -> list[str]:
    found: dict[str, int] = {}
    for current, directories, names in os.walk(root, followlinks=False):
        base = Path(current)
        for name in names + [d for d in directories if (base / d).is_symlink()]:
            path = base / name
            found[path.relative_to(root).as_posix()] = os.lstat(path).st_mode
    found.pop(pack_manifest.MANIFEST_NAME, None)
    expected = {}
    for entry in manifest["files"]:
        expected.setdefault(entry["path"], entry)
    problems = [f"unexpected file {path}" for path in sorted(found) if path not in expected]
    for path, entry in sorted(expected.items()):
        mode = found.get(path)
        if mode is None:
            problems.append(f"missing file {path}")
        elif "link" in entry:
            target = os.readlink(root / path) if stat.S_ISLNK(mode) else None
            if target != entry["link"]:
                problems.append(f"link changed {path}")
            elif pack_manifest.link_escapes(path, target):
                problems.append(f"link leaves the pack {path}")
        elif not stat.S_ISREG(mode):
            problems.append(f"not a regular file {path}")
        else:
            target = root / path
            if target.stat().st_size != entry.get("size") or pack_manifest.sha256_file(target) != entry.get("sha256"):
                problems.append(f"content changed {path}")
            elif bool(entry.get("executable")) != bool(mode & stat.S_IXUSR):
                problems.append(f"mode changed {path}")
    return problems


def load_manifest(path: Path, descriptor: dict) -> dict:
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise VerifyError("the archive has no readable manifest")
    if pack_manifest.sha256_file(path) != descriptor["manifest_sha256"]:
        raise VerifyError("the manifest does not match the descriptor")
    try:
        manifest = json.loads(path.read_text())
        files = manifest["files"]
        for entry in files:
            entry["path"]
    except (ValueError, KeyError, TypeError) as error:
        raise VerifyError(f"the manifest is unreadable: {error}") from error
    if manifest.get("version") != descriptor["version"] or manifest.get("platform") != descriptor["platform"]:
        raise VerifyError("the manifest does not match the descriptor")
    if manifest.get("installed_bytes") != descriptor["installed_bytes"]:
        raise VerifyError("the manifest size does not match the descriptor")
    return manifest


def check_tree(root: Path, descriptor: dict) -> dict:
    manifest = load_manifest(root / pack_manifest.MANIFEST_NAME, descriptor)
    problems = tree_problems(root, manifest)
    if problems:
        raise VerifyError("the tree differs from its manifest: " + "; ".join(problems[:PROBLEM_LIMIT]))
    return manifest


def install(location: str, pinned: str | None, into: Path) -> dict:
    if into.exists() and any(into.iterdir()):
        raise VerifyError(f"{into} is not empty")
    descriptor = parse_descriptor(fetch(location, MAX_DESCRIPTOR_BYTES), pinned)
    staging = into.parent / f".{into.name}.staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        archive = staging / "pack.tar.gz"
        archive.write_bytes(fetch(sibling(location, descriptor["archive"]["file"]), descriptor["archive"]["size"]))
        if archive.stat().st_size != descriptor["archive"]["size"]:
            raise VerifyError("the archive size does not match the descriptor")
        if pack_manifest.sha256_file(archive) != descriptor["archive"]["sha256"]:
            raise VerifyError("the archive does not match its SHA-256")
        tree = staging / "tree"
        tree.mkdir()
        try:
            with tarfile.open(archive) as bundle:
                bundle.extractall(tree, filter="data")
        except (tarfile.TarError, OSError) as error:
            raise VerifyError(f"the archive cannot be unpacked: {error}") from error
        manifest = check_tree(tree, descriptor)
        if into.exists():
            into.rmdir()
        into.parent.mkdir(parents=True, exist_ok=True)
        tree.rename(into)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"version": descriptor["version"], "platform": descriptor["platform"],
            "manifest_sha256": descriptor["manifest_sha256"], "root": str(into), "files": len(manifest["files"])}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("descriptor", help="path or address of node-pack.json")
    parser.add_argument("--sha256", required=True, help="SHA-256 the descriptor must have")
    parser.add_argument("--into", type=Path, required=True, help="empty or missing directory to receive the tree")
    arguments = parser.parse_args(argv)
    try:
        result = install(arguments.descriptor, arguments.sha256, arguments.into)
    except (VerifyError, OSError) as error:
        sys.exit(f"node pack: {error}")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
