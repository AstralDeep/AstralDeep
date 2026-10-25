"""Verifies private Mac package identity checks, immutable output, and failure reporting.
Actual DMG creation and mounted payload verification also run on the Mac host.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "package_macos_candidate.py"
SPEC = importlib.util.spec_from_file_location("package_macos_candidate", SCRIPT)
assert SPEC and SPEC.loader
candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate)
TEAM = "593LGH35GS"
DEEP = "a" * 40
PROJECTION = "b" * 40


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    app = tmp_path / "AstralDeep.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/AstralDeep").write_bytes(b"synthetic executable")
    info = {
        "CFBundleIdentifier": "com.personalailabs.astraldeep", "CFBundleExecutable": "AstralDeep",
        "CFBundleShortVersionString": "1.8", "CFBundleVersion": "69",
        "LSMinimumSystemVersion": "14.0", "ASTRALDistribution": "direct-download",
    }
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    monkeypatch.setattr(candidate.platform, "system", lambda: "Darwin")
    state = {
        "signature": f"Authority=Developer ID Application: Example\nTeamIdentifier={TEAM}\nflags=0x10000(runtime)\nTimestamp=example",
        "entitlements": {"com.apple.security.device.audio-input": True}, "architectures": "arm64",
        "fail": None, "calls": [],
    }

    def run(arguments, **kwargs):
        state["calls"].append(arguments)
        if arguments[0] == state["fail"]:
            return subprocess.CompletedProcess(arguments, 1, b"", b"intentional failure")
        stdout, stderr = b"", b""
        if arguments[0] == "codesign" and "--entitlements" in arguments:
            assert "--xml" in arguments
            stdout = plistlib.dumps(state["entitlements"])
        elif arguments[0] == "codesign" and "--display" in arguments:
            stderr = state["signature"].encode()
        elif arguments[0] == "lipo":
            stdout = state["architectures"].encode()
        elif arguments[0] == "ditto":
            shutil.copytree(arguments[1], arguments[2])
            if state.get("change_staged_app"):
                (Path(arguments[2]) / "Contents/MacOS/AstralDeep").write_bytes(b"changed executable")
        elif arguments[:2] == ("hdiutil", "create"):
            staging = Path(arguments[arguments.index("-srcfolder") + 1])
            assert (staging / "Applications").readlink() == Path("/Applications")
            assert sorted(p.name for p in staging.iterdir()) == ["Applications", "AstralDeep.app"]
            Path(arguments[-1]).write_bytes(b"synthetic image")
        return subprocess.CompletedProcess(arguments, 0, stdout, stderr)

    monkeypatch.setattr(candidate.subprocess, "run", run)
    return app, info, state


def test_private_candidate_binds_actual_bytes_and_cannot_claim_public_release(bundle, tmp_path):
    app, _, state = bundle
    output = tmp_path / "package"
    image = candidate.package_candidate(app, output, TEAM, DEEP, PROJECTION)
    manifest = json.loads((output / "candidate.json").read_text())
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    assert image.name == "AstralDeep-1.8-69-arm64.candidate.dmg"
    assert image.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    assert manifest["releaseAuthorized"] is False
    assert manifest["distribution"] == "candidate-only"
    assert manifest["notarization"] == "not-established"
    assert manifest["source"] == {"deep": DEEP, "projection": PROJECTION}
    assert manifest["file"]["sha256"] == digest
    assert (output / "SHA256SUMS").read_text() == f"{digest}  {image.name}\n"
    assert ("hdiutil", "verify", str(image)) in state["calls"]
    before = image.read_bytes()
    with pytest.raises(FileExistsError):
        candidate.package_candidate(app, output, TEAM, DEEP, PROJECTION)
    assert image.read_bytes() == before


@pytest.mark.parametrize("patch", [
    {"CFBundleIdentifier": "other"}, {"ASTRALDistribution": "store"},
    {"CFBundleShortVersionString": "1.8<script>"}, {"CFBundleShortVersionString": False},
    {"CFBundleVersion": "0"}, {"CFBundleVersion": 69},
    {"LSMinimumSystemVersion": "unknown"}, {"LSMinimumSystemVersion": False},
    {"CFBundleExecutable": "../other"},
])
def test_invalid_bundle_metadata_is_rejected(bundle, patch):
    app, info, _ = bundle
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({**info, **patch}))
    with pytest.raises(candidate.CandidateError):
        candidate.inspect_app(app, TEAM)


def test_unsigned_foreign_team_debug_and_unsupported_bundles_are_rejected(bundle):
    app, _, state = bundle
    for team in ["not-a-team", "1234567890"]:
        with pytest.raises(candidate.CandidateError):
            candidate.inspect_app(app, team)
    original = state["signature"]
    for signature in [original.replace("runtime", ""), original.replace("Timestamp=", ""), "Signature=adhoc"]:
        state["signature"] = signature
        with pytest.raises(candidate.CandidateError):
            candidate.inspect_app(app, TEAM)
    state["signature"] = original
    state["entitlements"]["com.apple.security.get-task-allow"] = True
    with pytest.raises(candidate.CandidateError):
        candidate.inspect_app(app, TEAM)
    state["entitlements"] = {"com.apple.security.device.audio-input": True}
    for architectures, expected in [("x86_64", "x86_64"), ("x86_64 arm64", "universal")]:
        state["architectures"] = architectures
        assert candidate.inspect_app(app, TEAM)["architecture"] == expected
    state["architectures"] = "i386"
    with pytest.raises(candidate.CandidateError):
        candidate.inspect_app(app, TEAM)
    with pytest.raises(candidate.CandidateError):
        candidate.inspect_app(app.parent, TEAM)


def test_platform_source_and_tool_failures_never_produce_success_metadata(bundle, tmp_path, monkeypatch):
    app, _, state = bundle
    monkeypatch.setattr(candidate.platform, "system", lambda: "Linux")
    with pytest.raises(candidate.CandidateError, match="requires macOS"):
        candidate.package_candidate(app, tmp_path / "wrong-platform", TEAM, DEEP, PROJECTION)
    monkeypatch.setattr(candidate.platform, "system", lambda: "Darwin")
    with pytest.raises(candidate.CandidateError, match="exact commit"):
        candidate.package_candidate(app, tmp_path / "wrong-source", TEAM, "main", PROJECTION)
    state["fail"] = "codesign"
    with pytest.raises(candidate.CandidateError, match="codesign failed"):
        candidate.package_candidate(app, tmp_path / "bad-signature", TEAM, DEEP, PROJECTION)
    state["fail"] = "hdiutil"
    output = tmp_path / "bad-image"
    with pytest.raises(candidate.CandidateError, match="hdiutil failed"):
        candidate.package_candidate(app, output, TEAM, DEEP, PROJECTION)
    assert not (output / "candidate.json").exists()
    assert not (output / "SHA256SUMS").exists()


def test_changed_copy_is_rejected_before_image_creation(bundle, tmp_path):
    app, _, state = bundle
    state["change_staged_app"] = True
    output = tmp_path / "changed-copy"
    with pytest.raises(candidate.CandidateError, match="Staged app changed"):
        candidate.package_candidate(app, output, TEAM, DEEP, PROJECTION)
    assert not any(call[:2] == ("hdiutil", "create") for call in state["calls"])
    assert not (output / "candidate.json").exists()


def test_cli_reports_private_success_and_errors(bundle, tmp_path, monkeypatch, capsys):
    app, _, _ = bundle
    output = tmp_path / "cli"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--app", str(app), "--output", str(output),
                                    "--team-id", TEAM, "--deep", DEEP, "--projection", PROJECTION])
    assert candidate.main() == 0
    assert "public release remains unauthorized" in capsys.readouterr().out
    with pytest.raises(SystemExit) as failure:
        candidate.main()
    assert failure.value.code == 1
    assert "packaging failed" in capsys.readouterr().err
