"""Bounded session ownership for the isolated voice-worker control client."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from .voice_transcript import (
    TranscriptProofBinding,
    TranscriptProofError,
    TranscriptSessionScope,
    canonical_transcript,
    derive_session_proof_key,
    issue_transcript_proof_with_key,
)

logger = logging.getLogger(__name__)
_LIVEKIT_VENDOR_LOGGERS = ("livekit", "livekit.rtc", "livekit.rtc.synchronizer")

SESSION_QUEUE_SIZE = 32
SHUTDOWN_TIMEOUT_SECONDS = 5.0
AUDIO_STREAM_SAMPLE_RATE = 16_000
AUDIO_STREAM_FRAME_MS = 32
AUDIO_FRAME_SAMPLES = 512
AUDIO_STREAM_CAPACITY = 32
RTC_EVENT_QUEUE_SIZE = 64
RTC_CONNECT_TIMEOUT_SECONDS = 8.0
OUTPUT_SAMPLE_RATE = 24_000
OUTPUT_FRAME_SAMPLES = 480
OUTPUT_QUEUE_MS = 200
SPEECH_QUIESCE_TIMEOUT_SECONDS = 0.25
OUTPUT_OPERATION_TIMEOUT_SECONDS = 0.25
OUTPUT_DRAIN_TIMEOUT_SECONDS = 0.5
CLIENT_PLAYOUT_CONFIRMATION_TIMEOUT_SECONDS = 8.0
# A client terminal event proves that the authenticated device finished its
# declared playout, but it cannot prove that the device audio route and the
# upstream LiveKit microphone buffer contain no final render-tail frames. Keep
# capture fenced for one short, bounded tail window after ordinary playout.
# Explicit barge-in still reopens immediately through the separate stop path.
POST_PLAYOUT_CAPTURE_GUARD_SECONDS = 0.5
SELF_SPEECH_SUPPRESSION_WINDOW_SECONDS = 3.0
MAX_RECENT_SPEECH_FINGERPRINTS = 8
VAD_THRESHOLD = 0.5
# Silero's streaming iterator uses a lower negative threshold once speech has
# started so small posterior dips do not split an utterance. Keep the launch
# threshold unchanged, but require both 64 ms of high-confidence evidence and
# a 128-ms candidate before allocating a turn. Ambiguous frames provide bounded
# pre-roll and may bridge that evidence within one 512-ms window. This survives
# normal Opus posterior smoothing while rejecting a lone spike and keeping the
# pre-recognition buffer finite. The turn endpoints after a bounded run of
# below-release evidence so ordinary clause pauses survive as one
# conversational turn (VAD_END_SILENCE_FRAMES below).
VAD_RELEASE_THRESHOLD = VAD_THRESHOLD - 0.15
VAD_MIN_HIGH_CONFIDENCE_FRAMES = 2
VAD_MIN_CANDIDATE_FRAMES = 4
VAD_MAX_CANDIDATE_FRAMES = 16
# 066 R-9 follow-through: a TRUE bounded pre-roll. Speech onsets ramp from
# below the release threshold (unvoiced plosives, soft attacks), and the
# candidate buffer resets on any sub-release frame — so the head of an
# utterance was lost and users were transcribed "from the middle". The ring
# retains the last 24 admitted frames (768 ms — a strict superset of the
# 512 ms candidate window) regardless of posterior dips, and activation
# seeds the utterance from it. Fenced frames never reach the ring (admission
# is checked upstream), and a capture-epoch change (fence transition) clears
# it, so audio from before an assistant playout can never resurface in the
# next turn.
VAD_PREROLL_FRAMES = 24
# Feature 066 near-real-time tuning. The 065 launch value was a fixed 40
# frames (1.28 s); that figure was an implementation choice, never a spec
# contract (no 065 spec/plan/research text pins it). Endpointing floors of
# roughly 0.8-1.0 s are standard for conversational agents, and 066 optimizes
# time-to-transcript, so the default drops to 960 ms (30 frames). Operators
# tune VOICE_ENDPOINT_SILENCE_MS, clamped to a sane [320, 2560] ms and rounded
# to whole 32 ms frames; unset/invalid values fall back to the default. Read
# once at import like the feature flags - changing it requires a restart.
_ENDPOINT_SILENCE_DEFAULT_MS = 960
_ENDPOINT_SILENCE_MIN_MS = 320
_ENDPOINT_SILENCE_MAX_MS = 2_560


def _endpoint_silence_frames(raw: str | None) -> int:
    """Return the clamped, frame-rounded endpoint-silence run length."""

    try:
        requested_ms = int(str(raw).strip(), 10)
    except (TypeError, ValueError):
        requested_ms = _ENDPOINT_SILENCE_DEFAULT_MS
    clamped_ms = min(
        max(requested_ms, _ENDPOINT_SILENCE_MIN_MS), _ENDPOINT_SILENCE_MAX_MS
    )
    return max(1, round(clamped_ms / AUDIO_STREAM_FRAME_MS))


VAD_END_SILENCE_FRAMES = _endpoint_silence_frames(
    os.environ.get("VOICE_ENDPOINT_SILENCE_MS")
)
# Feature 066: the endpoint-silence run is proven non-speech frame by frame
# (any at-or-above-release frame resets the counter), so all but a short tail
# is trimmed before the batch ASR POST - at the default endpoint that removes
# ~0.83 s of upload bytes and whisper decode time from every turn. Four frames
# (128 ms) of retained tail preserve the release transient so the recognizer
# closes the final word cleanly.
ASR_TAIL_SILENCE_FRAMES = 4
MAX_UTTERANCE_FRAMES = 1_875
# Whisper is documented to mint stock phrases from speech-free audio ("Thank
# you.", "Obrigado.", ... — both observed live 2026-08-05 entering chat as
# genuine user turns). Refusal requires a CONJUNCTION: the canonical text is
# one of these normalized stock phrases AND the utterance carried fewer
# at-or-above-VAD_THRESHOLD frames than the shortest of them can physically
# be spoken in (8 frames = 256 ms at the 32 ms frame size). Neither half is
# safe alone: the phrase list would eat a genuine "thank you", the duration
# floor would eat genuine short commands ("stop", "yes").
ASR_HALLUCINATION_MIN_VOICED_FRAMES = 8
_ASR_STOCK_HALLUCINATIONS = frozenset(
    {
        "thankyou",
        "thankyouverymuch",
        "thanksforwatching",
        "thankyouforwatching",
        "thankyousomuchforwatching",
        "obrigado",
        "obrigada",
        "you",
        "bye",
        "goodbye",
    }
)
MAX_RETAINED_FINALS = 4
MAX_RETAINED_FINAL_BYTES = 48 * 1024
MAX_SEEN_ANNOUNCEMENTS = 64
MAX_CLOSED_SESSION_FENCES = 256
VOICE_TRANSCRIPT_TOPIC = "astraldeep.voice.transcript.v1"
VOICE_ANNOUNCEMENT_TOPIC = "astraldeep.voice.announcement.v1"
MAX_TRANSCRIPT_ENVELOPE_BYTES = 12 * 1024
MAX_ANNOUNCEMENT_ENVELOPE_BYTES = 4 * 1024
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_SAFE_FAILURE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ProtocolViolation(RuntimeError):
    """A content-free authenticated control-protocol violation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CapacityExceeded(RuntimeError):
    """The authenticated worker has no unclaimed session slot."""


class AssignmentConflict(RuntimeError):
    """A session was rebound without an authorized assignment transition."""


class ClosedSessionRace(RuntimeError):
    """A command arrived for an exact generation that just closed locally."""

    def __init__(
        self,
        *,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        media_state: str,
    ) -> None:
        self.session_id = session_id
        self.generation = generation
        self.media_grant_revision = media_grant_revision
        self.media_state = media_state
        super().__init__("session_closed_race")


class RtcSessionError(RuntimeError):
    """A redacted direct-RTC failure safe for control-plane reporting."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True, repr=False)
class SessionNotice:
    """One bounded event emitted from the serialized media owner.

    Text is deliberately excluded from ``repr`` so an accidental exception or
    diagnostic representation cannot copy transcript content into logs.
    """

    kind: str
    reason: str | None = None
    announcement_id: str | None = None
    text: str | None = field(default=None, repr=False)
    language: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return (
            "SessionNotice("
            f"kind={self.kind!r}, reason={self.reason!r}, "
            f"announcement_id={self.announcement_id!r}, "
            "text=<redacted>, "
            f"language={self.language!r}, metadata=<redacted>)"
        )


class VadEngine(Protocol):
    """Exact-frame voice activity inference owned by one session."""

    def probability(self, pcm_s16le: bytes) -> float:
        """Return one speech probability for exactly 512 mono 16-kHz samples."""

    def reset(self) -> None:
        """Clear recurrent state between utterances and on every fence."""


class AsrAdapter(Protocol):
    """Bounded batch transcription seam."""

    async def transcribe_pcm16(self, pcm_s16le: bytes) -> Any:
        """Transcribe one already-ended in-memory utterance."""


class TtsAdapter(Protocol):
    """Bounded fixed-profile synthesis seam."""

    async def synthesize(self, text: str, *, max_duration_samples: int) -> Any:
        """Return validated mono 24-kHz PCM."""


class RtcFactory(Protocol):
    """Narrow factory around the exact pinned ``livekit.rtc`` API."""

    def create_room(self) -> Any: ...

    def room_options(self, *, auto_subscribe: bool, connect_timeout: float) -> Any: ...

    def is_audio_publication(self, publication: Any) -> bool: ...

    def is_microphone_publication(self, publication: Any) -> bool: ...

    def create_audio_stream(
        self,
        track: Any,
        *,
        capacity: int,
        sample_rate: int,
        num_channels: int,
        frame_size_ms: int,
    ) -> Any: ...

    def decode_audio_event(self, event: Any) -> tuple[bytes, int, int, int]: ...

    def stream_buffer_depth(self, event: Any, stream: Any) -> int: ...

    def create_audio_source(
        self,
        *,
        sample_rate: int,
        num_channels: int,
        queue_size_ms: int,
    ) -> Any: ...

    def create_local_audio_track(self, name: str, source: Any) -> Any: ...

    def track_publish_options(self) -> Any: ...

    def create_output_frame(
        self, pcm_s16le: bytes, *, sample_rate: int, num_channels: int
    ) -> Any: ...


class LiveKitRtcFactory:
    """Production adapter for the audited ``livekit==1.1.14`` surface.

    The import is intentionally delayed. Host contract tests can exercise the
    state machine with injected fakes, while the isolated worker image proves
    the native package and this adapter together under Python 3.11.
    """

    def __init__(self, rtc_module: Any | None = None) -> None:
        # The dependency's diagnostic payloads may include credentialed RTC
        # state. Product-owned logs remain bounded and content-free.
        for logger_name in _LIVEKIT_VENDOR_LOGGERS:
            logging.getLogger(logger_name).disabled = True
        if rtc_module is None:
            try:
                from livekit import rtc as rtc_module
            except ImportError:  # pragma: no cover - image/host split
                raise RtcSessionError("livekit_runtime_unavailable") from None
        self._rtc = rtc_module

    def create_room(self) -> Any:
        return self._rtc.Room()

    def room_options(self, *, auto_subscribe: bool, connect_timeout: float) -> Any:
        return self._rtc.RoomOptions(
            auto_subscribe=auto_subscribe,
            connect_timeout=connect_timeout,
        )

    def is_audio_publication(self, publication: Any) -> bool:
        return publication.kind == self._rtc.TrackKind.KIND_AUDIO

    def is_microphone_publication(self, publication: Any) -> bool:
        return publication.source == self._rtc.TrackSource.SOURCE_MICROPHONE

    def create_audio_stream(
        self,
        track: Any,
        *,
        capacity: int,
        sample_rate: int,
        num_channels: int,
        frame_size_ms: int,
    ) -> Any:
        return self._rtc.AudioStream.from_track(
            track=track,
            capacity=capacity,
            sample_rate=sample_rate,
            num_channels=num_channels,
            frame_size_ms=frame_size_ms,
        )

    def decode_audio_event(self, event: Any) -> tuple[bytes, int, int, int]:
        frame = event.frame
        return (
            bytes(frame.data.cast("B")),
            frame.sample_rate,
            frame.num_channels,
            frame.samples_per_channel,
        )

    def stream_buffer_depth(self, event: Any, stream: Any) -> int:
        del event
        # This private shape is intentionally guarded against the exact pin.
        # The public API exposes no overflow count even though RingQueue drops
        # its oldest item at capacity. Seeing a full queue therefore aborts the
        # utterance conservatively before damaged audio reaches ASR.
        queue = getattr(stream, "_queue", None)
        buffered = getattr(queue, "_queue", ())
        return len(buffered)

    def create_audio_source(
        self,
        *,
        sample_rate: int,
        num_channels: int,
        queue_size_ms: int,
    ) -> Any:
        return self._rtc.AudioSource(
            sample_rate=sample_rate,
            num_channels=num_channels,
            queue_size_ms=queue_size_ms,
        )

    def create_local_audio_track(self, name: str, source: Any) -> Any:
        return self._rtc.LocalAudioTrack.create_audio_track(name, source)

    def track_publish_options(self) -> Any:
        options = self._rtc.TrackPublishOptions()
        # LiveKit's audio-source grant vocabulary has no distinct
        # "assistant" source. Publish the worker's synthetic mono audio under
        # the microphone audio class so the room grant can remain restricted
        # to audio only instead of granting every camera/screen source.
        options.source = self._rtc.TrackSource.SOURCE_MICROPHONE
        return options

    def create_output_frame(
        self, pcm_s16le: bytes, *, sample_rate: int, num_channels: int
    ) -> Any:
        if len(pcm_s16le) % (2 * num_channels):
            raise RtcSessionError("invalid_output_pcm")
        frame = self._rtc.AudioFrame.create(
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=len(pcm_s16le) // (2 * num_channels),
        )
        frame.data.cast("B")[:] = pcm_s16le
        return frame


DEFAULT_VAD_MODEL_PATH = "/opt/voice-assets/silero_vad.onnx"
# One ONNX graph per model path per worker process. Building it costs a
# blocking disk read plus a graph load, and it used to run once per voice
# session inside the supervisor-wide lock, serializing every concurrent
# activation behind it. Only a successful build is shared: a failure must stay
# reproducible for the next construction of the same path.
_VAD_INFERENCE_SESSIONS: dict[str, Any] = {}


def _build_vad_inference_session(model_path: Path | str) -> Any:
    try:
        import onnxruntime as ort
    except ImportError:  # pragma: no cover - image/host split
        raise RtcSessionError("vad_runtime_unavailable") from None
    path = Path(model_path)
    if not path.is_file():
        raise RtcSessionError("vad_model_unavailable")
    key = str(path.resolve())
    cached = _VAD_INFERENCE_SESSIONS.get(key)
    if cached is not None:
        return cached
    try:
        built = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    except Exception:
        raise RtcSessionError("vad_model_invalid") from None
    _VAD_INFERENCE_SESSIONS[key] = built
    return built


async def preload_vad_model(model_path: Path | str = DEFAULT_VAD_MODEL_PATH) -> None:
    """Build the shared VAD graph off the event loop before the first session."""

    try:
        await asyncio.to_thread(_build_vad_inference_session, model_path)
    except Exception:
        # The per-session constructor raises the same reason again; a missing
        # or unreadable asset must not change when the worker gives up.
        return


class SileroVad:
    """Exact Silero v6 recurrent ONNX inference for 32-ms input frames."""

    def __init__(
        self,
        *,
        model_path: Path | str = DEFAULT_VAD_MODEL_PATH,
    ) -> None:
        try:
            import numpy as np
        except ImportError:  # pragma: no cover - image/host split
            raise RtcSessionError("vad_runtime_unavailable") from None
        self._session = _build_vad_inference_session(model_path)
        self._np = np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    @property
    def recurrent_state(self) -> Any:
        """Expose state only for exact in-image shape/reset verification."""

        return self._state

    @property
    def recurrent_state_shape(self) -> tuple[int, ...]:
        return tuple(self._state.shape)

    def probability(self, pcm_s16le: bytes) -> float:
        if len(pcm_s16le) != AUDIO_FRAME_SAMPLES * 2:
            raise RtcSessionError("invalid_vad_frame")
        samples = self._np.frombuffer(pcm_s16le, dtype="<i2").astype(self._np.float32)
        samples = (samples / 32768.0).reshape(1, AUDIO_FRAME_SAMPLES)
        try:
            output, next_state = self._session.run(
                None,
                {
                    "input": samples,
                    "state": self._state,
                    "sr": self._np.array(
                        AUDIO_STREAM_SAMPLE_RATE, dtype=self._np.int64
                    ),
                },
            )
        except Exception:
            raise RtcSessionError("vad_inference_failed") from None
        if output.shape != (1, 1) or next_state.shape != (2, 1, 128):
            raise RtcSessionError("vad_output_invalid")
        probability = float(output[0, 0])
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise RtcSessionError("vad_output_invalid")
        self._state = next_state
        return probability

    def reset(self) -> None:
        self._state.fill(0.0)


@dataclass(slots=True, repr=False)
class WorkerRtcGrant:
    """A short-lived direct-RTC grant whose bearer remains memory-only."""

    revision: int
    livekit_url: str
    join_token: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    room_name: str
    worker_identity: str

    def clear_secrets(self) -> None:
        """Drop the only worker-owned reference to the join bearer."""

        self.join_token = ""

    def __repr__(self) -> str:
        return (
            "WorkerRtcGrant("
            f"revision={self.revision!r}, livekit_url=<redacted>, "
            "join_token=<redacted>, "
            f"issued_at={self.issued_at!r}, expires_at={self.expires_at!r}, "
            f"room_name={self.room_name!r}, "
            f"worker_identity={self.worker_identity!r})"
        )


@dataclass(slots=True, repr=False)
class SessionBinding:
    """Validated immutable assignment fields plus a clearable RTC bearer."""

    session_id: str
    generation: int
    assignment_id: str
    room_name: str
    worker_identity: str
    transport: str
    media_grant_revision: int
    worker_rtc_grant_revision: int
    client_participant_identity: str
    grant_expires_at: datetime
    worker_rtc_grant: WorkerRtcGrant
    visible_chat_id: str
    chat_context_revision: int

    def clear_secrets(self) -> None:
        self.worker_rtc_grant.clear_secrets()

    @property
    def idempotency_key(self) -> tuple[str, int, str, int]:
        return (
            self.session_id,
            self.generation,
            self.assignment_id,
            self.worker_rtc_grant_revision,
        )

    @property
    def reconnect_key(self) -> tuple[str, int, str, str, str, str, int, str, str, int, str]:
        """Return content-free fields that cannot change during RTC reconnect."""

        return (
            self.session_id,
            self.generation,
            self.assignment_id,
            self.room_name,
            self.worker_identity,
            self.transport,
            self.media_grant_revision,
            self.client_participant_identity,
            self.visible_chat_id,
            self.chat_context_revision,
            self.worker_rtc_grant.livekit_url,
        )

    def __repr__(self) -> str:
        return (
            "SessionBinding("
            f"session_id={self.session_id!r}, generation={self.generation!r}, "
            f"assignment_id={self.assignment_id!r}, room_name={self.room_name!r}, "
            f"worker_identity={self.worker_identity!r}, transport={self.transport!r}, "
            f"media_grant_revision={self.media_grant_revision!r}, "
            f"worker_rtc_grant_revision={self.worker_rtc_grant_revision!r}, "
            f"client_participant_identity={self.client_participant_identity!r}, "
            f"grant_expires_at={self.grant_expires_at!r}, "
            "worker_rtc_grant=<redacted>, "
            f"visible_chat_id={self.visible_chat_id!r}, "
            f"chat_context_revision={self.chat_context_revision!r})"
        )


class SessionRuntime(Protocol):
    """The narrow control-lifecycle interface implemented by an RTC session."""

    binding: SessionBinding

    async def run(self) -> None:
        """Own session resources until closed or failed."""

    def deliver(self, frame: dict[str, Any]) -> None:
        """Queue one already-authenticated and sequence-fenced frame."""

    async def close(self, reason: str) -> None:
        """Release media, buffered frames, and bearer references."""

    @property
    def media_state(self) -> str:
        """Return the content-free media state for heartbeat telemetry."""


class BoundControlSession:
    """A bounded lifecycle holder extended by the direct-RTC implementation.

    Feature task T047 supplies media processing. Until then this class owns no
    media authority, performs no dispatch, and never reports readiness.
    """

    def __init__(
        self,
        binding: SessionBinding,
        *,
        queue_size: int = SESSION_QUEUE_SIZE,
    ) -> None:
        if not 1 <= queue_size <= SESSION_QUEUE_SIZE:
            raise ValueError("invalid_session_queue_size")
        self.binding = binding
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(queue_size)
        self._closed = asyncio.Event()
        self._media_state = "connecting"

    async def run(self) -> None:
        await self._closed.wait()

    def deliver(self, frame: dict[str, Any]) -> None:
        if self._closed.is_set():
            raise ProtocolViolation("session_closed")
        self._queue.put_nowait(frame)

    async def close(self, reason: str) -> None:
        del reason
        self._media_state = "ended"
        self._closed.set()
        while not self._queue.empty():
            try:
                frame = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            _clear_buffered_value(frame)
        self.binding.clear_secrets()

    @property
    def media_state(self) -> str:
        return self._media_state


@dataclass(slots=True)
class _OwnedEvent:
    kind: str
    args: tuple[Any, ...] = ()


@dataclass(slots=True)
class _InputHandle:
    publication: Any
    stream: Any
    pump: asyncio.Task[None]


@dataclass(slots=True)
class _RecognitionBinding:
    client_turn_id: str
    media_grant_revision: int
    visible_chat_id: str
    chat_context_revision: int
    turn_id: str | None = None
    submission_id: str | None = None
    request_generation: str | None = None
    echo_fingerprints: frozenset[bytes] = frozenset()
    # At-or-above-VAD_THRESHOLD frames observed across the whole utterance,
    # stamped at finalize. Content-free speech-evidence floor for the stock
    # hallucination refusal in _recognition_complete.
    voiced_frames: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class _RecentSpeechFingerprint:
    """Content-free fingerprint retained only for bounded echo suppression."""

    digest: bytes = field(repr=False)
    expires_at: float


@dataclass(slots=True, repr=False)
class _RetainedFinal:
    binding: _RecognitionBinding
    text: str = field(repr=False)
    language: str
    finalized_at: datetime
    envelope: dict[str, Any] | None = field(default=None, repr=False)
    envelope_bytes: int = 0

    def __repr__(self) -> str:
        return (
            "_RetainedFinal("
            f"client_turn_id={self.binding.client_turn_id!r}, "
            "text=<redacted>, envelope=<redacted>, "
            f"language={self.language!r}, finalized_at={self.finalized_at!r})"
        )


@dataclass(slots=True, repr=False)
class _SpeechMeta:
    epoch: int
    announcement_id: str
    kind: str
    text: str = field(repr=False)
    max_duration_samples: int
    announcement_sequence: int
    turn_id: str | None
    quantum_role: str
    quantum_index: int
    result_reserved_samples_after: int | None
    duration_ms: int | None = None


def _normalized_speech(canonical: str) -> str:
    """Punctuation/case-insensitive speech form shared by echo + stock checks."""

    return "".join(
        character for character in canonical.casefold() if character.isalnum()
    )


def _speech_fingerprint(text: str) -> bytes | None:
    """Hash a punctuation/case-insensitive speech form without retaining text."""

    try:
        canonical = canonical_transcript(text)
    except TranscriptProofError:
        return None
    normalized = _normalized_speech(canonical)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8", errors="strict")).digest()


def validate_announcement_binding(frame: Mapping[str, Any]) -> None:
    """Enforce the greeting/turn invariant before text reaches synthesis."""

    kind = frame.get("kind")
    turn_id = frame.get("turn_id")
    if (kind == "greeting") != (turn_id is None):
        raise ProtocolViolation("announcement_turn_mismatch")
    if not isinstance(kind, str) or not kind:
        raise ProtocolViolation("invalid_announcement_kind")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        raise ProtocolViolation("invalid_announcement_turn")
    role = frame.get("quantum_role")
    index = frame.get("quantum_index")
    ceiling = frame.get("max_duration_samples")
    if role == "single":
        if index != 0 or kind == "result":
            raise ProtocolViolation("invalid_announcement_quantum")
    elif role == "result_opening":
        if kind != "result" or index != 0 or not _is_int_between(ceiling, 1, 36_000):
            raise ProtocolViolation("invalid_announcement_quantum")
    elif role == "result_continuation":
        if kind != "result" or not _is_int_between(index, 1, 31):
            raise ProtocolViolation("invalid_announcement_quantum")
    else:
        raise ProtocolViolation("invalid_announcement_quantum")
    if not _is_int_between(ceiling, 1, 96_000):
        raise ProtocolViolation("invalid_announcement_ceiling")


class DirectRtcSession(BoundControlSession):
    """One serialized, generation-fenced direct-RTC media owner.

    RTC callbacks and audio pumps only enqueue immutable events. This task owns
    subscription, VAD/endpointing, recognition transitions, output publication,
    reconnect reconciliation, and all buffer cleanup. Coordinator protocol and
    LiveKit data-envelope publication remain injected consumers of notices.
    """

    _CALLBACK_EVENTS = (
        "participant_connected",
        "participant_disconnected",
        "track_published",
        "track_unpublished",
        "track_subscribed",
        "track_unsubscribed",
        "reconnecting",
        "reconnected",
        "disconnected",
        "local_track_republished",
    )

    def __init__(
        self,
        binding: SessionBinding,
        *,
        rtc_factory: RtcFactory,
        vad: VadEngine,
        asr: AsrAdapter,
        tts: TtsAdapter,
        notice_sink: Callable[[SessionNotice], Any],
        worker_control_secret: bytes,
        utcnow: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        input_audio_gate: Callable[[str], bool] | None = None,
        queue_size: int = SESSION_QUEUE_SIZE,
        rtc_event_queue_size: int = RTC_EVENT_QUEUE_SIZE,
    ) -> None:
        super().__init__(binding, queue_size=queue_size)
        if not 1 <= rtc_event_queue_size <= RTC_EVENT_QUEUE_SIZE:
            raise ValueError("invalid_rtc_event_queue_size")
        self._rtc_factory = rtc_factory
        self._vad = vad
        self._asr = asr
        self._tts = tts
        self._notice_sink = notice_sink
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        # Platform SDKs own acoustic echo cancellation and route-specific audio
        # processing. This synchronous seam lets their verified route state
        # fail ASR ingestion closed without making the media worker claim it
        # can acoustically prove AEC from remote PCM.
        self._input_audio_gate = input_audio_gate
        try:
            proof_key = derive_session_proof_key(
                worker_control_secret,
                TranscriptSessionScope(
                    session_id=binding.session_id,
                    generation=binding.generation,
                    assignment_id=binding.assignment_id,
                    worker_identity=binding.worker_identity,
                ),
            )
        except TranscriptProofError:
            raise ValueError("invalid_worker_control_secret") from None
        self._transcript_proof_key = bytearray(proof_key)
        self._rtc_events: asyncio.Queue[_OwnedEvent] = asyncio.Queue(
            rtc_event_queue_size
        )
        self._control_waiter: asyncio.Task[dict[str, Any]] | None = None
        self._rtc_waiter: asyncio.Task[_OwnedEvent] | None = None
        self._overrun_waiter: asyncio.Task[bool] | None = None
        self._closed_waiter: asyncio.Task[bool] | None = None
        self._overrun_wakeup = asyncio.Event()
        self._overrun_reason: str | None = None
        self._started = asyncio.Event()
        self._run_entered = False
        self._teardown_lock = asyncio.Lock()
        self._teardown_task: asyncio.Task[None] | None = None
        self._teardown_complete = False
        self._room: Any | None = None
        self._room_connected = False
        self._input_handles: dict[str, _InputHandle] = {}
        self._subscribed_sids: set[str] = set()
        self._subscribed_publications: dict[str, Any] = {}
        self._capture_requested = False
        self._capture_open = False
        self._capture_epoch = 0
        self._context_synced = False
        self._reconnecting = False
        self._utterance = bytearray()
        self._candidate_speech_frames = 0
        self._utterance_voiced_frames = 0
        self._utterance_active = False
        self._silence_frames = 0
        self._vad_preroll: deque[bytes] = deque(maxlen=VAD_PREROLL_FRAMES)
        self._preroll_epoch = 0
        self._recognizing = False
        self._recognition_task: asyncio.Task[None] | None = None
        self._client_turn_id: str | None = None
        self._recognition_binding: _RecognitionBinding | None = None
        self._recognition_bindings: dict[str, _RecognitionBinding] = {}
        self._pending_context: tuple[str, int] | None = None
        self._retained_finals: dict[str, _RetainedFinal] = {}
        self._retained_final_bytes = 0
        self._retention_expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._seen_transcript_dispositions: deque[tuple[str, ...]] = deque(maxlen=64)
        self._speech_epoch = 0
        self._speech_meta: _SpeechMeta | None = None
        self._synthesis_task: asyncio.Task[None] | None = None
        self._speech_producer: asyncio.Task[None] | None = None
        self._playout_capture_hold = False
        self._playout_confirmation_task: asyncio.Task[None] | None = None
        self._playout_tail_guard = False
        self._playout_tail_task: asyncio.Task[None] | None = None
        self._playout_hold_epoch = 0
        self._playout_echo_fingerprint: bytes | None = None
        self._recent_speech_fingerprints: deque[_RecentSpeechFingerprint] = deque(
            maxlen=MAX_RECENT_SPEECH_FINGERPRINTS
        )
        self._retired_output_tasks: set[asyncio.Task[Any]] = set()
        self._output_source: Any | None = None
        self._output_publication: Any | None = None
        self._output_track_sid: str | None = None
        self._announcement_manifest_published = False
        self._seen_announcements: deque[str] = deque(maxlen=MAX_SEEN_ANNOUNCEMENTS)
        self._greeting_count = 0
        self._last_media_refresh_id: str | None = None
        self._last_media_grant_expires_at: datetime | None = None

    async def run(self) -> None:
        if self._run_entered:
            raise RtcSessionError("session_already_running")
        self._run_entered = True
        try:
            await self._connect()
            while not self._closed.is_set():
                source, value = await self._next_owned_event()
                if source == "closed":
                    break
                if source == "overrun":
                    await self._handle_queue_overrun()
                elif source == "control":
                    await self._handle_control(value)
                else:
                    await self._handle_rtc_event(value)
        except asyncio.CancelledError:
            await self._teardown(final_state="ended", reason="cancelled")
            raise
        except RtcSessionError as exc:
            self._media_state = "failed"
            if exc.reason != "notice_sink_failed":
                with suppress(RtcSessionError):
                    await self._emit(
                        SessionNotice(
                            "media_state",
                            reason=exc.reason,
                            metadata={"state": "failed"},
                        )
                    )
            await self._teardown(final_state="failed", reason=exc.reason)
            raise
        except Exception:
            self._media_state = "failed"
            with suppress(RtcSessionError):
                await self._emit(
                    SessionNotice(
                        "media_state",
                        reason="session_runtime_failed",
                        metadata={"state": "failed"},
                    )
                )
            await self._teardown(final_state="failed", reason="session_runtime_failed")
            raise RtcSessionError("session_runtime_failed") from None
        finally:
            teardown = self._teardown_task
            if teardown is not None and not teardown.done():
                await asyncio.shield(teardown)
            self.binding.clear_secrets()

    async def wait_started(self, timeout: float = 1.0) -> None:
        await asyncio.wait_for(asyncio.shield(self._started.wait()), timeout)

    async def close(self, reason: str) -> None:
        await self._teardown(final_state="ended", reason=reason)

    @property
    def context_synced(self) -> bool:
        return self._context_synced

    @property
    def capture_open(self) -> bool:
        return self._capture_open

    @property
    def capture_epoch(self) -> int:
        return self._capture_epoch

    @property
    def retained_audio_bytes(self) -> int:
        return len(self._utterance)

    @property
    def retained_final_count(self) -> int:
        return len(self._retained_finals)

    @property
    def retained_final_bytes(self) -> int:
        return self._retained_final_bytes

    @property
    def speech_epoch(self) -> int:
        return self._speech_epoch

    @property
    def output_track_sid(self) -> str | None:
        return self._output_track_sid

    @property
    def greeting_count(self) -> int:
        return self._greeting_count

    async def publish_transcript_envelope(self, envelope: Mapping[str, Any]) -> None:
        """Publish one already proof-bound final only to the assigned client.

        Proof construction and ``turn_bound`` ownership remain coordinator
        protocol responsibilities. This narrow media seam validates the full
        immutable binding, canonical text/digest shape, proof lifetime, packet
        ceiling, topic, reliability, and destination before touching LiveKit.
        It never copies transcript text onto the worker control channel.
        """

        required = {
            "type",
            "schema_version",
            "session_id",
            "generation",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
            "chat_context_revision",
            "media_grant_revision",
            "sequence",
            "final",
            "text",
            "detected_language",
            "text_digest_sha256",
            "transcript_proof",
            "proof_expires_at",
            "source_participant_identity",
        }
        if not isinstance(envelope, Mapping) or set(envelope) != required:
            raise ProtocolViolation("invalid_transcript_fields")
        if (
            envelope.get("type") != "voice_transcript"
            or envelope.get("schema_version") != "1"
        ):
            raise ProtocolViolation("invalid_transcript_type")
        for field_name in (
            "session_id",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
        ):
            _uuid4_value(envelope.get(field_name), "invalid_transcript_id")
        recognition_binding = self._recognition_bindings.get(
            str(envelope.get("client_turn_id"))
        )
        if recognition_binding is None:
            recognition_binding = self._recognition_binding
        if recognition_binding is None:
            raise ProtocolViolation("transcript_binding_mismatch")
        if (
            envelope["session_id"] != self.binding.session_id
            or envelope["generation"] != self.binding.generation
            or envelope["media_grant_revision"]
            != recognition_binding.media_grant_revision
            or envelope["chat_id"] != recognition_binding.visible_chat_id
            or envelope["chat_context_revision"]
            != recognition_binding.chat_context_revision
            or envelope["source_participant_identity"] != self.binding.worker_identity
            or envelope["client_turn_id"] != recognition_binding.client_turn_id
        ):
            raise ProtocolViolation("transcript_binding_mismatch")
        for name in ("turn_id", "submission_id", "request_generation"):
            expected = getattr(recognition_binding, name, None)
            if expected is not None and envelope[name] != expected:
                raise ProtocolViolation("transcript_binding_mismatch")
        if not _is_int_between(envelope["generation"], 1, 2**63 - 1) or not (
            _is_int_between(envelope["media_grant_revision"], 1, 2**63 - 1)
            and _is_int_between(envelope["chat_context_revision"], 1, 2**63 - 1)
            and _is_int_between(envelope["sequence"], 0, 2**63 - 1)
        ):
            raise ProtocolViolation("invalid_transcript_binding")
        if envelope["final"] is not True:
            raise ProtocolViolation("final_transcript_required")

        text = envelope["text"]
        if not isinstance(text, str) or not text:
            raise ProtocolViolation("empty_transcript")
        if len(text) > 8_000:
            raise ProtocolViolation("transcript_text_too_large")
        try:
            canonical = canonical_transcript(text)
        except TranscriptProofError as exc:
            raise ProtocolViolation(exc.code) from None
        if canonical != text:
            raise ProtocolViolation("noncanonical_transcript")
        try:
            encoded_text = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ProtocolViolation("invalid_transcript_text") from None
        digest = envelope["text_digest_sha256"]
        if (
            not isinstance(digest, str)
            or _LOWER_HEX_64.fullmatch(digest) is None
            or hashlib.sha256(encoded_text).hexdigest() != digest
        ):
            raise ProtocolViolation("transcript_digest_mismatch")
        proof = envelope["transcript_proof"]
        if not isinstance(proof, str) or _LOWER_HEX_64.fullmatch(proof) is None:
            raise ProtocolViolation("invalid_transcript_proof")
        language = envelope["detected_language"]
        if not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None:
            raise ProtocolViolation("invalid_transcript_language")
        expires_at_text = envelope["proof_expires_at"]
        expires_at = _parse_utc(expires_at_text)
        if expires_at_text != _format_utc(expires_at):
            raise ProtocolViolation("invalid_transcript_proof_expiry")
        now = self._aware_now()
        if expires_at <= now:
            raise ProtocolViolation("transcript_proof_expired")
        if expires_at > now + timedelta(minutes=2):
            raise ProtocolViolation("transcript_proof_lifetime_exceeded")

        try:
            payload = json.dumps(
                dict(envelope),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ProtocolViolation("invalid_transcript_envelope") from None
        if len(payload) > MAX_TRANSCRIPT_ENVELOPE_BYTES:
            raise ProtocolViolation("transcript_envelope_too_large")
        await self._publish_transcript_payload(payload)
        await self._emit(
            SessionNotice(
                "transcript_emitted",
                language=language,
                metadata={
                    "turn_id": envelope["turn_id"],
                    "client_turn_id": envelope["client_turn_id"],
                    "submission_id": envelope["submission_id"],
                    "request_generation": envelope["request_generation"],
                    "chat_id": envelope["chat_id"],
                    "chat_context_revision": envelope["chat_context_revision"],
                    "media_grant_revision": envelope["media_grant_revision"],
                    "final": True,
                    "utf8_bytes": len(encoded_text),
                    "text_digest_sha256": digest,
                    "proof_expires_at": expires_at_text,
                },
            )
        )

    async def _publish_transcript_payload(self, payload: bytes) -> None:
        """Deliver one validated envelope over the selected client transport."""

        if not self._room_connected or self._room is None:
            raise RtcSessionError("rtc_room_unavailable")
        try:
            await self._room.local_participant.publish_data(
                payload,
                reliable=True,
                destination_identities=[self.binding.client_participant_identity],
                topic=VOICE_TRANSCRIPT_TOPIC,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RtcSessionError("transcript_publish_failed") from None

    async def _publish_announcement_manifest(
        self,
        meta: _SpeechMeta,
        *,
        track_name: str,
        duration_samples: int,
    ) -> None:
        """Publish a content-free manifest before any PCM reaches the track."""

        if self._room is None or self._output_track_sid is None:
            raise RtcSessionError("rtc_room_unavailable")
        manifest: dict[str, Any] = {
            "type": "voice_announcement_media",
            "schema_version": "1",
            "session_id": self.binding.session_id,
            "generation": self.binding.generation,
            "media_grant_revision": self.binding.media_grant_revision,
            "announcement_id": meta.announcement_id,
            "announcement_sequence": meta.announcement_sequence,
            "transport": self.binding.transport,
            "worker_identity": self.binding.worker_identity,
            "turn_id": meta.turn_id,
            "kind": meta.kind,
            "quantum_role": meta.quantum_role,
            "quantum_index": meta.quantum_index,
            "track_sid": self._output_track_sid,
            "track_name": track_name,
            "duration_samples": duration_samples,
            "sample_rate_hz": OUTPUT_SAMPLE_RATE,
        }
        if meta.result_reserved_samples_after is not None:
            manifest["result_reserved_samples_after"] = (
                meta.result_reserved_samples_after
            )
        try:
            payload = json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise RtcSessionError("announcement_manifest_invalid") from None
        if len(payload) > MAX_ANNOUNCEMENT_ENVELOPE_BYTES:
            raise RtcSessionError("announcement_manifest_too_large")
        try:
            await self._room.local_participant.publish_data(
                payload,
                reliable=True,
                destination_identities=[self.binding.client_participant_identity],
                topic=VOICE_ANNOUNCEMENT_TOPIC,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RtcSessionError("announcement_manifest_publish_failed") from None

    async def _connect(self) -> None:
        room = self._rtc_factory.create_room()
        self._room = room
        for event_name in self._CALLBACK_EVENTS:
            room.on(event_name, self._callback(event_name))
        options = self._rtc_factory.room_options(
            auto_subscribe=False,
            connect_timeout=RTC_CONNECT_TIMEOUT_SECONDS,
        )
        grant = self.binding.worker_rtc_grant
        try:
            await room.connect(grant.livekit_url, grant.join_token, options)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RtcSessionError("rtc_connect_failed") from None
        finally:
            grant.clear_secrets()
        self._room_connected = True
        self._context_synced = True
        self._vad.reset()
        await self._reconcile_input()
        self._media_state = "ready"
        self._started.set()
        await self._emit(
            SessionNotice(
                "worker_ready",
                metadata={
                    "assignment_id": self.binding.assignment_id,
                    "worker_identity": self.binding.worker_identity,
                    "worker_rtc_grant_revision": (
                        self.binding.worker_rtc_grant_revision
                    ),
                    "profile_ready": True,
                },
            )
        )
        await self._emit(self._context_applied_notice())

    def _callback(self, event_name: str) -> Callable[..., None]:
        def enqueue_only(*args: Any) -> None:
            self._enqueue_rtc(_OwnedEvent(event_name, args))

        return enqueue_only

    def _enqueue_rtc(self, event: _OwnedEvent) -> None:
        if self._closed.is_set():
            _discard_owned_event(event)
            return
        try:
            self._rtc_events.put_nowait(event)
        except asyncio.QueueFull:
            _discard_owned_event(event)
            self._overrun_reason = (
                "audio_stream_overrun"
                if event.kind == "audio_frame"
                else "rtc_event_queue_overrun"
            )
            self._overrun_wakeup.set()

    async def _next_owned_event(self) -> tuple[str, Any]:
        # One persistent waiter per source, re-created only once it completes.
        # A value that becomes ready alongside another source stays parked in
        # its own finished task until the loop asks for it, so no waiter is
        # cancelled and re-created per event.
        if self._control_waiter is None:
            self._control_waiter = asyncio.create_task(self._queue.get())
        if self._rtc_waiter is None:
            self._rtc_waiter = asyncio.create_task(self._rtc_events.get())
        if self._overrun_waiter is None:
            self._overrun_waiter = asyncio.create_task(self._overrun_wakeup.wait())
        if self._closed_waiter is None:
            self._closed_waiter = asyncio.create_task(self._closed.wait())
        control_task = self._control_waiter
        rtc_task = self._rtc_waiter
        overrun_task = self._overrun_waiter
        closed_task = self._closed_waiter
        await asyncio.wait(
            (control_task, rtc_task, overrun_task, closed_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if closed_task.done():
            # Closed wins, and a value this call already recovered can no
            # longer reach the teardown drain: discard it here.
            if control_task.done():
                self._control_waiter = None
                _clear_buffered_value(_completed_waiter_value(control_task))
            if rtc_task.done():
                self._rtc_waiter = None
                event = _completed_waiter_value(rtc_task)
                if event is not None:
                    _discard_owned_event(event)
            return "closed", None
        if overrun_task.done():
            self._overrun_waiter = None
            self._overrun_wakeup.clear()
            return "overrun", None
        if control_task.done():
            self._control_waiter = None
            return "control", control_task.result()
        self._rtc_waiter = None
        return "rtc", rtc_task.result()

    async def _release_owned_waiters(self) -> None:
        """Cancel every persistent waiter, discarding any value one parked.

        A finished queue waiter holds its value outside the queue, so the
        teardown drain cannot reach it; zero retention requires this.
        """

        control = self._control_waiter
        rtc = self._rtc_waiter
        self._control_waiter = None
        self._rtc_waiter = None
        _clear_buffered_value(_completed_waiter_value(control))
        event = _completed_waiter_value(rtc)
        if event is not None:
            _discard_owned_event(event)
        waiters = (control, rtc, self._overrun_waiter, self._closed_waiter)
        self._overrun_waiter = None
        self._closed_waiter = None
        pending = [waiter for waiter in waiters if waiter is not None]
        for waiter in pending:
            waiter.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _handle_queue_overrun(self) -> None:
        reason = self._overrun_reason or "rtc_event_queue_overrun"
        self._overrun_reason = None
        await self._abort_utterance(reason)
        if reason != "audio_stream_overrun":
            raise RtcSessionError(reason)

    async def _handle_control(self, frame: dict[str, Any]) -> None:
        try:
            frame_type = frame.get("type")
            if frame_type == "set_capture":
                self._validate_frame_fence(frame)
                enabled = frame.get("enabled")
                if not isinstance(enabled, bool):
                    raise ProtocolViolation("invalid_capture_state")
                # A client-side VAD/barge-in request may arrive while output is
                # active. Fence and quiesce that output before capture can be
                # reopened so synthesized speech cannot become a user turn.
                if enabled and self._assistant_output_active():
                    await self._stop_speech("barge_in", emit=True)
                # Every set_capture command is coordinator-authenticated. A
                # false command is also allowed to retire the playout fence;
                # capture remains closed while lifecycle or microphone policy
                # is false, without leaking a timeout task.
                self._release_playout_capture_hold(guard_tail=enabled)
                self._capture_requested = enabled
                if not enabled:
                    await self._abort_utterance("capture_disabled", emit=False)
                self._update_capture_open()
                if enabled and not self._input_available():
                    await self._emit(
                        SessionNotice(
                            "capture_unavailable", reason="microphone_unavailable"
                        )
                    )
                elif enabled and self._capture_open:
                    await self._emit(
                        SessionNotice("media_state", metadata={"state": "listening"})
                    )
            elif frame_type == "speak":
                self._validate_frame_fence(frame)
                await self._start_speech(frame)
            elif frame_type == "stop_speech":
                self._validate_frame_fence(frame)
                await self._stop_speech(
                    str(frame.get("reason") or "user_stop"), emit=True
                )
            elif frame_type == "session_context_update":
                self._validate_frame_fence(frame)
                await self._context_update(frame)
            elif frame_type == "media_grant_rotated":
                await self._media_grant_rotated(frame)
            elif frame_type == "turn_bound":
                await self._turn_bound(frame)
            elif frame_type in {"transcript_accepted", "transcript_rejected"}:
                self._transcript_disposition(frame)
            elif frame_type == "end_session":
                self._validate_frame_fence(frame)
                await self.close(str(frame.get("reason") or "ended"))
        finally:
            _clear_buffered_value(frame)

    def _validate_frame_fence(self, frame: Mapping[str, Any]) -> None:
        if frame.get("session_id") != self.binding.session_id:
            raise ProtocolViolation("session_mismatch")
        if frame.get("generation") != self.binding.generation:
            raise ProtocolViolation("generation_mismatch")
        revision = frame.get("media_grant_revision")
        if revision is not None and revision != self.binding.media_grant_revision:
            raise ProtocolViolation("media_grant_revision_mismatch")

    async def _turn_bound(self, frame: Mapping[str, Any]) -> None:
        if (
            frame.get("session_id") != self.binding.session_id
            or frame.get("generation") != self.binding.generation
        ):
            raise ProtocolViolation("generation_mismatch")
        client_turn_id = _uuid4_value(
            frame.get("client_turn_id"),
            "invalid_client_turn_id",
        )
        recognition = self._recognition_bindings.get(client_turn_id)
        if recognition is None:
            raise ProtocolViolation("recognition_binding_unavailable")
        turn_id = _uuid4_value(frame.get("turn_id"), "invalid_turn_id")
        submission_id = _uuid4_value(
            frame.get("submission_id"),
            "invalid_submission_id",
        )
        request_generation = _uuid4_value(
            frame.get("request_generation"),
            "invalid_request_generation",
        )
        if (
            frame.get("media_grant_revision") != recognition.media_grant_revision
            or frame.get("chat_id") != recognition.visible_chat_id
            or frame.get("chat_context_revision") != recognition.chat_context_revision
        ):
            raise ProtocolViolation("turn_binding_mismatch")
        incoming = (turn_id, submission_id, request_generation)
        current = (
            recognition.turn_id,
            recognition.submission_id,
            recognition.request_generation,
        )
        if any(value is not None for value in current) and current != incoming:
            raise ProtocolViolation("turn_binding_conflict")
        recognition.turn_id = turn_id
        recognition.submission_id = submission_id
        recognition.request_generation = request_generation
        await self._publish_retained_final(client_turn_id)

    def _transcript_disposition(self, frame: Mapping[str, Any]) -> None:
        if (
            frame.get("session_id") != self.binding.session_id
            or frame.get("generation") != self.binding.generation
        ):
            raise ProtocolViolation("generation_mismatch")
        client_turn_id = _uuid4_value(
            frame.get("client_turn_id"),
            "invalid_client_turn_id",
        )
        signature = (
            str(frame.get("type")),
            client_turn_id,
            str(frame.get("turn_id")),
            str(frame.get("submission_id")),
            str(frame.get("request_generation")),
            str(frame.get("chat_id")),
            str(frame.get("media_grant_revision")),
        )
        recognition = self._recognition_bindings.get(client_turn_id)
        if recognition is None:
            if signature in self._seen_transcript_dispositions:
                return
            raise ProtocolViolation("transcript_disposition_mismatch")
        if (
            frame.get("turn_id") != recognition.turn_id
            or frame.get("submission_id") != recognition.submission_id
            or frame.get("request_generation") != recognition.request_generation
            or frame.get("chat_id") != recognition.visible_chat_id
            or frame.get("media_grant_revision") != recognition.media_grant_revision
        ):
            raise ProtocolViolation("transcript_disposition_mismatch")
        self._seen_transcript_dispositions.append(signature)
        self._clear_retained_final(client_turn_id)

    async def _context_update(self, frame: Mapping[str, Any]) -> None:
        chat_id = frame.get("visible_chat_id")
        revision = frame.get("chat_context_revision")
        if not isinstance(chat_id, str) or not _is_int_between(revision, 1, 2**63 - 1):
            raise ProtocolViolation("invalid_context_update")
        if revision < self.binding.chat_context_revision:
            raise ProtocolViolation("stale_context_revision")
        if (
            revision == self.binding.chat_context_revision
            and chat_id != self.binding.visible_chat_id
        ):
            raise ProtocolViolation("context_revision_conflict")
        if self._utterance and not self._utterance_active:
            await self._abort_utterance("context_changed", emit=False)
        self._context_synced = False
        self._update_capture_open()
        if self._recognizing or self._utterance_active:
            self._pending_context = (chat_id, revision)
            return
        await self._apply_context(chat_id, revision)

    async def _apply_context(self, chat_id: str, revision: int) -> None:
        self.binding.visible_chat_id = chat_id
        self.binding.chat_context_revision = revision
        self._pending_context = None
        self._context_synced = True
        became_listening = self._update_capture_open()
        await self._emit(self._context_applied_notice())
        if became_listening:
            await self._emit(
                SessionNotice("media_state", metadata={"state": "listening"})
            )

    async def _media_grant_rotated(self, frame: Mapping[str, Any]) -> None:
        if (
            frame.get("session_id") != self.binding.session_id
            or frame.get("generation") != self.binding.generation
        ):
            raise ProtocolViolation("generation_mismatch")
        previous = frame.get("previous_media_grant_revision")
        revision = frame.get("media_grant_revision")
        refresh_id = frame.get("refresh_id")
        identity = frame.get("client_participant_identity")
        expires_at = _parse_utc(frame.get("grant_expires_at"))
        if (
            revision == self.binding.media_grant_revision
            and previous == revision - 1
            and refresh_id == self._last_media_refresh_id
            and identity == self.binding.client_participant_identity
            and frame.get("transport") == self.binding.transport
            and expires_at == self._last_media_grant_expires_at
        ):
            await self._emit(
                SessionNotice(
                    "media_grant_applied",
                    metadata={
                        "refresh_id": refresh_id,
                        "media_grant_revision": revision,
                        "client_participant_identity": identity,
                    },
                )
            )
            return
        if (
            not _is_int_between(previous, 1, 2**63 - 1)
            or previous != self.binding.media_grant_revision
            or not _is_int_between(revision, previous + 1, 2**63 - 1)
            or not isinstance(refresh_id, str)
            or _UUID4.fullmatch(refresh_id) is None
            or not isinstance(identity, str)
            or not identity
            or frame.get("transport") != self.binding.transport
            or expires_at <= self._aware_now()
        ):
            raise ProtocolViolation("invalid_media_grant_rotation")
        self._fence_capture()
        await self._close_all_inputs()
        await self._abort_utterance("grant_rotated", emit=False)
        await self._stop_speech("stale", emit=True)
        self._release_playout_capture_hold()
        self.binding.media_grant_revision = revision
        self.binding.client_participant_identity = identity
        self.binding.grant_expires_at = expires_at
        self._last_media_refresh_id = refresh_id
        self._last_media_grant_expires_at = expires_at
        became_listening = await self._reconcile_input()
        await self._replay_retained_finals()
        await self._emit(
            SessionNotice(
                "media_grant_applied",
                metadata={
                    "refresh_id": refresh_id,
                    "media_grant_revision": revision,
                    "client_participant_identity": identity,
                },
            )
        )
        if became_listening:
            await self._emit(
                SessionNotice("media_state", metadata={"state": "listening"})
            )

    async def _handle_rtc_event(self, event: _OwnedEvent) -> None:
        kind = event.kind
        args = event.args
        if kind in {"participant_connected", "track_published"}:
            became_listening = await self._reconcile_input()
            await self._replay_retained_finals()
            if became_listening:
                await self._emit(
                    SessionNotice("media_state", metadata={"state": "listening"})
                )
        elif kind == "participant_disconnected":
            participant = args[0]
            if getattr(participant, "identity", None) == (
                self.binding.client_participant_identity
            ):
                await self._close_all_inputs()
                await self._abort_utterance("participant_disconnected")
                self._update_capture_open()
        elif kind == "track_subscribed":
            track, publication, participant = args
            if self._publication_authorized(participant, publication):
                await self._start_input(publication, track)
                became_listening = self._update_capture_open()
                await self._replay_retained_finals()
                if became_listening:
                    await self._emit(
                        SessionNotice(
                            "media_state",
                            metadata={"state": "listening"},
                        )
                    )
        elif kind in {"track_unpublished", "track_unsubscribed"}:
            publication = args[0] if kind == "track_unpublished" else args[1]
            participant = args[1] if kind == "track_unpublished" else args[2]
            sid = str(getattr(publication, "sid", ""))
            if (
                getattr(participant, "identity", None)
                == self.binding.client_participant_identity
                and sid in self._input_handles
            ):
                await self._close_input(sid)
                await self._abort_utterance("microphone_unavailable")
                self._update_capture_open()
        elif kind == "reconnecting":
            self._reconnecting = True
            self._fence_capture()
            self._release_playout_capture_hold()
            await self._close_all_inputs()
            await self._abort_utterance("reconnecting", emit=False)
            await self._stop_speech("reconnecting", emit=True)
            self._media_state = "reconnecting"
            await self._emit(
                SessionNotice(
                    "media_state",
                    reason="reconnecting",
                    metadata={"state": "reconnecting"},
                )
            )
        elif kind == "reconnected":
            await self._reconcile_input()
            self._reconnecting = False
            self._media_state = "ready"
            self._update_capture_open()
            await self._replay_retained_finals()
            if self._capture_open:
                await self._emit(
                    SessionNotice("media_state", metadata={"state": "listening"})
                )
        elif kind == "disconnected":
            raise RtcSessionError("rtc_disconnected")
        elif kind == "local_track_republished":
            publication, previous_sid = args
            if self._output_publication is publication and (
                self._output_track_sid == previous_sid
            ):
                self._output_track_sid = str(publication.sid)
        elif kind == "audio_frame":
            await self._handle_audio_frame(*args)
        elif kind == "audio_stream_failed":
            await self._abort_utterance("audio_stream_failed")
        elif kind == "recognition_complete":
            await self._recognition_complete(*args)
        elif kind == "transcript_expired":
            self._clear_retained_final(*args)
        elif kind == "synthesis_complete":
            await self._synthesis_complete(*args)
        elif kind == "speech_complete":
            await self._speech_complete(*args)
        elif kind == "client_playout_timeout":
            if (
                args == (self._playout_hold_epoch,)
                and self._playout_capture_hold
                and not self._playout_tail_guard
            ):
                raise RtcSessionError("client_playout_timeout")
        elif kind == "playout_tail_guard_elapsed":
            await self._finish_playout_tail_guard(*args)

    async def _reconcile_input(self) -> bool:
        room = self._room
        if room is None:
            return False
        expected = None
        for participant in room.remote_participants.values():
            if (
                getattr(participant, "identity", None)
                == self.binding.client_participant_identity
            ):
                expected = participant
                break
        candidates: list[Any] = []
        if expected is not None:
            for publication in expected.track_publications.values():
                if self._publication_authorized(expected, publication):
                    candidates.append(publication)
        candidates.sort(key=lambda item: str(getattr(item, "sid", "")))
        selected = candidates[:1]
        selected_sids = {str(publication.sid) for publication in selected}
        for sid in tuple(self._input_handles):
            if sid not in selected_sids:
                await self._close_input(sid)
        for publication in selected:
            sid = str(publication.sid)
            if sid not in self._subscribed_sids:
                publication.set_subscribed(True)
                self._subscribed_sids.add(sid)
                self._subscribed_publications[sid] = publication
            track = getattr(publication, "track", None)
            if track is not None:
                await self._start_input(publication, track)
        return self._update_capture_open()

    def _publication_authorized(self, participant: Any, publication: Any) -> bool:
        return (
            getattr(participant, "identity", None)
            == self.binding.client_participant_identity
            and self._rtc_factory.is_audio_publication(publication)
            and self._rtc_factory.is_microphone_publication(publication)
        )

    async def _start_input(self, publication: Any, track: Any) -> None:
        sid = str(publication.sid)
        current = self._input_handles.get(sid)
        if current is not None:
            return
        for other_sid in tuple(
            set(self._input_handles) | set(self._subscribed_publications)
        ):
            if other_sid != sid:
                await self._close_input(other_sid)
        try:
            stream = self._rtc_factory.create_audio_stream(
                track,
                capacity=AUDIO_STREAM_CAPACITY,
                sample_rate=AUDIO_STREAM_SAMPLE_RATE,
                num_channels=1,
                frame_size_ms=AUDIO_STREAM_FRAME_MS,
            )
        except Exception:
            raise RtcSessionError("audio_stream_create_failed") from None
        pump = asyncio.create_task(
            self._pump_input(sid, stream),
            name=f"voice-input-{self.binding.session_id}",
        )
        self._input_handles[sid] = _InputHandle(publication, stream, pump)

    async def _pump_input(self, sid: str, stream: Any) -> None:
        try:
            async for event in stream:
                pcm, rate, channels, samples = self._rtc_factory.decode_audio_event(
                    event
                )
                depth = self._rtc_factory.stream_buffer_depth(event, stream)
                self._enqueue_rtc(
                    _OwnedEvent(
                        "audio_frame",
                        (
                            sid,
                            pcm,
                            rate,
                            channels,
                            samples,
                            depth,
                            self._capture_epoch,
                        ),
                    )
                )
                if depth >= AUDIO_STREAM_CAPACITY:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._enqueue_rtc(_OwnedEvent("audio_stream_failed", (sid,)))

    async def _close_input(self, sid: str) -> None:
        handle = self._input_handles.pop(sid, None)
        self._subscribed_sids.discard(sid)
        publication = self._subscribed_publications.pop(sid, None)
        if publication is None and handle is not None:
            publication = handle.publication
        if publication is not None:
            with suppress(Exception):
                publication.set_subscribed(False)
        if handle is None:
            return
        handle.pump.cancel()
        await asyncio.gather(handle.pump, return_exceptions=True)
        with suppress(Exception):
            await asyncio.wait_for(handle.stream.aclose(), timeout=1.0)

    async def _close_all_inputs(self) -> None:
        for sid in tuple(
            set(self._input_handles) | set(self._subscribed_publications)
        ):
            await self._close_input(sid)

    def _update_capture_open(self) -> bool:
        previous = self._capture_open
        next_open = bool(
            self._capture_requested
            and (self._context_synced or self._utterance_active)
            and not self._reconnecting
            and self._input_available()
            and not self._assistant_output_active()
            and not self._playout_capture_hold
            and not self._recognizing
            and not self._closed.is_set()
        )
        if next_open != previous:
            self._capture_epoch += 1
        self._capture_open = next_open
        return not previous and next_open

    def _fence_capture(self) -> None:
        """Invalidate already-queued microphone frames and close ingestion."""

        self._capture_epoch += 1
        self._capture_open = False

    def _assistant_output_active(self) -> bool:
        return bool(
            self._speech_meta is not None
            or self._output_source is not None
            or self._synthesis_task is not None
            or self._speech_producer is not None
        )

    def _input_audio_admitted(self, source_id: str) -> bool:
        gate = self._input_audio_gate
        if gate is None:
            return True
        try:
            return gate(source_id) is True
        except Exception:
            logger.warning("voice_input_gate_failed reason=input_audio_gate_failed")
            return False

    def _input_available(self) -> bool:
        return bool(self._input_handles)

    def _input_source_authorized(self, source_id: str) -> bool:
        return source_id in self._input_handles

    async def _handle_audio_frame(
        self,
        sid: str,
        pcm: bytes,
        sample_rate: int,
        channels: int,
        samples: int,
        buffered_frames: int,
        capture_epoch: int | None = None,
    ) -> None:
        if (
            not self._input_source_authorized(sid)
            or not self._capture_open
            or self._assistant_output_active()
            or (capture_epoch is not None and capture_epoch != self._capture_epoch)
            or not self._input_audio_admitted(sid)
        ):
            return
        if buffered_frames >= AUDIO_STREAM_CAPACITY:
            await self._abort_utterance("audio_stream_overrun")
            return
        if (
            sample_rate != AUDIO_STREAM_SAMPLE_RATE
            or channels != 1
            or samples != AUDIO_FRAME_SAMPLES
            or len(pcm) != AUDIO_FRAME_SAMPLES * 2
        ):
            await self._abort_utterance("invalid_audio_frame")
            return
        try:
            probability = self._vad.probability(pcm)
        except Exception:
            await self._abort_utterance("vad_failed")
            return
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            await self._abort_utterance("vad_failed")
            return
        # Pre-roll ring maintenance for every ADMITTED, validated frame. A
        # capture-epoch change marks a fence transition (assistant playout,
        # reconnect), so the ring resets there and pre-fence audio can never
        # seed a later turn — exact fence semantics, no clock reads.
        if self._preroll_epoch != self._capture_epoch:
            self._vad_preroll.clear()
            self._preroll_epoch = self._capture_epoch
        self._vad_preroll.append(bytes(pcm))
        if not self._utterance_active:
            if probability >= VAD_THRESHOLD:
                self._utterance.extend(pcm)
                self._candidate_speech_frames += 1
            elif probability >= VAD_RELEASE_THRESHOLD:
                self._utterance.extend(pcm)
            else:
                self._utterance.clear()
                self._candidate_speech_frames = 0
                return
            candidate_frames = len(self._utterance) // (AUDIO_FRAME_SAMPLES * 2)
            if (
                self._candidate_speech_frames >= VAD_MIN_HIGH_CONFIDENCE_FRAMES
                and candidate_frames >= VAD_MIN_CANDIDATE_FRAMES
            ):
                if len(self._recognition_bindings) >= MAX_RETAINED_FINALS:
                    raise RtcSessionError("transcript_buffer_full")
                self._utterance_active = True
                # Voiced evidence accumulated during candidacy carries into
                # the utterance; the active branch below keeps counting.
                self._utterance_voiced_frames = self._candidate_speech_frames
                # Seed the turn from the pre-roll ring: it is a superset of
                # the candidate frames plus the onset audio the candidate
                # logic discarded on sub-release dips.
                self._utterance = bytearray(b"".join(self._vad_preroll))
                self._silence_frames = 0
                self._client_turn_id = str(uuid4())
                self._recognition_binding = _RecognitionBinding(
                    client_turn_id=self._client_turn_id,
                    media_grant_revision=self.binding.media_grant_revision,
                    visible_chat_id=self.binding.visible_chat_id,
                    chat_context_revision=self.binding.chat_context_revision,
                    echo_fingerprints=self._active_speech_fingerprints(),
                )
                self._recognition_bindings[self._recognition_binding.client_turn_id] = (
                    self._recognition_binding
                )
                recognition_metadata = self._recognition_metadata()
                await self._emit(
                    SessionNotice(
                        "recognition_started",
                        metadata=recognition_metadata,
                    )
                )
                await self._emit(SessionNotice("speech_detected"))
                await self._emit(
                    SessionNotice(
                        "media_state",
                        metadata={"state": "speech_detected"},
                    )
                )
            elif candidate_frames >= VAD_MAX_CANDIDATE_FRAMES:
                self._utterance.clear()
                self._candidate_speech_frames = 0
            return
        self._utterance.extend(pcm)
        if probability >= VAD_THRESHOLD:
            self._utterance_voiced_frames += 1
        if probability >= VAD_RELEASE_THRESHOLD:
            self._silence_frames = 0
        else:
            self._silence_frames += 1
        frame_count = len(self._utterance) // (AUDIO_FRAME_SAMPLES * 2)
        if (
            self._silence_frames >= VAD_END_SILENCE_FRAMES
            or frame_count >= MAX_UTTERANCE_FRAMES
        ):
            await self._finalize_utterance()

    async def _finalize_utterance(self) -> None:
        if not self._utterance_active or not self._utterance:
            await self._abort_utterance("empty_utterance", emit=False)
            return
        self._trim_trailing_silence()
        pcm = bytes(self._utterance)
        self._utterance.clear()
        if self._recognition_binding is not None:
            self._recognition_binding.voiced_frames = self._utterance_voiced_frames
        self._candidate_speech_frames = 0
        self._utterance_voiced_frames = 0
        self._utterance_active = False
        self._silence_frames = 0
        self._vad_preroll.clear()
        self._vad.reset()
        self._recognizing = True
        self._update_capture_open()
        await self._emit(
            SessionNotice("media_state", metadata={"state": "transcribing"})
        )
        self._recognition_task = asyncio.create_task(
            self._recognize(pcm),
            name=f"voice-asr-{self.binding.session_id}",
        )

    def _trim_trailing_silence(self) -> None:
        """Drop the proven trailing endpoint-silence run before batch ASR.

        ``_silence_frames`` already counts exactly the contiguous trailing
        below-release run (any speech-evidence frame resets it), so the VAD's
        existing per-frame verdicts identify the trim without re-analyzing
        audio. Internal clause pauses are bridged mid-utterance and are never
        trailing, so they stay intact. The max-length finalize path can also
        trim, bounded to at most the proven trailing run it arrived with
        (< the endpoint threshold, or it would have endpointed already). A
        bounded ASR_TAIL_SILENCE_FRAMES tail is kept for recognizer context.
        Fail closed: never trim into speech evidence or empty the buffer.
        """

        excess_frames = self._silence_frames - ASR_TAIL_SILENCE_FRAMES
        if excess_frames <= 0:
            return
        trim_bytes = excess_frames * AUDIO_FRAME_SAMPLES * 2
        if trim_bytes >= len(self._utterance):
            return
        del self._utterance[len(self._utterance) - trim_bytes :]

    async def _recognize(self, pcm: bytes) -> None:
        try:
            transcript = await self._asr.transcribe_pcm16(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_asr_failed reason=%s",
                _safe_adapter_failure_reason(exc),
            )
            self._enqueue_rtc(_OwnedEvent("recognition_complete", (None, "asr_failed")))
        else:
            self._enqueue_rtc(_OwnedEvent("recognition_complete", (transcript, None)))

    async def _recognition_complete(
        self, transcript: Any | None, reason: str | None
    ) -> None:
        self._recognition_task = None
        self._recognizing = False
        recognition = self._recognition_binding
        if reason is not None:
            if recognition is not None:
                await self._emit(
                    SessionNotice(
                        "recognition_failed",
                        reason="asr_failed",
                        metadata={"client_turn_id": recognition.client_turn_id},
                    )
                )
                self._discard_retained_final_content(recognition.client_turn_id)
        elif transcript is not None:
            text = getattr(transcript, "text", None)
            language = getattr(transcript, "language", None)
            if isinstance(text, str) and text.strip():
                try:
                    canonical = canonical_transcript(text)
                    if (
                        recognition is None
                        or not isinstance(language, str)
                        or _LANGUAGE.fullmatch(language) is None
                    ):
                        raise TranscriptProofError("invalid_transcript_language")
                    fingerprint = _speech_fingerprint(canonical)
                    if (
                        fingerprint is not None
                        and fingerprint in recognition.echo_fingerprints
                    ):
                        await self._emit(
                            SessionNotice(
                                "recognition_failed",
                                reason="self_speech",
                                metadata={"client_turn_id": recognition.client_turn_id},
                            )
                        )
                        self._clear_retained_final(recognition.client_turn_id)
                    elif (
                        _normalized_speech(canonical) in _ASR_STOCK_HALLUCINATIONS
                        and recognition.voiced_frames
                        < ASR_HALLUCINATION_MIN_VOICED_FRAMES
                    ):
                        # Stock phrase minted from speech-free audio: refuse
                        # it like self_speech (silently, no retry guidance)
                        # instead of submitting a turn the user never spoke.
                        await self._emit(
                            SessionNotice(
                                "recognition_failed",
                                reason="hallucinated_transcript",
                                metadata={"client_turn_id": recognition.client_turn_id},
                            )
                        )
                        self._clear_retained_final(recognition.client_turn_id)
                    else:
                        await self._retain_final(
                            recognition,
                            canonical,
                            language,
                        )
                except TranscriptProofError:
                    self._discard_retained_final_content(
                        "" if recognition is None else recognition.client_turn_id
                    )
                    if recognition is not None:
                        await self._emit(
                            SessionNotice(
                                "recognition_failed",
                                reason="invalid_asr_result",
                                metadata={"client_turn_id": recognition.client_turn_id},
                            )
                        )
            elif recognition is not None:
                await self._emit(
                    SessionNotice(
                        "recognition_failed",
                        reason="empty_transcript",
                        metadata={"client_turn_id": recognition.client_turn_id},
                    )
                )
                self._discard_retained_final_content(recognition.client_turn_id)
        elif recognition is not None:
            await self._emit(
                SessionNotice(
                    "recognition_failed",
                    reason="invalid_asr_result",
                    metadata={"client_turn_id": recognition.client_turn_id},
                )
            )
            self._discard_retained_final_content(recognition.client_turn_id)
        if self._pending_context is not None:
            await self._apply_context(*self._pending_context)
        self._client_turn_id = None
        self._recognition_binding = None
        self._update_capture_open()
        if self._capture_open:
            await self._emit(
                SessionNotice("media_state", metadata={"state": "listening"})
            )

    async def _retain_final(
        self,
        recognition: _RecognitionBinding,
        text: str,
        language: str,
    ) -> None:
        encoded_size = len(text.encode("utf-8", errors="strict"))
        existing = self._retained_finals.get(recognition.client_turn_id)
        if existing is not None:
            if existing.text != text or existing.language != language:
                raise TranscriptProofError("transcript_final_conflict")
            await self._publish_retained_final(recognition.client_turn_id)
            return
        if (
            len(self._retained_finals) >= MAX_RETAINED_FINALS
            or self._retained_final_bytes + encoded_size > MAX_RETAINED_FINAL_BYTES
        ):
            raise RtcSessionError("transcript_buffer_full")
        retained = _RetainedFinal(
            binding=recognition,
            text=text,
            language=language,
            finalized_at=self._aware_now(),
            envelope_bytes=encoded_size,
        )
        self._retained_finals[recognition.client_turn_id] = retained
        self._retained_final_bytes += encoded_size
        self._retention_expiry_tasks[recognition.client_turn_id] = asyncio.create_task(
            self._expire_retained_final(recognition.client_turn_id),
            name=(
                "voice-transcript-expiry-"
                f"{self.binding.session_id}-{recognition.client_turn_id}"
            ),
        )
        await self._publish_retained_final(recognition.client_turn_id)

    async def _publish_retained_final(self, client_turn_id: str) -> None:
        retained = self._retained_finals.get(client_turn_id)
        if retained is None:
            return
        recognition = retained.binding
        if (
            recognition.turn_id is None
            or recognition.submission_id is None
            or recognition.request_generation is None
        ):
            return
        if retained.envelope is None:
            try:
                proof_binding = TranscriptProofBinding(
                    session_id=self.binding.session_id,
                    generation=self.binding.generation,
                    media_grant_revision=recognition.media_grant_revision,
                    assignment_id=self.binding.assignment_id,
                    worker_identity=self.binding.worker_identity,
                    turn_id=recognition.turn_id,
                    client_turn_id=recognition.client_turn_id,
                    submission_id=recognition.submission_id,
                    request_generation=recognition.request_generation,
                    chat_id=recognition.visible_chat_id,
                    chat_context_revision=recognition.chat_context_revision,
                    detected_language=retained.language,
                )
                issued = issue_transcript_proof_with_key(
                    bytes(self._transcript_proof_key),
                    proof_binding,
                    retained.text,
                    now=retained.finalized_at,
                )
            except TranscriptProofError as exc:
                self._clear_retained_final(client_turn_id)
                raise RtcSessionError(exc.code) from None
            envelope = {
                "type": "voice_transcript",
                "schema_version": "1",
                "session_id": self.binding.session_id,
                "generation": self.binding.generation,
                "turn_id": recognition.turn_id,
                "client_turn_id": recognition.client_turn_id,
                "submission_id": recognition.submission_id,
                "request_generation": recognition.request_generation,
                "chat_id": recognition.visible_chat_id,
                "chat_context_revision": recognition.chat_context_revision,
                "media_grant_revision": recognition.media_grant_revision,
                "sequence": 0,
                "final": True,
                "text": issued.canonical_text,
                "detected_language": retained.language,
                "text_digest_sha256": issued.text_digest_sha256,
                "transcript_proof": issued.transcript_proof,
                "proof_expires_at": issued.proof_expires_at,
                "source_participant_identity": self.binding.worker_identity,
            }
            try:
                payload_size = len(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    ).encode("utf-8", errors="strict")
                )
            except (TypeError, ValueError, UnicodeEncodeError):
                self._clear_retained_final(client_turn_id)
                raise RtcSessionError("invalid_transcript_envelope") from None
            adjusted_total = (
                self._retained_final_bytes - retained.envelope_bytes + payload_size
            )
            if (
                payload_size > MAX_TRANSCRIPT_ENVELOPE_BYTES
                or adjusted_total > MAX_RETAINED_FINAL_BYTES
            ):
                self._clear_retained_final(client_turn_id)
                raise RtcSessionError("transcript_envelope_too_large")
            self._retained_final_bytes = adjusted_total
            retained.envelope_bytes = payload_size
            retained.envelope = envelope
        await self.publish_transcript_envelope(retained.envelope)

    async def _replay_retained_finals(self) -> None:
        for client_turn_id in tuple(self._retained_finals):
            await self._publish_retained_final(client_turn_id)

    async def _expire_retained_final(self, client_turn_id: str) -> None:
        retained = self._retained_finals.get(client_turn_id)
        if retained is None:
            return
        expires_at = retained.finalized_at + timedelta(minutes=2)
        delay = max(0.0, (expires_at - self._aware_now()).total_seconds())
        await asyncio.sleep(delay)
        self._enqueue_rtc(_OwnedEvent("transcript_expired", (client_turn_id,)))

    def _discard_retained_final_content(self, client_turn_id: str) -> None:
        retained = self._retained_finals.pop(client_turn_id, None)
        if retained is not None:
            self._retained_final_bytes = max(
                0,
                self._retained_final_bytes - retained.envelope_bytes,
            )
            retained.envelope = None
            retained.text = ""
        task = self._retention_expiry_tasks.pop(client_turn_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _clear_retained_final(self, client_turn_id: str) -> None:
        self._discard_retained_final_content(client_turn_id)
        self._recognition_bindings.pop(client_turn_id, None)
        if self._client_turn_id == client_turn_id:
            self._client_turn_id = None
        if (
            self._recognition_binding is not None
            and self._recognition_binding.client_turn_id == client_turn_id
        ):
            self._recognition_binding = None

    async def _abort_utterance(self, reason: str, *, emit: bool = True) -> None:
        had_audio = bool(self._utterance)
        self._utterance.clear()
        self._candidate_speech_frames = 0
        self._utterance_voiced_frames = 0
        self._utterance_active = False
        self._silence_frames = 0
        self._vad_preroll.clear()
        self._vad.reset()
        if not self._recognizing:
            if self._recognition_binding is not None:
                self._clear_retained_final(self._recognition_binding.client_turn_id)
            self._client_turn_id = None
            self._recognition_binding = None
        if emit and (had_audio or reason in {"audio_stream_overrun", "vad_failed"}):
            await self._emit(SessionNotice("utterance_aborted", reason=reason))

    async def _start_speech(self, frame: Mapping[str, Any]) -> None:
        validate_announcement_binding(frame)
        announcement_id = frame.get("announcement_id")
        text = frame.get("text")
        ceiling = frame.get("max_duration_samples")
        if (
            not isinstance(announcement_id, str)
            or not announcement_id
            or not isinstance(text, str)
            or not text.strip()
            or not _is_int_between(ceiling, 1, 96_000)
        ):
            raise ProtocolViolation("invalid_speak_command")
        if announcement_id in self._seen_announcements:
            return
        expires_at = _parse_utc(frame.get("expires_at"))
        if expires_at <= self._aware_now():
            raise ProtocolViolation("speak_command_expired")
        if self._reconnecting or not self._context_synced:
            await self._emit(
                SessionNotice(
                    "speech_failed",
                    reason="media_not_ready",
                    announcement_id=announcement_id,
                    metadata=self._speech_frame_metadata(frame),
                )
            )
            return
        await self._stop_speech("superseded", emit=True)
        # A replacement command is the only path that retires an older
        # published announcement without waiting for its client terminal.
        self._remember_playout_echo_fingerprint()
        await self._abort_utterance("assistant_speech", emit=False)
        self._seen_announcements.append(announcement_id)
        self._hold_capture_for_client_playout()
        self._announcement_manifest_published = False
        self._speech_epoch += 1
        epoch = self._speech_epoch
        self._speech_meta = _SpeechMeta(
            epoch=epoch,
            announcement_id=announcement_id,
            kind=str(frame["kind"]),
            text=text,
            max_duration_samples=ceiling,
            announcement_sequence=int(frame["announcement_sequence"]),
            turn_id=frame.get("turn_id"),
            quantum_role=str(frame["quantum_role"]),
            quantum_index=int(frame["quantum_index"]),
            result_reserved_samples_after=frame.get("result_reserved_samples_after"),
        )
        self._update_capture_open()
        self._synthesis_task = asyncio.create_task(
            self._synthesize(epoch, text, ceiling),
            name=f"voice-tts-{self.binding.session_id}",
        )

    async def _synthesize(self, epoch: int, text: str, ceiling: int) -> None:
        try:
            audio = await self._tts.synthesize(text, max_duration_samples=ceiling)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "voice_tts_failed reason=%s",
                _safe_adapter_failure_reason(exc),
            )
            self._enqueue_rtc(
                _OwnedEvent("synthesis_complete", (epoch, None, "tts_failed"))
            )
        else:
            self._enqueue_rtc(_OwnedEvent("synthesis_complete", (epoch, audio, None)))

    async def _synthesis_complete(
        self, epoch: int, audio: Any | None, reason: str | None
    ) -> None:
        meta = self._speech_meta
        if meta is None or meta.epoch != epoch or epoch != self._speech_epoch:
            return
        self._synthesis_task = None
        if reason is not None or audio is None:
            await self._speech_failed(reason or "tts_failed")
            return
        samples = getattr(audio, "samples", None)
        pcm_s16le = getattr(audio, "pcm_s16le", None)
        if (
            getattr(audio, "sample_rate", None) != OUTPUT_SAMPLE_RATE
            or getattr(audio, "channels", None) != 1
            or getattr(audio, "sample_width_bytes", None) != 2
            or not _is_int_between(samples, 1, meta.max_duration_samples)
            or not isinstance(pcm_s16le, bytes)
            or len(pcm_s16le) != samples * 2
        ):
            await self._speech_failed("invalid_synthesized_audio")
            return
        source: Any | None = None
        track_name = f"astraldeep.voice.{meta.announcement_id}"
        try:
            source = self._rtc_factory.create_audio_source(
                sample_rate=OUTPUT_SAMPLE_RATE,
                num_channels=1,
                queue_size_ms=OUTPUT_QUEUE_MS,
            )
            track = self._rtc_factory.create_local_audio_track(
                track_name,
                source,
            )
            if self._room is None:
                raise RtcSessionError("rtc_room_unavailable")
            publication = await self._room.local_participant.publish_track(
                track, self._rtc_factory.track_publish_options()
            )
        except Exception:
            if source is not None:
                with suppress(Exception):
                    await source.aclose()
            await self._speech_failed("output_publish_failed")
            return
        self._output_source = source
        self._output_publication = publication
        self._output_track_sid = str(publication.sid)
        meta.duration_ms = (samples * 1_000) // OUTPUT_SAMPLE_RATE
        try:
            await self._publish_announcement_manifest(
                meta,
                track_name=track_name,
                duration_samples=samples,
            )
        except asyncio.CancelledError:
            raise
        except RtcSessionError:
            await self._speech_failed("announcement_manifest_publish_failed")
            return
        self._announcement_manifest_published = True
        self._playout_echo_fingerprint = _speech_fingerprint(meta.text)
        await self._emit(self._speech_notice(meta, "speech_started"))
        self._speech_producer = asyncio.create_task(
            self._produce_speech(epoch, pcm_s16le, source),
            name=f"voice-output-{self.binding.session_id}",
        )

    async def _produce_speech(self, epoch: int, pcm_s16le: bytes, source: Any) -> None:
        try:
            frame_bytes = OUTPUT_FRAME_SAMPLES * 2
            for offset in range(0, len(pcm_s16le), frame_bytes):
                if epoch != self._speech_epoch:
                    return
                chunk = pcm_s16le[offset : offset + frame_bytes]
                await self._capture_output_chunk(epoch, chunk, source)
            # This is only a bounded SDK/source drain. Authenticated client
            # voice_playout_event frames remain the sole evidence that a user
            # device actually rendered any of these samples.
            await self._await_output_operation(
                source.wait_for_playout(),
                timeout=OUTPUT_DRAIN_TIMEOUT_SECONDS,
                reason="output_drain_timeout",
            )
        except asyncio.CancelledError:
            raise
        except RtcSessionError as exc:
            self._enqueue_rtc(_OwnedEvent("speech_complete", (epoch, exc.reason)))
        except Exception:
            self._enqueue_rtc(_OwnedEvent("speech_complete", (epoch, "output_failed")))
        else:
            self._enqueue_rtc(_OwnedEvent("speech_complete", (epoch, None)))

    async def _capture_output_chunk(
        self,
        epoch: int,
        chunk: bytes,
        source: Any,
    ) -> None:
        if epoch != self._speech_epoch or source is not self._output_source:
            return
        frame = self._rtc_factory.create_output_frame(
            chunk,
            sample_rate=OUTPUT_SAMPLE_RATE,
            num_channels=1,
        )
        await self._await_output_operation(
            source.capture_frame(frame),
            timeout=OUTPUT_OPERATION_TIMEOUT_SECONDS,
            reason="output_capture_timeout",
        )
        if epoch != self._speech_epoch or source is not self._output_source:
            with suppress(Exception):
                source.clear_queue()

    async def _speech_complete(self, epoch: int, reason: str | None) -> None:
        meta = self._speech_meta
        if meta is None or meta.epoch != epoch or epoch != self._speech_epoch:
            return
        self._speech_producer = None
        if reason is not None:
            await self._speech_failed(reason)
            return
        greeting = meta.kind == "greeting"
        await self._release_output(clear=False)
        self._speech_meta = None
        if greeting:
            self._greeting_count += 1
        self._schedule_playout_confirmation_timeout()
        self._announcement_manifest_published = False
        self._update_capture_open()
        await self._emit(self._speech_notice(meta, "speech_finished"))

    async def _speech_failed(self, reason: str) -> None:
        meta = self._speech_meta
        had_published_output = self._announcement_manifest_published
        await self._release_output(clear=True)
        self._speech_meta = None
        self._announcement_manifest_published = False
        if had_published_output:
            self._schedule_playout_confirmation_timeout()
        else:
            self._release_playout_capture_hold()
        self._update_capture_open()
        if meta is not None:
            await self._emit(self._speech_notice(meta, "speech_failed", reason=reason))
        if self._capture_open:
            await self._emit(
                SessionNotice("media_state", metadata={"state": "listening"})
            )

    async def _stop_speech(
        self,
        reason: str,
        *,
        emit: bool,
        advance_epoch: bool = True,
        fence_capture: bool = True,
    ) -> None:
        meta = self._speech_meta
        if (
            meta is None
            and self._output_source is None
            and self._synthesis_task is None
            and self._speech_producer is None
        ):
            # Source completion is not client playout completion. An explicit,
            # authenticated barge-in may therefore arrive after the source has
            # drained while its local-playout fence is still active. Retire
            # that fence immediately; ordinary user-stop/mute/end paths remain
            # proof-gated and retain the acoustic-tail behavior.
            if reason == "barge_in" and self._playout_capture_hold:
                self._fence_capture()
                self._release_playout_capture_hold()
                became_listening = self._update_capture_open()
                if became_listening:
                    await self._emit(
                        SessionNotice(
                            "media_state",
                            metadata={"state": "listening"},
                        )
                    )
            return
        # This fence is deliberately the first mutation: callbacks, source
        # captures, and already-queued microphone frames from the old speech
        # epoch become stale before any await or replacement work begins.
        if advance_epoch:
            self._speech_epoch += 1
        if fence_capture:
            self._fence_capture()
        source = self._output_source
        if source is not None:
            with suppress(Exception):
                source.clear_queue()
        tasks = [
            task
            for task in (self._synthesis_task, self._speech_producer)
            if task is not None
        ]
        await self._quiesce_output_tasks(tasks)
        self._synthesis_task = None
        self._speech_producer = None
        if source is not None:
            with suppress(Exception):
                source.clear_queue()
        if emit and meta is not None:
            await self._emit(
                self._speech_notice(meta, "speech_interrupted", reason=reason)
            )
        await self._release_output(clear=False, post_close_clear=True)
        self._speech_meta = None
        had_published_output = self._announcement_manifest_published
        self._announcement_manifest_published = False
        if reason == "barge_in" or not had_published_output:
            self._release_playout_capture_hold()
        else:
            self._schedule_playout_confirmation_timeout()
        self._update_capture_open()

    def _hold_capture_for_client_playout(self) -> None:
        """Keep ASR closed until the coordinator proves local playout ended."""

        confirmation_task = self._playout_confirmation_task
        self._playout_confirmation_task = None
        if (
            confirmation_task is not None
            and confirmation_task is not asyncio.current_task()
        ):
            confirmation_task.cancel()
        tail_task = self._playout_tail_task
        self._playout_tail_task = None
        if tail_task is not None and tail_task is not asyncio.current_task():
            tail_task.cancel()
        self._playout_hold_epoch += 1
        self._playout_tail_guard = False
        self._playout_capture_hold = True

    def _release_playout_capture_hold(self, *, guard_tail: bool = False) -> None:
        """Release one local-playout fence, optionally after an acoustic tail."""

        self._remember_playout_echo_fingerprint()
        confirmation_task = self._playout_confirmation_task
        self._playout_confirmation_task = None
        if (
            confirmation_task is not None
            and confirmation_task is not asyncio.current_task()
        ):
            confirmation_task.cancel()
        if guard_tail and self._playout_capture_hold:
            if self._playout_tail_guard:
                return
            self._playout_tail_guard = True
            epoch = self._playout_hold_epoch
            self._playout_tail_task = asyncio.create_task(
                self._wait_for_playout_tail_guard(epoch),
                name=f"voice-playout-tail-{self.binding.session_id}",
            )
            return
        self._playout_hold_epoch += 1
        self._playout_tail_guard = False
        self._playout_capture_hold = False
        tail_task = self._playout_tail_task
        self._playout_tail_task = None
        if tail_task is not None and tail_task is not asyncio.current_task():
            tail_task.cancel()

    def _remember_playout_echo_fingerprint(self) -> None:
        fingerprint = self._playout_echo_fingerprint
        self._playout_echo_fingerprint = None
        if fingerprint is None:
            return
        now = self._monotonic()
        self._prune_speech_fingerprints(now)
        self._recent_speech_fingerprints.append(
            _RecentSpeechFingerprint(
                digest=fingerprint,
                expires_at=now + SELF_SPEECH_SUPPRESSION_WINDOW_SECONDS,
            )
        )

    def _active_speech_fingerprints(self) -> frozenset[bytes]:
        now = self._monotonic()
        self._prune_speech_fingerprints(now)
        return frozenset(item.digest for item in self._recent_speech_fingerprints)

    def _prune_speech_fingerprints(self, now: float) -> None:
        while (
            self._recent_speech_fingerprints
            and self._recent_speech_fingerprints[0].expires_at <= now
        ):
            self._recent_speech_fingerprints.popleft()

    async def _wait_for_playout_tail_guard(self, epoch: int) -> None:
        try:
            await asyncio.sleep(POST_PLAYOUT_CAPTURE_GUARD_SECONDS)
        except asyncio.CancelledError:
            raise
        self._enqueue_rtc(_OwnedEvent("playout_tail_guard_elapsed", (epoch,)))

    async def _finish_playout_tail_guard(self, epoch: int) -> None:
        if (
            epoch != self._playout_hold_epoch
            or not self._playout_capture_hold
            or not self._playout_tail_guard
        ):
            return
        self._playout_tail_task = None
        self._playout_tail_guard = False
        self._playout_capture_hold = False
        became_listening = self._update_capture_open()
        if became_listening:
            await self._emit(
                SessionNotice("media_state", metadata={"state": "listening"})
            )

    def _schedule_playout_confirmation_timeout(self) -> None:
        """Fail closed when published output lacks a client terminal event."""

        if not self._playout_capture_hold or self._closed.is_set():
            return
        task = self._playout_confirmation_task
        if task is not None and not task.done():
            return
        epoch = self._playout_hold_epoch
        self._playout_confirmation_task = asyncio.create_task(
            self._wait_for_playout_confirmation(epoch),
            name=f"voice-playout-confirmation-{self.binding.session_id}",
        )

    async def _wait_for_playout_confirmation(self, epoch: int) -> None:
        try:
            await asyncio.sleep(CLIENT_PLAYOUT_CONFIRMATION_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        self._enqueue_rtc(_OwnedEvent("client_playout_timeout", (epoch,)))

    async def _release_output(
        self,
        *,
        clear: bool,
        post_close_clear: bool = False,
    ) -> None:
        source = self._output_source
        sid = self._output_track_sid
        self._output_source = None
        self._output_publication = None
        self._output_track_sid = None
        if source is not None and clear:
            with suppress(Exception):
                source.clear_queue()
        if sid is not None and self._room is not None:
            await self._bounded_output_cleanup(
                self._room.local_participant.unpublish_track(sid)
            )
        if source is not None:
            await self._bounded_output_cleanup(source.aclose())
        await self._drain_retired_output_tasks()
        if source is not None and (clear or post_close_clear):
            # A cancellation-resistant capture may finish only after aclose()
            # releases it. This final clear discards that last stale frame.
            with suppress(Exception):
                source.clear_queue()

    async def _await_output_operation(
        self,
        operation: Any,
        *,
        timeout: float,
        reason: str,
        failure_reason: str | None = None,
    ) -> None:
        task = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            self._retire_output_task(task)
            raise
        if task not in done:
            task.cancel()
            self._retire_output_task(task)
            raise RtcSessionError(reason)
        try:
            await task
        except asyncio.CancelledError:
            raise RtcSessionError(reason) from None
        except RtcSessionError:
            raise
        except Exception:
            if failure_reason is None:
                raise
            raise RtcSessionError(failure_reason) from None

    async def _bounded_output_cleanup(self, operation: Any) -> None:
        with suppress(RtcSessionError):
            await self._await_output_operation(
                operation,
                timeout=SPEECH_QUIESCE_TIMEOUT_SECONDS,
                reason="output_release_timeout",
                failure_reason="output_release_failed",
            )

    async def _quiesce_output_tasks(
        self,
        tasks: list[asyncio.Task[None]],
    ) -> None:
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        done, pending = await asyncio.wait(
            tasks,
            timeout=SPEECH_QUIESCE_TIMEOUT_SECONDS,
        )
        for task in done:
            if not task.cancelled():
                with suppress(Exception):
                    task.exception()
        for task in pending:
            self._retire_output_task(task)

    def _retire_output_task(self, task: asyncio.Task[Any]) -> None:
        self._retired_output_tasks.add(task)
        task.add_done_callback(self._retired_output_task_done)

    def _retired_output_task_done(self, task: asyncio.Task[Any]) -> None:
        self._retired_output_tasks.discard(task)
        if not task.cancelled():
            with suppress(Exception):
                task.exception()

    async def _drain_retired_output_tasks(self) -> None:
        pending = tuple(task for task in self._retired_output_tasks if not task.done())
        if pending:
            await asyncio.wait(
                pending,
                timeout=SPEECH_QUIESCE_TIMEOUT_SECONDS,
            )

    async def _cancel_retired_output_tasks(self) -> None:
        """Drop references to cancellation-resistant output work boundedly."""

        tasks = tuple(self._retired_output_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(
                tasks,
                timeout=SPEECH_QUIESCE_TIMEOUT_SECONDS,
            )
        for task in tasks:
            if task.done() and not task.cancelled():
                with suppress(Exception):
                    task.exception()
        self._retired_output_tasks.difference_update(tasks)

    def _context_applied_notice(self) -> SessionNotice:
        return SessionNotice(
            "session_context_applied",
            metadata={
                "media_grant_revision": self.binding.media_grant_revision,
                "visible_chat_id": self.binding.visible_chat_id,
                "chat_context_revision": self.binding.chat_context_revision,
            },
        )

    def _recognition_metadata(self) -> dict[str, Any]:
        binding = self._recognition_binding
        if binding is None:
            raise RtcSessionError("recognition_binding_unavailable")
        return {
            "client_turn_id": binding.client_turn_id,
            "media_grant_revision": binding.media_grant_revision,
            "visible_chat_id": binding.visible_chat_id,
            "chat_context_revision": binding.chat_context_revision,
        }

    def _speech_frame_metadata(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        metadata = {
            "announcement_sequence": frame.get("announcement_sequence"),
            "media_grant_revision": self.binding.media_grant_revision,
            "turn_id": frame.get("turn_id"),
            "kind": frame.get("kind"),
            "quantum_role": frame.get("quantum_role"),
            "quantum_index": frame.get("quantum_index"),
        }
        reserved = frame.get("result_reserved_samples_after")
        if reserved is not None:
            metadata["result_reserved_samples_after"] = reserved
        return metadata

    def _speech_notice(
        self,
        meta: _SpeechMeta,
        kind: str,
        *,
        reason: str | None = None,
    ) -> SessionNotice:
        metadata: dict[str, Any] = {
            "announcement_sequence": meta.announcement_sequence,
            "media_grant_revision": self.binding.media_grant_revision,
            "turn_id": meta.turn_id,
            "kind": meta.kind,
            "quantum_role": meta.quantum_role,
            "quantum_index": meta.quantum_index,
        }
        if meta.result_reserved_samples_after is not None:
            metadata["result_reserved_samples_after"] = (
                meta.result_reserved_samples_after
            )
        if kind == "speech_finished" and meta.duration_ms is not None:
            metadata["duration_ms"] = meta.duration_ms
        return SessionNotice(
            kind,
            reason=reason,
            announcement_id=meta.announcement_id,
            metadata=metadata,
        )

    def _aware_now(self) -> datetime:
        value = self._utcnow()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RtcSessionError("worker_clock_invalid")
        return value.astimezone(UTC)

    async def _emit(self, notice: SessionNotice) -> None:
        try:
            result = self._notice_sink(notice)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RtcSessionError("notice_sink_failed") from None

    async def _teardown(self, *, final_state: str, reason: str) -> None:
        async with self._teardown_lock:
            if self._teardown_complete:
                return
            task = self._teardown_task
            if task is None:
                # Fence both ingress and egress before the first await. Every
                # queued callback/frame from the prior lifecycle is now stale,
                # even when the caller itself is cancelled during cleanup.
                self._closed.set()
                self._fence_capture()
                self._release_playout_capture_hold()
                self._speech_epoch += 1
                task = asyncio.create_task(
                    self._teardown_owned(final_state=final_state, reason=reason),
                    name=f"voice-teardown-{self.binding.session_id}",
                )
                self._teardown_task = task
        await asyncio.shield(task)

    async def _teardown_owned(self, *, final_state: str, reason: str) -> None:
        """Own every bounded cleanup primitive for one terminal lifecycle."""

        del reason
        try:
            with suppress(Exception):
                await self._stop_speech(
                    "session_end",
                    emit=False,
                    advance_epoch=False,
                    fence_capture=False,
                )
            recognition = self._recognition_task
            self._recognition_task = None
            if recognition is not None:
                recognition.cancel()
                await asyncio.gather(recognition, return_exceptions=True)
            with suppress(Exception):
                await self._close_all_inputs()
            with suppress(Exception):
                await self._abort_utterance("session_end", emit=False)
            expiry_tasks = tuple(self._retention_expiry_tasks.values())
            for client_turn_id in tuple(self._retained_finals):
                self._clear_retained_final(client_turn_id)
            for task in expiry_tasks:
                task.cancel()
            if expiry_tasks:
                await asyncio.gather(*expiry_tasks, return_exceptions=True)
            self._recognition_bindings.clear()
            self._retention_expiry_tasks.clear()
            self._retained_final_bytes = 0
            self._seen_transcript_dispositions.clear()
            self._seen_announcements.clear()
            self._playout_echo_fingerprint = None
            self._recent_speech_fingerprints.clear()
            for index in range(len(self._transcript_proof_key)):
                self._transcript_proof_key[index] = 0
            self._client_turn_id = None
            self._recognition_binding = None
            self._pending_context = None
            self._overrun_reason = None
            self._overrun_wakeup.clear()
            # Release before draining so no surviving waiter can park a value
            # the drain has already passed.
            await self._release_owned_waiters()
            while not self._queue.empty():
                with suppress(asyncio.QueueEmpty):
                    _clear_buffered_value(self._queue.get_nowait())
            while not self._rtc_events.empty():
                with suppress(asyncio.QueueEmpty):
                    _discard_owned_event(self._rtc_events.get_nowait())
            await self._cancel_retired_output_tasks()
            room = self._room
            self._room = None
            if room is not None:
                with suppress(Exception):
                    await asyncio.wait_for(room.disconnect(), timeout=2.0)
            self._room_connected = False
            self._subscribed_sids.clear()
            self._subscribed_publications.clear()
        finally:
            self._closed.set()
            for index in range(len(self._transcript_proof_key)):
                self._transcript_proof_key[index] = 0
            self.binding.clear_secrets()
            self._fence_capture()
            self._media_state = final_state
            self._started.set()
            self._teardown_complete = True


@dataclass(slots=True)
class _SessionEntry:
    binding: SessionBinding
    runtime: SessionRuntime
    task: asyncio.Task[None]
    control_media_grant_revision: int


@dataclass(frozen=True, slots=True)
class _ClosedSessionFence:
    generation: int
    media_grant_revision: int
    worker_rtc_grant_revision: int
    reconnect_key: (
        tuple[str, int, str, str, str, str, int, str, str, int, str] | None
    )
    media_state: str


SessionFactory = Callable[[SessionBinding], SessionRuntime]


class SessionSupervisor:
    """Multiplex assignments without exceeding capacity or retaining secrets."""

    def __init__(
        self,
        *,
        max_sessions: int,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if not 1 <= max_sessions <= 100:
            raise ValueError("invalid_max_sessions")
        self._advertised_max_sessions = max_sessions
        self._max_sessions = max_sessions
        self._session_factory = session_factory or BoundControlSession
        self._entries: dict[str, _SessionEntry] = {}
        self._closed_fences: dict[str, _ClosedSessionFence] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_complete = asyncio.Event()
        self._shutdown_complete.set()

    @property
    def active_count(self) -> int:
        return len(self._entries)

    def session_states(self) -> tuple[tuple[str, int, str], ...]:
        """Return a bearer-free snapshot for bounded heartbeat generation."""

        return tuple(
            (
                entry.binding.session_id,
                entry.binding.generation,
                entry.runtime.media_state,
            )
            for entry in self._entries.values()
        )

    def retained_sequence_fences(self) -> frozenset[tuple[str, int]]:
        """Return active and bounded closed fences for transport pruning."""

        active = {
            (entry.binding.session_id, entry.binding.generation)
            for entry in self._entries.values()
        }
        closed = {
            (session_id, fence.generation)
            for session_id, fence in self._closed_fences.items()
        }
        return frozenset(active | closed)

    def watch_session(
        self,
        *,
        session_id: str,
        generation: int,
        media_grant_revision: int,
    ) -> Any:
        """Return only an exact current Watch bridge assignment."""

        entry = self._entries.get(session_id)
        if (
            entry is None
            or entry.binding.transport != "watch_pcm_websocket"
            or entry.binding.generation != generation
            or entry.control_media_grant_revision != media_grant_revision
            or not callable(getattr(entry.runtime, "attach_bridge", None))
            or not callable(getattr(entry.runtime, "feed_microphone_frame", None))
        ):
            raise ProtocolViolation("stale_watch_assignment")
        return entry.runtime

    async def set_capacity(self, accepted_max_sessions: int) -> None:
        """Apply the coordinator's no-greater-than-advertised capacity."""

        if not 1 <= accepted_max_sessions <= self._advertised_max_sessions:
            raise ProtocolViolation("invalid_accepted_capacity")
        async with self._lock:
            if self._entries:
                raise ProtocolViolation("capacity_changed_after_assignment")
            self._max_sessions = accepted_max_sessions

    async def start(self, binding: SessionBinding) -> bool:
        """Start, retry, or replace one exactly fenced RTC assignment."""

        async with self._lock:
            if self._shutting_down:
                binding.clear_secrets()
                raise CapacityExceeded("worker_shutting_down")
            existing = self._entries.get(binding.session_id)
            if existing is not None:
                if existing.binding.idempotency_key == binding.idempotency_key:
                    if existing.binding.reconnect_key != binding.reconnect_key:
                        binding.clear_secrets()
                        raise AssignmentConflict("assignment_conflict")
                    binding.clear_secrets()
                    return False
                if not self._valid_worker_grant_reconnect(
                    existing.binding,
                    binding,
                ):
                    binding.clear_secrets()
                    raise AssignmentConflict("assignment_conflict")
                del self._entries[binding.session_id]
                self._remember_closed(existing)
                existing.binding.clear_secrets()
                await _close_entries([existing], "worker_grant_replaced")
            closed = self._closed_fences.get(binding.session_id)
            if closed is not None:
                if closed.generation > binding.generation:
                    binding.clear_secrets()
                    raise AssignmentConflict("stale_assignment")
                if closed.generation == binding.generation:
                    if (
                        binding.worker_rtc_grant_revision
                        <= closed.worker_rtc_grant_revision
                    ):
                        binding.clear_secrets()
                        raise AssignmentConflict("stale_assignment")
                    if binding.reconnect_key != closed.reconnect_key:
                        binding.clear_secrets()
                        raise AssignmentConflict("assignment_conflict")
            if len(self._entries) >= self._max_sessions:
                binding.clear_secrets()
                raise CapacityExceeded("capacity_exceeded")
            try:
                runtime = self._session_factory(binding)
                task = asyncio.create_task(
                    runtime.run(),
                    name=f"voice-session-{binding.session_id}",
                )
            except BaseException:
                binding.clear_secrets()
                raise
            entry = _SessionEntry(
                binding=binding,
                runtime=runtime,
                task=task,
                control_media_grant_revision=binding.media_grant_revision,
            )
            self._entries[binding.session_id] = entry
            if closed is not None and closed.generation == binding.generation:
                self._closed_fences.pop(binding.session_id, None)
            task.add_done_callback(
                lambda completed, session_id=binding.session_id: (
                    self._session_completed(session_id, completed)
                )
            )
            return True

    def deliver(self, frame: dict[str, Any]) -> None:
        """Deliver only to the currently bound generation and grant revision."""

        session_id = frame.get("session_id")
        entry = self._entries.get(session_id)
        if entry is None:
            closed = self._closed_fences.get(session_id)
            if closed is None:
                raise ProtocolViolation("unknown_session")
            if closed.generation != frame.get("generation"):
                raise ProtocolViolation("generation_mismatch")
            if closed.media_grant_revision != frame.get("media_grant_revision"):
                raise ProtocolViolation("media_grant_revision_mismatch")
            raise ClosedSessionRace(
                session_id=session_id,
                generation=closed.generation,
                media_grant_revision=closed.media_grant_revision,
                media_state=closed.media_state,
            )
        if frame.get("generation") != entry.binding.generation:
            raise ProtocolViolation("generation_mismatch")
        transport = frame.get("transport")
        if transport is not None and transport != entry.binding.transport:
            raise ProtocolViolation("transport_mismatch")
        rotated_revision: int | None = None
        if frame.get("type") == "media_grant_rotated":
            previous = frame.get("previous_media_grant_revision")
            revision = frame.get("media_grant_revision")
            expected_revision = entry.control_media_grant_revision + 1
            if previous == entry.control_media_grant_revision and _is_int_between(
                revision,
                expected_revision,
                expected_revision,
            ):
                rotated_revision = revision
            elif not (
                revision == entry.control_media_grant_revision
                and previous == revision - 1
            ):
                raise ProtocolViolation("media_grant_revision_mismatch")
        elif frame.get("type") not in {
            "turn_bound",
            "transcript_accepted",
            "transcript_rejected",
        }:
            revision = frame.get("media_grant_revision")
            if revision is not None and revision != entry.control_media_grant_revision:
                raise ProtocolViolation("media_grant_revision_mismatch")
        try:
            entry.runtime.deliver(frame)
        except asyncio.QueueFull as exc:
            raise ProtocolViolation("session_queue_full") from exc
        if rotated_revision is not None:
            entry.control_media_grant_revision = rotated_revision

    async def end(
        self,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        reason: str,
    ) -> None:
        """End exactly one current assignment after all equality checks."""

        async with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                closed = self._closed_fences.get(session_id)
                if closed is not None:
                    if closed.generation != generation:
                        raise ProtocolViolation("generation_mismatch")
                    if closed.media_grant_revision != media_grant_revision:
                        raise ProtocolViolation("media_grant_revision_mismatch")
                    return
                raise ProtocolViolation("unknown_session")
            if generation != entry.binding.generation:
                raise ProtocolViolation("generation_mismatch")
            if media_grant_revision != entry.control_media_grant_revision:
                raise ProtocolViolation("media_grant_revision_mismatch")
            del self._entries[session_id]
            self._remember_closed(entry)
        await _close_entries([entry], reason)

    async def reject_control_frame(
        self,
        *,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        allow_unbound: bool,
    ) -> ClosedSessionRace | None:
        """Quarantine one attributable control failure without stopping peers.

        A non-bind command is isolatable only when its session and generation
        exactly match an active or bounded closed fence.  A syntactically
        attributable bind may establish a failed fence even when capacity was
        never available.  This keeps the multiplexed pool alive while making
        the rejected assignment terminal and bounding its sequence state.
        """

        entry: _SessionEntry | None = None
        race_revision = media_grant_revision
        async with self._lock:
            current = self._entries.get(session_id)
            if current is not None:
                if current.binding.generation != generation:
                    if not allow_unbound:
                        return None
                    if generation < current.binding.generation:
                        return ClosedSessionRace(
                            session_id=session_id,
                            generation=generation,
                            media_grant_revision=media_grant_revision,
                            media_state="failed",
                        )
                    entry = current
                    del self._entries[session_id]
                    self._remember_closed(entry, media_state="failed")
                    self._remember_rejected(
                        session_id=session_id,
                        generation=generation,
                        media_grant_revision=media_grant_revision,
                    )
                else:
                    entry = current
                    del self._entries[session_id]
                    self._remember_closed(entry, media_state="failed")
                    race_revision = entry.control_media_grant_revision
            else:
                closed = self._closed_fences.get(session_id)
                if closed is not None and closed.generation == generation:
                    return ClosedSessionRace(
                        session_id=session_id,
                        generation=generation,
                        media_grant_revision=closed.media_grant_revision,
                        media_state=closed.media_state,
                    )
                if not allow_unbound:
                    return None
                self._remember_rejected(
                    session_id=session_id,
                    generation=generation,
                    media_grant_revision=media_grant_revision,
                )
            fence = self._closed_fences[session_id]
            if fence.generation == generation:
                race_revision = fence.media_grant_revision

        if entry is not None:
            await _close_entries([entry], "control_protocol_error")
        return ClosedSessionRace(
            session_id=session_id,
            generation=generation,
            media_grant_revision=race_revision,
            media_state="failed",
        )

    async def shutdown(self, reason: str) -> None:
        """Bound cleanup time and erase all queued bearer/text references."""

        async with self._lock:
            if self._shutting_down:
                shutdown_complete = self._shutdown_complete
                entries: list[_SessionEntry] | None = None
            else:
                self._shutting_down = True
                self._shutdown_complete.clear()
                shutdown_complete = self._shutdown_complete
                entries = list(self._entries.values())
                self._entries.clear()
                for entry in entries:
                    self._remember_closed(entry)
        if entries is None:
            await shutdown_complete.wait()
            return
        for entry in entries:
            entry.binding.clear_secrets()
        try:
            await _close_entries(entries, reason)
        finally:
            async with self._lock:
                self._max_sessions = self._advertised_max_sessions
                self._shutting_down = False
                self._shutdown_complete.set()

    def _session_completed(
        self, session_id: str, completed: asyncio.Task[None]
    ) -> None:
        entry = self._entries.get(session_id)
        if entry is not None and entry.task is completed:
            del self._entries[session_id]
            self._remember_closed(entry)
            entry.binding.clear_secrets()
        if not completed.cancelled():
            completed.exception()

    def _remember_closed(
        self,
        entry: _SessionEntry,
        *,
        media_state: str | None = None,
    ) -> None:
        current = self._closed_fences.get(entry.binding.session_id)
        if current is None or current.generation <= entry.binding.generation:
            self._closed_fences.pop(entry.binding.session_id, None)
            resolved_state = media_state or entry.runtime.media_state
            if resolved_state not in {"failed", "ended"}:
                resolved_state = "ended"
            self._closed_fences[entry.binding.session_id] = _ClosedSessionFence(
                generation=entry.binding.generation,
                media_grant_revision=entry.control_media_grant_revision,
                worker_rtc_grant_revision=entry.binding.worker_rtc_grant_revision,
                reconnect_key=entry.binding.reconnect_key,
                media_state=resolved_state,
            )
        self._trim_closed_fences()

    def _remember_rejected(
        self,
        *,
        session_id: str,
        generation: int,
        media_grant_revision: int,
    ) -> None:
        current = self._closed_fences.get(session_id)
        if current is None or current.generation <= generation:
            self._closed_fences.pop(session_id, None)
            self._closed_fences[session_id] = _ClosedSessionFence(
                generation=generation,
                media_grant_revision=media_grant_revision,
                worker_rtc_grant_revision=0,
                reconnect_key=None,
                media_state="failed",
            )
        self._trim_closed_fences()

    def _trim_closed_fences(self) -> None:
        while len(self._closed_fences) > MAX_CLOSED_SESSION_FENCES:
            oldest = next(iter(self._closed_fences))
            del self._closed_fences[oldest]

    @staticmethod
    def _valid_worker_grant_reconnect(
        current: SessionBinding,
        requested: SessionBinding,
    ) -> bool:
        return (
            requested.reconnect_key == current.reconnect_key
            and requested.worker_rtc_grant_revision
            > current.worker_rtc_grant_revision
        )


async def _close_entries(entries: list[_SessionEntry], reason: str) -> None:
    if not entries:
        return
    close_tasks = [
        asyncio.create_task(entry.runtime.close(reason)) for entry in entries
    ]
    done, pending = await asyncio.wait(close_tasks, timeout=SHUTDOWN_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        if not task.cancelled():
            task.exception()

    run_tasks = [entry.task for entry in entries]
    done, pending = await asyncio.wait(run_tasks, timeout=SHUTDOWN_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        if not task.cancelled():
            task.exception()
    for entry in entries:
        entry.binding.clear_secrets()


def _clear_buffered_value(value: Any) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _clear_buffered_value(nested)
        value.clear()
    elif isinstance(value, list):
        for nested in value:
            _clear_buffered_value(nested)
        value.clear()


def _completed_waiter_value(task: asyncio.Task[Any] | None) -> Any:
    """Return a finished waiter's value, or None when it carries none."""

    if task is None or not task.done() or task.cancelled():
        return None
    if task.exception() is not None:
        return None
    return task.result()


def _discard_owned_event(event: _OwnedEvent) -> None:
    for value in event.args:
        if isinstance(value, (dict, list)):
            _clear_buffered_value(value)
    event.args = ()


def _safe_adapter_failure_reason(exc: Exception) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and _SAFE_FAILURE_REASON.fullmatch(reason):
        return reason
    return "unexpected_adapter_error"


def _is_int_between(value: Any, minimum: int, maximum: int) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and minimum <= value <= maximum
    )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ProtocolViolation("invalid_timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ProtocolViolation("invalid_timestamp") from None
    if parsed.tzinfo is None:
        raise ProtocolViolation("invalid_timestamp")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _uuid4_value(value: Any, code: str) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        raise ProtocolViolation(code)
    try:
        parsed = UUID(value)
    except ValueError:
        raise ProtocolViolation(code) from None
    if parsed.version != 4 or str(parsed) != value:
        raise ProtocolViolation(code)
    return value
