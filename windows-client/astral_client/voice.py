"""Feature-065 conversational voice controller for the native Windows client.

The controller owns only permission, authenticated session control, and direct
RTC media. Recognized finals are copied into the existing strict
``chat_message`` transport by :mod:`astral_client.protocol`; this module never
dispatches an agent, invokes a tool, or invents an assistant answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from PySide6.QtCore import (
    QCoreApplication,
    QMicrophonePermission,
    QObject,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtMultimedia import (
    QAudio,
    QAudioFormat,
    QAudioSink,
    QAudioSource,
    QMediaDevices,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .protocol import (
    VOICE_TRANSCRIPT_TOPIC,
    VoiceTranscriptSubmission,
    WindowsProtocolError,
)


_LIVEKIT_VENDOR_LOGGERS = ("livekit", "livekit.rtc", "livekit.rtc.synchronizer")


def _disable_livekit_vendor_logging() -> None:
    """Prevent dependency diagnostics from retaining credentialed RTC data."""

    for logger_name in _LIVEKIT_VENDOR_LOGGERS:
        logging.getLogger(logger_name).disabled = True


_disable_livekit_vendor_logging()


_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_BINDING = re.compile(r"^[A-Za-z0-9._~-]{32,512}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTROL_ORDER = (
    "voice-start",
    "voice-takeover",
    "voice-end",
    "voice-microphone",
    "voice-stop-speech",
    "voice-mute",
    "voice-chat-context",
    "voice-sensitive-recap",
)
# Glyphs for the server-owned `icon` names in webrender/chrome/composer_model.py.
# Mirrors the web client's VOICE_ICONS vocabulary (client.js) so a control means
# the same thing on every surface. Keyed on the ICON, not the control key, so
# two controls sharing an icon stay consistent.
_CONTROL_GLYPHS = {
    "microphone": "🎙",
    "device-transfer": "🔄",
    "stop": "⏹",
    "speaker-stop": "🔇",
    "speaker-muted": "🔈",
    "chat": "💬",
    "speaker-consent": "🔊",
}
# A voice status the composer stays quiet about: session off with nothing the
# server actually wants to say. Mirrors client.js `state === "off" && !message`
# (the reason rides through as the message, so the neutral one counts as none).
_QUIET_VOICE_MESSAGES = frozenset({"", "off", "ready"})
_CONTROL_ACTIONS = {
    "voice-start": "voice_session_start",
    "voice-takeover": "voice_session_takeover",
    "voice-end": "voice_session_end",
    "voice-microphone": "voice_microphone_set",
    "voice-stop-speech": "voice_speech_stop",
    "voice-mute": "voice_speech_mute_set",
    "voice-chat-context": "voice_visible_chat_update",
    "voice-sensitive-recap": "voice_sensitive_recap_request",
}
_VOICE_STATES = {
    "off",
    "unavailable",
    "connecting",
    "greeting",
    "listening",
    "speech_detected",
    "transcribing",
    "acknowledging",
    "processing",
    "waiting_on_user",
    "speaking_progress",
    "speaking_result",
    "muted",
    "suspended",
    "reconnecting",
    "error",
    "ended",
}
_VOICE_REASONS = {
    "ready",
    "feature_disabled",
    "authentication_required",
    "permission_not_determined",
    "permission_denied",
    "permission_restricted",
    "no_microphone",
    "no_audio_output",
    "media_unavailable",
    "worker_unavailable",
    "asr_unavailable",
    "tts_unavailable",
    "voice_unavailable",
    "output_language_unsupported",
    "capacity_exhausted",
    "takeover_required",
    "idle_expired",
    "backgrounded",
    "audio_interrupted",
    "chat_context_unavailable",
    "auth_expired",
    "network_interrupted",
    "media_error",
    "speech_error",
    "stale_generation",
    "ended_by_user",
    "internal_error",
}
_DEVICE_KINDS = {"web", "windows", "android", "ios", "macos", "watchos"}
_INACTIVE_VOICE_STATES = {
    "off",
    "unavailable",
    "suspended",
    "reconnecting",
    "error",
    "ended",
}
_FOREGROUND_LEASE_RENEWAL_MS = 20_000
VOICE_ANNOUNCEMENT_TOPIC = "astraldeep.voice.announcement.v1"
_MAX_ANNOUNCEMENT_BYTES = 4 * 1024
_MAX_ANNOUNCEMENT_SAMPLES = 96_000
_MAX_RESULT_OPENING_SAMPLES = 36_000
_MAX_RESULT_SAMPLES = 720_000
_UNMATCHED_TRACK_TIMEOUT_S = 1.0
_ANNOUNCEMENT_KINDS = {
    "greeting",
    "acknowledgement",
    "progress",
    "waiting",
    "result",
    "sensitive_notice",
    "failure",
    "refusal",
    "cancellation",
}
_SINGLE_ANNOUNCEMENT_KINDS = _ANNOUNCEMENT_KINDS - {"result"}
_TURN_STATES = {
    "recognizing",
    "submitting",
    "accepted",
    "processing",
    "waiting_on_user",
    "succeeded",
    "failed",
    "refused",
    "cancelled",
    "abandoned",
}
_TURN_OUTPUT_POLICIES = {
    "pending",
    "full_recap",
    "english_lifecycle_only",
}
_TURN_OUTPUT_REASONS = {
    "language_pending",
    "ready",
    "output_language_unsupported",
}
_TURN_SPEECH_OUTCOMES = {
    "source_finished",
    "failed",
    "suppressed",
}
_TERMINAL_TURN_NOTICES = {
    "failed": ("Request did not complete.", "Voice request did not complete"),
    "cancelled": ("Request did not complete.", "Voice request did not complete"),
    "abandoned": ("Request did not complete.", "Voice request did not complete"),
    "refused": ("Request did not start.", "Voice request did not start"),
}
_TURN_NOTICE_CLEAR_STATES = {
    "recognizing",
    "submitting",
    "accepted",
    "processing",
    "succeeded",
}
_TURN_VOICE_PHASES = {
    "recognizing": "transcribing",
    "submitting": "acknowledging",
    "accepted": "processing",
    "processing": "processing",
    "waiting_on_user": "waiting_on_user",
    "succeeded": "speaking_result",
    "failed": "listening",
    "refused": "listening",
    "cancelled": "listening",
    "abandoned": "listening",
}
_LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


@dataclass
class _ActivePlayout:
    manifest: dict[str, Any]
    publication: Any
    track: Any
    task: Optional[asyncio.Task] = None
    started: bool = False
    terminal: bool = False


def _uuid4(value: object) -> bool:
    return isinstance(value, str) and _UUID4.fullmatch(value) is not None


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _nullable_positive(value: object) -> bool:
    return value is None or _positive(value)


def _timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError:
        return None


class VoiceHttpError(RuntimeError):
    """Content-free authenticated voice-control failure."""

    def __init__(self, code: str, status: int = 0) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


# 066 T032 parity: honest composer lines for server refusal reasons, wording
# aligned with web's VOICE_REASON_TEXT and Apple's messageFor. An unmapped
# code still renders verbatim (honest, if terse) rather than a generic line.
_REFUSAL_REASON_TEXT = {
    "worker_unavailable": "No voice worker is available right now. You can keep typing.",
    "asr_unavailable": "The speech recognition service is unavailable right now. You can keep typing.",
    "tts_unavailable": "The speech synthesis service is unavailable right now. You can keep typing.",
    "voice_unavailable": "Voice is temporarily unavailable. You can keep typing.",
    "media_unavailable": "Voice is temporarily unavailable. You can keep typing.",
    "capacity_exhausted": "Voice is at capacity right now. Try again shortly.",
    "feature_disabled": "Voice is not enabled on this server. You can keep typing.",
    "authentication_required": "Sign in to use voice. You can keep typing.",
    "auth_expired": "Voice ended because your session expired. You can keep typing.",
    "output_language_unsupported": "Voice output is not supported for this language. You can keep typing.",
    "chat_context_unavailable": "The active chat changed before voice could start. Try again.",
}


def _refusal_line(reason: object) -> str:
    code = str(reason or "voice_unavailable")
    return _REFUSAL_REASON_TEXT.get(code, code)


class VoiceHttpClient:
    """Bounded stdlib client for the server's authenticated voice REST API."""

    def __init__(
        self,
        http_base: str,
        token_provider: Callable[[], str],
        *,
        opener=urllib.request.urlopen,
        timeout: float = 10.0,
    ) -> None:
        self.http_base = http_base.rstrip("/")
        self.token_provider = token_provider
        self.opener = opener
        self.timeout = timeout

    def capability(self) -> dict[str, Any]:
        return self._request("GET", "/api/voice/capability")

    def create(self, body: dict[str, Any], scope: dict[str, str]) -> dict[str, Any]:
        return self._request("POST", "/api/voice/sessions", body, scope)

    def takeover(
        self,
        session_id: str,
        body: dict[str, Any],
        scope: dict[str, str],
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/voice/sessions/{session_id}/takeover", body, scope
        )

    def update(
        self,
        session_id: str,
        body: dict[str, Any],
        scope: dict[str, str],
    ) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/api/voice/sessions/{session_id}", body, scope
        )

    def end(
        self,
        session_id: str,
        generation: int,
        grant_revision: int,
        scope: dict[str, str],
    ) -> None:
        query = urlencode(
            {
                "expected_generation": generation,
                "expected_media_grant_revision": grant_revision,
            }
        )
        self._request(
            "DELETE", f"/api/voice/sessions/{session_id}?{query}", None, scope
        )

    def stop_speech(
        self,
        session_id: str,
        body: dict[str, Any],
        scope: dict[str, str],
    ) -> None:
        self._request(
            "POST", f"/api/voice/sessions/{session_id}/speech/stop", body, scope
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        scope: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        token = self.token_provider()
        if not isinstance(token, str) or not token:
            raise VoiceHttpError("authentication_required", 401)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if scope is not None:
            headers.update(
                {
                    "X-Astral-Device-Id": scope["device_id"],
                    "X-Astral-Connection-Generation": scope[
                        "connection_generation"
                    ],
                    "X-Astral-Voice-Control-Binding": scope["control_binding"],
                }
            )
        data = None
        if body is not None:
            data = json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.http_base + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(256 * 1024)
        except urllib.error.HTTPError as exc:
            raw = exc.read(64 * 1024)
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, TypeError):
                payload = {}
            code = payload.get("code") if isinstance(payload, dict) else None
            if code == "voice_takeover_required" and isinstance(
                payload.get("current_session"), dict
            ):
                return {
                    "error": code,
                    "current_session": payload["current_session"],
                }
            raise VoiceHttpError(str(code or "voice_request_failed"), exc.code) from None
        except Exception as exc:
            raise VoiceHttpError("network_interrupted") from exc
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            raise VoiceHttpError("invalid_voice_response") from None
        if not isinstance(payload, dict):
            raise VoiceHttpError("invalid_voice_response")
        return payload


class QtAudioBackend(QObject):
    """QtMultimedia capture/playout with no persistent audio buffer."""

    capability_changed = Signal(dict)
    _playback_ready = Signal(bytes, int, int)
    _playout_begin = Signal(object)
    _playout_chunk = Signal(str, bytes)
    _playout_seal = Signal(str)
    _playout_interrupt = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._devices = QMediaDevices(self)
        self._devices.audioInputsChanged.connect(self._emit_capability)
        self._devices.audioOutputsChanged.connect(self._emit_capability)
        self._capture: Optional[QAudioSource] = None
        self._capture_device = None
        self._capture_callback: Optional[Callable[[bytes], None]] = None
        self._sink: Optional[QAudioSink] = None
        self._sink_device = None
        self._sink_format: Optional[tuple[int, int]] = None
        self._playout_id: Optional[str] = None
        self._playout_byte_budget = 0
        self._playout_received_bytes = 0
        self._playout_buffer = bytearray()
        self._playout_sealed = False
        self._playout_started = False
        self._playout_started_callback: Optional[Callable[[], None]] = None
        self._playout_finished_callback: Optional[Callable[[str], None]] = None
        self._playout_timer = QTimer(self)
        self._playout_timer.setInterval(5)
        self._playout_timer.timeout.connect(self._pump_playout)
        self._playback_ready.connect(self._write_playback)
        self._playout_begin.connect(self._begin_playout)
        self._playout_chunk.connect(self._queue_playout_chunk)
        self._playout_seal.connect(self._seal_playout)
        self._playout_interrupt.connect(self._interrupt_playout)

    def capability(self) -> dict[str, Any]:
        app = QCoreApplication.instance()
        status = (
            app.checkPermission(QMicrophonePermission())
            if app is not None
            else Qt.PermissionStatus.Undetermined
        )
        permission = {
            Qt.PermissionStatus.Undetermined: "not_determined",
            Qt.PermissionStatus.Granted: "authorized",
            Qt.PermissionStatus.Denied: "denied",
        }.get(status, "restricted")
        has_microphone = bool(QMediaDevices.audioInputs())
        has_output = bool(QMediaDevices.audioOutputs())
        return {
            "has_microphone": has_microphone,
            "has_audio_output": has_output,
            "microphone_permission": permission,
            "full_duplex": has_microphone and has_output,
            "transport": "livekit",
        }

    def request_microphone_permission(self, callback: Callable[[str], None]) -> None:
        app = QCoreApplication.instance()
        if app is None:
            callback("restricted")
            return
        permission = QMicrophonePermission()
        current = app.checkPermission(permission)
        if current != Qt.PermissionStatus.Undetermined:
            callback(
                "authorized"
                if current == Qt.PermissionStatus.Granted
                else "denied"
            )
            return

        def _resolved(value) -> None:
            status = app.checkPermission(value)
            callback(
                "authorized"
                if status == Qt.PermissionStatus.Granted
                else "denied"
            )

        app.requestPermission(permission, _resolved)

    def start_capture(self, callback: Callable[[bytes], None]) -> None:
        self.stop_capture()
        inputs = QMediaDevices.audioInputs()
        if not inputs:
            raise RuntimeError("no_microphone")
        audio_format = _pcm_format(48000, 1)
        device = QMediaDevices.defaultAudioInput()
        if device.isNull() or not device.isFormatSupported(audio_format):
            raise RuntimeError("media_unavailable")
        self._capture_callback = callback
        self._capture = QAudioSource(device, audio_format, self)
        self._capture_device = self._capture.start()
        if self._capture_device is None:
            self.stop_capture()
            raise RuntimeError("media_unavailable")
        self._capture_device.readyRead.connect(self._read_capture)

    @Slot()
    def _read_capture(self) -> None:
        if self._capture_device is None or self._capture_callback is None:
            return
        chunk = bytes(self._capture_device.readAll())
        if chunk:
            self._capture_callback(chunk)

    def stop_capture(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture.deleteLater()
        self._capture = None
        self._capture_device = None
        self._capture_callback = None

    def play_pcm(self, data: bytes, sample_rate: int, channels: int) -> None:
        if data:
            self._playback_ready.emit(bytes(data), sample_rate, channels)

    @Slot(bytes, int, int)
    def _write_playback(self, data: bytes, sample_rate: int, channels: int) -> None:
        if self._playout_id is not None:
            return
        requested = (sample_rate, channels)
        if self._sink is None or self._sink_format != requested:
            self._reset_sink()
            outputs = QMediaDevices.audioOutputs()
            if not outputs:
                return
            audio_format = _pcm_format(sample_rate, channels)
            device = QMediaDevices.defaultAudioOutput()
            if device.isNull() or not device.isFormatSupported(audio_format):
                return
            self._sink = QAudioSink(device, audio_format, self)
            self._sink_device = self._sink.start()
            self._sink_format = requested
        if self._sink_device is not None:
            self._sink_device.write(data)

    def begin_playout(
        self,
        playout_id: str,
        sample_rate: int,
        channels: int,
        duration_samples: int,
        on_started: Callable[[], None],
        on_finished: Callable[[str], None],
    ) -> None:
        self._playout_begin.emit(
            {
                "playout_id": playout_id,
                "sample_rate": sample_rate,
                "channels": channels,
                "duration_samples": duration_samples,
                "on_started": on_started,
                "on_finished": on_finished,
            }
        )

    def push_playout(self, playout_id: str, data: bytes) -> None:
        if data:
            self._playout_chunk.emit(playout_id, bytes(data))

    def seal_playout(self, playout_id: str) -> None:
        self._playout_seal.emit(playout_id)

    def interrupt_playout(self, playout_id: Optional[str] = None) -> None:
        self._playout_interrupt.emit(playout_id)

    @Slot(object)
    def _begin_playout(self, command: object) -> None:
        if not isinstance(command, dict):
            return
        playout_id = command.get("playout_id")
        sample_rate = command.get("sample_rate")
        channels = command.get("channels")
        duration_samples = command.get("duration_samples")
        on_started = command.get("on_started")
        on_finished = command.get("on_finished")
        if (
            not isinstance(playout_id, str)
            or not playout_id
            or sample_rate != 24_000
            or channels != 1
            or isinstance(duration_samples, bool)
            or not isinstance(duration_samples, int)
            or not 1 <= duration_samples <= _MAX_ANNOUNCEMENT_SAMPLES
            or not callable(on_started)
            or not callable(on_finished)
        ):
            if callable(on_finished):
                on_finished("interrupted")
            return
        if self._playout_id is not None:
            self._finish_playout("interrupted")
        self._reset_sink()
        self._playout_id = playout_id
        self._sink_format = (sample_rate, channels)
        self._playout_byte_budget = duration_samples * channels * 2
        self._playout_received_bytes = 0
        self._playout_buffer.clear()
        self._playout_sealed = False
        self._playout_started = False
        self._playout_started_callback = on_started
        self._playout_finished_callback = on_finished

    @Slot(str, bytes)
    def _queue_playout_chunk(self, playout_id: str, data: bytes) -> None:
        if self._playout_id != playout_id or self._playout_sealed or not data:
            return
        remaining = self._playout_byte_budget - self._playout_received_bytes
        if len(data) > remaining:
            data = data[:remaining]
        if not data:
            return
        self._playout_received_bytes += len(data)
        self._playout_buffer.extend(data)
        if self._sink is None and not self._open_playout_sink():
            self._finish_playout("interrupted")
            return
        self._pump_playout()

    def _open_playout_sink(self) -> bool:
        outputs = QMediaDevices.audioOutputs()
        if not outputs or self._sink_format is None:
            return False
        sample_rate, channels = self._sink_format
        audio_format = _pcm_format(sample_rate, channels)
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull() or not device.isFormatSupported(audio_format):
            return False
        self._sink = QAudioSink(device, audio_format, self)
        self._sink.stateChanged.connect(self._playout_state_changed)
        self._sink_device = self._sink.start()
        return self._sink_device is not None

    @Slot()
    def _pump_playout(self) -> None:
        if self._playout_id is None or self._sink is None or self._sink_device is None:
            self._playout_timer.stop()
            return
        if self._playout_buffer:
            available = max(0, int(self._sink.bytesFree()))
            if available:
                chunk = bytes(self._playout_buffer[:available])
                written = int(self._sink_device.write(chunk))
                if written < 0:
                    self._finish_playout("interrupted")
                    return
                if written:
                    del self._playout_buffer[:written]
                    if not self._playout_started:
                        self._playout_started = True
                        callback = self._playout_started_callback
                        if callback is not None:
                            callback()
        if self._playout_buffer or self._playout_sealed:
            self._playout_timer.start()
        else:
            self._playout_timer.stop()
        if (
            self._playout_sealed
            and not self._playout_buffer
            and self._playout_started
            and self._sink.state() == QAudio.State.IdleState
        ):
            self._finish_playout("finished")

    @Slot(str)
    def _seal_playout(self, playout_id: str) -> None:
        if self._playout_id != playout_id:
            return
        if self._playout_received_bytes != self._playout_byte_budget:
            self._finish_playout("interrupted")
            return
        self._playout_sealed = True
        self._pump_playout()

    @Slot(object)
    def _interrupt_playout(self, playout_id: object) -> None:
        if self._playout_id is None:
            self._reset_sink()
            return
        if playout_id is not None and playout_id != self._playout_id:
            return
        self._finish_playout("interrupted")

    @Slot(object)
    def _playout_state_changed(self, state: object) -> None:
        if self._playout_id is None:
            return
        if (
            state == QAudio.State.IdleState
            and self._playout_sealed
            and not self._playout_buffer
            and self._playout_started
        ):
            self._finish_playout("finished")
        elif state == QAudio.State.StoppedState and self._playout_started:
            self._finish_playout("interrupted")

    def _finish_playout(self, phase: str) -> None:
        callback = self._playout_finished_callback
        self._playout_timer.stop()
        self._playout_id = None
        self._playout_byte_budget = 0
        self._playout_received_bytes = 0
        self._playout_buffer.clear()
        self._playout_sealed = False
        self._playout_started = False
        self._playout_started_callback = None
        self._playout_finished_callback = None
        self._reset_sink()
        if callback is not None:
            callback(phase)

    def _reset_sink(self) -> None:
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink = None
        self._sink_device = None
        self._sink_format = None

    def stop_playback(self) -> None:
        self.interrupt_playout()

    def stop_all(self) -> None:
        self.stop_capture()
        self.stop_playback()

    @Slot()
    def _emit_capability(self) -> None:
        self.capability_changed.emit(self.capability())


def _pcm_format(sample_rate: int, channels: int) -> QAudioFormat:
    value = QAudioFormat()
    value.setSampleRate(sample_rate)
    value.setChannelCount(channels)
    value.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return value


class LiveKitRoomSession:
    """One direct-RTC room with a Qt microphone source and audio sink."""

    def __init__(self, *, stream_factory: Optional[Callable[..., Any]] = None) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._room = None
        self._source = None
        self._track = None
        self._stop_event = None
        self._audio = None
        self._on_data = None
        self._on_state = None
        self._on_playout = None
        self._grant: Optional[dict[str, Any]] = None
        self._stream_factory = stream_factory
        self._announcements: dict[str, dict[str, Any]] = {}
        self._publications: dict[str, Any] = {}
        self._tracks: dict[str, Any] = {}
        self._participant_by_sid: dict[str, str] = {}
        self._match_timeouts: dict[str, asyncio.TimerHandle] = {}
        self._subscribing: set[str] = set()
        self._active_playout: Optional[_ActivePlayout] = None
        self._microphone_enabled = True
        self._close_requested = threading.Event()

    def connect(self, grant, audio, on_data, on_state, on_playout=None) -> None:
        self.close()
        close_requested = threading.Event()
        self._close_requested = close_requested
        self._grant = dict(grant)
        self._audio = audio
        self._on_data = on_data
        self._on_state = on_state
        self._on_playout = on_playout
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(close_requested,),
            daemon=True,
        )
        self._thread.start()

    def _thread_main(self, close_requested: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run(close_requested))
        except Exception:
            self._state("error", "media_error")
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._loop = None

    async def _run(self, close_requested: threading.Event) -> None:
        from livekit import rtc

        grant = self._grant or {}
        room = rtc.Room(asyncio.get_running_loop())
        self._room = room
        worker_identity = grant["worker_identity"]

        @room.on("data_received")
        def _data(packet) -> None:
            participant = packet.participant
            identity = getattr(participant, "identity", "") if participant else ""
            maximum = {
                VOICE_TRANSCRIPT_TOPIC: 12 * 1024,
                VOICE_ANNOUNCEMENT_TOPIC: _MAX_ANNOUNCEMENT_BYTES,
            }.get(packet.topic)
            if identity != worker_identity or maximum is None:
                return
            if len(packet.data) > maximum:
                return
            try:
                value = json.loads(packet.data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError):
                return
            if isinstance(value, dict) and self._on_data is not None:
                self._on_data(packet.topic, identity, value)

        @room.on("track_published")
        def _published(publication, participant) -> None:
            self._remember_publication(publication, participant.identity)

        @room.on("track_subscribed")
        def _subscribed(track, publication, participant) -> None:
            self._remember_subscribed_track(track, publication, participant.identity)

        @room.on("track_unpublished")
        def _unpublished(publication, _participant) -> None:
            self._drop_sid(str(publication.sid), emit_interrupted=True)

        @room.on("track_unsubscribed")
        def _unsubscribed(_track, publication, _participant) -> None:
            self._drop_sid(str(publication.sid), emit_interrupted=True)

        @room.on("reconnecting")
        def _reconnecting() -> None:
            self._interrupt_all_playout()
            self._state("reconnecting", "network_interrupted")

        @room.on("reconnected")
        def _reconnected() -> None:
            self._state("connected", "")

        @room.on("disconnected")
        def _disconnected(_reason) -> None:
            self._state("disconnected", "network_interrupted")

        await room.connect(
            grant["url"],
            grant["join_token"],
            rtc.RoomOptions(auto_subscribe=False, connect_timeout=10.0),
        )
        for participant in room.remote_participants.values():
            if participant.identity != worker_identity:
                continue
            for publication in participant.track_publications.values():
                self._remember_publication(publication, participant.identity)
        self._source = rtc.AudioSource(48000, 1, queue_size_ms=200)
        self._track = rtc.LocalAudioTrack.create_audio_track(
            "astraldeep.microphone", self._source
        )
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(self._track, options)
        if self._microphone_enabled:
            self._track.unmute()
            self._audio.start_capture(self._capture)
        else:
            self._track.mute()
        self._stop_event = asyncio.Event()
        self._state("connected", "")
        if close_requested.is_set():
            self._stop_event.set()
        await self._stop_event.wait()
        self._interrupt_all_playout()
        self._audio.stop_all()
        await room.disconnect()

    def authorize_announcement(self, manifest: dict[str, Any]) -> None:
        """Install one controller-validated content-free manifest on the RTC loop."""

        loop = self._loop
        if loop is None:
            return
        value = dict(manifest)
        loop.call_soon_threadsafe(self._authorize_announcement, value)

    def _authorize_announcement(self, manifest: dict[str, Any]) -> None:
        sid = manifest["track_sid"]
        if (
            sid in self._announcements
            or sid in self._tracks
            or (
                self._active_playout is not None
                and self._active_playout.manifest["track_sid"] == sid
            )
        ):
            return
        self._announcements[sid] = manifest
        self._arm_match_timeout(sid)
        self._match_sid(sid)

    def _remember_publication(self, publication: Any, participant_identity: str) -> None:
        from livekit import rtc

        sid = str(getattr(publication, "sid", ""))
        name = str(getattr(publication, "name", ""))
        if (
            participant_identity != (self._grant or {}).get("worker_identity")
            or getattr(publication, "kind", None) != rtc.TrackKind.KIND_AUDIO
            or _OPAQUE.fullmatch(sid) is None
            or _OPAQUE.fullmatch(name) is None
        ):
            try:
                publication.set_subscribed(False)
            except Exception:
                pass
            return
        existing = self._publications.get(sid)
        if existing is not None and existing is not publication:
            try:
                publication.set_subscribed(False)
            except Exception:
                pass
            return
        self._publications[sid] = publication
        self._participant_by_sid[sid] = participant_identity
        self._arm_match_timeout(sid)
        self._match_sid(sid)

    def _remember_subscribed_track(
        self,
        track: Any,
        publication: Any,
        participant_identity: str,
    ) -> None:
        from livekit import rtc

        sid = str(getattr(publication, "sid", ""))
        manifest = self._announcements.get(sid)
        if (
            manifest is None
            or self._publications.get(sid) is not publication
            or participant_identity != (self._grant or {}).get("worker_identity")
            or getattr(track, "kind", None) != rtc.TrackKind.KIND_AUDIO
            or str(getattr(publication, "name", "")) != manifest["track_name"]
            or str(getattr(track, "sid", "")) != sid
            or str(getattr(track, "name", "")) != manifest["track_name"]
        ):
            try:
                publication.set_subscribed(False)
            except Exception:
                pass
            self._drop_sid(sid, emit_interrupted=False)
            return
        self._tracks[sid] = track
        self._subscribing.discard(sid)
        self._start_next_playout()

    def _arm_match_timeout(self, sid: str) -> None:
        if sid in self._match_timeouts:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
        if loop is not None:
            self._match_timeouts[sid] = loop.call_later(
                _UNMATCHED_TRACK_TIMEOUT_S,
                self._expire_unmatched,
                sid,
            )

    def _expire_unmatched(self, sid: str) -> None:
        self._match_timeouts.pop(sid, None)
        if (
            self._active_playout is None
            or self._active_playout.manifest["track_sid"] != sid
        ):
            self._drop_sid(sid, emit_interrupted=False)
            self._start_next_playout()

    def _match_sid(self, sid: str) -> None:
        manifest = self._announcements.get(sid)
        publication = self._publications.get(sid)
        if manifest is None or publication is None:
            return
        if str(getattr(publication, "name", "")) != manifest["track_name"]:
            self._drop_sid(sid, emit_interrupted=False)
            return
        if sid in self._subscribing:
            return
        self._subscribing.add(sid)
        try:
            publication.set_subscribed(True)
        except Exception:
            self._drop_sid(sid, emit_interrupted=False)
            return
        track = getattr(publication, "track", None)
        if track is not None:
            self._remember_subscribed_track(
                track,
                publication,
                self._participant_by_sid.get(sid, ""),
            )

    def _start_next_playout(self) -> None:
        if self._active_playout is not None or not self._announcements:
            return
        sid, manifest = min(
            self._announcements.items(),
            key=lambda item: item[1]["announcement_sequence"],
        )
        publication = self._publications.get(sid)
        track = self._tracks.get(sid)
        if publication is None or track is None:
            return
        timeout = self._match_timeouts.pop(sid, None)
        if timeout is not None:
            timeout.cancel()
        active = _ActivePlayout(manifest, publication, track)
        self._active_playout = active
        active.task = asyncio.create_task(self._render_playout(active))

    async def _render_playout(self, active: _ActivePlayout) -> None:
        from livekit import rtc

        manifest = active.manifest
        playout_id = manifest["announcement_id"]
        completion = asyncio.get_running_loop().create_future()

        def _started() -> None:
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._playout_started, active)

        def _finished(phase: str) -> None:
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(
                    self._playout_audio_finished,
                    active,
                    phase,
                    completion,
                )

        self._audio.begin_playout(
            playout_id,
            24_000,
            1,
            manifest["duration_samples"],
            _started,
            _finished,
        )
        stream_factory = self._stream_factory or rtc.AudioStream
        stream = stream_factory(
            active.track,
            sample_rate=24_000,
            num_channels=1,
            capacity=16,
        )
        rendered_samples = 0
        exact = False
        try:
            async for event in stream:
                frame = event.frame
                samples = getattr(frame, "samples_per_channel", None)
                data = bytes(frame.data)
                if (
                    getattr(frame, "sample_rate", None) != 24_000
                    or getattr(frame, "num_channels", None) != 1
                    or isinstance(samples, bool)
                    or not isinstance(samples, int)
                    or samples < 1
                    or len(data) != samples * 2
                ):
                    break
                remaining = manifest["duration_samples"] - rendered_samples
                if remaining <= 0:
                    exact = True
                    break
                accepted = min(samples, remaining)
                self._audio.push_playout(playout_id, data[: accepted * 2])
                rendered_samples += accepted
                if rendered_samples == manifest["duration_samples"]:
                    exact = True
                    break
            if exact:
                self._audio.seal_playout(playout_id)
            else:
                self._audio.interrupt_playout(playout_id)
            try:
                await asyncio.wait_for(
                    completion,
                    timeout=max(2.0, manifest["duration_samples"] / 24_000 + 1.0),
                )
            except TimeoutError:
                self._audio.interrupt_playout(playout_id)
                self._playout_terminal(active, "interrupted")
        except asyncio.CancelledError:
            self._audio.interrupt_playout(playout_id)
            self._playout_terminal(active, "interrupted")
        finally:
            await stream.aclose()
            try:
                active.publication.set_subscribed(False)
            except Exception:
                pass
            self._finish_active(active)

    def _playout_started(self, active: _ActivePlayout) -> None:
        if active is not self._active_playout or active.started or active.terminal:
            return
        active.started = True
        if self._on_playout is not None:
            self._on_playout(dict(active.manifest), "started")

    def _playout_audio_finished(
        self,
        active: _ActivePlayout,
        phase: str,
        completion: asyncio.Future,
    ) -> None:
        terminal_phase = "finished" if phase == "finished" else "interrupted"
        self._playout_terminal(active, terminal_phase)
        if not completion.done():
            completion.set_result(terminal_phase)

    def _playout_terminal(self, active: _ActivePlayout, phase: str) -> None:
        if active.terminal:
            return
        active.terminal = True
        if active.started and self._on_playout is not None:
            self._on_playout(dict(active.manifest), phase)

    def _finish_active(self, active: _ActivePlayout) -> None:
        sid = active.manifest["track_sid"]
        self._announcements.pop(sid, None)
        self._publications.pop(sid, None)
        self._tracks.pop(sid, None)
        self._participant_by_sid.pop(sid, None)
        self._subscribing.discard(sid)
        if self._active_playout is active:
            self._active_playout = None
        self._start_next_playout()

    def _drop_sid(self, sid: str, *, emit_interrupted: bool) -> None:
        active = self._active_playout
        if active is not None and active.manifest["track_sid"] == sid:
            if emit_interrupted:
                self._playout_terminal(active, "interrupted")
            if active.task is not None and not active.task.done():
                active.task.cancel()
            self._audio.interrupt_playout(active.manifest["announcement_id"])
            return
        timeout = self._match_timeouts.pop(sid, None)
        if timeout is not None:
            timeout.cancel()
        publication = self._publications.pop(sid, None)
        if publication is not None:
            try:
                publication.set_subscribed(False)
            except Exception:
                pass
        self._announcements.pop(sid, None)
        self._tracks.pop(sid, None)
        self._participant_by_sid.pop(sid, None)
        self._subscribing.discard(sid)

    def _interrupt_all_playout(self) -> None:
        active = self._active_playout
        for timeout in self._match_timeouts.values():
            timeout.cancel()
        self._match_timeouts.clear()
        for publication in self._publications.values():
            try:
                publication.set_subscribed(False)
            except Exception:
                pass
        self._announcements.clear()
        self._publications.clear()
        self._tracks.clear()
        self._participant_by_sid.clear()
        self._subscribing.clear()
        if active is not None:
            self._playout_terminal(active, "interrupted")
            if active.task is not None and not active.task.done():
                active.task.cancel()
            self._audio.interrupt_playout(active.manifest["announcement_id"])
            self._active_playout = None

    def _capture(self, pcm: bytes) -> None:
        loop = self._loop
        source = self._source
        if loop is None or source is None or not pcm:
            return
        usable = len(pcm) - (len(pcm) % 2)
        if usable != len(pcm):
            pcm = pcm[:usable]
        samples = usable // 2
        if samples < 1:
            return

        async def _push() -> None:
            from livekit import rtc

            await source.capture_frame(rtc.AudioFrame(pcm, 48000, 1, samples))

        asyncio.run_coroutine_threadsafe(_push(), loop)

    def set_microphone_enabled(self, enabled: bool) -> None:
        self._microphone_enabled = bool(enabled)
        loop = self._loop
        if loop is None:
            return

        def _apply() -> None:
            if self._track is None:
                return
            if self._microphone_enabled:
                self._track.unmute()
                try:
                    self._audio.start_capture(self._capture)
                except RuntimeError:
                    self._state("error", "media_error")
            else:
                self._track.mute()
                self._audio.stop_capture()

        loop.call_soon_threadsafe(_apply)

    def stop_playback(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._interrupt_all_playout)
        elif self._audio is not None:
            self._audio.stop_playback()

    def close(self) -> None:
        self._close_requested.set()
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(self._interrupt_all_playout)
            loop.call_soon_threadsafe(stop_event.set)
        elif self._audio is not None:
            self._audio.stop_all()

    def _state(self, state: str, message: str) -> None:
        if self._on_state is not None:
            self._on_state(state, message)


class VoiceComposerWidget(QWidget):
    """Accessible native renderer for the server-owned composer controls."""

    action_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("voiceComposer")
        self._connection: Optional[str] = None
        self._revision = -1
        self._buttons: dict[str, QPushButton] = {}
        self._request_notice_turn_id: Optional[str] = None
        self._request_notice_occurred_at: Optional[datetime] = None
        self._controls = QHBoxLayout()
        self._controls.setContentsMargins(0, 0, 0, 0)
        self._controls.setSpacing(6)
        self.status_label = QLabel("Voice: unavailable")
        self.status_label.setObjectName("voiceConversationStatus")
        self.status_label.setAccessibleName("Voice conversation status")
        self.status_label.setAccessibleDescription("Voice controls are loading")
        # Pre-frame the state is unknown, not "unavailable" — web hides the
        # line and says "Checking voice availability…" in the control tooltip.
        self.status_label.setVisible(False)
        self.transcript_label = QLabel("")
        self.transcript_label.setObjectName("voiceTranscriptPreview")
        self.transcript_label.setAccessibleName("Voice transcript preview")
        self.transcript_label.setAccessibleDescription("")
        self.transcript_label.setWordWrap(True)
        self.transcript_label.setVisible(False)
        self.request_notice_label = QLabel("")
        self.request_notice_label.setObjectName("voiceRequestTerminalNotice")
        self.request_notice_label.setProperty(
            "astralAccessibilityControl", "voice-request-outcome"
        )
        self.request_notice_label.setProperty("noticeKind", "request_failure")
        self.request_notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.request_notice_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.request_notice_label.setWordWrap(True)
        self.request_notice_label.setAccessibleName("Voice request outcome")
        self.request_notice_label.setAccessibleDescription("")
        self.request_notice_label.setVisible(False)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addLayout(self._controls)
        top.addWidget(self.status_label, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(top)
        layout.addWidget(self.request_notice_label)
        layout.addWidget(self.transcript_label)

    def apply_composer_state(self, frame: dict[str, Any], connection: str) -> bool:
        if not _valid_composer_frame(frame, connection):
            return False
        revision = frame["revision"]
        if self._connection == connection and revision <= self._revision:
            return False
        self._connection = connection
        self._revision = revision
        voice = frame["voice"]
        self._clear_buttons()
        controls = {control["key"]: control for control in voice["controls"]}
        for key in _CONTROL_ORDER:
            control = controls.get(key)
            if control is None or not control["visible"]:
                continue
            # 066 cross-client style parity: web and Android render the voice
            # controls as ICONS with the server's label carried in the tooltip
            # + accessible name. Windows rendered the label as button TEXT, so
            # a composer that reads "Start voice conversation | Voice: off"
            # beside a phone's single mic glyph looked like another product.
            # Same server model, same order, same labels — icon presentation.
            # An unmapped icon name keeps its text rather than becoming a
            # blank button.
            glyph = _CONTROL_GLYPHS.get(control["icon"], "")
            button = QPushButton(glyph or control["label"])
            button.setObjectName("voiceComposerControl")
            button.setProperty("iconOnly", bool(glyph))
            button.setProperty("voiceControlKey", key)
            button.setProperty("voiceAction", control["action"])
            button.setProperty("pressed", control["pressed"])
            button.setProperty("busy", control["busy"])
            button.setAccessibleName(control["label"])
            states = []
            if control["pressed"]:
                states.append("selected")
            if control["busy"]:
                states.append("busy")
            button.setAccessibleDescription(", ".join(states) or "Voice control")
            button.setToolTip(control["label"])
            button.setProperty("serverEnabled", control["enabled"] and not control["busy"])
            button.setEnabled(control["enabled"] and not control["busy"])
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            action = control["action"]
            button.clicked.connect(
                lambda _checked=False, selected=action: self._request_action(selected)
            )
            self._buttons[key] = button
            self._controls.addWidget(button)
        message = voice.get("message") or voice["reason"]
        if voice["reason"] == "speech_error":
            self.set_speech_error(message)
        else:
            self.set_voice_status(voice["state"], message)
        if voice["state"] == "off" and voice.get("session_id") is None:
            self.clear_request_notice()
        return True

    def _request_action(self, action: str) -> None:
        if action == "voice_session_end":
            self.clear_request_notice()
        self.action_requested.emit(action)

    def set_voice_status(self, state: str, message: str) -> None:
        safe_state = state if state in _VOICE_STATES else "error"
        safe_message = str(message or safe_state).strip()[:240]
        self.status_label.setText(f"Voice: {safe_state.replace('_', ' ')}")
        self.status_label.setAccessibleDescription(safe_message)
        # 066 parity: web hides its voice state line when the session is off
        # and the server has nothing to say (`hidden = state === "off" &&
        # !message` in client.js), so an idle composer is just the mic icon.
        # Windows kept a permanent "Voice: off" chip in the composer row.
        # Anything the server actually reports — a reason, an error, any live
        # state — still shows, and the accessible description is set either
        # way, so a screen reader loses nothing.
        self.status_label.setVisible(
            not (safe_state == "off" and safe_message.lower() in _QUIET_VOICE_MESSAGES)
        )
        self.setProperty("voiceState", safe_state)

    def set_voice_turn_status(
        self,
        state: str,
        message: str,
        *,
        turn_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        """Show a persistent, non-color terminal outcome for one voice request."""

        self.set_voice_status(_TURN_VOICE_PHASES.get(state, "error"), message)
        terminal_notice = _TERMINAL_TURN_NOTICES.get(state)
        if terminal_notice is None:
            self._clear_request_notice_for_newer_turn(
                state=state,
                turn_id=turn_id,
                occurred_at=occurred_at,
            )
            return
        heading, accessible_name = terminal_notice
        self._show_request_notice(
            heading=heading,
            message=message,
            accessible_name=accessible_name,
            kind="request_failure",
            turn_id=turn_id,
            occurred_at=occurred_at,
        )

    def set_voice_submission_rejected(
        self,
        message: str,
        *,
        retry_policy: str,
        turn_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        """Present a correlated terminal rejection without replaying it."""

        guidance = (
            "Please try speaking again, or use typed chat."
            if retry_policy == "explicit_user_retry"
            else (
                "This request will not retry automatically. "
                "Use typed chat to continue."
            )
        )
        self.set_voice_status("error", message)
        self._show_request_notice(
            heading="Request did not start.",
            message=message,
            guidance=guidance,
            accessible_name="Voice request did not start",
            kind="request_failure",
            turn_id=turn_id,
            occurred_at=occurred_at,
        )

    def set_speech_error(
        self,
        message: str,
        *,
        turn_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
        update_status: bool = True,
        text_result_available: bool = False,
    ) -> None:
        """Distinguish failed speech output from the underlying text request."""

        if update_status:
            self.set_voice_status("error", message)
        self._show_request_notice(
            heading=(
                "Speech playback failed."
                if text_result_available
                else (
                    "Speech playback failed. The text result may still be available "
                    "in the conversation."
                )
            ),
            message=message,
            guidance=(
                "The text result is still available in the conversation. "
                "Typed chat remains available."
                if text_result_available
                else None
            ),
            accessible_name="Voice speech error",
            kind="speech_error",
            turn_id=turn_id,
            occurred_at=occurred_at,
        )

    def clear_request_notice(self, *, preserve_fence: bool = False) -> None:
        """Hide and scrub the prior request outcome on an explicit reset."""

        if not preserve_fence:
            self._request_notice_turn_id = None
            self._request_notice_occurred_at = None
        self.request_notice_label.setText("")
        self.request_notice_label.setAccessibleName("Voice request outcome")
        self.request_notice_label.setAccessibleDescription("")
        self.request_notice_label.setVisible(False)

    def _clear_request_notice_for_newer_turn(
        self,
        *,
        state: str,
        turn_id: Optional[str],
        occurred_at: Optional[str],
    ) -> None:
        """Clear only when a distinct, non-older turn has demonstrably begun."""

        next_occurred_at = _timestamp(occurred_at)
        if state not in _TURN_NOTICE_CLEAR_STATES or not _uuid4(turn_id):
            return
        if next_occurred_at is None:
            return
        current_turn_id = self._request_notice_turn_id
        current_occurred_at = self._request_notice_occurred_at
        if (
            _uuid4(current_turn_id)
            and current_occurred_at is not None
            and next_occurred_at < current_occurred_at
        ):
            return
        self._request_notice_turn_id = turn_id
        self._request_notice_occurred_at = next_occurred_at
        if turn_id != current_turn_id:
            self.clear_request_notice(preserve_fence=True)

    def _show_request_notice(
        self,
        *,
        heading: str,
        message: str,
        accessible_name: str,
        kind: str,
        guidance: Optional[str] = None,
        turn_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        """Render and announce one server-explained request or speech outcome."""

        server_message = str(message or "No additional details were provided.")
        text = f"⚠ {heading}\n{server_message}"
        if guidance:
            text += f"\n{guidance}"
        parsed_occurred_at = _timestamp(occurred_at)
        if (
            _uuid4(turn_id)
            and parsed_occurred_at is not None
            and _uuid4(self._request_notice_turn_id)
            and self._request_notice_occurred_at is not None
            and parsed_occurred_at < self._request_notice_occurred_at
        ):
            return
        self._request_notice_turn_id = turn_id if _uuid4(turn_id) else None
        self._request_notice_occurred_at = parsed_occurred_at
        self.request_notice_label.setProperty("noticeKind", kind)
        self.request_notice_label.setText(text)
        self.request_notice_label.setAccessibleName(accessible_name)
        self.request_notice_label.setAccessibleDescription(text)
        self.request_notice_label.setVisible(True)
        style = self.request_notice_label.style()
        style.unpolish(self.request_notice_label)
        style.polish(self.request_notice_label)
        alert = getattr(QAccessible.Event, "Alert", None)
        if alert is not None:
            QAccessible.updateAccessibility(
                QAccessibleEvent(self.request_notice_label, alert)
            )

    def set_transcript(self, text: str, final: bool) -> None:
        bounded = str(text or "")[:8000]
        self.transcript_label.setText(bounded)
        self.transcript_label.setAccessibleDescription(
            ("Final transcript: " if final else "Partial transcript: ") + bounded
        )
        self.transcript_label.setProperty("final", bool(final))
        self.transcript_label.setVisible(bool(bounded))

    def set_composer_enabled(self, enabled: bool) -> None:
        for button in self._buttons.values():
            button.setEnabled(enabled and bool(button.property("serverEnabled")))

    def _clear_buttons(self) -> None:
        while self._controls.count():
            item = self._controls.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._buttons.clear()


def _valid_composer_frame(frame: object, connection: str) -> bool:
    if not isinstance(frame, dict) or set(frame) != {
        "type",
        "schema_version",
        "revision",
        "connection_generation",
        "voice",
    }:
        return False
    if (
        frame["type"] != "composer_state"
        or frame["schema_version"] != "1"
        or frame["connection_generation"] != connection
        or not isinstance(frame["revision"], int)
        or isinstance(frame["revision"], bool)
        or frame["revision"] < 0
        or not isinstance(frame["voice"], dict)
    ):
        return False
    voice = frame["voice"]
    required = {
        "available",
        "state",
        "speech_muted",
        "microphone_enabled",
        "foreground_active",
        "reason",
        "output_locale",
        "chat_context_revision",
        "applied_chat_context_revision",
        "chat_context_synced",
        "controls",
    }
    optional = {
        "message",
        "session_id",
        "generation",
        "media_grant_revision",
        "visible_chat_id",
        "foreground_turn_id",
        "owner_device",
        "idle_expires_at",
    }
    if (
        not required <= set(voice) <= required | optional
        or not isinstance(voice["state"], str)
        or voice["state"] not in _VOICE_STATES
        or not isinstance(voice["reason"], str)
        or voice["reason"] not in _VOICE_REASONS
        or voice["output_locale"] != "en-US"
        or any(
            not isinstance(voice[name], bool)
            for name in (
                "available",
                "speech_muted",
                "microphone_enabled",
                "foreground_active",
                "chat_context_synced",
            )
        )
        or not _nullable_positive(voice["chat_context_revision"])
        or not _nullable_positive(voice["applied_chat_context_revision"])
        or (
            not voice["foreground_active"]
            and (
                voice["microphone_enabled"]
                or voice["state"] not in _INACTIVE_VOICE_STATES
            )
        )
    ):
        return False
    for name in ("session_id", "visible_chat_id", "foreground_turn_id"):
        if name in voice and voice[name] is not None and not _uuid4(voice[name]):
            return False
    for name in ("generation", "media_grant_revision"):
        if name in voice and not _nullable_positive(voice[name]):
            return False
    message = voice.get("message")
    if message is not None and (not isinstance(message, str) or len(message) > 240):
        return False
    idle_expiry = voice.get("idle_expires_at")
    if idle_expiry is not None and _timestamp(idle_expiry) is None:
        return False
    owner = voice.get("owner_device")
    if owner is not None:
        owner_required = {"device_id", "device_kind", "generation"}
        owner_optional = {"device_label"}
        if (
            not isinstance(owner, dict)
            or not owner_required <= set(owner) <= owner_required | owner_optional
            or not _uuid4(owner.get("device_id"))
            or not isinstance(owner.get("device_kind"), str)
            or owner.get("device_kind") not in _DEVICE_KINDS
            or not _positive(owner.get("generation"))
            or (
                "device_label" in owner
                and (
                    not isinstance(owner["device_label"], str)
                    or len(owner["device_label"]) > 80
                )
            )
        ):
            return False
    controls = voice["controls"]
    if not isinstance(controls, list) or not 1 <= len(controls) <= 12:
        return False
    keys = []
    for control in controls:
        if not isinstance(control, dict) or set(control) != {
            "key",
            "action",
            "label",
            "icon",
            "visible",
            "enabled",
            "pressed",
            "busy",
        }:
            return False
        key = control["key"]
        if (
            not isinstance(key, str)
            or key not in _CONTROL_ACTIONS
            or control["action"] != _CONTROL_ACTIONS[key]
            or not isinstance(control["label"], str)
            or not 1 <= len(control["label"]) <= 80
            or not isinstance(control["icon"], str)
            or any(
                not isinstance(control[name], bool)
                for name in ("visible", "enabled", "pressed", "busy")
            )
        ):
            return False
        keys.append(key)
    expected = [key for key in _CONTROL_ORDER if key in keys]
    return keys == expected and len(keys) == len(set(keys))


def _valid_partial_transcript(frame: dict[str, Any]) -> bool:
    expected = {
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
        "source_participant_identity",
    }
    return (
        set(frame) == expected
        and frame.get("type") == "voice_transcript"
        and frame.get("schema_version") == "1"
        and frame.get("final") is False
        and frame.get("detected_language") is None
        and all(
            _uuid4(frame.get(name))
            for name in (
                "session_id",
                "turn_id",
                "client_turn_id",
                "submission_id",
                "request_generation",
                "chat_id",
            )
        )
        and all(
            _positive(frame.get(name))
            for name in (
                "generation",
                "chat_context_revision",
                "media_grant_revision",
            )
        )
        and isinstance(frame.get("sequence"), int)
        and not isinstance(frame.get("sequence"), bool)
        and frame["sequence"] >= 0
        and isinstance(frame.get("text"), str)
        and len(frame["text"]) <= 8000
        and isinstance(frame.get("source_participant_identity"), str)
        and _OPAQUE.fullmatch(frame["source_participant_identity"]) is not None
    )


class VoiceController(QObject):
    """Generation-fenced session reducer and explicit-action controller."""

    status_changed = Signal(str, str)
    transcript_changed = Signal(str, bool)
    chat_required = Signal(str, str)
    _lease_start_requested = Signal()
    _lease_stop_requested = Signal()

    def __init__(
        self,
        *,
        device_id: str,
        token_provider: Callable[[], str],
        http_base: str,
        connection_provider: Callable[[], Optional[str]],
        chat_provider: Callable[[], Optional[str]],
        transport: Any,
        audio: Optional[Any] = None,
        http: Optional[Any] = None,
        media: Optional[Any] = None,
        run_async: Optional[Callable[[Callable[[], None]], None]] = None,
        lease_timer: Optional[Any] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if not _uuid4(device_id):
            raise ValueError("voice device_id must be UUID4")
        self.device_id = device_id
        self.connection_provider = connection_provider
        self.chat_provider = chat_provider
        self.transport = transport
        self.audio = audio or QtAudioBackend(self)
        self.http = http or VoiceHttpClient(http_base, token_provider)
        self.media = media or LiveKitRoomSession()
        self._run_async = run_async or self._start_thread
        self.control_binding: Optional[str] = None
        self.control_binding_id: Optional[str] = None
        self.control_binding_connection: Optional[str] = None
        self.control_binding_expires_at: Optional[datetime] = None
        self.session_id: Optional[str] = None
        self.generation: Optional[int] = None
        self.media_grant_revision: Optional[int] = None
        self.worker_identity: Optional[str] = None
        self.visible_chat_id: Optional[str] = None
        self.chat_context_revision: Optional[int] = None
        self.microphone_enabled = False
        self.speech_muted = False
        self.state = "off"
        self.takeover_session_id: Optional[str] = None
        self.takeover_generation: Optional[int] = None
        self.takeover_grant_revision: Optional[int] = None
        self._seen_sequences: dict[str, int] = {}
        self._submitted_turns: set[str] = set()
        self._turn_sequences: dict[str, int] = {}
        self._last_announcement_sequence = 0
        self._announcement_ids: set[str] = set()
        self._announcement_track_sids: set[str] = set()
        self._announcement_track_names: set[str] = set()
        self._result_reserved_samples: dict[str, int] = {}
        self._result_quantum_indexes: dict[str, int] = {}
        self._playout_sequence = 0
        self._pending_chat_activation: Optional[tuple[str, str]] = None
        self._activation_id: Optional[str] = None
        self._foreground_active = True
        self._foreground_microphone_enabled = False
        self._lease_renewal_inflight = False
        self._session_update_lock = threading.Lock()
        self._session_ending = False
        self._lease_timer = lease_timer or QTimer(self)
        self._lease_timer.setInterval(_FOREGROUND_LEASE_RENEWAL_MS)
        self._lease_timer.setSingleShot(False)
        self._lease_timer.timeout.connect(self._renew_foreground_lease)
        self._lease_start_requested.connect(self._start_lease_heartbeat)
        self._lease_stop_requested.connect(self._stop_lease_heartbeat)
        app = QCoreApplication.instance()
        application_state_changed = getattr(app, "applicationStateChanged", None)
        if application_state_changed is not None and hasattr(
            application_state_changed, "connect"
        ):
            application_state_changed.connect(self._on_application_state_changed)
        changed = getattr(self.audio, "capability_changed", None)
        if changed is not None and hasattr(changed, "connect"):
            changed.connect(self._on_capability_changed)

    @staticmethod
    def _start_thread(work: Callable[[], None]) -> None:
        threading.Thread(target=work, daemon=True).start()

    def accept_frame(self, frame: dict[str, Any]) -> bool:
        frame_type = frame.get("type") if isinstance(frame, dict) else None
        if frame_type == "voice_control_binding":
            return self._accept_binding(frame)
        if frame_type == "composer_state":
            return self._accept_composer(frame)
        if frame_type == "voice_session_state":
            return self._accept_session_state(frame)
        if frame_type == "voice_turn_state":
            return self._accept_turn_state(frame)
        if frame_type == "auth_required":
            self.on_connection_rotated(None)
            self._set_status("unavailable", "Authentication is required for voice.")
            return True
        return False

    def _accept_binding(self, frame: dict[str, Any]) -> bool:
        if set(frame) != {
            "type",
            "schema_version",
            "device_id",
            "connection_generation",
            "binding_id",
            "binding",
            "expires_at",
        }:
            return False
        connection = self.connection_provider()
        expiry = _timestamp(frame.get("expires_at"))
        if (
            frame.get("schema_version") != "1"
            or frame.get("device_id") != self.device_id
            or frame.get("connection_generation") != connection
            or not _uuid4(frame.get("binding_id"))
            or not isinstance(frame.get("binding"), str)
            or _BINDING.fullmatch(frame["binding"]) is None
            or expiry is None
            or expiry <= datetime.now(timezone.utc)
        ):
            return False
        self.control_binding = frame["binding"]
        self.control_binding_id = frame["binding_id"]
        self.control_binding_connection = frame["connection_generation"]
        self.control_binding_expires_at = expiry
        return True

    def _accept_composer(self, frame: dict[str, Any]) -> bool:
        connection = self.connection_provider()
        if not isinstance(connection, str) or not _valid_composer_frame(frame, connection):
            return False
        voice = frame["voice"]
        session_id = voice.get("session_id")
        generation = voice.get("generation")
        grant_revision = voice.get("media_grant_revision")
        if _uuid4(session_id) and _positive(generation) and _positive(grant_revision):
            owner = voice.get("owner_device")
            if isinstance(owner, dict) and owner.get("device_id") != self.device_id:
                self.takeover_session_id = session_id
                self.takeover_generation = generation
                self.takeover_grant_revision = grant_revision
        return True

    def _accept_session_state(self, frame: dict[str, Any]) -> bool:
        if self.session_id is None:
            return False
        required = {
            "type",
            "schema_version",
            "session_id",
            "connection_generation",
            "generation",
            "media_grant_revision",
            "visible_chat_id",
            "chat_context_revision",
            "applied_chat_context_revision",
            "chat_context_synced",
            "state",
            "speech_muted",
            "microphone_enabled",
            "foreground_active",
            "reason",
            "occurred_at",
        }
        supplied = set(frame)
        if supplied != required and supplied != required | {"message"}:
            return False
        if (
            frame.get("schema_version") != "1"
            or frame.get("session_id") != self.session_id
            or frame.get("connection_generation") != self.connection_provider()
            or frame.get("generation") != self.generation
            or frame.get("media_grant_revision") != self.media_grant_revision
            or not _uuid4(frame.get("visible_chat_id"))
            or not _positive(frame.get("chat_context_revision"))
            or not _nullable_positive(frame.get("applied_chat_context_revision"))
            or not isinstance(frame.get("chat_context_synced"), bool)
            or not isinstance(frame.get("state"), str)
            or frame.get("state") not in _VOICE_STATES
            or not isinstance(frame.get("speech_muted"), bool)
            or not isinstance(frame.get("microphone_enabled"), bool)
            or not isinstance(frame.get("foreground_active"), bool)
            or not isinstance(frame.get("reason"), str)
            or frame.get("reason") not in _VOICE_REASONS
            or _timestamp(frame.get("occurred_at")) is None
            or (
                "message" in frame
                and (
                    not isinstance(frame["message"], str)
                    or len(frame["message"]) > 240
                )
            )
            or (
                not frame.get("foreground_active")
                and frame.get("microphone_enabled")
            )
        ):
            return False
        self.microphone_enabled = frame["microphone_enabled"]
        if frame["foreground_active"]:
            self._foreground_microphone_enabled = self.microphone_enabled
        self.speech_muted = frame["speech_muted"]
        self.visible_chat_id = frame["visible_chat_id"]
        self.chat_context_revision = frame["chat_context_revision"]
        self.media.set_microphone_enabled(
            self.microphone_enabled
            and bool(frame["foreground_active"])
            and bool(frame["chat_context_synced"])
        )
        message = frame.get("message") or frame["reason"]
        if frame["state"] == "ended" or frame["reason"] in {
            "idle_expired",
            "auth_expired",
            "stale_generation",
        }:
            self._teardown(frame["state"], message)
        else:
            if frame["foreground_active"] and self._foreground_active:
                self._lease_start_requested.emit()
            else:
                self._lease_stop_requested.emit()
            self._set_status(frame["state"], message)
        return True

    def _accept_turn_state(self, frame: dict[str, Any]) -> bool:
        """Reduce one current, strict turn state before it may affect native UI."""

        required = {
            "type",
            "schema_version",
            "session_id",
            "connection_generation",
            "generation",
            "media_grant_revision",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
            "chat_context_revision",
            "detected_language",
            "spoken_output_policy",
            "output_reason",
            "state",
            "foreground",
            "sensitive_result_pending",
            "sequence",
            "occurred_at",
        }
        supplied = set(frame) if isinstance(frame, dict) else set()
        if not required <= supplied <= required | {
            "result_id",
            "message",
            "speech_outcome",
        }:
            return False
        turn_id = frame.get("turn_id")
        sequence = frame.get("sequence")
        language = frame.get("detected_language")
        state = frame.get("state")
        if (
            frame.get("type") != "voice_turn_state"
            or frame.get("schema_version") != "1"
            or frame.get("session_id") != self.session_id
            or frame.get("connection_generation") != self.connection_provider()
            or frame.get("generation") != self.generation
            or frame.get("media_grant_revision") != self.media_grant_revision
            or not _uuid4(turn_id)
            or any(
                not _uuid4(frame.get(name))
                for name in (
                    "client_turn_id",
                    "submission_id",
                    "request_generation",
                    "chat_id",
                )
            )
            or not _positive(frame.get("chat_context_revision"))
            or state not in _TURN_STATES
            or not isinstance(frame.get("foreground"), bool)
            or not isinstance(frame.get("sensitive_result_pending"), bool)
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or _timestamp(frame.get("occurred_at")) is None
        ):
            return False
        if language is not None and (
            not isinstance(language, str)
            or len(language) > 32
            or _LANGUAGE_TAG.fullmatch(language) is None
        ):
            return False
        if state == "recognizing" and language is not None:
            return False
        if state not in {"recognizing", "abandoned"} and language is None:
            return False
        if language is None:
            expected_policy = "pending"
            expected_reason = "language_pending"
        elif language == "en" or language.startswith("en-"):
            expected_policy = "full_recap"
            expected_reason = "ready"
        else:
            expected_policy = "english_lifecycle_only"
            expected_reason = "output_language_unsupported"
        if (
            frame.get("spoken_output_policy") not in _TURN_OUTPUT_POLICIES
            or frame.get("output_reason") not in _TURN_OUTPUT_REASONS
            or frame.get("spoken_output_policy") != expected_policy
            or frame.get("output_reason") != expected_reason
        ):
            return False
        result_id = frame.get("result_id")
        if result_id is not None and (
            not isinstance(result_id, str) or _OPAQUE.fullmatch(result_id) is None
        ):
            return False
        message = frame.get("message")
        if message is not None and (
            not isinstance(message, str) or len(message) > 240
        ):
            return False
        speech_outcome = frame.get("speech_outcome")
        if "speech_outcome" in frame and speech_outcome not in _TURN_SPEECH_OUTCOMES:
            return False
        if speech_outcome is not None and state != "succeeded":
            return False
        if sequence <= self._turn_sequences.get(turn_id, -1):
            return False
        self._turn_sequences[turn_id] = sequence
        self._set_status(
            _TURN_VOICE_PHASES[state],
            message or state,
        )
        return True

    def handle_action(self, action: str) -> None:
        if action in {"voice_session_start", "voice_session_takeover"}:
            self._begin_activation(action)
        elif action == "voice_session_end":
            self._end()
        elif action == "voice_microphone_set":
            self._update_session(microphone_enabled=not self.microphone_enabled)
        elif action == "voice_speech_mute_set":
            self._update_session(speech_muted=not self.speech_muted)
        elif action == "voice_speech_stop":
            self._stop_speech()
        elif action == "voice_visible_chat_update":
            chat_id = self.chat_provider()
            if _uuid4(chat_id):
                self._update_session(visible_chat_id=chat_id)

    def _begin_activation(self, action: str) -> None:
        if self.state == "connecting":
            return
        self._foreground_active = True
        capability = self.audio.capability()
        if not capability.get("has_microphone"):
            self._set_status("unavailable", "No microphone is available; typed chat still works.")
            return
        if not capability.get("has_audio_output"):
            self._set_status("unavailable", "No audio output is available; typed chat still works.")
            return
        self._set_status("connecting", "Checking microphone permission…")
        self.audio.request_microphone_permission(
            lambda permission: self._permission_resolved(action, permission)
        )

    def _permission_resolved(self, action: str, permission: str) -> None:
        if permission != "authorized":
            self._set_status(
                "unavailable",
                "Microphone permission was denied; typed chat still works.",
            )
            return
        chat_id = self.chat_provider()
        activation_id = str(uuid.uuid4())
        if not _uuid4(chat_id):
            self._pending_chat_activation = (action, activation_id)
            self.chat_required.emit(action, activation_id)
            self._set_status("connecting", "Creating a conversation for voice…")
            return
        self._activate(action, activation_id, chat_id)

    def continue_activation(self, action: str, activation_id: str, chat_id: str) -> bool:
        if self._pending_chat_activation != (action, activation_id) or not _uuid4(chat_id):
            return False
        self._pending_chat_activation = None
        self._activate(action, activation_id, chat_id)
        return True

    def cancel_pending_activation(self) -> None:
        self._pending_chat_activation = None
        self._activation_id = None
        self._set_status("off", "Voice activation was cancelled.")

    def _activate(self, action: str, activation_id: str, chat_id: str) -> None:
        self._activation_id = activation_id
        try:
            scope = self._scope()
        except WindowsProtocolError as exc:
            self._activation_id = None
            self._set_status("unavailable", str(exc))
            return
        capability = self.audio.capability()
        capability["microphone_permission"] = "authorized"
        body = {
            "device_id": self.device_id,
            "device_kind": "windows",
            "visible_chat_id": chat_id,
            "activation_id": activation_id,
            "capability": capability,
            "foreground_active": True,
        }
        takeover_session_id: Optional[str] = None
        if action == "voice_session_takeover":
            if not (
                _uuid4(self.takeover_session_id)
                and _positive(self.takeover_generation)
                and _positive(self.takeover_grant_revision)
            ):
                self._set_status("error", "There is no current voice session to take over.")
                self._activation_id = None
                return
            takeover_session_id = self.takeover_session_id
            body.update(
                {
                    "expected_generation": self.takeover_generation,
                    "expected_media_grant_revision": self.takeover_grant_revision,
                }
            )
        self._set_status("connecting", "Connecting voice conversation…")

        def _work() -> None:
            try:
                ready = self.http.capability()
                if not isinstance(ready, dict):
                    raise WindowsProtocolError("voice capability response is malformed")
                if ready.get("status") != "ready" or ready.get("reason") != "ready":
                    if self._activation_id == activation_id:
                        self._activation_id = None
                    self._set_status(
                        "unavailable", _refusal_line(ready.get("reason"))
                    )
                    return
                if action == "voice_session_takeover":
                    response = self.http.takeover(takeover_session_id, body, scope)
                else:
                    response = self.http.create(body, scope)
                if self._activation_id != activation_id:
                    raise WindowsProtocolError("stale voice activation response")
                if response.get("error") == "voice_takeover_required":
                    current = response.get("current_session") or {}
                    if not (
                        _uuid4(current.get("session_id"))
                        and _positive(current.get("generation"))
                        and _positive(current.get("media_grant_revision"))
                    ):
                        raise WindowsProtocolError("takeover response is malformed")
                    self.takeover_session_id = current["session_id"]
                    self.takeover_generation = current["generation"]
                    self.takeover_grant_revision = current["media_grant_revision"]
                    self._activation_id = None
                    self._set_status(
                        "suspended",
                        "Voice is active on another device. Choose Take over to continue here.",
                    )
                    return
                if (
                    scope["connection_generation"] != self.connection_provider()
                    or scope["control_binding"] != self.control_binding
                ):
                    raise WindowsProtocolError("stale voice activation response")
                session, grant = self._validate_activation(response, chat_id)
                self.session_id = session["session_id"]
                self._session_ending = False
                self.generation = session["generation"]
                self.media_grant_revision = session["media_grant_revision"]
                self.worker_identity = grant["worker_identity"]
                self.visible_chat_id = session["visible_chat_id"]
                self.chat_context_revision = session["chat_context_revision"]
                self.microphone_enabled = bool(session["microphone_enabled"])
                self._foreground_microphone_enabled = self.microphone_enabled
                self.speech_muted = bool(session["speech_muted"])
                self._seen_sequences.clear()
                self._submitted_turns.clear()
                self._reset_announcement_ledger()
                self._activation_id = None
                self.media.connect(
                    grant,
                    self.audio,
                    self._on_media_data,
                    self._on_media_state,
                    self._on_media_playout,
                )
                self._lease_start_requested.emit()
            except (VoiceHttpError, WindowsProtocolError) as exc:
                if self._activation_id == activation_id:
                    self._activation_id = None
                if isinstance(exc, VoiceHttpError):
                    self._set_status("error", _refusal_line(exc.code))
                else:
                    self._set_status("error", str(exc))

        self._run_async(_work)

    def _validate_activation(
        self, response: object, chat_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(response, dict) or set(response) != {"session", "grant"}:
            raise WindowsProtocolError("voice activation response is malformed")
        session = response["session"]
        grant = response["grant"]
        if not isinstance(session, dict) or not isinstance(grant, dict):
            raise WindowsProtocolError("voice activation response is malformed")
        required_session = {
            "session_id",
            "device_id",
            "device_kind",
            "transport",
            "state",
            "generation",
            "media_grant_revision",
            "owner_connection_generation",
            "visible_chat_id",
            "applied_visible_chat_id",
            "chat_context_revision",
            "applied_chat_context_revision",
            "chat_context_synced",
            "foreground_active",
            "foreground_reason",
            "foreground_changed_at",
            "speech_muted",
            "microphone_enabled",
            "lease_expires_at",
            "started_at",
        }
        now = datetime.now(timezone.utc)
        lease_expiry = _timestamp(session.get("lease_expires_at"))
        allowed_session_fields = {
            frozenset(required_session),
            frozenset(required_session | {"idle_expires_at"}),
        }
        if frozenset(session) not in allowed_session_fields or not (
            _uuid4(session.get("session_id"))
            and session.get("device_id") == self.device_id
            and session.get("device_kind") == "windows"
            and session.get("transport") == "livekit"
            and session.get("state") == "active"
            and _positive(session.get("generation"))
            and _positive(session.get("media_grant_revision"))
            and session.get("owner_connection_generation")
            == self.connection_provider()
            and session.get("visible_chat_id") == chat_id
            and session.get("applied_visible_chat_id") == chat_id
            and _positive(session.get("chat_context_revision"))
            and session.get("applied_chat_context_revision")
            == session.get("chat_context_revision")
            and session.get("chat_context_synced") is True
            and session.get("foreground_active") is True
            and session.get("foreground_reason") == "foreground"
            and _timestamp(session.get("foreground_changed_at")) is not None
            and isinstance(session.get("speech_muted"), bool)
            and isinstance(session.get("microphone_enabled"), bool)
            and lease_expiry is not None
            and lease_expiry > now
            and _timestamp(session.get("started_at")) is not None
            and (
                session.get("idle_expires_at") is None
                or _timestamp(session.get("idle_expires_at")) is not None
            )
        ):
            raise WindowsProtocolError("voice session binding is invalid")
        required_grant = {
            "grant_id",
            "transport",
            "session_id",
            "generation",
            "media_grant_revision",
            "expires_at",
            "url",
            "join_token",
            "room_name",
            "participant_identity",
            "worker_identity",
        }
        grant_expiry = _timestamp(grant.get("expires_at"))
        if set(grant) != required_grant or not (
            isinstance(grant.get("grant_id"), str)
            and _OPAQUE.fullmatch(grant["grant_id"]) is not None
            and grant.get("transport") == "livekit"
            and grant.get("session_id") == session["session_id"]
            and grant.get("generation") == session["generation"]
            and grant.get("media_grant_revision") == session["media_grant_revision"]
            and isinstance(grant.get("url"), str)
            and grant["url"].startswith(("ws://", "wss://"))
            and isinstance(grant.get("join_token"), str)
            and 32 <= len(grant["join_token"]) <= 8192
            and isinstance(grant.get("room_name"), str)
            and _OPAQUE.fullmatch(grant["room_name"]) is not None
            and isinstance(grant.get("participant_identity"), str)
            and _OPAQUE.fullmatch(grant["participant_identity"]) is not None
            and isinstance(grant.get("worker_identity"), str)
            and _OPAQUE.fullmatch(grant["worker_identity"]) is not None
            and grant["participant_identity"] != grant["worker_identity"]
            and grant_expiry is not None
            and grant_expiry > now
        ):
            raise WindowsProtocolError("voice media grant is invalid")
        return session, grant

    def _scope(self) -> dict[str, str]:
        connection = self.connection_provider()
        if (
            not _uuid4(connection)
            or connection != self.control_binding_connection
            or self.control_binding is None
            or self.control_binding_expires_at is None
            or self.control_binding_expires_at <= datetime.now(timezone.utc)
        ):
            raise WindowsProtocolError("Voice control is reconnecting; typed chat still works.")
        return {
            "device_id": self.device_id,
            "connection_generation": connection,
            "control_binding": self.control_binding,
        }

    def _on_media_state(self, state: str, message: str) -> None:
        if state == "connected":
            self._set_status("greeting", "Voice connected; greeting is playing.")
        elif state == "reconnecting":
            self._set_status("reconnecting", "Voice media is reconnecting…")
        elif state in {"error", "disconnected"}:
            self._teardown("error", message or "Voice media disconnected.")

    def _on_media_data(self, topic: str, sender: str, frame: dict[str, Any]) -> None:
        if topic == VOICE_ANNOUNCEMENT_TOPIC:
            self._on_announcement_manifest(sender, frame)
            return
        if (
            topic != VOICE_TRANSCRIPT_TOPIC
            or sender != self.worker_identity
            or not isinstance(frame, dict)
            or frame.get("source_participant_identity") != sender
            or frame.get("session_id") != self.session_id
            or frame.get("generation") != self.generation
            or frame.get("media_grant_revision") != self.media_grant_revision
            or not _uuid4(frame.get("turn_id"))
            or not isinstance(frame.get("sequence"), int)
            or isinstance(frame.get("sequence"), bool)
            or frame["sequence"] < 0
        ):
            return
        final = frame.get("final") is True
        if final:
            try:
                VoiceTranscriptSubmission(dict(frame)).validate()
            except WindowsProtocolError:
                return
        elif not _valid_partial_transcript(frame):
            return
        turn_id = frame["turn_id"]
        previous = self._seen_sequences.get(turn_id, -1)
        if frame["sequence"] <= previous:
            return
        self._seen_sequences[turn_id] = frame["sequence"]
        text = frame.get("text")
        if not isinstance(text, str):
            return
        if not final:
            self.transcript_changed.emit(text[:8000], False)
            self._set_status("speech_detected", "Listening…")
            return
        if not text or turn_id in self._submitted_turns:
            return
        try:
            self.transport.send_voice_transcript(frame)
        except WindowsProtocolError:
            self._set_status("error", "The final transcript could not be submitted.")
            return
        self._submitted_turns.add(turn_id)
        self.transcript_changed.emit(text, True)
        self._set_status("transcribing", "Transcript submitted through normal chat.")

    def _on_announcement_manifest(
        self,
        sender: str,
        frame: dict[str, Any],
    ) -> None:
        base_fields = {
            "type",
            "schema_version",
            "session_id",
            "generation",
            "media_grant_revision",
            "announcement_id",
            "announcement_sequence",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "transport",
            "worker_identity",
            "sample_rate_hz",
            "duration_samples",
            "track_sid",
            "track_name",
        }
        supplied = set(frame) if isinstance(frame, dict) else set()
        if supplied not in (
            base_fields,
            base_fields | {"result_reserved_samples_after"},
        ):
            return
        sequence = frame.get("announcement_sequence")
        duration = frame.get("duration_samples")
        quantum_index = frame.get("quantum_index")
        if (
            frame.get("type") != "voice_announcement_media"
            or frame.get("schema_version") != "1"
            or sender != self.worker_identity
            or frame.get("worker_identity") != self.worker_identity
            or frame.get("session_id") != self.session_id
            or frame.get("generation") != self.generation
            or frame.get("media_grant_revision") != self.media_grant_revision
            or frame.get("transport") != "livekit"
            or frame.get("sample_rate_hz") != 24_000
            or not _uuid4(frame.get("announcement_id"))
            or not _positive(sequence)
            or sequence <= self._last_announcement_sequence
            or isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 1 <= duration <= _MAX_ANNOUNCEMENT_SAMPLES
            or isinstance(quantum_index, bool)
            or not isinstance(quantum_index, int)
            or not 0 <= quantum_index <= 31
            or frame.get("kind") not in _ANNOUNCEMENT_KINDS
            or not isinstance(frame.get("track_sid"), str)
            or _OPAQUE.fullmatch(frame["track_sid"]) is None
            or not isinstance(frame.get("track_name"), str)
            or _OPAQUE.fullmatch(frame["track_name"]) is None
        ):
            return
        if frame["kind"] == "greeting":
            if frame.get("turn_id") is not None:
                return
        elif not _uuid4(frame.get("turn_id")):
            return
        announcement_id = frame["announcement_id"]
        track_sid = frame["track_sid"]
        track_name = frame["track_name"]
        if (
            announcement_id in self._announcement_ids
            or track_sid in self._announcement_track_sids
            or track_name in self._announcement_track_names
        ):
            return
        role = frame.get("quantum_role")
        reservation = frame.get("result_reserved_samples_after")
        if role == "single":
            if (
                frame["kind"] not in _SINGLE_ANNOUNCEMENT_KINDS
                or quantum_index != 0
                or reservation is not None
            ):
                return
        elif role == "result_opening":
            if (
                frame["kind"] != "result"
                or quantum_index != 0
                or duration > _MAX_RESULT_OPENING_SAMPLES
                or isinstance(reservation, bool)
                or not isinstance(reservation, int)
                or not duration <= reservation <= _MAX_RESULT_OPENING_SAMPLES
                or frame["turn_id"] in self._result_quantum_indexes
            ):
                return
        elif role == "result_continuation":
            turn_id = frame["turn_id"]
            prior_index = self._result_quantum_indexes.get(turn_id)
            prior_reservation = self._result_reserved_samples.get(turn_id)
            if (
                frame["kind"] != "result"
                or quantum_index < 1
                or prior_index is None
                or quantum_index != prior_index + 1
                or isinstance(reservation, bool)
                or not isinstance(reservation, int)
                or prior_reservation is None
                or reservation < prior_reservation + duration
                or reservation > _MAX_RESULT_SAMPLES
            ):
                return
        else:
            return
        self._last_announcement_sequence = sequence
        self._announcement_ids.add(announcement_id)
        self._announcement_track_sids.add(track_sid)
        self._announcement_track_names.add(track_name)
        if frame["kind"] == "result":
            self._result_reserved_samples[frame["turn_id"]] = reservation
            self._result_quantum_indexes[frame["turn_id"]] = quantum_index
        if self.speech_muted or not self._foreground_active:
            return
        self.media.authorize_announcement(dict(frame))

    def _on_media_playout(self, manifest: dict[str, Any], phase: str) -> None:
        connection = self.connection_provider()
        if (
            phase not in {"started", "finished", "interrupted"}
            or not _uuid4(connection)
            or manifest.get("session_id") != self.session_id
            or manifest.get("generation") != self.generation
            or manifest.get("media_grant_revision") != self.media_grant_revision
            or manifest.get("announcement_id") not in self._announcement_ids
        ):
            return
        event = {
            "type": "voice_playout_event",
            "schema_version": "1",
            "device_id": self.device_id,
            "connection_generation": connection,
            "session_id": manifest["session_id"],
            "generation": manifest["generation"],
            "media_grant_revision": manifest["media_grant_revision"],
            "announcement_id": manifest["announcement_id"],
            "announcement_sequence": manifest["announcement_sequence"],
            "turn_id": manifest["turn_id"],
            "kind": manifest["kind"],
            "quantum_role": manifest["quantum_role"],
            "quantum_index": manifest["quantum_index"],
            "phase": phase,
            "client_sequence": self._playout_sequence,
            "observed_at": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
        if "result_reserved_samples_after" in manifest:
            event["result_reserved_samples_after"] = manifest[
                "result_reserved_samples_after"
            ]
        try:
            self.transport.send_voice_playout_event(event)
        except WindowsProtocolError:
            return
        self._playout_sequence += 1
        if phase == "started":
            if manifest["kind"] == "greeting":
                self._set_status("greeting", "Voice greeting is playing.")
            elif manifest["kind"] == "result":
                self._set_status("speaking_result", "Speaking the result summary.")
            else:
                self._set_status("speaking_progress", "Voice update is playing.")
        elif phase == "interrupted" or manifest["kind"] in {
            "greeting",
            "result",
            "failure",
            "refusal",
            "cancellation",
        }:
            self._set_status("listening", "Listening…")
        elif manifest["kind"] == "waiting":
            self._set_status("waiting_on_user", "Waiting for your input.")
        else:
            self._set_status("processing", "Still working…")

    def _reset_announcement_ledger(self) -> None:
        self._last_announcement_sequence = 0
        self._announcement_ids.clear()
        self._announcement_track_sids.clear()
        self._announcement_track_names.clear()
        self._result_reserved_samples.clear()
        self._result_quantum_indexes.clear()
        self._playout_sequence = 0

    def _update_session(self, **changes: Any) -> None:
        if not self._has_session():
            return
        try:
            scope = self._scope()
        except WindowsProtocolError as exc:
            self._set_status("error", str(exc))
            return
        body = {
            "expected_generation": self.generation,
            "expected_media_grant_revision": self.media_grant_revision,
            **changes,
        }
        session_id = self.session_id

        def _work() -> None:
            try:
                with self._session_update_lock:
                    expected_foreground = changes.get("foreground_active")
                    if (
                        self._session_ending
                        or self.session_id != session_id
                        or (
                            isinstance(expected_foreground, bool)
                            and expected_foreground != self._foreground_active
                        )
                    ):
                        return
                    self.http.update(session_id, body, scope)
                    if self.session_id != session_id:
                        return
                    if "microphone_enabled" in changes:
                        self.microphone_enabled = bool(changes["microphone_enabled"])
                        if self._foreground_active:
                            self._foreground_microphone_enabled = self.microphone_enabled
                        self.media.set_microphone_enabled(self.microphone_enabled)
                    if "speech_muted" in changes:
                        self.speech_muted = bool(changes["speech_muted"])
                        if self.speech_muted:
                            self.media.stop_playback()
            except VoiceHttpError as exc:
                self._set_status("error", str(exc))

        self._run_async(_work)

    @Slot()
    def _start_lease_heartbeat(self) -> None:
        if self._foreground_active and self._has_session():
            self._lease_timer.start()

    @Slot()
    def _stop_lease_heartbeat(self) -> None:
        self._lease_timer.stop()

    @Slot()
    def _renew_foreground_lease(self) -> None:
        """Renew the server lease without changing true-idle interaction state."""
        if (
            not self._foreground_active
            or not self._has_session()
            or self._session_ending
            or self._lease_renewal_inflight
        ):
            if not self._foreground_active or not self._has_session():
                self._lease_stop_requested.emit()
            return
        try:
            scope = self._scope()
        except WindowsProtocolError:
            self._lease_stop_requested.emit()
            return
        session_id = self.session_id
        generation = self.generation
        grant_revision = self.media_grant_revision
        body = {
            "expected_generation": generation,
            "expected_media_grant_revision": grant_revision,
            "foreground_active": True,
            "foreground_reason": "foreground",
        }
        self._lease_renewal_inflight = True

        def _work() -> None:
            try:
                with self._session_update_lock:
                    if (
                        not self._foreground_active
                        or self._session_ending
                        or self.session_id != session_id
                        or self.generation != generation
                        or self.media_grant_revision != grant_revision
                    ):
                        return
                    self.http.update(session_id, body, scope)
            except VoiceHttpError as exc:
                if (
                    self.session_id == session_id
                    and self.generation == generation
                    and self.media_grant_revision == grant_revision
                ):
                    self._set_status("error", str(exc))
                    self._lease_stop_requested.emit()
            finally:
                self._lease_renewal_inflight = False

        self._run_async(_work)

    @Slot(object)
    def _on_application_state_changed(self, state: object) -> None:
        self.set_foreground_active(
            state == Qt.ApplicationState.ApplicationActive,
            "foreground"
            if state == Qt.ApplicationState.ApplicationActive
            else "backgrounded",
        )

    def set_foreground_active(self, active: bool, reason: str) -> None:
        """Fence lease renewal and capture to the native app foreground."""
        if active:
            self._foreground_active = True
            if not self._has_session():
                return
            self._update_session(
                foreground_active=True,
                foreground_reason="foreground",
                microphone_enabled=self._foreground_microphone_enabled,
            )
            self._lease_start_requested.emit()
            return
        self._foreground_active = False
        self._lease_stop_requested.emit()
        if not self._has_session():
            return
        self._foreground_microphone_enabled = self.microphone_enabled
        self.media.set_microphone_enabled(False)
        self.media.stop_playback()
        self._update_session(
            foreground_active=False,
            foreground_reason=reason,
            microphone_enabled=False,
        )
        self._set_status("suspended", "Voice paused while the app is in the background.")

    def _stop_speech(self) -> None:
        if not self._has_session():
            return
        # A user interruption is a local realtime action first.  The bounded,
        # generation-fenced server request still runs below and still owns its
        # error semantics, but network latency must never leave stale speech
        # playing after the user has pressed Stop.
        self.media.stop_playback()
        try:
            scope = self._scope()
        except WindowsProtocolError as exc:
            self._set_status("error", str(exc))
            return
        body = {
            "expected_generation": self.generation,
            "expected_media_grant_revision": self.media_grant_revision,
        }
        session_id = self.session_id

        def _work() -> None:
            try:
                self.http.stop_speech(session_id, body, scope)
            except VoiceHttpError as exc:
                self._set_status("error", str(exc))

        self._run_async(_work)

    def _end(self) -> None:
        self._lease_stop_requested.emit()
        if not self._has_session():
            self._teardown("off", "Voice is off.")
            return
        try:
            scope = self._scope()
        except WindowsProtocolError:
            self._teardown("ended", "Voice ended locally.")
            return
        session_id = self.session_id
        generation = self.generation
        grant_revision = self.media_grant_revision
        self._session_ending = True

        def _work() -> None:
            try:
                with self._session_update_lock:
                    self.http.end(session_id, generation, grant_revision, scope)
            except VoiceHttpError:
                pass
            self._teardown("ended", "Voice conversation ended.")

        self._run_async(_work)

    def on_permission_changed(self, permission: str) -> None:
        if permission == "authorized":
            return
        if self._has_session():
            self.set_foreground_active(False, "route_unavailable")
        self._teardown(
            "unavailable",
            "Microphone permission is no longer available; typed chat still works.",
        )

    @Slot(dict)
    def _on_capability_changed(self, capability: dict[str, Any]) -> None:
        if not capability.get("has_microphone"):
            self.on_permission_changed("restricted")
        elif (
            capability.get("microphone_permission") != "authorized"
            and self._has_session()
        ):
            self.on_permission_changed(str(capability.get("microphone_permission")))

    def on_connection_rotated(self, connection: Optional[str]) -> None:
        if connection == self.control_binding_connection:
            return
        self.control_binding = None
        self.control_binding_id = None
        self.control_binding_connection = None
        self.control_binding_expires_at = None
        self._activation_id = None
        if self._has_session():
            self._teardown("reconnecting", "Voice control is reconnecting…")

    def visible_chat_changed(self, chat_id: Optional[str]) -> None:
        if self._has_session() and _uuid4(chat_id) and chat_id != self.visible_chat_id:
            self._update_session(visible_chat_id=chat_id)

    def close(self) -> None:
        self._lease_stop_requested.emit()
        self.media.close()
        self.audio.stop_all()
        self.control_binding = None

    def _has_session(self) -> bool:
        return (
            _uuid4(self.session_id)
            and _positive(self.generation)
            and _positive(self.media_grant_revision)
        )

    def _teardown(self, state: str, message: str) -> None:
        self._lease_stop_requested.emit()
        self.media.close()
        self.audio.stop_all()
        self.session_id = None
        self.generation = None
        self.media_grant_revision = None
        self.worker_identity = None
        self.visible_chat_id = None
        self.chat_context_revision = None
        self.microphone_enabled = False
        self._foreground_microphone_enabled = False
        self.speech_muted = False
        self._seen_sequences.clear()
        self._submitted_turns.clear()
        self._turn_sequences.clear()
        self._reset_announcement_ledger()
        self._activation_id = None
        self._session_ending = False
        self._set_status(state, message)

    def _set_status(self, state: str, message: str) -> None:
        self.state = state if state in _VOICE_STATES else "error"
        self.status_changed.emit(self.state, str(message or self.state)[:240])


__all__ = [
    "LiveKitRoomSession",
    "QtAudioBackend",
    "VoiceComposerWidget",
    "VoiceController",
    "VoiceHttpClient",
    "VoiceHttpError",
]
