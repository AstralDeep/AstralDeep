"""Direct-RTC media activator for the Feature-065 session runtime."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from orchestrator.livekit_service import LiveKitService
from orchestrator.voice_control_binding import VoiceControlClaims
from orchestrator.voice_coordinator import (
    AnnouncementFence,
    AnnouncementClaim,
    SessionBindRequest,
    SessionReservation,
    WorkerPool,
    deterministic_uuid4,
)
from orchestrator.voice_runtime import ActivatedVoiceMedia
from orchestrator.voice_sessions import VoiceSessionRecord, VoiceTurnRecord
from shared.protocol import VoicePlayoutEvent
from shared.watch_ticket import (
    WatchTicketError,
    derive_watch_nonce,
    issue_watch_ticket,
    watch_participant_identity,
)


_MAX_CLIENT_PLAYOUT_EVENTS_PER_SECOND = 8
_MAX_ACTIVE_ANNOUNCEMENTS = 256
_ANNOUNCEMENT_RETENTION_SECONDS = 120.0


class VoiceMediaActivationError(RuntimeError):
    """Content-free direct-media failure safe for the REST problem mapper."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ClientPlayoutObservation:
    """One exact content-free local-render event stamped by server time."""

    user_id: str
    fence: AnnouncementFence
    turn_announcement_sequence: int
    phase: str
    client_sequence: int
    received_at: datetime


@dataclass(slots=True)
class _ClientPlayoutRecord:
    user_id: str
    fence: AnnouncementFence
    turn_announcement_sequence: int
    registered_monotonic: float
    client_phase: str | None = None
    source_phase: str | None = None


class DirectRtcVoiceMedia:
    """Reserve one worker, deliver a purpose-bound grant, then expose media."""

    def __init__(
        self,
        *,
        livekit: LiveKitService,
        workers: WorkerPool,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        ready_timeout_seconds: float = 8.0,
        watch_ticket_secret: bytes | None = None,
        watch_bridge_url: str | None = None,
        observability: Any | None = None,
    ) -> None:
        if not 1 <= ready_timeout_seconds <= 15:
            raise ValueError("invalid_worker_ready_timeout")
        self._livekit = livekit
        self._workers = workers
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._ready_timeout = float(ready_timeout_seconds)
        if watch_ticket_secret is not None and (
            not isinstance(watch_ticket_secret, bytes)
            or not 32 <= len(watch_ticket_secret) <= 512
        ):
            raise ValueError("invalid_watch_ticket_secret")
        if watch_bridge_url is not None and (
            not isinstance(watch_bridge_url, str)
            or not watch_bridge_url.startswith("wss://")
            or len(watch_bridge_url) > 2_048
        ):
            raise ValueError("invalid_watch_bridge_url")
        self._watch_ticket_secret = watch_ticket_secret
        self._watch_bridge_url = watch_bridge_url
        self._observability = observability
        self._reservations: dict[tuple[str, int], SessionReservation] = {}
        self._sessions: dict[tuple[str, int], VoiceSessionRecord] = {}
        self._greeting_inflight: set[tuple[str, int]] = set()
        self._greeted: set[tuple[str, int]] = set()
        self._speech_waiters: dict[
            str,
            tuple[str, int, str, int, asyncio.Future[str]],
        ] = {}
        self._playout_records: dict[str, _ClientPlayoutRecord] = {}
        self._capture_playout_holds: dict[tuple[str, int], str] = {}
        self._capture_requested: dict[tuple[str, int], bool] = {}
        self._capture_command_locks: dict[
            tuple[str, int], asyncio.Lock
        ] = {}
        self._client_sequences: dict[tuple[str, str], int] = {}
        self._client_event_times: dict[tuple[str, str], deque[float]] = {}
        # ``voice_turn.announcement_sequence`` is a durable per-turn CAS
        # sequence, while clients consume one ordered media stream for the
        # entire session.  Keep the wire sequence separately so a greeting,
        # the first turn acknowledgement, and later turns can never collide.
        self._media_announcement_sequences: dict[tuple[str, int], int] = {}
        self._announcement_send_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._interruption_started_at: dict[tuple[str, int], float] = {}
        self._lock = asyncio.Lock()

    async def activate(self, session: VoiceSessionRecord) -> ActivatedVoiceMedia:
        if session.transport not in {"livekit", "watch_pcm_websocket"}:
            raise VoiceMediaActivationError("unsupported_transport")
        if session.transport == "watch_pcm_websocket" and (
            self._watch_ticket_secret is None or self._watch_bridge_url is None
        ):
            raise VoiceMediaActivationError("watch_bridge_unavailable")
        request = _bind_request(session)
        reservation = await self._workers.reserve_session(request)
        key = (session.session_id, session.generation)
        async with self._lock:
            current = self._reservations.get(key)
            if current is not None and current != reservation:
                await self._workers.release_session(
                    reservation.session_id,
                    reservation.generation,
                    reservation.assignment_id,
                )
                raise VoiceMediaActivationError("stale_worker_assignment")
            self._reservations[key] = reservation
            self._sessions[key] = session
            self._media_announcement_sequences.setdefault(key, 0)
            self._announcement_send_locks.setdefault(key, asyncio.Lock())
            self._capture_command_locks.setdefault(key, asyncio.Lock())
            self._capture_requested[key] = bool(
                session.foreground_active and session.microphone_enabled
            )
        room_created = False
        try:
            # Neither the worker nor the client grant has room-create
            # authority. The orchestrator creates one bounded two-party room
            # explicitly before minting either purpose-bound join grant.
            await self._livekit.ensure_room(session.room_name)
            room_created = True
            issued_at = self._now()
            worker_grant = self._livekit.mint_worker_grant(
                revision=request.worker_rtc_grant_revision,
                room_name=request.room_name,
                worker_identity=reservation.worker_identity,
                issued_at=issued_at,
            )
            await self._workers.deliver_session_bind(
                reservation,
                request,
                worker_grant,
            )
            await self._await_ready(session)
            await self._workers.send_session_command(
                reservation,
                "set_capture",
                {
                    "media_grant_revision": session.media_grant_revision,
                    "enabled": bool(
                        session.foreground_active and session.microphone_enabled
                    ),
                },
            )
            worker_grant_issued_at = _parse_timestamp(worker_grant["issued_at"])
            worker_grant_expires_at = _parse_timestamp(worker_grant["expires_at"])
            active_session = replace(
                session,
                worker_identity=reservation.worker_identity,
                worker_assignment_id=reservation.assignment_id,
                worker_rtc_grant_issued_at=worker_grant_issued_at,
                worker_rtc_grant_expires_at=worker_grant_expires_at,
            )
            async with self._lock:
                if self._reservations.get(key) != reservation:
                    raise VoiceMediaActivationError("stale_worker_assignment")
                self._sessions[key] = active_session
            client_grant = self._mint_client_grant(active_session, reservation)
            return ActivatedVoiceMedia(
                assignment_id=reservation.assignment_id,
                worker_identity=reservation.worker_identity,
                worker_grant_issued_at=worker_grant_issued_at,
                worker_grant_expires_at=worker_grant_expires_at,
                client_grant=client_grant,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._release(reservation))
            if room_created:
                await asyncio.shield(self._best_effort_remove_media(session))
            raise
        except Exception:
            await self._release(reservation)
            if room_created:
                await self._best_effort_remove_media(session)
            raise

    async def rotate_media_grant(
        self,
        previous: VoiceSessionRecord,
        session: VoiceSessionRecord,
        *,
        refresh_id: str,
    ) -> Mapping[str, Any]:
        """Apply a durable rotation at the worker before reminting a bearer."""

        key = (session.session_id, session.generation)
        async with self._lock:
            active = self._sessions.get(key)
            reservation = self._reservations.get(key)
        if active is None or reservation is None:
            raise VoiceMediaActivationError("worker_assignment_unavailable")
        if (
            session.user_id != active.user_id
            or session.transport != active.transport
            or session.worker_identity != active.worker_identity
            or session.media_grant_revision < active.media_grant_revision
        ):
            raise VoiceMediaActivationError("stale_media_grant_rotation")
        if session.media_grant_revision == active.media_grant_revision + 1:
            await self._workers.send_session_command(
                reservation,
                "media_grant_rotated",
                {
                    "refresh_id": refresh_id,
                    "previous_media_grant_revision": active.media_grant_revision,
                    "media_grant_revision": session.media_grant_revision,
                    "client_participant_identity": session.participant_identity,
                    "transport": session.transport,
                    "grant_expires_at": _format_timestamp(
                        session.media_grant_expires_at
                    ),
                },
            )
            waiter = getattr(self._workers, "await_media_grant_applied", None)
            if not callable(waiter):
                raise VoiceMediaActivationError("media_grant_apply_unavailable")
            try:
                await waiter(
                    session_id=session.session_id,
                    generation=session.generation,
                    refresh_id=refresh_id,
                    media_grant_revision=session.media_grant_revision,
                    client_participant_identity=session.participant_identity,
                    timeout_seconds=self._ready_timeout,
                )
            except asyncio.TimeoutError:
                raise VoiceMediaActivationError(
                    "media_grant_applied_timeout"
                ) from None
            async with self._lock:
                if self._reservations.get(key) != reservation:
                    raise VoiceMediaActivationError("stale_worker_assignment")
                self._sessions[key] = session
                self._capture_playout_holds.pop(key, None)
                self._capture_requested[key] = bool(
                    session.foreground_active and session.microphone_enabled
                )
            if session.transport == "livekit":
                try:
                    await self._livekit.remove_participant(
                        active.room_name,
                        active.participant_identity,
                    )
                except Exception:
                    # The worker revision fence already rejects this publisher.
                    pass
        elif session.media_grant_revision != active.media_grant_revision:
            raise VoiceMediaActivationError("stale_media_grant_rotation")
        return self._mint_client_grant(session, reservation)

    async def current_session(
        self,
        session_id: str,
        generation: int,
    ) -> VoiceSessionRecord | None:
        """Return the immutable active media snapshot for an exact fence."""

        if (
            not isinstance(session_id, str)
            or not session_id
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            return None
        async with self._lock:
            return self._sessions.get((session_id, generation))

    async def assignment_is_current(
        self,
        session: VoiceSessionRecord,
        *,
        assignment_id: str,
        worker_identity: str,
    ) -> bool:
        """Confirm the exact local and pool reservation without new authority."""

        key = (session.session_id, session.generation)
        async with self._lock:
            local = self._reservations.get(key)
        if (
            local is None
            or local.assignment_id != assignment_id
            or local.worker_identity != worker_identity
            or local.worker_rtc_grant_revision
            != session.worker_rtc_grant_revision
        ):
            return False
        try:
            current = await self._workers.current_reservation(
                session_id=session.session_id,
                generation=session.generation,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return current == local

    def _mint_client_grant(
        self,
        session: VoiceSessionRecord,
        reservation: SessionReservation,
    ) -> Mapping[str, Any]:
        grant_id = deterministic_uuid4(
            "voice-client-grant-v1",
            session.session_id,
            str(session.generation),
            str(session.media_grant_revision),
            session.participant_identity,
        )
        if session.transport == "livekit":
            return self._livekit.mint_client_grant(
                grant_id=grant_id,
                session_id=session.session_id,
                generation=session.generation,
                media_grant_revision=session.media_grant_revision,
                room_name=session.room_name,
                participant_identity=session.participant_identity,
                worker_identity=reservation.worker_identity,
                issued_at=session.media_grant_issued_at,
            )
        secret = self._watch_ticket_secret
        url = self._watch_bridge_url
        if secret is None or url is None:
            raise VoiceMediaActivationError("watch_bridge_unavailable")
        session_key = session.last_media_refresh_id or session.activation_id
        try:
            nonce = derive_watch_nonce(
                secret,
                user_id=session.user_id,
                session_key=session_key,
                generation=session.generation,
                media_grant_revision=session.media_grant_revision,
                device_id=session.device_id,
                connection_generation=session.owner_connection_generation,
            )
            if (
                not hashlib.sha256(nonce).digest()
                == session.media_grant_nonce_hash
                or watch_participant_identity(nonce)
                != session.participant_identity
            ):
                raise VoiceMediaActivationError("watch_ticket_fence_mismatch")
            ticket = issue_watch_ticket(
                secret,
                user_id=session.user_id,
                session_id=session.session_id,
                generation=session.generation,
                media_grant_revision=session.media_grant_revision,
                worker_identity=reservation.worker_identity,
                device_id=session.device_id,
                connection_generation=session.owner_connection_generation,
                issued_at=session.media_grant_issued_at,
                expires_at=session.media_grant_expires_at,
                nonce=nonce,
            )
        except WatchTicketError as exc:
            raise VoiceMediaActivationError(exc.code) from None
        return {
            "grant_id": grant_id,
            "transport": "watch_pcm_websocket",
            "session_id": session.session_id,
            "generation": session.generation,
            "media_grant_revision": session.media_grant_revision,
            "expires_at": _format_timestamp(session.media_grant_expires_at),
            "url": url,
            "ticket": ticket,
            "worker_identity": reservation.worker_identity,
            "capture": {
                "encoding": "pcm_s16le",
                "channels": 1,
                "sample_rate_hz": 16_000,
                "frame_duration_ms": 20,
            },
            "playback": {
                "encoding": "pcm_s16le",
                "channels": 1,
                "sample_rate_hz": 24_000,
                "frame_duration_ms": 20,
            },
        }

    async def apply_context(self, session: VoiceSessionRecord) -> None:
        reservation = await self._reservation(session)
        await self._workers.send_session_command(
            reservation,
            "session_context_update",
            {
                "media_grant_revision": session.media_grant_revision,
                "visible_chat_id": session.visible_chat_id,
                "chat_context_revision": session.chat_context_revision,
            },
        )
        await self._await_ready(session)

    async def set_capture(self, session: VoiceSessionRecord, enabled: bool) -> None:
        key = (session.session_id, session.generation)
        async with self._lock:
            reservation = self._reservations.get(key)
            capture_lock = self._capture_command_locks.get(key)
            if reservation is None or capture_lock is None:
                raise VoiceMediaActivationError("worker_assignment_unavailable")
        async with capture_lock:
            async with self._lock:
                if (
                    self._reservations.get(key) != reservation
                    or self._capture_command_locks.get(key) is not capture_lock
                ):
                    raise VoiceMediaActivationError(
                        "worker_assignment_unavailable"
                    )
                self._capture_requested[key] = bool(enabled)
                # A routine foreground heartbeat or idempotent microphone
                # update must not release an assistant-output fence. Only the
                # exact authenticated terminal playout event may do that.
                if enabled and key in self._capture_playout_holds:
                    return
            await self._workers.send_session_command(
                reservation,
                "set_capture",
                {
                    "media_grant_revision": session.media_grant_revision,
                    "enabled": bool(enabled),
                },
            )

    async def release_capture_after_playout(
        self,
        session: VoiceSessionRecord,
        announcement_id: str,
    ) -> bool:
        """Release capture only for the latest exact client playout fence."""

        key = (session.session_id, session.generation)
        async with self._lock:
            if self._capture_playout_holds.get(key) != announcement_id:
                return False
            reservation = self._reservations.get(key)
            active = self._sessions.get(key)
            capture_lock = self._capture_command_locks.get(key)
            if (
                reservation is None
                or active is None
                or capture_lock is None
                or active.user_id != session.user_id
                or active.media_grant_revision != session.media_grant_revision
            ):
                raise VoiceMediaActivationError("stale_session_fence")
            record = self._playout_records.get(announcement_id)
            if record is None or not self._playout_is_terminal(record):
                return False
        return await self._release_capture_hold(
            key,
            announcement_id,
            reservation,
            session.media_grant_revision,
            capture_lock,
        )

    async def _release_capture_hold(
        self,
        key: tuple[str, int],
        announcement_id: str,
        reservation: SessionReservation,
        media_grant_revision: int,
        capture_lock: asyncio.Lock,
    ) -> bool:
        """Release one fully observed hold without blocking other sessions."""

        async with capture_lock:
            async with self._lock:
                if (
                    self._capture_command_locks.get(key) is not capture_lock
                    or self._reservations.get(key) != reservation
                    or self._capture_playout_holds.get(key) != announcement_id
                ):
                    return False
                active = self._sessions.get(key)
                record = self._playout_records.get(announcement_id)
                if (
                    active is None
                    or active.media_grant_revision != media_grant_revision
                    or record is None
                    or not self._playout_is_terminal(record)
                ):
                    return False
                enabled = self._capture_requested.get(key, False)
            await self._workers.send_session_command(
                reservation,
                "set_capture",
                {
                    "media_grant_revision": media_grant_revision,
                    "enabled": enabled,
                },
            )
            async with self._lock:
                if (
                    self._capture_command_locks.get(key) is capture_lock
                    and self._reservations.get(key) == reservation
                    and self._capture_playout_holds.get(key)
                    == announcement_id
                ):
                    self._capture_playout_holds.pop(key, None)
                    return True
                return False

    async def stop_speech(self, session: VoiceSessionRecord) -> None:
        reservation = await self._reservation(session)
        key = (session.session_id, session.generation)
        started_at = self._monotonic_now()
        try:
            await self._workers.send_session_command(
                reservation,
                "stop_speech",
                {
                    "media_grant_revision": session.media_grant_revision,
                    "reason": "user_stop" if not session.speech_muted else "mute",
                },
            )
        except asyncio.CancelledError:
            self._record_event(
                session,
                "interruption",
                "failed",
                reason="internal_error",
            )
            raise
        except Exception:
            self._record_event(
                session,
                "interruption",
                "failed",
                reason="internal_error",
            )
            raise
        async with self._lock:
            self._interruption_started_at[key] = started_at
        self._record_event(
            session,
            "interruption",
            "requested",
            reason="user_request",
        )

    async def speak_turn(
        self,
        turn: VoiceTurnRecord,
        claim: AnnouncementClaim,
        *,
        text: str,
        sensitive_authorized: bool = False,
    ) -> None:
        """Deliver one already-reserved, bounded turn announcement."""

        if not isinstance(turn, VoiceTurnRecord):
            raise TypeError("turn must be VoiceTurnRecord")
        if not isinstance(claim, AnnouncementClaim):
            raise TypeError("claim must be AnnouncementClaim")
        if not isinstance(text, str) or not text.strip() or len(text) > 4_096:
            raise ValueError("invalid_speech_text")
        if not isinstance(sensitive_authorized, bool):
            raise ValueError("invalid_sensitive_authorization")
        key = (turn.session_id, turn.session_generation)
        waiter = asyncio.get_running_loop().create_future()
        async with self._lock:
            send_lock = self._announcement_send_locks.get(key)
            capture_lock = self._capture_command_locks.get(key)
            if send_lock is None or capture_lock is None:
                raise VoiceMediaActivationError("worker_assignment_unavailable")
        async with send_lock:
            async with capture_lock:
                async with self._lock:
                    session = self._sessions.get(key)
                    reservation = self._reservations.get(key)
                    if (
                        session is None
                        or reservation is None
                        or self._capture_command_locks.get(key)
                        is not capture_lock
                    ):
                        raise VoiceMediaActivationError(
                            "worker_assignment_unavailable"
                        )
                    if (
                        turn.media_grant_revision
                        != session.media_grant_revision
                        or claim.sequence < 1
                    ):
                        raise VoiceMediaActivationError(
                            "stale_announcement_fence"
                        )
                    if claim.announcement_id in self._speech_waiters:
                        raise VoiceMediaActivationError(
                            "announcement_already_inflight"
                        )
                    self._require_playout_registration_locked(
                        claim.announcement_id
                    )
                    media_sequence = (
                        self._next_media_announcement_sequence_locked(key)
                    )
                    fields: dict[str, Any] = {
                        "announcement_id": claim.announcement_id,
                        "announcement_sequence": media_sequence,
                        "media_grant_revision": turn.media_grant_revision,
                        "transport": session.transport,
                        "turn_id": turn.turn_id,
                        "kind": claim.kind,
                        "quantum_role": claim.quantum_role,
                        "quantum_index": claim.quantum_index,
                        "max_duration_samples": claim.max_duration_samples,
                        "text": text.strip(),
                        "sensitive_authorized": sensitive_authorized,
                        "expires_at": _format_timestamp(
                            self._now() + timedelta(seconds=30)
                        ),
                    }
                    if claim.phrase_key is not None:
                        fields["phrase_key"] = claim.phrase_key
                    if claim.result_reserved_samples_after is not None:
                        fields["result_reserved_samples_after"] = (
                            claim.result_reserved_samples_after
                        )
                    fence = AnnouncementFence(
                        session_id=turn.session_id,
                        generation=turn.session_generation,
                        media_grant_revision=turn.media_grant_revision,
                        announcement_id=claim.announcement_id,
                        announcement_sequence=media_sequence,
                        turn_id=turn.turn_id,
                        kind=claim.kind,
                        quantum_role=claim.quantum_role,
                        quantum_index=claim.quantum_index,
                        result_reserved_samples_after=(
                            claim.result_reserved_samples_after
                        ),
                        max_duration_samples=claim.max_duration_samples,
                        worker_identity=reservation.worker_identity,
                        device_id=session.device_id,
                        connection_generation=(
                            session.owner_connection_generation
                        ),
                        transport=session.transport,
                    )
                    self._register_playout_locked(
                        user_id=turn.user_id,
                        fence=fence,
                        turn_announcement_sequence=claim.sequence,
                    )
                    self._speech_waiters[claim.announcement_id] = (
                        turn.session_id,
                        turn.session_generation,
                        turn.turn_id,
                        media_sequence,
                        waiter,
                    )
                try:
                    await self._workers.send_session_command(
                        reservation,
                        "speak",
                        fields,
                    )
                except BaseException:
                    async with self._lock:
                        current = self._speech_waiters.pop(
                            claim.announcement_id,
                            None,
                        )
                        self._playout_records.pop(
                            claim.announcement_id,
                            None,
                        )
                        if (
                            self._capture_playout_holds.get(key)
                            == claim.announcement_id
                        ):
                            self._capture_playout_holds.pop(key, None)
                    if current is not None and not current[-1].done():
                        current[-1].cancel()
                    raise

    async def accept_client_playout(
        self,
        *,
        user_id: str,
        claims: VoiceControlClaims,
        event: VoicePlayoutEvent,
        session: VoiceSessionRecord,
    ) -> ClientPlayoutObservation:
        """Validate one event against its authenticated socket and command.

        The client wall clock remains diagnostic.  Operational state uses only
        the receipt timestamp captured after every owner, connection, session,
        generation, grant, announcement, order, and rate fence has passed.
        """

        if not isinstance(user_id, str) or not user_id:
            raise VoiceMediaActivationError("playout_owner_unavailable")
        if not isinstance(claims, VoiceControlClaims):
            raise VoiceMediaActivationError("playout_binding_unavailable")
        if not isinstance(event, VoicePlayoutEvent):
            raise VoiceMediaActivationError("invalid_client_playout_event")
        if not isinstance(session, VoiceSessionRecord):
            raise VoiceMediaActivationError("voice_session_unavailable")
        received_at = self._now()
        if claims.expires_at <= received_at:
            raise VoiceMediaActivationError("binding_expired")
        if (
            claims.subject != user_id
            or claims.device_id != event.device_id
            or claims.connection_generation != event.connection_generation
        ):
            raise VoiceMediaActivationError("owner_connection_mismatch")
        if (
            session.user_id != user_id
            or session.device_id != claims.device_id
            or session.owner_connection_generation
            != claims.connection_generation
            or session.session_id != event.session_id
            or session.generation != event.generation
            or session.media_grant_revision != event.media_grant_revision
            or session.ended_at is not None
        ):
            raise VoiceMediaActivationError("stale_session_fence")

        async with self._lock:
            now_monotonic = self._monotonic_now()
            self._prune_playout_locked(now_monotonic)
            record = self._playout_records.get(event.announcement_id)
            if record is None:
                raise VoiceMediaActivationError("unknown_announcement")
            if record.user_id != user_id:
                raise VoiceMediaActivationError("playout_owner_mismatch")
            active = self._sessions.get((event.session_id, event.generation))
            if (
                active is None
                or active.user_id != session.user_id
                or active.device_id != session.device_id
                or active.owner_connection_generation
                != session.owner_connection_generation
                or active.media_grant_revision
                != session.media_grant_revision
            ):
                raise VoiceMediaActivationError("stale_session_fence")
            self._validate_client_fence(event, record.fence)
            self._validate_client_phase(record, event.phase)
            sequence_key = (event.device_id, event.connection_generation)
            if event.client_sequence <= self._client_sequences.get(
                sequence_key, -1
            ):
                raise VoiceMediaActivationError(
                    "client_sequence_out_of_order"
                )
            self._check_client_rate_locked(sequence_key, now_monotonic)
            self._client_sequences[sequence_key] = event.client_sequence
            record.client_phase = event.phase
        return ClientPlayoutObservation(
            user_id=user_id,
            fence=record.fence,
            turn_announcement_sequence=record.turn_announcement_sequence,
            phase=event.phase,
            client_sequence=event.client_sequence,
            received_at=received_at,
        )

    async def await_speech_terminal(
        self,
        announcement_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> str:
        """Wait for the authenticated worker's exact source terminal."""

        if not isinstance(announcement_id, str) or not announcement_id:
            raise ValueError("invalid_announcement_id")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("invalid_speech_wait_timeout")
        async with self._lock:
            current = self._speech_waiters.get(announcement_id)
        if current is None:
            raise VoiceMediaActivationError("announcement_not_inflight")
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await asyncio.shield(current[-1])
        except TimeoutError:
            async with self._lock:
                stale = self._speech_waiters.pop(announcement_id, None)
            if stale is not None and not stale[-1].done():
                stale[-1].cancel()
            raise VoiceMediaActivationError("speech_terminal_timeout") from None
        else:
            async with self._lock:
                self._speech_waiters.pop(announcement_id, None)
            return result

    async def handle_worker_frame(self, frame: Mapping[str, Any]) -> None:
        """Start the one greeting only after the owner's microphone is live.

        WorkerPool has already authenticated and generation-fenced this frame.
        Waiting for the worker's ``listening`` transition makes the greeting
        audible instead of racing the REST response or the client's room join.
        """

        frame_type = frame.get("type")
        if frame_type == "media_state" and frame.get("state") in {
            "reconnecting",
            "failed",
            "ended",
        }:
            session_id = frame.get("session_id")
            generation = frame.get("generation")
            if isinstance(session_id, str) and isinstance(generation, int):
                async with self._lock:
                    self._capture_playout_holds.pop(
                        (session_id, generation),
                        None,
                    )
        elif frame_type == "media_state" and frame.get("state") == "listening":
            session_id = frame.get("session_id")
            generation = frame.get("generation")
            if isinstance(session_id, str) and isinstance(generation, int):
                key = (session_id, generation)
                async with self._lock:
                    announcement_id = self._capture_playout_holds.get(key)
                    record = self._playout_records.get(announcement_id or "")
                    # A pre-publication speech failure releases its worker hold
                    # without any possible client playout event. Only the
                    # ordered failed-source + explicit worker-listening pair
                    # may retire that coordinator-side bookkeeping fence.
                    if (
                        record is not None
                        and record.source_phase == "failed"
                        and record.client_phase is None
                    ):
                        self._capture_playout_holds.pop(key, None)
        if frame_type in {
            "speech_started",
            "speech_finished",
            "speech_interrupted",
            "speech_failed",
        }:
            announcement_id = frame.get("announcement_id")
            if not isinstance(announcement_id, str):
                return
            release_capture: tuple[
                tuple[str, int],
                SessionReservation,
                int,
                asyncio.Lock,
            ] | None = None
            terminal_waiter: tuple[
                asyncio.Future[str], VoiceSessionRecord | None, float | None
            ] | None = None
            async with self._lock:
                record = self._playout_records.get(announcement_id)
                matched_record = record is not None and self._worker_frame_matches(
                    frame, record.fence
                )
                if matched_record:
                    assert record is not None
                    record.source_phase = str(frame_type).removeprefix(
                        "speech_"
                    )
                    if self._playout_is_terminal(record):
                        key = (record.fence.session_id, record.fence.generation)
                        reservation = self._reservations.get(key)
                        capture_lock = self._capture_command_locks.get(key)
                        if reservation is not None and capture_lock is not None:
                            release_capture = (
                                key,
                                reservation,
                                record.fence.media_grant_revision,
                                capture_lock,
                            )
                current = self._speech_waiters.get(announcement_id)
                if frame_type == "speech_started":
                    return
                if current is not None:
                    session_id, generation, turn_id, sequence, waiter = current
                    if (
                        frame.get("session_id") == session_id
                        and frame.get("generation") == generation
                        and frame.get("turn_id") == turn_id
                        and frame.get("announcement_sequence") == sequence
                    ):
                        session = self._sessions.get((session_id, generation))
                        interruption_started_at = (
                            self._interruption_started_at.pop(
                                (session_id, generation),
                                None,
                            )
                        )
                        terminal_waiter = (
                            waiter,
                            session,
                            interruption_started_at,
                        )
            if release_capture is not None:
                key, reservation, media_grant_revision, capture_lock = (
                    release_capture
                )
                await self._release_capture_hold(
                    key,
                    announcement_id,
                    reservation,
                    media_grant_revision,
                    capture_lock,
                )
            if terminal_waiter is None:
                return
            waiter, session, interruption_started_at = terminal_waiter
            if not waiter.done():
                waiter.set_result(str(frame_type))
            if session is not None and frame_type == "speech_interrupted":
                self._record_event(
                    session,
                    "interruption",
                    "interrupted",
                    reason="user_request",
                )
                if interruption_started_at is not None:
                    self._observe_timing(
                        session,
                        "interruption",
                        max(0.0, self._monotonic_now() - interruption_started_at),
                    )
            return
        if frame_type != "media_state" or frame.get("state") != "listening":
            return
        session_id = frame.get("session_id")
        generation = frame.get("generation")
        if not isinstance(session_id, str) or not isinstance(generation, int):
            return
        key = (session_id, generation)
        async with self._lock:
            session = self._sessions.get(key)
            reservation = self._reservations.get(key)
            capture_lock = self._capture_command_locks.get(key)
            if (
                session is None
                or reservation is None
                or capture_lock is None
                or self._announcement_send_locks.get(key) is None
                or key in self._greeted
                or key in self._greeting_inflight
            ):
                return
            send_lock = self._announcement_send_locks[key]
            self._greeting_inflight.add(key)
        try:
            async with send_lock:
                async with capture_lock:
                    now = self._now()
                    announcement_id = deterministic_uuid4(
                        "voice-greeting-v1",
                        session.session_id,
                        str(session.generation),
                    )
                    async with self._lock:
                        if (
                            self._reservations.get(key) != reservation
                            or self._sessions.get(key) is None
                            or self._capture_command_locks.get(key)
                            is not capture_lock
                        ):
                            raise VoiceMediaActivationError(
                                "worker_assignment_unavailable"
                            )
                        self._require_playout_registration_locked(
                            announcement_id
                        )
                        media_sequence = (
                            self._next_media_announcement_sequence_locked(key)
                        )
                        fence = AnnouncementFence(
                            session_id=session.session_id,
                            generation=session.generation,
                            media_grant_revision=(
                                session.media_grant_revision
                            ),
                            announcement_id=announcement_id,
                            announcement_sequence=media_sequence,
                            turn_id=None,
                            kind="greeting",
                            quantum_role="single",
                            quantum_index=0,
                            result_reserved_samples_after=None,
                            max_duration_samples=96_000,
                            worker_identity=reservation.worker_identity,
                            device_id=session.device_id,
                            connection_generation=(
                                session.owner_connection_generation
                            ),
                            transport=session.transport,
                        )
                        self._register_playout_locked(
                            user_id=session.user_id,
                            fence=fence,
                            turn_announcement_sequence=1,
                        )
                    await self._workers.send_session_command(
                        reservation,
                        "speak",
                        {
                            "announcement_id": announcement_id,
                            "announcement_sequence": media_sequence,
                            "media_grant_revision": (
                                session.media_grant_revision
                            ),
                            "transport": session.transport,
                            "turn_id": None,
                            "kind": "greeting",
                            "quantum_role": "single",
                            "quantum_index": 0,
                            "max_duration_samples": 96_000,
                            "phrase_key": "hello_ready",
                            "text": "Hi! I'm ready when you are.",
                            "sensitive_authorized": False,
                            "expires_at": _format_timestamp(
                                now + timedelta(seconds=30)
                            ),
                        },
                    )
        except asyncio.CancelledError:
            async with self._lock:
                failed_announcement = locals().get("announcement_id", "")
                self._playout_records.pop(failed_announcement, None)
                if self._capture_playout_holds.get(key) == failed_announcement:
                    self._capture_playout_holds.pop(key, None)
            raise
        except Exception:
            # A later authenticated listening transition may retry. No bearer,
            # transcript, or speech text is retained in this failure path.
            async with self._lock:
                failed_announcement = locals().get("announcement_id", "")
                self._playout_records.pop(failed_announcement, None)
                if self._capture_playout_holds.get(key) == failed_announcement:
                    self._capture_playout_holds.pop(key, None)
            return
        else:
            async with self._lock:
                if self._reservations.get(key) == reservation:
                    self._greeted.add(key)
        finally:
            async with self._lock:
                self._greeting_inflight.discard(key)

    async def end(self, session: VoiceSessionRecord, reason: str) -> None:
        cleanup_started_at = self._monotonic_now()
        key = (session.session_id, session.generation)
        async with self._lock:
            capture_lock = self._capture_command_locks.get(key)
        if capture_lock is None:
            capture_lock = asyncio.Lock()
        async with capture_lock:
            async with self._lock:
                reservation = self._reservations.pop(key, None)
                active_session = self._sessions.pop(key, None)
                self._capture_playout_holds.pop(key, None)
                self._capture_requested.pop(key, None)
                self._capture_command_locks.pop(key, None)
                self._media_announcement_sequences.pop(key, None)
                self._announcement_send_locks.pop(key, None)
                self._interruption_started_at.pop(key, None)
                self._greeting_inflight.discard(key)
                self._greeted.discard(key)
                stale_waiters = [
                    (announcement_id, current[-1])
                    for announcement_id, current in self._speech_waiters.items()
                    if current[0] == session.session_id
                    and current[1] == session.generation
                ]
                for announcement_id, _waiter in stale_waiters:
                    self._speech_waiters.pop(announcement_id, None)
                for announcement_id, record in tuple(
                    self._playout_records.items()
                ):
                    if (
                        record.fence.session_id == session.session_id
                        and record.fence.generation == session.generation
                    ):
                        self._playout_records.pop(announcement_id, None)
        for _announcement_id, waiter in stale_waiters:
            if not waiter.done():
                waiter.set_result("session_ended")
        try:
            try:
                if reservation is not None:
                    try:
                        await self._workers.send_session_command(
                            reservation,
                            "end_session",
                            {
                                "media_grant_revision": session.media_grant_revision,
                                "reason": reason,
                            },
                        )
                    finally:
                        await self._release(reservation, remove_local=False)
            finally:
                # Participant and room cleanup is mandatory even when a worker is
                # unreachable or refuses the final control command.
                await self._remove_media(session)
        except asyncio.CancelledError:
            self._record_cleanup(session, "partial", cleanup_started_at)
            raise
        except Exception:
            self._record_cleanup(session, "partial", cleanup_started_at)
            raise
        else:
            outcome = (
                "complete"
                if reservation is not None or active_session is not None
                else "not_required"
            )
            self._record_cleanup(session, outcome, cleanup_started_at)

    async def abort(self, session: VoiceSessionRecord) -> None:
        try:
            await self.end(session, "media_error")
        except Exception:
            # The caller still durable-ends the generation. All grants are short
            # lived, and room reconciliation removes any stale participant.
            return

    async def _await_ready(self, session: VoiceSessionRecord) -> None:
        waiter = getattr(self._workers, "await_session_ready", None)
        if not callable(waiter):
            raise VoiceMediaActivationError("worker_readiness_unavailable")
        try:
            await waiter(
                session_id=session.session_id,
                generation=session.generation,
                visible_chat_id=session.visible_chat_id,
                chat_context_revision=session.chat_context_revision,
                timeout_seconds=self._ready_timeout,
            )
        except asyncio.TimeoutError:
            raise VoiceMediaActivationError("worker_ready_timeout") from None

    async def _reservation(self, session: VoiceSessionRecord) -> SessionReservation:
        async with self._lock:
            value = self._reservations.get((session.session_id, session.generation))
        if value is None:
            raise VoiceMediaActivationError("worker_assignment_unavailable")
        return value

    async def _release(
        self,
        reservation: SessionReservation,
        *,
        remove_local: bool = True,
    ) -> None:
        if remove_local:
            async with self._lock:
                key = (reservation.session_id, reservation.generation)
                capture_lock = self._capture_command_locks.get(key)
            if capture_lock is None:
                capture_lock = asyncio.Lock()
            async with capture_lock:
                async with self._lock:
                    if self._reservations.get(key) == reservation:
                        self._reservations.pop(key, None)
                        self._sessions.pop(key, None)
                        self._capture_playout_holds.pop(key, None)
                        self._capture_requested.pop(key, None)
                        self._capture_command_locks.pop(key, None)
                        self._media_announcement_sequences.pop(key, None)
                        self._announcement_send_locks.pop(key, None)
                        self._greeting_inflight.discard(key)
                        self._greeted.discard(key)
        await self._workers.release_session(
            reservation.session_id,
            reservation.generation,
            reservation.assignment_id,
        )

    async def _remove_media(self, session: VoiceSessionRecord) -> None:
        try:
            await self._livekit.remove_participant(
                session.room_name,
                session.participant_identity,
            )
        finally:
            await self._livekit.delete_room(session.room_name)

    async def _best_effort_remove_media(self, session: VoiceSessionRecord) -> None:
        try:
            await self._remove_media(session)
        except Exception:
            # Preserve the activation refusal. Short-lived grants remain
            # fenced, while the normal room reconciler can retry cleanup.
            return

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise VoiceMediaActivationError("invalid_media_clock")
        return value.astimezone(UTC)

    def _monotonic_now(self) -> float:
        value = self._monotonic()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise VoiceMediaActivationError("invalid_media_clock")
        return float(value)

    @staticmethod
    def _metric_dimensions(session: VoiceSessionRecord) -> tuple[str, str]:
        transport = (
            "watch_bridge"
            if session.transport == "watch_pcm_websocket"
            else session.transport
        )
        return session.device_kind, transport

    def _record_event(
        self,
        session: VoiceSessionRecord,
        event: str,
        outcome: str,
        *,
        reason: str,
    ) -> None:
        if self._observability is None:
            return
        client_kind, transport = self._metric_dimensions(session)
        self._observability.record_voice_event(
            event,
            outcome,
            reason=reason,
            client_kind=client_kind,
            transport=transport,
        )

    def _observe_timing(
        self,
        session: VoiceSessionRecord,
        timing: str,
        duration_seconds: float,
    ) -> None:
        if self._observability is None:
            return
        client_kind, transport = self._metric_dimensions(session)
        self._observability.observe_voice_timing(
            timing,
            duration_seconds,
            client_kind=client_kind,
            transport=transport,
        )

    def _record_cleanup(
        self,
        session: VoiceSessionRecord,
        outcome: str,
        started_at: float,
    ) -> None:
        if self._observability is None:
            return
        client_kind, transport = self._metric_dimensions(session)
        self._observability.record_voice_cleanup(
            outcome,
            client_kind=client_kind,
            transport=transport,
        )
        self._observability.observe_voice_timing(
            "cleanup",
            max(0.0, self._monotonic_now() - started_at),
            client_kind=client_kind,
            transport=transport,
        )

    def _register_playout_locked(
        self,
        *,
        user_id: str,
        fence: AnnouncementFence,
        turn_announcement_sequence: int,
    ) -> None:
        now = self._monotonic_now()
        self._prune_playout_locked(now)
        if fence.announcement_id in self._playout_records:
            raise VoiceMediaActivationError("announcement_already_registered")
        if len(self._playout_records) >= _MAX_ACTIVE_ANNOUNCEMENTS:
            raise VoiceMediaActivationError("announcement_capacity_exhausted")
        self._playout_records[fence.announcement_id] = _ClientPlayoutRecord(
            user_id=user_id,
            fence=fence,
            turn_announcement_sequence=turn_announcement_sequence,
            registered_monotonic=now,
        )
        self._capture_playout_holds[
            (fence.session_id, fence.generation)
        ] = fence.announcement_id

    def _require_playout_registration_locked(self, announcement_id: str) -> None:
        """Reject a duplicate/capacity miss before consuming a wire sequence."""

        self._prune_playout_locked(self._monotonic_now())
        if announcement_id in self._playout_records:
            raise VoiceMediaActivationError("announcement_already_inflight")
        if len(self._playout_records) >= _MAX_ACTIVE_ANNOUNCEMENTS:
            raise VoiceMediaActivationError("announcement_capacity_exhausted")

    def _next_media_announcement_sequence_locked(
        self,
        key: tuple[str, int],
    ) -> int:
        """Allocate one bounded session-global sequence while ``_lock`` is held."""

        current = self._media_announcement_sequences.get(key)
        if current is None:
            raise VoiceMediaActivationError("worker_assignment_unavailable")
        if current >= 2**63 - 1:
            raise VoiceMediaActivationError("announcement_sequence_exhausted")
        sequence = current + 1
        self._media_announcement_sequences[key] = sequence
        return sequence

    def _prune_playout_locked(self, now: float) -> None:
        expired = [
            announcement_id
            for announcement_id, record in self._playout_records.items()
            if now - record.registered_monotonic
            > _ANNOUNCEMENT_RETENTION_SECONDS
        ]
        for announcement_id in expired:
            self._playout_records.pop(announcement_id, None)
        cutoff = now - 1.0
        for key, events in tuple(self._client_event_times.items()):
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._client_event_times.pop(key, None)

    @staticmethod
    def _validate_client_fence(
        event: VoicePlayoutEvent,
        fence: AnnouncementFence,
    ) -> None:
        expected = {
            "device_id": fence.device_id,
            "connection_generation": fence.connection_generation,
            "session_id": fence.session_id,
            "generation": fence.generation,
            "media_grant_revision": fence.media_grant_revision,
            "announcement_id": fence.announcement_id,
            "announcement_sequence": fence.announcement_sequence,
            "turn_id": fence.turn_id,
            "kind": fence.kind,
            "quantum_role": fence.quantum_role,
            "quantum_index": fence.quantum_index,
            "result_reserved_samples_after": (
                fence.result_reserved_samples_after
            ),
        }
        if any(getattr(event, name) != value for name, value in expected.items()):
            raise VoiceMediaActivationError("playout_fence_mismatch")

    @staticmethod
    def _playout_is_terminal(record: _ClientPlayoutRecord) -> bool:
        return bool(
            record.source_phase in {"finished", "interrupted", "failed"}
            and record.client_phase in {"finished", "interrupted"}
        )

    @staticmethod
    def _validate_client_phase(
        record: _ClientPlayoutRecord,
        phase: str,
    ) -> None:
        if phase == "started":
            if record.client_phase is not None:
                raise VoiceMediaActivationError("client_playout_out_of_order")
            return
        if phase not in {"finished", "interrupted"}:
            raise VoiceMediaActivationError("invalid_client_playout_phase")
        if record.client_phase != "started":
            raise VoiceMediaActivationError("client_playout_out_of_order")

    def _check_client_rate_locked(
        self,
        key: tuple[str, str],
        now: float,
    ) -> None:
        events = self._client_event_times.setdefault(key, deque())
        cutoff = now - 1.0
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= _MAX_CLIENT_PLAYOUT_EVENTS_PER_SECOND:
            raise VoiceMediaActivationError("client_playout_rate_exceeded")
        events.append(now)

    @staticmethod
    def _worker_frame_matches(
        frame: Mapping[str, Any],
        fence: AnnouncementFence,
    ) -> bool:
        expected = {
            "session_id": fence.session_id,
            "generation": fence.generation,
            "media_grant_revision": fence.media_grant_revision,
            "announcement_id": fence.announcement_id,
            "announcement_sequence": fence.announcement_sequence,
            "turn_id": fence.turn_id,
            "kind": fence.kind,
            "quantum_role": fence.quantum_role,
            "quantum_index": fence.quantum_index,
            "result_reserved_samples_after": (
                fence.result_reserved_samples_after
            ),
        }
        return all(frame.get(name) == value for name, value in expected.items())


def _bind_request(session: VoiceSessionRecord) -> SessionBindRequest:
    return SessionBindRequest(
        session_id=session.session_id,
        generation=session.generation,
        room_name=session.room_name,
        transport=session.transport,
        media_grant_revision=session.media_grant_revision,
        worker_rtc_grant_revision=session.worker_rtc_grant_revision,
        client_participant_identity=session.participant_identity,
        visible_chat_id=session.visible_chat_id,
        chat_context_revision=session.chat_context_revision,
    )


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise VoiceMediaActivationError("invalid_grant_timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise VoiceMediaActivationError("invalid_grant_timestamp") from None
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VoiceMediaActivationError("invalid_media_clock")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


__all__ = [
    "ClientPlayoutObservation",
    "DirectRtcVoiceMedia",
    "VoiceMediaActivationError",
]
