"""Checks retired Apple hosted voice dependencies and the remaining exact native pins.
It also guards app signing configuration, entitlements, and fail-closed media defaults.
"""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTION_ROOT = REPO_ROOT / "components" / "AstralProjection"
if not (PROJECTION_ROOT / "apple-clients").is_dir():
    pytest.skip(
        "repo-root Apple manifests are not part of the product image",
        allow_module_level=True,
    )
PROJECT = (
    REPO_ROOT
    / "components/AstralProjection/apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj"
)
RESOLUTION = (
    REPO_ROOT
    / "components/AstralProjection/apple-clients/AstralApp/AstralApp.xcodeproj/project.xcworkspace"
    / "xcshareddata/swiftpm/Package.resolved"
)
CORE_PACKAGE = REPO_ROOT / "components/AstralProjection/apple-clients/AstralCore/Package.swift"
BASE_CONFIG = REPO_ROOT / "components/AstralProjection/apple-clients/Config/Base.xcconfig"
MACOS_ENTITLEMENTS = (
    REPO_ROOT / "components/AstralProjection/apple-clients/AstralApp/AstralApp-macOS.entitlements"
)
VOICE_SESSION_CONTROLLER = (
    REPO_ROOT
    / "components/AstralProjection/apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift"
)
DEPENDENCY_APPROVAL_PATH = Path(
    os.environ.get(
        "ASTRAL_VOICE_DEPENDENCY_APPROVAL_PATH",
        REPO_ROOT / "specs/065-conversational-voice/dependency-approval.md",
    )
)
def test_hosted_voice_sdk_is_absent_from_apple_targets() -> None:
    project = PROJECT.read_text(encoding="utf-8")

    assert "LiveKit" not in project
    assert "client-sdk-swift" not in project
    assert "webrtc-xcframework" not in project
    assert "LiveKit" not in CORE_PACKAGE.read_text(encoding="utf-8")


def test_project_does_not_override_injected_apple_development_team() -> None:
    project = PROJECT.read_text(encoding="utf-8")
    base_config = BASE_CONFIG.read_text(encoding="utf-8")

    assert "DEVELOPMENT_TEAM =" not in project
    assert "DEVELOPMENT_TEAM = $(ASTRAL_DEVELOPMENT_TEAM)" in base_config


def test_resolved_apple_graph_excludes_hosted_voice_and_keeps_exact_pins() -> None:
    resolution = json.loads(RESOLUTION.read_text(encoding="utf-8"))
    pins = {
        pin["identity"]: (pin["state"]["version"], pin["state"]["revision"])
        for pin in resolution["pins"]
    }

    assert resolution["version"] == 3
    assert pins == {
        "mlx-swift": (
            "0.33.0",
            "7ede4fd6d05f63305c1c0ebf4f8c1909a637c574",
        ),
        "sparkle": (
            "2.10.1",
            "ab7d71291238df6e0492143b120c9b696a35f603",
        ),
        "swift-numerics": (
            "1.1.1",
            "0c0290ff6b24942dadb83a929ffaaa1481df04a2",
        ),
    }


def test_approved_binary_target_checksums_are_recorded() -> None:
    approval = DEPENDENCY_APPROVAL_PATH.read_text(encoding="utf-8")

    assert "07c5caf718058af3c528dcabd257298c40e5a8527e4fb9f47c48336ba5899853" in approval
    assert "0d3f2ce159a224c728f8b131068d53bbf9b13d968cda0edc68a6a2290f2651ed" in approval


def test_sandboxed_macos_voice_keeps_network_and_audio_entitlements() -> None:
    entitlements = plistlib.loads(MACOS_ENTITLEMENTS.read_bytes())

    assert entitlements["com.apple.security.app-sandbox"] is True
    assert entitlements["com.apple.security.network.client"] is True
    assert entitlements["com.apple.security.network.server"] is True
    assert entitlements["com.apple.security.device.audio-input"] is True
    assert "com.apple.security.device.camera" not in entitlements


def test_retired_apple_media_defaults_refuse_hosted_voice() -> None:
    source = VOICE_SESSION_CONTROLLER.read_text(encoding="utf-8")
    media = source.split("final class UnavailableAppleVoiceMediaClient", 1)[1].split(
        "final class RetiredAppleVoiceControlAPI", 1
    )[0]

    assert "func connect(_ grant: AppleVoiceRetiredGrant) async throws { throw CompanionVoiceError.rejected }" in media
    assert "func authorize(_ announcement: VoiceAnnouncementMedia) -> Bool { false }" in media
    assert "let resolvedMedia = media ?? UnavailableAppleVoiceMediaClient()" in source
    assert "let resolvedAPI = api ?? RetiredAppleVoiceControlAPI()" in source
    assert "import LiveKit" not in source
    assert "URLSession" not in source
    assert "player.stop()" not in source
