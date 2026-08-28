"""Immediate client-local lifecycle cleanup and zero-retention tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
