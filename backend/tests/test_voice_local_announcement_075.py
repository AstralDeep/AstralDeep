"""Server-authorized client-local announcement and playout contracts."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.voice_coordinator import (
    APPROVED_PHRASE_TEXT,
    ClientLocalAnnouncementRegistry,
    ClaimUnavailable,
    LOCAL_ECHO_FENCE_SECONDS,
)
from shared.protocol import VoiceLocalPlayoutEvent


NOW = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)


def _id() -> str:
    return str(uuid.uuid4())


def _session(**changes: object) -> SimpleNamespace:
    values = {
        "session_id": _id(),
        "device_id": _id(),
        "owner_connection_generation": _id(),
        "generation": 1,
        "media_grant_revision": 1,
        "speech_backend": "client_local",
        "state": "active",
        "foreground_active": True,
        "speech_muted": False,
        "ended_at": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_lifecycle_announcement_uses_only_server_policy_text() -> None:
    registry = ClientLocalAnnouncementRegistry()
    session = _session()
    frame = registry.issue(
        session=session,
        kind="acknowledgement",
        turn_id=_id(),
        requested_text="untrusted client or model text",
        output_policy="lifecycle",
        mute_revision=1,
        consent_revision=1,
        now=NOW,
    )

    assert frame.text == APPROVED_PHRASE_TEXT["on_it"]
    assert frame.text_digest_sha256 == hashlib.sha256(frame.text.encode()).hexdigest()
    assert len(frame.text.encode()) <= 600
    assert frame.expires_at == "2026-08-28T19:00:10Z"


def test_greeting_has_null_turn_and_result_must_be_explicitly_authorized() -> None:
    registry = ClientLocalAnnouncementRegistry()
    session = _session()
    greeting = registry.issue(
        session=session,
        kind="greeting",
        turn_id=None,
        requested_text="ignored",
        output_policy="lifecycle",
        mute_revision=1,
        consent_revision=1,
        now=NOW,
    )
    assert greeting.turn_id is None

    with pytest.raises(ClaimUnavailable):
        registry.issue(
            session=session,
            kind="result",
            turn_id=_id(),
            requested_text="result",
            output_policy="full_recap",
            mute_revision=1,
            consent_revision=1,
            now=NOW,
            server_authorized=False,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"speech_muted": True},
        {"foreground_active": False},
        {"ended_at": NOW},
        {"speech_backend": "llm_factory"},
    ],
)
def test_announcement_fails_closed_for_mute_foreground_end_backend(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ClaimUnavailable):
        ClientLocalAnnouncementRegistry().issue(
            session=_session(**changes),
            kind="failure",
            turn_id=_id(),
            requested_text="ignored",
            output_policy="lifecycle",
            mute_revision=1,
            consent_revision=1,
            now=NOW,
        )


def test_playout_is_content_free_ordered_and_fenced_by_revisions() -> None:
    registry = ClientLocalAnnouncementRegistry()
    session = _session()
    announcement = registry.issue(
        session=session,
        kind="failure",
        turn_id=_id(),
        requested_text="ignored",
        output_policy="lifecycle",
        mute_revision=3,
        consent_revision=4,
        now=NOW,
    )
    event = VoiceLocalPlayoutEvent(
        device_id=announcement.device_id,
        connection_generation=announcement.connection_generation,
        session_id=announcement.session_id,
        generation=announcement.generation,
        speech_revision=announcement.speech_revision,
        announcement_id=announcement.announcement_id,
        announcement_sequence=announcement.announcement_sequence,
        turn_id=announcement.turn_id,
        kind=announcement.kind,
        phase="started",
        client_sequence=1,
        observed_at="2026-08-28T19:00:01Z",
    )
    registry.observe(
        session=session,
        event=event,
        mute_revision=3,
        consent_revision=4,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ClaimUnavailable):
        registry.observe(
            session=session,
            event=event,
            mute_revision=2,
            consent_revision=4,
            now=NOW + timedelta(seconds=2),
        )
    assert "text" not in event.to_json()
    assert LOCAL_ECHO_FENCE_SECONDS == 0.5


def test_local_announcement_terminal_playout_scrubs_and_fences_state() -> None:
    with pytest.raises(ValueError, match="invalid_local_announcement_capacity"):
        ClientLocalAnnouncementRegistry(capacity=0)

    registry = ClientLocalAnnouncementRegistry(capacity=1)
    session = _session()
    announcement = registry.issue(
        session=session,
        kind="result",
        turn_id=_id(),
        requested_text="Authorized recap",
        output_policy="full_recap",
        mute_revision=2,
        consent_revision=3,
        now=NOW,
        server_authorized=True,
    )
    assert registry.retained_counts() == {"sessions": 1, "announcements": 1}
    assert registry.next_revisions(
        session_id=session.session_id,
        generation=session.generation,
    ) == (2, 3)

    def event(phase: str, sequence: int) -> VoiceLocalPlayoutEvent:
        return VoiceLocalPlayoutEvent(
            device_id=announcement.device_id,
            connection_generation=announcement.connection_generation,
            session_id=announcement.session_id,
            generation=announcement.generation,
            speech_revision=announcement.speech_revision,
            announcement_id=announcement.announcement_id,
            announcement_sequence=announcement.announcement_sequence,
            turn_id=announcement.turn_id,
            kind=announcement.kind,
            phase=phase,
            client_sequence=sequence,
            observed_at="2026-08-28T19:00:01Z",
        )

    registry.observe_current(session=session, event=event("started", 1), now=NOW)
    registry.observe_current(session=session, event=event("finished", 2), now=NOW)
    assert registry.retained_counts()["announcements"] == 0
    with pytest.raises(ClaimUnavailable, match="local_playout_not_authorized"):
        registry.observe_current(session=session, event=event("finished", 3), now=NOW)

    registry.fence_session(
        session_id=session.session_id,
        generation=session.generation,
    )
    mute, consent = registry.next_revisions(
        session_id=session.session_id,
        generation=session.generation,
    )
    assert (mute, consent) == (3, 4)
    registry.clear_session(
        session_id=session.session_id,
        generation=session.generation,
    )
    assert registry.retained_counts() == {"sessions": 0, "announcements": 0}
    assert registry.next_revisions(
        session_id=session.session_id,
        generation=session.generation,
    ) == (1, 1)


def test_local_announcement_policy_revision_and_order_denials_are_stable() -> None:
    registry = ClientLocalAnnouncementRegistry()
    session = _session()
    issued = registry.issue(
        session=session,
        kind="failure",
        turn_id=_id(),
        requested_text="ignored",
        output_policy="lifecycle",
        mute_revision=2,
        consent_revision=2,
        now=NOW,
    )
    with pytest.raises(ClaimUnavailable, match="stale_local_announcement_revision"):
        registry.issue(
            session=session,
            kind="failure",
            turn_id=_id(),
            requested_text="ignored",
            output_policy="lifecycle",
            mute_revision=1,
            consent_revision=2,
            now=NOW,
        )
    with pytest.raises(ClaimUnavailable, match="local_announcement_policy_invalid"):
        registry.issue(
            session=session,
            kind="failure",
            turn_id=_id(),
            requested_text="ignored",
            output_policy="full_recap",
            mute_revision=2,
            consent_revision=2,
            now=NOW,
        )
    with pytest.raises(ClaimUnavailable, match="local_announcement_text_invalid"):
        registry.issue(
            session=session,
            kind="result",
            turn_id=_id(),
            requested_text="x" * 601,
            output_policy="full_recap",
            mute_revision=2,
            consent_revision=2,
            now=NOW,
            server_authorized=True,
        )
    finished = VoiceLocalPlayoutEvent(
        device_id=issued.device_id,
        connection_generation=issued.connection_generation,
        session_id=issued.session_id,
        generation=issued.generation,
        speech_revision=issued.speech_revision,
        announcement_id=issued.announcement_id,
        announcement_sequence=issued.announcement_sequence,
        turn_id=issued.turn_id,
        kind=issued.kind,
        phase="finished",
        client_sequence=1,
        observed_at="2026-08-28T19:00:01Z",
    )
    with pytest.raises(ClaimUnavailable, match="local_playout_out_of_order"):
        registry.observe_current(session=session, event=finished, now=NOW)
