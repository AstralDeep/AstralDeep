"""Feature-065 ROTE voice capability and form-factor contracts."""

from __future__ import annotations

from rote.capabilities import DeviceProfile, DeviceType


def _voice_device(**updates: object) -> dict[str, object]:
    device: dict[str, object] = {
        "device_type": "browser",
        "viewport_width": 1280,
        "viewport_height": 800,
        "has_microphone": True,
        "has_audio_output": True,
        "microphone_permission": "authorized",
        "full_duplex": True,
        "voice_transport": "livekit",
        "user_agent": "capability-only-a",
    }
    device.update(updates)
    return device


def test_rote_preserves_separate_runtime_voice_facts() -> None:
    profile = DeviceProfile.from_dict(_voice_device())

    assert profile.capabilities.has_microphone is True
    assert profile.capabilities.has_audio_output is True
    assert profile.capabilities.microphone_permission == "authorized"
    assert profile.capabilities.full_duplex is True
    assert profile.capabilities.voice_transport == "livekit"
    serialized = profile.to_dict()["capabilities"]
    assert serialized["has_microphone"] is True
    assert serialized["has_audio_output"] is True
    assert serialized["microphone_permission"] == "authorized"
    assert serialized["full_duplex"] is True
    assert serialized["voice_transport"] == "livekit"


def test_rote_normalizes_web_transport_alias_and_rejects_untrusted_values() -> None:
    web = _voice_device()
    web.pop("voice_transport")
    web["transport"] = "livekit"
    assert DeviceProfile.from_dict(web).capabilities.voice_transport == "livekit"

    malformed = _voice_device(
        has_microphone="yes",
        has_audio_output=1,
        full_duplex=None,
        microphone_permission="prompt",
        voice_transport="platform_tts",
    )
    capabilities = DeviceProfile.from_dict(malformed).capabilities
    assert capabilities.has_microphone is False
    assert capabilities.has_audio_output is False
    assert capabilities.full_duplex is False
    assert capabilities.microphone_permission == "not_determined"
    assert capabilities.voice_transport == ""


def test_rote_normalizes_nested_windows_voice_capability_without_identity_logic() -> None:
    windows = _voice_device(device_type="windows")
    for name in (
        "has_microphone",
        "has_audio_output",
        "microphone_permission",
        "full_duplex",
        "voice_transport",
    ):
        windows.pop(name)
    windows["voice"] = {
        "has_microphone": True,
        "has_audio_output": True,
        "microphone_permission": "authorized",
        "full_duplex": True,
        "transport": "livekit",
    }

    capabilities = DeviceProfile.from_dict(windows).capabilities
    assert capabilities.has_microphone is True
    assert capabilities.has_audio_output is True
    assert capabilities.microphone_permission == "authorized"
    assert capabilities.full_duplex is True
    assert capabilities.voice_transport == "livekit"


def test_form_factor_is_capability_driven_not_client_identity() -> None:
    first = DeviceProfile.from_dict(_voice_device(user_agent="client-a"))
    second = DeviceProfile.from_dict(_voice_device(user_agent="client-b"))
    assert first.device_type is second.device_type is DeviceType.BROWSER
    assert first.max_grid_columns == second.max_grid_columns
    assert first.supports_interactivity == second.supports_interactivity

    watch_sized = DeviceProfile.from_dict(
        _voice_device(
            viewport_width=190,
            device_type="browser",
            full_duplex=False,
            voice_transport="watch_pcm_websocket",
        )
    )
    assert watch_sized.device_type is DeviceType.WATCH
    assert watch_sized.max_grid_columns == 1
    assert watch_sized.capabilities.voice_transport == "watch_pcm_websocket"
    assert watch_sized.capabilities.full_duplex is False
