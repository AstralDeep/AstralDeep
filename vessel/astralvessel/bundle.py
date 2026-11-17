# Installs a node pack for Vessel's process mode: it checks the archive and every file against the pack manifest.
# Each version gets its own folder, so a refused install leaves the previous one alone.
# runtime.py runs the installed pack's interpreter and code, and operate.py resolves which pack a deployment uses.

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DESCRIPTOR_FORMAT = "astral.nodepack-descriptor/v1"
MANIFEST_FORMAT = "astral.nodepack-manifest/v1"
MANIFEST_NAME = "manifest.json"
MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
EXTRACT_SLACK = 64 * 1024 * 1024
PLATFORMS = ("macos-arm64", "linux-x86_64", "linux-aarch64")
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "[::1]")
DIGEST = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[A-Za-z0-9._+-]{1,64}")
PROBLEM_LIMIT = 20
DESCRIPTOR_FILE = "node-pack.json"
PINS_FILE = "node-bundle.json"


class BundleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class InstalledBundle:
    root: Path
    version: str
    platform: str
    manifest_sha256: str
    manifest: dict[str, Any] = field(compare=False, repr=False)

    @property
    def python(self) -> Path:
        return self.root / "python" / "bin" / "python3.11"

    @property
    def launcher(self) -> Path:
        return self.root / "launcher" / "astral_node_launch.py"

    @property
    def backend(self) -> Path:
        return self.root / "app" / "backend"

    @property
    def astral_exec(self) -> Path:
        return self.backend / "astral_exec"

    @property
    def ripgrep(self) -> Path | None:
        found = self.root / "bin" / "rg"
        return found if found.is_file() else None

    @property
    def mailbox(self) -> Path | None:
        found = self.root / "bin" / "astral-mailbox"
        return found if found.is_file() else None

    @property
    def distributions(self) -> dict[str, str]:
        listed = self.manifest.get("installed_distributions", [])
        return {d["name"].lower().replace("_", "-"): d["version"] for d in listed}


def platform_name() -> str:
    machine = os.uname().machine.lower()
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return "macos-arm64"
        raise BundleError("bundle.platform_unsupported", f"no node pack exists for macOS on {machine}")
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
    raise BundleError("bundle.platform_unsupported", f"no node pack exists for {sys.platform} on {machine}")


def install_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AstralVessel" / "bundles"
    return Path.home() / ".local" / "share" / "astralvessel" / "bundles"


def config_root() -> Path:
    packaged = Path(__file__).resolve().parent / "config"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent / "config"


def pinned(platform: str, pins: Path | None = None) -> tuple[str, str] | None:
    path = pins or config_root() / PINS_FILE
    try:
        entry = json.loads(path.read_text()).get("platforms", {}).get(platform) or {}
    except (OSError, ValueError):
        return None
    url, digest = entry.get("descriptor"), entry.get("sha256")
    return (url, digest) if url and digest else None


def resolve(bundle: str | None, sha256: str | None, platform: str, pins: Path | None = None) -> tuple[str, str | None]:
    # An explicit --bundle wins; otherwise the release's pin; otherwise there is nothing to install.
    if bundle:
        return bundle, sha256
    found = pinned(platform, pins)
    if found is None:
        raise BundleError("bundle.not_pinned", f"this release pins no node pack for {platform}; pass --bundle with the "
                          "pack's node-pack.json (a folder or an address) and --bundle-sha256 with its digest")
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _local(location: str) -> Path | None:
    url = urllib.parse.urlparse(location)
    if url.scheme in ("http", "https"):
        return None
    path = Path(urllib.request.url2pathname(url.path) if url.scheme == "file" else location).expanduser()
    return path / DESCRIPTOR_FILE if path.is_dir() else path


def _permitted(location: str) -> None:
    url = urllib.parse.urlparse(location)
    if url.scheme == "https" and url.hostname:
        return
    if url.scheme == "http" and (url.hostname or "").lower() in LOOPBACK_HOSTS:
        return
    if url.scheme in ("", "file"):
        return
    raise BundleError("bundle.address", f"{location} is not an https address, a loopback address or a local path")


def _read(location: str, limit: int) -> bytes:
    _permitted(location)
    local = _local(location)
    try:
        if local is not None:
            with local.open("rb") as handle:
                data = handle.read(limit + 1)
        else:
            with urllib.request.urlopen(location, timeout=60) as response:
                data = response.read(limit + 1)
    except OSError as exc:
        raise BundleError("bundle.unreachable", f"cannot read {location}: {exc}") from None
    if len(data) > limit:
        raise BundleError("bundle.descriptor", f"{location} is larger than {limit} bytes")
    return data


def _sibling(location: str, name: str) -> str:
    local = _local(location)
    if local is not None:
        return str(local.resolve().parent / name)
    return urllib.parse.urljoin(location, name)


def _download(location: str, target: Path, size: int) -> None:
    _permitted(location)
    local = _local(location)
    try:
        source = local.open("rb") if local is not None else urllib.request.urlopen(location, timeout=120)
        with source, target.open("wb") as handle:
            copied = 0
            for block in iter(lambda: source.read(1 << 20), b""):
                copied += len(block)
                if copied > size:
                    raise BundleError("bundle.archive", "the archive is larger than its descriptor says")
                handle.write(block)
    except OSError as exc:
        raise BundleError("bundle.unreachable", f"cannot read {location}: {exc}") from None


def parse_descriptor(raw: bytes, pinned_sha: str | None) -> dict[str, Any]:
    digest = hashlib.sha256(raw).hexdigest()
    if pinned_sha and pinned_sha.lower() != digest:
        raise BundleError("bundle.digest", "the descriptor does not match its pinned SHA-256")
    try:
        descriptor = json.loads(raw)
        archive = descriptor["archive"]
        version, platform, name = descriptor["version"], descriptor["platform"], archive["file"]
        size, archive_sha, installed = archive["size"], archive["sha256"], descriptor["installed_bytes"]
        manifest_sha = descriptor["manifest_sha256"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BundleError("bundle.descriptor", f"the descriptor is unreadable: {exc}") from None
    if descriptor.get("format") != DESCRIPTOR_FORMAT:
        raise BundleError("bundle.descriptor", "the descriptor format is not recognized")
    valid = (
        platform in PLATFORMS and isinstance(name, str) and name not in ("", ".", "..") and "/" not in name
        and isinstance(size, int) and size > 0 and isinstance(installed, int) and installed > 0
        and isinstance(archive_sha, str) and DIGEST.fullmatch(archive_sha)
        and isinstance(manifest_sha, str) and DIGEST.fullmatch(manifest_sha)
        and isinstance(version, str) and VERSION.fullmatch(version))
    if not valid:
        raise BundleError("bundle.descriptor", "the descriptor fields are out of range")
    descriptor["descriptor_sha256"] = digest
    return descriptor


def link_escapes(relative: str, target: str) -> bool:
    if os.path.isabs(target):
        return True
    resolved = os.path.normpath(os.path.join(os.path.dirname(relative), target))
    return resolved == ".." or resolved.startswith("../")


def tree_problems(root: Path, manifest: dict[str, Any]) -> list[str]:
    found: dict[str, int] = {}
    for current, directories, names in os.walk(root, followlinks=False):
        base = Path(current)
        for name in names + [d for d in directories if (base / d).is_symlink()]:
            path = base / name
            found[path.relative_to(root).as_posix()] = os.lstat(path).st_mode
    found.pop(MANIFEST_NAME, None)
    expected: dict[str, dict[str, Any]] = {}
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
            elif link_escapes(path, target):
                problems.append(f"link leaves the pack {path}")
        elif not stat.S_ISREG(mode):
            problems.append(f"not a regular file {path}")
        else:
            target = root / path
            if target.stat().st_size != entry.get("size") or _sha256_file(target) != entry.get("sha256"):
                problems.append(f"content changed {path}")
            elif bool(entry.get("executable")) != bool(mode & stat.S_IXUSR):
                problems.append(f"mode changed {path}")
    return problems


def _load_manifest(path: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BundleError("bundle.manifest", "the archive has no readable manifest")
    if _sha256_file(path) != descriptor["manifest_sha256"]:
        raise BundleError("bundle.manifest", "the manifest does not match the descriptor")
    try:
        manifest = json.loads(path.read_text())
        for entry in manifest["files"]:
            entry["path"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BundleError("bundle.manifest", f"the manifest is unreadable: {exc}") from None
    if manifest.get("format") != MANIFEST_FORMAT or manifest.get("version") != descriptor["version"] \
            or manifest.get("platform") != descriptor["platform"] \
            or manifest.get("installed_bytes") != descriptor["installed_bytes"]:
        raise BundleError("bundle.manifest", "the manifest does not match the descriptor")
    return manifest


def host_glibc() -> tuple[int, int] | None:
    try:
        name = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        return None
    found = re.fullmatch(r"glibc (\d+)\.(\d+).*", name)
    return (int(found[1]), int(found[2])) if found else None


def _check_glibc(manifest: dict[str, Any], platform: str, glibc: tuple[int, int] | None) -> None:
    required = manifest.get("glibc_required")
    if not platform.startswith("linux") or not required:
        return
    found = re.fullmatch(r"(\d+)\.(\d+)", str(required))
    if found is None:
        raise BundleError("bundle.manifest", f"glibc_required {required!r} is not a version")
    if glibc is None:
        raise BundleError("bundle.glibc", f"this pack needs glibc {required} and this system's C library is "
                          "not glibc")
    if glibc < (int(found[1]), int(found[2])):
        raise BundleError("bundle.glibc", f"this pack needs glibc {required} or newer, and this system has "
                          f"{glibc[0]}.{glibc[1]}")


def _extract(archive: Path, tree: Path, limit: int) -> None:
    total = 0
    try:
        with tarfile.open(archive) as tar:
            for member in tar:
                total += max(member.size, 0)
                if total > limit + EXTRACT_SLACK:
                    raise BundleError("bundle.archive", "the archive unpacks to more than its descriptor says")
                tar.extract(member, tree, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise BundleError("bundle.archive", f"the archive cannot be unpacked: {exc}") from None


def _open(root: Path, platform: str, descriptor_sha: str) -> InstalledBundle:
    manifest = json.loads((root / MANIFEST_NAME).read_text())
    return InstalledBundle(root, manifest["version"], platform, _sha256_file(root / MANIFEST_NAME), manifest)


def install(descriptor: str, sha256: str | None, root: Path | None = None, *, platform: str | None = None,
            glibc: tuple[int, int] | None | str = "host") -> InstalledBundle:
    # Remote descriptors must carry a pinned digest; a pack on this machine's own disk may omit it, and every other
    # check still runs. Versions install side by side, and a pack that fails any check leaves what was there untouched.
    platform = platform or platform_name()
    local = _local(descriptor)
    if sha256 is None and local is None:
        raise BundleError("bundle.digest", "a node pack fetched from an address needs --bundle-sha256")
    if sha256 is not None and not DIGEST.fullmatch(sha256.lower()):
        raise BundleError("bundle.digest", "the pinned SHA-256 must be 64 hexadecimal characters")
    parsed = parse_descriptor(_read(descriptor, MAX_DESCRIPTOR_BYTES), sha256)
    if parsed["platform"] != platform:
        raise BundleError("bundle.platform", f"this is a {parsed['platform']} pack and this machine needs {platform}")
    base = root or install_root()
    target = base / parsed["version"]
    marker = base / f".{parsed['version']}.installed"
    if target.is_dir() and marker.is_file() and marker.read_text().strip() == parsed["manifest_sha256"]:
        opened = _open(target, platform, parsed["descriptor_sha256"])
        _check_glibc(opened.manifest, platform, host_glibc() if glibc == "host" else glibc)
        return opened
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = base / f".staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    try:
        archive = staging / "pack.tar.gz"
        spec = parsed["archive"]
        _download(_sibling(descriptor, spec["file"]), archive, spec["size"])
        if archive.stat().st_size != spec["size"]:
            raise BundleError("bundle.archive", "the archive size does not match the descriptor")
        if _sha256_file(archive) != spec["sha256"]:
            raise BundleError("bundle.archive", "the archive does not match its SHA-256")
        tree = staging / "tree"
        tree.mkdir()
        _extract(archive, tree, parsed["installed_bytes"])
        archive.unlink()
        manifest = _load_manifest(tree / MANIFEST_NAME, parsed)
        _check_glibc(manifest, platform, host_glibc() if glibc == "host" else glibc)
        problems = tree_problems(tree, manifest)
        if problems:
            raise BundleError("bundle.tree", "the tree differs from its manifest: "
                              + "; ".join(problems[:PROBLEM_LIMIT]))
        marker.unlink(missing_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        tree.rename(target)
        marker.write_text(parsed["manifest_sha256"] + "\n")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return _open(target, platform, parsed["descriptor_sha256"])
