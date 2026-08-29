"""Real-PostgreSQL repository tests for Feature 065 voice ownership state."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import orchestrator.voice_sessions as voice_sessions_module
from shared.voice_transcript import TranscriptProofBinding, issue_transcript_proof
from tests.helpers.voice_plane_runtime import (
    VoicePlaneTestRuntime,
    isolated_voice_plane_runtime,
    voice_session_repository,
)

from orchestrator.voice_coordinator import (
    AnnouncementClaimRequest,
    ClaimUnavailable,
    RecognitionStart,
    StaleFence,
    TranscriptTurnBinding,
)
from orchestrator.voice_sessions import (
    ContextSyncPending,
    CreateSession,
    IdempotencyConflict,
    MediaGrantRefresh,
    RecognitionBinding,
    SessionControl,
    SessionTakeover,
    SessionUpdate,
    StaleSessionFence,
    TakeoverRequired,
    TranscriptSubmission,
    TranscriptSubmissionRejected,
    VoiceSessionNotFound,
    VoiceSessionRepository,
    VoiceSessionRepositoryError,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[VoicePlaneTestRuntime]:
    """Create an isolated database through the current Plane migration path."""

    with isolated_voice_plane_runtime("voice_065") as runtime:
        yield runtime


@pytest.fixture
def repository(database: VoicePlaneTestRuntime) -> VoiceSessionRepository:
    return voice_session_repository(database)


def _create(
    *,
    user_id: str | None = None,
    activation_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    chat_id: uuid.UUID | None = None,
    connection_generation: uuid.UUID | None = None,
    binding_id: uuid.UUID | None = None,
    room_name: str | None = None,
    participant_identity: str | None = None,
) -> CreateSession:
    token = uuid.uuid4().hex
    return CreateSession(
        user_id=user_id or f"voice-user-{token}",
        activation_id=str(activation_id or uuid.uuid4()),
        device_id=str(device_id or uuid.uuid4()),
        device_kind="web",
        transport="livekit",
        room_name=room_name or f"room-{token}",
        participant_identity=participant_identity or f"client-{token}",
        visible_chat_id=str(chat_id or uuid.uuid4()),
        owner_connection_generation=str(connection_generation or uuid.uuid4()),
        control_binding_id=str(binding_id or uuid.uuid4()),
        control_binding_expires_at=NOW + timedelta(minutes=10),
        lease_expires_at=NOW + timedelta(seconds=45),
        media_grant_nonce_hash=os.urandom(32),
        media_grant_issued_at=NOW,
        media_grant_expires_at=NOW + timedelta(minutes=5),
    )


def _activate_and_sync(
    repository: VoiceSessionRepository,
    create: CreateSession,
    *,
    owner_id: str = "replica-a",
):
    created = repository.create_session(create, now=NOW)
    session = created.session
    asyncio.run(
        repository.claim_control_lease(
            user_id=create.user_id,
            session_id=session.session_id,
            generation=session.generation,
            owner_id=owner_id,
            now=NOW,
        )
    )
    repository.assign_worker(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        assignment_id=str(uuid.uuid4()),
        worker_identity=f"worker-{uuid.uuid4().hex}",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    repository.apply_chat_context(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_media_grant_revision=session.media_grant_revision,
        control_owner_id=owner_id,
        visible_chat_id=session.visible_chat_id,
        chat_context_revision=session.chat_context_revision,
        now=NOW,
    )
    return repository.mark_session_active(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_media_grant_revision=session.media_grant_revision,
        now=NOW,
    )


def _control(create: CreateSession, **changes) -> SessionControl:
    values = {
        "device_id": create.device_id,
        "connection_generation": create.owner_connection_generation,
        "binding_id": create.control_binding_id,
        "binding_expires_at": create.control_binding_expires_at,
    }
    values.update(changes)
    return SessionControl(**values)


def test_create_is_durable_and_exact_activation_replay_is_idempotent(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    request = _create()

    first = repository.create_session(request, now=NOW)
    restarted = voice_session_repository(database)
    replay = restarted.create_session(request, now=NOW + timedelta(seconds=1))

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.session == first.session
    assert (
        database.fetch_one(
            "SELECT COUNT(*) AS count FROM voice_session WHERE user_id = ?",
            (request.user_id,),
        )["count"]
        == 1
    )


def test_concurrent_first_owner_is_row_locked_and_only_one_session_wins(
    repository: VoiceSessionRepository,
) -> None:
    user_id = f"voice-race-{uuid.uuid4().hex}"
    requests = (_create(user_id=user_id), _create(user_id=user_id))

    def attempt(request: CreateSession):
        try:
            return repository.create_session(request, now=NOW)
        except TakeoverRequired as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, requests))

    created = [result for result in results if not isinstance(result, Exception)]
    blocked = [result for result in results if isinstance(result, TakeoverRequired)]
    assert len(created) == 1
    assert len(blocked) == 1
    assert blocked[0].current.session_id == created[0].session.session_id
    assert repository.get_live_session(user_id=user_id) == created[0].session


def test_takeover_is_generation_cas_and_exact_retry_does_not_take_over_twice(
    repository: VoiceSessionRepository,
) -> None:
    user_id = f"voice-takeover-{uuid.uuid4().hex}"
    original_request = _create(user_id=user_id)
    original = repository.create_session(original_request, now=NOW).session
    replacement_base = _create(user_id=user_id)
    request = SessionTakeover(
        previous_session_id=original.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        create=replacement_base,
    )

    first = repository.take_over_session(request, now=NOW + timedelta(seconds=1))
    replay = repository.take_over_session(request, now=NOW + timedelta(seconds=2))

    assert first.replayed is False
    assert first.session.generation == 2
    assert first.session.takeover_of_session_id == original.session_id
    assert replay.replayed is True
    assert replay.session == first.session
    ended = repository.get_session(
        user_id=user_id,
        session_id=original.session_id,
    )
    assert ended.state == "ended"
    assert ended.end_reason == "takeover"
    stale_request = replace(
        request,
        create=_create(user_id=user_id),
    )
    with pytest.raises(StaleSessionFence, match="stale_generation"):
        repository.take_over_session(stale_request, now=NOW + timedelta(seconds=3))


def test_identity_end_exact_cas_replays_and_rejects_replacement(
    repository: VoiceSessionRepository,
) -> None:
    exact_create = _create()
    exact = repository.create_session(exact_create, now=NOW).session

    ended = repository.end_live_user_session(
        user_id=exact_create.user_id,
        reason="logout",
        now=NOW + timedelta(seconds=1),
        expected_session_id=exact.session_id,
        expected_generation=exact.generation,
    )
    replay = repository.end_live_user_session(
        user_id=exact_create.user_id,
        reason="logout",
        now=NOW + timedelta(seconds=2),
        expected_session_id=exact.session_id,
        expected_generation=exact.generation,
    )

    assert ended is not None
    assert replay == ended

    takeover_create = _create()
    original = repository.create_session(takeover_create, now=NOW).session
    replacement_request = SessionTakeover(
        previous_session_id=original.session_id,
        expected_generation=original.generation,
        expected_media_grant_revision=original.media_grant_revision,
        create=_create(user_id=takeover_create.user_id),
    )
    replacement = repository.take_over_session(
        replacement_request,
        now=NOW + timedelta(seconds=1),
    ).session

    with pytest.raises(StaleSessionFence, match="stale_generation"):
        repository.end_live_user_session(
            user_id=takeover_create.user_id,
            reason="auth_expired",
            now=NOW + timedelta(seconds=2),
            expected_session_id=original.session_id,
            expected_generation=original.generation,
        )
    assert (
        repository.get_live_session(user_id=takeover_create.user_id)
        == replacement
    )


def test_identity_end_unit_cas_is_checked_under_owner_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact identity fence is evaluated only after owner serialization."""

    expected_session_id = str(uuid.uuid4())
    replacement_session_id = str(uuid.uuid4())
    transaction = object()
    calls: list[str] = []
    live_row = {
        "session_id": expected_session_id,
        "generation": 1,
    }
    voice = SimpleNamespace(
        lock_identity=Mock(side_effect=lambda *_args, **_kwargs: calls.append("lock")),
        get_live_session_record=Mock(
            side_effect=lambda *_args, **_kwargs: (
                calls.append("live") or live_row
            )
        ),
        get_session_record=Mock(),
    )
    repository = object.__new__(VoiceSessionRepository)
    repository._plane = SimpleNamespace(  # type: ignore[attr-defined]
        transaction=lambda: nullcontext(transaction)
    )
    repository._voice = voice  # type: ignore[attr-defined]
    ended = object()
    repository._end_session_row = Mock(  # type: ignore[method-assign]
        return_value=ended
    )
    monkeypatch.setattr(
        voice_sessions_module,
        "_session",
        lambda row: row,
    )

    assert repository.end_live_user_session(
        user_id="user-a",
        reason="logout",
        now=NOW,
        expected_session_id=expected_session_id,
        expected_generation=1,
    ) is ended
    assert calls == ["lock", "live"]

    live_row = {
        "session_id": replacement_session_id,
        "generation": 2,
    }
    calls.clear()
    with pytest.raises(StaleSessionFence, match="stale_generation"):
        repository.end_live_user_session(
            user_id="user-a",
            reason="auth_expired",
            now=NOW,
            expected_session_id=expected_session_id,
            expected_generation=1,
        )
    assert calls == ["lock", "live"]
    repository._end_session_row.assert_called_once()

    ended_row = {
        "session_id": expected_session_id,
        "generation": 1,
        "ended_at": NOW,
    }
    live_row = None
    voice.get_session_record.return_value = ended_row
    assert repository.end_live_user_session(
        user_id="user-a",
        reason="logout",
        now=NOW,
        expected_session_id=expected_session_id,
        expected_generation=1,
    ) is ended_row
    with pytest.raises(ValueError, match="incomplete_identity_end_fence"):
        repository.end_live_user_session(
            user_id="user-a",
            reason="logout",
            now=NOW,
            expected_session_id=expected_session_id,
        )


def test_owner_scope_hides_sessions_and_stale_fences_fail_closed(
    repository: VoiceSessionRepository,
) -> None:
    request = _create()
    session = repository.create_session(request, now=NOW).session

    with pytest.raises(VoiceSessionNotFound):
        repository.get_session(user_id="another-user", session_id=session.session_id)
    with pytest.raises(StaleSessionFence, match="stale_generation"):
        repository.renew_session_lease(
            user_id=request.user_id,
            session_id=session.session_id,
            expected_generation=2,
            expected_media_grant_revision=1,
            lease_duration=timedelta(seconds=30),
            now=NOW,
        )


def test_same_device_reconnect_rebinds_but_cross_device_control_is_refused(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    active = _activate_and_sync(repository, create)
    replacement = _control(
        create,
        connection_generation=str(uuid.uuid4()),
        binding_id=str(uuid.uuid4()),
        binding_expires_at=NOW + timedelta(minutes=8),
    )

    rebound = repository.get_controlled_session(
        user_id=create.user_id,
        session_id=active.session_id,
        expected_generation=active.generation,
        expected_media_grant_revision=active.media_grant_revision,
        control=replacement,
        now=NOW + timedelta(seconds=1),
    )
    assert rebound.device_id == create.device_id
    assert rebound.owner_connection_generation == replacement.connection_generation
    assert rebound.control_binding_id == replacement.binding_id

    with pytest.raises(
        VoiceSessionRepositoryError,
        match="binding_scope_mismatch",
    ):
        repository.get_controlled_session(
            user_id=create.user_id,
            session_id=active.session_id,
            expected_generation=active.generation,
            expected_media_grant_revision=active.media_grant_revision,
            control=replace(replacement, device_id=str(uuid.uuid4())),
            now=NOW + timedelta(seconds=2),
        )


def test_fence_only_update_rebinds_control_without_resetting_true_idle(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    active = _activate_and_sync(repository, create)
    idle = repository.set_true_idle(
        user_id=create.user_id,
        session_id=active.session_id,
        expected_generation=active.generation,
        listening=True,
        user_input_gate=False,
        now=NOW + timedelta(seconds=1),
    )
    replacement = _control(
        create,
        connection_generation=str(uuid.uuid4()),
        binding_id=str(uuid.uuid4()),
        binding_expires_at=NOW + timedelta(minutes=8),
    )

    heartbeat = repository.update_session(
        SessionUpdate(
            user_id=create.user_id,
            session_id=active.session_id,
            expected_generation=active.generation,
            expected_media_grant_revision=active.media_grant_revision,
            control=replacement,
        ),
        now=NOW + timedelta(seconds=2),
    )

    assert heartbeat.owner_connection_generation == replacement.connection_generation
    assert heartbeat.control_binding_id == replacement.binding_id
    assert heartbeat.control_binding_expires_at == replacement.binding_expires_at
    assert heartbeat.last_interaction_at == idle.last_interaction_at
    assert heartbeat.idle_started_at == idle.idle_started_at
    assert heartbeat.foreground_active == active.foreground_active
    assert heartbeat.microphone_enabled == active.microphone_enabled
    assert heartbeat.speech_muted == active.speech_muted


def test_backend_mismatch_is_rejected_under_row_lock_before_rebinding(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    active = _activate_and_sync(repository, create)
    replacement = _control(
        create,
        connection_generation=str(uuid.uuid4()),
        binding_id=str(uuid.uuid4()),
        binding_expires_at=NOW + timedelta(minutes=8),
    )

    with pytest.raises(VoiceSessionRepositoryError, match="backend_mismatch"):
        repository.update_session(
            SessionUpdate(
                user_id=create.user_id,
                session_id=active.session_id,
                expected_generation=active.generation,
                expected_media_grant_revision=active.media_grant_revision,
                expected_speech_backend="client_local",
                control=replacement,
            ),
            now=NOW + timedelta(seconds=2),
        )

    unchanged = repository.get_session(
        user_id=create.user_id,
        session_id=active.session_id,
    )
    assert unchanged.speech_backend == "llm_factory"
    assert unchanged.owner_connection_generation == active.owner_connection_generation
    assert unchanged.control_binding_id == active.control_binding_id


def test_update_suspends_without_cancelling_turns_and_resumes_reconnecting(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    active = _activate_and_sync(repository, create)
    suspended = repository.update_session(
        SessionUpdate(
            user_id=create.user_id,
            session_id=active.session_id,
            expected_generation=active.generation,
            expected_media_grant_revision=active.media_grant_revision,
            control=_control(create),
            foreground_active=False,
            foreground_reason="backgrounded",
            microphone_enabled=False,
        ),
        now=NOW + timedelta(seconds=1),
    )
    assert suspended.state == "suspended"
    assert suspended.foreground_active is False
    assert suspended.microphone_enabled is False

    reconnecting = repository.update_session(
        SessionUpdate(
            user_id=create.user_id,
            session_id=active.session_id,
            expected_generation=active.generation,
            expected_media_grant_revision=active.media_grant_revision,
            control=_control(create),
            foreground_active=True,
            foreground_reason="foreground",
            microphone_enabled=True,
            interaction=True,
        ),
        now=NOW + timedelta(seconds=2),
    )
    assert reconnecting.state == "reconnecting"
    assert reconnecting.foreground_active is True
    assert reconnecting.microphone_enabled is True
    assert reconnecting.last_interaction_at == NOW + timedelta(seconds=2)


def test_context_update_is_revisioned_and_waits_for_worker_application(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    active = _activate_and_sync(repository, create)
    next_chat = str(uuid.uuid4())
    changed = repository.update_session(
        SessionUpdate(
            user_id=create.user_id,
            session_id=active.session_id,
            expected_generation=active.generation,
            expected_media_grant_revision=active.media_grant_revision,
            control=_control(create),
            visible_chat_id=next_chat,
        ),
        now=NOW + timedelta(seconds=1),
    )
    assert changed.visible_chat_id == next_chat
    assert changed.chat_context_revision == active.chat_context_revision + 1
    assert changed.chat_context_synced is False

    with pytest.raises(ContextSyncPending):
        repository.update_session(
            SessionUpdate(
                user_id=create.user_id,
                session_id=active.session_id,
                expected_generation=active.generation,
                expected_media_grant_revision=active.media_grant_revision,
                control=_control(create),
                visible_chat_id=str(uuid.uuid4()),
            ),
            now=NOW + timedelta(seconds=2),
        )


def test_user_end_abandons_unaccepted_turn_and_lease_expiry_releases_media(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Voice end", 1, 1),
    )
    active = _activate_and_sync(repository, create)
    recognizing = asyncio.run(
        repository.bind_worker_recognition(
            start=RecognitionStart(
                session_id=active.session_id,
                generation=active.generation,
                assignment_id=active.worker_assignment_id,
                worker_identity=active.worker_identity,
                client_turn_id=str(uuid.uuid4()),
                media_grant_revision=active.media_grant_revision,
                chat_id=chat_id,
                chat_context_revision=active.chat_context_revision,
            ),
            control_owner_id="replica-a",
            now=NOW,
        )
    ).turn
    ended = repository.end_session(
        user_id=create.user_id,
        session_id=active.session_id,
        expected_generation=active.generation,
        expected_media_grant_revision=active.media_grant_revision,
        control=_control(create),
        reason="user",
        now=NOW + timedelta(seconds=3),
    )
    assert ended.state == "ended"
    assert ended.end_reason == "user"
    assert ended.microphone_enabled is False
    assert ended.foreground_active is False
    abandoned = database.fetch_one(
        "SELECT state, rejection_reason, rejection_retry_policy "
        "FROM voice_turn WHERE turn_id = ?",
        (recognizing.turn_id,),
    )
    assert abandoned == {
        "state": "abandoned",
        "rejection_reason": "stale_session",
        "rejection_retry_policy": "explicit_user_retry",
    }
    database.execute(
        "UPDATE voice_turn SET state = 'recognizing', terminal_kind = NULL, "
        "rejection_reason = NULL, rejection_retry_policy = NULL, "
        "terminal_at = NULL WHERE turn_id = ?",
        (recognizing.turn_id,),
    )
    repaired = repository.reconcile_ended_unaccepted_turns(
        now=NOW + timedelta(seconds=4),
    )
    assert [turn.turn_id for turn in repaired] == [recognizing.turn_id]
    assert repaired[0].state == "abandoned"
    assert repaired[0].rejection_reason == "stale_session"
    assert (
        repository.reconcile_ended_unaccepted_turns(
            now=NOW + timedelta(seconds=5),
        )
        == ()
    )

    expiring_create = _create()
    expiring = repository.create_session(expiring_create, now=NOW).session
    assert repository.expire_session_leases(now=NOW + timedelta(seconds=44)) == ()
    expired = repository.expire_session_leases(now=NOW + timedelta(seconds=46))
    by_id = {item.session_id: item for item in expired}
    assert expiring.session_id in by_id
    assert by_id[expiring.session_id].end_reason == "lease_expired"


def test_identity_shutdown_and_repeated_end_share_one_accepted_work_fence(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Accepted lifecycle", 1, 1),
    )
    active = _activate_and_sync(repository, create)
    accepted = asyncio.run(
        repository.bind_worker_recognition(
            start=RecognitionStart(
                session_id=active.session_id,
                generation=active.generation,
                assignment_id=active.worker_assignment_id,
                worker_identity=active.worker_identity,
                client_turn_id=str(uuid.uuid4()),
                media_grant_revision=active.media_grant_revision,
                chat_id=chat_id,
                chat_context_revision=active.chat_context_revision,
            ),
            control_owner_id="replica-a",
            now=NOW,
        )
    ).turn
    database.execute(
        "UPDATE voice_turn SET state = 'accepted', accepted_at = ?, "
        "detected_language = 'en', spoken_output_policy = 'full_recap', "
        "output_reason = 'ready' WHERE turn_id = ?",
        (NOW, accepted.turn_id),
    )

    ended = repository.end_live_user_session(
        user_id=create.user_id,
        reason="logout",
        now=NOW + timedelta(seconds=2),
    )
    assert ended is not None
    assert ended.end_reason == "logout"
    assert (
        repository.end_live_user_session(
            user_id=create.user_id,
            reason="logout",
            now=NOW + timedelta(seconds=3),
        )
        is None
    )
    retained = database.fetch_one(
        "SELECT state, accepted_at, terminal_at FROM voice_turn WHERE turn_id = ?",
        (accepted.turn_id,),
    )
    assert retained == {
        "state": "accepted",
        "accepted_at": NOW,
        "terminal_at": None,
    }

    explicit_create = _create()
    explicit = repository.create_session(explicit_create, now=NOW).session
    first = repository.end_session(
        user_id=explicit_create.user_id,
        session_id=explicit.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        control=_control(explicit_create),
        reason="user",
        now=NOW + timedelta(seconds=1),
    )
    replay = repository.end_session(
        user_id=explicit_create.user_id,
        session_id=explicit.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        control=_control(explicit_create),
        reason="user",
        now=NOW + timedelta(seconds=2),
    )
    assert replay == first

    shutdown_create = _create()
    shutdown = _activate_and_sync(repository, shutdown_create)
    swept = repository.end_owned_sessions(
        owner_id="replica-a",
        reason="shutdown",
        now=NOW + timedelta(seconds=4),
    )
    assert [item.session_id for item in swept] == [shutdown.session_id]
    assert swept[0].end_reason == "shutdown"
    assert (
        repository.end_owned_sessions(
            owner_id="replica-a",
            reason="shutdown",
            now=NOW + timedelta(seconds=5),
        )
        == ()
    )


@pytest.mark.parametrize(
    ("operation_state", "terminal_code", "retry_after_ms", "expected_state"),
    (
        ("failed", "operation_failed", None, "failed"),
        ("cancelled", "cancelled_by_user", None, "cancelled"),
        ("retryable", "disconnected", 1_000, "failed"),
        ("completed", None, None, "failed"),
    ),
)
def test_ended_session_repairs_terminal_operation_without_exact_result(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
    operation_state: str,
    terminal_code: str | None,
    retry_after_ms: int | None,
    expected_state: str,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Voice terminal repair", 1, 1),
    )
    active = _activate_and_sync(repository, create)
    submitted = _admit_bound_turn(
        repository,
        active,
        text="Complete one ordinary request",
        now=NOW,
    ).turn
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'user', ?, ?)",
        (chat_id, create.user_id, '"Complete one ordinary request"', 2),
    )
    message_id = database.fetch_one(
        "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )["id"]
    operation_id = str(uuid.uuid4())
    connection_generation = str(uuid.uuid4())
    execution_lease_token = str(uuid.uuid4())
    database.execute(
        """
        INSERT INTO operation_record (
            operation_id, operation_kind, admission_class, owner_scope,
            owner_user_id, chat_id, connection_generation,
            request_generation, state, phase_code, execution_generation,
            execution_lease_token, state_revision, accepted_at, updated_at,
            started_at
        ) VALUES (
            ?, 'voice_chat_message', 'voice_interactive', 'user', ?, ?, ?, ?,
            'running', 'running', 1, ?, 2, ?, ?, ?
        )
        """,
        (
            operation_id,
            create.user_id,
            chat_id,
            connection_generation,
            submitted.request_generation,
            execution_lease_token,
            NOW,
            NOW + timedelta(seconds=1),
            NOW,
        ),
    )
    result_commit_id = str(uuid.uuid4())
    database.execute(
        """
        INSERT INTO conversation_commit (
            commit_id, chat_id, owner_user_id, request_generation,
            operation_id, operation_execution_generation,
            base_render_revision, state, started_at, aborted_at,
            publication_role, execution_base_render_revision,
            execution_base_components_sha256,
            execution_base_layouts_sha256
        ) VALUES (
            ?, ?, ?, ?, ?, 1, 0, 'aborted', ?, ?, 'assistant_result',
            0, ?, ?
        )
        """,
        (
            result_commit_id,
            chat_id,
            create.user_id,
            submitted.result_request_generation,
            operation_id,
            NOW,
            NOW + timedelta(seconds=4),
            "0" * 64,
            "1" * 64,
        ),
    )
    accepted = repository.accept_transcript(
        user_id=create.user_id,
        turn_id=submitted.turn_id,
        message_id=message_id,
        accepted_connection_generation=connection_generation,
        acceptance_commit_id=None,
        operation_id=operation_id,
        result_commit_id=result_commit_id,
        now=NOW + timedelta(seconds=2),
    ).turn
    assert accepted.state == "processing"

    repository.end_session(
        user_id=create.user_id,
        session_id=active.session_id,
        expected_generation=active.generation,
        expected_media_grant_revision=active.media_grant_revision,
        control=_control(create),
        reason="media_error",
        now=NOW + timedelta(seconds=3),
    )
    assert (
        repository.reconcile_ended_terminal_operation_turns(
            now=NOW + timedelta(seconds=3, milliseconds=500),
        )
        == ()
    )
    assert (
        repository.get_turn(
            user_id=create.user_id,
            turn_id=accepted.turn_id,
        ).state
        == "processing"
    )
    database.execute(
        """
        UPDATE operation_record
        SET state = ?, terminal_code = ?, retry_after_ms = ?,
            safe_summary = 'Operation failed', execution_lease_token = NULL,
            state_revision = 3, updated_at = ?, terminal_at = ?, purge_after = ?
        WHERE operation_id = ?
        """,
        (
            operation_state,
            terminal_code,
            retry_after_ms,
            NOW + timedelta(seconds=4),
            NOW + timedelta(seconds=4),
            NOW + timedelta(days=1),
            operation_id,
        ),
    )

    repaired = repository.reconcile_ended_terminal_operation_turns(
        now=NOW + timedelta(seconds=5),
    )

    assert [turn.turn_id for turn in repaired] == [accepted.turn_id]
    assert repaired[0].state == expected_state
    assert repaired[0].terminal_kind == expected_state
    assert repaired[0].result_commit_id is None
    assert repaired[0].recap_source == "terminal_status"
    assert repaired[0].sensitivity == "unknown"
    assert repaired[0].is_foreground is False
    assert repaired[0].terminal_at == NOW + timedelta(seconds=5)
    assert (
        repository.reconcile_ended_terminal_operation_turns(
            now=NOW + timedelta(seconds=6),
        )
        == ()
    )


@pytest.mark.parametrize(
    ("operation_state", "terminal_code", "expected_state"),
    (
        ("completed", None, "succeeded"),
        ("failed", "operation_failed", "failed"),
    ),
)
def test_ended_session_repairs_and_preserves_exact_committed_result(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
    operation_state: str,
    terminal_code: str | None,
    expected_state: str,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Voice committed repair", 1, 1),
    )
    active = _activate_and_sync(repository, create)
    submitted = _admit_bound_turn(
        repository,
        active,
        text="Complete one committed request",
        now=NOW,
    ).turn
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'user', ?, ?)",
        (chat_id, create.user_id, '"Complete one committed request"', 2),
    )
    message_id = database.fetch_one(
        "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )["id"]
    operation_id = str(uuid.uuid4())
    connection_generation = str(uuid.uuid4())
    database.execute(
        """
        INSERT INTO operation_record (
            operation_id, operation_kind, admission_class, owner_scope,
            owner_user_id, chat_id, connection_generation,
            request_generation, state, phase_code, execution_generation,
            execution_lease_token, state_revision, accepted_at, updated_at,
            started_at
        ) VALUES (
            ?, 'voice_chat_message', 'voice_interactive', 'user', ?, ?, ?, ?,
            'running', 'running', 1, ?, 2, ?, ?, ?
        )
        """,
        (
            operation_id,
            create.user_id,
            chat_id,
            connection_generation,
            submitted.request_generation,
            str(uuid.uuid4()),
            NOW,
            NOW + timedelta(seconds=1),
            NOW,
        ),
    )
    acceptance_commit_id = str(uuid.uuid4())
    result_commit_id = str(uuid.uuid4())
    database.execute(
        """
        INSERT INTO conversation_commit (
            commit_id, chat_id, owner_user_id, request_generation,
            operation_id, operation_execution_generation,
            base_render_revision, committed_render_revision, state,
            started_at, committed_at, publication_role,
            execution_base_render_revision,
            execution_base_components_sha256,
            execution_base_layouts_sha256
        ) VALUES (
            ?, ?, ?, ?, ?, 1, 0, 1, 'committed', ?, ?, 'user_acceptance',
            0, ?, ?
        )
        """,
        (
            acceptance_commit_id,
            chat_id,
            create.user_id,
            submitted.request_generation,
            operation_id,
            NOW,
            NOW + timedelta(seconds=1),
            "0" * 64,
            "1" * 64,
        ),
    )
    database.execute(
        """
        INSERT INTO conversation_commit (
            commit_id, chat_id, owner_user_id, request_generation,
            operation_id, operation_execution_generation,
            base_render_revision, committed_render_revision, state,
            started_at, committed_at, publication_role, parent_commit_id,
            execution_base_render_revision,
            execution_base_components_sha256,
            execution_base_layouts_sha256
        ) VALUES (
            ?, ?, ?, ?, ?, 1, 1, 2, 'committed', ?, ?, 'assistant_result', ?,
            1, ?, ?
        )
        """,
        (
            result_commit_id,
            chat_id,
            create.user_id,
            submitted.result_request_generation,
            operation_id,
            NOW,
            NOW + timedelta(seconds=2),
            acceptance_commit_id,
            "0" * 64,
            "1" * 64,
        ),
    )
    database.execute(
        """
        UPDATE messages
        SET conversation_commit_id = ?, commit_position = 0,
            committed_render_revision = 1
        WHERE id = ?
        """,
        (acceptance_commit_id, message_id),
    )
    database.execute(
        """
        INSERT INTO messages (
            chat_id, user_id, role, content, timestamp,
            conversation_commit_id, commit_position,
            committed_render_revision
        ) VALUES (?, ?, 'assistant', ?, ?, ?, 0, 2)
        """,
        (
            chat_id,
            create.user_id,
            '[{"type":"text","content":"Result available"}]',
            3,
            result_commit_id,
        ),
    )
    accepted = repository.accept_transcript(
        user_id=create.user_id,
        turn_id=submitted.turn_id,
        message_id=message_id,
        accepted_connection_generation=connection_generation,
        acceptance_commit_id=acceptance_commit_id,
        operation_id=operation_id,
        result_commit_id=result_commit_id,
        now=NOW + timedelta(seconds=2),
    ).turn
    repository.end_session(
        user_id=create.user_id,
        session_id=active.session_id,
        expected_generation=active.generation,
        expected_media_grant_revision=active.media_grant_revision,
        control=_control(create),
        reason="media_error",
        now=NOW + timedelta(seconds=3),
    )
    database.execute(
        """
        UPDATE operation_record
        SET state = ?, terminal_code = ?, safe_summary = 'Terminal outcome',
            execution_lease_token = NULL, state_revision = 3,
            updated_at = ?, terminal_at = ?, purge_after = ?
        WHERE operation_id = ?
        """,
        (
            operation_state,
            terminal_code,
            NOW + timedelta(seconds=4),
            NOW + timedelta(seconds=4),
            NOW + timedelta(days=1),
            operation_id,
        ),
    )

    repaired = repository.reconcile_ended_terminal_operation_turns(
        now=NOW + timedelta(seconds=5),
    )

    assert [turn.turn_id for turn in repaired] == [accepted.turn_id]
    assert repaired[0].state == expected_state
    assert repaired[0].terminal_kind == expected_state
    assert repaired[0].result_commit_id == result_commit_id
    # The ended media generation cannot truthfully claim that committed
    # content was extracted or spoken during crash recovery.
    assert repaired[0].recap_source == "terminal_status"
    assert repaired[0].sensitivity == "unknown"
    assert repaired[0].is_foreground is False


def test_control_lease_maintenance_renews_only_live_unexpired_owner_rows(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    owned_create = _create()
    foreign_create = _create()
    expired_create = _create()
    owned = _activate_and_sync(repository, owned_create, owner_id="replica-a")
    foreign = _activate_and_sync(repository, foreign_create, owner_id="replica-b")
    expired = _activate_and_sync(repository, expired_create, owner_id="replica-a")
    database.execute(
        "UPDATE voice_session SET control_lease_expires_at = ? "
        "WHERE session_id = ?",
        (NOW + timedelta(seconds=1), expired.session_id),
    )

    renewed = repository.renew_owned_control_leases(
        owner_id="replica-a",
        now=NOW + timedelta(seconds=5),
    )

    assert [session.session_id for session in renewed] == [owned.session_id]
    refreshed_owned = repository.get_session(
        user_id=owned_create.user_id,
        session_id=owned.session_id,
    )
    refreshed_foreign = repository.get_session(
        user_id=foreign_create.user_id,
        session_id=foreign.session_id,
    )
    refreshed_expired = repository.get_session(
        user_id=expired_create.user_id,
        session_id=expired.session_id,
    )
    assert refreshed_owned.control_lease_expires_at == NOW + timedelta(seconds=20)
    assert refreshed_foreign.control_lease_expires_at == NOW + timedelta(seconds=15)
    assert refreshed_expired.control_lease_expires_at == NOW + timedelta(seconds=1)

    with pytest.raises(ValueError, match="invalid_control_lease_batch_size"):
        repository.renew_owned_control_leases(
            owner_id="replica-a",
            now=NOW + timedelta(seconds=6),
            batch_size=0,
        )


def test_refresh_id_rotates_once_and_replays_only_the_exact_metadata(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    session = repository.create_session(create, now=NOW).session
    refresh = MediaGrantRefresh(
        user_id=create.user_id,
        session_id=session.session_id,
        refresh_id=str(uuid.uuid4()),
        expected_generation=1,
        expected_media_grant_revision=1,
        participant_identity=f"client-{uuid.uuid4().hex}",
        nonce_hash=os.urandom(32),
        issued_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )

    first = repository.refresh_media_grant(
        refresh,
        now=NOW + timedelta(seconds=1),
    )
    replay = repository.refresh_media_grant(
        refresh,
        now=NOW + timedelta(seconds=2),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert first.session.media_grant_revision == 2
    assert replay.session == first.session
    with pytest.raises(IdempotencyConflict, match="refresh_id_payload_mismatch"):
        repository.refresh_media_grant(
            replace(refresh, participant_identity=f"changed-{uuid.uuid4().hex}"),
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(StaleSessionFence, match="stale_media_grant_revision"):
        repository.refresh_media_grant(
            replace(refresh, refresh_id=str(uuid.uuid4())),
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(IdempotencyConflict, match="refresh_replay_expired"):
        repository.refresh_media_grant(
            refresh,
            now=NOW + timedelta(seconds=32),
        )


def test_same_refresh_race_has_one_rotation_and_one_replay(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    session = repository.create_session(create, now=NOW).session
    refresh = MediaGrantRefresh(
        user_id=create.user_id,
        session_id=session.session_id,
        refresh_id=str(uuid.uuid4()),
        expected_generation=1,
        expected_media_grant_revision=1,
        participant_identity=f"client-{uuid.uuid4().hex}",
        nonce_hash=os.urandom(32),
        issued_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _: repository.refresh_media_grant(
                    refresh,
                    now=NOW + timedelta(seconds=2),
                ),
                range(2),
            )
        )

    assert sorted(result.replayed for result in results) == [False, True]
    assert {result.session.media_grant_revision for result in results} == {2}


def test_grant_metadata_must_be_current_and_worker_grants_are_bounded(
    repository: VoiceSessionRepository,
) -> None:
    future_refresh = MediaGrantRefresh(
        user_id="voice-user-grant-validation",
        session_id=str(uuid.uuid4()),
        refresh_id=str(uuid.uuid4()),
        expected_generation=1,
        expected_media_grant_revision=1,
        participant_identity=f"client-{uuid.uuid4().hex}",
        nonce_hash=os.urandom(32),
        issued_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="invalid_grant_issued_at"):
        repository.refresh_media_grant(future_refresh, now=NOW)
    with pytest.raises(ValueError, match="invalid_media_grant_expiry"):
        repository.refresh_media_grant(
            replace(
                future_refresh,
                issued_at=NOW - timedelta(seconds=2),
                expires_at=NOW,
            ),
            now=NOW,
        )

    worker_args = {
        "user_id": "voice-user-grant-validation",
        "session_id": str(uuid.uuid4()),
        "expected_generation": 1,
        "assignment_id": str(uuid.uuid4()),
        "worker_identity": f"worker-{uuid.uuid4().hex}",
        "now": NOW,
    }
    with pytest.raises(ValueError, match="invalid_worker_grant_issued_at"):
        repository.assign_worker(
            **worker_args,
            issued_at=NOW - timedelta(microseconds=1),
            expires_at=NOW + timedelta(minutes=5),
        )
    with pytest.raises(ValueError, match="invalid_worker_grant_expiry"):
        repository.assign_worker(
            **worker_args,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5, microseconds=1),
        )


def test_worker_assignment_and_control_lease_are_idempotent_and_recoverable(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    session = repository.create_session(create, now=NOW).session
    assignment_id = str(uuid.uuid4())
    worker_identity = f"worker-{uuid.uuid4().hex}"

    assigned = repository.assign_worker(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=1,
        assignment_id=assignment_id,
        worker_identity=worker_identity,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    replay = repository.assign_worker(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=1,
        assignment_id=assignment_id,
        worker_identity=worker_identity,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    assert assigned.replayed is False
    assert replay.replayed is True
    assert assigned.session.worker_identity == worker_identity
    assert assigned.session.worker_assignment_id == assignment_id

    lease_a = asyncio.run(
        repository.claim_control_lease(
            user_id=create.user_id,
            session_id=session.session_id,
            generation=1,
            owner_id="replica-a",
            now=NOW,
        )
    )
    assert lease_a.owner_id == "replica-a"
    with pytest.raises(ClaimUnavailable, match="control_lease_owned"):
        asyncio.run(
            repository.claim_control_lease(
                user_id=create.user_id,
                session_id=session.session_id,
                generation=1,
                owner_id="replica-b",
                now=NOW + timedelta(seconds=1),
            )
        )
    lease_b = asyncio.run(
        repository.claim_control_lease(
            user_id=create.user_id,
            session_id=session.session_id,
            generation=1,
            owner_id="replica-b",
            now=NOW + timedelta(seconds=16),
        )
    )
    assert lease_b.owner_id == "replica-b"
    assert (
        asyncio.run(
            repository.release_control_lease(
                user_id=create.user_id,
                session_id=session.session_id,
                generation=1,
                owner_id="replica-a",
            )
        )
        is False
    )
    assert (
        asyncio.run(
            repository.release_control_lease(
                user_id=create.user_id,
                session_id=session.session_id,
                generation=1,
                owner_id="replica-b",
            )
        )
        is True
    )


def test_context_update_waits_for_current_worker_ack_and_is_revision_fenced(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    session = _activate_and_sync(repository, create)
    next_chat = str(uuid.uuid4())

    pending = repository.request_chat_context_update(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        expected_chat_context_revision=1,
        visible_chat_id=next_chat,
        now=NOW + timedelta(seconds=1),
    )
    assert pending.session.chat_context_revision == 2
    assert pending.session.applied_chat_context_revision == 1
    assert pending.session.chat_context_synced is False
    with pytest.raises(ContextSyncPending):
        repository.request_chat_context_update(
            user_id=create.user_id,
            session_id=session.session_id,
            expected_generation=1,
            expected_media_grant_revision=1,
            expected_chat_context_revision=2,
            visible_chat_id=str(uuid.uuid4()),
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(StaleSessionFence, match="stale_chat_context_revision"):
        repository.apply_chat_context(
            user_id=create.user_id,
            session_id=session.session_id,
            expected_generation=1,
            expected_media_grant_revision=1,
            control_owner_id="replica-a",
            visible_chat_id=next_chat,
            chat_context_revision=1,
            now=NOW + timedelta(seconds=2),
        )

    applied = repository.apply_chat_context(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        control_owner_id="replica-a",
        visible_chat_id=next_chat,
        chat_context_revision=2,
        now=NOW + timedelta(seconds=2),
    )
    replay = repository.apply_chat_context(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=1,
        expected_media_grant_revision=1,
        control_owner_id="replica-a",
        visible_chat_id=next_chat,
        chat_context_revision=2,
        now=NOW + timedelta(seconds=3),
    )
    assert applied.replayed is False
    assert replay.replayed is True
    assert applied.session.chat_context_synced is True


def _binding(session, client_turn_id: str | None = None) -> RecognitionBinding:
    return RecognitionBinding(
        user_id=session.user_id,
        session_id=session.session_id,
        session_generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        client_turn_id=client_turn_id or str(uuid.uuid4()),
        chat_id=session.visible_chat_id,
        chat_context_revision=session.chat_context_revision,
        execution_base_render_revision=0,
        control_owner_id="replica-a",
    )


def test_turn_binding_replay_is_exact_and_foreground_selection_is_atomic(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    create = _create()
    session = _activate_and_sync(repository, create)
    first_request = _binding(session)

    first = repository.bind_recognition_turn(first_request, now=NOW)
    restarted = voice_session_repository(database)
    replay = restarted.bind_recognition_turn(
        first_request, now=NOW + timedelta(seconds=1)
    )
    assert replay.replayed is True
    assert replay.turn == first.turn
    with pytest.raises(IdempotencyConflict, match="client_turn_binding_mismatch"):
        repository.bind_recognition_turn(
            replace(first_request, chat_id=str(uuid.uuid4())),
            now=NOW + timedelta(seconds=1),
        )

    second = repository.bind_recognition_turn(_binding(session), now=NOW).turn
    repository.select_foreground_turn(
        user_id=create.user_id,
        session_id=session.session_id,
        turn_id=first.turn.turn_id,
        expected_generation=1,
        now=NOW,
    )
    selected = repository.select_foreground_turn(
        user_id=create.user_id,
        session_id=session.session_id,
        turn_id=second.turn_id,
        expected_generation=1,
        now=NOW + timedelta(seconds=1),
    )
    assert selected.is_foreground is True
    assert (
        repository.get_turn(
            user_id=create.user_id,
            turn_id=first.turn.turn_id,
        ).is_foreground
        is False
    )
    rows = database.fetch_all(
        "SELECT turn_id FROM voice_turn WHERE session_id = ? AND is_foreground",
        (session.session_id,),
    )
    assert [str(row["turn_id"]) for row in rows] == [second.turn_id]
    with pytest.raises(StaleFence, match="stale_generation"):
        repository.select_foreground_turn(
            user_id=create.user_id,
            session_id=session.session_id,
            turn_id=second.turn_id,
            expected_generation=2,
            now=NOW,
        )


def test_worker_recognition_and_proof_admission_never_persist_content(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Voice proof", 1, 1),
    )
    session = _activate_and_sync(repository, create)
    start = RecognitionStart(
        session_id=session.session_id,
        generation=session.generation,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        client_turn_id=str(uuid.uuid4()),
        media_grant_revision=session.media_grant_revision,
        chat_id=chat_id,
        chat_context_revision=session.chat_context_revision,
    )

    first = asyncio.run(
        repository.bind_worker_recognition(
            start=start,
            control_owner_id="replica-a",
            now=NOW,
        )
    )
    replay = asyncio.run(
        repository.bind_worker_recognition(
            start=start,
            control_owner_id="replica-a",
            now=NOW + timedelta(seconds=1),
        )
    )
    assert replay.replayed is True
    assert replay.turn == first.turn
    proof_binding = TranscriptProofBinding(
        session_id=session.session_id,
        generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        turn_id=first.turn.turn_id,
        client_turn_id=first.turn.client_turn_id,
        submission_id=first.turn.submission_id,
        request_generation=first.turn.request_generation,
        chat_id=first.turn.chat_id,
        chat_context_revision=first.turn.chat_context_revision,
        detected_language="en",
    )
    issued = issue_transcript_proof(
        b"c" * 32,
        proof_binding,
        "  Schedule the follow-up  ",
        now=NOW,
    )
    submission = TranscriptSubmission(
        user_id=create.user_id,
        session_id=session.session_id,
        generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        turn_id=first.turn.turn_id,
        client_turn_id=first.turn.client_turn_id,
        submission_id=first.turn.submission_id,
        request_generation=first.turn.request_generation,
        chat_id=first.turn.chat_id,
        chat_context_revision=first.turn.chat_context_revision,
        source_participant_identity=session.worker_identity,
        detected_language="en",
        text=issued.canonical_text,
        text_digest_sha256=issued.text_digest_sha256,
        transcript_proof=issued.transcript_proof,
        proof_expires_at=issued.proof_expires_at,
    )

    admitted = repository.admit_transcript(
        submission,
        worker_control_secret=b"c" * 32,
        now=NOW + timedelta(seconds=2),
    )
    admitted_replay = repository.admit_transcript(
        submission,
        worker_control_secret=b"c" * 32,
        now=NOW + timedelta(seconds=3),
    )
    assert admitted.canonical_text == "Schedule the follow-up"
    assert admitted.turn.state == "submitting"
    assert admitted_replay.replayed is True
    assert "Schedule the follow-up" not in repr(submission)
    assert "Schedule the follow-up" not in repr(admitted)
    row = database.fetch_one(
        "SELECT * FROM voice_turn WHERE turn_id = ?",
        (first.turn.turn_id,),
    )
    assert not {
        "transcript_text",
        "transcript_proof",
        "text_digest_sha256",
        "proof_expires_at",
    }.intersection(row)

    altered = replace(submission, transcript_proof="f" * 64)
    with pytest.raises(TranscriptSubmissionRejected) as refusal:
        repository.admit_transcript(
            altered,
            worker_control_secret=b"c" * 32,
            now=NOW + timedelta(seconds=4),
        )
    assert (refusal.value.reason, refusal.value.retry_policy) == (
        "invalid_proof",
        "explicit_user_retry",
    )

    rejected = repository.reject_transcript(
        user_id=create.user_id,
        turn_id=first.turn.turn_id,
        reason="invalid_proof",
        retry_policy="explicit_user_retry",
        now=NOW + timedelta(seconds=5),
    )
    rejected_replay = repository.reject_transcript(
        user_id=create.user_id,
        turn_id=first.turn.turn_id,
        reason="invalid_proof",
        retry_policy="explicit_user_retry",
        now=NOW + timedelta(seconds=6),
    )
    assert rejected.turn.state == "abandoned"
    assert rejected_replay.replayed is True
    assert (
        repository.get_turn_by_submission(
            user_id=create.user_id,
            submission_id=first.turn.submission_id,
            request_generation=first.turn.request_generation,
        )
        == rejected.turn
    )


def test_worker_recognition_failure_is_atomic_idempotent_and_assignment_fenced(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Voice ASR failure", 1, 1),
    )
    session = _activate_and_sync(repository, create)
    start = RecognitionStart(
        session_id=session.session_id,
        generation=session.generation,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        client_turn_id=str(uuid.uuid4()),
        media_grant_revision=session.media_grant_revision,
        chat_id=chat_id,
        chat_context_revision=session.chat_context_revision,
    )
    created = asyncio.run(
        repository.bind_worker_recognition(
            start=start,
            control_owner_id="replica-a",
            now=NOW,
        )
    )
    binding = TranscriptTurnBinding.from_turn(
        created.turn,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
    )

    rejected = asyncio.run(
        repository.reject_worker_recognition(
            binding=binding,
            control_owner_id="replica-a",
            now=NOW + timedelta(seconds=1),
        )
    )
    replay = asyncio.run(
        repository.reject_worker_recognition(
            binding=binding,
            control_owner_id="replica-a",
            now=NOW + timedelta(seconds=2),
        )
    )

    assert rejected.turn.state == "abandoned"
    assert rejected.turn.rejection_reason == "malformed_final"
    assert rejected.turn.rejection_retry_policy == "explicit_user_retry"
    assert replay.replayed is True
    row = database.fetch_one(
        "SELECT state, rejection_reason, rejection_retry_policy "
        "FROM voice_turn WHERE turn_id = ?",
        (binding.turn_id,),
    )
    assert row == {
        "state": "abandoned",
        "rejection_reason": "malformed_final",
        "rejection_retry_policy": "explicit_user_retry",
    }

    with pytest.raises(StaleSessionFence, match="stale_worker_assignment"):
        asyncio.run(
            repository.reject_worker_recognition(
                binding=replace(binding, assignment_id=str(uuid.uuid4())),
                control_owner_id="replica-a",
                now=NOW + timedelta(seconds=3),
            )
        )


def test_worker_self_speech_suppression_is_content_free_and_non_retrying(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Voice self speech", 1, 1),
    )
    session = _activate_and_sync(repository, create)
    start = RecognitionStart(
        session_id=session.session_id,
        generation=session.generation,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        client_turn_id=str(uuid.uuid4()),
        media_grant_revision=session.media_grant_revision,
        chat_id=chat_id,
        chat_context_revision=session.chat_context_revision,
    )
    created = asyncio.run(
        repository.bind_worker_recognition(
            start=start,
            control_owner_id="replica-a",
            now=NOW,
        )
    )
    binding = TranscriptTurnBinding.from_turn(
        created.turn,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
    )

    with pytest.raises(ClaimUnavailable, match="control_lease_not_owned"):
        asyncio.run(
            repository.suppress_worker_self_speech(
                binding=binding,
                control_owner_id="replica-b",
                now=NOW + timedelta(milliseconds=100),
            )
        )
    with pytest.raises(StaleSessionFence, match="stale_generation"):
        asyncio.run(
            repository.suppress_worker_self_speech(
                binding=replace(binding, generation=binding.generation + 1),
                control_owner_id="replica-a",
                now=NOW + timedelta(milliseconds=200),
            )
        )
    with pytest.raises(StaleSessionFence, match="recognition_binding_conflict"):
        asyncio.run(
            repository.suppress_worker_self_speech(
                binding=replace(binding, chat_id=str(uuid.uuid4())),
                control_owner_id="replica-a",
                now=NOW + timedelta(milliseconds=300),
            )
        )

    suppressed = asyncio.run(
        repository.suppress_worker_self_speech(
            binding=binding,
            control_owner_id="replica-a",
            now=NOW + timedelta(seconds=1),
        )
    )
    replay = asyncio.run(
        repository.suppress_worker_self_speech(
            binding=binding,
            control_owner_id="replica-a",
            now=NOW + timedelta(seconds=2),
        )
    )

    assert suppressed.turn.state == "abandoned"
    assert suppressed.turn.rejection_reason == "malformed_final"
    assert suppressed.turn.rejection_retry_policy == "none"
    assert replay.replayed is True
    row = database.fetch_one(
        "SELECT state, rejection_reason, rejection_retry_policy "
        "FROM voice_turn WHERE turn_id = ?",
        (binding.turn_id,),
    )
    assert row == {
        "state": "abandoned",
        "rejection_reason": "malformed_final",
        "rejection_retry_policy": "none",
    }

    proof_binding = TranscriptProofBinding(
        session_id=binding.session_id,
        generation=binding.generation,
        media_grant_revision=binding.media_grant_revision,
        assignment_id=binding.assignment_id,
        worker_identity=binding.worker_identity,
        turn_id=binding.turn_id,
        client_turn_id=binding.client_turn_id,
        submission_id=binding.submission_id,
        request_generation=binding.request_generation,
        chat_id=binding.chat_id,
        chat_context_revision=binding.chat_context_revision,
        detected_language="en",
    )
    issued = issue_transcript_proof(
        b"c" * 32,
        proof_binding,
        "Echoed assistant speech",
        now=NOW,
    )
    submission = TranscriptSubmission(
        user_id=create.user_id,
        session_id=binding.session_id,
        generation=binding.generation,
        media_grant_revision=binding.media_grant_revision,
        turn_id=binding.turn_id,
        client_turn_id=binding.client_turn_id,
        submission_id=binding.submission_id,
        request_generation=binding.request_generation,
        chat_id=binding.chat_id,
        chat_context_revision=binding.chat_context_revision,
        source_participant_identity=binding.worker_identity,
        detected_language="en",
        text=issued.canonical_text,
        text_digest_sha256=issued.text_digest_sha256,
        transcript_proof=issued.transcript_proof,
        proof_expires_at=issued.proof_expires_at,
    )
    with pytest.raises(TranscriptSubmissionRejected) as refusal:
        repository.admit_transcript(
            submission,
            worker_control_secret=b"c" * 32,
            now=NOW + timedelta(seconds=2, milliseconds=500),
        )
    assert (refusal.value.reason, refusal.value.retry_policy) == (
        "invalid_binding",
        "none",
    )

    with pytest.raises(IdempotencyConflict, match="transcript_rejection_conflict"):
        asyncio.run(
            repository.reject_worker_recognition(
                binding=binding,
                control_owner_id="replica-a",
                now=NOW + timedelta(seconds=3),
            )
        )
    with pytest.raises(StaleSessionFence, match="stale_worker_assignment"):
        asyncio.run(
            repository.suppress_worker_self_speech(
                binding=replace(binding, assignment_id=str(uuid.uuid4())),
                control_owner_id="replica-a",
                now=NOW + timedelta(seconds=4),
            )
        )


def _admit_bound_turn(
    repository: VoiceSessionRepository,
    session,
    *,
    text: str,
    now: datetime,
):
    start = RecognitionStart(
        session_id=session.session_id,
        generation=session.generation,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        client_turn_id=str(uuid.uuid4()),
        media_grant_revision=session.media_grant_revision,
        chat_id=session.visible_chat_id,
        chat_context_revision=session.chat_context_revision,
    )
    bound = asyncio.run(
        repository.bind_worker_recognition(
            start=start,
            control_owner_id="replica-a",
            now=now,
        )
    ).turn
    proof_binding = TranscriptProofBinding(
        session_id=session.session_id,
        generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        assignment_id=session.worker_assignment_id,
        worker_identity=session.worker_identity,
        turn_id=bound.turn_id,
        client_turn_id=bound.client_turn_id,
        submission_id=bound.submission_id,
        request_generation=bound.request_generation,
        chat_id=bound.chat_id,
        chat_context_revision=bound.chat_context_revision,
        detected_language="en",
    )
    issued = issue_transcript_proof(
        b"d" * 32,
        proof_binding,
        text,
        now=now,
    )
    return repository.admit_transcript(
        TranscriptSubmission(
            user_id=session.user_id,
            session_id=session.session_id,
            generation=session.generation,
            media_grant_revision=session.media_grant_revision,
            turn_id=bound.turn_id,
            client_turn_id=bound.client_turn_id,
            submission_id=bound.submission_id,
            request_generation=bound.request_generation,
            chat_id=bound.chat_id,
            chat_context_revision=bound.chat_context_revision,
            source_participant_identity=session.worker_identity,
            detected_language="en",
            text=issued.canonical_text,
            text_digest_sha256=issued.text_digest_sha256,
            transcript_proof=issued.transcript_proof,
            proof_expires_at=issued.proof_expires_at,
        ),
        worker_control_secret=b"d" * 32,
        now=now + timedelta(seconds=1),
    )


def test_preacceptance_guidance_claim_is_exact_once_and_revision_fenced(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Rejected voice", 1, 1),
    )
    session = _activate_and_sync(repository, create)
    submitted = _admit_bound_turn(
        repository,
        session,
        text="Do not persist this text",
        now=NOW,
    ).turn
    rejected = repository.reject_transcript(
        user_id=create.user_id,
        turn_id=submitted.turn_id,
        reason="permission_denied",
        retry_policy="none",
        now=NOW + timedelta(seconds=2),
    ).turn
    request = AnnouncementClaimRequest(
        session_id=session.session_id,
        turn_id=rejected.turn_id,
        generation=session.generation,
        claim_id=str(uuid.uuid4()),
        kind="waiting",
        quantum_role="single",
        expected_sequence=0,
        expected_result_reserved_samples=0,
        expected_phrase_key="llm_setup_needed",
        expected_media_grant_revision=session.media_grant_revision,
        authorized_preacceptance_rejection_reason="permission_denied",
    )

    mutation = asyncio.run(
        repository.claim_announcement(
            user_id=create.user_id,
            request=request,
            now=NOW + timedelta(seconds=3),
        )
    )

    assert mutation.claim.kind == "waiting"
    assert mutation.claim.phrase_key == "llm_setup_needed"
    assert rejected.message_id is None
    assert rejected.accepted_at is None
    assert asyncio.run(
        repository.complete_announcement(
            user_id=create.user_id,
            session_id=session.session_id,
            turn_id=rejected.turn_id,
            generation=session.generation,
            claim_id=request.claim_id,
        )
    )
    with pytest.raises(
        ClaimUnavailable,
        match="preacceptance_refusal_already_announced",
    ):
        asyncio.run(
            repository.claim_announcement(
                user_id=create.user_id,
                request=replace(
                    request,
                    claim_id=str(uuid.uuid4()),
                    expected_sequence=1,
                ),
                now=NOW + timedelta(seconds=4),
            )
        )
    with pytest.raises(StaleSessionFence, match="stale_media_grant_revision"):
        asyncio.run(
            repository.claim_announcement(
                user_id=create.user_id,
                request=replace(
                    request,
                    claim_id=str(uuid.uuid4()),
                    expected_media_grant_revision=(
                        session.media_grant_revision + 1
                    ),
                ),
                now=NOW + timedelta(seconds=5),
            )
        )


def test_acceptance_foregrounds_without_cancelling_and_claims_speech_atomically(
    repository: VoiceSessionRepository,
    database: VoicePlaneTestRuntime,
) -> None:
    chat_id = str(uuid.uuid4())
    create = _create(chat_id=uuid.UUID(chat_id))
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, create.user_id, "Voice lifecycle", 1, 1),
    )
    session = _activate_and_sync(repository, create)
    first = _admit_bound_turn(
        repository,
        session,
        text="Build the report",
        now=NOW,
    )
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'user', ?, ?)",
        (chat_id, create.user_id, '"Build the report"', 2),
    )
    first_message = database.fetch_one(
        "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )["id"]
    connection_generation = str(uuid.uuid4())
    accepted = repository.accept_transcript(
        user_id=create.user_id,
        turn_id=first.turn.turn_id,
        message_id=first_message,
        accepted_connection_generation=connection_generation,
        acceptance_commit_id=None,
        operation_id=None,
        now=NOW + timedelta(seconds=2),
    )
    replay = repository.accept_transcript(
        user_id=create.user_id,
        turn_id=first.turn.turn_id,
        message_id=first_message,
        accepted_connection_generation=connection_generation,
        acceptance_commit_id=None,
        operation_id=None,
        now=NOW + timedelta(seconds=3),
    )
    assert accepted.turn.state == "processing"
    assert accepted.turn.is_foreground is True
    assert accepted.turn.message_id == first_message
    assert replay.replayed is True

    second = _admit_bound_turn(
        repository,
        session,
        text="Also check the totals",
        now=NOW + timedelta(seconds=4),
    )
    database.execute(
        "INSERT INTO messages (chat_id, user_id, role, content, timestamp) "
        "VALUES (?, ?, 'user', ?, ?)",
        (chat_id, create.user_id, '"Also check the totals"', 3),
    )
    second_message = database.fetch_one(
        "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )["id"]
    second_accepted = repository.accept_transcript(
        user_id=create.user_id,
        turn_id=second.turn.turn_id,
        message_id=second_message,
        accepted_connection_generation=str(uuid.uuid4()),
        acceptance_commit_id=None,
        operation_id=None,
        now=NOW + timedelta(seconds=6),
    ).turn
    first_after = repository.get_turn(
        user_id=create.user_id,
        turn_id=accepted.turn.turn_id,
    )
    assert second_accepted.is_foreground is True
    assert first_after.is_foreground is False
    assert first_after.state == "processing"

    request = AnnouncementClaimRequest(
        session_id=session.session_id,
        turn_id=second_accepted.turn_id,
        generation=session.generation,
        claim_id=str(uuid.uuid4()),
        kind="acknowledgement",
        quantum_role="single",
        expected_sequence=0,
        expected_result_reserved_samples=0,
    )
    claim = asyncio.run(
        repository.claim_announcement(
            user_id=create.user_id,
            request=request,
            now=NOW + timedelta(seconds=7),
        )
    )
    assert claim.claim.sequence == 1
    assert claim.claim.phrase_key in {
        "on_it",
        "working_on_it",
        "ill_get_started",
    }
    with pytest.raises(ClaimUnavailable, match="announcement_claim_owned"):
        asyncio.run(
            repository.claim_announcement(
                user_id=create.user_id,
                request=replace(
                    request,
                    claim_id=str(uuid.uuid4()),
                    expected_sequence=1,
                ),
                now=NOW + timedelta(seconds=8),
            )
        )
    assert asyncio.run(
        repository.complete_announcement(
            user_id=create.user_id,
            session_id=session.session_id,
            turn_id=second_accepted.turn_id,
            generation=session.generation,
            claim_id=request.claim_id,
        )
    )
    assert not asyncio.run(
        repository.complete_announcement(
            user_id=create.user_id,
            session_id=session.session_id,
            turn_id=second_accepted.turn_id,
            generation=session.generation,
            claim_id=request.claim_id,
        )
    )

    playout_started_at = NOW + timedelta(seconds=8, milliseconds=250)
    observed = repository.record_client_playout(
        user_id=create.user_id,
        device_id=create.device_id,
        connection_generation=create.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        announcement_id=claim.claim.announcement_id,
        announcement_sequence=claim.claim.sequence,
        turn_id=second_accepted.turn_id,
        kind=claim.claim.kind,
        quantum_role=claim.claim.quantum_role,
        quantum_index=claim.claim.quantum_index,
        result_reserved_samples_after=(
            claim.claim.result_reserved_samples_after
        ),
        phase="started",
        client_sequence=1,
        received_at=playout_started_at,
    )
    assert observed is not None
    with pytest.raises(
        VoiceSessionRepositoryError,
        match="owner_connection_mismatch",
    ):
        repository.record_client_playout(
            user_id=create.user_id,
            device_id=str(uuid.uuid4()),
            connection_generation=create.owner_connection_generation,
            session_id=session.session_id,
            generation=session.generation,
            media_grant_revision=session.media_grant_revision,
            announcement_id=claim.claim.announcement_id,
            announcement_sequence=claim.claim.sequence,
            turn_id=second_accepted.turn_id,
            kind=claim.claim.kind,
            quantum_role=claim.claim.quantum_role,
            quantum_index=claim.claim.quantum_index,
            result_reserved_samples_after=None,
            phase="finished",
            client_sequence=2,
            received_at=NOW + timedelta(seconds=8, milliseconds=500),
        )
    playout_finished_at = NOW + timedelta(seconds=8, milliseconds=750)
    repository.record_client_playout(
        user_id=create.user_id,
        device_id=create.device_id,
        connection_generation=create.owner_connection_generation,
        session_id=session.session_id,
        generation=session.generation,
        media_grant_revision=session.media_grant_revision,
        announcement_id=claim.claim.announcement_id,
        announcement_sequence=claim.claim.sequence,
        turn_id=second_accepted.turn_id,
        kind=claim.claim.kind,
        quantum_role=claim.claim.quantum_role,
        quantum_index=claim.claim.quantum_index,
        result_reserved_samples_after=None,
        phase="finished",
        client_sequence=2,
        received_at=playout_finished_at,
    )
    playout_row = database.fetch_one(
        "SELECT last_client_playout_started_at, "
        "last_client_playout_finished_at, last_client_playout_sequence "
        "FROM voice_turn WHERE turn_id = ?",
        (second_accepted.turn_id,),
    )
    assert playout_row["last_client_playout_started_at"] == (
        playout_started_at
    )
    assert playout_row["last_client_playout_finished_at"] == (
        playout_finished_at
    )
    assert playout_row["last_client_playout_sequence"] == 2
    with pytest.raises(
        StaleFence,
        match="client_sequence_out_of_order",
    ):
        repository.record_client_playout(
            user_id=create.user_id,
            device_id=create.device_id,
            connection_generation=create.owner_connection_generation,
            session_id=session.session_id,
            generation=session.generation,
            media_grant_revision=session.media_grant_revision,
            announcement_id=claim.claim.announcement_id,
            announcement_sequence=claim.claim.sequence,
            turn_id=second_accepted.turn_id,
            kind=claim.claim.kind,
            quantum_role=claim.claim.quantum_role,
            quantum_index=claim.claim.quantum_index,
            result_reserved_samples_after=None,
            phase="finished",
            client_sequence=2,
            received_at=NOW + timedelta(seconds=8, milliseconds=900),
        )

    terminal = repository.terminalize_turn(
        user_id=create.user_id,
        turn_id=second_accepted.turn_id,
        terminal_kind="succeeded",
        result_commit_id=None,
        recap_source="committed_visible_fallback",
        sensitivity="non_sensitive",
        now=NOW + timedelta(seconds=9),
    )
    assert terminal.turn.state == "succeeded"
    assert terminal.turn.is_foreground is False
    with pytest.raises(ClaimUnavailable, match="announcement_terminal"):
        asyncio.run(
            repository.claim_announcement(
                user_id=create.user_id,
                request=replace(
                    request,
                    claim_id=str(uuid.uuid4()),
                    expected_sequence=1,
                ),
                now=NOW + timedelta(seconds=10),
            )
        )


def test_true_idle_uses_server_receipt_time_and_expires_at_five_minutes(
    repository: VoiceSessionRepository,
) -> None:
    create = _create()
    session = _activate_and_sync(repository, create)

    idle = repository.set_true_idle(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=1,
        listening=True,
        user_input_gate=False,
        now=NOW,
    )
    assert idle.idle_started_at == NOW
    assert idle.idle_expires_at == NOW + timedelta(minutes=5)

    interaction_time = NOW + timedelta(seconds=100)
    interacted = repository.record_interaction(
        user_id=create.user_id,
        session_id=session.session_id,
        expected_generation=1,
        now=interaction_time,
    )
    assert interacted.last_interaction_at == interaction_time
    assert interacted.idle_started_at == interaction_time
    assert (
        repository.expire_true_idle(
            now=interaction_time + timedelta(minutes=5) - timedelta(microseconds=1)
        )
        == ()
    )
    expired = repository.expire_true_idle(now=interaction_time + timedelta(minutes=5))
    assert [item.session_id for item in expired] == [session.session_id]
    assert expired[0].state == "ended"
    assert expired[0].end_reason == "idle"


def test_active_turn_or_user_gate_prevents_true_idle(
    repository: VoiceSessionRepository,
) -> None:
    first_create = _create()
    first = _activate_and_sync(repository, first_create)
    repository.bind_recognition_turn(_binding(first), now=NOW)
    blocked_by_turn = repository.set_true_idle(
        user_id=first_create.user_id,
        session_id=first.session_id,
        expected_generation=1,
        listening=True,
        user_input_gate=False,
        now=NOW,
    )
    assert blocked_by_turn.idle_started_at is None

    second_create = _create()
    second = _activate_and_sync(repository, second_create)
    blocked_by_gate = repository.set_true_idle(
        user_id=second_create.user_id,
        session_id=second.session_id,
        expected_generation=1,
        listening=True,
        user_input_gate=True,
        now=NOW,
    )
    assert blocked_by_gate.idle_started_at is None


def test_constructor_and_input_validation_fail_before_database_use(
    database: VoicePlaneTestRuntime,
) -> None:
    with pytest.raises(TypeError, match="plane_runtime"):
        VoiceSessionRepository(plane_runtime=object())
    with pytest.raises(ValueError, match="invalid_activation_id"):
        replace(_create(), activation_id=str(uuid.uuid1()))
    with pytest.raises(ValueError, match="invalid_nonce_hash"):
        replace(_create(), media_grant_nonce_hash=b"short")
    with pytest.raises(ValueError, match="invalid_media_grant_expiry"):
        request = _create()
        replace(request, media_grant_expires_at=request.media_grant_issued_at)


def test_user_delete_of_lease_reaped_session_is_idempotent_066(
    repository: VoiceSessionRepository,
) -> None:
    """Bug B (2026-08-03): a client that stalls stops renewing, the reaper
    ends its session, and the user's later DELETE must succeed instead of
    raising session_already_ended (which mapped to a 503 on the wire)."""

    create = _create()
    active = _activate_and_sync(repository, create)
    reaped = {
        item.session_id: item
        for item in repository.expire_session_leases(now=NOW + timedelta(seconds=46))
    }
    assert active.session_id in reaped
    assert reaped[active.session_id].end_reason == "lease_expired"

    # Same device, rotated binding (bindings rotate every ~2 minutes, so the
    # DELETE rarely carries the binding stored on the reaped row).
    rotated = _control(
        create,
        binding_id=str(uuid.uuid4()),
        connection_generation=str(uuid.uuid4()),
    )
    ended = repository.end_session(
        user_id=create.user_id,
        session_id=active.session_id,
        expected_generation=active.generation,
        expected_media_grant_revision=active.media_grant_revision,
        control=rotated,
        reason="user",
        now=NOW + timedelta(seconds=50),
    )
    assert ended.state == "ended"
    assert ended.end_reason == "lease_expired"

    # A foreign device is still refused, and non-user internal reasons keep
    # their exact-replay semantics.
    with pytest.raises(VoiceSessionRepositoryError, match="binding_scope_mismatch"):
        repository.end_session(
            user_id=create.user_id,
            session_id=active.session_id,
            expected_generation=active.generation,
            expected_media_grant_revision=active.media_grant_revision,
            control=_control(create, device_id=str(uuid.uuid4())),
            reason="user",
            now=NOW + timedelta(seconds=51),
        )
    with pytest.raises(StaleSessionFence, match="session_already_ended"):
        repository.end_session(
            user_id=create.user_id,
            session_id=active.session_id,
            expected_generation=active.generation,
            expected_media_grant_revision=active.media_grant_revision,
            control=_control(create),
            reason="media_error",
            now=NOW + timedelta(seconds=52),
        )
