"""Supply-chain and target-boundary guards for feature 065's Apple SDK pin."""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if not (REPO_ROOT / "apple-clients").is_dir():
    pytest.skip(
        "repo-root Apple manifests are not part of the product image",
        allow_module_level=True,
    )
PROJECT = (
    REPO_ROOT
    / "apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj"
)
RESOLUTION = (
    REPO_ROOT
    / "apple-clients/AstralApp/AstralApp.xcodeproj/project.xcworkspace"
    / "xcshareddata/swiftpm/Package.resolved"
)
CORE_PACKAGE = REPO_ROOT / "apple-clients/AstralCore/Package.swift"
BASE_CONFIG = REPO_ROOT / "apple-clients/Config/Base.xcconfig"
MACOS_ENTITLEMENTS = (
    REPO_ROOT / "apple-clients/AstralApp/AstralApp-macOS.entitlements"
)
VOICE_SESSION_CONTROLLER = (
    REPO_ROOT
    / "apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift"
)
DEPENDENCY_APPROVAL_PATH = Path(
    os.environ.get(
        "ASTRAL_VOICE_DEPENDENCY_APPROVAL_PATH",
        REPO_ROOT / "specs/065-conversational-voice/dependency-approval.md",
    )
)
APP_TARGET_ID = "222F50262FFD60D80016B0D6"
WATCH_TARGET_ID = "AA00000000000000000000B3"
LIVEKIT_PRODUCT_ID = "AC6500000000000000000002"


def _native_target(project: str, target_id: str) -> str:
    section = project.split("/* Begin PBXNativeTarget section */", 1)[1].split(
        "/* End PBXNativeTarget section */", 1
    )[0]
    return section.split(f"{target_id} /*", 1)[1].split("\n\t\t};", 1)[0]


def test_livekit_is_exact_and_attached_only_to_astral_app() -> None:
    project = PROJECT.read_text(encoding="utf-8")

    assert 'repositoryURL = "https://github.com/livekit/client-sdk-swift.git";' in project
    assert "kind = exactVersion;" in project
    assert "version = 2.15.3;" in project
    assert LIVEKIT_PRODUCT_ID in _native_target(project, APP_TARGET_ID)
    assert LIVEKIT_PRODUCT_ID not in _native_target(project, WATCH_TARGET_ID)
    assert "LiveKit" not in CORE_PACKAGE.read_text(encoding="utf-8")


def test_project_does_not_override_injected_apple_development_team() -> None:
    project = PROJECT.read_text(encoding="utf-8")
    base_config = BASE_CONFIG.read_text(encoding="utf-8")

    assert "DEVELOPMENT_TEAM =" not in project
    assert "DEVELOPMENT_TEAM = $(ASTRAL_DEVELOPMENT_TEAM)" in base_config


def test_resolved_livekit_graph_is_exact_and_complete() -> None:
    resolution = json.loads(RESOLUTION.read_text(encoding="utf-8"))
    pins = {
        pin["identity"]: (pin["state"]["version"], pin["state"]["revision"])
        for pin in resolution["pins"]
    }

    assert resolution["version"] == 3
    assert pins == {
        "client-sdk-swift": (
            "2.15.3",
            "43ade336353ad12f9ec7fa1e6a51d9b3bd8121f8",
        ),
        "livekit-uniffi-xcframework": (
            "0.0.6",
            "7c161254ce7cd55debc48023f69a917076b12a26",
        ),
        "swift-protobuf": (
            "1.38.1",
            "55d7a1cc5666b85c13464aea1c4b4a90feccb4c8",
        ),
        "webrtc-xcframework": (
            "144.7559.11",
            "46f2af86f06b9a8a9158d37cadda5cb5a214e4c4",
        ),
    }


def test_approved_binary_target_checksums_are_recorded() -> None:
    approval = DEPENDENCY_APPROVAL_PATH.read_text(encoding="utf-8")

    assert "07c5caf718058af3c528dcabd257298c40e5a8527e4fb9f47c48336ba5899853" in approval
    assert "0d3f2ce159a224c728f8b131068d53bbf9b13d968cda0edc68a6a2290f2651ed" in approval


def test_sandboxed_macos_livekit_can_bind_ephemeral_rtc_sockets() -> None:
    entitlements = plistlib.loads(MACOS_ENTITLEMENTS.read_bytes())

    assert entitlements["com.apple.security.app-sandbox"] is True
    assert entitlements["com.apple.security.network.client"] is True
    assert entitlements["com.apple.security.network.server"] is True
    assert entitlements["com.apple.security.device.audio-input"] is True
    assert "com.apple.security.device.camera" not in entitlements


def test_audio_renderer_leaves_player_completion_queue_before_teardown() -> None:
    source = VOICE_SESSION_CONTROLLER.read_text(encoding="utf-8")
    finish = source.split(
        "private func finish(_ requestedPhase: String)", 1
    )[1].split("private func convertLocked", 1)[0]

    terminal_callback = finish.index("completion(phase)")
    deferred_cleanup = finish.index("DispatchQueue.main.async")
    player_stop = finish.index("player.stop()")
    engine_stop = finish.index("engine.stop()")

    assert terminal_callback < deferred_cleanup < player_stop < engine_stop
