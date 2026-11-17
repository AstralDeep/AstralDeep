"""Builds tiny macOS and Linux node pack fixtures from binaries compiled with cc in a temporary folder, for the signing and publisher tests.
It seals them with the real manifest writer so verify.py and the publisher read them like assembled packs.
"""

import json
import os
import platform as host
import shutil
import subprocess
from pathlib import Path

import pytest
from node_bundle_support import manifest as pm

needs_macos = pytest.mark.skipif(
    host.system() != "Darwin" or not shutil.which("cc") or not shutil.which("codesign"),
    reason="signing fixtures need macOS cc and codesign",
)

VERSION = "3.11.18-aaaaaaaa-ccccccc"
APP_INFO = (
    '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict>'
    "<key>CFBundleExecutable</key><string>fixapp</string>"
    "<key>CFBundleIdentifier</key><string>test.astral.fixapp</string>"
    "<key>CFBundlePackageType</key><string>APPL</string></dict></plist>"
)


def compile_c(destination: Path, *flags: str) -> None:
    source = destination.with_suffix(".c")
    source.write_text("int answer(void) { return 42; }\nint main(void) { return answer(); }\n"
                      if "-dynamiclib" not in flags and "-bundle" not in flags else "int answer(void) { return 42; }\n")
    subprocess.run(["cc", "-arch", "arm64", *flags, "-o", str(destination), str(source)], check=True,
                   capture_output=True)
    source.unlink()
    subprocess.run(["codesign", "--remove-signature", str(destination)], check=True, capture_output=True)


def unsigned_stage(stage: Path) -> None:
    for relative in ("python/bin", "python/lib/lib-dynload", "Applications/Fix.app/Contents/MacOS", "app"):
        (stage / relative).mkdir(parents=True)
    compile_c(stage / "python/bin/python3.11")
    compile_c(stage / "python/lib/libfix.dylib", "-dynamiclib")
    compile_c(stage / "python/lib/lib-dynload/ext.so", "-bundle")
    compile_c(stage / "Applications/Fix.app/Contents/MacOS/fixapp")
    (stage / "Applications/Fix.app/Contents/Info.plist").write_text(APP_INFO)
    (stage / "app/a.py").write_text("print(1)\n")
    os.symlink("python3.11", stage / "python/bin/python")


def seal(stage: Path, out: Path, platform: str, deep: str, extra: dict | None = None) -> Path:
    inputs = {
        "version": VERSION, "platform": platform, "deep": {"repository": "r", "commit": deep},
        "kernel": {"repository": "k", "commit": "c" * 40}, "components": {}, **(extra or {}),
    }
    manifest = pm.write_manifest(stage, inputs)
    out.mkdir(parents=True)
    archive = pm.write_archive(stage, out / f"{pm.ARCHIVE_PREFIX}-{VERSION}-{platform}.tar.gz")
    digest = pm.sha256_file(stage / pm.MANIFEST_NAME)
    (out / "node-pack.json").write_text(json.dumps(pm.descriptor(manifest, archive, digest), indent=1, sort_keys=True))
    shutil.copy2(stage / pm.MANIFEST_NAME, out / "node-pack-manifest.json")
    return out / "node-pack.json"


def unsigned_mac_pack(root: Path, deep: str = "d" * 40, extra: dict | None = None) -> Path:
    stage = root / "mac-stage"
    unsigned_stage(stage)
    return seal(stage, root / "mac-unsigned", "macos-arm64", deep, extra)


def linux_pack(root: Path, platform: str, deep: str = "d" * 40, extra: dict | None = None) -> Path:
    stage = root / f"{platform}-stage"
    (stage / "python/bin").mkdir(parents=True)
    (stage / "python/bin/python3.11").write_bytes(b"\x7fELF fixture")
    (stage / "python/bin/python3.11").chmod(0o755)
    return seal(stage, root / platform, platform, deep, extra)
