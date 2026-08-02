"""Server-owned conversational composer behavior for Feature 065."""

from __future__ import annotations

import pytest

from webrender.chrome.composer_model import (
    VoiceComposerContext,
    VoiceOwner,
    build_composer_state,
)


DEVICE = "00000000-0000-4000-8000-000000000001"
OTHER_DEVICE = "00000000-0000-4000-8000-000000000003"
CONNECTION = "00000000-0000-4000-8000-000000000002"
CHAT = "00000000-0000-4000-8000-000000000004"
OTHER_CHAT = "00000000-0000-4000-8000-000000000005"
SESSION = "00000000-0000-4000-8000-000000000006"


def _controls(frame: dict) -> dict[str, dict]:
    return {item["key"]: item for item in frame["voice"]["controls"]}


def test_off_state_has_one_enabled_start_and_canonical_action_order() -> None:
    frame = build_composer_state(
        VoiceComposerContext(
            revision=7,
            connection_generation=CONNECTION,
            local_device_id=DEVICE,
            available=True,
            state="off",
            reason="ready",
            visible_chat_id=CHAT,
        )
    )

    assert frame["type"] == "composer_state"
    assert frame["schema_version"] == "1"
    assert frame["voice"]["chat_context_synced"] is False
    assert [item["key"] for item in frame["voice"]["controls"]] == [
        "voice-start",
        "voice-takeover",
        "voice-end",
        "voice-microphone",
        "voice-stop-speech",
        "voice-mute",
        "voice-chat-context",
        "voice-sensitive-recap",
    ]
    controls = _controls(frame)
    assert controls["voice-start"] == {
        "key": "voice-start",
        "action": "voice_session_start",
        "label": "Start voice conversation",
        "icon": "microphone",
        "visible": True,
        "enabled": True,
        "pressed": False,
        "busy": False,
    }
    assert not any(
        item["visible"] for key, item in controls.items() if key != "voice-start"
    )


def test_current_owner_gets_microphone_mute_context_and_speech_controls() -> None:
    frame = build_composer_state(
        VoiceComposerContext(
            revision=9,
            connection_generation=CONNECTION,
            local_device_id=DEVICE,
            available=True,
            state="speaking_progress",
            reason="ready",
            speech_muted=False,
            microphone_enabled=True,
            foreground_active=True,
            chat_context_revision=4,
            applied_chat_context_revision=3,
            session_id=SESSION,
            generation=2,
            media_grant_revision=5,
            visible_chat_id=CHAT,
            selected_chat_id=OTHER_CHAT,
            owner_device=VoiceOwner(DEVICE, "web", 2, "Safari on Mac"),
            sensitive_recap_pending=True,
        )
    )

    controls = _controls(frame)
    assert not controls["voice-start"]["visible"]
    assert not controls["voice-takeover"]["visible"]
    for key in (
        "voice-end",
        "voice-microphone",
        "voice-stop-speech",
        "voice-mute",
        "voice-chat-context",
        "voice-sensitive-recap",
    ):
        assert controls[key]["visible"] and controls[key]["enabled"]
    assert controls["voice-microphone"]["pressed"]
    assert frame["voice"]["owner_device"]["device_label"] == "Safari on Mac"


def test_other_device_sees_only_takeover_and_never_local_media_controls() -> None:
    frame = build_composer_state(
        VoiceComposerContext(
            revision=3,
            connection_generation=CONNECTION,
            local_device_id=DEVICE,
            available=True,
            state="suspended",
            reason="takeover_required",
            session_id=SESSION,
            generation=8,
            media_grant_revision=9,
            visible_chat_id=CHAT,
            owner_device=VoiceOwner(OTHER_DEVICE, "android", 8),
        )
    )

    controls = _controls(frame)
    assert controls["voice-takeover"]["visible"]
    assert controls["voice-takeover"]["enabled"]
    assert not controls["voice-microphone"]["visible"]
    assert not controls["voice-end"]["visible"]


def test_unavailable_state_keeps_an_honest_disabled_start_affordance() -> None:
    frame = build_composer_state(
        VoiceComposerContext(
            revision=0,
            connection_generation=CONNECTION,
            local_device_id=DEVICE,
            available=False,
            state="unavailable",
            reason="worker_unavailable",
        )
    )
    start = _controls(frame)["voice-start"]
    assert start["visible"] and not start["enabled"]


@pytest.mark.parametrize(
    "changes",
    (
        {"state": "listening"},
        {"microphone_enabled": True},
        {"connection_generation": "not-a-uuid"},
        {"reason": "provider_body_said_secret"},
    ),
)
def test_invalid_or_client_inferred_state_fails_closed(changes: dict) -> None:
    values = dict(
        revision=0,
        connection_generation=CONNECTION,
        local_device_id=DEVICE,
        available=True,
        state="off",
        reason="ready",
    )
    values.update(changes)
    with pytest.raises(ValueError):
        build_composer_state(VoiceComposerContext(**values))
