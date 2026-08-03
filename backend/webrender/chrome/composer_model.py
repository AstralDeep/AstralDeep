"""Server-owned conversational-voice state for every chat composer.

Clients render this ordered model and invoke its named REST actions. They do
not independently infer ownership, takeover, permission, busy, microphone, or
speech state from local media callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


SCHEMA_VERSION = "1"
OUTPUT_LOCALE = "en-US"

VOICE_STATES = frozenset(
    {
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
)
VOICE_REASONS = frozenset(
    {
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
)
DEVICE_KINDS = frozenset({"web", "windows", "android", "ios", "macos", "watchos"})

_INACTIVE_STATES = frozenset(
    {"off", "unavailable", "suspended", "reconnecting", "error", "ended"}
)
_SPEAKING_STATES = frozenset({"speaking_progress", "speaking_result"})
_MICROPHONE_BUSY_STATES = frozenset({"speech_detected", "transcribing"})
_MICROPHONE_BLOCKED_STATES = frozenset(
    {"connecting", "greeting", "reconnecting", "error", "ended"}
)


def _uuid4(value: str | None, field: str, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID4 string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID4 string") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID4 string")
    return value


@dataclass(frozen=True, slots=True)
class VoiceOwner:
    """Current device owner, already authorized by the session repository."""

    device_id: str
    device_kind: str
    generation: int
    device_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        device_id = _uuid4(self.device_id, "owner_device.device_id", nullable=False)
        if self.device_kind not in DEVICE_KINDS:
            raise ValueError("owner_device.device_kind is unsupported")
        if isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("owner_device.generation must be positive")
        result: dict[str, Any] = {
            "device_id": device_id,
            "device_kind": self.device_kind,
            "generation": self.generation,
        }
        if self.device_label is not None:
            if not isinstance(self.device_label, str) or len(self.device_label) > 80:
                raise ValueError("owner_device.device_label is invalid")
            result["device_label"] = self.device_label
        return result


@dataclass(frozen=True, slots=True)
class VoiceComposerContext:
    """Authorized inputs used to derive one deterministic composer frame."""

    revision: int
    connection_generation: str
    local_device_id: str
    available: bool
    state: str
    reason: str
    speech_muted: bool = False
    microphone_enabled: bool = False
    foreground_active: bool = False
    chat_context_revision: int | None = None
    applied_chat_context_revision: int | None = None
    session_id: str | None = None
    generation: int | None = None
    media_grant_revision: int | None = None
    visible_chat_id: str | None = None
    selected_chat_id: str | None = None
    foreground_turn_id: str | None = None
    owner_device: VoiceOwner | None = None
    idle_expires_at: str | None = None
    message: str | None = None
    sensitive_recap_pending: bool = False


@dataclass(frozen=True, slots=True)
class ComposerControl:
    key: str
    action: str
    label: str
    icon: str
    visible: bool
    enabled: bool
    pressed: bool = False
    busy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "action": self.action,
            "label": self.label,
            "icon": self.icon,
            "visible": self.visible,
            "enabled": self.enabled,
            "pressed": self.pressed,
            "busy": self.busy,
        }


def build_composer_state(context: VoiceComposerContext) -> dict[str, Any]:
    """Build the canonical ordered voice composer frame."""

    _validate_context(context)
    owner = context.owner_device
    owns_session = owner is not None and owner.device_id == context.local_device_id
    has_session = context.session_id is not None
    active_owner = owns_session and context.foreground_active
    speaking = context.state in _SPEAKING_STATES
    chat_update_needed = (
        context.selected_chat_id is not None
        and (
            context.selected_chat_id != context.visible_chat_id
            or context.applied_chat_context_revision != context.chat_context_revision
        )
    )
    controls = (
        ComposerControl(
            "voice-start",
            "voice_session_start",
            "Start voice conversation",
            "microphone",
            visible=not has_session,
            enabled=not has_session and context.available,
            busy=context.state == "connecting",
        ),
        ComposerControl(
            "voice-takeover",
            "voice_session_takeover",
            "Take over voice session",
            "device-transfer",
            visible=has_session and not owns_session,
            enabled=has_session and not owns_session and context.available,
            busy=False,
        ),
        ComposerControl(
            "voice-end",
            "voice_session_end",
            "End voice conversation",
            "stop",
            visible=active_owner,
            enabled=active_owner,
        ),
        ComposerControl(
            "voice-microphone",
            "voice_microphone_set",
            "Microphone",
            "microphone",
            visible=active_owner,
            enabled=active_owner and context.state not in _MICROPHONE_BLOCKED_STATES,
            pressed=context.microphone_enabled,
            busy=context.state in _MICROPHONE_BUSY_STATES,
        ),
        ComposerControl(
            "voice-stop-speech",
            "voice_speech_stop",
            "Stop speaking",
            "speaker-stop",
            visible=active_owner and speaking,
            enabled=active_owner and speaking,
        ),
        ComposerControl(
            "voice-mute",
            "voice_speech_mute_set",
            "Mute assistant speech",
            "speaker-muted",
            visible=active_owner,
            enabled=active_owner,
            pressed=context.speech_muted,
        ),
        ComposerControl(
            "voice-chat-context",
            "voice_visible_chat_update",
            "Update voice chat",
            "chat",
            visible=active_owner,
            enabled=active_owner and chat_update_needed,
        ),
        ComposerControl(
            "voice-sensitive-recap",
            "voice_sensitive_recap_request",
            "Read sensitive result",
            "speaker-consent",
            visible=active_owner and context.sensitive_recap_pending,
            enabled=active_owner and context.sensitive_recap_pending,
        ),
    )
    voice: dict[str, Any] = {
        "available": context.available,
        "state": context.state,
        "speech_muted": context.speech_muted,
        "microphone_enabled": context.microphone_enabled,
        "foreground_active": context.foreground_active,
        "reason": context.reason,
        "output_locale": OUTPUT_LOCALE,
        "chat_context_revision": context.chat_context_revision,
        "applied_chat_context_revision": context.applied_chat_context_revision,
        "chat_context_synced": (
            context.chat_context_revision is not None
            and context.applied_chat_context_revision == context.chat_context_revision
        ),
        "session_id": context.session_id,
        "generation": context.generation,
        "media_grant_revision": context.media_grant_revision,
        "visible_chat_id": context.visible_chat_id,
        "foreground_turn_id": context.foreground_turn_id,
        "owner_device": owner.to_dict() if owner is not None else None,
        "idle_expires_at": context.idle_expires_at,
        "controls": [control.to_dict() for control in controls],
    }
    if context.message is not None:
        voice["message"] = context.message
    return {
        "type": "composer_state",
        "schema_version": SCHEMA_VERSION,
        "revision": context.revision,
        "connection_generation": context.connection_generation,
        "voice": voice,
    }


def _validate_context(context: VoiceComposerContext) -> None:
    if isinstance(context.revision, bool) or context.revision < 0:
        raise ValueError("revision must be non-negative")
    _uuid4(context.connection_generation, "connection_generation", nullable=False)
    _uuid4(context.local_device_id, "local_device_id", nullable=False)
    if context.state not in VOICE_STATES:
        raise ValueError("unsupported voice state")
    if context.reason not in VOICE_REASONS:
        raise ValueError("unsupported voice reason")
    if not context.foreground_active and context.microphone_enabled:
        raise ValueError("an inactive composer cannot enable its microphone")
    if not context.foreground_active and context.state not in _INACTIVE_STATES:
        raise ValueError("inactive composer has an active voice state")
    for field in (
        "chat_context_revision",
        "applied_chat_context_revision",
        "generation",
        "media_grant_revision",
    ):
        value = getattr(context, field)
        if value is not None and (isinstance(value, bool) or value < 1):
            raise ValueError(f"{field} must be positive")
    for field in (
        "session_id",
        "visible_chat_id",
        "selected_chat_id",
        "foreground_turn_id",
    ):
        _uuid4(getattr(context, field), field)
    if context.foreground_active and (
        context.session_id is None
        or context.owner_device is None
        or context.generation is None
        or context.media_grant_revision is None
    ):
        raise ValueError("foreground voice state is missing session ownership")
    if context.message is not None and (
        not isinstance(context.message, str) or len(context.message) > 240
    ):
        raise ValueError("message is invalid")


__all__ = [
    "ComposerControl",
    "VoiceComposerContext",
    "VoiceOwner",
    "build_composer_state",
]
