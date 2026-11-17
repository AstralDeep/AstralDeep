"""Tests for src/astralplane/repositories/voice.py: session/turn creation and idempotent
replay, mixed-backend metadata rejection, owner-scoped reads, and generation-fenced
transitions with attribution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _support import ScriptedTransaction

from astralplane.errors import PlaneError
from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.voice import (
    VoiceRepository,
    VoiceSessionCreate,
    VoiceSessionState,
    VoiceTurnCreate,
    VoiceTurnState,
)

NOW = datetime(2026, 8, 13, 20, tzinfo=UTC)


def session_create(**overrides: object) -> VoiceSessionCreate:
    values: dict[str, object] = {
        "session_id": "session-1",
        "owner_id": "owner-1",
        "activation_id": "activation-1",
        "device_id": "device-1",
        "device_kind": "web",
        "speech_backend": "llm_factory",
        "transport": "livekit",
        "room_name": "room-1",
        "participant_identity": "participant-1",
        "visible_chat_id": "chat-1",
        "owner_connection_generation": "connection-1",
        "control_binding_id": "binding-1",
        "control_binding_expires_at": NOW + timedelta(minutes=2),
        "lease_expires_at": NOW + timedelta(minutes=1),
        "media_grant_nonce_hash": b"n" * 32,
        "media_grant_issued_at": NOW,
        "media_grant_expires_at": NOW + timedelta(seconds=30),
        "started_at": NOW,
    }
    values.update(overrides)
    return VoiceSessionCreate(**values)  # type: ignore[arg-type]


def session_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "session_id": "session-1",
        "user_id": "owner-1",
        "activation_id": "activation-1",
        "device_id": "device-1",
        "device_kind": "web",
        "speech_backend": "llm_factory",
        "transport": "livekit",
        "room_name": "room-1",
        "participant_identity": "participant-1",
        "visible_chat_id": "chat-1",
        "chat_context_revision": 1,
        "state": "starting",
        "generation": 1,
        "owner_connection_generation": "connection-1",
        "control_binding_id": "binding-1",
        "control_binding_expires_at": NOW + timedelta(minutes=2),
        "lease_expires_at": NOW + timedelta(minutes=1),
        "media_grant_nonce_hash": b"n" * 32,
        "media_grant_issued_at": NOW,
        "media_grant_expires_at": NOW + timedelta(seconds=30),
        "media_grant_consumed_at": None,
        "last_media_refresh_id": None,
        "worker_identity": None,
        "worker_assignment_id": None,
        "worker_rtc_grant_revision": 1,
        "worker_rtc_grant_issued_at": None,
        "worker_rtc_grant_expires_at": None,
        "started_at": NOW,
        "updated_at": NOW,
        "ended_at": None,
        "end_reason": None,
    }
    row.update(overrides)
    return row


def turn_create(**overrides: object) -> VoiceTurnCreate:
    values: dict[str, object] = {
        "turn_id": "turn-1",
        "client_turn_id": "client-turn-1",
        "session_id": "session-1",
        "session_generation": 1,
        "media_grant_revision": 1,
        "owner_id": "owner-1",
        "chat_id": "chat-1",
        "chat_context_revision": 1,
        "execution_base_render_revision": 0,
        "submission_id": "submission-1",
        "request_generation": "request-1",
    }
    values.update(overrides)
    return VoiceTurnCreate(**values)  # type: ignore[arg-type]


def turn_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "turn_id": "turn-1",
        "client_turn_id": "client-turn-1",
        "session_id": "session-1",
        "session_generation": 1,
        "media_grant_revision": 1,
        "user_id": "owner-1",
        "chat_id": "chat-1",
        "chat_context_revision": 1,
        "execution_base_render_revision": 0,
        "submission_id": "submission-1",
        "request_generation": "request-1",
        "state": "recognizing",
        "operation_id": None,
        "result_id": None,
        "terminal_kind": None,
        "accepted_at": None,
        "terminal_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"device_kind": "terminal"},
        {"transport": "ssh"},
        {"media_grant_nonce_hash": b"short"},
        {"started_at": NOW.replace(tzinfo=None)},
        {"lease_expires_at": NOW},
        {"media_grant_expires_at": NOW},
    ],
)
def test_session_metadata_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        session_create(**changes)


def test_client_local_session_metadata_has_no_remote_media_fields() -> None:
    created = session_create(
        speech_backend="client_local",
        transport="client_local",
        room_name=None,
        participant_identity=None,
        media_grant_nonce_hash=None,
        media_grant_issued_at=None,
        media_grant_expires_at=None,
    )

    assert created.speech_backend == "client_local"
    assert created.transport == "client_local"


@pytest.mark.parametrize(
    "changes",
    [
        {"speech_backend": "remote"},
        {"speech_backend": "llm_factory", "transport": "client_local"},
        {"speech_backend": "client_local", "transport": "livekit"},
        {
            "speech_backend": "client_local",
            "transport": "client_local",
            "room_name": "remote-room",
            "participant_identity": None,
            "media_grant_nonce_hash": None,
            "media_grant_issued_at": None,
            "media_grant_expires_at": None,
        },
        {
            "speech_backend": "client_local",
            "transport": "client_local",
            "room_name": None,
            "participant_identity": "remote-participant",
            "media_grant_nonce_hash": None,
            "media_grant_issued_at": None,
            "media_grant_expires_at": None,
        },
        {
            "speech_backend": "client_local",
            "transport": "client_local",
            "room_name": None,
            "participant_identity": None,
            "media_grant_nonce_hash": b"n" * 32,
            "media_grant_issued_at": None,
            "media_grant_expires_at": None,
        },
        {"speech_backend": "llm_factory", "room_name": None},
        {"speech_backend": "llm_factory", "participant_identity": None},
        {"speech_backend": "llm_factory", "media_grant_nonce_hash": None},
        {"speech_backend": "llm_factory", "media_grant_issued_at": None},
        {"speech_backend": "llm_factory", "media_grant_expires_at": None},
    ],
)
def test_session_metadata_rejects_mixed_backend_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="speech backend"):
        session_create(**changes)


def test_create_session_is_idempotent_and_owner_scoped() -> None:
    repository = VoiceRepository()
    created = repository.create_session(ScriptedTransaction(one=[session_row()]), session_create())
    assert created.owner_id == "owner-1"
    assert created.state is VoiceSessionState.STARTING
    replay = repository.create_session(
        ScriptedTransaction(one=[None, session_row()]), session_create()
    )
    assert replay == created
    with pytest.raises(PlaneError) as raised:
        repository.create_session(
            ScriptedTransaction(one=[None, session_row(device_id="different")]),
            session_create(),
        )
    assert raised.value.code == "voice_session_idempotency_conflict"


def test_create_and_read_client_local_session_preserve_immutable_backend() -> None:
    create = session_create(
        speech_backend="client_local",
        transport="client_local",
        room_name=None,
        participant_identity=None,
        media_grant_nonce_hash=None,
        media_grant_issued_at=None,
        media_grant_expires_at=None,
    )
    row = session_row(
        speech_backend="client_local",
        transport="client_local",
        room_name=None,
        participant_identity=None,
        worker_identity=None,
        media_grant_nonce_hash=None,
        media_grant_issued_at=None,
        media_grant_expires_at=None,
        media_grant_consumed_at=None,
        last_media_refresh_id=None,
        worker_assignment_id=None,
        worker_rtc_grant_revision=None,
        worker_rtc_grant_issued_at=None,
        worker_rtc_grant_expires_at=None,
    )
    transaction = ScriptedTransaction(one=[row])

    created = VoiceRepository().create_session(transaction, create)

    assert created.speech_backend == "client_local"
    assert created.transport == "client_local"
    assert transaction.calls[0][2][5:10] == (
        "client_local",
        "client_local",
        None,
        None,
        "chat-1",
    )


def test_session_read_rejects_mixed_persisted_backend_shape() -> None:
    with pytest.raises(RepositoryValidationError, match="speech backend"):
        VoiceRepository().get_session(
            ScriptedTransaction(
                one=[
                    session_row(
                        speech_backend="client_local",
                        transport="client_local",
                        room_name="remote-room",
                    )
                ]
            ),
            owner_id="owner-1",
            session_id="session-1",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"media_grant_nonce_hash": b"short"},
        {"media_grant_expires_at": NOW},
        {"media_grant_consumed_at": NOW - timedelta(seconds=1)},
        {"worker_identity": "worker-1"},
        {
            "worker_identity": "worker-1",
            "worker_assignment_id": "assignment-1",
            "worker_rtc_grant_issued_at": NOW + timedelta(seconds=2),
            "worker_rtc_grant_expires_at": NOW + timedelta(seconds=1),
        },
    ],
)
def test_session_read_rejects_malformed_remote_backend_shape(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(RepositoryValidationError, match="speech backend"):
        VoiceRepository().get_session(
            ScriptedTransaction(one=[session_row(**overrides)]),
            owner_id="owner-1",
            session_id="session-1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "session-2"),
        ("user_id", "owner-2"),
        ("activation_id", "activation-2"),
        ("speech_backend", "client_local"),
        ("transport", "watch_pcm_websocket"),
        ("room_name", "room-2"),
        ("participant_identity", "participant-2"),
        ("control_binding_id", "binding-2"),
        ("control_binding_expires_at", NOW + timedelta(minutes=3)),
        ("lease_expires_at", NOW + timedelta(minutes=3)),
        ("media_grant_nonce_hash", b"x" * 32),
        ("media_grant_issued_at", NOW + timedelta(seconds=1)),
        ("media_grant_expires_at", NOW + timedelta(seconds=31)),
        ("started_at", NOW + timedelta(seconds=1)),
    ],
)
def test_session_replay_compares_every_security_identity(field: str, value: object) -> None:
    with pytest.raises(PlaneError) as raised:
        VoiceRepository().create_session(
            ScriptedTransaction(one=[None, session_row(**{field: value})]),
            session_create(),
        )
    assert raised.value.code == "voice_session_idempotency_conflict"


def test_session_reads_hide_cross_owner_rows() -> None:
    repository = VoiceRepository()
    value = repository.get_session(
        ScriptedTransaction(one=[session_row()]),
        owner_id="owner-1",
        session_id="session-1",
    )
    assert value is not None
    assert (
        repository.get_session(
            ScriptedTransaction(one=[None]), owner_id="other", session_id="session-1"
        )
        is None
    )
    assert (
        repository.get_live_session(ScriptedTransaction(one=[session_row()]), owner_id="owner-1")
        == value
    )
    assert repository.get_live_session(ScriptedTransaction(one=[None]), owner_id="other") is None


def test_session_transitions_use_generation_and_owner_fences() -> None:
    repository = VoiceRepository()
    active_row = session_row(
        state="active",
        generation=2,
        chat_context_revision=2,
        visible_chat_id="chat-2",
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    transaction = ScriptedTransaction(one=[active_row])
    active = repository.advance_session(
        transaction,
        owner_id="owner-1",
        session_id="session-1",
        expected_generation=1,
        expected_context_revision=1,
        state=VoiceSessionState.ACTIVE,
        visible_chat_id="chat-2",
        lease_expires_at=NOW + timedelta(minutes=2),
        now=NOW,
    )
    assert active.generation == 2
    assert "user_id = %s" in transaction.fetch_sql()

    ended_row = session_row(
        state="ended",
        generation=3,
        ended_at=NOW + timedelta(minutes=1),
        end_reason="user",
    )
    ended = repository.end_session(
        ScriptedTransaction(one=[ended_row]),
        owner_id="owner-1",
        session_id="session-1",
        expected_generation=2,
        reason="user",
        ended_at=NOW + timedelta(minutes=1),
    )
    assert ended.state is VoiceSessionState.ENDED
    assert ended.end_reason == "user"

    unavailable_transaction = ScriptedTransaction(
        one=[
            session_row(
                state="ended",
                generation=3,
                ended_at=NOW + timedelta(minutes=1),
                end_reason="chat_deleted",
            )
        ]
    )
    repository.end_session(
        unavailable_transaction,
        owner_id="owner-1",
        session_id="session-1",
        expected_generation=2,
        reason="chat_deleted",
        ended_at=NOW + timedelta(minutes=1),
    )
    assert "chat_unavailable_at" in unavailable_transaction.fetch_sql()
    assert unavailable_transaction.calls[0][2][2:5] == (
        "chat_deleted",
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=1),
    )


def test_session_transition_rejections_are_visible() -> None:
    repository = VoiceRepository()
    with pytest.raises(ValueError):
        repository.advance_session(
            ScriptedTransaction(),
            owner_id="owner-1",
            session_id="session-1",
            expected_generation=1,
            expected_context_revision=1,
            state=VoiceSessionState.ENDED,
            visible_chat_id="chat-1",
            lease_expires_at=NOW + timedelta(seconds=1),
            now=NOW,
        )
    with pytest.raises(ValueError):
        repository.advance_session(
            ScriptedTransaction(),
            owner_id="owner-1",
            session_id="session-1",
            expected_generation=0,
            expected_context_revision=1,
            state=VoiceSessionState.ACTIVE,
            visible_chat_id="chat-1",
            lease_expires_at=NOW,
            now=NOW,
        )
    with pytest.raises(PlaneError) as raised:
        repository.advance_session(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            session_id="session-1",
            expected_generation=1,
            expected_context_revision=1,
            state=VoiceSessionState.ACTIVE,
            visible_chat_id="chat-1",
            lease_expires_at=NOW + timedelta(seconds=1),
            now=NOW,
        )
    assert raised.value.code == "voice_session_state_conflict"
    with pytest.raises(ValueError):
        repository.end_session(
            ScriptedTransaction(),
            owner_id="owner-1",
            session_id="session-1",
            expected_generation=1,
            reason="unknown",
            ended_at=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"turn_id": ""},
        {"session_generation": 0},
        {"execution_base_render_revision": -1},
    ],
)
def test_turn_metadata_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        turn_create(**changes)


def test_turn_create_read_and_idempotency() -> None:
    repository = VoiceRepository()
    created = repository.create_turn(ScriptedTransaction(one=[turn_row()]), turn_create())
    assert created.state is VoiceTurnState.RECOGNIZING
    assert (
        repository.create_turn(ScriptedTransaction(one=[None, turn_row()]), turn_create())
        == created
    )
    assert (
        repository.get_turn(
            ScriptedTransaction(one=[turn_row()]), owner_id="owner-1", turn_id="turn-1"
        )
        == created
    )
    assert (
        repository.get_turn(ScriptedTransaction(one=[None]), owner_id="other", turn_id="turn-1")
        is None
    )
    with pytest.raises(PlaneError) as raised:
        repository.create_turn(
            ScriptedTransaction(one=[None, turn_row(chat_id="other")]), turn_create()
        )
    assert raised.value.code == "voice_turn_idempotency_conflict"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("turn_id", "turn-2"),
        ("client_turn_id", "client-turn-2"),
        ("session_id", "session-2"),
        ("session_generation", 2),
        ("media_grant_revision", 2),
        ("user_id", "owner-2"),
        ("chat_id", "chat-2"),
        ("chat_context_revision", 2),
        ("execution_base_render_revision", 1),
        ("submission_id", "submission-2"),
        ("request_generation", "request-2"),
    ],
)
def test_turn_replay_compares_every_security_and_fencing_identity(
    field: str, value: object
) -> None:
    with pytest.raises(PlaneError) as raised:
        VoiceRepository().create_turn(
            ScriptedTransaction(one=[None, turn_row(**{field: value})]), turn_create()
        )
    assert raised.value.code == "voice_turn_idempotency_conflict"


def test_turn_create_binds_session_chat_and_media_grant() -> None:
    transaction = ScriptedTransaction(one=[turn_row()])
    VoiceRepository().create_turn(transaction, turn_create())
    statement = transaction.fetch_sql()
    assert "visible_chat_id = %s" in statement
    assert "media_grant_revision = %s" in statement
    assert transaction.calls[0][2][-2:] == ("chat-1", 1)


def test_turn_transitions_preserve_attribution_and_terminal_evidence() -> None:
    repository = VoiceRepository()
    accepted_row = turn_row(state="accepted", operation_id="operation-1", accepted_at=NOW)
    transaction = ScriptedTransaction(one=[accepted_row])
    accepted = repository.transition_turn(
        transaction,
        owner_id="owner-1",
        turn_id="turn-1",
        expected_state=VoiceTurnState.SUBMITTING,
        state=VoiceTurnState.ACCEPTED,
        operation_id="operation-1",
        result_id=None,
        now=NOW,
    )
    assert accepted.accepted_at == NOW
    assert transaction.calls[0][2][9:11] == ("owner-1", "submitting")  # type: ignore[index]

    complete_row = turn_row(
        state="succeeded",
        operation_id="operation-1",
        result_id="result-1",
        terminal_kind="succeeded",
        terminal_at=NOW,
    )
    complete = repository.transition_turn(
        ScriptedTransaction(one=[complete_row]),
        owner_id="owner-1",
        turn_id="turn-1",
        expected_state=VoiceTurnState.PROCESSING,
        state=VoiceTurnState.SUCCEEDED,
        operation_id=None,
        result_id="result-1",
        now=NOW,
    )
    assert complete.terminal_kind == "succeeded"
    assert complete.result_id == "result-1"


def test_stale_turn_transition_is_visible() -> None:
    with pytest.raises(PlaneError) as raised:
        VoiceRepository().transition_turn(
            ScriptedTransaction(one=[None]),
            owner_id="owner-1",
            turn_id="turn-1",
            expected_state=VoiceTurnState.PROCESSING,
            state=VoiceTurnState.FAILED,
            operation_id=None,
            result_id=None,
            now=NOW,
        )
    assert raised.value.code == "voice_turn_state_conflict"


def test_turn_transition_cannot_reopen_terminal_or_overwrite_attribution() -> None:
    repository = VoiceRepository()
    terminal = ScriptedTransaction(one=[None])
    with pytest.raises(PlaneError) as raised:
        repository.transition_turn(
            terminal,
            owner_id="owner-1",
            turn_id="turn-1",
            expected_state=VoiceTurnState.SUCCEEDED,
            state=VoiceTurnState.PROCESSING,
            operation_id="operation-1",
            result_id="result-1",
            now=NOW,
        )
    assert raised.value.code == "voice_turn_state_conflict"
    assert "state NOT IN" in terminal.fetch_sql()

    overwrite = ScriptedTransaction(one=[None])
    with pytest.raises(PlaneError):
        repository.transition_turn(
            overwrite,
            owner_id="owner-1",
            turn_id="turn-1",
            expected_state=VoiceTurnState.PROCESSING,
            state=VoiceTurnState.WAITING_ON_USER,
            operation_id="different-operation",
            result_id="different-result",
            now=NOW,
        )
    statement = overwrite.fetch_sql()
    assert "COALESCE(operation_id, %s)" in statement
    assert "operation_id::text = %s" in statement
    assert "COALESCE(result_id, %s)" in statement
    assert "result_id = %s" in statement
