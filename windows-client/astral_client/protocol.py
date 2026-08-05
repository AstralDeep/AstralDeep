"""WebSocket client for the AstralDeep orchestrator.

Speaks the exact client protocol: connects to ws://<host>/ws, sends `register_ui`
(token + device caps) first, then streams JSON messages. Inbound messages are
delivered to the Qt main thread via the `message` signal; outbound `ui_event` /
`chat_message` are sent thread-safely onto the asyncio loop.

Feature 044 (FR-003): the transport owns the connection lifecycle — it
auto-reconnects after a drop with exponential backoff (1 s base, x2, 30 s cap,
reset on a successful open), buffers outbound frames composed while
disconnected in a bounded queue flushed FIFO on (re)connect, and surfaces every
state change through the `status` signal so the app can keep the connection
state visible. Queue overflow is never silent: the oldest frame is dropped AND
a `send_dropped:` status is emitted for the UI to surface.

Runs the asyncio websocket loop in a daemon thread so the Qt UI stays responsive.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import re
import threading
import unicodedata
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

import websockets
from PySide6.QtCore import QObject, QSettings, Signal

from . import __version__


_MAX_UINT64 = (1 << 64) - 1
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VOICE_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

RUNTIME_CONTRACT_VERSIONS = (2,)
RUNTIME_LOCK_ARTIFACT = "requirements-release.lock.txt"
RUNTIME_LOCK_SHA256 = "82d58a54a8cd1a7ffd925d724ba247f0cfb09e4de0747aeca4a869f7dd87ba35"

VOICE_DEVICE_ID_KEY = "astraldeep.voice.device_id.v1"
VOICE_TRANSCRIPT_TOPIC = "astraldeep.voice.transcript.v1"
MAX_PENDING_VOICE_FINALS = 4
MAX_PENDING_VOICE_BYTES = 48 * 1024
_VOICE_PLAYOUT_KINDS = frozenset(
    {
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
)
_VOICE_SINGLE_KINDS = _VOICE_PLAYOUT_KINDS - {"result"}


class WindowsProtocolError(ValueError):
    """An inbound or outbound feature-060 wire value failed closed."""


def _uuid4(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise WindowsProtocolError(f"{name} must be a UUID4 string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise WindowsProtocolError(f"{name} must be a UUID4 string") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise WindowsProtocolError(f"{name} must be a canonical UUID4 string")
    return value


def _uint64(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_UINT64
    ):
        raise WindowsProtocolError(f"{name} must be an unsigned 64-bit integer")
    return value


def _utc(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WindowsProtocolError(f"{name} must be an RFC3339 UTC string")
    try:
        datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise WindowsProtocolError(f"{name} must be an RFC3339 UTC string") from exc
    return value


def _exact(data: dict[str, Any], expected: set[str], frame_type: str) -> None:
    if set(data) != expected:
        raise WindowsProtocolError(f"{frame_type} fields do not match the contract")


def _is_uuid4(value: object) -> bool:
    try:
        _uuid4(value, "value")
    except WindowsProtocolError:
        return False
    return True


def load_or_create_voice_device_id(settings: Optional[QSettings] = None) -> str:
    """Return one stable, non-secret installation UUID4 for voice ownership."""

    store = settings or QSettings("AstralDeep", "WindowsClient")
    current = store.value(VOICE_DEVICE_ID_KEY, "", type=str) or ""
    if _is_uuid4(current):
        return current
    device_id = str(uuid.uuid4())
    store.setValue(VOICE_DEVICE_ID_KEY, device_id)
    store.sync()
    return device_id


def _validate_semantic_json(value: object, name: str = "semantic value") -> None:
    """Validate JSON semantics and reject web-only presentation authority.

    Python's JSON decoder accepts non-finite numbers by default. Native
    continuity state must fail closed on those values and on any nested
    ``_presentation`` member, which is reserved exclusively for web sockets.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WindowsProtocolError(f"{name} contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_semantic_json(item, name)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WindowsProtocolError(f"{name} contains a non-string key")
            if key == "_presentation":
                raise WindowsProtocolError("native semantic state contains web presentation")
            _validate_semantic_json(item, name)
        return
    raise WindowsProtocolError(f"{name} is not JSON-compatible")


def _validate_component(component: object) -> dict[str, Any]:
    if (
        not isinstance(component, dict)
        or not isinstance(component.get("type"), str)
        or not component["type"]
    ):
        raise WindowsProtocolError("semantic component is invalid")
    _validate_semantic_json(component, "semantic component")
    return component


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise WindowsProtocolError("semantic value is not canonical JSON") from exc


def decode_token_account(token: str) -> Optional[tuple[str, str]]:
    """Read unverified ``iss``/``sub`` claims only to namespace local storage.

    This helper is not an authentication decision. The orchestrator remains
    solely responsible for verifying the token and authorizing chat access.
    """

    if not isinstance(token, str):
        return None
    pieces = token.split(".")
    if len(pieces) != 3:
        return None
    try:
        encoded = pieces[1] + "=" * (-len(pieces[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(claims, dict):
        return None
    issuer = claims.get("iss")
    subject = claims.get("sub")
    if not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject:
        return None
    return issuer, subject


class ConversationResumeStore:
    """Account-scoped, non-credential active-chat locator in ``QSettings``.

    The account identity appears only as a SHA-256 digest in the key. Values
    are exact version-one JSON and synchronous ``sync()`` calls make selection
    durable before load, registration, or presentation changes continue.
    Unknown schemas and malformed future values are retained but never used.
    """

    PREFIX = "astraldeep.active_chat.v1."
    ACCOUNT_KEY = "astraldeep.active_chat.account.v1"
    CLEAR_REASONS = frozenset(
        {
            "explicit_new_chat",
            "definitive_sign_out",
            "account_switch",
            "confirmed_deletion",
        }
    )

    def __init__(self, settings: Optional[QSettings] = None):
        self.settings = settings or QSettings("AstralDeep", "WindowsClient")
        self.storage_key: Optional[str] = None

    @staticmethod
    def account_key(issuer: str, subject: str) -> str:
        if not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject:
            raise WindowsProtocolError("issuer and subject must be non-empty strings")
        digest = hashlib.sha256(
            issuer.encode("utf-8") + b"\x00" + subject.encode("utf-8")
        ).hexdigest()
        return f"{ConversationResumeStore.PREFIX}{digest}"

    def bind_account(self, issuer: str, subject: str) -> str:
        """Select an account key, clearing a definitively switched account."""

        next_key = self.account_key(issuer, subject)
        previous = self.settings.value(self.ACCOUNT_KEY, "", type=str) or ""
        if previous and previous != next_key:
            self.settings.remove(previous)
        self.storage_key = next_key
        self.settings.setValue(self.ACCOUNT_KEY, next_key)
        self.settings.sync()
        return next_key

    def bind_token(self, token: str) -> bool:
        identity = decode_token_account(token)
        if identity is None:
            self.storage_key = None
            return False
        self.bind_account(*identity)
        return True

    def active_chat(self) -> Optional[str]:
        if self.storage_key is None:
            return None
        raw = self.settings.value(self.storage_key, "", type=str) or ""
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "chat_id", "updated_at"}
            or value.get("schema_version") != 1
            or not _is_uuid4(value.get("chat_id"))
        ):
            return None
        try:
            _utc(value.get("updated_at"), "updated_at")
        except WindowsProtocolError:
            return None
        return value["chat_id"]

    def set_active_chat(self, chat_id: str) -> bool:
        if self.storage_key is None:
            return False
        _uuid4(chat_id, "chat_id")
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        value = {
            "schema_version": 1,
            "chat_id": chat_id,
            "updated_at": updated_at,
        }
        self.settings.setValue(self.storage_key, _stable_json(value))
        self.settings.sync()
        return True

    def clear(self, reason: str, chat_id: Optional[str] = None) -> bool:
        """Clear only one of the four definitive contract events."""

        if reason not in self.CLEAR_REASONS or self.storage_key is None:
            return False
        if reason == "confirmed_deletion" and chat_id != self.active_chat():
            return False
        self.settings.remove(self.storage_key)
        self.settings.sync()
        return True


# 066 T023 contract extension: the closed set of variants a canonical text
# part may carry (mirrors backend shared/protocol.py CANONICAL_TEXT_PART_VARIANTS).
CANONICAL_TEXT_PART_VARIANTS = frozenset({"caption"})


@dataclass(frozen=True)
class SemanticPart:
    """One validated canonical transcript part in original wire order."""

    type: str
    text: Optional[str] = None
    components: tuple[dict[str, Any], ...] = ()
    value: Any = None
    plain_text: Optional[str] = None
    code: Optional[str] = None
    message: Optional[str] = None
    variant: Optional[str] = None


@dataclass(frozen=True)
class SemanticMessage:
    """One visible, validated canonical transcript message."""

    message_id: str
    role: str
    created_at: str
    parts: tuple[SemanticPart, ...]
    attachments: tuple[dict[str, Any], ...]


def decode_semantic_transcript(transcript: object) -> list[SemanticMessage]:
    """Decode canonical transcript forms without language debug formatting."""

    if not isinstance(transcript, list):
        raise WindowsProtocolError("transcript must be an array")
    decoded: list[SemanticMessage] = []
    message_fields = {"message_id", "role", "created_at", "parts", "attachments"}
    for message in transcript:
        if not isinstance(message, dict) or set(message) != message_fields:
            raise WindowsProtocolError("transcript message fields are invalid")
        message_id = message.get("message_id")
        role = message.get("role")
        if not isinstance(message_id, str) or not message_id:
            raise WindowsProtocolError("message_id must be non-empty")
        if role not in {"user", "assistant", "system", "tool"}:
            raise WindowsProtocolError("transcript role is invalid")
        _utc(message.get("created_at"), "created_at")
        attachments = message.get("attachments")
        if not isinstance(attachments, list):
            raise WindowsProtocolError("attachments must be an array")
        safe_attachments: list[dict[str, Any]] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise WindowsProtocolError("attachment must be an object")
            _validate_semantic_json(attachment, "attachment")
            safe_attachments.append(copy.deepcopy(attachment))
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            raise WindowsProtocolError("transcript parts must be non-empty")
        safe_parts: list[SemanticPart] = []
        visible = bool(safe_attachments)
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise WindowsProtocolError("transcript part is invalid")
            part_type = part["type"]
            if part_type == "text":
                # 066 T023: exactly {type, text} plus an OPTIONAL bounded variant.
                if set(part) == {"type", "text", "variant"}:
                    if part.get("variant") not in CANONICAL_TEXT_PART_VARIANTS:
                        raise WindowsProtocolError(
                            "text part variant is outside the canonical set"
                        )
                else:
                    _exact(part, {"type", "text"}, "text part")
                if not isinstance(part.get("text"), str):
                    raise WindowsProtocolError("text part must contain text")
                safe_parts.append(
                    SemanticPart(
                        type="text", text=part["text"], variant=part.get("variant")
                    )
                )
                visible = visible or bool(part["text"])
            elif part_type == "components":
                _exact(part, {"type", "components"}, "components part")
                components = part.get("components")
                if not isinstance(components, list):
                    raise WindowsProtocolError("components part must contain an array")
                safe_components = tuple(
                    copy.deepcopy(_validate_component(component))
                    for component in components
                )
                safe_parts.append(
                    SemanticPart(type="components", components=safe_components)
                )
                visible = visible or bool(safe_components)
            elif part_type == "structured":
                _exact(part, {"type", "value", "plain_text"}, "structured part")
                if not isinstance(part.get("plain_text"), str):
                    raise WindowsProtocolError("structured plain_text must be a string")
                _validate_semantic_json(part.get("value"), "structured value")
                safe_parts.append(
                    SemanticPart(
                        type="structured",
                        value=copy.deepcopy(part.get("value")),
                        plain_text=part["plain_text"],
                    )
                )
                visible = visible or bool(part["plain_text"])
            elif part_type == "recovery":
                _exact(part, {"type", "code", "message"}, "recovery part")
                if (
                    not isinstance(part.get("code"), str)
                    or not part["code"]
                    or not isinstance(part.get("message"), str)
                    or not part["message"]
                ):
                    raise WindowsProtocolError("recovery part is invalid")
                safe_parts.append(
                    SemanticPart(
                        type="recovery",
                        code=part["code"],
                        message=part["message"],
                    )
                )
                visible = True
            else:
                raise WindowsProtocolError("transcript part type is invalid")
        if not visible:
            safe_parts.append(
                SemanticPart(
                    type="recovery",
                    code="saved_content_unrenderable",
                    message="A saved response could not be displayed.",
                )
            )
        decoded.append(
            SemanticMessage(
                message_id=message_id,
                role=role,
                created_at=message["created_at"],
                parts=tuple(safe_parts),
                attachments=tuple(safe_attachments),
            )
        )
    return decoded


@dataclass(frozen=True)
class ConversationSnapshot:
    """Complete committed transcript and canvas received in one frame."""

    schema_version: int
    snapshot_id: str
    chat_id: str
    connection_generation: str
    request_generation: str
    snapshot_purpose: str
    render_revision: int
    committed_at: str
    transcript: list[dict[str, Any]]
    canvas: dict[str, Any]
    type: str = "conversation_snapshot"

    def validate(self) -> None:
        if (
            self.type != "conversation_snapshot"
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise WindowsProtocolError("conversation snapshot version/type is invalid")
        for name in (
            "snapshot_id",
            "chat_id",
            "connection_generation",
            "request_generation",
        ):
            _uuid4(getattr(self, name), name)
        if self.snapshot_purpose not in {"hydration", "commit"}:
            raise WindowsProtocolError("snapshot_purpose is invalid")
        _uint64(self.render_revision, "render_revision")
        _utc(self.committed_at, "committed_at")
        decode_semantic_transcript(self.transcript)
        if (
            not isinstance(self.canvas, dict)
            or set(self.canvas) != {"target", "components"}
            or self.canvas.get("target") != "canvas"
            or not isinstance(self.canvas.get("components"), list)
        ):
            raise WindowsProtocolError("canvas must be one complete canvas object")
        for component in self.canvas["components"]:
            _validate_component(component)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationSnapshot":
        _exact(
            data,
            {
                "type",
                "schema_version",
                "snapshot_id",
                "chat_id",
                "connection_generation",
                "request_generation",
                "snapshot_purpose",
                "render_revision",
                "committed_at",
                "transcript",
                "canvas",
            },
            "conversation_snapshot",
        )
        model = cls(**data)
        model.validate()
        return model


@dataclass(frozen=True)
class ConversationCommitReady:
    """Prelude binding a detached commit snapshot to the active connection."""

    schema_version: int
    chat_id: str
    connection_generation: str
    request_generation: str
    render_revision: int
    type: str = "conversation_commit_ready"

    def validate(self) -> None:
        if (
            self.type != "conversation_commit_ready"
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise WindowsProtocolError("conversation commit prelude version/type is invalid")
        _uuid4(self.chat_id, "chat_id")
        _uuid4(self.connection_generation, "connection_generation")
        _uuid4(self.request_generation, "request_generation")
        _uint64(self.render_revision, "render_revision")
        if self.render_revision == 0:
            raise WindowsProtocolError("conversation commit render_revision must be positive")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationCommitReady":
        _exact(
            data,
            {
                "type",
                "schema_version",
                "chat_id",
                "connection_generation",
                "request_generation",
                "render_revision",
            },
            "conversation_commit_ready",
        )
        model = cls(**data)
        model.validate()
        return model


@dataclass
class _ConversationRequest:
    generation: str
    purpose: str
    hydration_applied: bool = False
    accepted_snapshot_id: Optional[str] = None
    accepted_canonical: Optional[str] = None
    last_frame_sequence: int = 0
    snapshot_applied: bool = False
    expected_render_revision: Optional[int] = None


class ConversationContinuityReducer:
    """Purpose-aware atomic snapshot and disposable-overlay reducer.

    The reducer owns only protocol state. Qt widgets are changed by the app
    after ``snapshot_applied`` returns, so invalid or stale frames cannot
    partially mutate either committed surface.
    """

    def __init__(self) -> None:
        self.active_chat_id: Optional[str] = None
        self.connection_generation: Optional[str] = None
        self._request: Optional[_ConversationRequest] = None
        self._revisions: dict[str, int] = {}
        self._committed: dict[str, ConversationSnapshot] = {}
        self._last_snapshot_ids: dict[str, str] = {}
        self._seen_snapshot_ids: dict[str, set[str]] = {}
        self._used_requests: set[tuple[str, str]] = set()
        self.overlay_frames: list[dict[str, Any]] = []

    @property
    def request_generation(self) -> Optional[str]:
        return self._request.generation if self._request is not None else None

    @property
    def request_purpose(self) -> Optional[str]:
        return self._request.purpose if self._request is not None else None

    @property
    def last_committed_render_revision(self) -> int:
        if self.active_chat_id is None:
            return 0
        return self._revisions.get(self.active_chat_id, 0)

    @property
    def committed_snapshot(self) -> Optional[ConversationSnapshot]:
        if self.active_chat_id is None:
            return None
        return self._committed.get(self.active_chat_id)

    def activate_chat(self, chat_id: Optional[str]) -> None:
        if chat_id is not None:
            _uuid4(chat_id, "chat_id")
        preserve_unbound_commit = (
            self.active_chat_id is None
            and chat_id is not None
            and self._request is not None
            and self._request.purpose == "commit"
        )
        if chat_id != self.active_chat_id and not preserve_unbound_commit:
            self._request = None
            self.overlay_frames.clear()
        self.active_chat_id = chat_id

    def clear_chat(self, chat_id: Optional[str] = None, *, all_accounts: bool = False) -> None:
        target = chat_id or self.active_chat_id
        if all_accounts:
            self._revisions.clear()
            self._committed.clear()
            self._last_snapshot_ids.clear()
            self._seen_snapshot_ids.clear()
        elif target is not None:
            self._revisions.pop(target, None)
            self._committed.pop(target, None)
            self._last_snapshot_ids.pop(target, None)
            self._seen_snapshot_ids.pop(target, None)
        if target == self.active_chat_id or all_accounts:
            self.active_chat_id = None
            self._request = None
            self.overlay_frames.clear()

    def bind_connection(self, connection_generation: str) -> None:
        _uuid4(connection_generation, "connection_generation")
        if connection_generation != self.connection_generation:
            self.connection_generation = connection_generation
            self._request = None
            self.overlay_frames.clear()

    def open_request(
        self,
        purpose: str,
        request_generation: str,
        *,
        expected_render_revision: Optional[int] = None,
    ) -> None:
        if purpose not in {"hydration", "commit"}:
            raise WindowsProtocolError("conversation request purpose is invalid")
        _uuid4(request_generation, "request_generation")
        if self.connection_generation is not None:
            identity = (self.connection_generation, request_generation)
            if identity in self._used_requests:
                raise WindowsProtocolError("request generation was already used")
            self._used_requests.add(identity)
        if expected_render_revision is not None:
            _uint64(expected_render_revision, "expected_render_revision")
        self._request = _ConversationRequest(
            generation=request_generation,
            purpose=purpose,
            expected_render_revision=expected_render_revision,
        )
        self.overlay_frames.clear()

    def reduce_commit_ready(self, frame: dict[str, Any]) -> str:
        try:
            ready = ConversationCommitReady.from_dict(frame)
        except (WindowsProtocolError, TypeError):
            return "invalid_commit_ready"
        if (
            ready.chat_id != self.active_chat_id
            or ready.connection_generation != self.connection_generation
        ):
            return "wrong_scope"
        if ready.render_revision <= self.last_committed_render_revision:
            return "non_increasing_commit_ready"
        identity = (ready.connection_generation, ready.request_generation)
        if identity in self._used_requests:
            return "reused_request_generation"
        try:
            self.open_request(
                "commit",
                ready.request_generation,
                expected_render_revision=ready.render_revision,
            )
        except WindowsProtocolError:
            return "invalid_commit_ready"
        return "commit_ready"

    def reduce_snapshot(self, frame: dict[str, Any]) -> str:
        try:
            snapshot = ConversationSnapshot.from_dict(frame)
            canonical = _stable_json(frame)
        except (WindowsProtocolError, TypeError):
            return "invalid_snapshot"
        request = self._request
        if (
            request is None
            or snapshot.chat_id != self.active_chat_id
            or snapshot.connection_generation != self.connection_generation
            or snapshot.request_generation != request.generation
        ):
            return "wrong_scope"
        if snapshot.snapshot_purpose != request.purpose:
            return "wrong_purpose"
        if (
            request.expected_render_revision is not None
            and snapshot.render_revision != request.expected_render_revision
        ):
            return "wrong_revision"
        committed = self.last_committed_render_revision
        if snapshot.render_revision < committed:
            return "stale_frame_ignored"
        if snapshot.render_revision == committed and request.purpose != "hydration":
            return "unexpected_equal_commit"
        if snapshot.render_revision == committed and request.hydration_applied:
            if (
                snapshot.snapshot_id == request.accepted_snapshot_id
                and canonical == request.accepted_canonical
            ):
                return "snapshot_replay"
            return "revision_conflict"
        seen = self._seen_snapshot_ids.setdefault(snapshot.chat_id, set())
        if (
            self._last_snapshot_ids.get(snapshot.chat_id) == snapshot.snapshot_id
            or snapshot.snapshot_id in seen
        ):
            return "revision_conflict"
        self._revisions[snapshot.chat_id] = snapshot.render_revision
        self._committed[snapshot.chat_id] = snapshot
        self._last_snapshot_ids[snapshot.chat_id] = snapshot.snapshot_id
        seen.add(snapshot.snapshot_id)
        request.accepted_snapshot_id = snapshot.snapshot_id
        request.accepted_canonical = canonical
        request.snapshot_applied = True
        if request.purpose == "hydration":
            request.hydration_applied = True
        self.overlay_frames.clear()
        return "snapshot_applied"

    def reduce_transient(self, frame: dict[str, Any]) -> str:
        request = self._request
        try:
            if frame.get("type") not in {
                "ui_render",
                "ui_update",
                "ui_upsert",
                "ui_append",
                "ui_stream_data",
            }:
                raise WindowsProtocolError("frame is not a transient render")
            _uuid4(frame.get("chat_id"), "chat_id")
            _uuid4(frame.get("connection_generation"), "connection_generation")
            _uuid4(frame.get("request_generation"), "request_generation")
            _uint64(frame.get("base_render_revision"), "base_render_revision")
            sequence = _uint64(frame.get("frame_sequence"), "frame_sequence")
            if "components" in frame:
                components = frame["components"]
                if not isinstance(components, list):
                    raise WindowsProtocolError("transient components must be an array")
                for component in components:
                    _validate_component(component)
        except WindowsProtocolError:
            return "transient_frame_ignored"
        if (
            request is None
            or request.snapshot_applied
            or frame["chat_id"] != self.active_chat_id
            or frame["connection_generation"] != self.connection_generation
            or frame["request_generation"] != request.generation
            or frame["base_render_revision"] != self.last_committed_render_revision
            or sequence <= request.last_frame_sequence
        ):
            return "transient_frame_ignored"
        request.last_frame_sequence = sequence
        self.overlay_frames.append(copy.deepcopy(frame))
        return "transient_overlay_applied"

    def clear_transient(self) -> None:
        self.overlay_frames.clear()


@dataclass(frozen=True)
class OperationStatus:
    """Server-owned operation state; local submitting is never represented here."""

    operation_id: str
    action: str
    surface: str
    chat_id: Optional[str]
    connection_generation: str
    request_generation: str
    sequence: int
    state: str
    phase: str
    label: str
    terminal: bool
    retryable: bool
    error: Optional[dict[str, Any]]
    retry_after_ms: Optional[int]
    updated_at: str
    type: str = "operation_status"

    _FLAGS: ClassVar[dict[str, tuple[bool, bool]]] = {
        "accepted": (False, False),
        "validating": (False, False),
        "persisting": (False, False),
        "running": (False, False),
        "completed": (True, False),
        "failed": (True, False),
        "cancelled": (True, False),
        "retryable": (True, True),
    }
    _ERROR_CODES: ClassVar[frozenset[str]] = frozenset(
        {
            "invalid_input",
            "validation_failed",
            "provider_unavailable",
            "network_unavailable",
            "deadline_exceeded",
            "capacity_exceeded",
            "queue_wait_expired",
            "registration_timeout",
            "disconnected",
            "cancelled_by_user",
            "operation_failed",
            "conflict",
            "incompatible_runtime",
            "agent_offline",
            "stale_generation",
        }
    )

    def validate(self) -> None:
        if self.type != "operation_status":
            raise WindowsProtocolError("operation_status type is invalid")
        _uuid4(self.operation_id, "operation_id")
        for name in ("action", "surface", "phase"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SNAKE_CASE.fullmatch(value) is None:
                raise WindowsProtocolError(f"{name} must be snake case")
        if self.chat_id is not None:
            _uuid4(self.chat_id, "chat_id")
        _uuid4(self.connection_generation, "connection_generation")
        _uuid4(self.request_generation, "request_generation")
        _uint64(self.sequence, "sequence")
        if not isinstance(self.label, str) or not self.label.strip():
            raise WindowsProtocolError("label must be non-empty")
        if self._FLAGS.get(self.state) != (self.terminal, self.retryable):
            raise WindowsProtocolError("operation state flags disagree")
        needs_error = self.state in {"failed", "cancelled", "retryable"}
        if needs_error:
            if (
                not isinstance(self.error, dict)
                or set(self.error) != {"code", "message"}
                or not all(isinstance(value, str) and value for value in self.error.values())
            ):
                raise WindowsProtocolError("terminal operation error is invalid")
            if self.error["code"] not in self._ERROR_CODES:
                raise WindowsProtocolError("terminal operation error code is not canonical")
        elif self.error is not None:
            raise WindowsProtocolError("operation error must be null")
        if self.retry_after_ms is not None:
            _uint64(self.retry_after_ms, "retry_after_ms")
            if self.state != "retryable":
                raise WindowsProtocolError("retry_after_ms requires retryable state")
        _utc(self.updated_at, "updated_at")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationStatus":
        _exact(data, {item.name for item in fields(cls)}, "operation_status")
        model = cls(**data)
        model.validate()
        return model


@dataclass(frozen=True)
class AdmissionRefusal:
    """Exact pre-admission refusal correlated to one client submission."""

    submission_id: str
    accepted: bool
    code: str
    message: str
    retryable: bool
    retry_after_ms: Optional[int]
    type: str = "error"

    _CODES: ClassVar[frozenset[str]] = frozenset(
        {
            "capacity_exceeded",
            "registration_required",
            "registration_timeout",
            "idempotency_conflict",
            "connection_closing",
            "service_draining",
            "invalid_input",
            "registration_queue_full",
            "operation_failed",
        }
    )

    def validate(self) -> None:
        if self.type != "error" or self.accepted is not False:
            raise WindowsProtocolError("admission refusal discriminator is invalid")
        _uuid4(self.submission_id, "submission_id")
        if self.code not in self._CODES:
            raise WindowsProtocolError("admission refusal code is not canonical")
        if not isinstance(self.message, str) or not self.message.strip():
            raise WindowsProtocolError("admission refusal message must be non-empty")
        if not isinstance(self.retryable, bool):
            raise WindowsProtocolError("admission refusal retryable must be boolean")
        if self.retry_after_ms is not None:
            _uint64(self.retry_after_ms, "retry_after_ms")
            if not self.retryable:
                raise WindowsProtocolError(
                    "admission refusal retry_after_ms requires retryable"
                )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdmissionRefusal":
        _exact(data, {item.name for item in fields(cls)}, "admission refusal")
        model = cls(**data)
        model.validate()
        return model


@dataclass(frozen=True)
class AgentLifecycle:
    """One generation-fenced projection of the authoritative agent runtime."""

    agent_id: str
    revision_id: Optional[str]
    runtime_instance_id: Optional[str]
    lifecycle_generation: int
    state_revision: int
    state: str
    reason_code: Optional[str]
    label: str
    updated_at: str
    type: str = "agent_lifecycle"

    _REASON_CODES: ClassVar[frozenset[str]] = frozenset(
        {
            "invalid_host_registration",
            "runtime_contract_unsupported",
            "runtime_lock_mismatch",
            "bundle_digest_mismatch",
            "bundle_install_failed",
            "child_start_failed",
            "child_registration_timeout",
            "child_exited",
            "child_hung",
            "host_lost",
            "agent_offline",
            "agent_deleted",
            "stale_runtime_generation",
            "revision_promotion_failed",
            "inventory_required",
            "process_cleanup_timeout",
        }
    )

    def validate(self) -> None:
        if self.type != "agent_lifecycle" or not self.agent_id:
            raise WindowsProtocolError("agent lifecycle type/identity is invalid")
        if self.revision_id is not None:
            _uuid4(self.revision_id, "revision_id")
        if self.runtime_instance_id is not None:
            _uuid4(self.runtime_instance_id, "runtime_instance_id")
        _uint64(self.lifecycle_generation, "lifecycle_generation")
        _uint64(self.state_revision, "state_revision")
        if self.state not in {"starting", "online", "updating", "failed", "offline"}:
            raise WindowsProtocolError("agent lifecycle state is invalid")
        if self.state in {"starting", "online", "updating"} and (
            self.revision_id is None or self.runtime_instance_id is None
        ):
            raise WindowsProtocolError(
                "active lifecycle state requires revision and runtime instance"
            )
        if self.reason_code is not None:
            if (
                not isinstance(self.reason_code, str)
                or self.reason_code not in self._REASON_CODES
            ):
                raise WindowsProtocolError("reason_code is not canonical")
        if not self.label:
            raise WindowsProtocolError("label must be non-empty")
        _utc(self.updated_at, "updated_at")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentLifecycle":
        _exact(data, {item.name for item in fields(cls)}, "agent_lifecycle")
        model = cls(**data)
        model.validate()
        return model


@dataclass(frozen=True)
class LocalOperationSubmission:
    """Immediate client-only projection created before outbound socket I/O."""

    submission_id: str
    request_generation: str
    action: str
    chat_id: Optional[str]
    label: str = "Submitting…"

    def validate(self) -> None:
        _uuid4(self.submission_id, "submission_id")
        _uuid4(self.request_generation, "request_generation")
        if _SNAKE_CASE.fullmatch(self.action) is None:
            raise WindowsProtocolError("submission action must be snake case")
        if self.chat_id is not None:
            _uuid4(self.chat_id, "chat_id")
        if not isinstance(self.label, str) or not self.label.strip():
            raise WindowsProtocolError("submission label must be non-empty")


@dataclass(frozen=True)
class VoiceTranscriptSubmission:
    """One worker-bound final retained only until correlated disposition."""

    transcript: dict[str, Any]

    _BASE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
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
            "text_digest_sha256",
            "transcript_proof",
            "proof_expires_at",
        }
    )

    def validate(self) -> None:
        value = self.transcript
        if not isinstance(value, dict) or set(value) != self._BASE_FIELDS:
            raise WindowsProtocolError("final voice transcript fields are invalid")
        if value["type"] != "voice_transcript" or value["schema_version"] != "1":
            raise WindowsProtocolError("voice transcript discriminator is invalid")
        if value["final"] is not True:
            raise WindowsProtocolError("only final voice transcripts can be submitted")
        for field_name in (
            "session_id",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
        ):
            _uuid4(value[field_name], field_name)
        for field_name in (
            "generation",
            "chat_context_revision",
            "media_grant_revision",
        ):
            if (
                isinstance(value[field_name], bool)
                or not isinstance(value[field_name], int)
                or value[field_name] < 1
            ):
                raise WindowsProtocolError(f"{field_name} must be a positive integer")
        _uint64(value["sequence"], "sequence")
        text = value["text"]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 8000
            or text != text.strip()
            or unicodedata.normalize("NFC", text) != text
            or "\r" in text
            or any(
                ord(character) < 32 and character not in {"\t", "\n"}
                for character in text
            )
        ):
            raise WindowsProtocolError("final transcript text is not canonical")
        language = value["detected_language"]
        if (
            not isinstance(language, str)
            or re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language) is None
        ):
            raise WindowsProtocolError("detected_language is invalid")
        worker = value["source_participant_identity"]
        if not isinstance(worker, str) or _VOICE_OPAQUE.fullmatch(worker) is None:
            raise WindowsProtocolError("source participant identity is invalid")
        digest = value["text_digest_sha256"]
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest
        ):
            raise WindowsProtocolError("voice transcript text digest is invalid")
        proof = value["transcript_proof"]
        if not isinstance(proof, str) or _SHA256.fullmatch(proof) is None:
            raise WindowsProtocolError("voice transcript proof is invalid")
        _utc(value["proof_expires_at"], "proof_expires_at")
        encoded_size = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if encoded_size > 12 * 1024:
            raise WindowsProtocolError("voice transcript exceeds the data envelope")

    @property
    def turn_id(self) -> str:
        return str(self.transcript["turn_id"])

    @property
    def local_projection(self) -> LocalOperationSubmission:
        return LocalOperationSubmission(
            submission_id=str(self.transcript["submission_id"]),
            request_generation=str(self.transcript["request_generation"]),
            action="chat_message",
            chat_id=str(self.transcript["chat_id"]),
        )

    def expired(self, now: Optional[datetime] = None) -> bool:
        expiry = datetime.fromisoformat(
            str(self.transcript["proof_expires_at"])[0:-1] + "+00:00"
        )
        return expiry <= (now or datetime.now(timezone.utc))

    def to_frame(self, connection_generation: str) -> dict[str, Any]:
        self.validate()
        _uuid4(connection_generation, "connection_generation")
        value = self.transcript
        voice_origin = {
            "schema_version": "1",
            "session_id": value["session_id"],
            "generation": value["generation"],
            "media_grant_revision": value["media_grant_revision"],
            "turn_id": value["turn_id"],
            "client_turn_id": value["client_turn_id"],
            "chat_context_revision": value["chat_context_revision"],
            "source_participant_identity": value["source_participant_identity"],
            "detected_language": value["detected_language"],
            "text_digest_sha256": value["text_digest_sha256"],
            "transcript_proof": value["transcript_proof"],
            "proof_expires_at": value["proof_expires_at"],
        }
        return {
            "type": "ui_event",
            "action": "chat_message",
            "session_id": value["chat_id"],
            "connection_generation": connection_generation,
            "submission_id": value["submission_id"],
            "request_generation": value["request_generation"],
            "payload": {
                "message": value["text"],
                "chat_id": value["chat_id"],
                "connection_generation": connection_generation,
                "submission_id": value["submission_id"],
                "request_generation": value["request_generation"],
                "snapshot_purpose": "commit",
                "voice_origin": voice_origin,
            },
        }


@dataclass(frozen=True)
class QueuedReplayPreparation:
    """Exact queued identity plus the new connection fence to install first."""

    connection_generation: str
    submission: LocalOperationSubmission
    request_purpose: Optional[str]

    def validate(self) -> None:
        _uuid4(self.connection_generation, "connection_generation")
        self.submission.validate()
        expected = {
            "chat_message": "commit",
            "load_chat": "hydration",
        }.get(self.submission.action)
        if self.request_purpose != expected:
            raise WindowsProtocolError("queued replay request purpose is invalid")
        if self.request_purpose == "hydration" and self.submission.chat_id is None:
            raise WindowsProtocolError("queued hydration requires a chat")


class QueuedReplayAcknowledgement:
    """Thread-safe one-shot result for the GUI's before-send preparation."""

    def __init__(self) -> None:
        self.accepted = False
        self.reason = "preparation did not complete"
        self.ready = threading.Event()

    def complete(self, accepted: bool, reason: str = "") -> None:
        if self.ready.is_set():
            return
        self.accepted = bool(accepted)
        self.reason = reason or ("" if accepted else "preparation rejected")
        self.ready.set()


@dataclass(frozen=True)
class AgentHostRegistration:
    """Structured v2 registration sent by the Windows desktop host."""

    host_id: str
    supported_runtime_contract_versions: tuple[int, ...]
    runtime_lock_sha256: str
    platform: str
    client_version: str

    def validate(self) -> None:
        _uuid4(self.host_id, "host_id")
        versions = self.supported_runtime_contract_versions
        if (
            not versions
            or tuple(sorted(set(versions))) != versions
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in versions)
        ):
            raise WindowsProtocolError("runtime contract versions are invalid")
        if _SHA256.fullmatch(self.runtime_lock_sha256) is None:
            raise WindowsProtocolError("runtime lock digest is invalid")
        if self.platform != "windows":
            raise WindowsProtocolError("Windows host platform is invalid")
        if _SEMVER.fullmatch(self.client_version) is None:
            raise WindowsProtocolError("client version must be strict SemVer")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "host_id": self.host_id,
            "supported_runtime_contract_versions": list(
                self.supported_runtime_contract_versions
            ),
            "runtime_lock_sha256": self.runtime_lock_sha256,
            "platform": self.platform,
            "client_version": self.client_version,
        }


@dataclass(frozen=True)
class AgentHostRegistered:
    """Validated server acknowledgement for the current host connection."""

    host_id: str
    host_session_id: str
    inventory_required: bool
    accepted_at: str
    type: str = "agent_host_registered"

    def validate(self) -> None:
        if self.type != "agent_host_registered":
            raise WindowsProtocolError("agent host acknowledgement type is invalid")
        _uuid4(self.host_id, "host_id")
        _uuid4(self.host_session_id, "host_session_id")
        if not isinstance(self.inventory_required, bool):
            raise WindowsProtocolError("inventory_required must be boolean")
        _utc(self.accepted_at, "accepted_at")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentHostRegistered":
        _exact(data, {item.name for item in fields(cls)}, "agent_host_registered")
        model = cls(**data)
        model.validate()
        return model


@dataclass(frozen=True)
class AgentHostRegistrationRefused:
    """Exact non-disclosing refusal for an incompatible host registration."""

    code: str
    retryable: bool
    details: dict[str, Any]
    refused_at: str
    type: str = "agent_host_registration_refused"

    def validate(self) -> None:
        if self.type != "agent_host_registration_refused" or self.retryable is not False:
            raise WindowsProtocolError("agent host refusal type/flags are invalid")
        if not isinstance(self.details, dict):
            raise WindowsProtocolError("agent host refusal details must be an object")
        if self.code == "runtime_contract_unsupported":
            _exact(
                self.details,
                {
                    "required_runtime_contract_version",
                    "supported_runtime_contract_versions",
                },
                "runtime_contract_unsupported details",
            )
            required = self.details["required_runtime_contract_version"]
            supported = self.details["supported_runtime_contract_versions"]
            if type(required) is not int or required <= 0:
                raise WindowsProtocolError("required runtime contract version is invalid")
            if (
                not isinstance(supported, list)
                or any(type(item) is not int or item <= 0 for item in supported)
                or supported != sorted(set(supported))
            ):
                raise WindowsProtocolError("supported runtime contract versions are invalid")
        elif self.code == "runtime_lock_mismatch":
            _exact(
                self.details,
                {"expected_sha256_prefix", "actual_sha256_prefix"},
                "runtime_lock_mismatch details",
            )
            if any(
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{12}", value) is None
                for value in self.details.values()
            ):
                raise WindowsProtocolError("runtime lock digest prefixes are invalid")
        elif self.code == "invalid_host_registration":
            _exact(
                self.details,
                {"field"},
                "invalid_host_registration details",
            )
            field = self.details["field"]
            if not isinstance(field, str) or _SNAKE_CASE.fullmatch(field) is None:
                raise WindowsProtocolError("invalid host registration field is unsafe")
        else:
            raise WindowsProtocolError("agent host refusal code is invalid")
        _utc(self.refused_at, "refused_at")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentHostRegistrationRefused":
        _exact(
            data,
            {item.name for item in fields(cls)},
            "agent_host_registration_refused",
        )
        model = cls(**data)
        model.validate()
        return model


@dataclass(frozen=True)
class MacOSHostCapability:
    """Candidate-owned macOS host applicability value; never inferred locally."""

    supported: bool
    runtime_contract_versions: tuple[int, ...]
    source_feature: Optional[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MacOSHostCapability":
        _exact(
            data,
            {"supported", "runtime_contract_versions", "source_feature"},
            "macOS host capability",
        )
        versions = data.get("runtime_contract_versions")
        if not isinstance(versions, list):
            raise WindowsProtocolError("runtime_contract_versions must be an array")
        model = cls(
            supported=data.get("supported"),
            runtime_contract_versions=tuple(versions),
            source_feature=data.get("source_feature"),
        )
        if not isinstance(model.supported, bool):
            raise WindowsProtocolError("supported must be boolean")
        if model.supported:
            if 2 not in model.runtime_contract_versions or model.source_feature != "059":
                raise WindowsProtocolError("supported macOS hosting requires feature 059")
        elif model.runtime_contract_versions or model.source_feature is not None:
            raise WindowsProtocolError("unsupported macOS hosting has invalid metadata")
        return model


def parse_runtime_frame(data: dict[str, Any]) -> object:
    """Parse a recognized 060 frame strictly, leaving legacy frames as dicts."""

    parser = {
        "conversation_snapshot": ConversationSnapshot.from_dict,
        "conversation_commit_ready": ConversationCommitReady.from_dict,
        "operation_status": OperationStatus.from_dict,
        "agent_lifecycle": AgentLifecycle.from_dict,
        "agent_host_registered": AgentHostRegistered.from_dict,
        "agent_host_registration_refused": AgentHostRegistrationRefused.from_dict,
    }.get(data.get("type"))
    return parser(data) if parser is not None else data

#: Bounded outbound buffer while disconnected (matches the Android client).
MAX_QUEUE = 64

#: The GUI must acknowledge a queued replay fence promptly. A timeout keeps
#: the exact frame queued and forces a reconnect instead of sending unfenced.
REPLAY_PREPARATION_TIMEOUT_S = 2.0

#: Reconnect backoff bounds (seconds) — 1 s base doubling to a 30 s cap.
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0


def backoff_delay_s(attempt: int, base: float = BACKOFF_BASE_S,
                    cap: float = BACKOFF_MAX_S) -> float:
    """Delay before reconnect ``attempt`` (1-based): base * 2^(attempt-1), capped.

    Mirrors the Android client's ``backoffDelayMs`` so both natives share the
    same contract (specs/044 contracts/session-lifecycle.md §1).
    """
    if attempt <= 1:
        return base
    return min(base * (2 ** (attempt - 1)), cap)


def device_caps(
    width: int = 1280,
    height: int = 860,
    supported_types=None,
    voice_capability: Optional[dict[str, Any]] = None,
) -> dict:
    """Report this client as a native ``windows`` device with the set of SDUI
    primitive types it renders natively. ROTE keys off ``device_type`` for the
    desktop host-config and uses ``supported_types`` to substitute web-only
    primitives (e.g. audio) the native renderer can't draw — so the
    server adapts to the desktop app's real capabilities, not the web view's."""
    caps = {
        "device_type": "windows",
        "screen_width": width, "screen_height": height,
        "viewport_width": width, "viewport_height": height,
        "pixel_ratio": 1.0, "has_touch": False, "user_agent": "AstralWindowsClient/0.1",
        "connection_type": "wifi",
    }
    if supported_types:
        caps["supported_types"] = list(supported_types)
    if voice_capability is not None:
        caps["voice"] = dict(voice_capability)
    return caps


class OrchestratorClient(QObject):
    message = Signal(dict)        # any inbound server message {type: ...}
    # Synchronously emitted on the caller's thread before `_send` can perform
    # socket I/O. Native UI owners use this to render the client-only
    # "Submitting…" projection without claiming server acceptance.
    submission = Signal(object)
    # Exact client identity discarded from the bounded queue. The UI settles
    # only this local projection while the status signal explains the loss.
    submission_dropped = Signal(object)
    # Queued replays use a synchronous acknowledgement protocol across the
    # transport/GUI thread boundary. The receiver installs the new connection
    # and request fence plus the exact local projection, then completes ack.
    queued_replay_preparation = Signal(object, object)
    connection_generation_changed = Signal(str)
    voice_submission_settled = Signal(str, str)
    # "connecting" | "connected" | "reconnecting:<attempt>" |
    # "auth_required:<reason>" | "closed:<why>" | "send_dropped:<action>"
    status = Signal(str)

    def __init__(
        self,
        url: str,
        token: str,
        device: Optional[dict] = None,
        *,
        host_id: Optional[str] = None,
        device_id: Optional[str] = None,
    ):
        super().__init__()
        self.url = url
        self.token = token
        self.device = device or device_caps()
        self.device_id = _uuid4(
            device_id or load_or_create_voice_device_id(),
            "device_id",
        )
        # register_ui session id. The app points this at the ACTIVE CHAT id so
        # a reconnect's re-register resumes that chat's fan-out + background-
        # task replay server-side (feature 055); "win-client" is the no-chat
        # default this client has always sent.
        self.session_id: str = "win-client"
        # Feature 060 replaces the client-invented host session with a
        # structured runtime-contract advertisement and server-issued ack.
        # MainWindow supplies the installation-persisted identity owned by the
        # BYO host. The fallback keeps isolated transport tests/source users
        # compatible; production construction never relies on it.
        self.host_id: str = _uuid4(host_id or str(uuid.uuid4()), "host_id")
        self.host_session_id: Optional[str] = None
        self.connection_generation: Optional[str] = None
        self.resume_chat_id: Optional[str] = None
        self.request_generation: Optional[str] = None
        self.request_purpose: Optional[str] = None
        self.request_chat_id: Optional[str] = None
        self.host_registration = AgentHostRegistration(
            host_id=self.host_id,
            supported_runtime_contract_versions=RUNTIME_CONTRACT_VERSIONS,
            runtime_lock_sha256=RUNTIME_LOCK_SHA256,
            platform="windows",
            client_version=__version__,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = False
        self._auth_hold = False   # auth_required seen: don't loop on a bad token
        self._connected = False
        self._had_session = False
        self._pending: deque[str] = deque()
        self._voice_pending: dict[str, VoiceTranscriptSubmission] = {}
        self._queued_replay_preparation_required = False

    def require_queued_replay_preparation(self) -> None:
        """Require the GUI acknowledgement before any retained UI event send."""

        self._queued_replay_preparation_required = True

    def configure_resume(self, chat_id: Optional[str]) -> None:
        """Bind the synchronously persisted active chat used by registration."""

        if chat_id is not None:
            _uuid4(chat_id, "chat_id")
        self.resume_chat_id = chat_id
        self.session_id = chat_id or "win-client"

    def begin_conversation_request(
        self,
        purpose: str,
        chat_id: Optional[str],
        generation: Optional[str] = None,
    ) -> str:
        """Open one fresh hydration/commit equality fence for normalized work."""

        if purpose not in {"hydration", "commit"}:
            raise WindowsProtocolError("conversation request purpose is invalid")
        if chat_id is not None:
            _uuid4(chat_id, "chat_id")
        request_generation = generation or str(uuid.uuid4())
        _uuid4(request_generation, "request_generation")
        self.request_generation = request_generation
        self.request_purpose = purpose
        self.request_chat_id = chat_id
        return request_generation

    def adopt_server_request(self, purpose: str, chat_id: str, generation: str) -> None:
        """Adopt a validated server-originated request fence (commit prelude)."""

        self.begin_conversation_request(purpose, chat_id, generation)

    def configure_agent_host(self, host_id: str) -> None:
        """Bind the persisted installation ID before the transport starts."""

        if self._thread.is_alive() or self._connected or self._ws is not None:
            raise RuntimeError("agent host identity is immutable after client start")
        self.host_id = _uuid4(host_id, "host_id")
        self.host_session_id = None
        self.host_registration = AgentHostRegistration(
            host_id=self.host_id,
            supported_runtime_contract_versions=RUNTIME_CONTRACT_VERSIONS,
            runtime_lock_sha256=RUNTIME_LOCK_SHA256,
            platform="windows",
            client_version=__version__,
        )

    # --- lifecycle ------------------------------------------------------- #
    def _safe_status(self, s: str) -> None:
        """Emit a status signal, tolerating teardown (the C++ QObject may be
        deleted while this daemon thread is still running)."""
        try:
            self.status.emit(s)
        except RuntimeError:
            pass

    def _safe_message(self, m: dict) -> None:
        try:
            self.message.emit(m)
        except RuntimeError:
            pass

    def _handle_runtime_frame(self, msg: dict[str, Any]) -> bool:
        """Validate recognized 060 frames and bind a matching host ack.

        Returns ``False`` for malformed or wrong-host frames so callers drop
        them without mutating UI or host state.
        """

        try:
            parsed = parse_runtime_frame(msg)
        except WindowsProtocolError:
            self._safe_status(f"protocol_error:{msg.get('type') or 'unknown'}")
            return False
        if isinstance(parsed, AgentHostRegistered):
            if parsed.host_id != self.host_id:
                self._safe_status("protocol_error:agent_host_registered")
                return False
            if (
                self.host_session_id is not None
                and self.host_session_id != parsed.host_session_id
            ):
                # A server session is immutable for one accepted connection.
                # Only the next register_ui generation may bind another one.
                self._safe_status("protocol_error:agent_host_registered")
                return False
            self.host_session_id = parsed.host_session_id
            self._safe_status("agent_host_registered")
        elif isinstance(parsed, AgentHostRegistrationRefused):
            self.host_session_id = None
            self._safe_status(f"agent_host_registration_refused:{parsed.code}")
        frame_type = msg.get("type")
        if frame_type in {"user_message_acked", "voice_submission_rejected"}:
            settled = self.settle_voice_submission(msg)
            is_voice_ack = (
                frame_type == "user_message_acked"
                and msg.get("voice_turn_id") is not None
            )
            if not settled and (frame_type == "voice_submission_rejected" or is_voice_ack):
                self._safe_status(f"protocol_error:{frame_type}")
                return False
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._loop and self._ws:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except RuntimeError:
                pass

    def _should_reconnect(self) -> bool:
        """Auto-reconnect unless the app is quitting or the server demanded
        re-authentication (the app owns the refresh + rebuild in that case)."""
        return not self._stop and not self._auth_hold

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        attempt = 0
        while True:
            self._had_session = False
            try:
                self._loop.run_until_complete(self._main())
                if not self._stop and not self._auth_hold:
                    self._safe_status("closed:server")
            except Exception as exc:  # surface connection failures to the UI
                if not self._stop:
                    self._safe_status(f"closed:{exc}")
            self._connected = False
            self._ws = None
            if self._had_session:
                attempt = 0  # successful open resets the backoff (FR-003)
            if not self._should_reconnect():
                break
            attempt += 1
            self._safe_status(f"reconnecting:{attempt}")
            if not self._interruptible_sleep(backoff_delay_s(attempt)):
                break

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in slices so stop()/auth_required end the wait promptly.
        Returns False when the loop should exit instead of reconnecting."""
        remaining = seconds
        while remaining > 0:
            if not self._should_reconnect():
                return False
            step = min(0.25, remaining)
            self._loop.run_until_complete(asyncio.sleep(step))
            remaining -= step
        return self._should_reconnect()

    def _register_frame(self) -> dict:
        """The register_ui handshake frame, rebuilt per (re)connect so
        ``session_id`` reflects the chat that was open when the drop happened."""
        self.connection_generation = str(uuid.uuid4())
        try:
            self.connection_generation_changed.emit(self.connection_generation)
        except RuntimeError:
            pass
        self.host_session_id = None
        capabilities = ["render", "stream", "agent_host"]
        if isinstance(self.device.get("voice"), dict):
            capabilities.append("voice")
        frame = {
            "type": "register_ui",
            "token": self.token,
            "capabilities": capabilities,
            "device_id": self.device_id,
            "connection_generation": self.connection_generation,
            "agent_host": self.host_registration.to_dict(),
            "session_id": self.session_id,
            "device": self.device,
            "resumed": self.resume_chat_id is not None,
        }
        if self.resume_chat_id is not None:
            generation = self.begin_conversation_request(
                "hydration", self.resume_chat_id
            )
            frame["resume"] = {
                "schema_version": 1,
                "active_chat_id": self.resume_chat_id,
                "request_generation": generation,
            }
        else:
            self.request_generation = None
            self.request_purpose = None
            self.request_chat_id = None
        return frame

    async def _main(self) -> None:
        self._safe_status("connecting")
        async with websockets.connect(self.url, max_size=16 * 1024 * 1024,
                                      ping_interval=20) as ws:
            self._ws = ws
            self._had_session = True
            await ws.send(json.dumps(self._register_frame()))
            await self._finish_open(ws)
            async for raw in ws:
                if self._stop:
                    break
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict):
                    if not self._handle_runtime_frame(msg):
                        continue
                    if msg.get("type") == "auth_required":
                        # Hold auto-reconnect: retrying with the same token
                        # would loop; the app refreshes and rebuilds instead.
                        self._auth_hold = True
                        self._safe_status(f"auth_required:{msg.get('reason', '')}")
                    self._safe_message(msg)

    async def _finish_open(self, ws) -> None:
        """Post-register open sequence. Drain the offline queue FIFO BEFORE
        flipping `_connected`, so any queued frame goes out ahead of a new
        direct send. If `_connected` were set first, a frame sent by the
        "connected" handler could race ahead of the queued backlog and reorder
        reconnect delivery. Then drain ONCE MORE after the flip: a frame
        appended to `_pending` between the first drain and the flip would
        otherwise sit unflushed while the connection stays healthy — once
        `_connected` is True no new frame enters the queue, so the second
        drain deterministically closes that window (FR-003)."""
        await self._flush_pending(ws)
        self._connected = True
        await self._flush_pending(ws)
        await self._resend_voice_pending(ws)
        self._safe_status("connected")

    # --- outbound -------------------------------------------------------- #
    async def _flush_pending(self, ws) -> None:
        """Restore replay fences, then drain queued frames FIFO (FR-003)."""
        while self._pending and not self._stop:
            frame = self._rebind_pending_conversation_frame(self._pending.popleft())
            submission = self._queued_submission_from_frame(frame)
            if submission is None:
                self._safe_status(f"send_rejected:{self._queued_action(frame)}")
                continue
            preparation = self._queued_replay_preparation(frame, submission)
            if preparation is None:
                self._safe_status(f"send_rejected:{self._queued_action(frame)}")
                continue
            if self._queued_replay_preparation_required:
                acknowledged = await self._prepare_queued_replay(preparation)
                socket_state = getattr(getattr(ws, "state", None), "name", "")
                socket_closed = bool(getattr(ws, "closed", False)) or socket_state in {
                    "CLOSING",
                    "CLOSED",
                }
                if not acknowledged or self._stop or socket_closed:
                    # The frame is still reliable queued work: keep its exact
                    # IDs, fail visibly, and force the outer loop to establish
                    # a fresh connection before trying the handshake again.
                    self._pending.appendleft(frame)
                    self._safe_status(f"replay_deferred:{submission.action}")
                    raise ConnectionError("queued replay preparation failed")
            else:
                # Headless/transport-only consumers retain the legacy signal;
                # the shipping GUI opts into the acknowledged path above.
                self.submission.emit(submission)
            try:
                await ws.send(frame)
            except BaseException:
                # No successful await means the frame never left the reliable
                # queue contract. Preserve its exact identities for the next
                # connection and let the transport loop reconnect.
                self._pending.appendleft(frame)
                raise

    async def _prepare_queued_replay(
        self,
        preparation: QueuedReplayPreparation,
    ) -> bool:
        """Wait without blocking the socket loop for the GUI's exact fence."""

        acknowledgement = QueuedReplayAcknowledgement()
        try:
            self.queued_replay_preparation.emit(preparation, acknowledgement)
        except RuntimeError:
            return False
        ready = await asyncio.to_thread(
            acknowledgement.ready.wait,
            REPLAY_PREPARATION_TIMEOUT_S,
        )
        return ready and acknowledgement.accepted

    @staticmethod
    def _queued_action(serialized: str) -> str:
        try:
            frame = json.loads(serialized)
        except (ValueError, TypeError):
            return "message"
        action = frame.get("action") if isinstance(frame, dict) else None
        return action if isinstance(action, str) and action else "message"

    @staticmethod
    def _queued_submission_from_frame(
        serialized: str,
    ) -> Optional[LocalOperationSubmission]:
        """Parse exact safe replay identity from one serialized UI event."""

        try:
            frame = json.loads(serialized)
        except (ValueError, TypeError):
            return None
        if not isinstance(frame, dict) or frame.get("type") != "ui_event":
            return None
        action = frame.get("action")
        payload = frame.get("payload")
        if (
            not isinstance(action, str)
            or _SNAKE_CASE.fullmatch(action) is None
            or not isinstance(payload, dict)
        ):
            return None
        submission_id = frame.get("submission_id")
        request_generation = frame.get("request_generation")
        if (
            not _is_uuid4(submission_id)
            or not _is_uuid4(request_generation)
            or payload.get("submission_id") != submission_id
            or payload.get("request_generation") != request_generation
        ):
            return None
        if "surface" in payload and (
            not isinstance(payload["surface"], str)
            or _SNAKE_CASE.fullmatch(payload["surface"]) is None
        ):
            return None
        explicit_chat = payload.get("chat_id")
        if explicit_chat is not None and not _is_uuid4(explicit_chat):
            return None
        session_chat = frame.get("session_id")
        chat_id = explicit_chat or (session_chat if _is_uuid4(session_chat) else None)
        submission = LocalOperationSubmission(
            submission_id=submission_id,
            request_generation=request_generation,
            action=action,
            chat_id=chat_id,
        )
        try:
            submission.validate()
        except WindowsProtocolError:
            return None
        return submission

    def _queued_replay_preparation(
        self,
        serialized: str,
        submission: LocalOperationSubmission,
    ) -> Optional[QueuedReplayPreparation]:
        """Construct the strict GUI handshake from the rebound exact frame."""

        try:
            frame = json.loads(serialized)
        except (ValueError, TypeError):
            return None
        payload = frame.get("payload") if isinstance(frame, dict) else None
        if not isinstance(payload, dict) or not _is_uuid4(self.connection_generation):
            return None
        purpose = payload.get("snapshot_purpose")
        if purpose is not None and purpose not in {"hydration", "commit"}:
            return None
        preparation = QueuedReplayPreparation(
            connection_generation=self.connection_generation,
            submission=submission,
            request_purpose=purpose,
        )
        try:
            preparation.validate()
        except WindowsProtocolError:
            return None
        return preparation

    def _rebind_pending_conversation_frame(self, serialized: str) -> str:
        """Fence queued normalized UI work to the newly opened connection.

        Submission and request generations remain the identities of the same
        queued work; only the connection equality fence changes. The last
        queued conversation request becomes the transport's current request
        before ``connected`` is emitted, so the UI reducer expects the final
        coherent snapshot. Surface operations are rebound too so their
        canonical status can be checked against the current connection.
        """

        if self.connection_generation is None:
            return serialized
        try:
            frame = json.loads(serialized)
        except (ValueError, TypeError):
            return serialized
        if not isinstance(frame, dict) or frame.get("type") != "ui_event":
            return serialized
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return serialized
        frame["connection_generation"] = self.connection_generation
        chat_id = payload.get("chat_id")
        generation = payload.get("request_generation")
        purpose = payload.get("snapshot_purpose")
        scoped_chat = _is_uuid4(chat_id) or (
            chat_id is None and purpose == "commit"
        )
        if (
            frame.get("action") in {"chat_message", "load_chat"}
            and scoped_chat
            and _is_uuid4(generation)
            and purpose in {"hydration", "commit"}
        ):
            payload["connection_generation"] = self.connection_generation
            self.request_chat_id = chat_id
            self.request_generation = generation
            self.request_purpose = purpose
        return json.dumps(frame)

    def _send(self, obj: dict) -> None:
        frame = json.dumps(obj)
        # Snapshot `_ws`/`_loop` under the guard: the transport thread can null
        # `_ws` between the check and the attribute access, which would raise an
        # AttributeError inside a Qt slot (TOCTOU). If the snapshot is None after
        # the guard, fall through to the queue path.
        ws = self._ws
        loop = self._loop
        if self._connected and loop and ws:
            fut = asyncio.run_coroutine_threadsafe(ws.send(frame), loop)
            # The socket can die AFTER the `_connected` check with the flag
            # still True — a fire-and-forget send would then vanish silently.
            # Re-queue a failed fast-path send through the offline path so it
            # goes out on the next (re)connect. The callback runs on the
            # asyncio loop thread; deque appends are thread-safe.
            fut.add_done_callback(lambda f: self._on_fast_send_done(f, frame))
            return
        self._queue_frame(frame)

    def _on_fast_send_done(self, fut, frame: str) -> None:
        """Done-callback for a connected fast-path send: on failure the frame is
        re-queued so an outbound frame never just vanishes (FR-003)."""
        try:
            failed = fut.cancelled() or fut.exception() is not None
        except Exception:  # noqa: BLE001 — treat an unreadable future as failed
            failed = True
        if failed:
            self._queue_frame(frame)

    def _queue_frame(self, frame: str) -> None:
        """Queue a frame for the (re)connect flush with a bounded buffer;
        overflow is dropped-oldest AND surfaced — an outbound frame never just
        vanishes."""
        submission = self._queued_submission_from_frame(frame)
        if submission is None:
            self._safe_status(f"send_rejected:{self._queued_action(frame)}")
            return
        self._pending.append(frame)
        while len(self._pending) > MAX_QUEUE:
            dropped = self._pending.popleft()
            dropped_submission = self._queued_submission_from_frame(dropped)
            if dropped_submission is not None:
                self.submission_dropped.emit(dropped_submission)
            try:
                action = json.loads(dropped).get("action", "message")
            except (ValueError, TypeError, AttributeError):
                action = "message"
            self._safe_status(f"send_dropped:{action}")

    def send_event(
        self,
        action: str,
        payload: dict,
        session_id: Optional[str] = None,
    ) -> LocalOperationSubmission:
        """Identify, project, then send one UI event.

        Both client identities are present at the top level and in ``payload``.
        Canonical caller-supplied identities are preserved; absent or malformed
        values are replaced with fresh UUID4s before the local projection is
        emitted. The signal is deliberately emitted before ``_send`` so UI
        feedback cannot race behind socket I/O.
        """

        if not isinstance(payload, dict):
            raise WindowsProtocolError("ui_event payload must be an object")
        if not isinstance(action, str) or _SNAKE_CASE.fullmatch(action) is None:
            raise WindowsProtocolError("ui_event action must be snake case")
        safe_payload = dict(payload)
        supplied_submission = safe_payload.get("submission_id")
        submission_id = (
            supplied_submission
            if _is_uuid4(supplied_submission)
            else str(uuid.uuid4())
        )
        supplied_request = safe_payload.get("request_generation")
        request_generation = (
            supplied_request if _is_uuid4(supplied_request) else str(uuid.uuid4())
        )
        safe_payload["submission_id"] = submission_id
        safe_payload["request_generation"] = request_generation
        frame = {
            "type": "ui_event",
            "action": action,
            "session_id": session_id,
            "submission_id": submission_id,
            "request_generation": request_generation,
            "payload": safe_payload,
        }
        connection = safe_payload.get("connection_generation")
        if not _is_uuid4(connection):
            connection = self.connection_generation
        if _is_uuid4(connection):
            frame["connection_generation"] = connection
        chat_id = safe_payload.get("chat_id")
        if not _is_uuid4(chat_id):
            chat_id = session_id if _is_uuid4(session_id) else None
        local = LocalOperationSubmission(
            submission_id=submission_id,
            request_generation=request_generation,
            action=action,
            chat_id=chat_id,
        )
        local.validate()
        self.submission.emit(local)
        self._send(frame)
        return local

    def send_voice_transcript(
        self,
        transcript: dict[str, Any],
    ) -> LocalOperationSubmission:
        """Submit one proven final through the ordinary ``chat_message`` path.

        The immutable recognition binding is retained only in memory until a
        fully correlated acknowledgement or rejection arrives. Reconnects
        replace only the UI connection fence; they never mint new turn IDs.
        """

        submission = VoiceTranscriptSubmission(copy.deepcopy(transcript))
        submission.validate()
        if submission.expired():
            raise WindowsProtocolError("voice transcript proof has expired")
        existing = self._voice_pending.get(submission.turn_id)
        if existing is not None:
            if existing.transcript != submission.transcript:
                raise WindowsProtocolError("voice turn identity was reused")
            return existing.local_projection
        if len(self._voice_pending) >= MAX_PENDING_VOICE_FINALS:
            raise WindowsProtocolError("voice transcript retention is full")
        retained_size = sum(
            len(_stable_json(item.transcript).encode("utf-8"))
            for item in self._voice_pending.values()
        )
        retained_size += len(_stable_json(submission.transcript).encode("utf-8"))
        if retained_size > MAX_PENDING_VOICE_BYTES:
            raise WindowsProtocolError("voice transcript retention is full")
        if not _is_uuid4(self.connection_generation):
            raise WindowsProtocolError("voice submission requires a current connection")
        self._voice_pending[submission.turn_id] = submission
        local = submission.local_projection
        local.validate()
        self.submission.emit(local)
        self._send_voice_frame(submission.to_frame(self.connection_generation))
        return local

    def send_correlated_new_chat(
        self,
        submission_id: str,
        request_generation: str,
    ) -> bool:
        """Send the strict current-socket new-chat prelude for voice activation."""

        _uuid4(submission_id, "submission_id")
        _uuid4(request_generation, "request_generation")
        connection = _uuid4(
            self.connection_generation,
            "connection_generation",
        )
        correlation = {
            "schema_version": "1",
            "connection_generation": connection,
            "submission_id": submission_id,
            "request_generation": request_generation,
        }
        frame = {
            "type": "ui_event",
            "action": "new_chat",
            **correlation,
            "payload": dict(correlation),
        }
        loop = self._loop
        ws = self._ws
        if not self._connected or loop is None or ws is None:
            self._safe_status("voice_activation_cancelled:connection_unavailable")
            return False
        future = asyncio.run_coroutine_threadsafe(
            ws.send(json.dumps(frame, separators=(",", ":"))), loop
        )
        future.add_done_callback(self._consume_host_send_result)
        return True

    def _send_voice_frame(self, frame: dict[str, Any]) -> None:
        """Send without the ordinary offline queue; the proof-bound store retries."""

        loop = self._loop
        ws = self._ws
        if not self._connected or loop is None or ws is None:
            self._safe_status("voice_submission_pending")
            return
        future = asyncio.run_coroutine_threadsafe(
            ws.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":"))),
            loop,
        )
        future.add_done_callback(self._consume_host_send_result)

    def send_voice_playout_event(self, frame: dict[str, Any]) -> None:
        """Send one strict, content-free local playout observation.

        Playout evidence is current-socket telemetry, never a replayable UI
        action.  It therefore uses the direct voice send seam but is not added
        to either the ordinary offline queue or the retained transcript store.
        """

        required = {
            "type",
            "schema_version",
            "device_id",
            "connection_generation",
            "session_id",
            "generation",
            "media_grant_revision",
            "announcement_id",
            "announcement_sequence",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "phase",
            "client_sequence",
            "observed_at",
        }
        supplied = set(frame) if isinstance(frame, dict) else set()
        if supplied != required and supplied != required | {
            "result_reserved_samples_after"
        }:
            raise WindowsProtocolError("voice playout fields are invalid")
        if frame.get("type") != "voice_playout_event" or frame.get(
            "schema_version"
        ) != "1":
            raise WindowsProtocolError("voice playout discriminator is invalid")
        if frame.get("device_id") != self.device_id:
            raise WindowsProtocolError("voice playout device is invalid")
        connection = _uuid4(
            frame.get("connection_generation"),
            "connection_generation",
        )
        if connection != self.connection_generation:
            raise WindowsProtocolError("voice playout connection is stale")
        for name in ("session_id", "announcement_id"):
            _uuid4(frame.get(name), name)
        for name in (
            "generation",
            "media_grant_revision",
            "announcement_sequence",
        ):
            value = frame.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise WindowsProtocolError(f"{name} must be a positive integer")
        _uint64(frame.get("quantum_index"), "quantum_index")
        if frame["quantum_index"] > 31:
            raise WindowsProtocolError("quantum_index exceeds the contract")
        _uint64(frame.get("client_sequence"), "client_sequence")
        if frame.get("kind") not in _VOICE_PLAYOUT_KINDS:
            raise WindowsProtocolError("voice playout kind is invalid")
        if frame["kind"] == "greeting":
            if frame.get("turn_id") is not None:
                raise WindowsProtocolError("greeting turn_id must be null")
        else:
            _uuid4(frame.get("turn_id"), "turn_id")
        reservation = frame.get("result_reserved_samples_after")
        role = frame.get("quantum_role")
        if role == "single":
            if (
                frame["kind"] not in _VOICE_SINGLE_KINDS
                or frame["quantum_index"] != 0
                or reservation is not None
            ):
                raise WindowsProtocolError("single playout quantum is invalid")
        elif role == "result_opening":
            if (
                frame["kind"] != "result"
                or frame["quantum_index"] != 0
                or isinstance(reservation, bool)
                or not isinstance(reservation, int)
                or not 1 <= reservation <= 36_000
            ):
                raise WindowsProtocolError("result opening playout is invalid")
        elif role == "result_continuation":
            if (
                frame["kind"] != "result"
                or not 1 <= frame["quantum_index"] <= 31
                or isinstance(reservation, bool)
                or not isinstance(reservation, int)
                or not 1 <= reservation <= 720_000
            ):
                raise WindowsProtocolError("result continuation playout is invalid")
        else:
            raise WindowsProtocolError("voice playout quantum role is invalid")
        if frame.get("phase") not in {"started", "finished", "interrupted"}:
            raise WindowsProtocolError("voice playout phase is invalid")
        _utc(frame.get("observed_at"), "observed_at")
        try:
            encoded = json.dumps(
                frame,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WindowsProtocolError("voice playout is not canonical JSON") from exc
        if len(encoded) > 2 * 1024:
            raise WindowsProtocolError("voice playout exceeds the data envelope")
        self._send_voice_frame(copy.deepcopy(frame))

    def _pending_voice_frames(self) -> list[dict[str, Any]]:
        """Return current-connection retries, expiring stale proofs visibly."""

        if not _is_uuid4(self.connection_generation):
            return []
        frames: list[dict[str, Any]] = []
        for turn_id, submission in list(self._voice_pending.items()):
            if submission.expired():
                self._voice_pending.pop(turn_id, None)
                self._safe_status("voice_submission_expired")
                continue
            frames.append(submission.to_frame(self.connection_generation))
        return frames

    async def _resend_voice_pending(self, ws) -> None:
        for frame in self._pending_voice_frames():
            await ws.send(
                json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
            )

    def settle_voice_submission(self, frame: dict[str, Any]) -> bool:
        """Clear only a fully correlated current-connection ack/rejection."""

        if not isinstance(frame, dict):
            return False
        frame_type = frame.get("type")
        if frame_type == "user_message_acked":
            required = {
                "type",
                "schema_version",
                "chat_id",
                "message_id",
                "submission_id",
                "request_generation",
                "connection_generation",
                "voice_turn_id",
            }
            if set(frame) != required:
                return False
            turn_id = frame.get("voice_turn_id")
            submission = self._voice_pending.get(str(turn_id))
            if submission is None:
                return False
            value = submission.transcript
            valid = (
                frame.get("schema_version") == "1"
                and isinstance(frame.get("message_id"), int)
                and not isinstance(frame.get("message_id"), bool)
                and frame["message_id"] >= 1
                and frame.get("chat_id") == value["chat_id"]
                and frame.get("submission_id") == value["submission_id"]
                and frame.get("request_generation") == value["request_generation"]
                and frame.get("connection_generation") == self.connection_generation
            )
            disposition = "accepted"
        elif frame_type == "voice_submission_rejected":
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
                "reason",
                "retry_policy",
                "occurred_at",
            }
            supplied = set(frame)
            if supplied != required and supplied != required | {"message"}:
                return False
            turn_id = frame.get("turn_id")
            submission = self._voice_pending.get(str(turn_id))
            if submission is None:
                return False
            value = submission.transcript
            try:
                _utc(frame.get("occurred_at"), "occurred_at")
            except WindowsProtocolError:
                return False
            message = frame.get("message")
            if message is not None and (
                not isinstance(message, str) or len(message) > 240
            ):
                return False
            valid = (
                frame.get("schema_version") == "1"
                and frame.get("session_id") == value["session_id"]
                and frame.get("connection_generation") == self.connection_generation
                and frame.get("generation") == value["generation"]
                and frame.get("media_grant_revision")
                == value["media_grant_revision"]
                and frame.get("client_turn_id") == value["client_turn_id"]
                and frame.get("submission_id") == value["submission_id"]
                and frame.get("request_generation") == value["request_generation"]
                and frame.get("chat_id") == value["chat_id"]
                and frame.get("reason")
                in {
                    "capacity_exhausted",
                    "chat_unavailable",
                    "invalid_binding",
                    "invalid_proof",
                    "proof_expired",
                    "permission_denied",
                    "stale_session",
                    "malformed_final",
                }
                and frame.get("retry_policy") in {"explicit_user_retry", "none"}
            )
            disposition = "rejected"
        else:
            return False
        if not valid:
            return False
        self._voice_pending.pop(str(turn_id), None)
        try:
            self.voice_submission_settled.emit(str(turn_id), disposition)
        except RuntimeError:
            pass
        return True

    def send_host_frame(self, frame: dict[str, Any]) -> None:
        """Send one exact v2 host frame only on its currently bound socket.

        Host frames carry ``host_session_id`` and must never enter the generic
        reconnect queue: replaying one on the next connection would send a stale
        session frame before that connection receives its acknowledgement.
        """

        if (
            not isinstance(frame, dict)
            or not isinstance(frame.get("type"), str)
            or not frame["type"].startswith("agent_")
        ):
            raise WindowsProtocolError("agent host frame is invalid")
        loop = self._loop
        ws = self._ws
        if not self._connected or loop is None or ws is None:
            return
        serialized = json.dumps(frame)
        future = asyncio.run_coroutine_threadsafe(ws.send(serialized), loop)
        # A failed session-fenced send is intentionally not re-queued. Socket
        # loss/reconciliation creates a new server session and fresh frames.
        future.add_done_callback(self._consume_host_send_result)

    @staticmethod
    def _consume_host_send_result(future) -> None:
        try:
            if not future.cancelled():
                future.exception()
        except Exception:
            pass

    def send_chat(
        self,
        message: str,
        chat_id: Optional[str] = None,
        attachments: Optional[list] = None,
        request_generation: Optional[str] = None,
        submission_id: Optional[str] = None,
    ) -> LocalOperationSubmission:
        payload: dict[str, Any] = {"message": message}
        if chat_id:
            payload["chat_id"] = chat_id
        if chat_id is None or _is_uuid4(chat_id):
            generation = request_generation or self.begin_conversation_request(
                "commit", chat_id
            )
            _uuid4(generation, "request_generation")
            self.request_generation = generation
            self.request_purpose = "commit"
            self.request_chat_id = chat_id
            payload.update(
                {
                    "connection_generation": self.connection_generation,
                    "request_generation": generation,
                    "snapshot_purpose": "commit",
                }
            )
        if attachments:
            payload["attachments"] = attachments
        if submission_id is not None:
            payload["submission_id"] = submission_id
        return self.send_event("chat_message", payload, session_id=chat_id)
