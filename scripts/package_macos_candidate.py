"""Packages a signed direct-download Mac app as a private DMG candidate with diagnostic checksums.
The protected release publisher owns notarization, release evidence, and public distribution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
import tempfile
from pathlib import Path


class CandidateError(ValueError):
    pass


def command(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(arguments, capture_output=True, check=False)
    if result.returncode:
        raise CandidateError(f"{arguments[0]} failed: {result.stderr.decode(errors='replace')[-1500:]}")
    return result


def inspect_app(app: Path, team_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[A-Z0-9]{10}", team_id):
        raise CandidateError("Expected Apple team ID must contain ten uppercase letters or digits")
    if not app.is_dir() or app.suffix != ".app" or app.is_symlink():
        raise CandidateError("Input must be a regular app bundle")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "com.personalailabs.astraldeep":
        raise CandidateError("Bundle identity is not AstralDeep")
    if info.get("ASTRALDistribution") != "direct-download":
        raise CandidateError("Only the explicit direct-download distribution can be packaged")
    version = info.get("CFBundleShortVersionString", "")
    build = info.get("CFBundleVersion", "")
    minimum = info.get("LSMinimumSystemVersion", "")
    if not isinstance(version, str) or not re.fullmatch(r"[1-9]\d*\.\d+(?:\.\d+)?", version):
        raise CandidateError("Marketing version is invalid")
    if not isinstance(build, str) or not isinstance(minimum, str) or (
        not re.fullmatch(r"[1-9]\d*", build)
        or not re.fullmatch(r"[1-9]\d*\.\d+(?:\.\d+)?", minimum)
    ):
        raise CandidateError("Build number or minimum macOS version is invalid")
    executable = info.get("CFBundleExecutable", "")
    if executable != "AstralDeep":
        raise CandidateError("Executable identity is not AstralDeep")
    binary = app / "Contents/MacOS" / executable
    command("codesign", "--verify", "--deep", "--strict", str(app))
    signature = command("codesign", "--display", "--verbose=4", str(app)).stderr.decode()
    if not all(value in signature for value in (
        "Authority=Developer ID Application:", f"TeamIdentifier={team_id}", "Timestamp=",
    )) or not re.search(r"flags=0x[0-9a-f]+\([^)]*\bruntime\b", signature) or "Signature=adhoc" in signature:
        raise CandidateError("App requires the expected timestamped Developer ID and hardened runtime")
    entitlement_result = command("codesign", "--display", "--entitlements", "-", "--xml", str(app))
    entitlements = plistlib.loads(entitlement_result.stdout)
    if entitlements != {"com.apple.security.device.audio-input": True}:
        raise CandidateError("App entitlements differ from the approved direct-download profile")
    architectures = set(command("lipo", "-archs", str(binary)).stdout.decode().split())
    architecture = {
        frozenset({"arm64"}): "arm64", frozenset({"x86_64"}): "x86_64",
        frozenset({"arm64", "x86_64"}): "universal",
    }.get(frozenset(architectures))
    if architecture is None:
        raise CandidateError("Unsupported executable architectures")
    return {
        "version": version, "build": int(build), "minimumMacOS": minimum,
        "architecture": architecture, "bundleID": info["CFBundleIdentifier"],
        "executableSHA256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "teamID": team_id,
    }


def package_candidate(app: Path, output: Path, team_id: str, deep: str, projection: str) -> Path:
    if platform.system() != "Darwin":
        raise CandidateError("DMG packaging requires macOS")
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (deep, projection)):
        raise CandidateError("Source revisions must be exact commit hashes")
    app = app.resolve(strict=True)
    identity = inspect_app(app, team_id)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    name = f"AstralDeep-{identity['version']}-{identity['build']}-{identity['architecture']}.candidate.dmg"
    image = output / name
    with tempfile.TemporaryDirectory(prefix="astral-mac-package-") as temporary:
        staging = Path(temporary) / "volume"
        staging.mkdir()
        staged_app = staging / "AstralDeep.app"
        command("ditto", str(app), str(staged_app))
        staged_identity = inspect_app(staged_app, team_id)
        if staged_identity != identity:
            raise CandidateError("Staged app changed while packaging")
        identity = staged_identity
        (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
        command("hdiutil", "create", "-volname", "AstralDeep", "-srcfolder", str(staging),
                "-fs", "HFS+", "-format", "UDZO", str(image))
    os.chmod(image, 0o600)
    command("hdiutil", "verify", str(image))
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest = {
        "schemaVersion": 1, "distribution": "candidate-only", "releaseAuthorized": False,
        "notarization": "not-established", "source": {"deep": deep, "projection": projection},
        "app": identity, "file": {"name": name, "bytes": image.stat().st_size, "sha256": digest},
    }
    (output / "candidate.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text(f"{digest}  {name}\n")
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--deep", required=True)
    parser.add_argument("--projection", required=True)
    arguments = parser.parse_args()
    try:
        image = package_candidate(arguments.app, arguments.output, arguments.team_id,
                                  arguments.deep, arguments.projection)
    except (CandidateError, OSError, plistlib.InvalidFileException) as error:
        parser.exit(1, f"Mac candidate packaging failed: {error}\n")
    print(f"Private candidate created: {image}; public release remains unauthorized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
