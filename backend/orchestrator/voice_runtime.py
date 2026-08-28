"""Transactional conversational-voice session lifecycle orchestration.

This layer joins the authenticated REST control plane to durable session rows
and a direct-RTC media activator.  It never submits a chat query and never sees
the operator speech endpoint or credential; recognized text still enters the
ordinary authenticated WebSocket dispatcher.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping, Protocol

from orchestrator.voice_api import VoiceApiError, VoiceHttpResult
from orchestrator.voice_backend import VoiceSpeechBackend
from orchestrator.voice_sessions import (
    CreateSession,
    GRANT_REPLAY_WINDOW,
    MediaGrantRefresh,
    SessionControl,
    SessionTakeover,
    SessionUpdate,
    TakeoverRequired,
    VoiceSessionRecord,
    VoiceSessionRepository,
)
from shared.watch_ticket import derive_watch_nonce, watch_participant_identity


logger = logging.getLogger(__name__)


async def _join_task_outcome_through_cancellation(
    task: asyncio.Task[Any],
) -> tuple[Any, BaseException | None, asyncio.CancelledError | None]:
    """Join retained repository work despite repeated caller cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                cancellation = cancellation or exc
            if task.done():
                break
        except BaseException:
            break
    try:
        return task.result(), None, cancellation
    except BaseException as error:
        return None, error, cancellation


@dataclass(frozen=True, slots=True)
class ActivatedVoiceMedia:
    """Safe activation receipt; only the client grant is returned over REST."""

    assignment_id: str
    worker_identity: str
    worker_grant_issued_at: datetime
    worker_grant_expires_at: datetime
    client_grant: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _ActiveWorkerAssignment:
    """Exact non-secret activation fence retained for disconnect repair."""

    session: VoiceSessionRecord
    assignment_id: str
    worker_identity: str


@dataclass(eq=False, slots=True)
class _LocalActivationReservation:
    """Exact request-owned capacity reserved before one local mutation."""

    user_id: str
    activation_id: str


@dataclass(slots=True)
class _LocalActivationKeyState:
    """Bounded FIFO ownership for one content-free activation identity."""

    owner: _LocalActivationReservation
    waiters: deque[
        tuple[_LocalActivationReservation, asyncio.Future[None]]
    ]


@dataclass(frozen=True, slots=True)
class _PendingLocalActivationCleanup:
    """Content-free handoff for one active session not returned to its caller."""

    session: VoiceSessionRecord
    create: CreateSession
    reservation: _LocalActivationReservation


class VoiceMediaActivator(Protocol):
    async def activate(self, session: VoiceSessionRecord) -> ActivatedVoiceMedia: ...

    async def assignment_is_current(
        self,
        session: VoiceSessionRecord,
        *,
        assignment_id: str,
        worker_identity: str,
    ) -> bool: ...

    async def apply_context(self, session: VoiceSessionRecord) -> None: ...

    async def set_capture(self, session: VoiceSessionRecord, enabled: bool) -> None: ...

    async def barge_in(self, session: VoiceSessionRecord) -> None: ...

    async def stop_speech(self, session: VoiceSessionRecord) -> None: ...

    async def end(self, session: VoiceSessionRecord, reason: str) -> None: ...

    async def abort(self, session: VoiceSessionRecord) -> None: ...

    async def rotate_media_grant(
        self,
        previous: VoiceSessionRecord,
        session: VoiceSessionRecord,
        *,
        refresh_id: str,
    ) -> Mapping[str, Any]: ...


class VoiceSessionRuntime:
    """Generation-fenced API runtime over PostgreSQL and direct RTC."""

    def __init__(
        self,
        *,
        repository: VoiceSessionRepository,
        capability: Any,
        media: VoiceMediaActivator,
        replica_id: str,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 45,
        media_grant_seconds: int = 300,
        media_grant_secret: bytes | None = None,
        observability: Any | None = None,
        speech_backend: VoiceSpeechBackend = VoiceSpeechBackend.LLM_FACTORY,
    ) -> None:
        if not 15 <= lease_seconds <= 300:
            raise ValueError("invalid_voice_lease")
        if not 30 <= media_grant_seconds <= 300:
            raise ValueError("invalid_media_grant_lifetime")
        if not isinstance(replica_id, str) or not 1 <= len(replica_id) <= 128:
            raise ValueError("invalid_replica_id")
        self._repository = repository
        self._capability = capability
        self._media = media
        self._replica_id = replica_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease = timedelta(seconds=lease_seconds)
        self._grant_lifetime = timedelta(seconds=media_grant_seconds)
        if media_grant_secret is not None and (
            not isinstance(media_grant_secret, bytes)
            or not 32 <= len(media_grant_secret) <= 512
        ):
            raise ValueError("invalid_media_grant_secret")
        self._media_grant_secret = media_grant_secret
        self._observability = observability
        self.speech_backend = speech_backend
        self._speech_mute_handler: (
            Callable[[str, int, bool], Awaitable[None]] | None
        ) = None
        self._speech_stop_handler: (
            Callable[[str, int], Awaitable[None]] | None
        ) = None
        self._speech_suspend_handler: (
            Callable[[str, int, bool], Awaitable[None]] | None
        ) = None
        self._session_end_handler: (
            Callable[[VoiceSessionRecord, str], Awaitable[None]] | None
        ) = None
        self._session_state_publisher: (
            Callable[[VoiceSessionRecord], Awaitable[None]] | None
        ) = None
        self._local_buffer_cleanup_handler: (
            Callable[[VoiceSessionRecord], Awaitable[None]] | None
        ) = None
        self._active_worker_assignments: dict[str, _ActiveWorkerAssignment] = {}
        self._pending_local_activation_cleanup: dict[
            _LocalActivationReservation, _PendingLocalActivationCleanup
        ] = {}
        self._local_activation_reservations: set[
            _LocalActivationReservation
        ] = set()
        self._local_activation_keys: dict[
            tuple[str, str], _LocalActivationKeyState
        ] = {}
        self._local_activation_capacity_lock = asyncio.Lock()

    def bind_speech_mute_handler(
        self,
        handler: Callable[[str, int, bool], Awaitable[None]],
    ) -> None:
        """Bind the sole server-owned cadence stream after service assembly."""

        if not callable(handler):
            raise TypeError("speech mute handler must be callable")
        if self._speech_mute_handler is not None:
            raise RuntimeError("speech_mute_handler_already_bound")
        self._speech_mute_handler = handler

    def bind_speech_stop_handler(
        self,
        handler: Callable[[str, int], Awaitable[None]],
    ) -> None:
        """Bind intentional source interruption to the serialized stream."""

        if not callable(handler):
            raise TypeError("speech stop handler must be callable")
        if self._speech_stop_handler is not None:
            raise RuntimeError("speech_stop_handler_already_bound")
        self._speech_stop_handler = handler

    def bind_speech_suspend_handler(
        self,
        handler: Callable[[str, int, bool], Awaitable[None]],
    ) -> None:
        """Bind foreground suspension to the server-owned output gate."""

        if not callable(handler):
            raise TypeError("speech suspend handler must be callable")
        if self._speech_suspend_handler is not None:
            raise RuntimeError("speech_suspend_handler_already_bound")
        self._speech_suspend_handler = handler

    def bind_session_end_handler(
        self,
        handler: Callable[[VoiceSessionRecord, str], Awaitable[None]],
    ) -> None:
        """Bind exact-session timer/announcement cleanup after durable end."""

        if not callable(handler):
            raise TypeError("session end handler must be callable")
        if self._session_end_handler is not None:
            raise RuntimeError("session_end_handler_already_bound")
        self._session_end_handler = handler

    def bind_local_buffer_cleanup_handler(
        self,
        handler: Callable[[VoiceSessionRecord], Awaitable[None]],
    ) -> None:
        if not callable(handler):
            raise TypeError("local buffer cleanup handler must be callable")
        if self._local_buffer_cleanup_handler is not None:
            raise RuntimeError("local_buffer_cleanup_handler_already_bound")
        self._local_buffer_cleanup_handler = handler

    def bind_session_state_publisher(
        self,
        handler: Callable[[VoiceSessionRecord], Awaitable[None]],
    ) -> None:
        """Bind the owner-socket ``voice_session_state`` projection push.

        Every client shipped a ``voice_session_state`` reducer in feature 065
        (context resync, microphone restore, ended teardown) but the server
        never emitted the frame; a chat-context switch therefore had no
        asynchronous confirmation and a reaper-ended session left clients
        believing a session still existed.
        """

        if not callable(handler):
            raise TypeError("session state publisher must be callable")
        if self._session_state_publisher is not None:
            raise RuntimeError("session_state_publisher_already_bound")
        self._session_state_publisher = handler

    async def publish_session_state(self, session: VoiceSessionRecord) -> None:
        """Best-effort durable-state push; REST results stay authoritative."""

        handler = self._session_state_publisher
        if handler is None:
            return
        try:
            await handler(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "voice_session_state_publish_unavailable",
            )

    def release_worker_assignment_fence(
        self,
        session: VoiceSessionRecord,
    ) -> None:
        """Forget one exact activation after any durable lifecycle end."""

        self._forget_worker_assignment(session)

    async def get_capability(self, *, user_id: str) -> Mapping[str, Any]:
        _user_id(user_id)
        capability = await self._capability.readiness()
        return capability.to_dict()

    async def get_composer_state(
        self,
        *,
        user_id: str,
        device_id: str,
        device_kind: str,
        connection_generation: str,
        selected_chat_id: str | None,
        revision: int,
    ) -> Mapping[str, Any]:
        """Project one authoritative cross-client composer state.

        This read contains no media bearer or transcript content. It is used by
        the authenticated WebSocket publisher after registration and after
        every REST lifecycle mutation, so controls cannot drift from the
        durable owner/generation state.
        """

        from webrender.chrome.composer_model import (
            VoiceComposerContext,
            VoiceOwner,
            build_composer_state,
        )

        checked_user = _user_id(user_id)
        capability = await self._capability.readiness()
        session = await asyncio.to_thread(
            self._repository.get_live_session,
            user_id=checked_user,
        )
        available = capability.status == "ready" and capability.reason == "ready"
        if session is None:
            return build_composer_state(
                VoiceComposerContext(
                    revision=revision,
                    connection_generation=connection_generation,
                    local_device_id=device_id,
                    available=available,
                    state="off" if available else "unavailable",
                    reason="ready"
                    if available
                    else _composer_reason(capability.reason),
                    selected_chat_id=selected_chat_id,
                    visible_chat_id=selected_chat_id,
                )
            )

        owns_session = session.device_id == device_id
        state, reason = _composer_session_state(session, owns_session=owns_session)
        return build_composer_state(
            VoiceComposerContext(
                revision=revision,
                connection_generation=connection_generation,
                local_device_id=device_id,
                available=available,
                state=state,
                reason=reason,
                speech_muted=session.speech_muted,
                microphone_enabled=(
                    owns_session
                    and session.foreground_active
                    and session.microphone_enabled
                ),
                foreground_active=owns_session and session.foreground_active,
                chat_context_revision=session.chat_context_revision,
                applied_chat_context_revision=(session.applied_chat_context_revision),
                session_id=session.session_id,
                generation=session.generation,
                media_grant_revision=session.media_grant_revision,
                visible_chat_id=session.visible_chat_id,
                selected_chat_id=selected_chat_id,
                owner_device=VoiceOwner(
                    session.device_id,
                    session.device_kind,
                    session.generation,
                ),
                idle_expires_at=(
                    None
                    if session.idle_expires_at is None
                    else _iso(session.idle_expires_at)
                ),
                message=_composer_session_message(
                    session,
                    state=state,
                    owns_session=owns_session,
                ),
            )
        )

    async def create_session(
        self,
        *,
        user_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> VoiceHttpResult:
        now = self._now()
        await self._require_ready()
        create = self._create_request(user_id, control, request, now=now)
        local_reservation = None
        if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
            local_reservation = await self._reserve_local_activation(create)
        mutation_task = asyncio.create_task(
            asyncio.to_thread(
                self._repository.create_session,
                create,
                now=now,
            )
        )
        try:
            mutation = await asyncio.shield(mutation_task)
        except asyncio.CancelledError as cancellation:
            mutation, _mutation_error, _extra_cancellation = (
                await _join_task_outcome_through_cancellation(mutation_task)
            )
            if (
                mutation is not None
                and self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
            ):
                await self._settle_cancelled_local_mutation(
                    mutation,
                    create,
                    local_reservation,
                )
            elif self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
                await self._join_local_activation_release(local_reservation)
            raise cancellation
        except TakeoverRequired as exc:
            if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
                await self._join_local_activation_release(local_reservation)
            raise VoiceApiError(
                "voice_takeover_required",
                status_code=409,
                payload={"owner": _session_projection(exc.current)},
            ) from None
        except Exception:
            if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
                await self._join_local_activation_release(local_reservation)
            raise
        try:
            if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
                session = await self._activate_local(
                    mutation.session,
                    create,
                    local_reservation,
                    replayed=mutation.replayed,
                )
                media_grant = None
            else:
                session, media_grant = await self._activate(mutation.session, create)
        except asyncio.CancelledError:
            self._record_session_event(
                mutation.session,
                "session",
                "failed",
                reason="internal_error",
            )
            raise
        except Exception:
            self._record_session_event(
                mutation.session,
                "session",
                "failed",
                reason="internal_error",
            )
            raise
        if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
            await self._complete_local_activation_before_return(
                session,
                create,
                local_reservation,
                replayed=mutation.replayed,
            )
        self._observe_session_timing(
            session,
            "activation",
            max(0.0, (self._now() - now).total_seconds()),
        )
        if mutation.replayed:
            self._record_session_event(
                session,
                "deduplication",
                "replayed",
                reason="activation_replay",
            )
        else:
            self._record_session_event(session, "session", "started")
            self._record_session_state(session, "starting", "none")
        payload = {"session": _session_projection(session)}
        if media_grant is not None:
            payload["grant"] = media_grant
        return VoiceHttpResult(
            payload,
            status_code=200 if mutation.replayed else 201,
        )

    async def take_over_session(
        self,
        *,
        user_id: str,
        session_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> VoiceHttpResult:
        now = self._now()
        await self._require_ready()
        create = self._create_request(user_id, control, request, now=now)
        takeover = SessionTakeover(
            previous_session_id=session_id,
            expected_generation=request["expected_generation"],
            expected_media_grant_revision=request["expected_media_grant_revision"],
            create=create,
        )
        previous = await asyncio.to_thread(
            self._repository.get_session,
            user_id=user_id,
            session_id=session_id,
        )
        local_reservation = None
        if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
            local_reservation = await self._reserve_local_activation(create)
        mutation_task = asyncio.create_task(
            asyncio.to_thread(
                self._repository.take_over_session,
                takeover,
                now=now,
            )
        )
        try:
            mutation = await asyncio.shield(mutation_task)
        except asyncio.CancelledError as cancellation:
            mutation, _mutation_error, _extra_cancellation = (
                await _join_task_outcome_through_cancellation(mutation_task)
            )
            if (
                mutation is not None
                and self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
            ):
                await self._settle_cancelled_local_mutation(
                    mutation,
                    create,
                    local_reservation,
                )
            elif self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
                await self._join_local_activation_release(local_reservation)
            raise cancellation
        except Exception:
            if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
                await self._join_local_activation_release(local_reservation)
            raise
        activation_started = False
        try:
            await self._cleanup_ended_session(previous, "takeover", fail_open=True)
            if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
                activation_started = True
                session = await self._activate_local(
                    mutation.session,
                    create,
                    local_reservation,
                    replayed=mutation.replayed,
                )
                media_grant = None
            else:
                session, media_grant = await self._activate(mutation.session, create)
        except asyncio.CancelledError as cancellation:
            if (
                self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
                and not activation_started
            ):
                await self._settle_cancelled_local_mutation(
                    mutation,
                    create,
                    local_reservation,
                )
            raise cancellation
        except Exception:
            if (
                self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
                and not activation_started
            ):
                await self._settle_failed_local_mutation(
                    mutation,
                    create,
                    local_reservation,
                )
            raise
        if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
            await self._complete_local_activation_before_return(
                session,
                create,
                local_reservation,
                replayed=mutation.replayed,
            )
        self._record_session_event(
            session,
            "takeover",
            "succeeded",
            reason="takeover",
        )
        self._record_session_event(
            session,
            "session",
            "started",
            reason="takeover",
        )
        self._record_session_state(session, "starting", "takeover")
        payload = {"session": _session_projection(session)}
        if media_grant is not None:
            payload["grant"] = media_grant
        return VoiceHttpResult(
            payload,
            status_code=200,
        )

    async def update_session(
        self,
        *,
        user_id: str,
        session_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        now = self._now()
        update = SessionUpdate(
            user_id=user_id,
            session_id=session_id,
            expected_generation=request["expected_generation"],
            expected_media_grant_revision=request["expected_media_grant_revision"],
            control=_control(control),
            visible_chat_id=request.get("visible_chat_id"),
            speech_muted=request.get("speech_muted"),
            microphone_enabled=request.get("microphone_enabled"),
            foreground_active=request.get("foreground_active"),
            foreground_reason=request.get("foreground_reason"),
            interaction=request.get("interaction"),
        )
        session = await asyncio.to_thread(
            self._repository.update_session,
            update,
            now=now,
        )
        # Every authenticated, generation-fenced owner PATCH is also the
        # reconnect/crash lease heartbeat.  The request may remain a semantic
        # no-op (and therefore must not reset true-idle time); only server
        # receipt time extends this independent cleanup lease.
        session = await asyncio.to_thread(
            self._repository.renew_session_lease,
            user_id=user_id,
            session_id=session_id,
            expected_generation=session.generation,
            expected_media_grant_revision=session.media_grant_revision,
            lease_duration=self._lease,
            now=now,
        )
        await self._claim_control(session, now)
        if (
            self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
            and self._local_buffer_cleanup_handler is not None
            and any(
                request.get(name) is not None
                for name in (
                    "visible_chat_id",
                    "speech_muted",
                    "microphone_enabled",
                    "foreground_active",
                )
            )
        ):
            await self._local_buffer_cleanup_handler(session)
        if request.get("visible_chat_id") is not None:
            await self._media.apply_context(session)
            applied = await asyncio.to_thread(
                self._repository.apply_chat_context,
                user_id=user_id,
                session_id=session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                control_owner_id=self._replica_id,
                visible_chat_id=session.visible_chat_id,
                chat_context_revision=session.chat_context_revision,
                now=self._now(),
            )
            session = applied.session
        if request.get("foreground_active") is False:
            if self._speech_suspend_handler is not None:
                await self._speech_suspend_handler(
                    session.session_id,
                    session.generation,
                    True,
                )
            else:
                await self._media.stop_speech(session)
            if request.get("speech_muted") is not None:
                if self._speech_mute_handler is not None:
                    await self._speech_mute_handler(
                        session.session_id,
                        session.generation,
                        session.speech_muted,
                    )
                elif session.speech_muted:
                    await self._media.stop_speech(session)
            # Keep the persistent server-owned speech gate installed if the
            # worker capture command fails.  The durable session is already
            # backgrounded and must not publish unsolicited output.
            await self._media.set_capture(session, False)
        elif request.get("foreground_active") is True:
            if request.get("speech_muted") is not None:
                if self._speech_mute_handler is not None:
                    await self._speech_mute_handler(
                        session.session_id,
                        session.generation,
                        session.speech_muted,
                    )
                elif session.speech_muted:
                    await self._media.stop_speech(session)
            await self._media.set_capture(session, bool(session.microphone_enabled))
            session = await asyncio.to_thread(
                self._repository.mark_session_active,
                user_id=user_id,
                session_id=session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                now=self._now(),
            )
            # Release queued speech only after context/capture restoration and
            # the durable active transition both succeed.  A failed foreground
            # attempt therefore remains safely suspended for an explicit retry.
            if self._speech_suspend_handler is not None:
                await self._speech_suspend_handler(
                    session.session_id,
                    session.generation,
                    False,
                )
        if (
            request.get("foreground_active") is None
            and request.get("speech_muted") is not None
        ):
            if self._speech_mute_handler is not None:
                await self._speech_mute_handler(
                    session.session_id,
                    session.generation,
                    session.speech_muted,
                )
            elif session.speech_muted:
                await self._media.stop_speech(session)
        await self.publish_session_state(session)
        return _session_projection(session)

    async def end_session(
        self,
        *,
        user_id: str,
        session_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> None:
        ended = await asyncio.to_thread(
            self._repository.end_session,
            user_id=user_id,
            session_id=session_id,
            expected_generation=request["expected_generation"],
            expected_media_grant_revision=request["expected_media_grant_revision"],
            control=_control(control),
            reason="user",
            now=self._now(),
        )
        # The durable end is authoritative. A worker may already have fenced
        # the assignment, or LiveKit may have already removed the room; those
        # stale cleanup outcomes must not turn a successful DELETE into 503.
        await self._cleanup_ended_session(ended, "user", fail_open=True)
        await self.publish_session_state(ended)
        self._record_session_event(
            ended,
            "session",
            "ended",
            reason="user_request",
        )
        self._record_session_state(ended, "ended", "user_request")

    async def stop_speech(
        self,
        *,
        user_id: str,
        session_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> None:
        session = await asyncio.to_thread(
            self._repository.get_controlled_session,
            user_id=user_id,
            session_id=session_id,
            expected_generation=request["expected_generation"],
            expected_media_grant_revision=request["expected_media_grant_revision"],
            control=_control(control),
            now=self._now(),
        )
        if (
            self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
            and self._local_buffer_cleanup_handler is not None
        ):
            await self._local_buffer_cleanup_handler(session)
        if self._speech_stop_handler is not None:
            await self._speech_stop_handler(
                session.session_id,
                session.generation,
            )
        else:
            await self._media.barge_in(session)

    async def get_media_grant_state(
        self,
        *,
        user_id: str,
        session_id: str,
        control: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Recover current fences without returning a bearer or identity."""

        current = await asyncio.to_thread(
            self._repository.get_session,
            user_id=user_id,
            session_id=session_id,
        )
        session = await asyncio.to_thread(
            self._repository.get_controlled_session,
            user_id=user_id,
            session_id=session_id,
            expected_generation=current.generation,
            expected_media_grant_revision=current.media_grant_revision,
            control=_control(control),
            now=self._now(),
        )
        return _media_grant_state(session, now=self._now())

    async def refresh_media_grant(
        self,
        *,
        user_id: str,
        session_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> VoiceHttpResult:
        """Rotate once, wait for worker application, then expose the grant."""

        await self._require_ready()
        now = self._now()
        current = await asyncio.to_thread(
            self._repository.get_session,
            user_id=user_id,
            session_id=session_id,
        )
        replay_candidate = current.last_media_refresh_id == request["refresh_id"]
        expected_generation = request["expected_generation"]
        expected_revision = request["expected_media_grant_revision"]
        if replay_candidate:
            if (
                current.generation != expected_generation
                or current.media_grant_revision != expected_revision + 1
            ):
                raise _grant_conflict(
                    "refresh_id_payload_mismatch",
                    current,
                    now=now,
                    retryable=False,
                )
            controlled_revision = current.media_grant_revision
        else:
            controlled_revision = expected_revision
        try:
            previous = await asyncio.to_thread(
                self._repository.get_controlled_session,
                user_id=user_id,
                session_id=session_id,
                expected_generation=expected_generation,
                expected_media_grant_revision=controlled_revision,
                control=_control(control),
                now=now,
            )
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code in {"stale_generation", "stale_media_grant_revision"}:
                raise _grant_conflict(
                    code,
                    current,
                    now=now,
                    retryable=True,
                ) from None
            raise
        refresh_id = request["refresh_id"]
        if previous.last_media_refresh_id == refresh_id:
            refresh = MediaGrantRefresh(
                user_id=user_id,
                session_id=session_id,
                refresh_id=refresh_id,
                expected_generation=request["expected_generation"],
                expected_media_grant_revision=request["expected_media_grant_revision"],
                participant_identity=previous.participant_identity,
                nonce_hash=previous.media_grant_nonce_hash,
                issued_at=previous.media_grant_issued_at,
                expires_at=previous.media_grant_expires_at,
            )
        else:
            revision = previous.media_grant_revision + 1
            stable = hashlib.sha256(
                b"astraldeep.voice.media-refresh.v1\0"
                + user_id.encode("utf-8")
                + b"\0"
                + session_id.encode("ascii")
                + b"\0"
                + refresh_id.encode("ascii")
            ).hexdigest()
            nonce_hash = hashlib.sha256(stable.encode("ascii")).digest()
            participant_identity = f"client-{stable[:64]}"
            if previous.transport == "watch_pcm_websocket":
                nonce = self._watch_nonce(
                    user_id=user_id,
                    session_key=refresh_id,
                    generation=previous.generation,
                    media_grant_revision=revision,
                    device_id=previous.device_id,
                    connection_generation=control["connection_generation"],
                )
                nonce_hash = hashlib.sha256(nonce).digest()
                participant_identity = watch_participant_identity(nonce)
            refresh = MediaGrantRefresh(
                user_id=user_id,
                session_id=session_id,
                refresh_id=refresh_id,
                expected_generation=previous.generation,
                expected_media_grant_revision=previous.media_grant_revision,
                participant_identity=participant_identity,
                nonce_hash=nonce_hash,
                issued_at=now,
                expires_at=now + self._grant_lifetime,
            )
        try:
            mutation = await asyncio.to_thread(
                self._repository.refresh_media_grant,
                refresh,
                now=now,
            )
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code in {
                "stale_generation",
                "stale_media_grant_revision",
                "refresh_id_payload_mismatch",
                "refresh_replay_expired",
            }:
                raise _grant_conflict(
                    code,
                    current,
                    now=now,
                    retryable=code
                    in {"stale_generation", "stale_media_grant_revision"},
                ) from None
            raise
        session = mutation.session
        self._refresh_worker_assignment_fence(session)
        if not await self._worker_assignment_is_current(session):
            await self._fail_media_session(
                session,
                control=_control(control),
            )
            raise VoiceApiError(
                "worker_assignment_unavailable",
                status_code=503,
            )
        try:
            grant = await self._media.rotate_media_grant(
                previous,
                session,
                refresh_id=refresh_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not await self._worker_assignment_is_current(session):
                await self._fail_media_session(
                    session,
                    control=_control(control),
                )
            raise VoiceApiError(
                getattr(exc, "code", "media_grant_apply_failed"),
                status_code=503,
            ) from None
        if not await self._worker_assignment_is_current(session):
            await self._fail_media_session(
                session,
                control=_control(control),
            )
            raise VoiceApiError(
                "worker_assignment_unavailable",
                status_code=503,
            )
        replay_expires_at = min(
            session.media_grant_issued_at + GRANT_REPLAY_WINDOW,
            session.media_grant_expires_at,
        )
        if mutation.replayed:
            self._record_session_event(
                session,
                "deduplication",
                "replayed",
                reason="media_grant_replay",
            )
        else:
            self._record_session_event(
                session,
                "reconnect",
                "recovered",
                reason="media_grant_rotated",
            )
        return VoiceHttpResult(
            {
                "refresh_id": refresh_id,
                "replayed": mutation.replayed,
                "replay_expires_at": _iso(replay_expires_at),
                "session": _session_projection(session),
                "grant": dict(grant),
            },
            status_code=200 if mutation.replayed else 201,
        )

    async def _require_ready(self) -> None:
        if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
            await self._drain_pending_local_activation_cleanup()
            return
        capability = await self._capability.readiness()
        if capability.status == "ready" and capability.reason == "ready":
            return
        status = 429 if capability.reason == "capacity_exhausted" else 503
        raise VoiceApiError(capability.reason, status_code=status)

    def _create_request(
        self,
        user_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        now: datetime,
    ) -> CreateSession:
        _validate_activation(request)
        if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
            _validate_local_activation(request)
        activation_id = request["activation_id"]
        transport = request["capability"]["transport"]
        if self.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL:
            if transport != "client_local":
                raise VoiceApiError("unsupported_transport", status_code=400)
            return CreateSession(
                user_id=user_id,
                activation_id=activation_id,
                device_id=request["device_id"],
                device_kind=request["device_kind"],
                speech_backend="client_local",
                transport="client_local",
                room_name=None,
                participant_identity=None,
                visible_chat_id=request["visible_chat_id"],
                owner_connection_generation=control["connection_generation"],
                control_binding_id=control["binding_id"],
                control_binding_expires_at=control["binding_expires_at"],
                lease_expires_at=now + self._lease,
                media_grant_nonce_hash=None,
                media_grant_issued_at=None,
                media_grant_expires_at=None,
            )
        if request["device_kind"] == "watchos":
            if transport != "watch_pcm_websocket":
                raise VoiceApiError("unsupported_transport", status_code=400)
        elif transport != "livekit":
            raise VoiceApiError("unsupported_transport", status_code=400)
        stable = hashlib.sha256(
            b"astraldeep.voice.session-identities.v1\0"
            + _user_id(user_id).encode("utf-8")
            + b"\0"
            + activation_id.encode("ascii")
        ).hexdigest()
        participant_identity = f"client-{stable[32:64]}"
        nonce_hash = hashlib.sha256(
            b"astraldeep.voice.media-grant.v1\0" + stable.encode("ascii")
        ).digest()
        if transport == "watch_pcm_websocket":
            nonce = self._watch_nonce(
                user_id=user_id,
                session_key=activation_id,
                generation=1,
                media_grant_revision=1,
                device_id=request["device_id"],
                connection_generation=control["connection_generation"],
            )
            participant_identity = watch_participant_identity(nonce)
            nonce_hash = hashlib.sha256(nonce).digest()
        return CreateSession(
            user_id=user_id,
            activation_id=activation_id,
            device_id=request["device_id"],
            device_kind=request["device_kind"],
            transport=transport,
            room_name=f"voice-{stable[:32]}",
            participant_identity=participant_identity,
            visible_chat_id=request["visible_chat_id"],
            owner_connection_generation=control["connection_generation"],
            control_binding_id=control["binding_id"],
            control_binding_expires_at=control["binding_expires_at"],
            lease_expires_at=now + self._lease,
            media_grant_nonce_hash=nonce_hash,
            media_grant_issued_at=now,
            media_grant_expires_at=now + self._grant_lifetime,
            speech_backend="llm_factory",
        )

    async def _activate_local(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation | None,
        *,
        replayed: bool = False,
    ) -> VoiceSessionRecord:
        """Activate durable local ownership without constructing media work."""

        exact_reservation = self._require_local_activation_reservation(reservation)
        try:
            if session.ended_at is not None:
                raise VoiceApiError("activation_replay_ended", status_code=409)
            if session.speech_backend != "client_local":
                raise VoiceApiError("backend_mismatch", status_code=409)
            await self._claim_control(session, self._now())
            applied = await asyncio.to_thread(
                self._repository.apply_chat_context,
                user_id=create.user_id,
                session_id=session.session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                control_owner_id=self._replica_id,
                visible_chat_id=session.visible_chat_id,
                chat_context_revision=session.chat_context_revision,
                now=self._now(),
            )
            active = await asyncio.to_thread(
                self._repository.mark_session_active,
                user_id=create.user_id,
                session_id=session.session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                now=self._now(),
            )
            if not applied.session.chat_context_synced or not active.chat_context_synced:
                raise RuntimeError("chat_context_not_applied")
            return active
        except asyncio.CancelledError as cancellation:
            await self._settle_local_activation_failure(
                session,
                create,
                exact_reservation,
                replayed=replayed,
            )
            raise cancellation
        except Exception as failure:
            try:
                await self._settle_local_activation_failure(
                    session,
                    create,
                    exact_reservation,
                    replayed=replayed,
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("voice_local_activation_settlement_unavailable")
            raise failure

    def _watch_nonce(
        self,
        *,
        user_id: str,
        session_key: str,
        generation: int,
        media_grant_revision: int,
        device_id: str,
        connection_generation: str,
    ) -> bytes:
        secret = self._media_grant_secret
        if secret is None:
            raise VoiceApiError("watch_bridge_unavailable", status_code=503)
        return derive_watch_nonce(
            secret,
            user_id=user_id,
            session_key=session_key,
            generation=generation,
            media_grant_revision=media_grant_revision,
            device_id=device_id,
            connection_generation=connection_generation,
        )

    async def _activate(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
    ) -> tuple[VoiceSessionRecord, Mapping[str, Any]]:
        if session.ended_at is not None:
            raise VoiceApiError("activation_replay_ended", status_code=409)
        await self._claim_control(session, self._now())
        try:
            receipt = await self._media.activate(session)
            assigned = await asyncio.to_thread(
                self._repository.assign_worker,
                user_id=create.user_id,
                session_id=session.session_id,
                expected_generation=session.generation,
                assignment_id=receipt.assignment_id,
                worker_identity=receipt.worker_identity,
                issued_at=receipt.worker_grant_issued_at,
                expires_at=receipt.worker_grant_expires_at,
                now=receipt.worker_grant_issued_at,
            )
            if assigned.session.worker_identity != receipt.worker_identity:
                raise RuntimeError("worker_assignment_mismatch")
            self._active_worker_assignments[session.session_id] = (
                _ActiveWorkerAssignment(
                    session=assigned.session,
                    assignment_id=receipt.assignment_id,
                    worker_identity=receipt.worker_identity,
                )
            )
            if not await self._worker_assignment_is_current(
                assigned.session,
            ):
                raise VoiceApiError(
                    "worker_assignment_unavailable",
                    status_code=503,
                )
            applied = await asyncio.to_thread(
                self._repository.apply_chat_context,
                user_id=create.user_id,
                session_id=session.session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                control_owner_id=self._replica_id,
                visible_chat_id=session.visible_chat_id,
                chat_context_revision=session.chat_context_revision,
                now=self._now(),
            )
            active = await asyncio.to_thread(
                self._repository.mark_session_active,
                user_id=create.user_id,
                session_id=session.session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                now=self._now(),
            )
            if (
                not applied.session.chat_context_synced
                or not active.chat_context_synced
            ):
                raise RuntimeError("chat_context_not_applied")
            self._refresh_worker_assignment_fence(active)
            if not await self._worker_assignment_is_current(active):
                raise VoiceApiError(
                    "worker_assignment_unavailable",
                    status_code=503,
                )
            return active, dict(receipt.client_grant)
        except asyncio.CancelledError:
            await asyncio.shield(self._abort_activation(session, create))
            raise
        except Exception:
            await self._abort_activation(session, create)
            raise

    @staticmethod
    def _require_local_activation_reservation(
        reservation: _LocalActivationReservation | None,
    ) -> _LocalActivationReservation:
        if not isinstance(reservation, _LocalActivationReservation):
            raise RuntimeError("local_activation_reservation_missing")
        return reservation

    @staticmethod
    def _pending_local_activation_matches(
        pending: _PendingLocalActivationCleanup,
        *,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation,
    ) -> bool:
        return (
            pending.reservation is reservation
            and pending.session.session_id == session.session_id
            and pending.session.generation == session.generation
            and pending.create is create
        )

    def _handoff_local_activation_cleanup_locked(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation,
    ) -> _PendingLocalActivationCleanup:
        current = self._pending_local_activation_cleanup.get(reservation)
        if current is not None:
            if not self._pending_local_activation_matches(
                current,
                session=session,
                create=create,
                reservation=reservation,
            ):
                raise RuntimeError("local_activation_cleanup_identity_mismatch")
            return current
        if reservation not in self._local_activation_reservations:
            raise RuntimeError("local_activation_reservation_lost")
        pending = _PendingLocalActivationCleanup(
            session=session,
            create=create,
            reservation=reservation,
        )
        self._pending_local_activation_cleanup[reservation] = pending
        self._local_activation_reservations.remove(reservation)
        return pending

    @staticmethod
    def _local_activation_key(
        reservation: _LocalActivationReservation,
    ) -> tuple[str, str]:
        return reservation.user_id, reservation.activation_id

    def _release_local_activation_locked(
        self,
        reservation: _LocalActivationReservation,
    ) -> None:
        """Release only one exact owner/waiter and advance its FIFO key."""

        self._local_activation_reservations.discard(reservation)
        key = self._local_activation_key(reservation)
        state = self._local_activation_keys.get(key)
        if state is None:
            return
        if state.owner is not reservation:
            state.waiters = deque(
                (candidate, waiter)
                for candidate, waiter in state.waiters
                if candidate is not reservation
            )
            return
        while state.waiters:
            candidate, waiter = state.waiters.popleft()
            if (
                candidate in self._local_activation_reservations
                and not waiter.done()
            ):
                state.owner = candidate
                waiter.set_result(None)
                return
        self._local_activation_keys.pop(key, None)

    async def _reconcile_local_activation_abort(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation,
    ) -> None:
        async with self._local_activation_capacity_lock:
            self._handoff_local_activation_cleanup_locked(
                session,
                create,
                reservation,
            )
        if await self._abort_activation(session, create):
            async with self._local_activation_capacity_lock:
                self._pending_local_activation_cleanup.pop(reservation, None)
                self._release_local_activation_locked(reservation)
            return

    async def _join_local_activation_release(
        self,
        reservation: _LocalActivationReservation | None,
    ) -> None:
        exact_reservation = self._require_local_activation_reservation(reservation)
        task = asyncio.create_task(
            self._release_local_activation(exact_reservation),
            name=(
                "voice-local-activation-release-"
                f"{exact_reservation.activation_id}"
            ),
        )
        _result, error, cancellation = (
            await _join_task_outcome_through_cancellation(task)
        )
        if cancellation is not None:
            raise cancellation
        if error is not None:
            raise error

    async def _join_local_activation_abort(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation | None,
    ) -> None:
        exact_reservation = self._require_local_activation_reservation(reservation)
        task = asyncio.create_task(
            self._reconcile_local_activation_abort(
                session,
                create,
                exact_reservation,
            ),
            name=f"voice-local-activation-abort-{session.session_id}",
        )
        _result, error, cancellation = (
            await _join_task_outcome_through_cancellation(task)
        )
        if cancellation is not None:
            raise cancellation
        if error is not None:
            raise error

    async def _settle_local_activation_failure(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation | None,
        *,
        replayed: bool,
    ) -> None:
        if replayed:
            await self._join_local_activation_release(reservation)
            return
        await self._join_local_activation_abort(session, create, reservation)

    async def _settle_cancelled_local_mutation(
        self,
        mutation: Any,
        create: CreateSession,
        reservation: _LocalActivationReservation | None,
    ) -> None:
        await self._settle_local_activation_failure(
            mutation.session,
            create,
            reservation,
            replayed=bool(mutation.replayed),
        )

    async def _settle_failed_local_mutation(
        self,
        mutation: Any,
        create: CreateSession,
        reservation: _LocalActivationReservation | None,
    ) -> None:
        await self._settle_cancelled_local_mutation(
            mutation,
            create,
            reservation,
        )

    async def _acquire_local_activation_return_handoff(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation,
    ) -> _PendingLocalActivationCleanup:
        await self._local_activation_capacity_lock.acquire()
        try:
            return self._handoff_local_activation_cleanup_locked(
                session,
                create,
                reservation,
            )
        except BaseException:
            self._local_activation_capacity_lock.release()
            raise

    async def _complete_local_activation_before_return(
        self,
        session: VoiceSessionRecord,
        create: CreateSession,
        reservation: _LocalActivationReservation | None,
        *,
        replayed: bool,
    ) -> None:
        exact_reservation = self._require_local_activation_reservation(reservation)
        if replayed:
            await self._join_local_activation_release(exact_reservation)
            return
        task = asyncio.create_task(
            self._acquire_local_activation_return_handoff(
                session,
                create,
                exact_reservation,
            ),
            name=f"voice-local-activation-handoff-{session.session_id}",
        )
        pending, error, cancellation = (
            await _join_task_outcome_through_cancellation(task)
        )
        if error is not None:
            try:
                await self._join_local_activation_abort(
                    session,
                    create,
                    exact_reservation,
                )
            except BaseException:
                logger.warning("voice_local_activation_handoff_unavailable")
            raise error
        if pending is None:
            raise RuntimeError("local_activation_handoff_missing")
        try:
            if cancellation is None:
                current = self._pending_local_activation_cleanup.get(
                    exact_reservation
                )
                if current is not pending:
                    raise RuntimeError("local_activation_cleanup_identity_mismatch")
                self._pending_local_activation_cleanup.pop(exact_reservation)
                self._release_local_activation_locked(exact_reservation)
        finally:
            self._local_activation_capacity_lock.release()
        if cancellation is None:
            return
        try:
            await self._join_local_activation_abort(
                session,
                create,
                exact_reservation,
            )
        except BaseException:
            pass
        raise cancellation

    async def _reserve_local_activation(
        self,
        create: CreateSession,
    ) -> _LocalActivationReservation:
        reservation = _LocalActivationReservation(
            create.user_id,
            create.activation_id,
        )
        waiter: asyncio.Future[None] | None = None
        async with self._local_activation_capacity_lock:
            retained = len(self._pending_local_activation_cleanup)
            reserved = len(self._local_activation_reservations)
            if retained + reserved >= 256:
                raise VoiceApiError(
                    "local_cleanup_capacity_exhausted",
                    status_code=503,
                )
            self._local_activation_reservations.add(reservation)
            key = self._local_activation_key(reservation)
            state = self._local_activation_keys.get(key)
            if state is None:
                self._local_activation_keys[key] = _LocalActivationKeyState(
                    owner=reservation,
                    waiters=deque(),
                )
            else:
                waiter = asyncio.get_running_loop().create_future()
                state.waiters.append((reservation, waiter))
        if waiter is not None:
            try:
                await waiter
            except BaseException as failure:
                cleanup = asyncio.create_task(
                    self._release_local_activation(reservation),
                    name=(
                        "voice-local-activation-waiter-release-"
                        f"{reservation.activation_id}"
                    ),
                )
                _result, error, _cancellation = (
                    await _join_task_outcome_through_cancellation(cleanup)
                )
                if error is not None:
                    raise error
                raise failure
        return reservation

    async def _release_local_activation(
        self,
        reservation: _LocalActivationReservation,
    ) -> None:
        async with self._local_activation_capacity_lock:
            self._release_local_activation_locked(reservation)

    async def _drain_pending_local_activation_cleanup(self) -> None:
        for reservation, pending in tuple(
            self._pending_local_activation_cleanup.items()
        ):
            if await self._abort_activation(pending.session, pending.create):
                async with self._local_activation_capacity_lock:
                    if (
                        self._pending_local_activation_cleanup.get(reservation)
                        is pending
                    ):
                        self._pending_local_activation_cleanup.pop(
                            reservation,
                            None,
                        )
                        self._release_local_activation_locked(reservation)

    async def _abort_activation(
        self, session: VoiceSessionRecord, create: CreateSession
    ) -> bool:
        try:
            await self._media.abort(session)
        except BaseException:
            # Activation cleanup is best effort and must never mask the
            # original activation exception or cancellation.
            pass
        try:
            ended = await asyncio.to_thread(
                self._repository.end_session,
                user_id=create.user_id,
                session_id=session.session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                control=SessionControl(
                    device_id=create.device_id,
                    connection_generation=create.owner_connection_generation,
                    binding_id=create.control_binding_id,
                    binding_expires_at=create.control_binding_expires_at,
                ),
                reason="media_error",
                now=self._now(),
            )
        except BaseException:
            ended = None
        if ended is not None:
            try:
                await self._notify_session_end(ended, "media_error")
            except BaseException:
                pass
        self._forget_worker_assignment(session)
        try:
            return bool(
                ended is not None
                and ended.session_id == session.session_id
                and ended.generation == session.generation
                and ended.media_grant_revision == session.media_grant_revision
                and ended.ended_at is not None
            )
        except AttributeError:
            return False

    async def _fail_media_session(
        self,
        session: VoiceSessionRecord,
        *,
        control: SessionControl,
    ) -> None:
        """Fail closed one exact media generation without cancelling work."""

        try:
            ended = await asyncio.to_thread(
                self._repository.end_session,
                user_id=session.user_id,
                session_id=session.session_id,
                expected_generation=session.generation,
                expected_media_grant_revision=session.media_grant_revision,
                control=control,
                reason="media_error",
                now=self._now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._forget_worker_assignment(session)
            return
        await self._cleanup_ended_session(
            ended,
            "media_error",
            fail_open=True,
        )

    async def _worker_assignment_is_current(
        self,
        session: VoiceSessionRecord,
    ) -> bool:
        """Read-only exact liveness check at every client-grant boundary."""

        fence = self._active_worker_assignments.get(session.session_id)
        if (
            fence is None
            or fence.session.generation != session.generation
            or fence.session.media_grant_revision
            != session.media_grant_revision
        ):
            return False
        checker = getattr(self._media, "assignment_is_current", None)
        if not callable(checker):
            return False
        try:
            return bool(
                await checker(
                    session,
                    assignment_id=fence.assignment_id,
                    worker_identity=fence.worker_identity,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _cleanup_ended_session(
        self,
        session: VoiceSessionRecord,
        reason: str,
        *,
        fail_open: bool = False,
    ) -> None:
        """Close worker media and server timers for one durable end fence."""

        media_error: Exception | None = None
        notification_error: Exception | None = None
        try:
            await self._media.end(session, reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            media_error = exc
        finally:
            try:
                await self._notify_session_end(session, reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                notification_error = exc
            finally:
                self._forget_worker_assignment(session)
        if fail_open:
            if media_error is not None:
                logger.warning(
                    "voice_media_cleanup_unavailable reason=media_end_failed"
                )
            if notification_error is not None:
                logger.warning(
                    "voice_session_cleanup_unavailable reason=end_handler_failed"
                )
            return
        if media_error is not None:
            raise media_error
        if notification_error is not None:
            raise notification_error

    async def reconcile_worker_disconnect(
        self,
        *,
        worker_identity: str,
        released_session_ids: tuple[str, ...],
        released_assignment_ids: tuple[str, ...],
        assignment_is_current: Callable[[str], bool] | None = None,
    ) -> tuple[VoiceSessionRecord, ...]:
        """Fail closed only exact activations released by one worker transport.

        The pool has already fenced these in-memory assignments. Durable
        compare-and-swap checks below prevent a delayed disconnect callback
        from ending a newer assignment, while accepted turns retain their
        ordinary agentic lifecycle through the repository's media-only end.
        """

        if not isinstance(worker_identity, str) or not worker_identity:
            raise ValueError("invalid_worker_identity")
        if not isinstance(released_session_ids, tuple) or not isinstance(
            released_assignment_ids, tuple
        ):
            raise TypeError("released worker fences must be tuples")
        if assignment_is_current is not None and not callable(
            assignment_is_current
        ):
            raise TypeError("assignment_is_current must be callable")
        session_ids = frozenset(released_session_ids)
        assignment_ids = frozenset(released_assignment_ids)
        candidates = tuple(
            fence
            for session_id, fence in self._active_worker_assignments.items()
            if fence.worker_identity == worker_identity
            and (
                session_id in session_ids
                or fence.assignment_id in assignment_ids
            )
        )
        reconciled: list[VoiceSessionRecord] = []
        for fence in candidates:
            current_fence = self._active_worker_assignments.get(
                fence.session.session_id
            )
            if current_fence != fence:
                continue
            if self._assignment_remains_current(
                fence.session.session_id,
                assignment_is_current,
            ):
                continue
            try:
                current = await asyncio.to_thread(
                    self._repository.get_session,
                    user_id=fence.session.user_id,
                    session_id=fence.session.session_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "voice_worker_disconnect_reconcile_unavailable "
                    "reason=session_read_failed"
                )
                await self._cleanup_ended_session(
                    fence.session,
                    "media_error",
                    fail_open=True,
                )
                continue
            if not self._worker_assignment_matches(current, fence):
                self._forget_worker_assignment(fence.session)
                continue
            if self._assignment_remains_current(
                current.session_id,
                assignment_is_current,
            ):
                continue
            try:
                ended = await asyncio.to_thread(
                    self._repository.end_session,
                    user_id=current.user_id,
                    session_id=current.session_id,
                    expected_generation=current.generation,
                    expected_media_grant_revision=current.media_grant_revision,
                    control=SessionControl(
                        device_id=current.device_id,
                        connection_generation=(
                            current.owner_connection_generation
                        ),
                        binding_id=current.control_binding_id,
                        binding_expires_at=current.control_binding_expires_at,
                    ),
                    reason="media_error",
                    now=self._now(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "voice_worker_disconnect_reconcile_unavailable "
                    "reason=durable_end_failed"
                )
                await self._cleanup_ended_session(
                    current,
                    "media_error",
                    fail_open=True,
                )
                continue
            await self._cleanup_ended_session(
                ended,
                "media_error",
                fail_open=True,
            )
            self._record_session_event(
                ended,
                "session",
                "ended",
                reason="media_error",
            )
            self._record_session_state(ended, "ended", "media_error")
            reconciled.append(ended)
        return tuple(reconciled)

    @staticmethod
    def _assignment_remains_current(
        session_id: str,
        assignment_is_current: Callable[[str], bool] | None,
    ) -> bool:
        if assignment_is_current is None:
            return False
        try:
            return bool(assignment_is_current(session_id))
        except Exception:
            # Uncertain assignment state is a stale-cleanup denial, never
            # authority to end a potentially newer media generation.
            logger.warning(
                "voice_worker_disconnect_reconcile_unavailable "
                "reason=assignment_check_failed"
            )
            return True

    @staticmethod
    def _worker_assignment_matches(
        session: VoiceSessionRecord,
        fence: _ActiveWorkerAssignment,
    ) -> bool:
        return (
            session.ended_at is None
            and session.session_id == fence.session.session_id
            and session.user_id == fence.session.user_id
            and session.generation == fence.session.generation
            and session.media_grant_revision
            == fence.session.media_grant_revision
            and session.worker_assignment_id == fence.assignment_id
            and session.worker_identity == fence.worker_identity
        )

    def _forget_worker_assignment(self, session: VoiceSessionRecord) -> None:
        current = self._active_worker_assignments.get(session.session_id)
        if current is not None and (
            current.session.generation == session.generation
            and (
                session.worker_assignment_id is None
                or current.assignment_id == session.worker_assignment_id
            )
        ):
            self._active_worker_assignments.pop(session.session_id, None)

    def _refresh_worker_assignment_fence(
        self,
        session: VoiceSessionRecord,
    ) -> None:
        current = self._active_worker_assignments.get(session.session_id)
        if current is not None and (
            current.session.generation == session.generation
            and session.worker_assignment_id == current.assignment_id
            and session.worker_identity == current.worker_identity
        ):
            self._active_worker_assignments[session.session_id] = (
                _ActiveWorkerAssignment(
                    session=session,
                    assignment_id=current.assignment_id,
                    worker_identity=current.worker_identity,
                )
            )

    async def _notify_session_end(
        self,
        session: VoiceSessionRecord,
        reason: str,
    ) -> None:
        handler = self._session_end_handler
        if handler is not None:
            await handler(session, reason)

    async def _claim_control(self, session: VoiceSessionRecord, now: datetime) -> None:
        state = await self._repository.claim_control_lease(
            user_id=session.user_id,
            session_id=session.session_id,
            generation=session.generation,
            owner_id=self._replica_id,
            now=now,
        )
        if state.owner_id != self._replica_id:
            raise RuntimeError("voice_control_lease_not_owned")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("voice_clock_invalid")
        return value.astimezone(UTC)

    @staticmethod
    def _metric_dimensions(session: VoiceSessionRecord) -> tuple[str, str]:
        transport = (
            "watch_bridge"
            if session.transport == "watch_pcm_websocket"
            else session.transport
        )
        return session.device_kind, transport

    def _record_session_event(
        self,
        session: VoiceSessionRecord,
        event: str,
        outcome: str,
        *,
        reason: str = "none",
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

    def _record_session_state(
        self,
        session: VoiceSessionRecord,
        state: str,
        reason: str,
    ) -> None:
        if self._observability is None:
            return
        client_kind, transport = self._metric_dimensions(session)
        self._observability.record_voice_state(
            state=state,
            reason=reason,
            client_kind=client_kind,
            transport=transport,
        )

    def _observe_session_timing(
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


def _control(value: Mapping[str, Any]) -> SessionControl:
    return SessionControl(
        device_id=value["device_id"],
        connection_generation=value["connection_generation"],
        binding_id=value["binding_id"],
        binding_expires_at=value["binding_expires_at"],
    )


def _validate_activation(request: Mapping[str, Any]) -> None:
    capability = request.get("capability")
    if not isinstance(capability, Mapping):
        raise VoiceApiError("invalid_request", status_code=400)
    if not request.get("foreground_active"):
        raise VoiceApiError("permission_denied", status_code=400)
    if not capability.get("has_microphone"):
        raise VoiceApiError("no_microphone", status_code=400)
    if not capability.get("has_audio_output"):
        raise VoiceApiError("no_audio_output", status_code=400)
    permission = capability.get("microphone_permission")
    if permission != "authorized":
        reason = (
            "permission_restricted"
            if permission == "restricted"
            else "permission_denied"
        )
        raise VoiceApiError(reason, status_code=400)


def _validate_local_activation(request: Mapping[str, Any]) -> None:
    capability = request.get("capability")
    if not isinstance(capability, Mapping):
        raise VoiceApiError("invalid_request", status_code=400)
    exact = {
        "contract",
        "transport",
        "configured_locale",
        "full_duplex",
        "has_microphone",
        "has_audio_output",
        "microphone_permission",
        "recognition_permission",
        "recognition_processing",
        "recognition_locale",
        "recognition_installation",
        "synthesis_processing",
        "synthesis_locale",
    }
    if set(capability) != exact:
        raise VoiceApiError("invalid_request", status_code=400)
    required = {
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
    }
    if any(capability.get(name) != value for name, value in required.items()):
        raise VoiceApiError("client_readiness_required", status_code=422)


def session_state_frame(
    session: VoiceSessionRecord,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Build the manifest ``voice_session_state`` frame for the owner device.

    The field set matches Projection's ``contracts/ui_protocol.json`` exactly; state/reason
    reuse the same derivation the composer projection applies for the owning
    device, so REST responses, composer frames, and this push cannot disagree.
    """

    if session.ended_at is not None:
        state = "ended"
        if session.end_reason == "idle":
            reason = "idle_expired"
        elif session.end_reason == "lease_expired":
            reason = "network_interrupted"
        else:
            reason = "ended_by_user"
    else:
        state, reason = _composer_session_state(session, owns_session=True)
    return {
        "type": "voice_session_state",
        "schema_version": "1",
        "connection_generation": session.owner_connection_generation,
        "session_id": session.session_id,
        "generation": session.generation,
        "media_grant_revision": session.media_grant_revision,
        "state": state,
        "reason": reason,
        "visible_chat_id": session.visible_chat_id,
        "chat_context_revision": session.chat_context_revision,
        "applied_chat_context_revision": session.applied_chat_context_revision,
        "chat_context_synced": session.chat_context_synced,
        "speech_muted": session.speech_muted,
        "microphone_enabled": (
            session.microphone_enabled if session.foreground_active else False
        ),
        "foreground_active": session.foreground_active,
        "occurred_at": _iso(now),
    }


def _session_projection(session: VoiceSessionRecord) -> dict[str, Any]:
    """Return only the non-secret client session vocabulary."""

    projection = {
        "session_id": session.session_id,
        "device_id": session.device_id,
        "device_kind": session.device_kind,
        "transport": session.transport,
        "state": session.state,
        "generation": session.generation,
        "media_grant_revision": session.media_grant_revision,
        "owner_connection_generation": session.owner_connection_generation,
        "visible_chat_id": session.visible_chat_id,
        "applied_visible_chat_id": session.applied_visible_chat_id,
        "chat_context_revision": session.chat_context_revision,
        "applied_chat_context_revision": session.applied_chat_context_revision,
        "chat_context_synced": session.chat_context_synced,
        "foreground_active": session.foreground_active,
        "foreground_reason": session.foreground_reason,
        "foreground_changed_at": _iso(session.updated_at),
        "speech_muted": session.speech_muted,
        "microphone_enabled": session.microphone_enabled,
        "lease_expires_at": _iso(session.lease_expires_at),
        "started_at": _iso(session.started_at),
        "idle_expires_at": (
            None if session.idle_expires_at is None else _iso(session.idle_expires_at)
        ),
    }
    # The v1 remote response must remain byte-compatible.  The discriminator
    # is emitted only for the separately versioned client-local v2 surface.
    if session.speech_backend == "client_local":
        projection["speech_backend"] = "client_local"
    return projection


def _media_grant_state(
    session: VoiceSessionRecord,
    *,
    now: datetime,
) -> dict[str, Any]:
    if session.ended_at is not None:
        status = "unavailable"
        expires_at = None
    elif session.media_grant_expires_at <= now:
        status = "expired"
        expires_at = _iso(session.media_grant_expires_at)
    elif session.state in {"starting", "reconnecting"}:
        status = "pending_worker"
        expires_at = _iso(session.media_grant_expires_at)
    elif session.state in {"active", "suspended"}:
        status = "active"
        expires_at = _iso(session.media_grant_expires_at)
    else:
        status = "unavailable"
        expires_at = _iso(session.media_grant_expires_at)
    return {
        "session": _session_projection(session),
        "grant_state": {
            "transport": session.transport,
            "media_grant_revision": session.media_grant_revision,
            "status": status,
            "expires_at": expires_at,
        },
    }


def _grant_conflict(
    code: str,
    session: VoiceSessionRecord,
    *,
    now: datetime,
    retryable: bool,
) -> VoiceApiError:
    return VoiceApiError(
        code,
        status_code=409,
        payload={
            "message": "The voice media grant could not be refreshed from that state.",
            "retryable": bool(retryable),
            "current": _media_grant_state(session, now=now),
        },
    )


def _composer_reason(value: object) -> str:
    reason = str(value or "voice_unavailable")
    if reason in {
        "ready",
        "feature_disabled",
        "authentication_required",
        "permission_denied",
        "permission_restricted",
        "no_microphone",
        "no_audio_output",
        "worker_unavailable",
        "asr_unavailable",
        "tts_unavailable",
        "voice_unavailable",
        "output_language_unsupported",
        "capacity_exhausted",
        "internal_error",
    }:
        return reason
    if reason in {"media_unconfigured", "media_unreachable", "unsupported_transport"}:
        return "media_unavailable"
    return "voice_unavailable"


def _composer_session_state(
    session: VoiceSessionRecord, *, owns_session: bool
) -> tuple[str, str]:
    if not owns_session:
        return "suspended", "takeover_required"
    if not session.foreground_active:
        if session.foreground_reason == "audio_interrupted":
            return "suspended", "audio_interrupted"
        if session.foreground_reason in {"connection_lost", "route_unavailable"}:
            return "reconnecting", "network_interrupted"
        return "suspended", "backgrounded"
    if session.state == "starting":
        return "connecting", "ready"
    if session.state == "active":
        return ("muted" if session.speech_muted else "listening"), "ready"
    if session.state == "suspended":
        if session.foreground_reason == "audio_interrupted":
            return "suspended", "audio_interrupted"
        if session.foreground_reason in {"connection_lost", "route_unavailable"}:
            return "reconnecting", "network_interrupted"
        return "suspended", "backgrounded"
    if session.state == "reconnecting":
        return "reconnecting", "network_interrupted"
    if session.state == "error":
        return "error", "media_error"
    if session.state in {"ending", "ended"}:
        reason = "idle_expired" if session.end_reason == "idle" else "ended_by_user"
        return "ended", reason
    return "error", "internal_error"


def _composer_session_message(
    session: VoiceSessionRecord,
    *,
    state: str,
    owns_session: bool,
) -> str | None:
    """Describe independent microphone and assistant-speech mute controls."""

    if (
        not owns_session
        or session.state != "active"
        or not session.foreground_active
        or state not in {"listening", "muted"}
    ):
        return None
    if session.speech_muted and not session.microphone_enabled:
        return "Microphone and assistant speech are muted."
    if session.speech_muted:
        return "Assistant speech is muted."
    if not session.microphone_enabled:
        return "Microphone is off."
    return None


def _user_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("invalid_user_id")
    return value


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


__all__ = [
    "ActivatedVoiceMedia",
    "VoiceMediaActivator",
    "VoiceSessionRuntime",
    "session_state_frame",
]
