"""Immediate client-local lifecycle cleanup and zero-retention tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.voice_control_binding import VoiceControlBindingError

from orchestrator.voice_control_binding import ClientLocalBindingRegistry
from orchestrator.voice_coordinator import ClientLocalAnnouncementRegistry


NOW = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


def test_session_cleanup_drops_turn_digest_announcement_and_sequences() -> None:
    binding = ClientLocalBindingRegistry()
    announcements = ClientLocalAnnouncementRegistry()
    authority = SimpleNamespace(
        socket_id=1,
        user_id="user-a",
        device_id="00000000-0000-4000-8000-000000000001",
        connection_generation="00000000-0000-4000-8000-000000000002",
        binding_id="00000000-0000-4000-8000-000000000003",
        session_id="00000000-0000-4000-8000-000000000004",
        generation=1,
        speech_revision=1,
        client_turn_id="00000000-0000-4000-8000-000000000005",
        turn_id="00000000-0000-4000-8000-000000000006",
        submission_id="00000000-0000-4000-8000-000000000007",
        request_generation="00000000-0000-4000-8000-000000000008",
        chat_id="00000000-0000-4000-8000-000000000009",
        chat_context_revision=1,
        recognition_sequence=1,
        expires_at=NOW + timedelta(minutes=2),
    )
    binding._turns[("user-a", authority.client_turn_id)] = authority
    binding._inflight_final_digests[("user-a", authority.client_turn_id)] = "a" * 64

    binding.clear_session(session_id=authority.session_id, generation=1)
    announcements.clear_session(session_id=authority.session_id, generation=1)

    assert binding._turns == {}
    assert binding._inflight_final_digests == {}
    assert binding._sequences == {}
    assert announcements.retained_counts() == {
        "sessions": 0,
        "announcements": 0,
    }


def test_local_registries_retain_no_audio_text_engine_endpoint_or_credentials() -> None:
    names = set(vars(ClientLocalBindingRegistry())) | set(
        vars(ClientLocalAnnouncementRegistry())
    )
    assert not names & {
        "audio",
        "transcript",
        "text",
        "buffer",
        "engine",
        "endpoint",
        "credential",
        "token",
    }


def _ready_scope(*, session_id: str, sequence: int = 1) -> tuple[object, object, object]:
    device = "00000000-0000-4000-8000-000000000011"
    connection = "00000000-0000-4000-8000-000000000012"
    binding = "00000000-0000-4000-8000-000000000013"
    claims = SimpleNamespace(
        subject="user-a",
        device_id=device,
        connection_generation=connection,
        binding_id=binding,
        expires_at=NOW + timedelta(minutes=4),
    )
    session = SimpleNamespace(
        user_id="user-a",
        device_id=device,
        owner_connection_generation=connection,
        control_binding_id=binding,
        control_binding_expires_at=NOW + timedelta(minutes=4),
        lease_expires_at=NOW + timedelta(minutes=2),
        session_id=session_id,
        generation=1,
        media_grant_revision=1,
        speech_backend="client_local",
        state="active",
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        visible_chat_id="00000000-0000-4000-8000-000000000014",
        chat_context_revision=1,
        applied_visible_chat_id="00000000-0000-4000-8000-000000000014",
        applied_chat_context_revision=1,
    )
    frame = SimpleNamespace(
        device_id=device,
        connection_generation=connection,
        session_id=session_id,
        generation=1,
        speech_revision=1,
        client_sequence=sequence,
        validate=lambda: None,
    )
    return claims, session, frame


def test_readiness_only_sequences_are_bounded_expiring_and_exactly_cleaned() -> None:
    first_session = "00000000-0000-4000-8000-000000000021"
    second_session = "00000000-0000-4000-8000-000000000022"
    registry = ClientLocalBindingRegistry(capacity=1)
    claims, session, frame = _ready_scope(session_id=first_session)
    registry.authorize_ready(
        socket_id=7,
        current_socket_id=7,
        user_id="user-a",
        claims=claims,
        session=session,
        frame=frame,
        now=NOW,
    )
    _, second, second_frame = _ready_scope(session_id=second_session)
    with pytest.raises(VoiceControlBindingError, match="capacity_exhausted"):
        registry.authorize_ready(
            socket_id=8,
            current_socket_id=8,
            user_id="user-a",
            claims=claims,
            session=second,
            frame=second_frame,
            now=NOW,
        )

    registry.clear_connection(
        user_id="user-a",
        device_id=claims.device_id,
        connection_generation=claims.connection_generation,
        socket_id=7,
    )
    assert registry._sequences == {}
    registry.authorize_ready(
        socket_id=8,
        current_socket_id=8,
        user_id="user-a",
        claims=claims,
        session=second,
        frame=second_frame,
        now=NOW,
    )
    registry.clear_session(session_id=second_session, generation=1)
    assert registry._sequences == {}

    registry.authorize_ready(
        socket_id=9,
        current_socket_id=9,
        user_id="user-a",
        claims=claims,
        session=session,
        frame=frame,
        now=NOW,
    )
    registry._prune(NOW + timedelta(minutes=5))
    assert registry._sequences == {}
