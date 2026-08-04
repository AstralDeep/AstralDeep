"""Fail-closed construction of the Feature-065 voice control/media services."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

from orchestrator.livekit_service import (
    LiveKitService,
    LiveKitSettings,
    VoiceCapabilityService,
)
from orchestrator.voice_control_binding import VoiceControlClaims
from orchestrator.voice_coordinator import (
    APPROVED_PHRASE_TEXT,
    PREACCEPTANCE_REJECTION_PHRASES,
    AnnouncementClaimRequest,
    AnnouncementMutation,
    CADENCE_HARD_GAP_SECONDS,
    CadenceDecision,
    CoordinatorClock,
    HANDOFF_BUDGET_SECONDS,
    PlayoutCompletion,
    SpeechCadenceScheduler,
    StaleFence,
    VoiceCoordinator,
    WorkerPool,
    WorkerPoolPolicy,
    WorkerRegistrationReceipt,
)
from orchestrator.voice_media import DirectRtcVoiceMedia
from orchestrator.voice_api import VoiceApiError
from orchestrator.voice_recap import SensitiveRecapRegistry, VoiceRecapError
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.voice_runtime import VoiceSessionRuntime
from orchestrator.voice_sessions import (
    ChatUnavailableMutation,
    SessionControl,
    TranscriptAdmission,
    TranscriptSubmissionRejected,
    TranscriptSubmission,
    VoiceSessionRepository,
    VoiceSessionRecord,
    VoiceTurnRecord,
)
from orchestrator.voice_worker_endpoint import (
    WorkerControlEndpoint,
    WorkerControlSettings,
    install_router,
)
from shared.feature_flags import flags
from shared.protocol import VoicePlayoutEvent


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
logger = logging.getLogger(__name__)
_MAX_ANNOUNCEMENT_FENCES = 256
_MAX_PREACCEPTANCE_GUIDANCE_TASKS = 32


class VoiceBootstrapError(RuntimeError):
    """Content-free deployment refusal that leaves ordinary chat untouched."""


@dataclass(slots=True)
class _AnnouncementCommand:
    """One in-process lifecycle mutation for a session-owned output stream."""

    action: str
    turn: VoiceTurnRecord | None
    future: asyncio.Future[Any] = field(repr=False)
    muted: bool | None = None
    waiting_reason: str | None = None
    terminal_kind: str | None = None
    rejection_reason: str | None = None
    recap_text: str = field(default="", repr=False)
    recap_source: str = "none"
    sensitivity: str = "unknown"
    result_commit_id: str | None = None
    sensitive_text: str = field(default="", repr=False)


@dataclass(slots=True)
class _PreparedQuantum:
    """One already-reserved speech command; text stays process-local."""

    turn: VoiceTurnRecord
    mutation: AnnouncementMutation
    text: str = field(repr=False)
    sensitive_authorized: bool = False
    claim_completed: bool = False


@dataclass(slots=True)
class _QuantumBundle:
    """Serialized terminal or consented recap quanta for one exact turn."""

    turn: VoiceTurnRecord
    quanta: deque[_PreparedQuantum] = field(repr=False)
    completion: asyncio.Future[Any] = field(repr=False)
    speech_outcome: str = "source_finished"
    intentionally_suppressed: bool = False


@dataclass(frozen=True, slots=True)
class VoiceTerminalAnnouncementResult:
    """Durable terminal turn plus its ephemeral source-speech outcome.

    ``source_finished`` means only that every requested speech quantum reached
    the worker/source terminal event.  It does not claim local client playout
    or audibility, which remains independently observed by playout events.
    """

    turn: VoiceTurnRecord
    speech_outcome: str

    def __post_init__(self) -> None:
        if self.speech_outcome not in {
            "source_finished",
            "failed",
            "suppressed",
        }:
            raise ValueError("invalid_terminal_speech_outcome")


class _SessionAnnouncementRunner:
    """Own the sole assistant-speech stream for one session generation.

    Commands are applied while media is in flight.  This lets a same-turn
    waiting or terminal transition fence stale progress immediately, while a
    newer accepted turn is merely queued and never supersedes the older
    turn's current lifecycle utterance.
    """

    _IDLE_WAIT_SECONDS = 60.0
    _SOURCE_TERMINAL_SECONDS = 12.0

    def __init__(
        self,
        services: VoiceServices,
        *,
        session_id: str,
        generation: int,
        clock: CoordinatorClock,
        muted: bool = False,
    ) -> None:
        self._services = services
        self.session_id = session_id
        self.generation = generation
        self._clock = clock
        self._scheduler = SpeechCadenceScheduler(clock)
        self._commands: deque[_AnnouncementCommand] = deque()
        self._wake = asyncio.Event()
        self._terminal: dict[str, _QuantumBundle] = {}
        self._continuations: deque[_QuantumBundle] = deque()
        self._turn_users: dict[str, str] = {}
        self._turn_attribution: dict[str, str] = {}
        self._start_waiters: dict[str, list[asyncio.Future[Any]]] = {}
        self._waiting_waiters: dict[str, list[asyncio.Future[Any]]] = {}
        self._mute_waiters: list[asyncio.Future[Any]] = []
        self._stop_waiters: list[asyncio.Future[Any]] = []
        self._muted = muted
        self._speaking = False
        self._active_bundle: _QuantumBundle | None = None
        self._active_turn_id: str | None = None
        self._stop_requested = False
        self._closing = False
        self.task = asyncio.create_task(
            self._run(),
            name=f"voice-announcements-{session_id}",
        )

    def submit(self, command: _AnnouncementCommand) -> None:
        """Queue one bounded command without starting another speech task."""

        if self._closing or self.task.done():
            raise VoiceBootstrapError("voice_announcement_runner_unavailable")
        if len(self._commands) >= 32:
            raise VoiceBootstrapError("voice_announcement_queue_full")
        self._commands.append(command)
        self._wake.set()

    def wake(self) -> None:
        """Wake fake-clock tests or an externally advanced monotonic source."""

        self._wake.set()

    async def close(self) -> None:
        """Fence timers/queued speech and settle callers without content."""

        if self._closing:
            await asyncio.gather(self.task, return_exceptions=True)
            return
        self._closing = True
        self._wake.set()
        if self._speaking:
            await self._stop_current_speech()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self._fail_pending(VoiceBootstrapError("voice_announcement_runner_closed"))

    async def _run(self) -> None:
        try:
            while not self._closing:
                await self._drain_commands()
                if self._closing:
                    return
                decision = self._scheduler.next_decision()
                if decision is not None:
                    await self._execute_decision(decision)
                    continue
                bundle = self._next_continuation()
                if bundle is not None:
                    await self._execute_continuation(bundle)
                    continue
                await self._wait_for_work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A command can fail after it fenced the scheduler but before the
            # in-flight media waiter reports terminal.  Always send the exact
            # session stop on runner failure so stale audio cannot outlive it.
            await self._stop_current_speech()
            logger.warning(
                "voice_announcement_runner_failed reason=%s",
                _safe_failure_reason(exc),
            )
            self._fail_pending(VoiceBootstrapError("voice_announcement_runner_failed"))

    async def _drain_commands(self) -> bool:
        preempted = False
        while self._commands:
            command = self._commands.popleft()
            try:
                preempted = await self._apply_command(command) or preempted
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not command.future.done():
                    command.future.set_exception(exc)
                if command.action == "terminal":
                    raise
        if not self._commands:
            self._wake.clear()
        stop_requested = self._stop_requested
        self._stop_requested = False
        if preempted or stop_requested:
            await self._stop_current_speech(barge_in=stop_requested)
        for future in self._mute_waiters:
            _settle(future, None)
        self._mute_waiters.clear()
        for future in self._stop_waiters:
            _settle(future, None)
        self._stop_waiters.clear()
        return preempted

    async def _apply_command(self, command: _AnnouncementCommand) -> bool:
        if command.action == "mute":
            if not isinstance(command.muted, bool):
                raise ValueError("invalid_speech_muted")
            preempted = self._set_muted(command.muted)
            if preempted:
                self._mute_waiters.append(command.future)
            else:
                _settle(command.future, None)
            return preempted
        if command.action == "stop":
            preempted = self._mark_intentional_stop()
            self._stop_requested = True
            self._stop_waiters.append(command.future)
            return preempted
        turn = command.turn
        if turn is None:
            raise TypeError("turn must be VoiceTurnRecord")
        self._validate_turn(turn)
        if command.action == "preacceptance":
            if self._muted:
                _settle(command.future, None)
                return False
            quantum = await self._services._prepare_preacceptance_quantum(
                turn,
                reason=command.rejection_reason,
            )
            self._continuations.append(
                _QuantumBundle(
                    turn=turn,
                    quanta=deque((quantum,)),
                    completion=command.future,
                )
            )
            return False
        if command.action == "start":
            self._ensure_turn(turn)
            if (
                self._muted
                or self._scheduler.snapshot(turn.turn_id).acknowledgement_started
            ):
                _settle(command.future, None)
            else:
                self._start_waiters.setdefault(turn.turn_id, []).append(command.future)
            return False
        if command.action == "waiting":
            self._ensure_turn(turn)
            if self._scheduler.snapshot(turn.turn_id).lifecycle == "waiting_on_user":
                _settle(command.future, None)
                return False
            preempted = self._scheduler.set_lifecycle(
                turn.turn_id,
                "waiting_on_user",
                waiting_reason=command.waiting_reason,
            )
            self._waiting_waiters.setdefault(turn.turn_id, []).append(command.future)
            await self._services._set_turn_idle(
                turn,
                listening=False,
                user_input_gate=True,
            )
            if self._muted:
                self._settle_waiters(self._waiting_waiters, turn.turn_id)
            return preempted
        if command.action == "resume":
            self._ensure_turn(turn)
            preempted = self._scheduler.set_lifecycle(
                turn.turn_id,
                "processing",
            )
            await self._services._set_turn_idle(
                turn,
                listening=False,
                user_input_gate=False,
            )
            _settle(command.future, None)
            return preempted
        if command.action == "abandon":
            preempted = self._abandon_turn(turn)
            _settle(command.future, None)
            return preempted
        if command.action == "terminal":
            self._ensure_turn(turn)
            lifecycle = command.terminal_kind
            if lifecycle not in {"succeeded", "failed", "refused", "cancelled"}:
                raise ValueError("invalid_terminal_kind")
            preempted = self._scheduler.set_lifecycle(turn.turn_id, lifecycle)
            (
                terminal_turn,
                quanta,
                reservation_complete,
            ) = await self._services._prepare_terminal_quanta(
                turn,
                terminal_kind=lifecycle,
                recap_text=command.recap_text,
                recap_source=command.recap_source,
                sensitivity=command.sensitivity,
                result_commit_id=command.result_commit_id,
                attribution=self._turn_attribution.get(turn.turn_id),
            )
            self._terminal[turn.turn_id] = _QuantumBundle(
                turn=terminal_turn,
                quanta=deque(quanta),
                completion=command.future,
                speech_outcome=(
                    "suppressed"
                    if self._muted
                    else (
                        "source_finished"
                        if quanta and reservation_complete
                        else "failed"
                    )
                ),
            )
            if self._muted:
                self._drop_muted_bundles()
            return preempted
        if command.action == "sensitive":
            if self._muted:
                _settle(command.future, None)
                return False
            quanta = await self._services._prepare_sensitive_quanta(
                turn,
                command.sensitive_text,
            )
            if not quanta:
                _settle(command.future, None)
                return False
            self._continuations.append(
                _QuantumBundle(
                    turn=turn,
                    quanta=deque(quanta),
                    completion=command.future,
                )
            )
            return False
        raise ValueError("invalid_announcement_command")

    def _validate_turn(self, turn: VoiceTurnRecord) -> None:
        if not isinstance(turn, VoiceTurnRecord):
            raise TypeError("turn must be VoiceTurnRecord")
        if (
            turn.session_id != self.session_id
            or turn.session_generation != self.generation
        ):
            raise VoiceBootstrapError("voice_announcement_session_mismatch")

    def _ensure_turn(self, turn: VoiceTurnRecord) -> None:
        existing_user = self._turn_users.get(turn.turn_id)
        if existing_user is not None and existing_user != turn.user_id:
            raise VoiceBootstrapError("voice_announcement_owner_mismatch")
        self._turn_users[turn.turn_id] = turn.user_id
        if self._scheduler.has_turn(turn.turn_id):
            return
        active_turn_ids = tuple(
            turn_id
            for turn_id in self._turn_users
            if turn_id != turn.turn_id and self._scheduler.has_turn(turn_id)
        )
        if active_turn_ids:
            # The scheduler permits at most two active turns. Labels describe
            # request order, not completion order, and remain bound even if
            # the earlier terminal bundle drains before the later one.
            self._turn_attribution[active_turn_ids[-1]] = "earlier"
            self._turn_attribution[turn.turn_id] = "latest"
        self._scheduler.add_turn(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            generation=turn.session_generation,
            media_grant_revision=turn.media_grant_revision,
            announcement_sequence=turn.announcement_sequence,
            last_phrase_key=turn.last_phrase_key,
            next_due_at=turn.next_announcement_due_at,
        )
        if self._muted:
            self._scheduler.set_muted(turn.turn_id, True)

    def _set_muted(self, muted: bool) -> bool:
        """Apply one session-wide speech fence without queuing catch-up audio."""

        if self._muted == muted:
            return False
        self._muted = muted
        preempted = False
        for turn_id in tuple(self._turn_users):
            if self._scheduler.has_turn(turn_id):
                preempted = self._scheduler.set_muted(turn_id, muted) or preempted
        if muted:
            for turn_id in tuple(self._start_waiters):
                self._settle_waiters(self._start_waiters, turn_id)
            for turn_id in tuple(self._waiting_waiters):
                self._settle_waiters(self._waiting_waiters, turn_id)
            self._drop_muted_bundles()
        return (muted and self._speaking) or preempted

    def _mark_intentional_stop(self) -> bool:
        """Fence active and queued output for this exact session generation."""

        bundle = self._active_bundle
        if bundle is not None:
            bundle.intentionally_suppressed = True
            if bundle.quanta:
                # Remaining quanta are deliberately discarded and therefore
                # cannot truthfully be described as source-finished.
                bundle.quanta.clear()
                bundle.speech_outcome = "suppressed"
        preempted = self._one_shot_scheduler_fence(self._active_turn_id)

        queued = tuple(self._continuations)
        self._continuations.clear()
        completed_ids: set[int] = set()
        for queued_bundle in queued:
            queued_bundle.intentionally_suppressed = True
            queued_bundle.quanta.clear()
            queued_bundle.speech_outcome = "suppressed"
            preempted = (
                self._one_shot_scheduler_fence(queued_bundle.turn.turn_id)
                or preempted
            )
            if self._terminal.get(queued_bundle.turn.turn_id) is queued_bundle:
                self._complete_terminal_bundle(queued_bundle)
            else:
                _settle(queued_bundle.completion, None)
            completed_ids.add(id(queued_bundle))

        # A terminal bundle whose first quantum has not started is owned by the
        # scheduler rather than the continuation deque. Suppress and settle it
        # in the same stop command so an idle-boundary race cannot restart it.
        for queued_bundle in tuple(self._terminal.values()):
            if queued_bundle is bundle or id(queued_bundle) in completed_ids:
                continue
            queued_bundle.intentionally_suppressed = True
            queued_bundle.quanta.clear()
            queued_bundle.speech_outcome = "suppressed"
            preempted = (
                self._one_shot_scheduler_fence(queued_bundle.turn.turn_id)
                or preempted
            )
            self._complete_terminal_bundle(queued_bundle)
        return preempted

    def _one_shot_scheduler_fence(self, turn_id: str | None) -> bool:
        """Cancel one active/offered quantum without persisting user mute."""

        if turn_id is None or not self._scheduler.has_turn(turn_id):
            return False
        snapshot = self._scheduler.snapshot(turn_id)
        if snapshot.muted:
            return False
        preempted = self._scheduler.set_muted(turn_id, True)
        if not self._muted:
            # Explicit stop/background interruption is a one-shot fence. The
            # durable user mute state remains authoritative for future speech.
            self._scheduler.set_muted(turn_id, False)
        return preempted

    def _drop_muted_bundles(self) -> None:
        """Discard queued result speech while preserving durable text outcomes."""

        active = self._active_bundle
        if active is not None:
            # Mute/background is accepted while the source may already be
            # racing to its normal terminal event.  Fence the remainder now so
            # a late ``speech_finished`` cannot requeue it after the user has
            # asked for silence.  The active bundle is settled only after its
            # exact source waiter terminates.
            active.intentionally_suppressed = True
            active.quanta.clear()
            active.speech_outcome = "suppressed"
        queued = tuple(self._continuations)
        self._continuations.clear()
        for bundle in queued:
            bundle.quanta.clear()
            if bundle.speech_outcome == "source_finished":
                bundle.speech_outcome = "suppressed"
            if self._terminal.get(bundle.turn.turn_id) is bundle:
                self._complete_terminal_bundle(bundle)
            else:
                _settle(bundle.completion, None)
        for bundle in tuple(self._terminal.values()):
            if bundle is self._active_bundle:
                continue
            bundle.quanta.clear()
            if bundle.speech_outcome == "source_finished":
                bundle.speech_outcome = "suppressed"
            self._complete_terminal_bundle(bundle)

    def _abandon_turn(self, turn: VoiceTurnRecord) -> bool:
        """Cancel only voice output for an unavailable origin chat."""

        turn_id = turn.turn_id
        preempted = False
        if self._scheduler.has_turn(turn_id):
            preempted = self._scheduler.abandon_turn(turn_id)
        self._settle_waiters(self._start_waiters, turn_id)
        self._settle_waiters(self._waiting_waiters, turn_id)
        terminal = self._terminal.pop(turn_id, None)
        if terminal is not None:
            terminal.quanta.clear()
            _settle(terminal.completion, None)
        retained: deque[_QuantumBundle] = deque()
        for bundle in self._continuations:
            if bundle.turn.turn_id == turn_id:
                bundle.quanta.clear()
                _settle(bundle.completion, None)
            else:
                retained.append(bundle)
        self._continuations = retained
        if self._active_bundle is not None:
            preempted = (
                self._active_bundle.turn.turn_id == turn_id
                or preempted
            )
        self._turn_users.pop(turn_id, None)
        self._turn_attribution.pop(turn_id, None)
        return preempted

    async def _execute_decision(self, decision: CadenceDecision) -> None:
        terminal_bundle = self._terminal.get(decision.turn_id)
        if decision.terminal:
            if terminal_bundle is None or not terminal_bundle.quanta:
                await self._finish_silent_terminal(decision, terminal_bundle)
                return
            quantum = terminal_bundle.quanta.popleft()
        else:
            refreshed = await asyncio.to_thread(
                self._services.repository.get_turn,
                user_id=self._turn_user_id(decision.turn_id),
                turn_id=decision.turn_id,
            )
            mutation = await self._services._reserve_announcement(
                refreshed,
                decision.kind,
                expected_phrase_key=decision.phrase_key,
            )
            phrase_key = mutation.claim.phrase_key
            if phrase_key is None:
                raise VoiceBootstrapError("lifecycle_phrase_unavailable")
            quantum = _PreparedQuantum(
                turn=refreshed,
                mutation=mutation,
                text=APPROVED_PHRASE_TEXT[phrase_key],
            )
        self._validate_decision_claim(decision, quantum.mutation)
        self._scheduler.start(decision)
        await self._services.observe_cadence_start(
            quantum.turn,
            decision,
            started_monotonic=self._clock.monotonic(),
            started_at=self._clock.utcnow(),
        )
        self._active_bundle = terminal_bundle
        self._active_turn_id = decision.turn_id
        try:
            status, preempted = await self._play_quantum(quantum)
        finally:
            self._active_bundle = None
            self._active_turn_id = None
        if not preempted:
            self._scheduler.finish(decision, self._completion(decision))
        if decision.kind == "acknowledgement":
            self._settle_waiters(self._start_waiters, decision.turn_id)
        elif decision.kind == "waiting":
            self._settle_waiters(self._waiting_waiters, decision.turn_id)
        if decision.terminal:
            assert terminal_bundle is not None
            if self._terminal.get(decision.turn_id) is not terminal_bundle:
                return
            if status != "speech_finished":
                terminal_bundle.speech_outcome = self._terminal_speech_outcome(
                    terminal_bundle,
                    status=status,
                )
                terminal_bundle.quanta.clear()
            if terminal_bundle.quanta:
                self._continuations.append(terminal_bundle)
            else:
                self._complete_terminal_bundle(terminal_bundle)

    async def _finish_silent_terminal(
        self,
        decision: CadenceDecision,
        bundle: _QuantumBundle | None,
    ) -> None:
        self._scheduler.start(decision)
        self._scheduler.finish(decision, self._completion(decision))
        if bundle is not None:
            self._complete_terminal_bundle(bundle)

    async def _execute_continuation(self, bundle: _QuantumBundle) -> None:
        quantum = bundle.quanta.popleft()
        self._active_bundle = bundle
        self._active_turn_id = bundle.turn.turn_id
        try:
            status, preempted = await self._play_quantum(quantum)
        finally:
            self._active_bundle = None
            self._active_turn_id = None
        if preempted and bundle.turn.turn_id not in self._turn_users:
            bundle.quanta.clear()
            _settle(bundle.completion, None)
            return
        if status != "speech_finished":
            bundle.speech_outcome = self._terminal_speech_outcome(
                bundle,
                status=status,
            )
            bundle.quanta.clear()
        if bundle.quanta:
            self._continuations.append(bundle)
        elif bundle.turn.turn_id in self._terminal:
            self._complete_terminal_bundle(bundle)
        else:
            _settle(bundle.completion, None)

    async def _play_quantum(
        self,
        quantum: _PreparedQuantum,
    ) -> tuple[str, bool]:
        claim = quantum.mutation.claim
        sent = False
        try:
            await self._services.media.speak_turn(
                quantum.turn,
                claim,
                text=quantum.text,
                sensitive_authorized=quantum.sensitive_authorized,
            )
            sent = True
            self._speaking = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_announcement_send_failed reason=%s",
                _safe_failure_reason(exc),
            )
        finally:
            if not quantum.claim_completed:
                await self._services._complete_announcement(
                    quantum.turn,
                    claim.claim_id,
                )
        if not sent:
            await self._services._record_turn_event(
                quantum.turn,
                "tts",
                "failed",
                reason="speech_unavailable",
            )
            return "speech_failed", False

        terminal_task = asyncio.create_task(
            self._services.media.await_speech_terminal(
                claim.announcement_id,
                timeout_seconds=self._SOURCE_TERMINAL_SECONDS,
            )
        )
        preempted = False
        try:
            while not terminal_task.done():
                if self._commands:
                    preempted = await self._drain_commands() or preempted
                    continue
                self._wake.clear()
                if self._commands:
                    self._wake.set()
                    continue
                wake_task = asyncio.create_task(self._wake.wait())
                done, _pending = await asyncio.wait(
                    {terminal_task, wake_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wake_task in done:
                    preempted = await self._drain_commands() or preempted
                else:
                    wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
                    if wake_task.done() and not wake_task.cancelled():
                        preempted = await self._drain_commands() or preempted
            try:
                return str(terminal_task.result()), preempted
            except Exception as exc:
                logger.warning(
                    "voice_announcement_terminal_failed reason=%s",
                    _safe_failure_reason(exc),
                )
                await self._services._record_turn_event(
                    quantum.turn,
                    "tts",
                    "failed",
                    reason="speech_unavailable",
                )
                return "speech_failed", preempted
        finally:
            if not terminal_task.done():
                terminal_task.cancel()
                await asyncio.gather(terminal_task, return_exceptions=True)
            self._speaking = False

    async def _stop_current_speech(self, *, barge_in: bool = False) -> None:
        session = await self._services.media.current_session(
            self.session_id,
            self.generation,
        )
        if session is None:
            return
        try:
            if barge_in:
                await self._services.media.barge_in(session)
            else:
                await self._services.media.stop_speech(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_announcement_stop_failed reason=%s",
                _safe_failure_reason(exc),
            )

    def _next_continuation(self) -> _QuantumBundle | None:
        if not self._continuations:
            return None
        # Handoff is a maximum switch-latency allowance, not an inter-quantum
        # pause. Attempt eligible continuation media immediately; the cadence
        # reservation below still protects an equally due peer's hard bound.
        bundle = self._continuations[0]
        claim = bundle.quanta[0].mutation.claim
        duration = claim.max_duration_samples / 24_000
        deadline = self._scheduler.next_hard_deadline_delay()
        if deadline is not None and deadline + 1e-9 < duration + HANDOFF_BUDGET_SECONDS:
            return None
        return self._continuations.popleft()

    async def _wait_for_work(self) -> None:
        delay = self._scheduler.next_wake_delay()
        if self._continuations:
            deadline = self._scheduler.next_hard_deadline_delay()
            if deadline is None or deadline >= (
                self._continuations[0].quanta[0].mutation.claim.max_duration_samples
                / 24_000
                + HANDOFF_BUDGET_SECONDS
            ):
                delay = 0.0
        timeout = (
            self._IDLE_WAIT_SECONDS
            if delay is None
            else min(
                self._IDLE_WAIT_SECONDS,
                max(0.001, delay),
            )
        )
        self._wake.clear()
        if self._commands:
            self._wake.set()
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except TimeoutError:
            return

    def _completion(self, decision: CadenceDecision) -> PlayoutCompletion:
        completed_at = self._clock.utcnow()
        return PlayoutCompletion(
            announcement_id=decision.announcement_id,
            turn_id=decision.turn_id,
            source_finished_at=completed_at,
            client_finished_at=completed_at,
            completed_at=completed_at,
            completed_monotonic=self._clock.monotonic(),
        )

    def _validate_decision_claim(
        self,
        decision: CadenceDecision,
        mutation: AnnouncementMutation,
    ) -> None:
        claim = mutation.claim
        if (
            claim.announcement_id != decision.announcement_id
            or claim.sequence != decision.sequence
            or claim.kind != decision.kind
            or claim.quantum_role != decision.quantum_role
            or claim.quantum_index != decision.quantum_index
            or claim.max_duration_samples != decision.max_duration_samples
            or claim.phrase_key != decision.phrase_key
        ):
            raise VoiceBootstrapError("voice_announcement_claim_mismatch")

    def _turn_user_id(self, turn_id: str) -> str:
        user_id = self._turn_users.get(turn_id)
        if user_id is None:
            raise VoiceBootstrapError("voice_announcement_owner_unavailable")
        return user_id

    def _complete_terminal_bundle(self, bundle: _QuantumBundle) -> None:
        self._terminal.pop(bundle.turn.turn_id, None)
        if self._scheduler.has_turn(bundle.turn.turn_id):
            self._scheduler.remove_turn(bundle.turn.turn_id)
        self._turn_users.pop(bundle.turn.turn_id, None)
        self._turn_attribution.pop(bundle.turn.turn_id, None)
        _settle(
            bundle.completion,
            VoiceTerminalAnnouncementResult(
                turn=bundle.turn,
                speech_outcome=bundle.speech_outcome,
            ),
        )

    def _terminal_speech_outcome(
        self,
        bundle: _QuantumBundle,
        *,
        status: str,
    ) -> str:
        """Classify intentional lifecycle fences separately from speech failure."""

        # ``speech_interrupted`` is an authenticated, exact-announcement worker
        # terminal.  Its protocol reasons are intentional fences (barge-in,
        # user stop, mute, takeover, end, terminal supersession, or staleness),
        # whereas synthesis/publication failures use ``speech_failed``.
        if status == "speech_interrupted":
            return "suppressed"
        key = (bundle.turn.session_id, bundle.turn.session_generation)
        if (
            bundle.intentionally_suppressed
            or self._muted
            or key in self._services.announcement_closed_sessions
            or bundle.turn.turn_id in self._services.announcement_abandoned_turns
        ):
            return "suppressed"
        return "failed"

    @staticmethod
    def _settle_waiters(
        waiters: dict[str, list[asyncio.Future[Any]]],
        turn_id: str,
    ) -> None:
        for future in waiters.pop(turn_id, []):
            _settle(future, None)

    def _fail_pending(self, exc: Exception) -> None:
        for command in self._commands:
            if not command.future.done():
                command.future.set_exception(exc)
        self._commands.clear()
        for waiters in (*self._start_waiters.values(), *self._waiting_waiters.values()):
            for future in waiters:
                if not future.done():
                    future.set_exception(exc)
        self._start_waiters.clear()
        self._waiting_waiters.clear()
        for future in self._mute_waiters:
            if not future.done():
                future.set_exception(exc)
        self._mute_waiters.clear()
        for future in self._stop_waiters:
            if not future.done():
                future.set_exception(exc)
        self._stop_waiters.clear()
        bundles = [*self._terminal.values(), *self._continuations]
        self._terminal.clear()
        self._continuations.clear()
        self._turn_users.clear()
        for bundle in bundles:
            if not bundle.completion.done():
                bundle.completion.set_exception(exc)


@dataclass(slots=True)
class VoiceServices:
    livekit: LiveKitService
    worker_pool: WorkerPool
    repository: VoiceSessionRepository
    coordinator: VoiceCoordinator
    capability: VoiceCapabilityService
    media: DirectRtcVoiceMedia
    runtime: VoiceSessionRuntime
    worker_control_settings: WorkerControlSettings = field(repr=False)
    observability: RuntimeObservability | None = field(default=None, repr=False)
    worker_endpoint: WorkerControlEndpoint | None = field(
        default=None,
        init=False,
        repr=False,
    )
    announcement_clock_factory: Callable[[], CoordinatorClock] = field(
        default=CoordinatorClock,
        repr=False,
    )
    announcement_runners: dict[tuple[str, int], _SessionAnnouncementRunner] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    announcement_runner_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    announcement_muted_sessions: set[tuple[str, int]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    announcement_suspended_sessions: set[tuple[str, int]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    announcement_closed_sessions: dict[tuple[str, int], None] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    announcement_abandoned_turns: dict[str, None] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    preacceptance_guided_turns: dict[str, None] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    preacceptance_guidance_tasks: set[asyncio.Task[None]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    maintenance_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    terminal_turn_notifier: (
        Callable[[VoiceTurnRecord], Awaitable[None]] | None
    ) = field(
        default=None,
        init=False,
        repr=False,
    )
    listening_sessions: set[tuple[str, int]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    reconnecting_sessions: set[tuple[str, int]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    sensitive_recaps: SensitiveRecapRegistry = field(
        default_factory=SensitiveRecapRegistry,
        repr=False,
    )

    def bind_terminal_turn_notifier(
        self,
        notifier: Callable[[VoiceTurnRecord], Awaitable[None]],
    ) -> None:
        """Bind one content-free repaired-turn delivery seam."""

        if not callable(notifier):
            raise TypeError("terminal turn notifier must be callable")
        if self.terminal_turn_notifier is not None:
            raise RuntimeError("terminal_turn_notifier_already_bound")
        self.terminal_turn_notifier = notifier

    def install_worker_control(
        self,
        app: Any,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> WorkerControlEndpoint:
        """Mount the authenticated pool socket exactly once on the root app."""

        del environ
        endpoint = install_router(
            app,
            self.worker_pool,
            settings=self.worker_control_settings,
            disconnect_hook=self.handle_worker_disconnect,
            frame_hook=self.handle_worker_frame,
        )
        self.worker_endpoint = endpoint
        return endpoint

    async def handle_worker_disconnect(
        self,
        receipt: WorkerRegistrationReceipt,
        released_session_ids: tuple[str, ...],
    ) -> None:
        """Reconcile only assignments still absent after one exact disconnect.

        WorkerPool already makes a stale replaced connection's unregister a
        no-op. This second check closes the narrow scheduling window between
        unregister and durable cleanup: if any session has since acquired a
        current assignment, the delayed callback cannot end it.
        """

        try:
            await self.runtime.reconcile_worker_disconnect(
                worker_identity=receipt.worker_identity,
                released_session_ids=tuple(sorted(set(released_session_ids))),
                released_assignment_ids=receipt.fenced_assignments,
                assignment_is_current=self._worker_assignment_is_current,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The pool fence has already failed media closed. Durable lease
            # expiry and room reconciliation remain bounded backstops.
            logger.warning(
                "voice_worker_disconnect_reconcile_unavailable "
                "reason=runtime_cleanup_failed"
            )

    def _worker_assignment_is_current(self, session_id: str) -> bool:
        """Deny stale cleanup whenever the pool has a current assignment."""

        try:
            self.worker_pool.assignment_snapshot(session_id)
        except StaleFence:
            return False
        return True

    async def handle_worker_frame(
        self,
        receipt: WorkerRegistrationReceipt,
        frame: Mapping[str, Any],
    ) -> None:
        """Apply only post-authentication worker effects to durable state."""

        frame_type = frame.get("type")
        terminal_state = self._terminal_worker_state(frame)
        try:
            if frame_type == "recognition_started":
                await self.coordinator.bind_recognition_started(frame)
                await self._apply_worker_idle_state(frame, listening=False)
                await self._record_frame_event(frame, "turn", "recognizing")
            elif frame_type == "recognition_failed":
                self_speech = frame.get("reason") == "self_speech"
                if self_speech:
                    logger.info("voice_self_speech_suppressed reason=self_speech")
                    await self.coordinator.suppress_self_speech(frame)
                else:
                    logger.warning(
                        "voice_recognition_failed reason=%s",
                        frame.get("reason", "invalid_asr_result"),
                    )
                    rejection = await self.coordinator.reject_recognition_failed(frame)
                    self.schedule_preacceptance_rejection(
                        rejection.turn,
                        reason="malformed_final",
                    )
                await self._apply_worker_idle_state(frame, listening=False)
                await self._record_frame_event(
                    frame,
                    "turn",
                    "rejected",
                    reason=(
                        "self_speech_suppressed"
                        if self_speech
                        else "speech_unavailable"
                    ),
                )
            elif frame_type in {
                "media_state",
                "speech_started",
                "speech_finished",
                "speech_interrupted",
                "speech_failed",
            }:
                await self.media.handle_worker_frame(frame)
                listening = (
                    frame.get("state") == "listening"
                    if frame_type == "media_state"
                    else False
                )
                await self._apply_worker_idle_state(frame, listening=listening)
                await self._record_media_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            if terminal_state is None:
                raise
            # Terminal assignment repair is the authoritative fail-closed
            # effect. A local media/metrics failure must not strand its slot or
            # escalate one session failure to every peer on the worker socket.
            logger.warning(
                "voice_terminal_worker_effect_unavailable "
                "reason=terminal_effect_failed"
            )
        if terminal_state is not None:
            await self._release_terminal_worker_assignment(
                receipt,
                frame,
                terminal_state=terminal_state,
            )

    @staticmethod
    def _terminal_worker_state(frame: Mapping[str, Any]) -> str | None:
        """Project only authenticated worker states that end one media lease."""

        frame_type = frame.get("type")
        if frame_type == "media_state":
            state = frame.get("state")
        elif frame_type == "heartbeat":
            state = frame.get("media_state")
        elif frame_type == "worker_ready" and frame.get("profile_ready") is False:
            state = "failed"
        else:
            return None
        return state if state in {"failed", "ended"} else None

    async def _release_terminal_worker_assignment(
        self,
        receipt: WorkerRegistrationReceipt,
        frame: Mapping[str, Any],
        *,
        terminal_state: str,
    ) -> None:
        """Release and reconcile one exact terminal assignment, leaving peers live."""

        session_id = frame.get("session_id")
        generation = frame.get("generation")
        if (
            not isinstance(session_id, str)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
        ):
            return
        release = await self.worker_pool.release_terminal_assignment(
            connection_id=receipt.connection_id,
            session_id=session_id,
            generation=generation,
            terminal_state=terminal_state,
        )
        if release is None:
            return
        await self.handle_worker_disconnect(
            WorkerRegistrationReceipt(
                connection_id=release.connection_id,
                worker_identity=release.worker_identity,
                accepted_max_sessions=release.accepted_max_sessions,
                fenced_assignments=(release.assignment_id,),
            ),
            (release.session_id,),
        )

    async def _apply_worker_idle_state(
        self,
        frame: Mapping[str, Any],
        *,
        listening: bool,
    ) -> None:
        """Translate an authenticated worker state into true-idle state."""

        session_id = frame.get("session_id")
        generation = frame.get("generation")
        if not isinstance(session_id, str) or not isinstance(generation, int):
            return
        session = await self.media.current_session(session_id, generation)
        if session is None:
            return
        eligible = bool(
            listening and session.foreground_active and session.microphone_enabled
        )
        key = (session_id, generation)
        if eligible:
            self.listening_sessions.add(key)
        else:
            self.listening_sessions.discard(key)
        try:
            await asyncio.to_thread(
                self.repository.set_true_idle,
                user_id=session.user_id,
                session_id=session_id,
                expected_generation=generation,
                listening=eligible,
                user_input_gate=False,
                now=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Voice idle-state update unavailable", exc_info=True)

    async def admit_transcript(
        self,
        request: TranscriptSubmission,
        *,
        now: datetime,
    ) -> TranscriptAdmission:
        """Verify a final with the same memory-only secret as its worker."""

        try:
            admission = await asyncio.to_thread(
                self.repository.admit_transcript,
                request,
                worker_control_secret=self.worker_control_settings.secret,
                now=now,
            )
        except TranscriptSubmissionRejected:
            await self._record_submission_event(
                request,
                "turn",
                "rejected",
                reason="protocol_violation",
            )
            raise
        if admission.replayed:
            await self._record_turn_event(
                admission.turn,
                "deduplication",
                "replayed",
                reason="transcript_replay",
            )
        else:
            await self._record_turn_event(
                admission.turn,
                "turn",
                "submitted",
            )
        return admission

    async def _record_submission_event(
        self,
        request: TranscriptSubmission,
        event: str,
        outcome: str,
        *,
        reason: str,
    ) -> None:
        session = await self.media.current_session(
            request.session_id,
            request.generation,
        )
        if session is not None:
            self._record_session_event(
                session,
                event,
                outcome,
                reason=reason,
            )

    async def _record_frame_event(
        self,
        frame: Mapping[str, Any],
        event: str,
        outcome: str,
        *,
        reason: str = "none",
    ) -> None:
        session_id = frame.get("session_id")
        generation = frame.get("generation")
        if not isinstance(session_id, str) or not isinstance(generation, int):
            return
        session = await self.media.current_session(session_id, generation)
        if session is not None:
            self._record_session_event(
                session,
                event,
                outcome,
                reason=reason,
            )

    async def _record_media_frame(self, frame: Mapping[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type == "media_state":
            state = frame.get("state")
            mapped = {
                "connecting": ("starting", "none"),
                "listening": ("listening", "none"),
                "speech_detected": ("recognizing", "none"),
                "transcribing": ("recognizing", "none"),
                "reconnecting": ("suspended", "reconnecting"),
                "failed": ("degraded", "internal_error"),
                "ended": ("ended", "none"),
            }.get(state)
            if mapped is None:
                return
            session = await self._session_from_frame(frame)
            if session is None:
                return
            self._record_session_state(session, *mapped)
            key = (session.session_id, session.generation)
            if state == "reconnecting":
                self.reconnecting_sessions.add(key)
                self._record_session_event(
                    session,
                    "reconnect",
                    "started",
                    reason="reconnecting",
                )
            elif state == "listening" and key in self.reconnecting_sessions:
                self.reconnecting_sessions.discard(key)
                self._record_session_event(
                    session,
                    "reconnect",
                    "recovered",
                    reason="reconnecting",
                )
            return
        tts_outcome = {
            "speech_started": "started",
            "speech_finished": "succeeded",
            "speech_interrupted": "interrupted",
            "speech_failed": "failed",
        }.get(frame_type)
        if tts_outcome is not None:
            await self._record_frame_event(
                frame,
                "tts",
                tts_outcome,
                reason=("speech_unavailable" if tts_outcome == "failed" else "none"),
            )

    async def _session_from_frame(
        self,
        frame: Mapping[str, Any],
    ) -> VoiceSessionRecord | None:
        session_id = frame.get("session_id")
        generation = frame.get("generation")
        if not isinstance(session_id, str) or not isinstance(generation, int):
            return None
        return await self.media.current_session(session_id, generation)

    async def _record_turn_event(
        self,
        turn: VoiceTurnRecord,
        event: str,
        outcome: str,
        *,
        reason: str = "none",
    ) -> None:
        session = await self.media.current_session(
            turn.session_id,
            turn.session_generation,
        )
        if session is not None:
            self._record_session_event(
                session,
                event,
                outcome,
                reason=reason,
            )

    async def observe_cadence_start(
        self,
        turn: VoiceTurnRecord,
        decision: CadenceDecision,
        *,
        started_monotonic: float,
        started_at: datetime,
    ) -> None:
        """Record content-free server timings at the serialized stream start."""

        if self.observability is None:
            return
        session = await self.media.current_session(
            turn.session_id,
            turn.session_generation,
        )
        if session is None:
            return
        client_kind, transport = _metric_dimensions(session)
        if decision.kind == "acknowledgement" and turn.accepted_at is not None:
            timing = "acknowledgement"
            duration = max(0.0, (started_at - turn.accepted_at).total_seconds())
        elif decision.kind == "progress":
            timing = "cadence_gap"
            previous_finish = decision.latest_start_monotonic - CADENCE_HARD_GAP_SECONDS
            duration = max(0.0, started_monotonic - previous_finish)
        elif decision.terminal and turn.terminal_at is not None:
            timing = "speech_start"
            duration = max(0.0, (started_at - turn.terminal_at).total_seconds())
        else:
            return
        self.observability.observe_voice_timing(
            timing,
            duration,
            client_kind=client_kind,
            transport=transport,
        )

    def _record_session_event(
        self,
        session: VoiceSessionRecord,
        event: str,
        outcome: str,
        *,
        reason: str,
    ) -> None:
        if self.observability is None:
            return
        client_kind, transport = _metric_dimensions(session)
        self.observability.record_voice_event(
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
        if self.observability is None:
            return
        client_kind, transport = _metric_dimensions(session)
        self.observability.record_voice_state(
            state=state,
            reason=reason,
            client_kind=client_kind,
            transport=transport,
        )

    def start(self, *, maintenance_interval_seconds: float = 5.0) -> None:
        """Start one bounded server-owned lease/idle cleanup loop."""

        if not 0.5 <= maintenance_interval_seconds <= 60:
            raise ValueError("invalid_voice_maintenance_interval")
        if self.maintenance_task is not None and not self.maintenance_task.done():
            return
        self.maintenance_task = asyncio.create_task(
            self._maintenance_loop(float(maintenance_interval_seconds)),
            name="voice-session-maintenance",
        )

    async def _maintenance_loop(self, interval_seconds: float) -> None:
        while True:
            try:
                await self._sweep_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Voice session maintenance sweep failed", exc_info=True)
            await asyncio.sleep(interval_seconds)

    async def _sweep_sessions(self) -> None:
        """End expired media and reconcile only durably terminal accepted work."""

        now = datetime.now(UTC)
        await asyncio.to_thread(
            self.repository.renew_owned_control_leases,
            owner_id=self.coordinator.replica_id,
            now=now,
        )
        lease_expired, idle_expired = await asyncio.gather(
            asyncio.to_thread(self.repository.expire_session_leases, now=now),
            asyncio.to_thread(self.repository.expire_true_idle, now=now),
        )
        await asyncio.to_thread(
            self.repository.reconcile_ended_unaccepted_turns,
            now=now,
        )
        repaired_terminals = await asyncio.to_thread(
            self.repository.reconcile_ended_terminal_operation_turns,
            now=now,
        )
        if repaired_terminals:
            logger.info(
                "voice_ended_terminal_turns_reconciled count=%s",
                len(repaired_terminals),
            )
        for turn in repaired_terminals:
            notifier = self.terminal_turn_notifier
            if notifier is None:
                break
            try:
                await notifier(turn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "voice_terminal_turn_notification_unavailable reason=%s",
                    _safe_failure_reason(exc),
                )
        ended: dict[tuple[str, int], Any] = {}
        for session in (*lease_expired, *idle_expired):
            ended[(session.session_id, session.generation)] = session
        for session in ended.values():
            await self.handle_runtime_session_end(
                session,
                session.end_reason or "lease_expired",
            )
            # A reaper end is otherwise invisible to the owner device: without
            # this push a client that silently stopped renewing keeps showing
            # a live session it no longer has (and its later DELETE conflicts).
            publish_state = getattr(self.runtime, "publish_session_state", None)
            if callable(publish_state):
                await publish_state(session)
            metric_reason = (
                "idle_expired" if session.end_reason == "idle" else "lease_expired"
            )
            try:
                await self.media.end(
                    session,
                    "idle" if session.end_reason == "idle" else "lease_expired",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Durable end fences the generation. Short-lived grants and
                # later reconciliation remain the cleanup backstop.
                logger.warning(
                    "voice_media_cleanup_unavailable reason=media_end_failed"
                )
            finally:
                self._record_session_event(
                    session,
                    "session",
                    "expired",
                    reason=metric_reason,
                )
                self._record_session_state(
                    session,
                    "ended",
                    metric_reason,
                )

    async def _close_announcement_runner(
        self,
        session_id: str,
        generation: int,
    ) -> None:
        """Release one exact session stream without touching accepted work."""

        key = (session_id, generation)
        async with self.announcement_runner_lock:
            runner = self.announcement_runners.pop(key, None)
            self.announcement_muted_sessions.discard(key)
            self.announcement_suspended_sessions.discard(key)
        if runner is not None:
            await runner.close()

    async def handle_runtime_session_end(
        self,
        session: VoiceSessionRecord,
        reason: str,
    ) -> None:
        """Fence server-owned queues after runtime media teardown."""

        del reason
        release_fence = getattr(
            self.runtime,
            "release_worker_assignment_fence",
            None,
        )
        if callable(release_fence):
            release_fence(session)
        key = (session.session_id, session.generation)
        self.listening_sessions.discard(key)
        self.reconnecting_sessions.discard(key)
        async with self.announcement_runner_lock:
            self._remember_announcement_fence(
                self.announcement_closed_sessions,
                key,
            )
            runner = self.announcement_runners.pop(key, None)
            self.announcement_muted_sessions.discard(key)
            self.announcement_suspended_sessions.discard(key)
        if runner is not None:
            await runner.close()

    async def end_user_voice_session(
        self,
        *,
        user_id: str,
        reason: str,
    ) -> VoiceSessionRecord | None:
        """Apply logout/auth-expiry teardown without cancelling accepted work."""

        ended = await asyncio.to_thread(
            self.repository.end_live_user_session,
            user_id=user_id,
            reason=reason,
            now=datetime.now(UTC),
        )
        if ended is not None:
            await self._cleanup_ended_session(ended, reason)
        return ended

    async def handle_chat_unavailable(
        self,
        mutation: ChatUnavailableMutation,
    ) -> None:
        """Consume one deletion/revocation receipt at the live media edge."""

        if not isinstance(mutation, ChatUnavailableMutation):
            raise TypeError("mutation must be ChatUnavailableMutation")
        async with self.announcement_runner_lock:
            for turn_id in mutation.accepted_turn_ids:
                self._remember_announcement_fence(
                    self.announcement_abandoned_turns,
                    turn_id,
                )
        ended_keys = {
            (session.session_id, session.generation)
            for session in mutation.ended_sessions
        }
        for turn_id in mutation.unaccepted_turn_ids:
            try:
                turn = await asyncio.to_thread(
                    self.repository.get_turn,
                    user_id=mutation.user_id,
                    turn_id=turn_id,
                )
                await self.coordinator.emit_transcript_rejected(
                    turn,
                    reason="chat_unavailable",
                    retry_policy="explicit_user_retry",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "voice_chat_rejection_delivery_unavailable",
                    exc_info=True,
                )
        for turn_id in mutation.accepted_turn_ids:
            try:
                turn = await asyncio.to_thread(
                    self.repository.get_turn,
                    user_id=mutation.user_id,
                    turn_id=turn_id,
                )
                if (
                    turn.session_id,
                    turn.session_generation,
                ) not in ended_keys:
                    await self._fence_turn_announcements(turn)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "voice_chat_announcement_fence_unavailable",
                    exc_info=True,
                )
        for session in mutation.ended_sessions:
            await self._cleanup_ended_session(session, session.end_reason or "chat_deleted")

    async def _fence_turn_announcements(self, turn: VoiceTurnRecord) -> None:
        key = (turn.session_id, turn.session_generation)
        async with self.announcement_runner_lock:
            self._remember_announcement_fence(
                self.announcement_abandoned_turns,
                turn.turn_id,
            )
            runner = self.announcement_runners.get(key)
        if runner is None or runner.task.done():
            return
        future = asyncio.get_running_loop().create_future()
        runner.submit(_AnnouncementCommand("abandon", turn, future))
        await future

    async def _cleanup_ended_session(
        self,
        session: VoiceSessionRecord,
        reason: str,
    ) -> None:
        """Idempotently close timers, worker media, participant, and room."""

        await self.handle_runtime_session_end(session, reason)
        try:
            await self.media.end(session, reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The durable end fence is authoritative. The bounded grant and
            # room reconcilers remain the cleanup backstop.
            logger.warning(
                "voice_media_cleanup_unavailable reason=media_end_failed"
            )

    async def set_session_speech_muted(
        self,
        session_id: str,
        generation: int,
        muted: bool,
    ) -> None:
        """Fence or freshly resume the exact session's serialized speech stream."""

        if not isinstance(session_id, str) or not session_id:
            raise ValueError("invalid_session_id")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("invalid_generation")
        if not isinstance(muted, bool):
            raise ValueError("invalid_speech_muted")
        key = (session_id, generation)
        async with self.announcement_runner_lock:
            if muted:
                self.announcement_muted_sessions.add(key)
            else:
                self.announcement_muted_sessions.discard(key)
            runner = self.announcement_runners.get(key)
            blocked = muted or key in self.announcement_suspended_sessions
        if runner is None or runner.task.done():
            return
        future = asyncio.get_running_loop().create_future()
        try:
            runner.submit(
                _AnnouncementCommand(
                    "mute",
                    None,
                    future,
                    muted=blocked,
                )
            )
            await future
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_speech_mute_fence_unavailable reason=%s",
                _safe_failure_reason(exc),
            )

    async def set_session_speech_suspended(
        self,
        session_id: str,
        generation: int,
        suspended: bool,
    ) -> None:
        """Persistently block unsolicited output for an exact background session."""

        if not isinstance(session_id, str) or not session_id:
            raise ValueError("invalid_session_id")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("invalid_generation")
        if not isinstance(suspended, bool):
            raise ValueError("invalid_speech_suspended")
        key = (session_id, generation)
        async with self.announcement_runner_lock:
            if suspended:
                self.announcement_suspended_sessions.add(key)
            else:
                self.announcement_suspended_sessions.discard(key)
            runner = self.announcement_runners.get(key)
            blocked = suspended or key in self.announcement_muted_sessions
        if runner is None or runner.task.done():
            return
        future = asyncio.get_running_loop().create_future()
        runner.submit(
            _AnnouncementCommand(
                "mute",
                None,
                future,
                muted=blocked,
            )
        )
        await future

    async def handle_client_playout(
        self,
        *,
        user_id: str,
        claims: VoiceControlClaims,
        event: VoicePlayoutEvent,
    ) -> None:
        """Accept one direct, content-free UI playout observation."""

        session = await asyncio.to_thread(
            self.repository.get_session,
            user_id=user_id,
            session_id=event.session_id,
        )
        observation = await self.media.accept_client_playout(
            user_id=user_id,
            claims=claims,
            event=event,
            session=session,
        )
        fence = observation.fence
        await asyncio.to_thread(
            self.repository.record_client_playout,
            user_id=user_id,
            device_id=claims.device_id,
            connection_generation=claims.connection_generation,
            session_id=fence.session_id,
            generation=fence.generation,
            media_grant_revision=fence.media_grant_revision,
            announcement_id=fence.announcement_id,
            announcement_sequence=(
                observation.turn_announcement_sequence
            ),
            turn_id=fence.turn_id,
            kind=fence.kind,
            quantum_role=fence.quantum_role,
            quantum_index=fence.quantum_index,
            result_reserved_samples_after=(fence.result_reserved_samples_after),
            phase=observation.phase,
            client_sequence=observation.client_sequence,
            received_at=observation.received_at,
        )
        if observation.phase in {"finished", "interrupted"}:
            await self.media.release_capture_after_playout(
                session,
                fence.announcement_id,
            )

    async def start_turn_announcements(self, turn: VoiceTurnRecord) -> None:
        """Queue one exactly-once acknowledgement on the session stream."""

        if not isinstance(turn, VoiceTurnRecord):
            raise TypeError("turn must be VoiceTurnRecord")
        await self._set_turn_idle(
            turn,
            listening=False,
            user_input_gate=False,
        )
        future = asyncio.get_running_loop().create_future()
        try:
            runner = await self._announcement_runner(turn)
            runner.submit(_AnnouncementCommand("start", turn, future))
            await future
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Speech degradation never rolls back durable chat acceptance.
            logger.warning(
                "voice_acknowledgement_unavailable reason=%s",
                _safe_failure_reason(exc),
            )

    async def speak_preacceptance_rejection(
        self,
        turn: VoiceTurnRecord,
        *,
        reason: str,
    ) -> None:
        """Speak one safe instruction after a worker cleared rejected text.

        This is deliberately not an accepted-turn lifecycle: it never adds the
        abandoned turn to the progress scheduler, and the repository permits
        only the exact first announcement for the persisted rejection reason.
        """

        if not isinstance(turn, VoiceTurnRecord):
            raise TypeError("turn must be VoiceTurnRecord")
        if reason not in PREACCEPTANCE_REJECTION_PHRASES:
            raise ValueError("invalid_preacceptance_rejection_reason")
        if (
            turn.state != "abandoned"
            or turn.rejection_reason != reason
            or turn.message_id is not None
            or turn.accepted_at is not None
            or turn.acceptance_commit_id is not None
            or turn.operation_id is not None
        ):
            return
        session = await self.media.current_session(
            turn.session_id,
            turn.session_generation,
        )
        if (
            session is None
            or session.generation != turn.session_generation
            or session.media_grant_revision != turn.media_grant_revision
            or session.state != "active"
            or session.speech_muted
        ):
            return
        key = (turn.session_id, turn.session_generation)
        async with self.announcement_runner_lock:
            if (
                key in self.announcement_closed_sessions
                or key in self.announcement_muted_sessions
                or turn.turn_id in self.preacceptance_guided_turns
            ):
                return
            self._remember_announcement_fence(
                self.preacceptance_guided_turns,
                turn.turn_id,
            )
        future = asyncio.get_running_loop().create_future()
        try:
            runner = await self._announcement_runner(turn)
            runner.submit(
                _AnnouncementCommand(
                    "preacceptance",
                    turn,
                    future,
                    rejection_reason=reason,
                )
            )
            await future
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_preacceptance_guidance_unavailable reason=%s",
                _safe_failure_reason(exc),
            )

    def schedule_preacceptance_rejection(
        self,
        turn: VoiceTurnRecord,
        *,
        reason: str,
    ) -> None:
        """Run worker-origin guidance off its control receive loop."""

        if (
            len(self.preacceptance_guidance_tasks)
            >= _MAX_PREACCEPTANCE_GUIDANCE_TASKS
        ):
            logger.warning(
                "voice_preacceptance_guidance_unavailable reason=task_limit"
            )
            return
        task = asyncio.create_task(
            self.speak_preacceptance_rejection(turn, reason=reason),
            name=f"voice-preacceptance-{turn.turn_id}",
        )
        self.preacceptance_guidance_tasks.add(task)
        task.add_done_callback(self._preacceptance_guidance_done)

    def _preacceptance_guidance_done(self, task: asyncio.Task[None]) -> None:
        self.preacceptance_guidance_tasks.discard(task)
        if task.cancelled():
            return
        try:
            failure = task.exception()
        except asyncio.CancelledError:
            return
        if failure is not None:
            logger.warning(
                "voice_preacceptance_guidance_task_failed reason=%s",
                _safe_failure_reason(failure),
            )

    async def wait_turn_announcements(
        self,
        turn: VoiceTurnRecord,
        *,
        waiting_reason: str,
    ) -> None:
        """Speak one allowlisted action request and suspend progress cadence."""

        future = asyncio.get_running_loop().create_future()
        runner = await self._announcement_runner(turn)
        runner.submit(
            _AnnouncementCommand(
                "waiting",
                turn,
                future,
                waiting_reason=waiting_reason,
            )
        )
        await future

    async def resume_turn_announcements(self, turn: VoiceTurnRecord) -> None:
        """Resume a waiting turn's cadence without replaying its wait prompt."""

        future = asyncio.get_running_loop().create_future()
        runner = await self._announcement_runner(turn)
        runner.submit(_AnnouncementCommand("resume", turn, future))
        await future

    async def finish_turn_announcements(
        self,
        turn: VoiceTurnRecord,
        *,
        terminal_kind: str,
        recap_text: str,
        recap_source: str,
        sensitivity: str,
        result_commit_id: str | None,
        with_delivery_status: bool = False,
    ) -> VoiceTurnRecord | VoiceTerminalAnnouncementResult:
        """Fence stale progress and serialize one honest terminal outcome."""

        if terminal_kind not in {"succeeded", "failed", "refused", "cancelled"}:
            raise ValueError("invalid_terminal_kind")
        future = asyncio.get_running_loop().create_future()
        try:
            runner = await self._announcement_runner(turn)
            runner.submit(
                _AnnouncementCommand(
                    "terminal",
                    turn,
                    future,
                    terminal_kind=terminal_kind,
                    recap_text=recap_text,
                    recap_source=recap_source,
                    sensitivity=sensitivity,
                    result_commit_id=result_commit_id,
                )
            )
            delivery = await future
            if not isinstance(delivery, VoiceTerminalAnnouncementResult):
                raise VoiceBootstrapError("terminal_speech_outcome_unavailable")
            terminal_turn = delivery.turn
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_terminal_announcement_unavailable reason=%s",
                _safe_failure_reason(exc),
            )
            refreshed = await asyncio.to_thread(
                self.repository.get_turn,
                user_id=turn.user_id,
                turn_id=turn.turn_id,
            )
            if refreshed.state in {"succeeded", "failed", "refused", "cancelled"}:
                await self._record_turn_event(
                    refreshed,
                    "turn",
                    refreshed.state,
                )
                delivery = VoiceTerminalAnnouncementResult(
                    turn=refreshed,
                    speech_outcome=self._fallback_terminal_speech_outcome(refreshed),
                )
                return delivery if with_delivery_status else refreshed
            if (
                refreshed.state == "abandoned"
                and refreshed.origin_chat_unavailable_at is not None
            ):
                return refreshed
            if refreshed.state not in {
                "accepted",
                "processing",
                "waiting_on_user",
            }:
                raise
            terminal = await asyncio.to_thread(
                self.repository.terminalize_turn,
                user_id=refreshed.user_id,
                turn_id=refreshed.turn_id,
                terminal_kind=terminal_kind,
                result_commit_id=result_commit_id,
                recap_source=recap_source,
                sensitivity=sensitivity,
                now=datetime.now(UTC),
            )
            terminal_turn = terminal.turn
            delivery = VoiceTerminalAnnouncementResult(
                turn=terminal_turn,
                speech_outcome=self._fallback_terminal_speech_outcome(terminal_turn),
            )
        await self._record_turn_event(
            terminal_turn,
            "turn",
            terminal_kind,
        )
        if (
            terminal_turn.session_id,
            terminal_turn.session_generation,
        ) in self.listening_sessions:
            await self._set_turn_idle(
                terminal_turn,
                listening=True,
                user_input_gate=False,
            )
        return delivery if with_delivery_status else terminal_turn

    def _fallback_terminal_speech_outcome(self, turn: VoiceTurnRecord) -> str:
        """Classify a failed scheduling path without consulting speech content."""

        key = (turn.session_id, turn.session_generation)
        if (
            key in self.announcement_closed_sessions
            or key in self.announcement_muted_sessions
            or key in self.announcement_suspended_sessions
            or turn.turn_id in self.announcement_abandoned_turns
        ):
            return "suppressed"
        return "failed"

    async def stop_session_speech(
        self,
        session_id: str,
        generation: int,
    ) -> None:
        """Intentionally stop one exact session-generation output stream.

        Routing runtime stop/background controls through the serialized runner
        binds a later worker ``speech_interrupted`` event to the exact active
        turn.  A session without a live runner still receives the same bounded
        media stop command, but no unrelated turn is inferred or mutated.
        """

        if not isinstance(session_id, str) or not session_id:
            raise ValueError("invalid_session_id")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("invalid_generation")
        key = (session_id, generation)
        async with self.announcement_runner_lock:
            runner = self.announcement_runners.get(key)
        if runner is not None and not runner.task.done():
            future = asyncio.get_running_loop().create_future()
            runner.submit(_AnnouncementCommand("stop", None, future))
            await future
            return
        session = await self.media.current_session(session_id, generation)
        if session is not None:
            await self.media.barge_in(session)

    async def _announcement_runner(
        self,
        turn: VoiceTurnRecord,
    ) -> _SessionAnnouncementRunner:
        """Return the one stream owner for an exact session generation."""

        if not isinstance(turn, VoiceTurnRecord):
            raise TypeError("turn must be VoiceTurnRecord")
        key = (turn.session_id, turn.session_generation)
        async with self.announcement_runner_lock:
            if (
                key in self.announcement_closed_sessions
                or turn.turn_id in self.announcement_abandoned_turns
            ):
                raise VoiceBootstrapError("voice_announcement_lifecycle_ended")
            current = self.announcement_runners.get(key)
            if current is not None and not current.task.done():
                return current
            clock = self.announcement_clock_factory()
            if not isinstance(clock, CoordinatorClock):
                raise TypeError(
                    "announcement clock factory must return CoordinatorClock"
                )
            runner = _SessionAnnouncementRunner(
                self,
                session_id=turn.session_id,
                generation=turn.session_generation,
                clock=clock,
                muted=(
                    key in self.announcement_muted_sessions
                    or key in self.announcement_suspended_sessions
                ),
            )
            self.announcement_runners[key] = runner
            return runner

    @staticmethod
    def _remember_announcement_fence(fences: dict[Any, None], key: Any) -> None:
        """Retain a bounded exact-session/turn tombstone in insertion order."""

        fences.pop(key, None)
        fences[key] = None
        while len(fences) > _MAX_ANNOUNCEMENT_FENCES:
            del fences[next(iter(fences))]

    async def _prepare_terminal_quanta(
        self,
        turn: VoiceTurnRecord,
        *,
        terminal_kind: str,
        recap_text: str,
        recap_source: str,
        sensitivity: str,
        result_commit_id: str | None,
        attribution: str | None = None,
    ) -> tuple[VoiceTurnRecord, list[_PreparedQuantum], bool]:
        """Reserve bounded terminal speech before the durable terminal fence."""

        refreshed = await asyncio.to_thread(
            self.repository.get_turn,
            user_id=turn.user_id,
            turn_id=turn.turn_id,
        )
        kind = {
            "succeeded": "result",
            "failed": "failure",
            "refused": "refusal",
            "cancelled": "cancellation",
        }.get(terminal_kind)
        if kind is None:
            raise ValueError("invalid_terminal_kind")
        texts = (
            _result_quanta(recap_text, attribution=attribution)
            if kind == "result"
            else [""]
        )
        reserved: list[_PreparedQuantum] = []
        cursor = refreshed
        for text in texts:
            try:
                mutation = await self._reserve_announcement(cursor, kind)
                claim_text = text
                if kind != "result":
                    phrase_key = mutation.claim.phrase_key
                    if phrase_key is None:
                        raise VoiceBootstrapError("terminal_phrase_unavailable")
                    claim_text = APPROVED_PHRASE_TEXT[phrase_key]
                await self._complete_announcement(
                    cursor,
                    mutation.claim.claim_id,
                )
                reserved.append(
                    _PreparedQuantum(
                        turn=cursor,
                        mutation=mutation,
                        text=claim_text,
                        claim_completed=True,
                    )
                )
                cursor = await asyncio.to_thread(
                    self.repository.get_turn,
                    user_id=cursor.user_id,
                    turn_id=cursor.turn_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "voice_terminal_reservation_failed reason=%s",
                    _safe_failure_reason(exc),
                )
                break
        terminal = await asyncio.to_thread(
            self.repository.terminalize_turn,
            user_id=refreshed.user_id,
            turn_id=refreshed.turn_id,
            terminal_kind=terminal_kind,
            result_commit_id=result_commit_id,
            recap_source=recap_source,
            sensitivity=sensitivity,
            now=datetime.now(UTC),
        )
        return (
            terminal.turn,
            [
                _PreparedQuantum(
                    turn=terminal.turn,
                    mutation=item.mutation,
                    text=item.text,
                    claim_completed=item.claim_completed,
                )
                for item in reserved
            ],
            len(reserved) == len(texts),
        )

    async def _prepare_preacceptance_quantum(
        self,
        turn: VoiceTurnRecord,
        *,
        reason: str | None,
    ) -> _PreparedQuantum:
        """Reserve one exact abandoned-turn phrase with no caller text."""

        try:
            kind, phrase_key = PREACCEPTANCE_REJECTION_PHRASES[reason]
        except (KeyError, TypeError):
            raise VoiceBootstrapError(
                "invalid_preacceptance_rejection_reason"
            ) from None
        refreshed = await asyncio.to_thread(
            self.repository.get_turn,
            user_id=turn.user_id,
            turn_id=turn.turn_id,
        )
        if (
            refreshed.session_id != turn.session_id
            or refreshed.session_generation != turn.session_generation
            or refreshed.media_grant_revision != turn.media_grant_revision
            or refreshed.state != "abandoned"
            or refreshed.rejection_reason != reason
            or refreshed.message_id is not None
            or refreshed.accepted_at is not None
        ):
            raise VoiceBootstrapError("stale_preacceptance_rejection")
        mutation = await self._reserve_announcement(
            refreshed,
            kind,
            expected_phrase_key=phrase_key,
            authorized_preacceptance_rejection_reason=reason,
        )
        await self._complete_announcement(
            refreshed,
            mutation.claim.claim_id,
        )
        return _PreparedQuantum(
            turn=refreshed,
            mutation=mutation,
            text=APPROVED_PHRASE_TEXT[phrase_key],
            claim_completed=True,
        )

    async def _prepare_sensitive_quanta(
        self,
        turn: VoiceTurnRecord,
        text: str,
    ) -> list[_PreparedQuantum]:
        """Reserve consented detail quanta for the same serialized stream."""

        prepared: list[_PreparedQuantum] = []
        cursor = turn
        for quantum_text in _sensitive_result_quanta(text):
            try:
                mutation = await self._reserve_announcement(
                    cursor,
                    "result",
                    authorized_terminal_sensitive_recap=True,
                )
                await self._complete_announcement(
                    cursor,
                    mutation.claim.claim_id,
                )
                prepared.append(
                    _PreparedQuantum(
                        turn=cursor,
                        mutation=mutation,
                        text=quantum_text,
                        sensitive_authorized=True,
                        claim_completed=True,
                    )
                )
                cursor = await asyncio.to_thread(
                    self.repository.get_turn,
                    user_id=cursor.user_id,
                    turn_id=cursor.turn_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "voice_sensitive_reservation_failed reason=%s",
                    _safe_failure_reason(exc),
                )
                break
        return prepared

    async def remember_sensitive_recap(
        self,
        turn: VoiceTurnRecord,
        *,
        result_id: str,
        text: str,
    ) -> None:
        """Stage one bounded result recap only in process memory."""

        await self.sensitive_recaps.remember(
            user_id=turn.user_id,
            session_id=turn.session_id,
            generation=turn.session_generation,
            media_grant_revision=turn.media_grant_revision,
            turn_id=turn.turn_id,
            result_id=result_id,
            text=text,
            now=datetime.now(UTC),
        )

    async def consent_sensitive_recap(
        self,
        *,
        user_id: str,
        session_id: str,
        result_id: str,
        control: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> None:
        """Consume one exact consent and speak only its bound sensitive recap."""

        now = datetime.now(UTC)
        session = await asyncio.to_thread(
            self.repository.get_controlled_session,
            user_id=user_id,
            session_id=session_id,
            expected_generation=request["expected_generation"],
            expected_media_grant_revision=request["expected_media_grant_revision"],
            control=SessionControl(
                device_id=control["device_id"],
                connection_generation=control["connection_generation"],
                binding_id=control["binding_id"],
                binding_expires_at=control["binding_expires_at"],
            ),
            now=now,
        )
        turn = await asyncio.to_thread(
            self.repository.get_turn,
            user_id=user_id,
            turn_id=request["turn_id"],
        )
        if not (
            request.get("consent_method") in {"tap", "strict_spoken_control"}
            and turn.session_id == session.session_id
            and turn.session_generation == session.generation
            and turn.media_grant_revision == session.media_grant_revision
            and turn.result_commit_id == result_id
            and turn.state == "succeeded"
            and turn.sensitivity == "sensitive"
        ):
            raise VoiceApiError("sensitive_consent_scope_mismatch", status_code=409)
        try:
            text = await self.sensitive_recaps.consume(
                user_id=user_id,
                session_id=session_id,
                generation=session.generation,
                media_grant_revision=session.media_grant_revision,
                turn_id=turn.turn_id,
                result_id=result_id,
                now=now,
            )
        except VoiceRecapError as exc:
            raise VoiceApiError(exc.code, status_code=409) from None
        future = asyncio.get_running_loop().create_future()
        runner = await self._announcement_runner(turn)
        runner.submit(
            _AnnouncementCommand(
                "sensitive",
                turn,
                future,
                sensitive_text=text,
            )
        )
        try:
            await future
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_sensitive_recap_unavailable reason=%s",
                _safe_failure_reason(exc),
            )

    async def _set_turn_idle(
        self,
        turn: VoiceTurnRecord,
        *,
        listening: bool,
        user_input_gate: bool,
    ) -> None:
        try:
            await asyncio.to_thread(
                self.repository.set_true_idle,
                user_id=turn.user_id,
                session_id=turn.session_id,
                expected_generation=turn.session_generation,
                listening=listening,
                user_input_gate=user_input_gate,
                now=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Voice turn idle fence unavailable", exc_info=True)

    async def _reserve_announcement(
        self,
        turn: VoiceTurnRecord,
        kind: str,
        *,
        expected_phrase_key: str | None = None,
        authorized_terminal_sensitive_recap: bool = False,
        authorized_preacceptance_rejection_reason: str | None = None,
    ) -> AnnouncementMutation:
        role = (
            (
                "result_opening"
                if turn.result_quantum_count == 0
                else "result_continuation"
            )
            if kind == "result"
            else "single"
        )
        request = AnnouncementClaimRequest(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            generation=turn.session_generation,
            claim_id=str(uuid.uuid4()),
            kind=kind,
            quantum_role=role,
            expected_sequence=turn.announcement_sequence,
            expected_result_reserved_samples=turn.result_reserved_samples,
            expected_phrase_key=expected_phrase_key,
            authorized_terminal_sensitive_recap=(authorized_terminal_sensitive_recap),
            expected_media_grant_revision=turn.media_grant_revision,
            authorized_preacceptance_rejection_reason=(
                authorized_preacceptance_rejection_reason
            ),
        )
        return await self.coordinator.claim_turn_announcement(
            user_id=turn.user_id,
            request=request,
        )

    async def _complete_announcement(
        self,
        turn: VoiceTurnRecord,
        claim_id: str,
    ) -> None:
        try:
            await self.coordinator.complete_turn_announcement(
                user_id=turn.user_id,
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                generation=turn.session_generation,
                claim_id=claim_id,
            )
        except Exception:
            logger.debug(
                "Voice announcement claim release unavailable",
                exc_info=True,
            )

    async def close(self) -> None:
        maintenance = self.maintenance_task
        self.maintenance_task = None
        if maintenance is not None:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
        try:
            ended = await asyncio.to_thread(
                self.repository.end_owned_sessions,
                owner_id=self.coordinator.replica_id,
                reason="shutdown",
                now=datetime.now(UTC),
            )
        except Exception:
            ended = ()
            logger.warning(
                "voice_shutdown_durable_fence_unavailable",
                exc_info=True,
            )
        for session in ended:
            await self._cleanup_ended_session(session, "shutdown")
        self.listening_sessions.clear()
        self.reconnecting_sessions.clear()
        guidance_tasks = tuple(self.preacceptance_guidance_tasks)
        self.preacceptance_guidance_tasks.clear()
        for task in guidance_tasks:
            task.cancel()
        if guidance_tasks:
            await asyncio.gather(*guidance_tasks, return_exceptions=True)
        async with self.announcement_runner_lock:
            runners = tuple(self.announcement_runners.values())
            self.announcement_runners.clear()
            self.announcement_muted_sessions.clear()
            self.announcement_suspended_sessions.clear()
            self.preacceptance_guided_turns.clear()
        if runners:
            await asyncio.gather(
                *(runner.close() for runner in runners),
                return_exceptions=True,
            )
        await self.sensitive_recaps.clear()
        try:
            await self.worker_pool.shutdown()
        finally:
            await self.livekit.close()


def build_voice_services(
    database: Any,
    *,
    environ: Mapping[str, str] | None = None,
    observability: RuntimeObservability | None = None,
) -> VoiceServices:
    values = environ if environ is not None else os.environ
    environment = values.get("ASTRAL_ENV", "").strip().lower() or "production"
    development = environment in {"development", "dev", "test"}
    closure = values.get("VOICE_WORKER_CLOSURE_SHA256", "").strip()
    if not closure and development:
        closure = "0" * 64
    if _SHA256.fullmatch(closure) is None:
        raise VoiceBootstrapError("invalid_voice_worker_closure")
    if closure == "0" * 64 and not development:
        raise VoiceBootstrapError("unapproved_voice_worker_closure")
    replica_id = values.get("VOICE_COORDINATOR_REPLICA_ID", "").strip()
    if not replica_id:
        if not development:
            raise VoiceBootstrapError("missing_voice_replica_id")
        replica_id = "voice-coordinator-local-1"

    voice_observability = observability or RuntimeObservability(
        retention_seconds=_bounded_int(
            values,
            "OPERATION_RETENTION_SECONDS",
            86_400,
            1,
            31_536_000,
        ),
        deployment_instance=values.get(
            "RUNTIME_METRICS_INSTANCE",
            "astraldeep",
        ).strip()
        or "astraldeep",
    )

    livekit_settings = LiveKitSettings.from_environ(values)
    worker_control_settings = WorkerControlSettings.from_environ(values)
    watch_bridge_url = values.get("VOICE_WATCH_BRIDGE_PUBLIC_URL", "").strip()
    if not watch_bridge_url and development:
        watch_bridge_url = "wss://localhost:7890/api/voice/watch-bridge"
    if not watch_bridge_url:
        raise VoiceBootstrapError("missing_watch_bridge_url")
    worker_pool = WorkerPool(
        WorkerPoolPolicy(
            runtime_closure_sha256=closure,
            max_workers=_bounded_int(values, "VOICE_MAX_WORKERS", 8, 1, 32),
            max_sessions_per_worker=_bounded_int(
                values,
                "VOICE_MAX_SESSIONS_PER_WORKER",
                4,
                1,
                100,
            ),
            max_total_sessions=_bounded_int(
                values,
                "VOICE_MAX_TOTAL_SESSIONS",
                100,
                1,
                1_000,
            ),
            allow_unapproved_development_closure=development,
            allow_insecure_livekit_url=development,
        )
    )
    livekit = LiveKitService(livekit_settings)
    repository = VoiceSessionRepository(database)
    coordinator = VoiceCoordinator(
        worker_pool,
        repository,
        replica_id=replica_id,
    )
    capability = VoiceCapabilityService(
        livekit=livekit,
        workers=worker_pool,
        feature_enabled=lambda: flags.is_enabled("conversational_voice"),
        supported_transports=("livekit", "watch_pcm_websocket"),
        observability=voice_observability,
    )
    media = DirectRtcVoiceMedia(
        livekit=livekit,
        workers=worker_pool,
        watch_ticket_secret=worker_control_settings.secret,
        watch_bridge_url=watch_bridge_url,
        observability=voice_observability,
    )
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=capability,
        media=media,
        replica_id=replica_id,
        media_grant_secret=worker_control_settings.secret,
        observability=voice_observability,
    )
    services = VoiceServices(
        livekit=livekit,
        worker_pool=worker_pool,
        repository=repository,
        coordinator=coordinator,
        capability=capability,
        media=media,
        runtime=runtime,
        worker_control_settings=worker_control_settings,
        observability=voice_observability,
    )
    runtime.bind_speech_mute_handler(services.set_session_speech_muted)
    runtime.bind_speech_stop_handler(services.stop_session_speech)
    runtime.bind_speech_suspend_handler(services.set_session_speech_suspended)
    runtime.bind_session_end_handler(services.handle_runtime_session_end)
    return services


def install_voice_worker_control(
    app: Any,
    services: VoiceServices | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> WorkerControlEndpoint | None:
    """Keep the route absent when fail-closed voice construction did not pass."""

    if services is None:
        return None
    return services.install_worker_control(app, environ=environ)


def _metric_dimensions(session: VoiceSessionRecord) -> tuple[str, str]:
    """Map durable media vocabulary to the reviewed metric dimensions."""

    transport = (
        "watch_bridge"
        if session.transport == "watch_pcm_websocket"
        else session.transport
    )
    return session.device_kind, transport


def _bounded_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name, "").strip()
    try:
        value = default if not raw else int(raw)
    except ValueError:
        raise VoiceBootstrapError("invalid_voice_capacity") from None
    if not minimum <= value <= maximum:
        raise VoiceBootstrapError("invalid_voice_capacity")
    return value


def _settle(future: asyncio.Future[Any], value: Any) -> None:
    """Complete one in-process waiter without raising on a stale caller."""

    if not future.done():
        future.set_result(value)


def _safe_failure_reason(exc: BaseException) -> str:
    """Return only an allowlisted content-free exception reason."""

    code = getattr(exc, "code", None)
    if isinstance(code, str) and _SAFE_REASON.fullmatch(code) is not None:
        return code
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ValueError | TypeError):
        return "invalid_state"
    return "internal_error"


def _result_quanta(text: str, *, attribution: str | None = None) -> list[str]:
    """Fit a recap into one 1.5s opening plus <=7 four-second continuations."""

    openings = {
        None: "Done.",
        "earlier": "Earlier request done.",
        "latest": "Latest request done.",
    }
    try:
        opening = openings[attribution]
    except (KeyError, TypeError):
        raise ValueError("invalid_result_attribution") from None
    if not isinstance(text, str) or not text.strip():
        return [opening]
    words = text.split()[:63]
    quanta = [opening]
    quanta.extend(
        " ".join(words[offset : offset + 9]) for offset in range(0, len(words), 9)
    )
    return quanta


def _sensitive_result_quanta(text: str) -> list[str]:
    """Fit consented details into the seven remaining result continuations."""

    if not isinstance(text, str) or not text.strip():
        return ["The sensitive result is available on screen."]
    words = text.split()[:63]
    return [" ".join(words[offset : offset + 9]) for offset in range(0, len(words), 9)]


__all__ = [
    "VoiceBootstrapError",
    "VoiceServices",
    "VoiceTerminalAnnouncementResult",
    "build_voice_services",
    "install_voice_worker_control",
]
