"""Worker-pool and coordinator-fence tests for conversational voice (065)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from orchestrator.voice_coordinator import (
    FIXED_VOICE_PROFILE,
    AnnouncementClaimRequest,
    AnnouncementState,
    AnnouncementStateAdapter,
    CapacityUnavailable,
    ClaimUnavailable,
    ControlLeaseAdapter,
    ControlLeaseState,
    ControlProtocolError,
    ControlSendError,
    CoordinatorClock,
    MonotonicScheduler,
    PhraseBook,
    RegistrationError,
    SessionBindRequest,
    StaleFence,
    VoiceCoordinator,
    VoiceCoordinatorError,
    WorkerPool,
    WorkerPoolPolicy,
    deterministic_uuid4,
)

NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
SESSION_1 = "10000000-0000-4000-8000-000000000001"
SESSION_2 = "10000000-0000-4000-8000-000000000002"
SESSION_3 = "10000000-0000-4000-8000-000000000003"
CHAT = "20000000-0000-4000-8000-000000000001"
TURN = "30000000-0000-4000-8000-000000000001"
CLAIM_1 = "40000000-0000-4000-8000-000000000001"
CLAIM_2 = "40000000-0000-4000-8000-000000000002"
CLOSURE = "a" * 64


class FakeClock:
    def __init__(self) -> None:
        self.utc = NOW
        self.mono = 100.0

    def utcnow(self) -> datetime:
        return self.utc

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def send(self, payload: str) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class FakeCoordinatorRepository:
    def __init__(self) -> None:
        self.control = ControlLeaseState(generation=3)
        self.announcement = AnnouncementState(generation=3)
        self.control_adapter = ControlLeaseAdapter(ttl_seconds=10)
        self.announcement_adapter = AnnouncementStateAdapter(
            _phrase_book(), claim_ttl_seconds=5
        )
        self.calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None
        self.recognition_turns: list[SimpleNamespace] = []

    async def claim_control_lease(self, **values):
        if self.failure:
            raise self.failure
        self.calls.append(("claim_control", values["user_id"]))
        self.control = self.control_adapter.claim(
            self.control,
            generation=values["generation"],
            owner_id=values["owner_id"],
            now=values["now"],
        )
        return self.control

    async def release_control_lease(self, **values):
        if self.failure:
            raise self.failure
        before = self.control
        self.control = self.control_adapter.release(
            self.control,
            generation=values["generation"],
            owner_id=values["owner_id"],
        )
        return before != self.control

    async def claim_announcement(self, **values):
        if self.failure:
            raise self.failure
        self.calls.append(("claim_announcement", values["user_id"]))
        mutation = self.announcement_adapter.claim(
            self.announcement, values["request"], now=values["now"]
        )
        self.announcement = mutation.state
        return mutation

    async def complete_announcement(self, **values):
        if self.failure:
            raise self.failure
        before = self.announcement
        self.announcement = self.announcement_adapter.complete(
            self.announcement,
            generation=values["generation"],
            claim_id=values["claim_id"],
        )
        return before != self.announcement

    async def bind_worker_recognition(self, **values):
        if self.failure:
            raise self.failure
        start = values["start"]
        sequence = len(self.recognition_turns) + 1
        turn = SimpleNamespace(
            session_id=start.session_id,
            session_generation=start.generation,
            media_grant_revision=start.media_grant_revision,
            turn_id=deterministic_uuid4(
                "test-recognition-turn",
                start.client_turn_id,
                str(sequence),
            ),
            client_turn_id=start.client_turn_id,
            submission_id=deterministic_uuid4(
                "test-recognition-submission",
                start.client_turn_id,
                str(sequence),
            ),
            request_generation=deterministic_uuid4(
                "test-recognition-request",
                start.client_turn_id,
                str(sequence),
            ),
            chat_id=start.chat_id,
            chat_context_revision=start.chat_context_revision,
        )
        self.recognition_turns.append(turn)
        return SimpleNamespace(turn=turn, replayed=False)

    async def reject_worker_recognition(self, **values):
        if self.failure:
            raise self.failure
        binding = values["binding"]
        turn = next(
            item
            for item in self.recognition_turns
            if item.client_turn_id == binding.client_turn_id
        )
        replayed = getattr(turn, "state", None) == "abandoned"
        turn.state = "abandoned"
        turn.rejection_reason = "malformed_final"
        turn.rejection_retry_policy = "explicit_user_retry"
        return SimpleNamespace(turn=turn, replayed=replayed)

    async def suppress_worker_self_speech(self, **values):
        if self.failure:
            raise self.failure
        binding = values["binding"]
        turn = next(
            item
            for item in self.recognition_turns
            if item.client_turn_id == binding.client_turn_id
        )
        replayed = getattr(turn, "state", None) == "abandoned"
        turn.state = "abandoned"
        turn.rejection_reason = "malformed_final"
        turn.rejection_retry_policy = "none"
        return SimpleNamespace(turn=turn, replayed=replayed)


def _policy(**changes: object) -> WorkerPoolPolicy:
    values: dict[str, object] = {
        "runtime_closure_sha256": CLOSURE,
        "max_workers": 4,
        "max_sessions_per_worker": 4,
        "max_total_sessions": 8,
        "heartbeat_interval_seconds": 5,
        "connection_lease_seconds": 16,
        "send_timeout_seconds": 0.05,
        "max_receive_frames": 120,
        "receive_window_seconds": 1.0,
        "allow_insecure_livekit_url": True,
    }
    values.update(changes)
    return WorkerPoolPolicy(**values)


def _registration(identity: str, *, capacity: int = 2, **changes: object) -> dict:
    frame: dict[str, object] = {
        "type": "worker_register",
        "schema_version": "1",
        "message_id": deterministic_uuid4("registration", identity),
        "sequence": 0,
        "sent_at": "2026-07-31T18:00:00Z",
        "worker_identity": identity,
        "max_sessions": capacity,
        "runtime_closure_sha256": CLOSURE,
        "profile": dict(FIXED_VOICE_PROFILE),
    }
    frame.update(changes)
    return frame


def _request(session_id: str, *, generation: int = 1, worker_revision: int = 1):
    return SessionBindRequest(
        session_id=session_id,
        generation=generation,
        room_name=f"room-{session_id[-1]}",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=worker_revision,
        client_participant_identity=f"client-{session_id[-1]}",
        visible_chat_id=CHAT,
        chat_context_revision=1,
    )


def _grant(worker: str, request: SessionBindRequest) -> dict[str, object]:
    return {
        "revision": request.worker_rtc_grant_revision,
        "livekit_url": "ws://livekit:7880",
        "join_token": "secret-worker-room-token-" + "x" * 40,
        "issued_at": "2026-07-31T18:00:00Z",
        "expires_at": "2026-07-31T18:05:00Z",
        "room_name": request.room_name,
        "worker_identity": worker,
    }


@pytest.mark.asyncio
async def test_registration_requires_authenticated_exact_closure_and_profile() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    socket = FakeSocket()

    receipt = await pool.register_worker(
        _registration("voice-worker-a"),
        socket,
        authenticated_identity="voice-worker-a",
    )

    registered = json.loads(socket.sent[0])
    assert registered == {
        "accepted_max_sessions": 2,
        "connection_id": receipt.connection_id,
        "heartbeat_interval_seconds": 5,
        "message_id": registered["message_id"],
        "registered_at": "2026-07-31T18:00:00Z",
        "schema_version": "1",
        "sent_at": "2026-07-31T18:00:00Z",
        "sequence": 0,
        "type": "worker_registered",
        "worker_identity": "voice-worker-a",
    }
    assert UUID(receipt.connection_id).version == 4
    assert pool.readiness().reason == "ready"

    invalid = [
        (_registration("voice-worker-a"), "voice-worker-b", "identity_mismatch"),
        (
            _registration("voice-worker-a", runtime_closure_sha256="b" * 64),
            "voice-worker-a",
            "closure_mismatch",
        ),
        (
            _registration(
                "voice-worker-a",
                profile={**FIXED_VOICE_PROFILE, "voice": "af_alloy"},
            ),
            "voice-worker-a",
            "profile_mismatch",
        ),
        (
            _registration("voice-worker-a", unexpected=True),
            "voice-worker-a",
            "invalid_registration_fields",
        ),
    ]
    for frame, identity, code in invalid:
        with pytest.raises(RegistrationError, match=code):
            await pool.register_worker(
                frame, FakeSocket(), authenticated_identity=identity
            )


@pytest.mark.asyncio
async def test_duplicate_identity_fences_old_connection_and_assignments() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    first = FakeSocket()
    old = await pool.register_worker(
        _registration("voice-worker-a"), first, authenticated_identity="voice-worker-a"
    )
    reservation = await pool.reserve_session(_request(SESSION_1))

    second = FakeSocket()
    new = await pool.register_worker(
        _registration("voice-worker-a"), second, authenticated_identity="voice-worker-a"
    )

    assert new.connection_id != old.connection_id
    assert new.fenced_assignments == (reservation.assignment_id,)
    assert first.closed == [(4001, "connection_replaced")]
    with pytest.raises(StaleFence, match="stale_connection"):
        await pool.receive_worker_frame(
            old.connection_id,
            json.dumps(_worker_ready(reservation, sequence=0)),
        )


@pytest.mark.asyncio
async def test_selection_is_deterministic_capacity_aware_and_atomic() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-b", capacity=2),
        FakeSocket(),
        authenticated_identity="worker-b",
    )
    await pool.register_worker(
        _registration("worker-a", capacity=1),
        FakeSocket(),
        authenticated_identity="worker-a",
    )

    reservations = await asyncio.gather(
        pool.reserve_session(_request(SESSION_1)),
        pool.reserve_session(_request(SESSION_2)),
        pool.reserve_session(_request(SESSION_3)),
    )

    assert [item.worker_identity for item in reservations] == [
        "worker-a",
        "worker-b",
        "worker-b",
    ]
    assert len({item.assignment_id for item in reservations}) == 3
    same = await pool.reserve_session(_request(SESSION_1))
    assert same == reservations[0]
    with pytest.raises(CapacityUnavailable, match="worker_capacity_exhausted"):
        await pool.reserve_session(_request("10000000-0000-4000-8000-000000000004"))


def _worker_ready(reservation, *, sequence: int, revision: int = 1) -> dict:
    return {
        "type": "worker_ready",
        "schema_version": "1",
        "message_id": deterministic_uuid4(
            "ready", reservation.assignment_id, str(sequence)
        ),
        "session_id": reservation.session_id,
        "generation": reservation.generation,
        "sequence": sequence,
        "sent_at": "2026-07-31T18:00:01Z",
        "assignment_id": reservation.assignment_id,
        "worker_identity": reservation.worker_identity,
        "worker_rtc_grant_revision": revision,
        "profile_ready": True,
    }


@pytest.mark.asyncio
async def test_session_bind_injects_validated_grant_and_fences_sequences() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)

    frame = await pool.deliver_session_bind(
        reservation, request, _grant("worker-a", request)
    )

    assert frame["sequence"] == 0
    assert frame["worker_identity"] == "worker-a"
    assert frame["worker_rtc_grant"]["join_token"].startswith("secret-worker")
    assert json.loads(socket.sent[-1]) == frame
    assert "secret-worker-room-token" not in repr(reservation)

    accepted = await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0)),
    )
    assert accepted["type"] == "worker_ready"
    assert pool.assignment_snapshot(SESSION_1).ready is True

    heartbeat = {
        "type": "heartbeat",
        "schema_version": "1",
        "message_id": deterministic_uuid4("heartbeat", SESSION_1),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:02Z",
        "media_state": "ready",
    }
    await pool.receive_worker_frame(reservation.connection_id, json.dumps(heartbeat))
    assert pool.assignment_snapshot(SESSION_1).media_state == "ready"
    with pytest.raises(ControlProtocolError, match="sequence_out_of_order"):
        await pool.receive_worker_frame(
            reservation.connection_id, json.dumps(heartbeat)
        )
    with pytest.raises(ControlProtocolError, match="wrong_direction"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps({**heartbeat, "type": "speak", "sequence": 2}),
        )


@pytest.mark.asyncio
async def test_bounded_ready_and_media_applied_waits_require_exact_ordered_acks() -> (
    None
):
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), FakeSocket(), authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)
    waiting = asyncio.create_task(
        pool.await_session_ready(
            session_id=SESSION_1,
            generation=1,
            visible_chat_id=CHAT,
            chat_context_revision=1,
            timeout_seconds=0.2,
        )
    )
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0)),
    )
    await asyncio.sleep(0)
    assert not waiting.done()

    context_applied = {
        "type": "session_context_applied",
        "schema_version": "1",
        "message_id": deterministic_uuid4("context-applied", SESSION_1),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:01Z",
        "media_grant_revision": 1,
        "visible_chat_id": CHAT,
        "chat_context_revision": 1,
        "occurred_at": "2026-07-31T18:00:01Z",
    }
    with pytest.raises(StaleFence, match="stale_chat_context_revision"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps({**context_applied, "visible_chat_id": SESSION_2}),
        )
    await pool.receive_worker_frame(
        reservation.connection_id, json.dumps(context_applied)
    )
    ready = await waiting
    assert ready.ready is True
    assert ready.applied_visible_chat_id == CHAT
    assert ready.applied_chat_context_revision == 1

    with pytest.raises(ControlProtocolError, match="invalid_context_update_fields"):
        await pool.send_session_command(
            reservation,
            "session_context_update",
            {"visible_chat_id": SESSION_2, "chat_context_revision": 2},
        )
    await pool.send_session_command(
        reservation,
        "session_context_update",
        {
            "media_grant_revision": 1,
            "visible_chat_id": SESSION_2,
            "chat_context_revision": 2,
        },
    )
    context_wait = asyncio.create_task(
        pool.await_session_ready(
            session_id=SESSION_1,
            generation=1,
            visible_chat_id=SESSION_2,
            chat_context_revision=2,
            timeout_seconds=0.2,
        )
    )
    updated_context = {
        **context_applied,
        "message_id": deterministic_uuid4("context-applied", SESSION_2),
        "sequence": 2,
        "visible_chat_id": SESSION_2,
        "chat_context_revision": 2,
    }
    await pool.receive_worker_frame(
        reservation.connection_id, json.dumps(updated_context)
    )
    assert (await context_wait).applied_chat_context_revision == 2

    refresh_id = deterministic_uuid4("refresh", SESSION_1)
    rotated_identity = "client-rotated"
    await pool.send_session_command(
        reservation,
        "media_grant_rotated",
        {
            "refresh_id": refresh_id,
            "previous_media_grant_revision": 1,
            "media_grant_revision": 2,
            "client_participant_identity": rotated_identity,
            "transport": "livekit",
            "grant_expires_at": "2026-07-31T18:05:00Z",
        },
    )
    media_wait = asyncio.create_task(
        pool.await_media_grant_applied(
            session_id=SESSION_1,
            generation=1,
            refresh_id=refresh_id,
            media_grant_revision=2,
            client_participant_identity=rotated_identity,
            timeout_seconds=0.2,
        )
    )
    applied = {
        "type": "media_grant_applied",
        "schema_version": "1",
        "message_id": deterministic_uuid4("media-applied", SESSION_1),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 3,
        "sent_at": "2026-07-31T18:00:02Z",
        "refresh_id": refresh_id,
        "media_grant_revision": 2,
        "client_participant_identity": rotated_identity,
        "occurred_at": "2026-07-31T18:00:02Z",
    }
    await pool.receive_worker_frame(reservation.connection_id, json.dumps(applied))
    media_ready = await media_wait
    assert media_ready.applied_media_refresh_id == refresh_id
    assert media_ready.applied_media_grant_revision == 2

    # An exact REST retry re-drives the pending worker command without
    # advancing the durable revision or clearing the prior acknowledgement.
    await pool.send_session_command(
        reservation,
        "media_grant_rotated",
        {
            "refresh_id": refresh_id,
            "previous_media_grant_revision": 1,
            "media_grant_revision": 2,
            "client_participant_identity": rotated_identity,
            "transport": "livekit",
            "grant_expires_at": "2026-07-31T18:05:00Z",
        },
    )
    replay_ready = await pool.await_media_grant_applied(
        session_id=SESSION_1,
        generation=1,
        refresh_id=refresh_id,
        media_grant_revision=2,
        client_participant_identity=rotated_identity,
        timeout_seconds=0.2,
    )
    assert replay_ready.applied_media_refresh_id == refresh_id
    with pytest.raises(StaleFence, match="stale_media_grant_revision"):
        await pool.send_session_command(
            reservation,
            "media_grant_rotated",
            {
                "refresh_id": deterministic_uuid4("other-refresh", SESSION_1),
                "previous_media_grant_revision": 1,
                "media_grant_revision": 2,
                "client_participant_identity": rotated_identity,
                "transport": "livekit",
                "grant_expires_at": "2026-07-31T18:05:00Z",
            },
        )

    with pytest.raises(CapacityUnavailable, match="worker_session_ready_timeout"):
        await pool.await_session_ready(
            session_id=SESSION_1,
            generation=1,
            visible_chat_id=CHAT,
            chat_context_revision=1,
            timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_recognition_is_durably_bound_before_correlated_dispositions() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"),
        socket,
        authenticated_identity="worker-a",
    )
    reservation = await pool.reserve_session(_request(SESSION_1))
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0)),
    )
    context_applied = {
        "type": "session_context_applied",
        "schema_version": "1",
        "message_id": deterministic_uuid4("context-applied", SESSION_1),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:01Z",
        "media_grant_revision": 1,
        "visible_chat_id": CHAT,
        "chat_context_revision": 1,
        "occurred_at": "2026-07-31T18:00:01Z",
    }
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(context_applied),
    )
    repository = FakeCoordinatorRepository()
    coordinator = VoiceCoordinator(
        pool,
        repository,
        replica_id="replica-a",
        utcnow=clock.utcnow,
    )

    def recognition(client_turn_id: str, sequence: int) -> dict[str, object]:
        return {
            "type": "recognition_started",
            "schema_version": "1",
            "message_id": deterministic_uuid4("recognition-started", client_turn_id),
            "session_id": SESSION_1,
            "generation": 1,
            "sequence": sequence,
            "sent_at": "2026-07-31T18:00:02Z",
            "client_turn_id": client_turn_id,
            "media_grant_revision": 1,
            "visible_chat_id": CHAT,
            "chat_context_revision": 1,
            "occurred_at": "2026-07-31T18:00:02Z",
        }

    first_client_turn = deterministic_uuid4("client-turn", "first")
    first_frame = await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(recognition(first_client_turn, 2)),
    )
    first_binding = await coordinator.bind_recognition_started(first_frame)
    assert json.loads(socket.sent[-1]) == {
        "type": "turn_bound",
        "schema_version": "1",
        "message_id": json.loads(socket.sent[-1])["message_id"],
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 0,
        "sent_at": "2026-07-31T18:00:00Z",
        "client_turn_id": first_client_turn,
        "turn_id": first_binding.turn_id,
        "chat_id": CHAT,
        "chat_context_revision": 1,
        "media_grant_revision": 1,
        "submission_id": first_binding.submission_id,
        "request_generation": first_binding.request_generation,
    }
    accepted = await coordinator.emit_transcript_accepted(
        repository.recognition_turns[0],
        accepted_message_id=41,
    )
    assert accepted["accepted_message_id"] == 41

    second_client_turn = deterministic_uuid4("client-turn", "second")
    second_frame = await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(recognition(second_client_turn, 3)),
    )
    await coordinator.bind_recognition_started(second_frame)
    rejected = await coordinator.emit_transcript_rejected(
        repository.recognition_turns[1],
        reason="permission_denied",
        retry_policy="none",
    )
    assert rejected["reason"] == "permission_denied"
    assert rejected["retry_policy"] == "none"

    control_frames = [json.loads(value) for value in socket.sent[1:]]
    assert [frame["type"] for frame in control_frames] == [
        "turn_bound",
        "transcript_accepted",
        "turn_bound",
        "transcript_rejected",
    ]
    rendered = "".join(socket.sent)
    assert "transcript_proof" not in rendered
    assert '"text"' not in rendered

    stale = recognition(
        deterministic_uuid4("client-turn", "stale"),
        4,
    )
    stale["visible_chat_id"] = SESSION_2
    with pytest.raises(StaleFence, match="stale_chat_context_revision"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(stale),
        )


@pytest.mark.asyncio
async def test_recognition_failure_is_bounded_durable_and_replay_fenced() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"),
        socket,
        authenticated_identity="worker-a",
    )
    reservation = await pool.reserve_session(_request(SESSION_1))
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0)),
    )
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(
            {
                "type": "session_context_applied",
                "schema_version": "1",
                "message_id": deterministic_uuid4("context-applied", SESSION_1),
                "session_id": SESSION_1,
                "generation": 1,
                "sequence": 1,
                "sent_at": "2026-07-31T18:00:01Z",
                "media_grant_revision": 1,
                "visible_chat_id": CHAT,
                "chat_context_revision": 1,
                "occurred_at": "2026-07-31T18:00:01Z",
            }
        ),
    )
    repository = FakeCoordinatorRepository()
    coordinator = VoiceCoordinator(
        pool,
        repository,
        replica_id="replica-a",
        utcnow=clock.utcnow,
    )
    client_turn_id = deterministic_uuid4("client-turn", "asr-failure")
    started = await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(
            {
                "type": "recognition_started",
                "schema_version": "1",
                "message_id": deterministic_uuid4(
                    "recognition-started", client_turn_id
                ),
                "session_id": SESSION_1,
                "generation": 1,
                "sequence": 2,
                "sent_at": "2026-07-31T18:00:02Z",
                "client_turn_id": client_turn_id,
                "media_grant_revision": 1,
                "visible_chat_id": CHAT,
                "chat_context_revision": 1,
                "occurred_at": "2026-07-31T18:00:02Z",
            }
        ),
    )
    binding = await coordinator.bind_recognition_started(started)
    failure = {
        "type": "recognition_failed",
        "schema_version": "1",
        "message_id": deterministic_uuid4("recognition-failed", client_turn_id),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 3,
        "sent_at": "2026-07-31T18:00:03Z",
        "client_turn_id": client_turn_id,
        "reason": "asr_failed",
        "occurred_at": "2026-07-31T18:00:03Z",
    }
    invalid = {**failure, "reason": "provider_body"}
    with pytest.raises(
        ControlProtocolError,
        match="invalid_recognition_failure_reason",
    ):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(invalid),
        )

    accepted = await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(failure),
    )
    mutation = await coordinator.reject_recognition_failed(accepted)
    assert mutation.turn.turn_id == binding.turn_id
    assert mutation.turn.state == "abandoned"
    assert mutation.turn.rejection_reason == "malformed_final"
    assert mutation.turn.rejection_retry_policy == "explicit_user_retry"
    disposition = json.loads(socket.sent[-1])
    assert disposition["type"] == "transcript_rejected"
    assert disposition["turn_id"] == binding.turn_id
    assert disposition["client_turn_id"] == client_turn_id
    assert disposition["reason"] == "malformed_final"
    assert disposition["retry_policy"] == "explicit_user_retry"

    with pytest.raises(ControlProtocolError, match="sequence_out_of_order"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(failure),
        )
    stale = {
        **failure,
        "message_id": deterministic_uuid4("recognition-failed-replay", client_turn_id),
        "sequence": 4,
    }
    with pytest.raises(StaleFence, match="recognition_not_bound"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(stale),
        )


@pytest.mark.asyncio
async def test_self_speech_is_durably_suppressed_without_worker_disposition() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"),
        socket,
        authenticated_identity="worker-a",
    )
    reservation = await pool.reserve_session(_request(SESSION_1))
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0)),
    )
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(
            {
                "type": "session_context_applied",
                "schema_version": "1",
                "message_id": deterministic_uuid4("context-applied", SESSION_1),
                "session_id": SESSION_1,
                "generation": 1,
                "sequence": 1,
                "sent_at": "2026-07-31T18:00:01Z",
                "media_grant_revision": 1,
                "visible_chat_id": CHAT,
                "chat_context_revision": 1,
                "occurred_at": "2026-07-31T18:00:01Z",
            }
        ),
    )
    repository = FakeCoordinatorRepository()
    coordinator = VoiceCoordinator(
        pool,
        repository,
        replica_id="replica-a",
        utcnow=clock.utcnow,
    )
    client_turn_id = deterministic_uuid4("client-turn", "self-speech")
    started = await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(
            {
                "type": "recognition_started",
                "schema_version": "1",
                "message_id": deterministic_uuid4(
                    "recognition-started", client_turn_id
                ),
                "session_id": SESSION_1,
                "generation": 1,
                "sequence": 2,
                "sent_at": "2026-07-31T18:00:02Z",
                "client_turn_id": client_turn_id,
                "media_grant_revision": 1,
                "visible_chat_id": CHAT,
                "chat_context_revision": 1,
                "occurred_at": "2026-07-31T18:00:02Z",
            }
        ),
    )
    binding = await coordinator.bind_recognition_started(started)
    failure = {
        "type": "recognition_failed",
        "schema_version": "1",
        "message_id": deterministic_uuid4("recognition-failed", client_turn_id),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 3,
        "sent_at": "2026-07-31T18:00:03Z",
        "client_turn_id": client_turn_id,
        "reason": "self_speech",
        "occurred_at": "2026-07-31T18:00:03Z",
    }
    accepted = await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(failure),
    )

    with pytest.raises(
        ControlProtocolError,
        match="invalid_recognition_failure_reason",
    ):
        await coordinator.reject_recognition_failed(accepted)
    with pytest.raises(StaleFence, match="stale_worker_assignment"):
        await pool.clear_suppressed_recognition(
            replace(binding, assignment_id=deterministic_uuid4("assignment", "stale"))
        )
    assert (
        await pool.current_recognition_binding(
            session_id=SESSION_1,
            generation=1,
            client_turn_id=client_turn_id,
        )
        == binding
    )

    sent_before = tuple(socket.sent)
    mutation = await coordinator.suppress_self_speech(accepted)
    assert mutation.turn.turn_id == binding.turn_id
    assert mutation.turn.state == "abandoned"
    assert mutation.turn.rejection_reason == "malformed_final"
    assert mutation.turn.rejection_retry_policy == "none"
    assert tuple(socket.sent) == sent_before
    assert [json.loads(value)["type"] for value in socket.sent[1:]] == ["turn_bound"]

    with pytest.raises(StaleFence, match="recognition_not_bound"):
        await pool.current_recognition_binding(
            session_id=SESSION_1,
            generation=1,
            client_turn_id=client_turn_id,
        )
    replay = {
        **failure,
        "message_id": deterministic_uuid4("recognition-failed-replay", client_turn_id),
        "sequence": 4,
    }
    with pytest.raises(StaleFence, match="recognition_not_bound"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(replay),
        )


@pytest.mark.asyncio
async def test_higher_grant_revision_rebind_is_ordered_and_stale_is_rejected() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    original = _request(SESSION_1)
    reservation = await pool.reserve_session(original)
    first = await pool.deliver_session_bind(
        reservation, original, _grant("worker-a", original)
    )

    clock.advance(1)
    renewed = _request(SESSION_1, worker_revision=2)
    same = await pool.reserve_session(renewed)
    second = await pool.deliver_session_bind(
        same,
        renewed,
        {
            **_grant("worker-a", renewed),
            "issued_at": "2026-07-31T18:00:01Z",
            "expires_at": "2026-07-31T18:05:01Z",
        },
    )

    assert same.assignment_id == reservation.assignment_id
    assert (first["sequence"], second["sequence"]) == (0, 1)
    assert second["worker_rtc_grant_revision"] == 2
    with pytest.raises(StaleFence, match="stale_worker_grant_revision"):
        await pool.reserve_session(original)


@pytest.mark.asyncio
async def test_grant_mismatch_and_send_timeout_are_content_free_and_fence_socket() -> (
    None
):
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)
    token = str(_grant("worker-a", request)["join_token"])
    with pytest.raises(ControlProtocolError, match="grant_room_mismatch") as error:
        await pool.deliver_session_bind(
            reservation,
            request,
            {**_grant("worker-a", request), "room_name": "wrong-room"},
        )
    assert token not in repr(error.value)

    socket.gate = asyncio.Event()
    with pytest.raises(ControlSendError, match="send_timeout") as timeout:
        await pool.deliver_session_bind(
            reservation, request, _grant("worker-a", request)
        )
    assert token not in repr(timeout.value)
    assert socket.closed[-1] == (1011, "send_failed")
    assert pool.readiness().reason == "worker_unavailable"


@pytest.mark.asyncio
async def test_cancelled_ambiguous_send_fences_the_connection() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    socket.gate = asyncio.Event()
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)
    sending = asyncio.create_task(
        pool.deliver_session_bind(reservation, request, _grant("worker-a", request))
    )
    await asyncio.sleep(0)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert socket.closed[-1] == (1011, "send_cancelled")
    assert pool.readiness().reason == "worker_unavailable"


@pytest.mark.asyncio
async def test_receive_rate_size_and_json_are_bounded_before_side_effects() -> None:
    clock = FakeClock()
    pool = WorkerPool(
        _policy(max_receive_frames=2), utcnow=clock.utcnow, monotonic=clock.monotonic
    )
    socket = FakeSocket()
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)
    ready = _worker_ready(reservation, sequence=0)
    await pool.receive_worker_frame(reservation.connection_id, json.dumps(ready))
    heartbeat = {
        "type": "heartbeat",
        "schema_version": "1",
        "message_id": deterministic_uuid4("heartbeat", SESSION_1),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:02Z",
        "media_state": "ready",
    }
    await pool.receive_worker_frame(reservation.connection_id, json.dumps(heartbeat))
    with pytest.raises(ControlProtocolError, match="frame_rate_exceeded"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps({**heartbeat, "sequence": 2}),
        )

    clock.advance(2)
    with pytest.raises(ControlProtocolError, match="text_frame_required"):
        await pool.receive_worker_frame(reservation.connection_id, b"{}")
    with pytest.raises(ControlProtocolError, match="frame_too_large"):
        await pool.receive_worker_frame(
            reservation.connection_id, "x" * (15 * 1024 + 1)
        )
    with pytest.raises(ControlProtocolError, match="duplicate_json_key"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            '{"type":"heartbeat","type":"heartbeat"}',
        )


@pytest.mark.asyncio
async def test_worker_lease_touch_expiry_and_readiness_are_monotonic() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    socket = FakeSocket()
    receipt = await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    clock.advance(15)
    await pool.touch_connection(receipt.connection_id)
    clock.advance(15)
    assert await pool.expire_connections() == ()
    clock.advance(2)
    assert await pool.expire_connections() == (receipt.connection_id,)
    assert socket.closed == [(4000, "lease_expired")]
    assert pool.readiness().capacity_total == 0


@pytest.mark.asyncio
async def test_worker_lease_expiry_preserves_exact_credential_free_releases() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    socket = FakeSocket()
    receipt = await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    reservation = await pool.reserve_session(_request(SESSION_1))
    clock.advance(31)

    releases = await pool.expire_connection_leases()

    assert len(releases) == 1
    release = releases[0]
    assert release.connection_id == receipt.connection_id
    assert release.worker_identity == "worker-a"
    assert release.accepted_max_sessions == receipt.accepted_max_sessions
    assert release.session_ids == (SESSION_1,)
    assert release.assignment_ids == (reservation.assignment_id,)
    assert "token" not in repr(release).lower()
    assert socket.closed == [(4000, "lease_expired")]
    assert await pool.expire_connection_leases() == ()
    assert await pool.unregister_worker(receipt.connection_id) == ()


@pytest.mark.asyncio
async def test_concurrent_pool_shutdown_waits_for_one_bounded_cleanup() -> None:
    class BlockingCloseSocket(FakeSocket):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_release = asyncio.Event()

        async def close(self, code: int = 1000, reason: str = "") -> None:
            self.close_started.set()
            await self.close_release.wait()
            await super().close(code=code, reason=reason)

    clock = FakeClock()
    socket = BlockingCloseSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    await pool.reserve_session(_request(SESSION_1))

    first = asyncio.create_task(pool.shutdown())
    await socket.close_started.wait()
    second = asyncio.create_task(pool.shutdown())
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    socket.close_release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result == (SESSION_1,)
    assert socket.closed == [(1001, "coordinator_shutdown")]
    assert await pool.shutdown() == ()


@pytest.mark.asyncio
async def test_ordered_pool_heartbeat_keeps_idle_connection_live() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    socket = FakeSocket()
    receipt = await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )

    def heartbeat(sequence: int, **changes: object) -> dict[str, object]:
        frame: dict[str, object] = {
            "type": "pool_heartbeat",
            "schema_version": "1",
            "message_id": deterministic_uuid4(
                "pool-heartbeat", receipt.connection_id, str(sequence)
            ),
            "sequence": sequence,
            "sent_at": "2026-07-31T18:00:02Z",
            "worker_identity": "worker-a",
            "connection_id": receipt.connection_id,
        }
        frame.update(changes)
        return frame

    for sequence in range(1, 4):
        clock.advance(30)
        accepted = await pool.receive_worker_frame(
            receipt.connection_id, json.dumps(heartbeat(sequence))
        )
        assert accepted["type"] == "pool_heartbeat"
        assert pool.readiness().ready is True

    clock.advance(34)
    with pytest.raises(ControlProtocolError, match="sequence_out_of_order"):
        await pool.receive_worker_frame(
            receipt.connection_id, json.dumps(heartbeat(3))
        )
    clock.advance(2)
    assert await pool.expire_connections() == (receipt.connection_id,)
    assert socket.closed == [(4000, "lease_expired")]


@pytest.mark.asyncio
async def test_pool_heartbeat_rejects_cross_connection_and_identity() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    receipt = await pool.register_worker(
        _registration("worker-a"), FakeSocket(), authenticated_identity="worker-a"
    )
    base = {
        "type": "pool_heartbeat",
        "schema_version": "1",
        "message_id": deterministic_uuid4("pool-heartbeat", "invalid-fences"),
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:02Z",
        "worker_identity": "worker-a",
        "connection_id": receipt.connection_id,
    }

    with pytest.raises(StaleFence, match="stale_connection"):
        await pool.receive_worker_frame(
            receipt.connection_id,
            json.dumps(
                {
                    **base,
                    "connection_id": deterministic_uuid4(
                        "other-pool-connection"
                    ),
                }
            ),
        )
    with pytest.raises(StaleFence, match="worker_identity_mismatch"):
        await pool.receive_worker_frame(
            receipt.connection_id,
            json.dumps({**base, "worker_identity": "worker-b"}),
        )

    await pool.receive_worker_frame(receipt.connection_id, json.dumps(base))


@pytest.mark.asyncio
async def test_generic_commands_are_bounded_directional_and_releasable() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    reservation = await pool.reserve_session(_request(SESSION_1))

    frame = await pool.send_session_command(
        reservation,
        "set_capture",
        {"media_grant_revision": 1, "enabled": False, "reason": "backgrounded"},
    )
    assert frame["sequence"] == 0
    assert json.loads(socket.sent[-1]) == frame
    assert pool.assignment_snapshot(SESSION_1).next_outgoing_sequence == 1

    with pytest.raises(ControlProtocolError, match="wrong_direction"):
        await pool.send_session_command(reservation, "heartbeat", {})
    with pytest.raises(ControlProtocolError, match="protected_frame_field"):
        await pool.send_session_command(reservation, "end_session", {"sequence": 2})
    with pytest.raises(ControlProtocolError, match="forbidden_coordinator_content"):
        await pool.send_session_command(
            reservation,
            "speak",
            {"text": "safe", "nested": [{"api_secret": "must-not-pass"}]},
        )
    with pytest.raises(ControlProtocolError, match="invalid_outgoing_frame"):
        await pool.send_session_command(
            reservation, "end_session", {"reason": {"not-json"}}
        )
    with pytest.raises(ControlProtocolError, match="outgoing_frame_too_large"):
        await pool.send_session_command(
            reservation, "speak", {"text": "x" * (15 * 1024)}
        )

    assert await pool.release_session(SESSION_1, 1, reservation.assignment_id)
    assert not await pool.release_session(SESSION_1, 1, reservation.assignment_id)
    with pytest.raises(StaleFence, match="stale_assignment"):
        pool.assignment_snapshot(SESSION_1)


@pytest.mark.asyncio
async def test_per_session_send_waiters_are_bounded() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(
        _policy(max_pending_session_sends=1),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic,
    )
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    reservation = await pool.reserve_session(_request(SESSION_1))
    socket.gate = asyncio.Event()
    first = asyncio.create_task(
        pool.send_session_command(
            reservation,
            "set_capture",
            {"media_grant_revision": 1, "enabled": False, "reason": "backgrounded"},
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(ControlSendError, match="send_queue_full"):
        await pool.send_session_command(
            reservation,
            "set_capture",
            {"media_grant_revision": 1, "enabled": True, "reason": "foreground"},
        )
    socket.gate.set()
    assert (await first)["sequence"] == 0


@pytest.mark.asyncio
async def test_receive_generic_failure_and_fence_rejections() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    reservation = await pool.reserve_session(_request(SESSION_1))

    with pytest.raises(ControlProtocolError, match="unknown_frame_type"):
        await pool.receive_worker_frame(
            reservation.connection_id, json.dumps({"type": "not_real"})
        )
    bad_base = _worker_ready(reservation, sequence=0)
    bad_base.pop("sent_at")
    with pytest.raises(ControlProtocolError, match="invalid_worker_frame_base"):
        await pool.receive_worker_frame(reservation.connection_id, json.dumps(bad_base))
    with pytest.raises(ControlProtocolError, match="forbidden_worker_content"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps({**_worker_ready(reservation, sequence=0), "audio": []}),
        )
    with pytest.raises(StaleFence, match="stale_generation"):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps({**_worker_ready(reservation, sequence=0), "generation": 2}),
        )

    not_ready = {
        **_worker_ready(reservation, sequence=0),
        "profile_ready": False,
        "reason": "speech_unavailable",
    }
    await pool.receive_worker_frame(reservation.connection_id, json.dumps(not_ready))
    assert pool.assignment_snapshot(SESSION_1).media_state == "failed"
    generic = {
        "type": "media_state",
        "schema_version": "1",
        "message_id": deterministic_uuid4("media-state", SESSION_1),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:02Z",
        "state": "listening",
        "occurred_at": "2026-07-31T18:00:02Z",
    }
    assert (
        await pool.receive_worker_frame(reservation.connection_id, json.dumps(generic))
    )["state"] == "listening"

    wrong_session = {**generic, "sequence": 2, "session_id": SESSION_2}
    with pytest.raises(StaleFence, match="stale_assignment"):
        await pool.receive_worker_frame(
            reservation.connection_id, json.dumps(wrong_session)
        )


@pytest.mark.parametrize("terminal_state", ["failed", "ended"])
@pytest.mark.asyncio
async def test_explicit_terminal_media_state_fences_commands_but_allows_cleanup(
    terminal_state: str,
) -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0)),
    )
    prior_signal = pool._assignments[SESSION_1].state_changed
    terminal = {
        "type": "media_state",
        "schema_version": "1",
        "message_id": deterministic_uuid4(
            "terminal-media-state", SESSION_1, terminal_state
        ),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:02Z",
        "state": terminal_state,
        "reason": "client_playout_timeout",
        "occurred_at": "2026-07-31T18:00:02Z",
    }

    await pool.receive_worker_frame(reservation.connection_id, json.dumps(terminal))

    snapshot = pool.assignment_snapshot(SESSION_1)
    assert snapshot.media_state == terminal_state
    assert snapshot.ready is False
    assert prior_signal.is_set()

    duplicate_ready = _worker_ready(reservation, sequence=2)
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(duplicate_ready),
    )
    stale_heartbeat = {
        "type": "heartbeat",
        "schema_version": "1",
        "message_id": deterministic_uuid4(
            "terminal-stale-heartbeat", SESSION_1, terminal_state
        ),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 3,
        "sent_at": "2026-07-31T18:00:03Z",
        "media_state": "ready",
    }
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(stale_heartbeat),
    )
    sticky = pool.assignment_snapshot(SESSION_1)
    assert sticky.media_state == terminal_state
    assert sticky.ready is False

    sent_before = list(socket.sent)
    with pytest.raises(StaleFence, match="terminal_assignment"):
        await pool.deliver_session_bind(
            reservation,
            request,
            _grant("worker-a", request),
        )
    with pytest.raises(StaleFence, match="terminal_assignment"):
        await pool.send_session_command(
            reservation,
            "set_capture",
            {"media_grant_revision": 1, "enabled": True},
        )
    assert socket.sent == sent_before
    assert pool.assignment_snapshot(SESSION_1).next_outgoing_sequence == 0

    cleanup = await pool.send_session_command(
        reservation,
        "end_session",
        {"media_grant_revision": 1, "reason": "media_error"},
    )
    assert cleanup["sequence"] == 0
    assert json.loads(socket.sent[-1]) == cleanup


@pytest.mark.asyncio
async def test_terminal_assignment_revives_only_for_higher_worker_grant() -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0)),
    )
    terminal = {
        "type": "media_state",
        "schema_version": "1",
        "message_id": deterministic_uuid4("rebind-terminal", SESSION_1),
        "session_id": SESSION_1,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:02Z",
        "state": "failed",
        "reason": "media_error",
        "occurred_at": "2026-07-31T18:00:02Z",
    }
    await pool.receive_worker_frame(reservation.connection_id, json.dumps(terminal))

    same = await pool.reserve_session(request)
    with pytest.raises(StaleFence, match="terminal_assignment"):
        await pool.deliver_session_bind(same, request, _grant("worker-a", request))

    renewed = _request(SESSION_1, worker_revision=2)
    renewed_reservation = await pool.reserve_session(renewed)
    reconnecting = pool.assignment_snapshot(SESSION_1)
    assert reconnecting.media_state == "reconnecting"
    assert reconnecting.ready is False
    rebound = await pool.deliver_session_bind(
        renewed_reservation,
        renewed,
        _grant("worker-a", renewed),
    )
    assert rebound["worker_rtc_grant_revision"] == 2
    await pool.receive_worker_frame(
        renewed_reservation.connection_id,
        json.dumps(
            _worker_ready(
                renewed_reservation,
                sequence=2,
                revision=2,
            )
        ),
    )
    revived = pool.assignment_snapshot(SESSION_1)
    assert revived.media_state == "ready"
    assert revived.ready is True


@pytest.mark.parametrize(
    ("frame_type", "terminal_state"),
    (
        ("media_state", "failed"),
        ("media_state", "ended"),
        ("heartbeat", "failed"),
        ("heartbeat", "ended"),
        ("worker_ready", "failed"),
    ),
)
@pytest.mark.asyncio
async def test_terminal_release_frees_only_a_and_b_sequences_normally(
    frame_type: str,
    terminal_state: str,
) -> None:
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a", capacity=2),
        socket,
        authenticated_identity="worker-a",
    )
    reservation_a = await pool.reserve_session(_request(SESSION_1))
    reservation_b = await pool.reserve_session(_request(SESSION_2))
    for reservation in (reservation_a, reservation_b):
        await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(_worker_ready(reservation, sequence=0)),
        )
    assert pool.readiness().capacity_available == 0

    if frame_type == "worker_ready":
        terminal = {
            **_worker_ready(reservation_a, sequence=1),
            "profile_ready": False,
            "reason": "speech_unavailable",
        }
    elif frame_type == "heartbeat":
        terminal = {
            "type": "heartbeat",
            "schema_version": "1",
            "message_id": deterministic_uuid4(
                "terminal-heartbeat-release",
                SESSION_1,
                terminal_state,
            ),
            "session_id": SESSION_1,
            "generation": 1,
            "sequence": 1,
            "sent_at": "2026-07-31T18:00:02Z",
            "media_state": terminal_state,
        }
    else:
        terminal = {
            "type": "media_state",
            "schema_version": "1",
            "message_id": deterministic_uuid4(
                "terminal-media-release",
                SESSION_1,
                terminal_state,
            ),
            "session_id": SESSION_1,
            "generation": 1,
            "sequence": 1,
            "sent_at": "2026-07-31T18:00:02Z",
            "state": terminal_state,
            "reason": "media_error",
            "occurred_at": "2026-07-31T18:00:02Z",
        }
    await pool.receive_worker_frame(
        reservation_a.connection_id,
        json.dumps(terminal),
    )

    assert (
        await pool.release_terminal_assignment(
            connection_id=deterministic_uuid4(
                "wrong-terminal-release-connection",
                SESSION_1,
            ),
            session_id=SESSION_1,
            generation=1,
            terminal_state=terminal_state,
        )
        is None
    )
    assert (
        await pool.release_terminal_assignment(
            connection_id=reservation_a.connection_id,
            session_id=SESSION_1,
            generation=1,
            terminal_state=("ended" if terminal_state == "failed" else "failed"),
        )
        is None
    )
    assert pool.readiness().capacity_available == 0

    released = await pool.release_terminal_assignment(
        connection_id=reservation_a.connection_id,
        session_id=SESSION_1,
        generation=1,
        terminal_state=terminal_state,
    )

    assert released is not None
    assert released.reservation == reservation_a
    assert released.connection_id == reservation_a.connection_id
    assert released.session_id == SESSION_1
    assert released.generation == 1
    assert released.assignment_id == reservation_a.assignment_id
    assert released.worker_identity == "worker-a"
    assert released.worker_rtc_grant_revision == 1
    assert released.accepted_max_sessions == 2
    assert released.terminal_state == terminal_state
    assert pool.readiness().capacity_available == 1
    with pytest.raises(StaleFence, match="stale_assignment"):
        pool.assignment_snapshot(SESSION_1)

    command_b = await pool.send_session_command(
        reservation_b,
        "set_capture",
        {"media_grant_revision": 1, "enabled": True},
    )
    heartbeat_b = {
        "type": "heartbeat",
        "schema_version": "1",
        "message_id": deterministic_uuid4("healthy-peer-heartbeat", SESSION_2),
        "session_id": SESSION_2,
        "generation": 1,
        "sequence": 1,
        "sent_at": "2026-07-31T18:00:03Z",
        "media_state": "ready",
    }
    await pool.receive_worker_frame(
        reservation_b.connection_id,
        json.dumps(heartbeat_b),
    )
    healthy = pool.assignment_snapshot(SESSION_2)
    assert command_b["sequence"] == 0
    assert healthy.next_outgoing_sequence == 1
    assert healthy.next_incoming_sequence == 2
    assert healthy.media_state == "ready"

    assert (
        await pool.release_terminal_assignment(
            connection_id=reservation_a.connection_id,
            session_id=SESSION_1,
            generation=1,
            terminal_state=terminal_state,
        )
        is None
    )
    replacement = await pool.reserve_session(_request(SESSION_1, generation=2))
    assert (
        await pool.release_terminal_assignment(
            connection_id=reservation_a.connection_id,
            session_id=SESSION_1,
            generation=1,
            terminal_state=terminal_state,
        )
        is None
    )
    assert pool.assignment_snapshot(SESSION_1).assignment_id == replacement.assignment_id


@pytest.mark.asyncio
async def test_registration_capacity_send_and_uuid_factory_fail_closed() -> None:
    clock = FakeClock()
    pool = WorkerPool(
        _policy(max_workers=1), utcnow=clock.utcnow, monotonic=clock.monotonic
    )
    await pool.register_worker(
        _registration("worker-a"), FakeSocket(), authenticated_identity="worker-a"
    )
    with pytest.raises(RegistrationError, match="worker_registry_full"):
        await pool.register_worker(
            _registration("worker-b"),
            FakeSocket(),
            authenticated_identity="worker-b",
        )

    failing = FakeSocket()
    failing.error = RuntimeError("provider body and token must not escape")
    with pytest.raises(ControlSendError, match="send_failed") as error:
        await WorkerPool(
            _policy(), utcnow=clock.utcnow, monotonic=clock.monotonic
        ).register_worker(
            _registration("worker-c"), failing, authenticated_identity="worker-c"
        )
    assert "provider body" not in repr(error.value)
    assert failing.closed == [(1011, "registration_failed")]

    invalid_uuid_pool = WorkerPool(
        _policy(),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic,
        uuid_factory=lambda: UUID("00000000-0000-1000-8000-000000000001"),
    )
    with pytest.raises(RuntimeError, match="connection_id_factory_must_return_uuid4"):
        await invalid_uuid_pool.register_worker(
            _registration("worker-d"),
            FakeSocket(),
            authenticated_identity="worker-d",
        )


@pytest.mark.asyncio
async def test_generation_takeover_total_capacity_and_request_conflict() -> None:
    clock = FakeClock()
    pool = WorkerPool(
        _policy(max_total_sessions=1),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic,
    )
    await pool.register_worker(
        _registration("worker-a", capacity=2),
        FakeSocket(),
        authenticated_identity="worker-a",
    )
    original = await pool.reserve_session(_request(SESSION_1))
    with pytest.raises(CapacityUnavailable, match="deployment_capacity_exhausted"):
        await pool.reserve_session(_request(SESSION_2))
    with pytest.raises(StaleFence, match="assignment_request_conflict"):
        await pool.reserve_session(
            replace(_request(SESSION_1), visible_chat_id=SESSION_2)
        )
    replacement = await pool.reserve_session(_request(SESSION_1, generation=2))
    assert replacement.assignment_id != original.assignment_id
    with pytest.raises(StaleFence, match="stale_generation"):
        await pool.reserve_session(_request(SESSION_1, generation=1))
    assert not await pool.release_session(SESSION_1, 1, original.assignment_id)


@pytest.mark.asyncio
async def test_worker_grant_contract_rejects_every_cross_binding_variant() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), FakeSocket(), authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)
    grant = _grant("worker-a", request)
    cases = [
        (
            {key: value for key, value in grant.items() if key != "revision"},
            "invalid_worker_grant_fields",
        ),
        ({**grant, "revision": 2}, "grant_revision_mismatch"),
        ({**grant, "worker_identity": "worker-b"}, "grant_worker_mismatch"),
        ({**grant, "livekit_url": "https://not-rtc.example"}, "invalid_livekit_url"),
        ({**grant, "join_token": "short"}, "invalid_worker_join_token"),
        ({**grant, "issued_at": "2026-07-31T17:59:59Z"}, "grant_predates_assignment"),
        (
            {
                **grant,
                "issued_at": "2026-07-31T18:00:06Z",
                "expires_at": "2026-07-31T18:05:06Z",
            },
            "grant_not_yet_valid",
        ),
        ({**grant, "expires_at": "2026-07-31T18:00:00Z"}, "grant_expired"),
        ({**grant, "expires_at": "2026-07-31T18:06:00Z"}, "grant_lifetime_exceeded"),
    ]
    for invalid, code in cases:
        with pytest.raises(ControlProtocolError, match=code):
            await pool.deliver_session_bind(reservation, request, invalid)

    secure_pool = WorkerPool(
        _policy(allow_insecure_livekit_url=False),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic,
    )
    await secure_pool.register_worker(
        _registration("worker-secure"),
        FakeSocket(),
        authenticated_identity="worker-secure",
    )
    secure_reservation = await secure_pool.reserve_session(request)
    with pytest.raises(ControlProtocolError, match="invalid_livekit_url"):
        await secure_pool.deliver_session_bind(
            secure_reservation,
            request,
            _grant("worker-secure", request),
        )


@pytest.mark.asyncio
async def test_same_second_worker_grant_does_not_predate_fractional_assignment() -> None:
    clock = FakeClock()
    clock.utc = NOW.replace(microsecond=987_654)
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"), socket, authenticated_identity="worker-a"
    )
    request = _request(SESSION_1)
    reservation = await pool.reserve_session(request)

    delivered = await pool.deliver_session_bind(
        reservation,
        request,
        _grant("worker-a", request),
    )

    assert delivered["assignment_id"] == reservation.assignment_id
    assert json.loads(socket.sent[-1])["type"] == "session_bind"


def test_policy_clock_and_deterministic_uuid_validation() -> None:
    stable = deterministic_uuid4("announcement", SESSION_1, TURN, "1")
    assert stable == deterministic_uuid4("announcement", SESSION_1, TURN, "1")
    assert stable != deterministic_uuid4("announcement", SESSION_1, TURN, "2")
    assert UUID(stable).version == 4
    with pytest.raises(ValueError, match="invalid_deterministic_id_part"):
        deterministic_uuid4("announcement", "")
    with pytest.raises(ValueError, match="invalid_closure_digest"):
        _policy(runtime_closure_sha256="0" * 64)
    assert WorkerPoolPolicy(
        runtime_closure_sha256="0" * 64,
        allow_unapproved_development_closure=True,
    ).allow_unapproved_development_closure
    for changes, code in (
        ({"max_workers": 0}, "invalid_max_workers"),
        ({"max_sessions_per_worker": 101}, "invalid_worker_capacity"),
        ({"max_total_sessions": 0}, "invalid_total_capacity"),
        ({"heartbeat_interval_seconds": 4}, "invalid_heartbeat_interval"),
        ({"connection_lease_seconds": 5}, "invalid_connection_lease"),
        ({"send_timeout_seconds": float("nan")}, "invalid_send_timeout"),
        ({"max_receive_frames": 0}, "invalid_frame_rate"),
        ({"receive_window_seconds": float("inf")}, "invalid_frame_window"),
        ({"max_pending_session_sends": 0}, "invalid_send_queue_bound"),
        ({"allow_insecure_livekit_url": "yes"}, "invalid_insecure_livekit_policy"),
    ):
        with pytest.raises(ValueError, match=code):
            _policy(**changes)

    fake = FakeClock()
    clock = CoordinatorClock(utcnow=fake.utcnow, monotonic=fake.monotonic)
    assert clock.utcnow() == NOW
    assert clock.monotonic() == 100.0
    fake.mono = 99.0
    with pytest.raises(RuntimeError, match="monotonic_clock_regressed"):
        clock.monotonic()
    fake.utc = fake.utc.replace(tzinfo=None)
    with pytest.raises(RuntimeError, match="utc_clock_must_be_timezone_aware"):
        clock.utcnow()
    with pytest.raises(RuntimeError, match="invalid_monotonic_clock"):
        CoordinatorClock(monotonic=lambda: float("nan")).monotonic()

    sanitized = VoiceCoordinatorError("upstream secret value")
    assert sanitized.code == "voice_coordinator_error"


def test_monotonic_scheduler_uses_utc_only_for_crash_recovery() -> None:
    fake = FakeClock()
    clock = CoordinatorClock(utcnow=fake.utcnow, monotonic=fake.monotonic)
    scheduler = MonotonicScheduler(clock, max_delay_seconds=30)
    deadline = scheduler.schedule_after(14)
    assert deadline.due_monotonic == 114
    assert deadline.recovery_due_at == NOW + timedelta(seconds=14)
    fake.advance(5)
    assert scheduler.remaining(deadline) == 9
    recovered = scheduler.recover(deadline.recovery_due_at)
    assert recovered.due_monotonic == 114
    fake.advance(9)
    assert scheduler.is_due(deadline)
    with pytest.raises(ValueError, match="invalid_schedule_delay"):
        scheduler.schedule_after(31)
    with pytest.raises(ValueError, match="invalid_max_schedule_delay"):
        MonotonicScheduler(clock, max_delay_seconds=float("inf"))


def test_control_lease_claim_renew_stale_and_crash_takeover() -> None:
    adapter = ControlLeaseAdapter(ttl_seconds=10)
    state = ControlLeaseState(generation=3)
    claimed = adapter.claim(state, generation=3, owner_id="replica-a", now=NOW)
    assert claimed.owner_id == "replica-a"
    assert claimed.expires_at == NOW + timedelta(seconds=10)
    renewed = adapter.claim(
        claimed, generation=3, owner_id="replica-a", now=NOW + timedelta(seconds=5)
    )
    assert renewed.expires_at == NOW + timedelta(seconds=15)
    with pytest.raises(ClaimUnavailable, match="control_lease_owned"):
        adapter.claim(
            renewed,
            generation=3,
            owner_id="replica-b",
            now=NOW + timedelta(seconds=14),
        )
    recovered = adapter.claim(
        renewed,
        generation=3,
        owner_id="replica-b",
        now=NOW + timedelta(seconds=15),
    )
    assert recovered.owner_id == "replica-b"
    with pytest.raises(StaleFence, match="stale_generation"):
        adapter.claim(recovered, generation=2, owner_id="replica-b", now=NOW)
    assert adapter.release(recovered, generation=3, owner_id="replica-a") == recovered
    assert (
        adapter.release(recovered, generation=3, owner_id="replica-b").owner_id is None
    )


@pytest.mark.asyncio
async def test_voice_coordinator_repository_seam_validates_every_claim() -> None:
    clock = FakeClock()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    repository = FakeCoordinatorRepository()
    coordinator = VoiceCoordinator(
        pool, repository, replica_id="orchestrator-a", utcnow=clock.utcnow
    )

    control = await coordinator.claim_session_control(
        user_id="user-123", session_id=SESSION_1, generation=3
    )
    assert control.owner_id == "orchestrator-a"
    assert await coordinator.release_session_control(
        user_id="user-123", session_id=SESSION_1, generation=3
    )

    mutation = await coordinator.claim_turn_announcement(
        user_id="user-123", request=_claim_request()
    )
    assert mutation.claim.sequence == 1
    assert await coordinator.complete_turn_announcement(
        user_id="user-123",
        session_id=SESSION_1,
        turn_id=TURN,
        generation=3,
        claim_id=CLAIM_1,
    )
    assert repository.calls == [
        ("claim_control", "user-123"),
        ("claim_announcement", "user-123"),
    ]


@pytest.mark.asyncio
async def test_voice_coordinator_repository_failures_are_redacted() -> None:
    clock = FakeClock()
    repository = FakeCoordinatorRepository()
    repository.failure = RuntimeError("database URL and transcript must not escape")
    coordinator = VoiceCoordinator(
        WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic),
        repository,
        replica_id="orchestrator-a",
        utcnow=clock.utcnow,
    )

    with pytest.raises(
        ClaimUnavailable, match="coordinator_repository_failed"
    ) as error:
        await coordinator.claim_session_control(
            user_id="user-123", session_id=SESSION_1, generation=3
        )
    assert "database URL" not in repr(error.value)
    with pytest.raises(ClaimUnavailable, match="coordinator_repository_failed"):
        await coordinator.claim_turn_announcement(
            user_id="user-123", request=_claim_request()
        )
    with pytest.raises(ClaimUnavailable, match="coordinator_repository_failed"):
        await coordinator.release_session_control(
            user_id="user-123", session_id=SESSION_1, generation=3
        )
    with pytest.raises(ClaimUnavailable, match="coordinator_repository_failed"):
        await coordinator.complete_turn_announcement(
            user_id="user-123",
            session_id=SESSION_1,
            turn_id=TURN,
            generation=3,
            claim_id=CLAIM_1,
        )
    with pytest.raises(ValueError, match="invalid_user_id"):
        await coordinator.claim_session_control(
            user_id=" user-123", session_id=SESSION_1, generation=3
        )


def _phrase_book() -> PhraseBook:
    return PhraseBook(
        {
            "acknowledgement": ("on_it", "working_on_it", "ill_get_started"),
            "progress": ("still_working", "making_progress"),
            "waiting": ("action_needed",),
        }
    )


def test_phrase_selection_is_deterministic_bounded_and_never_repeats() -> None:
    book = _phrase_book()
    first = book.select(
        kind="acknowledgement", stable_id=TURN, sequence=1, last_phrase_key=None
    )
    assert first == book.select(
        kind="acknowledgement", stable_id=TURN, sequence=1, last_phrase_key=None
    )
    second = book.select(
        kind="acknowledgement", stable_id=TURN, sequence=2, last_phrase_key=first
    )
    assert second != first
    with pytest.raises(ValueError, match="invalid_phrase_key"):
        PhraseBook({"progress": ("not a valid key",)})
    with pytest.raises(ValueError, match="invalid_phrase_key"):
        PhraseBook({"progress": ("1_invalid", "valid_key")})
    with pytest.raises(ClaimUnavailable, match="phrase_kind_unavailable"):
        book.select(kind="failure", stable_id=TURN, sequence=1, last_phrase_key=None)


def _claim_request(
    *,
    claim_id: str = CLAIM_1,
    kind: str = "acknowledgement",
    role: str = "single",
    expected_sequence: int = 0,
    expected_reserved: int = 0,
) -> AnnouncementClaimRequest:
    return AnnouncementClaimRequest(
        session_id=SESSION_1,
        turn_id=TURN,
        generation=3,
        claim_id=claim_id,
        kind=kind,
        quantum_role=role,
        expected_sequence=expected_sequence,
        expected_result_reserved_samples=expected_reserved,
    )


def test_announcement_claim_sequence_cas_phrase_and_completion() -> None:
    adapter = AnnouncementStateAdapter(_phrase_book(), claim_ttl_seconds=5)
    initial = AnnouncementState(generation=3)
    mutation = adapter.claim(initial, _claim_request(), now=NOW)

    assert mutation.claim.sequence == 1
    assert UUID(mutation.claim.announcement_id).version == 4
    assert mutation.claim.phrase_key in {
        "on_it",
        "working_on_it",
        "ill_get_started",
    }
    assert mutation.claim.quantum_index == 0
    assert mutation.claim.result_reserved_samples_after is None
    assert mutation.state.announcement_sequence == 1
    assert mutation.state.result_reserved_samples == 0

    exact = adapter.claim(
        mutation.state,
        _claim_request(expected_sequence=1),
        now=NOW + timedelta(seconds=1),
    )
    assert exact.claim == mutation.claim
    with pytest.raises(ClaimUnavailable, match="announcement_claim_owned"):
        adapter.claim(
            mutation.state,
            _claim_request(claim_id=CLAIM_2, expected_sequence=1),
            now=NOW + timedelta(seconds=1),
        )
    completed = adapter.complete(mutation.state, generation=3, claim_id=CLAIM_1)
    assert completed.announcement_claim_id is None
    with pytest.raises(ClaimUnavailable, match="announcement_cas_miss"):
        adapter.claim(completed, _claim_request(), now=NOW)
    with pytest.raises(ClaimUnavailable, match="announcement_claim_not_owned"):
        adapter.complete(mutation.state, generation=3, claim_id=CLAIM_2)
    with pytest.raises(StaleFence, match="stale_generation"):
        adapter.complete(mutation.state, generation=4, claim_id=CLAIM_1)

    with pytest.raises(ClaimUnavailable, match="announcement_claim_mismatch"):
        adapter.claim(
            mutation.state,
            _claim_request(kind="progress", expected_sequence=1, expected_reserved=0),
            now=NOW + timedelta(seconds=1),
        )


def test_result_reservation_is_conservative_bounded_and_never_refunded() -> None:
    adapter = AnnouncementStateAdapter(_phrase_book(), claim_ttl_seconds=5)
    state = AnnouncementState(generation=3)
    opening = adapter.claim(
        state,
        _claim_request(kind="result", role="result_opening"),
        now=NOW,
    )
    assert opening.claim.quantum_index == 0
    assert opening.claim.max_duration_samples == 36_000
    assert opening.claim.result_reserved_samples_after == 36_000
    state = adapter.complete(opening.state, generation=3, claim_id=CLAIM_1)

    continuation = adapter.claim(
        state,
        _claim_request(
            claim_id=CLAIM_2,
            kind="result",
            role="result_continuation",
            expected_sequence=1,
            expected_reserved=36_000,
        ),
        now=NOW + timedelta(seconds=1),
    )
    assert continuation.claim.quantum_index == 1
    assert continuation.claim.max_duration_samples == 96_000
    assert continuation.claim.result_reserved_samples_after == 132_000
    # Failure/interruption completes ownership but deliberately retains reservation.
    state = adapter.complete(continuation.state, generation=3, claim_id=CLAIM_2)
    assert state.result_reserved_samples == 132_000
    assert state.result_quantum_count == 2

    full = replace(
        state,
        result_reserved_samples=708_000,
        result_quantum_count=31,
        announcement_sequence=31,
    )
    with pytest.raises(ClaimUnavailable, match="result_sample_budget_exhausted"):
        adapter.claim(
            full,
            _claim_request(
                kind="result",
                role="result_continuation",
                expected_sequence=31,
                expected_reserved=708_000,
            ),
            now=NOW,
        )


def test_expired_claim_recovers_same_id_sequence_phrase_and_reservation() -> None:
    adapter = AnnouncementStateAdapter(_phrase_book(), claim_ttl_seconds=5)
    first = adapter.claim(
        AnnouncementState(generation=3),
        _claim_request(kind="result", role="result_opening"),
        now=NOW,
    )

    recovered = adapter.claim(
        first.state,
        _claim_request(
            claim_id=CLAIM_2,
            kind="result",
            role="result_opening",
            expected_sequence=1,
            expected_reserved=36_000,
        ),
        now=NOW + timedelta(seconds=5),
    )

    assert recovered.claim.recovered is True
    assert recovered.claim.announcement_id == first.claim.announcement_id
    assert recovered.claim.sequence == 1
    assert recovered.claim.result_reserved_samples_after == 36_000
    assert recovered.state.result_quantum_count == 1
    with pytest.raises(ClaimUnavailable, match="claim_recovery_required"):
        adapter.claim(
            first.state,
            _claim_request(
                claim_id=CLAIM_2,
                kind="progress",
                expected_sequence=1,
                expected_reserved=36_000,
            ),
            now=NOW + timedelta(seconds=5),
        )


def test_announcement_stale_and_lifecycle_fences_fail_closed() -> None:
    adapter = AnnouncementStateAdapter(_phrase_book(), claim_ttl_seconds=5)
    with pytest.raises(StaleFence, match="stale_generation"):
        adapter.claim(AnnouncementState(generation=4), _claim_request(), now=NOW)
    for state, reason in (
        (AnnouncementState(generation=3, terminal=True), "announcement_terminal"),
        (
            AnnouncementState(generation=3, speech_enabled=False),
            "speech_disabled",
        ),
        (
            AnnouncementState(generation=3, origin_available=False),
            "origin_unavailable",
        ),
    ):
        with pytest.raises(ClaimUnavailable, match=reason):
            adapter.claim(state, _claim_request(), now=NOW)


def test_coordinator_state_objects_reject_malformed_persisted_values() -> None:
    with pytest.raises(ValueError, match="invalid_transport"):
        replace(_request(SESSION_1), transport="carrier_pigeon")
    with pytest.raises(ValueError, match="invalid_control_lease_state"):
        ControlLeaseState(generation=1, owner_id="replica-a")
    with pytest.raises(ValueError, match="invalid_last_announcement_kind"):
        AnnouncementState(generation=1, last_announcement_kind="invented")
    with pytest.raises(ValueError, match="invalid_last_phrase_key"):
        AnnouncementState(generation=1, last_phrase_key="not valid")
    with pytest.raises(ValueError, match="invalid_announcement_claim_state"):
        AnnouncementState(generation=1, announcement_claim_id=CLAIM_1)
    with pytest.raises(ValueError, match="invalid_announcement_kind"):
        replace(_claim_request(), kind="greeting")
    with pytest.raises(ValueError, match="invalid_quantum_role"):
        replace(_claim_request(), quantum_role="unbounded")
    with pytest.raises(ValueError, match="invalid_announcement_claim_ttl"):
        AnnouncementStateAdapter(_phrase_book(), claim_ttl_seconds=31)

    adapter = AnnouncementStateAdapter(_phrase_book())
    with pytest.raises(ClaimUnavailable, match="invalid_result_quantum"):
        adapter.claim(
            AnnouncementState(generation=3),
            _claim_request(kind="result", role="result_continuation"),
            now=NOW,
        )
    with pytest.raises(ClaimUnavailable, match="invalid_single_quantum"):
        adapter.claim(
            AnnouncementState(generation=3),
            _claim_request(kind="progress", role="result_opening"),
            now=NOW,
        )
    with pytest.raises(ClaimUnavailable, match="result_quantum_budget_exhausted"):
        adapter.claim(
            AnnouncementState(
                generation=3,
                announcement_sequence=32,
                result_reserved_samples=708_000,
                result_quantum_count=32,
            ),
            _claim_request(
                kind="result",
                role="result_continuation",
                expected_sequence=32,
                expected_reserved=708_000,
            ),
            now=NOW,
        )
