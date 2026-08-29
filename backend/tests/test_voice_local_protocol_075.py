"""Strict schema-v2 client-local WebSocket parser tests."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from shared.protocol import (
    Message,
    ProtocolValidationError,
    VoiceLocalAnnouncement,
    VoiceLocalFinal,
    VoiceLocalFinalRejected,
    VoiceLocalPlayoutEvent,
    VoiceLocalReady,
    VoiceLocalRecognitionFailed,
    VoiceLocalRecognitionStarted,
    VoiceLocalSessionReady,
    VoiceLocalTurnBound,
    VoicePlayoutEvent,
)


DEVICE = "00000000-0000-4000-8000-000000000301"
CONNECTION = "00000000-0000-4000-8000-000000000302"
SESSION = "00000000-0000-4000-8000-000000000303"
CHAT = "00000000-0000-4000-8000-000000000304"
CLIENT_TURN = "00000000-0000-4000-8000-000000000305"
TURN = "00000000-0000-4000-8000-000000000306"
SUBMISSION = "00000000-0000-4000-8000-000000000307"
REQUEST = "00000000-0000-4000-8000-000000000308"
ANNOUNCEMENT = "00000000-0000-4000-8000-000000000309"
NOW = "2026-08-28T18:00:00Z"


def _common() -> dict[str, object]:
    return {
        "schema_version": "2",
        "speech_backend": "client_local",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "session_id": SESSION,
        "generation": 1,
        "speech_revision": 1,
    }


def _turn() -> dict[str, object]:
    return {
        "client_turn_id": CLIENT_TURN,
        "turn_id": TURN,
        "submission_id": SUBMISSION,
        "request_generation": REQUEST,
        "chat_id": CHAT,
        "chat_context_revision": 1,
        "recognition_sequence": 1,
    }


def _frames() -> list[tuple[type, dict[str, object]]]:
    text = "hello"
    digest = hashlib.sha256(text.encode()).hexdigest()
    return [
        (
            VoiceLocalReady,
            {
                **_common(),
                "type": "voice_local_ready",
                "contract": "client_local/v1",
                "transport": "client_local",
                "configured_locale": "en-US",
                "full_duplex": False,
                "has_microphone": True,
                "has_audio_output": True,
                "microphone_permission": "authorized",
                "recognition_permission": "authorized",
                "recognition_processing": "guaranteed_local",
                "recognition_locale": "ready",
                "recognition_installation": "ready",
                "synthesis_processing": "guaranteed_local",
                "synthesis_locale": "ready",
                "client_sequence": 1,
            },
        ),
        (
            VoiceLocalSessionReady,
            {
                **_common(),
                "type": "voice_local_session_ready",
                "contract": "client_local/v1",
                "transport": "client_local",
                "configured_locale": "en-US",
                "chat_id": CHAT,
                "chat_context_revision": 1,
                "applied_chat_context_revision": 1,
                "foreground_active": True,
                "microphone_enabled": True,
                "speech_muted": False,
                "lease_expires_at": "2026-08-28T18:05:00Z",
            },
        ),
        (
            VoiceLocalRecognitionStarted,
            {
                **_common(),
                "type": "voice_local_recognition_started",
                "client_turn_id": CLIENT_TURN,
                "chat_id": CHAT,
                "chat_context_revision": 1,
                "recognition_sequence": 1,
            },
        ),
        (
            VoiceLocalTurnBound,
            {
                **_common(),
                **_turn(),
                "type": "voice_local_turn_bound",
                "binding_expires_at": "2026-08-28T18:02:00Z",
            },
        ),
        (
            VoiceLocalFinal,
            {
                **_common(),
                **_turn(),
                "type": "voice_local_final",
                "final": True,
                "recognized_locale": "en-US",
                "text": text,
                "text_digest_sha256": digest,
            },
        ),
        (
            VoiceLocalRecognitionFailed,
            {
                **_common(),
                **_turn(),
                "type": "voice_local_recognition_failed",
                "reason": "local_recognition_failed",
            },
        ),
        (
            VoiceLocalFinalRejected,
            {
                **_common(),
                **_turn(),
                "type": "voice_local_final_rejected",
                "reason": "invalid_binding",
                "retry_policy": "none",
                "occurred_at": NOW,
            },
        ),
        (
            VoiceLocalAnnouncement,
            {
                **_common(),
                "type": "voice_local_announcement",
                "announcement_id": ANNOUNCEMENT,
                "announcement_sequence": 1,
                "turn_id": TURN,
                "kind": "acknowledgement",
                "output_policy": "lifecycle",
                "locale": "en-US",
                "text": text,
                "text_digest_sha256": digest,
                "expires_at": "2026-08-28T18:00:10Z",
                "foreground_required": True,
                "mute_revision": 1,
                "consent_revision": 1,
            },
        ),
        (
            VoiceLocalPlayoutEvent,
            {
                **_common(),
                "type": "voice_local_playout_event",
                "announcement_id": ANNOUNCEMENT,
                "announcement_sequence": 1,
                "turn_id": TURN,
                "kind": "acknowledgement",
                "phase": "finished",
                "client_sequence": 2,
                "observed_at": NOW,
            },
        ),
    ]


@pytest.mark.parametrize(("expected_type", "frame"), _frames())
def test_every_local_frame_parses_with_exact_runtime_type(
    expected_type: type,
    frame: dict[str, object],
) -> None:
    parsed = Message.from_json(json.dumps(frame, separators=(",", ":")))

    assert type(parsed) is expected_type
    assert json.loads(parsed.to_json()) == frame


@pytest.mark.parametrize(("_expected_type", "frame"), _frames())
def test_every_local_frame_rejects_unknown_and_missing_keys(
    _expected_type: type,
    frame: dict[str, object],
) -> None:
    unknown = copy.deepcopy(frame)
    unknown["endpoint"] = "https://forbidden.example"
    missing = copy.deepcopy(frame)
    missing.pop(next(key for key in frame if key != "type"))

    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(unknown))
    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(missing))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "1"),
        ("speech_backend", "llm_factory"),
        ("device_id", "not-a-uuid"),
        ("generation", 0),
        ("speech_revision", True),
        ("recognized_locale", "en-GB"),
        ("text_digest_sha256", "A" * 64),
        ("recognition_sequence", 0),
    ],
)
def test_local_final_rejects_invalid_binding_locale_digest_and_sequence(
    field: str,
    value: object,
) -> None:
    frame = dict(next(frame for cls, frame in _frames() if cls is VoiceLocalFinal))
    frame[field] = value

    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(frame))


def test_local_final_enforces_scalar_and_utf8_envelope_bounds() -> None:
    frame = dict(next(frame for cls, frame in _frames() if cls is VoiceLocalFinal))
    frame["text"] = "a" * 8001
    frame["text_digest_sha256"] = hashlib.sha256(
        str(frame["text"]).encode()
    ).hexdigest()

    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(frame))


def test_local_announcement_enforces_600_utf8_bytes_not_characters() -> None:
    frame = dict(
        next(frame for cls, frame in _frames() if cls is VoiceLocalAnnouncement)
    )
    frame["text"] = "é" * 301
    frame["text_digest_sha256"] = hashlib.sha256(
        str(frame["text"]).encode()
    ).hexdigest()

    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(frame, ensure_ascii=False))


def test_remote_v1_playout_parser_is_unchanged_and_rejects_local_fields() -> None:
    remote = {
        "type": "voice_playout_event",
        "schema_version": "1",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "session_id": SESSION,
        "generation": 1,
        "media_grant_revision": 1,
        "announcement_id": ANNOUNCEMENT,
        "announcement_sequence": 1,
        "turn_id": TURN,
        "kind": "acknowledgement",
        "quantum_role": "single",
        "quantum_index": 0,
        "phase": "finished",
        "client_sequence": 1,
        "observed_at": NOW,
    }

    assert isinstance(Message.from_json(json.dumps(remote)), VoicePlayoutEvent)
    remote["speech_backend"] = "client_local"
    with pytest.raises(ProtocolValidationError):
        Message.from_json(json.dumps(remote))
