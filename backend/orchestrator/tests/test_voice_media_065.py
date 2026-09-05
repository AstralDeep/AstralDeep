"""Direct-RTC media activation tests for Feature 065."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.voice_bootstrap import VoiceServices
from orchestrator.voice_control_binding import VoiceControlClaims
from orchestrator.voice_coordinator import (
    AnnouncementClaim,
    SessionReservation,
    deterministic_uuid4,
)
from orchestrator.voice_media import DirectRtcVoiceMedia, VoiceMediaActivationError
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.voice_runtime import VoiceSessionRuntime
from orchestrator.voice_sessions import VoiceSessionRecord, VoiceTurnRecord
from orchestrator.voice_worker_endpoint import WorkerControlSettings
from shared.protocol import VoicePlayoutEvent
from shared.watch_ticket import (
    derive_watch_nonce,
    verify_watch_ticket,
    watch_participant_identity,
)


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
SESSION = "00000000-0000-4000-8000-000000000001"
ASSIGNMENT = "00000000-0000-4000-8000-000000000002"
CONNECTION = "00000000-0000-4000-8000-000000000003"
DEVICE = "00000000-0000-4000-8000-000000000004"
BINDING = "00000000-0000-4000-8000-000000000005"
CHAT = "00000000-0000-4000-8000-000000000006"
TURN = "00000000-0000-4000-8000-000000000008"
CLIENT_TURN = "00000000-0000-4000-8000-000000000009"
REFRESH = "00000000-0000-4000-8000-000000000013"
SECOND_SESSION = "00000000-0000-4000-8000-000000000031"
SECOND_ASSIGNMENT = "00000000-0000-4000-8000-000000000032"
SECOND_CONNECTION = "00000000-0000-4000-8000-000000000033"
SECOND_DEVICE = "00000000-0000-4000-8000-000000000034"
SECOND_BINDING = "00000000-0000-4000-8000-000000000035"
SECOND_CHAT = "00000000-0000-4000-8000-000000000036"


class _Workers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_command: str | None = None
        self.media_apply_gate = None
        self.assignment_current = True
        self.reservation = SessionReservation(
            session_id=SESSION,
            generation=1,
            assignment_id=ASSIGNMENT,
            worker_identity="voice-worker-a",
            connection_id=CONNECTION,
            worker_rtc_grant_revision=1,
        )

    async def reserve_session(self, request):
        self.calls.append(("reserve", request))
        return self.reservation

    async def deliver_session_bind(self, reservation, request, grant):
        self.calls.append(("bind", grant["join_token"]))
        return {"type": "session_bind"}

    async def await_session_ready(self, **kwargs):
        self.calls.append(("ready", kwargs))

    async def send_session_command(self, reservation, frame_type, fields):
        self.calls.append((frame_type, fields))
        if frame_type == self.fail_command:
            raise RuntimeError("synthetic worker transport detail")
        return {"type": frame_type}

    async def await_media_grant_applied(self, **kwargs):
        self.calls.append(("media_applied", kwargs))
        if self.media_apply_gate is not None:
            await self.media_apply_gate.wait()

    async def release_session(self, session_id, generation, assignment_id):
        self.calls.append(("release", (session_id, generation, assignment_id)))
        return True

    async def current_reservation(self, *, session_id, generation):
        self.calls.append(("current_reservation", (session_id, generation)))
        if not self.assignment_current:
            raise RuntimeError("stale_assignment")
        return self.reservation


class _BlockingCaptureWorkers(_Workers):
    def __init__(self) -> None:
        super().__init__()
        self.second_reservation = SessionReservation(
            session_id=SECOND_SESSION,
            generation=1,
            assignment_id=SECOND_ASSIGNMENT,
            worker_identity="voice-worker-b",
            connection_id=SECOND_CONNECTION,
            worker_rtc_grant_revision=1,
        )
        self.block_capture_session: str | None = None
        self.capture_started = asyncio.Event()
        self.capture_release = asyncio.Event()
        self.capture_command_sessions: list[str] = []

    async def reserve_session(self, request):
        self.calls.append(("reserve", request))
        if request.session_id == SECOND_SESSION:
            return self.second_reservation
        return self.reservation

    async def send_session_command(self, reservation, frame_type, fields):
        self.calls.append((frame_type, fields))
        if frame_type == self.fail_command:
            raise RuntimeError("synthetic worker transport detail")
        if frame_type == "set_capture":
            self.capture_command_sessions.append(reservation.session_id)
        if (
            frame_type == "set_capture"
            and reservation.session_id == self.block_capture_session
        ):
            self.capture_started.set()
            await self.capture_release.wait()
        return {"type": frame_type}


class _LiveKit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def mint_worker_grant(self, **kwargs):
        self.calls.append(("worker_grant", kwargs))
        return {
            "revision": 1,
            "livekit_url": "ws://livekit:7880",
            "join_token": "worker-secret-token-value-that-is-long-enough",
            "issued_at": "2026-07-31T18:00:00Z",
            "expires_at": "2026-07-31T18:05:00Z",
            "room_name": kwargs["room_name"],
            "worker_identity": kwargs["worker_identity"],
        }

    async def ensure_room(self, room_name):
        self.calls.append(("ensure", room_name))

    def mint_client_grant(self, **kwargs):
        self.calls.append(("client_grant", kwargs))
        return {
            "grant_id": kwargs["grant_id"],
            "transport": "livekit",
            "join_token": "client-secret-token-value-that-is-long-enough",
        }

    async def remove_participant(self, room_name, identity):
        self.calls.append(("remove", (room_name, identity)))

    async def delete_room(self, room_name):
        self.calls.append(("delete", room_name))


def _session(**changes: Any) -> VoiceSessionRecord:
    values = {
        "session_id": SESSION,
        "user_id": "user-a",
        "activation_id": "00000000-0000-4000-8000-000000000007",
        "device_id": DEVICE,
        "device_kind": "web",
        "transport": "livekit",
        "room_name": "voice-room-a",
        "participant_identity": "voice-client-a",
        "worker_identity": None,
        "visible_chat_id": CHAT,
        "chat_context_revision": 1,
        "applied_visible_chat_id": None,
        "applied_chat_context_revision": None,
        "state": "starting",
        "speech_muted": False,
        "microphone_enabled": True,
        "foreground_active": True,
        "foreground_reason": "foreground",
        "generation": 1,
        "media_grant_revision": 1,
        "owner_connection_generation": CONNECTION,
        "control_binding_id": BINDING,
        "control_binding_expires_at": NOW + timedelta(minutes=10),
        "lease_expires_at": NOW + timedelta(seconds=45),
        "control_owner_id": None,
        "control_lease_expires_at": None,
        "last_interaction_at": NOW,
        "idle_started_at": None,
        "started_at": NOW,
        "updated_at": NOW,
        "ended_at": None,
        "end_reason": None,
        "chat_unavailable_at": None,
        "takeover_of_session_id": None,
        "media_grant_nonce_hash": b"m" * 32,
        "media_grant_expires_at": NOW + timedelta(minutes=5),
        "media_grant_consumed_at": None,
        "last_media_refresh_id": None,
        "media_grant_issued_at": NOW,
        "worker_assignment_id": None,
        "worker_rtc_grant_revision": 1,
        "worker_rtc_grant_issued_at": None,
        "worker_rtc_grant_expires_at": None,
    }
    values.update(changes)
    return VoiceSessionRecord(**values)


def _second_session() -> VoiceSessionRecord:
    return _session(
        session_id=SECOND_SESSION,
        user_id="user-b",
        activation_id="00000000-0000-4000-8000-000000000037",
        device_id=SECOND_DEVICE,
        room_name="voice-room-b",
        participant_identity="voice-client-b",
        visible_chat_id=SECOND_CHAT,
        owner_connection_generation=SECOND_CONNECTION,
        control_binding_id=SECOND_BINDING,
        media_grant_nonce_hash=b"n" * 32,
    )


def _turn(**changes: Any) -> VoiceTurnRecord:
    values = {
        "turn_id": TURN,
        "client_turn_id": CLIENT_TURN,
        "session_id": SESSION,
        "session_generation": 1,
        "media_grant_revision": 1,
        "user_id": "user-a",
        "chat_id": CHAT,
        "chat_context_revision": 1,
        "execution_base_render_revision": 0,
        "submission_id": "00000000-0000-4000-8000-000000000010",
        "request_generation": "00000000-0000-4000-8000-000000000011",
        "message_id": 1,
        "acceptance_commit_id": None,
        "result_commit_id": None,
        "operation_id": None,
        "state": "processing",
        "is_foreground": True,
        "detected_language": "en",
        "spoken_output_policy": "full_recap",
        "output_reason": "ready",
        "terminal_kind": None,
        "rejection_reason": None,
        "rejection_retry_policy": None,
        "recap_source": "none",
        "sensitivity": "unknown",
        "announcement_sequence": 1,
        "result_reserved_samples": 0,
        "result_quantum_count": 0,
        "last_phrase_key": "on_it",
        "next_announcement_due_at": NOW + timedelta(seconds=14),
        "accepted_at": NOW,
        "processing_started_at": NOW,
        "terminal_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return VoiceTurnRecord(**values)


def _claim() -> AnnouncementClaim:
    return AnnouncementClaim(
        claim_id="00000000-0000-4000-8000-000000000012",
        announcement_id=deterministic_uuid4(
            "voice-announcement-v1",
            SESSION,
            TURN,
            "1",
            "1",
            "acknowledgement",
            "single",
            "0",
        ),
        sequence=1,
        kind="acknowledgement",
        quantum_role="single",
        quantum_index=0,
        max_duration_samples=96_000,
        result_reserved_samples_after=None,
        phrase_key="on_it",
        claim_expires_at=NOW + timedelta(seconds=5),
    )


def _playout(
    claim: AnnouncementClaim,
    *,
    phase: str,
    sequence: int,
    **changes: Any,
) -> VoicePlayoutEvent:
    values = {
        "type": "voice_playout_event",
        "schema_version": "1",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "session_id": SESSION,
        "generation": 1,
        "media_grant_revision": 1,
        "announcement_id": claim.announcement_id,
        "announcement_sequence": claim.sequence,
        "turn_id": TURN,
        "kind": claim.kind,
        "quantum_role": claim.quantum_role,
        "quantum_index": claim.quantum_index,
        "phase": phase,
        "client_sequence": sequence,
        "observed_at": "2099-01-01T00:00:00Z",
    }
    values.update(changes)
    return VoicePlayoutEvent.from_dict(values)


def _source_lifecycle(
    claim: AnnouncementClaim,
    phase: str,
    *,
    announcement_sequence: int | None = None,
) -> dict[str, Any]:
    return {
        "type": f"speech_{phase}",
        "session_id": SESSION,
        "generation": 1,
        "media_grant_revision": 1,
        "announcement_id": claim.announcement_id,
        "announcement_sequence": (
            claim.sequence
            if announcement_sequence is None
            else announcement_sequence
        ),
        "turn_id": TURN,
        "kind": claim.kind,
        "quantum_role": claim.quantum_role,
        "quantum_index": claim.quantum_index,
        "result_reserved_samples_after": claim.result_reserved_samples_after,
    }


@pytest.mark.asyncio
async def test_activation_waits_for_worker_and_context_before_client_grant() -> None:
    workers = _Workers()
    livekit = _LiveKit()
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=workers,
        clock=lambda: NOW,
    )
    receipt = await media.activate(_session())

    assert receipt.assignment_id == ASSIGNMENT
    assert await media.current_session(SESSION, 1) == replace(
        _session(),
        worker_identity="voice-worker-a",
        worker_assignment_id=ASSIGNMENT,
        worker_rtc_grant_issued_at=NOW,
        worker_rtc_grant_expires_at=NOW + timedelta(minutes=5),
    )
    assert await media.current_session(SESSION, 2) is None
    assert receipt.client_grant["transport"] == "livekit"
    assert [name for name, _ in workers.calls] == [
        "reserve",
        "bind",
        "ready",
        "set_capture",
    ]
    ready = workers.calls[-2][1]
    assert ready["visible_chat_id"] == CHAT
    assert ready["chat_context_revision"] == 1
    assert ready["timeout_seconds"] == 8.0
    assert [name for name, _ in livekit.calls] == [
        "ensure",
        "worker_grant",
        "client_grant",
    ]


@pytest.mark.asyncio
async def test_blocked_capture_command_does_not_delay_another_session() -> None:
    workers = _BlockingCaptureWorkers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
    )
    first_session = _session()
    second_session = _second_session()
    await media.activate(first_session)
    await media.activate(second_session)
    workers.block_capture_session = SESSION

    blocked = asyncio.create_task(media.set_capture(first_session, False))
    await workers.capture_started.wait()
    try:
        await asyncio.wait_for(
            media.set_capture(second_session, False),
            timeout=0.2,
        )
        assert not blocked.done()
        assert workers.capture_command_sessions[-1] == SECOND_SESSION
    finally:
        workers.capture_release.set()
        await asyncio.gather(blocked, return_exceptions=True)


@pytest.mark.asyncio
async def test_blocked_playout_release_does_not_delay_another_session() -> None:
    workers = _BlockingCaptureWorkers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    first_session = _session()
    second_session = _second_session()
    await media.activate(first_session)
    await media.activate(second_session)
    turn = _turn()
    claim = _claim()
    await media.speak_turn(turn, claim, text="On it!")
    binding = VoiceControlClaims(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        binding_id=BINDING,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(claim, phase="started", sequence=1),
        session=first_session,
    )
    await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(claim, phase="finished", sequence=2),
        session=first_session,
    )
    workers.block_capture_session = SESSION

    blocked_release = asyncio.create_task(
        media.handle_worker_frame(_source_lifecycle(claim, "finished"))
    )
    await workers.capture_started.wait()
    same_session_update: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(
            media.set_capture(second_session, False),
            timeout=0.2,
        )
        assert not blocked_release.done()
        assert workers.capture_command_sessions[-1] == SECOND_SESSION
        same_session_update = asyncio.create_task(
            media.set_capture(first_session, False)
        )
        await asyncio.sleep(0)
        assert not same_session_update.done()
    finally:
        workers.capture_release.set()
        await asyncio.gather(
            blocked_release,
            *(
                (same_session_update,)
                if same_session_update is not None
                else ()
            ),
            return_exceptions=True,
        )

    assert (SESSION, 1) not in media._capture_playout_holds
    assert workers.capture_command_sessions[-3:] == [
        SESSION,
        SECOND_SESSION,
        SESSION,
    ]


@pytest.mark.asyncio
async def test_failed_playout_release_retains_exact_hold_for_retry() -> None:
    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    session = _session()
    await media.activate(session)
    turn = _turn()
    claim = _claim()
    await media.speak_turn(turn, claim, text="On it!")
    binding = VoiceControlClaims(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        binding_id=BINDING,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(claim, phase="started", sequence=1),
        session=session,
    )
    await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(claim, phase="finished", sequence=2),
        session=session,
    )
    workers.fail_command = "set_capture"

    with pytest.raises(RuntimeError, match="synthetic worker transport detail"):
        await media.handle_worker_frame(_source_lifecycle(claim, "finished"))
    assert media._capture_playout_holds[(SESSION, 1)] == claim.announcement_id

    workers.fail_command = None
    await media.handle_worker_frame(_source_lifecycle(claim, "finished"))
    assert (SESSION, 1) not in media._capture_playout_holds


@pytest.mark.asyncio
async def test_assignment_liveness_requires_matching_local_and_pool_fences() -> None:
    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
    )
    await media.activate(_session())
    active = await media.current_session(SESSION, 1)
    assert active is not None

    assert await media.assignment_is_current(
        active,
        assignment_id=ASSIGNMENT,
        worker_identity="voice-worker-a",
    )
    assert not await media.assignment_is_current(
        active,
        assignment_id="00000000-0000-4000-8000-000000000099",
        worker_identity="voice-worker-a",
    )
    workers.assignment_current = False
    assert not await media.assignment_is_current(
        active,
        assignment_id=ASSIGNMENT,
        worker_identity="voice-worker-a",
    )


@pytest.mark.parametrize("ready_timeout_seconds", (0.99, 15.01))
def test_worker_ready_timeout_stays_inside_launch_bounds(
    ready_timeout_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="invalid_worker_ready_timeout"):
        DirectRtcVoiceMedia(
            livekit=_LiveKit(),
            workers=_Workers(),
            ready_timeout_seconds=ready_timeout_seconds,
        )


@pytest.mark.asyncio
async def test_control_commands_share_one_assignment_and_end_releases_room() -> None:
    workers = _Workers()
    livekit = _LiveKit()
    metrics = RuntimeObservability(deployment_instance="test")
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=workers,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
        observability=metrics,
    )
    session = _session()
    await media.activate(session)
    await media.apply_context(session)
    await media.set_capture(session, False)
    await media.stop_speech(session)
    await media.end(session, "user")

    names = [name for name, _ in workers.calls]
    assert "session_context_update" in names
    assert "set_capture" in names
    assert "stop_speech" in names
    assert "end_session" in names
    assert names[-1] == "release"
    assert livekit.calls[-2:] == [
        ("remove", ("voice-room-a", "voice-client-a")),
        ("delete", "voice-room-a"),
    ]
    snapshot = metrics.snapshot()
    assert {sample.name for sample in snapshot} == {
        "voice_cleanup_seconds",
        "voice_cleanup_total",
        "voice_interruption_total",
    }
    assert SESSION not in repr(snapshot)


@pytest.mark.asyncio
async def test_authenticated_runtime_stop_barges_in_before_capture_can_be_reenabled() -> (
    None
):
    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    session = _session()
    await media.activate(session)
    active = await media.current_session(SESSION, 1)
    assert active is not None
    await media.speak_turn(_turn(), _claim(), text="On it!")
    assert media._capture_playout_holds[(SESSION, 1)] == _claim().announcement_id

    class Repository:
        def get_controlled_session(self, **kwargs):
            assert kwargs["user_id"] == "user-a"
            assert kwargs["session_id"] == SESSION
            assert kwargs["expected_generation"] == 1
            assert kwargs["expected_media_grant_revision"] == 1
            assert kwargs["control"].device_id == DEVICE
            assert kwargs["control"].connection_generation == CONNECTION
            return active

    repository = Repository()
    runtime = VoiceSessionRuntime(
        repository=repository,  # type: ignore[arg-type]
        capability=object(),
        media=media,
        replica_id="replica-a",
        clock=lambda: NOW,
    )
    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=workers,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        media=media,
        runtime=runtime,
        backend_selection=runtime.backend_selection,
        speech_backend=runtime.speech_backend,
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )
    runtime.bind_speech_stop_handler(services.stop_session_speech)
    runner = await services._announcement_runner(_turn())

    try:
        capture_commands_before = len(
            [name for name, _fields in workers.calls if name == "set_capture"]
        )
        await runtime.stop_speech(
            user_id="user-a",
            session_id=SESSION,
            control={
                "subject": "user-a",
                "device_id": DEVICE,
                "connection_generation": CONNECTION,
                "binding_id": BINDING,
                "binding_expires_at": NOW + timedelta(minutes=10),
            },
            request={
                "expected_generation": 1,
                "expected_media_grant_revision": 1,
            },
        )

        assert not runner.task.done()
        assert workers.calls[-1] == (
            "stop_speech",
            {"media_grant_revision": 1, "reason": "barge_in"},
        )
        assert (SESSION, 1) not in media._capture_playout_holds
        assert len(
            [name for name, _fields in workers.calls if name == "set_capture"]
        ) == capture_commands_before

        # A later authenticated enable must reach the worker. A stale
        # coordinator playout hold would swallow it and strand capture closed.
        await media.set_capture(active, True)
        assert workers.calls[-1] == (
            "set_capture",
            {"media_grant_revision": 1, "enabled": True},
        )
    finally:
        await services._close_announcement_runner(SESSION, 1)


@pytest.mark.asyncio
async def test_lifecycle_stop_retains_playout_hold_for_authenticated_terminal() -> None:
    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
    )
    session = _session()
    await media.activate(session)
    active = await media.current_session(SESSION, 1)
    assert active is not None
    claim = _claim()
    await media.speak_turn(_turn(), claim, text="On it!")

    await media.stop_speech(active)

    assert workers.calls[-1] == (
        "stop_speech",
        {"media_grant_revision": 1, "reason": "user_stop"},
    )
    assert media._capture_playout_holds[(SESSION, 1)] == claim.announcement_id


@pytest.mark.asyncio
async def test_greeting_waits_for_listening_and_is_sent_exactly_once() -> None:
    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
    )
    session = _session()
    await media.activate(session)
    frame = {
        "type": "media_state",
        "session_id": session.session_id,
        "generation": session.generation,
        "state": "listening",
    }

    await media.handle_worker_frame({**frame, "state": "connecting"})
    await media.handle_worker_frame(frame)
    await media.handle_worker_frame(frame)

    speaks = [fields for name, fields in workers.calls if name == "speak"]
    assert len(speaks) == 1
    assert speaks[0] == {
        "announcement_id": "e2a710b6-dd86-4f16-ba9a-8cef866ffc99",
        "announcement_sequence": 1,
        "media_grant_revision": 1,
        "transport": "livekit",
        "turn_id": None,
        "kind": "greeting",
        "quantum_role": "single",
        "quantum_index": 0,
        "max_duration_samples": 96_000,
        "phrase_key": "hello_ready",
        "text": "Hi! I'm ready when you are.",
        "sensitive_authorized": False,
        "expires_at": "2026-07-31T18:00:30.000Z",
    }


@pytest.mark.asyncio
async def test_wire_sequences_do_not_collide_between_greeting_or_turns() -> None:
    """A turn-local claim sequence may reset without replaying on the wire."""

    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    session = _session()
    await media.activate(session)
    await media.handle_worker_frame(
        {
            "type": "media_state",
            "session_id": SESSION,
            "generation": 1,
            "state": "listening",
        }
    )

    first_turn = _turn()
    first_ack = _claim()
    await media.speak_turn(first_turn, first_ack, text="On it!")
    first_progress = replace(
        first_ack,
        claim_id="00000000-0000-4000-8000-000000000022",
        announcement_id=deterministic_uuid4(
            "voice-announcement-v1",
            SESSION,
            TURN,
            "2",
            "2",
            "progress",
            "single",
            "0",
        ),
        sequence=2,
        kind="progress",
        phrase_key="still_working",
    )
    await media.speak_turn(first_turn, first_progress, text="Still working on it.")

    second_turn_id = "00000000-0000-4000-8000-000000000028"
    second_turn = _turn(
        turn_id=second_turn_id,
        client_turn_id="00000000-0000-4000-8000-000000000029",
        submission_id="00000000-0000-4000-8000-000000000030",
        request_generation="00000000-0000-4000-8000-000000000031",
        announcement_sequence=1,
    )
    second_ack = replace(
        first_ack,
        claim_id="00000000-0000-4000-8000-000000000032",
        announcement_id=deterministic_uuid4(
            "voice-announcement-v1",
            SESSION,
            second_turn_id,
            "1",
            "1",
            "acknowledgement",
            "single",
            "0",
        ),
    )
    await media.speak_turn(second_turn, second_ack, text="I'll get started.")

    speaks = [fields for name, fields in workers.calls if name == "speak"]
    assert [fields["announcement_sequence"] for fields in speaks] == [1, 2, 3, 4]
    assert [
        first_ack.sequence,
        first_progress.sequence,
        second_ack.sequence,
    ] == [1, 2, 1]

    binding = VoiceControlClaims(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        binding_id=BINDING,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    started = await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(
            first_ack,
            phase="started",
            sequence=1,
            announcement_sequence=2,
        ),
        session=session,
    )
    finished = await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(
            first_ack,
            phase="finished",
            sequence=2,
            announcement_sequence=2,
        ),
        session=session,
    )
    assert started.fence.announcement_sequence == 2
    assert started.turn_announcement_sequence == 1
    assert finished.turn_announcement_sequence == 1

    terminal = asyncio.create_task(
        media.await_speech_terminal(first_ack.announcement_id)
    )
    await media.handle_worker_frame(
        _source_lifecycle(
            first_ack,
            "finished",
            announcement_sequence=2,
        )
    )
    assert await asyncio.wait_for(terminal, timeout=0.1) == "speech_finished"

    with pytest.raises(
        VoiceMediaActivationError,
        match="announcement_already_inflight",
    ):
        await media.speak_turn(first_turn, first_progress, text="Still working on it.")
    assert media._media_announcement_sequences[(SESSION, 1)] == 4

    active = await media.current_session(SESSION, 1)
    assert active is not None
    rotated = replace(
        active,
        participant_identity="voice-client-next",
        media_grant_revision=2,
        media_grant_nonce_hash=b"n" * 32,
        media_grant_issued_at=NOW + timedelta(seconds=1),
        media_grant_expires_at=NOW + timedelta(minutes=4),
        last_media_refresh_id=REFRESH,
    )
    await media.rotate_media_grant(active, rotated, refresh_id=REFRESH)
    refreshed_turn_id = "00000000-0000-4000-8000-000000000038"
    refreshed_turn = _turn(
        turn_id=refreshed_turn_id,
        client_turn_id="00000000-0000-4000-8000-000000000039",
        submission_id="00000000-0000-4000-8000-000000000040",
        request_generation="00000000-0000-4000-8000-000000000041",
        media_grant_revision=2,
        announcement_sequence=1,
    )
    refreshed_ack = replace(
        first_ack,
        claim_id="00000000-0000-4000-8000-000000000042",
        announcement_id=deterministic_uuid4(
            "voice-announcement-v1",
            SESSION,
            refreshed_turn_id,
            "1",
            "1",
            "acknowledgement",
            "single",
            "0",
        ),
    )
    await media.speak_turn(
        refreshed_turn,
        refreshed_ack,
        text="On it!",
    )
    assert [
        fields["announcement_sequence"]
        for name, fields in workers.calls
        if name == "speak"
    ] == [1, 2, 3, 4, 5]

    await media.end(rotated, "user")
    assert (SESSION, 1) not in media._media_announcement_sequences


@pytest.mark.asyncio
async def test_concurrent_greeting_and_turn_send_in_wire_sequence_order() -> None:
    class BlockingWorkers(_Workers):
        def __init__(self) -> None:
            super().__init__()
            self.first_speak_started = asyncio.Event()
            self.release_first_speak = asyncio.Event()
            self.speak_count = 0

        async def send_session_command(self, reservation, frame_type, fields):
            self.calls.append((frame_type, fields))
            if frame_type != "speak":
                return {"type": frame_type}
            self.speak_count += 1
            if self.speak_count == 1:
                self.first_speak_started.set()
                await self.release_first_speak.wait()
            return {"type": frame_type}

    workers = BlockingWorkers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
    )
    session = _session()
    await media.activate(session)
    greeting = asyncio.create_task(
        media.handle_worker_frame(
            {
                "type": "media_state",
                "session_id": SESSION,
                "generation": 1,
                "state": "listening",
            }
        )
    )
    await workers.first_speak_started.wait()
    acknowledgement = asyncio.create_task(
        media.speak_turn(_turn(), _claim(), text="On it!")
    )
    await asyncio.sleep(0)
    assert [name for name, _fields in workers.calls].count("speak") == 1

    workers.release_first_speak.set()
    await asyncio.gather(greeting, acknowledgement)
    speaks = [fields for name, fields in workers.calls if name == "speak"]
    assert [fields["kind"] for fields in speaks] == [
        "greeting",
        "acknowledgement",
    ]
    assert [fields["announcement_sequence"] for fields in speaks] == [1, 2]

    await media.end(session, "user")
    assert (SESSION, 1) not in media._announcement_send_locks
    assert (SESSION, 1) not in media._capture_command_locks


@pytest.mark.asyncio
async def test_failed_greeting_send_can_retry_without_replaying_success() -> None:
    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
    )
    session = _session()
    await media.activate(session)
    frame = {
        "type": "media_state",
        "session_id": session.session_id,
        "generation": session.generation,
        "state": "listening",
    }
    workers.fail_command = "speak"
    await media.handle_worker_frame(frame)
    workers.fail_command = None
    await media.handle_worker_frame(frame)
    await media.handle_worker_frame(frame)

    speaks = [fields for name, fields in workers.calls if name == "speak"]
    assert [fields["announcement_sequence"] for fields in speaks] == [1, 2]


@pytest.mark.asyncio
async def test_end_removes_participant_and_room_when_worker_command_fails() -> None:
    workers = _Workers()
    livekit = _LiveKit()
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=workers,
        clock=lambda: NOW,
    )
    session = _session()
    await media.activate(session)
    workers.fail_command = "end_session"

    with pytest.raises(RuntimeError, match="synthetic worker transport detail"):
        await media.end(session, "user")

    assert workers.calls[-1][0] == "release"
    assert livekit.calls[-2:] == [
        ("remove", ("voice-room-a", "voice-client-a")),
        ("delete", "voice-room-a"),
    ]


@pytest.mark.asyncio
async def test_missing_exact_readiness_seam_fails_before_client_grant_and_releases() -> None:
    workers = _Workers()
    workers.await_session_ready = None
    livekit = _LiveKit()
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=workers,
        clock=lambda: NOW,
    )
    with pytest.raises(
        VoiceMediaActivationError,
        match="worker_readiness_unavailable",
    ):
        await media.activate(_session())
    assert workers.calls[-1][0] == "release"
    assert not any(name == "client_grant" for name, _ in livekit.calls)


@pytest.mark.asyncio
async def test_watch_transport_fails_closed_until_bounded_bridge_exists() -> None:
    workers = _Workers()
    livekit = _LiveKit()
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=workers,
        clock=lambda: NOW,
    )
    with pytest.raises(VoiceMediaActivationError, match="watch_bridge_unavailable"):
        await media.activate(_session(transport="watch_pcm_websocket"))
    assert workers.calls == []
    assert livekit.calls == []


@pytest.mark.asyncio
async def test_watch_transport_mints_exact_one_time_ticket_without_livekit_bearer() -> None:
    secret = b"watch-ticket-test-secret-that-is-long-enough"
    activation_id = "00000000-0000-4000-8000-000000000007"
    nonce = derive_watch_nonce(
        secret,
        user_id="user-a",
        session_key=activation_id,
        generation=1,
        media_grant_revision=1,
        device_id=DEVICE,
        connection_generation=CONNECTION,
    )
    workers = _Workers()
    livekit = _LiveKit()
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=workers,
        clock=lambda: NOW,
        watch_ticket_secret=secret,
        watch_bridge_url="wss://voice.example.invalid/api/voice/watch-bridge",
    )
    receipt = await media.activate(
        _session(
            device_kind="watchos",
            transport="watch_pcm_websocket",
            participant_identity=watch_participant_identity(nonce),
            media_grant_nonce_hash=hashlib.sha256(nonce).digest(),
        )
    )
    grant = receipt.client_grant
    assert grant["transport"] == "watch_pcm_websocket"
    assert grant["url"] == "wss://voice.example.invalid/api/voice/watch-bridge"
    assert "join_token" not in grant
    claims = verify_watch_ticket(
        grant["ticket"],
        secret,
        now=NOW,
        expected_worker_identity=receipt.worker_identity,
    )
    assert claims.session_id == SESSION
    assert claims.device_id == DEVICE
    assert claims.connection_generation == CONNECTION


@pytest.mark.asyncio
async def test_exact_refresh_retry_redrives_pending_worker_without_second_revision() -> None:
    workers = _Workers()
    workers.media_apply_gate = asyncio.Event()
    livekit = _LiveKit()
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=workers,
        clock=lambda: NOW,
    )
    await media.activate(_session())
    previous = await media.current_session(SESSION, 1)
    assert previous is not None
    assert previous.worker_identity == "voice-worker-a"
    rotated = replace(
        previous,
        participant_identity="voice-client-next",
        media_grant_revision=2,
        media_grant_nonce_hash=b"n" * 32,
        media_grant_issued_at=NOW + timedelta(seconds=1),
        media_grant_expires_at=NOW + timedelta(minutes=4),
        last_media_refresh_id=REFRESH,
    )
    first = asyncio.create_task(
        media.rotate_media_grant(previous, rotated, refresh_id=REFRESH)
    )
    while len(
        [call for call in workers.calls if call[0] == "media_grant_rotated"]
    ) < 1:
        await asyncio.sleep(0)
    replay = asyncio.create_task(
        media.rotate_media_grant(rotated, rotated, refresh_id=REFRESH)
    )
    while len(
        [call for call in workers.calls if call[0] == "media_grant_rotated"]
    ) < 2:
        await asyncio.sleep(0)
    workers.media_apply_gate.set()
    first_grant, replay_grant = await asyncio.gather(first, replay)
    assert first_grant == replay_grant
    rotations = [
        fields
        for name, fields in workers.calls
        if name == "media_grant_rotated"
    ]
    assert len(rotations) == 2
    assert {fields["media_grant_revision"] for fields in rotations} == {2}


@pytest.mark.asyncio
async def test_client_playout_is_bound_to_exact_owner_command_and_server_time() -> None:
    workers = _Workers()
    media = DirectRtcVoiceMedia(
        livekit=_LiveKit(),
        workers=workers,
        clock=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    session = _session()
    await media.activate(session)
    turn = _turn()
    claim = _claim()
    await media.speak_turn(turn, claim, text="On it!")
    binding = VoiceControlClaims(
        subject="user-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        binding_id=BINDING,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )

    with pytest.raises(
        VoiceMediaActivationError,
        match="owner_connection_mismatch",
    ):
        await media.accept_client_playout(
            user_id="another-user",
            claims=binding,
            event=_playout(claim, phase="started", sequence=1),
            session=session,
        )
    with pytest.raises(
        VoiceMediaActivationError,
        match="playout_fence_mismatch",
    ):
        await media.accept_client_playout(
            user_id="user-a",
            claims=binding,
            event=_playout(
                claim,
                phase="started",
                sequence=1,
                announcement_sequence=2,
            ),
            session=session,
        )

    started = await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(claim, phase="started", sequence=1),
        session=session,
    )
    assert started.received_at == NOW
    assert started.phase == "started"
    assert started.fence.announcement_id == claim.announcement_id
    with pytest.raises(
        VoiceMediaActivationError,
        match="client_sequence_out_of_order",
    ):
        await media.accept_client_playout(
            user_id="user-a",
            claims=binding,
            event=_playout(claim, phase="finished", sequence=1),
            session=session,
        )
    finished = await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(claim, phase="finished", sequence=2),
        session=session,
    )
    assert finished.received_at == NOW
    capture_commands_before = len(
        [name for name, _fields in workers.calls if name == "set_capture"]
    )
    assert not await media.release_capture_after_playout(
        session,
        claim.announcement_id,
    )
    await media.handle_worker_frame(_source_lifecycle(claim, "finished"))
    capture_commands = [
        fields for name, fields in workers.calls if name == "set_capture"
    ]
    assert len(capture_commands) == capture_commands_before + 1
    assert capture_commands[-1] == {
        "media_grant_revision": 1,
        "enabled": True,
    }
    assert not await media.release_capture_after_playout(
        session,
        claim.announcement_id,
    )

    newer_claim = replace(
        claim,
        claim_id="00000000-0000-4000-8000-000000000022",
        announcement_id=deterministic_uuid4(
            "voice-announcement-v1",
            SESSION,
            TURN,
            "2",
            "2",
            "progress",
            "single",
            "0",
        ),
        sequence=2,
        kind="progress",
        phrase_key="still_working",
    )
    await media.speak_turn(turn, newer_claim, text="Still working on it.")
    media._greeted.add((SESSION, 1))
    calls_before_routine_enable = len(
        [name for name, _fields in workers.calls if name == "set_capture"]
    )
    await media.set_capture(session, True)
    assert len(
        [name for name, _fields in workers.calls if name == "set_capture"]
    ) == calls_before_routine_enable
    await media.handle_worker_frame(
        {
            "type": "media_state",
            "state": "listening",
            "session_id": SESSION,
            "generation": 1,
        }
    )
    await media.set_capture(session, True)
    assert len(
        [name for name, _fields in workers.calls if name == "set_capture"]
    ) == calls_before_routine_enable
    assert not await media.release_capture_after_playout(
        session,
        claim.announcement_id,
    )

    await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(newer_claim, phase="started", sequence=3),
        session=session,
    )
    await media.accept_client_playout(
        user_id="user-a",
        claims=binding,
        event=_playout(newer_claim, phase="finished", sequence=4),
        session=session,
    )
    assert not await media.release_capture_after_playout(
        session,
        newer_claim.announcement_id,
    )
    capture_commands_before = len(
        [name for name, _fields in workers.calls if name == "set_capture"]
    )
    await media.handle_worker_frame(_source_lifecycle(newer_claim, "finished"))
    assert len(
        [name for name, _fields in workers.calls if name == "set_capture"]
    ) == capture_commands_before + 1
    assert not await media.release_capture_after_playout(
        session,
        newer_claim.announcement_id,
    )
