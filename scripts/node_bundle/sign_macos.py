"""Signs every Mach-O file and bundle in a macOS node pack inside out with one codesign identity, then re-seals the manifest, archive and descriptor.
release_macos.py's publisher calls verify_signed_pack before it trusts a pack, and the input is the output directory assemble.py wrote.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import manifest as pack_manifest
import verify
from pack_error import PackError

HERE = Path(__file__).resolve().parent
BUNDLE_SUFFIXES = {".app", ".framework", ".xpc", ".bundle"}
BUNDLE_INFO = ("Contents/Info.plist", "Resources/Info.plist", "Info.plist")
DERIVED_KEYS = {"format", "installed_bytes", "files", "signing"}
PACK_PLATFORM = "macos-arm64"
Runner = Callable[..., subprocess.CompletedProcess]


def macho_reader():
    spec = importlib.util.spec_from_file_location("pack_macho", HERE / "natives" / "macho.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pack_macho"] = module
    spec.loader.exec_module(module)
    return module.read_slices


def code_files(tree: Path) -> list[str]:
    read_slices = macho_reader()
    found = []
    for current, _, names in os.walk(tree, followlinks=False):
        for name in names:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            if read_slices(path):
                found.append(path.relative_to(tree).as_posix())
    return sorted(found)


def code_bundles(tree: Path) -> list[str]:
    found = []
    for current, directories, _ in os.walk(tree, followlinks=False):
        for name in directories:
            path = Path(current) / name
            if path.suffix in BUNDLE_SUFFIXES and not path.is_symlink() and any((path / info).is_file() for info in BUNDLE_INFO):
                found.append(path.relative_to(tree).as_posix())
    return sorted(found)


def inside_out(paths: list[str]) -> list[str]:
    return sorted(paths, key=lambda path: (-path.count("/"), path))


def codesign(path: Path, identity: str, run: Runner) -> None:
    done = run(["codesign", "--force", "--sign", identity, str(path)], capture_output=True, text=True)
    if done.returncode:
        raise PackError(f"codesign refused {path.name}: {done.stderr.strip()[-300:]}")


def sign_tree(tree: Path, identity: str, run: Runner = subprocess.run) -> list[str]:
    files, bundles = code_files(tree), code_bundles(tree)
    for relative in inside_out(files) + inside_out(bundles):
        codesign(tree / relative, identity, run)
    return sorted(files + bundles)


def reseal(tree: Path, old: dict, old_digest: str, identity: str, signed: list[str], out: Path, archive_name: str) -> None:
    inputs = {key: value for key, value in old.items() if key not in DERIVED_KEYS}
    inputs["signing"] = {"identity": identity, "sealed_from_manifest_sha256": old_digest, "files": signed}
    sealed = pack_manifest.write_manifest(tree, inputs)
    archive = pack_manifest.write_archive(tree, out / archive_name)
    digest = pack_manifest.sha256_file(tree / pack_manifest.MANIFEST_NAME)
    descriptor = pack_manifest.descriptor(sealed, archive, digest)
    (out / "node-pack.json").write_text(json.dumps(descriptor, indent=1, sort_keys=True) + "\n")
    (out / "node-pack-manifest.json").write_bytes((tree / pack_manifest.MANIFEST_NAME).read_bytes())


def verify_signed_pack(descriptor: Path, run: Runner = subprocess.run) -> dict:
    with tempfile.TemporaryDirectory(prefix="node-pack-verify-") as scratch:
        tree = Path(scratch) / "tree"
        try:
            verify.install(str(descriptor), pack_manifest.sha256_file(descriptor), tree)
        except verify.VerifyError as error:
            raise PackError(f"the pack does not verify: {error}") from error
        sealed = json.loads((tree / pack_manifest.MANIFEST_NAME).read_text())
        record = sealed.get("signing")
        if not isinstance(record, dict) or not record.get("files"):
            raise PackError("the manifest predates signing")
        listed = set(record["files"])
        unsigned = [path for path in code_files(tree) if path not in listed]
        if unsigned:
            raise PackError(f"unsigned code in the pack: {', '.join(unsigned[:5])}")
        for relative in sorted(listed):
            done = run(["codesign", "--verify", "--strict", str(tree / relative)], capture_output=True, text=True)
            if done.returncode:
                raise PackError(f"the signature on {relative} does not verify: {done.stderr.strip()[-300:]}")
        return {"identity": record.get("identity"), "files": len(listed)}


def sign_pack(descriptor: Path, identity: str, out: Path, run: Runner = subprocess.run) -> Path:
    if out.exists() and any(out.iterdir()):
        raise PackError(f"{out} is not empty")
    with tempfile.TemporaryDirectory(prefix="node-pack-sign-") as scratch:
        tree = Path(scratch) / "tree"
        try:
            installed = verify.install(str(descriptor), pack_manifest.sha256_file(descriptor), tree)
        except verify.VerifyError as error:
            raise PackError(f"the pack does not verify: {error}") from error
        if installed["platform"] != PACK_PLATFORM:
            raise PackError(f"only {PACK_PLATFORM} packs are signed, not {installed['platform']}")
        old = json.loads((tree / pack_manifest.MANIFEST_NAME).read_text())
        if "signing" in old:
            raise PackError("the pack is already signed")
        archive_name = json.loads(descriptor.read_text())["archive"]["file"]
        signed = sign_tree(tree, identity, run)
        if not signed:
            raise PackError("the pack holds no code to sign")
        out.mkdir(parents=True, exist_ok=True)
        reseal(tree, old, installed["manifest_sha256"], identity, signed, out, archive_name)
    verify_signed_pack(out / "node-pack.json", run)
    return out / "node-pack.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("descriptor", type=Path, help="node-pack.json of the unsigned macOS pack")
    parser.add_argument("--identity", required=True, help="codesign identity; - signs ad hoc")
    parser.add_argument("--out", type=Path, required=True, help="empty or missing directory for the signed pack")
    arguments = parser.parse_args(argv)
    try:
        written = sign_pack(arguments.descriptor, arguments.identity, arguments.out)
    except (PackError, verify.VerifyError, OSError, ValueError) as error:
        sys.exit(f"node pack signing: {error}")
    print(written)


if __name__ == "__main__":
    main()
