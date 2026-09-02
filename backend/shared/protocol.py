"""
Protocol message types for inter-agent communication.

Defines:
- MCP Protocol: MCPRequest, MCPResponse
- UI Protocol: UIEvent, UIRender, UIUpdate, UIAppend
- A2A Protocol: AgentCard, AgentSkill, RegisterAgent, RegisterUI
- Tool Streaming: ToolStreamData, ToolStreamEnd, ToolStreamCancel
  (see specs/001-tool-stream-ui/contracts/protocol-messages.md §B)
"""
import json
import re
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, ClassVar, Dict, List, Mapping, Optional


_MAX_UINT64 = (1 << 64) - 1
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STRICT_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_VOICE_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_VOICE_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_VOICE_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_VOICE_SCHEMA_VERSION = "1"
_VOICE_PLAYOUT_MAX_BYTES = 2 * 1024
_VOICE_CONTROL_BINDING_MAX_LIFETIME = timedelta(minutes=10)


class ProtocolValidationError(ValueError):
    """A wire value failed its public protocol contract."""


MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_SUPPORTED_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION,)
MCP_HEADER_MISMATCH = -32020
MCP_MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
MCP_UNSUPPORTED_PROTOCOL_VERSION = -32022
MCP_INVALID_REQUEST = -32600
MCP_INVALID_PARAMS = -32602
MCP_RESULT_COMPLETE = "complete"


class MCPProtocolError(ProtocolValidationError):
    """A modern MCP envelope cannot be processed safely."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = dict(data or {})

    def to_error(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": False,
        }
        if self.data:
            error["data"] = dict(self.data)
        return error


def _known_dataclass_fields(cls: type, data: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep additive wire fields from crashing a version-skewed reader."""

    valid_fields = {item.name for item in fields(cls)}
    return {key: value for key, value in data.items() if key in valid_fields}


def _require_uuid4(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProtocolValidationError(f"{field_name} must be a UUID4 string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProtocolValidationError(
            f"{field_name} must be a UUID4 string"
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ProtocolValidationError(f"{field_name} must be a canonical UUID4 string")
    return value


def _require_uint64(value: object, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_UINT64
    ):
        raise ProtocolValidationError(
            f"{field_name} must be an unsigned 64-bit integer"
        )
    return value


def _require_rfc3339_utc(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProtocolValidationError(f"{field_name} must be an RFC3339 UTC string")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ProtocolValidationError(
            f"{field_name} must be an RFC3339 UTC string"
        ) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ProtocolValidationError(f"{field_name} must be an RFC3339 UTC string")
    return value


def _require_snake_case(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SNAKE_CASE.fullmatch(value) is None:
        raise ProtocolValidationError(f"{field_name} must be a snake-case value")
    return value


def _require_voice_schema_version(value: object, field_name: str = "schema_version") -> str:
    if value != _VOICE_SCHEMA_VERSION:
        raise ProtocolValidationError(f"{field_name} must be exactly \"1\"")
    return value


def _require_positive_integer(
    value: object,
    field_name: str,
    *,
    minimum: int = 1,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProtocolValidationError(
            f"{field_name} must be an integer greater than or equal to {minimum}"
        )
    if maximum is not None and value > maximum:
        raise ProtocolValidationError(
            f"{field_name} must be no greater than {maximum}"
        )
    return value


def _require_bounded_string(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ProtocolValidationError(
            f"{field_name} must contain between {minimum} and {maximum} characters"
        )
    return value


def _require_voice_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _VOICE_UTC_TIMESTAMP.fullmatch(value) is None:
        raise ProtocolValidationError(
            f"{field_name} must be a canonical RFC3339 UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProtocolValidationError(
            f"{field_name} must be a canonical RFC3339 UTC timestamp"
        ) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ProtocolValidationError(
            f"{field_name} must be a canonical RFC3339 UTC timestamp"
        )
    return value


def _voice_timestamp_datetime(value: object, field_name: str) -> datetime:
    canonical = _require_voice_timestamp(value, field_name)
    return datetime.fromisoformat(canonical[:-1] + "+00:00").astimezone(UTC)


def _require_aware_datetime(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProtocolValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_exact_fields(
    data: Mapping[str, Any],
    required: set[str],
    *,
    optional: set[str] | None = None,
    label: str,
) -> None:
    optional = optional or set()
    supplied = set(data)
    if not required <= supplied or supplied - required - optional:
        raise ProtocolValidationError(
            f"{label} must contain exactly its canonical fields"
        )


def _voice_json(data: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(data),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError("voice frame must be valid JSON") from exc


def _require_raw_utf8_size(value: object, *, maximum: int, label: str) -> None:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        encoded = bytes(value)
    else:
        return
    if len(encoded) > maximum:
        raise ProtocolValidationError(
            f"{label} exceeds the {maximum}-byte UTF-8 envelope limit"
        )


def _reject_json_duplicate_keys(
    pairs: List[tuple[str, Any]],
) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ProtocolValidationError("message contains a duplicate JSON key")
        value[name] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ProtocolValidationError("message contains a non-finite JSON number")

# --- Base Message ---
@dataclass
class Message:
    type: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(json_str: str) -> 'Message':
        try:
            data = json.loads(
                json_str,
                object_pairs_hook=_reject_json_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except ProtocolValidationError:
            raise
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProtocolValidationError("message must be valid JSON") from exc
        if not isinstance(data, Mapping):
            raise ProtocolValidationError("message must be a JSON object")
        msg_type = data.get('type')
        if msg_type == 'mcp_request':
            # Filter unknown keys so older peers parsing newer MCP envelopes
            # (and vice versa) survive additive fields instead of silently
            # dropping the frame and leaving its caller to time out.
            return MCPRequest(**_known_dataclass_fields(MCPRequest, data))
        elif msg_type == 'mcp_response':
            return MCPResponse(**_known_dataclass_fields(MCPResponse, data))
        elif msg_type == 'ui_event' and data.get("action") == "new_chat" and (
            "schema_version" in data
        ):
            return CorrelatedNewChat.from_dict(data)
        elif msg_type == 'ui_event':
            return UIEvent(**data)
        elif msg_type == 'ui_render':
            return UIRender(**data)
        elif msg_type == 'ui_update':
            return UIUpdate(**data)
        elif msg_type == 'ui_append':
            return UIAppend(**data)
        elif msg_type == 'ui_upsert':
            return UIUpsert(**data)
        elif msg_type == 'auth_required':
            return AuthRequired(**data)
        elif msg_type == 'register_agent':
            return RegisterAgent.from_json(json_str)
        elif msg_type == 'register_ui':
            return RegisterUI.from_json(json_str)
        elif msg_type == 'tool_progress':
            return ToolProgress(**data)
        elif msg_type == 'tool_stream_data':
            return ToolStreamData(**data)
        elif msg_type == 'tool_stream_end':
            return ToolStreamEnd(**data)
        elif msg_type == 'tool_stream_cancel':
            return ToolStreamCancel(**data)
        elif msg_type == 'audit_append':
            return AuditAppend(**data)
        elif msg_type == 'llm_config_set':
            return LLMConfigSet(**data)
        elif msg_type == 'llm_config_clear':
            return LLMConfigClear(**data)
        elif msg_type == 'llm_config_ack':
            return LLMConfigAck(**data)
        elif msg_type == 'llm_usage_report':
            return LLMUsageReport(**data)
        elif msg_type == 'agent_hop_request':
            return AgentHopRequest(**data)
        elif msg_type == 'agent_hop_response':
            return AgentHopResponse(**data)
        elif msg_type == 'conversation_snapshot':
            return ConversationSnapshot.from_dict(data)
        elif msg_type == 'conversation_commit_ready':
            return ConversationCommitReady.from_dict(data)
        elif msg_type == 'operation_status':
            return OperationStatus.from_dict(data)
        elif msg_type == 'agent_lifecycle':
            return AgentLifecycle.from_dict(data)
        elif msg_type == 'agent_host_registered':
            return AgentHostRegistered.from_dict(data)
        elif msg_type == 'chat_created':
            return ChatCreated.from_dict(data)
        elif msg_type == 'voice_control_binding':
            return VoiceControlBinding.from_dict(data)
        elif msg_type == 'user_message_acked':
            return UserMessageAcknowledged.from_dict(data)
        elif msg_type == 'voice_submission_rejected':
            return VoiceSubmissionRejected.from_dict(data)
        elif msg_type == 'voice_playout_event':
            _require_raw_utf8_size(
                json_str,
                maximum=_VOICE_PLAYOUT_MAX_BYTES,
                label="voice_playout_event",
            )
            return VoicePlayoutEvent.from_dict(data)
        elif msg_type == 'voice_local_ready':
            return VoiceLocalReady.from_dict(data)
        elif msg_type == 'voice_local_session_ready':
            return VoiceLocalSessionReady.from_dict(data)
        elif msg_type == 'voice_local_recognition_started':
            return VoiceLocalRecognitionStarted.from_dict(data)
        elif msg_type == 'voice_local_turn_bound':
            return VoiceLocalTurnBound.from_dict(data)
        elif msg_type == 'voice_local_final':
            return VoiceLocalFinal.from_dict(data)
        elif msg_type == 'voice_local_recognition_failed':
            return VoiceLocalRecognitionFailed.from_dict(data)
        elif msg_type == 'voice_local_final_rejected':
            return VoiceLocalFinalRejected.from_dict(data)
        elif msg_type == 'voice_local_announcement':
            return VoiceLocalAnnouncement.from_dict(data)
        elif msg_type == 'voice_local_playout_event':
            return VoiceLocalPlayoutEvent.from_dict(data)
        return Message(**data)

# --- MCP Protocol Wrappers ---
@dataclass
class MCPRequest(Message):
    """A tool-call request from orchestrator to agent.

    For streaming tools (001-tool-stream-ui), the orchestrator sets the
    following conventional keys inside ``params`` (no schema change required
    because params is already an open dict):

    - ``_stream``: ``True`` when the orchestrator wants the agent to treat
      the request as long-lived and emit ``ToolStreamData`` chunks instead of
      a single ``MCPResponse``.
    - ``_stream_id``: The canonical ``stream_id`` the agent must echo on
      every emitted chunk so the orchestrator can correlate them back to the
      originating ``StreamSubscription``.

    Agents that do not understand these keys (e.g. when ``FF_TOOL_STREAMING``
    is off) MUST ignore them and run the tool to completion as a normal
    one-shot call.
    """
    type: str = "mcp_request"
    request_id: str = ""
    method: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    # 064 Phase A: per-request modern MCP metadata stays on the envelope. It
    # must never be injected into params["arguments"], because several agents
    # intentionally splat that mapping into a tool signature.
    protocol_version: Optional[str] = None
    caller_capabilities: Optional[Dict[str, Any]] = None
    caller_info: Optional[Dict[str, Any]] = None

    def validate_protocol_metadata(self, *, allow_legacy: bool = True) -> None:
        """Validate modern metadata without inferring cross-request state."""

        modern_declared = any(
            value is not None
            for value in (
                self.protocol_version,
                self.caller_capabilities,
                self.caller_info,
            )
        )
        if not modern_declared and allow_legacy:
            return
        if self.protocol_version is None or self.caller_capabilities is None:
            raise MCPProtocolError(
                MCP_INVALID_PARAMS,
                "protocol_version and caller_capabilities are required",
            )
        if self.protocol_version not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPProtocolError(
                MCP_UNSUPPORTED_PROTOCOL_VERSION,
                f"Unsupported MCP protocol version: {self.protocol_version}",
                data={"supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS)},
            )
        if not isinstance(self.caller_capabilities, dict):
            raise MCPProtocolError(
                MCP_INVALID_PARAMS,
                "caller_capabilities must be an object",
            )
        if self.caller_info is not None and not isinstance(self.caller_info, dict):
            raise MCPProtocolError(MCP_INVALID_PARAMS, "caller_info must be an object")

@dataclass
class MCPResponse(Message):
    type: str = "mcp_response"
    request_id: str = ""
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    ui_components: Optional[List[Dict[str, Any]]] = None
    # Feature 004: correlation_id propagated from the orchestrator's
    # ToolDispatchAudit context so every produced UI component can be
    # tagged with the originating tool dispatch's audit correlation_id.
    # The orchestrator stamps this onto the response after the audit
    # context closes; agents do not set it.
    correlation_id: Optional[str] = None
    # 064 Phase A: the internal snake_case projection of MCP Result.resultType
    # plus the SHOULD-level identity of the responder.
    result_type: str = MCP_RESULT_COMPLETE
    responder_info: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.validate_result_shape()

    def validate_result_shape(self) -> None:
        if not isinstance(self.result_type, str) or not self.result_type:
            raise ProtocolValidationError("result_type must be a non-empty string")
        if self.responder_info is not None and not isinstance(self.responder_info, dict):
            raise ProtocolValidationError("responder_info must be an object")
        if self.error is not None and not isinstance(self.error, dict):
            raise ProtocolValidationError("error must be an object")
        if self.ui_components is not None and not isinstance(self.ui_components, list):
            raise ProtocolValidationError("ui_components must be an array")
        if self.error is not None and (
            self.result is not None or self.ui_components
        ):
            raise ProtocolValidationError(
                "an MCP response cannot carry an error with a result or renderable components"
            )

@dataclass
class AgentHopRequest(Message):
    """056 US1 — an agent's request for a MEDIATED hop to a peer agent's tool.

    Sent by ``AgentRuntime.call_agent_tool`` over the agent's existing control
    channel (the in-process loopback for built-ins, the agent WebSocket for
    networked agents) — never a peer connection. The orchestrator resolves the
    hop against its OWN dispatch record for ``parent_request_id`` (the
    initiator supplies no authority) and re-enters the full single-path gate
    stack under a freshly minted child delegation. Backend-internal: not part
    of the client UI protocol (ui_protocol.json unchanged).
    """
    type: str = "agent_hop_request"
    request_id: str = ""
    parent_request_id: str = ""
    initiator_agent_id: str = ""
    callee_agent_id: str = ""
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)

@dataclass
class AgentHopResponse(Message):
    """056 US1 — the mediated hop's outcome, delivered back to the initiator.

    ``response`` carries the peer MCPResponse fields (result/error/
    ui_components). For in-process initiators the orchestrator resolves the
    awaiting future directly; this frame is the networked-agent delivery.
    """
    type: str = "agent_hop_response"
    request_id: str = ""
    response: Optional[Dict[str, Any]] = None

# --- UI Protocol ---
@dataclass
class UIEvent(Message):
    type: str = "ui_event"
    action: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    # Feature 060: client-created operation identity is carried at the frame
    # boundary as well as in payload for thin-client compatibility.  These
    # fields are optional only for direct/internal legacy seams; the finite
    # connection ingress requires both before admission.
    submission_id: Optional[str] = None
    request_generation: Optional[str] = None
    connection_generation: Optional[str] = None
    snapshot_purpose: Optional[str] = None
    surface: Optional[str] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate the open action payload and every supplied scope identity."""

        if self.type != "ui_event":
            raise ProtocolValidationError("type must be ui_event")
        _require_snake_case(self.action, "action")
        if not isinstance(self.payload, dict):
            raise ProtocolValidationError("payload must be an object")
        if "voice_origin" in self.payload:
            if self.action != "chat_message":
                raise ProtocolValidationError(
                    "voice_origin is valid only on the chat_message action"
                )
            VoiceOrigin.from_dict(self.payload["voice_origin"])

        identity_fields = (
            "submission_id",
            "request_generation",
            "connection_generation",
        )
        for field_name in identity_fields:
            frame_value = getattr(self, field_name)
            payload_value = self.payload.get(field_name)
            if frame_value is not None:
                _require_uuid4(frame_value, field_name)
            if payload_value is not None:
                _require_uuid4(payload_value, f"payload.{field_name}")
            if (
                frame_value is not None
                and payload_value is not None
                and frame_value != payload_value
            ):
                raise ProtocolValidationError(
                    f"{field_name} must match payload.{field_name}"
                )

        payload_surface = self.payload.get("surface")
        if self.surface is not None:
            _require_snake_case(self.surface, "surface")
        if payload_surface is not None:
            _require_snake_case(payload_surface, "payload.surface")
        if (
            self.surface is not None
            and payload_surface is not None
            and self.surface != payload_surface
        ):
            raise ProtocolValidationError("surface must match payload.surface")

        payload_purpose = self.payload.get("snapshot_purpose")
        for value, field_name in (
            (self.snapshot_purpose, "snapshot_purpose"),
            (payload_purpose, "payload.snapshot_purpose"),
        ):
            if value is not None and value not in {"hydration", "commit"}:
                raise ProtocolValidationError(
                    f"{field_name} must be hydration or commit"
                )
        if (
            self.snapshot_purpose is not None
            and payload_purpose is not None
            and self.snapshot_purpose != payload_purpose
        ):
            raise ProtocolValidationError(
                "snapshot_purpose must match payload.snapshot_purpose"
            )

    @property
    def voice_origin(self) -> Optional["VoiceOrigin"]:
        """Return a validated voice binding without mutating the open payload."""

        value = self.payload.get("voice_origin")
        if value is None:
            return None
        return VoiceOrigin.from_dict(value)

    def to_json(self) -> str:
        self.validate()
        return super().to_json()


@dataclass(frozen=True)
class VoiceOrigin:
    """Immutable proof-bearing origin copied onto a normal chat message.

    This parser validates the complete public shape only. Equality with
    server-created turn state, proof freshness, and constant-time proof
    verification remain at the authenticated ingress boundary (T071).
    """

    schema_version: str
    session_id: str
    generation: int
    media_grant_revision: int
    turn_id: str
    client_turn_id: str
    chat_context_revision: int
    source_participant_identity: str
    detected_language: str
    text_digest_sha256: str
    transcript_proof: str = field(repr=False)
    proof_expires_at: str = ""

    _FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "session_id",
        "generation",
        "media_grant_revision",
        "turn_id",
        "client_turn_id",
        "chat_context_revision",
        "source_participant_identity",
        "detected_language",
        "text_digest_sha256",
        "transcript_proof",
        "proof_expires_at",
    }

    def validate(self) -> None:
        _require_voice_schema_version(self.schema_version)
        for name in ("session_id", "turn_id", "client_turn_id"):
            _require_uuid4(getattr(self, name), name)
        _require_positive_integer(self.generation, "generation")
        _require_positive_integer(
            self.media_grant_revision, "media_grant_revision"
        )
        _require_positive_integer(
            self.chat_context_revision, "chat_context_revision"
        )
        identity = _require_bounded_string(
            self.source_participant_identity,
            "source_participant_identity",
            minimum=1,
            maximum=128,
        )
        if _VOICE_OPAQUE_ID.fullmatch(identity) is None:
            raise ProtocolValidationError(
                "source_participant_identity is not a canonical opaque ID"
            )
        language = _require_bounded_string(
            self.detected_language,
            "detected_language",
            minimum=2,
            maximum=32,
        )
        if _VOICE_LANGUAGE.fullmatch(language) is None:
            raise ProtocolValidationError("detected_language is not canonical")
        if _LOWER_SHA256.fullmatch(self.text_digest_sha256) is None:
            raise ProtocolValidationError(
                "text_digest_sha256 must be 64 lowercase hexadecimal characters"
            )
        if _LOWER_SHA256.fullmatch(self.transcript_proof) is None:
            raise ProtocolValidationError(
                "transcript_proof must be 64 lowercase hexadecimal characters"
            )
        _require_voice_timestamp(self.proof_expires_at, "proof_expires_at")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {name: getattr(self, name) for name in self._FIELDS}

    @classmethod
    def from_dict(cls, data: object) -> "VoiceOrigin":
        if not isinstance(data, Mapping):
            raise ProtocolValidationError("voice_origin must be an object")
        _require_exact_fields(data, cls._FIELDS, label="voice_origin")
        origin = cls(**dict(data))
        origin.validate()
        return origin


@dataclass
class CorrelatedNewChat(UIEvent):
    """Strict idempotent ordinary ``new_chat`` action used by voice start."""

    action: str = "new_chat"
    schema_version: str = _VOICE_SCHEMA_VERSION

    _FIELDS: ClassVar[set[str]] = {
        "type",
        "action",
        "schema_version",
        "connection_generation",
        "submission_id",
        "request_generation",
        "payload",
    }
    _CORRELATIONS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "connection_generation",
        "submission_id",
        "request_generation",
    )

    def validate(self) -> None:
        if self.type != "ui_event" or self.action != "new_chat":
            raise ProtocolValidationError(
                "correlated new_chat must be a ui_event new_chat action"
            )
        if any(
            value is not None
            for value in (
                self.session_id,
                self.snapshot_purpose,
                self.surface,
            )
        ):
            raise ProtocolValidationError(
                "correlated new_chat cannot carry unrelated UI-event fields"
            )
        _require_voice_schema_version(self.schema_version)
        for name in (
            "connection_generation",
            "submission_id",
            "request_generation",
        ):
            _require_uuid4(getattr(self, name), name)
        if not isinstance(self.payload, Mapping):
            raise ProtocolValidationError("new_chat payload must be an object")
        payload_fields = set(self._CORRELATIONS)
        _require_exact_fields(
            self.payload,
            payload_fields,
            label="new_chat payload",
        )
        for name in self._CORRELATIONS:
            if getattr(self, name) != self.payload.get(name):
                raise ProtocolValidationError(
                    f"{name} must match payload.{name}"
                )

    def to_json(self) -> str:
        self.validate()
        return _voice_json(
            {
                "type": self.type,
                "action": self.action,
                "schema_version": self.schema_version,
                "connection_generation": self.connection_generation,
                "submission_id": self.submission_id,
                "request_generation": self.request_generation,
                "payload": dict(self.payload),
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CorrelatedNewChat":
        _require_exact_fields(data, cls._FIELDS, label="correlated new_chat")
        frame = cls(**dict(data))
        frame.validate()
        return frame


@dataclass
class ChatCreated(Message):
    """Strict owner-validated reply to :class:`CorrelatedNewChat`."""

    type: str = "chat_created"
    schema_version: str = _VOICE_SCHEMA_VERSION
    connection_generation: str = ""
    submission_id: str = ""
    request_generation: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    _FIELDS: ClassVar[set[str]] = {
        "type",
        "schema_version",
        "connection_generation",
        "submission_id",
        "request_generation",
        "payload",
    }
    _PAYLOAD_FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "chat_id",
        "from_message",
        "connection_generation",
        "submission_id",
        "request_generation",
    }

    def validate(self) -> None:
        if self.type != "chat_created":
            raise ProtocolValidationError("type must be chat_created")
        _require_voice_schema_version(self.schema_version)
        for name in (
            "connection_generation",
            "submission_id",
            "request_generation",
        ):
            _require_uuid4(getattr(self, name), name)
        if not isinstance(self.payload, Mapping):
            raise ProtocolValidationError("chat_created payload must be an object")
        _require_exact_fields(
            self.payload,
            self._PAYLOAD_FIELDS,
            label="chat_created payload",
        )
        _require_uuid4(self.payload.get("chat_id"), "payload.chat_id")
        if self.payload.get("from_message") is not False:
            raise ProtocolValidationError(
                "correlated chat_created payload.from_message must be false"
            )
        for name in (
            "schema_version",
            "connection_generation",
            "submission_id",
            "request_generation",
        ):
            if getattr(self, name) != self.payload.get(name):
                raise ProtocolValidationError(
                    f"{name} must match payload.{name}"
                )

    def to_json(self) -> str:
        self.validate()
        return _voice_json(
            {
                "type": self.type,
                "schema_version": self.schema_version,
                "connection_generation": self.connection_generation,
                "submission_id": self.submission_id,
                "request_generation": self.request_generation,
                "payload": dict(self.payload),
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatCreated":
        _require_exact_fields(data, cls._FIELDS, label="chat_created")
        frame = cls(**dict(data))
        frame.validate()
        return frame


@dataclass
class VoiceControlBinding(Message):
    """One-use delivery frame for an opaque, memory-only control bearer."""

    type: str = "voice_control_binding"
    schema_version: str = _VOICE_SCHEMA_VERSION
    device_id: str = ""
    connection_generation: str = ""
    binding_id: str = ""
    binding: str = field(default="", repr=False)
    expires_at: str = ""

    _FIELDS: ClassVar[set[str]] = {
        "type",
        "schema_version",
        "device_id",
        "connection_generation",
        "binding_id",
        "binding",
        "expires_at",
    }

    def validate(self) -> None:
        if self.type != "voice_control_binding":
            raise ProtocolValidationError("type must be voice_control_binding")
        _require_voice_schema_version(self.schema_version)
        for name in ("device_id", "connection_generation", "binding_id"):
            _require_uuid4(getattr(self, name), name)
        _require_bounded_string(
            self.binding,
            "binding",
            minimum=32,
            maximum=512,
        )
        _require_voice_timestamp(self.expires_at, "expires_at")

    def validate_lifetime(
        self,
        *,
        received_at: datetime,
        credential_expires_at: datetime,
    ) -> None:
        """Reject a stale bearer or one that outlives its bounded authority."""

        self.validate()
        received = _require_aware_datetime(received_at, "received_at")
        credential_expiry = _require_aware_datetime(
            credential_expires_at, "credential_expires_at"
        )
        expiry = _voice_timestamp_datetime(self.expires_at, "expires_at")
        if expiry <= received:
            raise ProtocolValidationError("control binding is expired")
        if expiry > received + _VOICE_CONTROL_BINDING_MAX_LIFETIME:
            raise ProtocolValidationError(
                "control binding lifetime exceeds ten minutes"
            )
        if expiry > credential_expiry:
            raise ProtocolValidationError(
                "control binding outlives the authenticated credential"
            )

    def redacted_dict(self) -> Dict[str, Any]:
        """Return the only representation safe for diagnostics."""

        self.validate()
        return {
            "type": self.type,
            "schema_version": self.schema_version,
            "device_id": self.device_id,
            "connection_generation": self.connection_generation,
            "binding_id": self.binding_id,
            "binding": "[REDACTED]",
            "expires_at": self.expires_at,
        }

    def to_json(self) -> str:
        self.validate()
        return _voice_json(
            {
                "type": self.type,
                "schema_version": self.schema_version,
                "device_id": self.device_id,
                "connection_generation": self.connection_generation,
                "binding_id": self.binding_id,
                "binding": self.binding,
                "expires_at": self.expires_at,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceControlBinding":
        _require_exact_fields(data, cls._FIELDS, label="voice_control_binding")
        frame = cls(**dict(data))
        frame.validate()
        return frame


class VoiceControlBindingMemory:
    """Ephemeral per-socket binding slot with rotation and expiry fencing."""

    __slots__ = ("_current",)

    def __init__(self) -> None:
        self._current: Optional[VoiceControlBinding] = None

    def __repr__(self) -> str:
        return f"VoiceControlBindingMemory(active={self._current is not None})"

    def rotate(
        self,
        binding: VoiceControlBinding,
        *,
        received_at: datetime,
        credential_expires_at: datetime,
    ) -> None:
        """Install a fresh bearer and irreversibly supersede the prior one."""

        if not isinstance(binding, VoiceControlBinding):
            raise ProtocolValidationError("binding must be a voice control binding")
        binding.validate_lifetime(
            received_at=received_at,
            credential_expires_at=credential_expires_at,
        )
        prior = self._current
        if prior is not None:
            if binding.device_id != prior.device_id:
                raise ProtocolValidationError(
                    "control binding rotation cannot change device identity"
                )
            if (
                binding.binding_id == prior.binding_id
                or binding.binding == prior.binding
            ):
                raise ProtocolValidationError(
                    "control binding rotation must replace bearer identity"
                )
        self._current = binding

    def current(
        self,
        *,
        device_id: str,
        connection_generation: str,
        at: datetime,
    ) -> Optional[VoiceControlBinding]:
        """Return only a live exact-scope binding, clearing expired memory."""

        _require_uuid4(device_id, "device_id")
        _require_uuid4(connection_generation, "connection_generation")
        now = _require_aware_datetime(at, "at")
        current = self._current
        if current is None:
            return None
        if _voice_timestamp_datetime(current.expires_at, "expires_at") <= now:
            self._current = None
            return None
        if (
            current.device_id != device_id
            or current.connection_generation != connection_generation
        ):
            return None
        return current

    def clear(self) -> None:
        """Drop the bearer synchronously on socket/auth teardown."""

        self._current = None


_VOICE_LOCAL_COMMON_FIELDS = {
    "type", "schema_version", "speech_backend", "device_id",
    "connection_generation", "session_id", "generation", "speech_revision",
}
_VOICE_LOCAL_TURN_FIELDS = {
    "client_turn_id", "turn_id", "submission_id", "request_generation",
    "chat_id", "chat_context_revision", "recognition_sequence",
}
_VOICE_LOCAL_KINDS = {
    "greeting", "acknowledgement", "progress", "waiting", "result",
    "sensitive_notice", "failure", "refusal", "cancellation",
}


@dataclass
class _VoiceLocalCommon(Message):
    """Shared strict identity fence for client-local schema-v2 frames."""

    type: str = ""
    schema_version: str = "2"
    speech_backend: str = "client_local"
    device_id: str = ""
    connection_generation: str = ""
    session_id: str = ""
    generation: int = 0
    speech_revision: int = 0

    def _validate_common(self, expected_type: str) -> None:
        if self.type != expected_type:
            raise ProtocolValidationError(f"type must be {expected_type}")
        if self.schema_version != "2":
            raise ProtocolValidationError("schema_version must be exactly \"2\"")
        if self.speech_backend != "client_local":
            raise ProtocolValidationError("speech_backend must be client_local")
        for name in ("device_id", "connection_generation", "session_id"):
            _require_uuid4(getattr(self, name), name)
        for name in ("generation", "speech_revision"):
            _require_positive_integer(
                getattr(self, name),
                name,
                maximum=9_223_372_036_854_775_807,
            )

    def _to_local_json(self) -> str:
        return _voice_json(asdict(self))


def _validate_local_turn(frame: Any) -> None:
    for name in (
        "client_turn_id", "turn_id", "submission_id", "request_generation", "chat_id",
    ):
        _require_uuid4(getattr(frame, name), name)
    for name in ("chat_context_revision", "recognition_sequence"):
        _require_positive_integer(
            getattr(frame, name),
            name,
            maximum=9_223_372_036_854_775_807,
        )


def _validate_local_digest(value: object) -> None:
    if not isinstance(value, str) or _LOWER_SHA256.fullmatch(value) is None:
        raise ProtocolValidationError("text_digest_sha256 must be lowercase SHA-256")


@dataclass
class VoiceLocalReady(_VoiceLocalCommon):
    type: str = "voice_local_ready"
    contract: str = "client_local/v1"
    transport: str = "client_local"
    configured_locale: str = "en-US"
    full_duplex: bool = False
    has_microphone: bool = True
    has_audio_output: bool = True
    microphone_permission: str = "authorized"
    recognition_permission: str = "authorized"
    recognition_processing: str = "guaranteed_local"
    recognition_locale: str = "ready"
    recognition_installation: str = "ready"
    synthesis_processing: str = "guaranteed_local"
    synthesis_locale: str = "ready"
    client_sequence: int = 0

    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | {
        "contract", "transport", "configured_locale", "full_duplex",
        "has_microphone", "has_audio_output", "microphone_permission",
        "recognition_permission", "recognition_processing", "recognition_locale",
        "recognition_installation", "synthesis_processing", "synthesis_locale",
        "client_sequence",
    }

    def validate(self) -> None:
        self._validate_common("voice_local_ready")
        exact = {
            "contract": "client_local/v1", "transport": "client_local",
            "configured_locale": "en-US", "full_duplex": False,
            "has_microphone": True, "has_audio_output": True,
            "microphone_permission": "authorized",
            "recognition_permission": "authorized",
            "recognition_processing": "guaranteed_local",
            "recognition_locale": "ready", "recognition_installation": "ready",
            "synthesis_processing": "guaranteed_local", "synthesis_locale": "ready",
        }
        if any(getattr(self, name) != value for name, value in exact.items()):
            raise ProtocolValidationError("voice_local_ready capability is not eligible")
        _require_positive_integer(self.client_sequence, "client_sequence")

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalReady":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_ready")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class VoiceLocalSessionReady(_VoiceLocalCommon):
    type: str = "voice_local_session_ready"
    contract: str = "client_local/v1"
    transport: str = "client_local"
    configured_locale: str = "en-US"
    chat_id: str = ""
    chat_context_revision: int = 0
    applied_chat_context_revision: int = 0
    foreground_active: bool = True
    microphone_enabled: bool = False
    speech_muted: bool = False
    lease_expires_at: str = ""

    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | {
        "contract", "transport", "configured_locale", "chat_id",
        "chat_context_revision", "applied_chat_context_revision",
        "foreground_active", "microphone_enabled", "speech_muted",
        "lease_expires_at",
    }

    def validate(self) -> None:
        self._validate_common("voice_local_session_ready")
        if (self.contract, self.transport, self.configured_locale) != (
            "client_local/v1", "client_local", "en-US"
        ) or self.foreground_active is not True:
            raise ProtocolValidationError("voice_local_session_ready is malformed")
        if not isinstance(self.microphone_enabled, bool) or not isinstance(self.speech_muted, bool):
            raise ProtocolValidationError("local session booleans are malformed")
        _require_uuid4(self.chat_id, "chat_id")
        _require_positive_integer(self.chat_context_revision, "chat_context_revision")
        _require_positive_integer(self.applied_chat_context_revision, "applied_chat_context_revision")
        _require_voice_timestamp(self.lease_expires_at, "lease_expires_at")

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalSessionReady":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_session_ready")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class VoiceLocalRecognitionStarted(_VoiceLocalCommon):
    type: str = "voice_local_recognition_started"
    client_turn_id: str = ""
    chat_id: str = ""
    chat_context_revision: int = 0
    recognition_sequence: int = 0

    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | {
        "client_turn_id", "chat_id", "chat_context_revision", "recognition_sequence"
    }

    def validate(self) -> None:
        self._validate_common("voice_local_recognition_started")
        _require_uuid4(self.client_turn_id, "client_turn_id")
        _require_uuid4(self.chat_id, "chat_id")
        _require_positive_integer(self.chat_context_revision, "chat_context_revision")
        _require_positive_integer(self.recognition_sequence, "recognition_sequence")

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalRecognitionStarted":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_recognition_started")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class _VoiceLocalTurn(_VoiceLocalCommon):
    client_turn_id: str = ""
    turn_id: str = ""
    submission_id: str = ""
    request_generation: str = ""
    chat_id: str = ""
    chat_context_revision: int = 0
    recognition_sequence: int = 0


@dataclass
class VoiceLocalTurnBound(_VoiceLocalTurn):
    type: str = "voice_local_turn_bound"
    binding_expires_at: str = ""
    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | _VOICE_LOCAL_TURN_FIELDS | {"binding_expires_at"}

    def validate(self) -> None:
        self._validate_common("voice_local_turn_bound")
        _validate_local_turn(self)
        _require_voice_timestamp(self.binding_expires_at, "binding_expires_at")

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalTurnBound":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_turn_bound")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class VoiceLocalFinal(_VoiceLocalTurn):
    type: str = "voice_local_final"
    final: bool = True
    recognized_locale: str = "en-US"
    text: str = ""
    text_digest_sha256: str = ""
    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | _VOICE_LOCAL_TURN_FIELDS | {
        "final", "recognized_locale", "text", "text_digest_sha256"
    }

    def validate(self) -> None:
        self._validate_common("voice_local_final")
        _validate_local_turn(self)
        if self.final is not True or self.recognized_locale != "en-US":
            raise ProtocolValidationError("voice_local_final is malformed")
        _require_bounded_string(self.text, "text", minimum=1, maximum=8000)
        _require_raw_utf8_size(self.text, maximum=32_000, label="voice_local_final.text")
        _validate_local_digest(self.text_digest_sha256)

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalFinal":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_final")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class VoiceLocalRecognitionFailed(_VoiceLocalTurn):
    type: str = "voice_local_recognition_failed"
    reason: str = ""
    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | _VOICE_LOCAL_TURN_FIELDS | {"reason"}
    _REASONS: ClassVar[set[str]] = {
        "local_recognition_failed", "local_recognition_cancelled",
        "local_audio_interrupted", "local_final_empty", "local_final_malformed",
        "stopped_by_user",
    }

    def validate(self) -> None:
        self._validate_common("voice_local_recognition_failed")
        _validate_local_turn(self)
        if self.reason not in self._REASONS:
            raise ProtocolValidationError("reason is not canonical")

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalRecognitionFailed":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_recognition_failed")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class VoiceLocalFinalRejected(_VoiceLocalTurn):
    type: str = "voice_local_final_rejected"
    reason: str = ""
    retry_policy: str = "none"
    occurred_at: str = ""
    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | _VOICE_LOCAL_TURN_FIELDS | {"reason", "retry_policy", "occurred_at"}
    _REASONS: ClassVar[set[str]] = {
        "invalid_binding", "stale_connection", "stale_session", "stale_speech_revision",
        "stale_chat_context", "stale_local_turn", "altered_local_final",
        "local_final_empty", "local_final_oversized", "local_final_malformed",
        "local_language_mismatch", "capacity_exhausted",
    }

    def validate(self) -> None:
        self._validate_common("voice_local_final_rejected")
        _validate_local_turn(self)
        if self.reason not in self._REASONS or self.retry_policy not in {"none", "explicit_user_retry"}:
            raise ProtocolValidationError("voice_local_final_rejected is malformed")
        _require_voice_timestamp(self.occurred_at, "occurred_at")

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalFinalRejected":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_final_rejected")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class VoiceLocalAnnouncement(_VoiceLocalCommon):
    type: str = "voice_local_announcement"
    announcement_id: str = ""
    announcement_sequence: int = 0
    turn_id: Optional[str] = None
    kind: str = ""
    output_policy: str = ""
    locale: str = "en-US"
    text: str = ""
    text_digest_sha256: str = ""
    expires_at: str = ""
    foreground_required: bool = True
    mute_revision: int = 0
    consent_revision: int = 0
    _FIELDS: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | {
        "announcement_id", "announcement_sequence", "turn_id", "kind",
        "output_policy", "locale", "text", "text_digest_sha256", "expires_at",
        "foreground_required", "mute_revision", "consent_revision",
    }

    def validate(self) -> None:
        self._validate_common("voice_local_announcement")
        _require_uuid4(self.announcement_id, "announcement_id")
        _require_positive_integer(self.announcement_sequence, "announcement_sequence")
        if self.kind not in _VOICE_LOCAL_KINDS or self.output_policy not in {"lifecycle", "full_recap"}:
            raise ProtocolValidationError("voice_local_announcement policy is malformed")
        if self.kind == "greeting":
            if self.turn_id is not None:
                raise ProtocolValidationError("greeting turn_id must be null")
        else:
            _require_uuid4(self.turn_id, "turn_id")
        if self.locale != "en-US" or self.foreground_required is not True:
            raise ProtocolValidationError("voice_local_announcement is malformed")
        _require_bounded_string(self.text, "text", minimum=1, maximum=600)
        _require_raw_utf8_size(self.text, maximum=600, label="voice_local_announcement.text")
        _validate_local_digest(self.text_digest_sha256)
        _require_voice_timestamp(self.expires_at, "expires_at")
        _require_positive_integer(self.mute_revision, "mute_revision")
        _require_positive_integer(self.consent_revision, "consent_revision")

    def to_json(self) -> str:
        self.validate()
        return self._to_local_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalAnnouncement":
        _require_exact_fields(data, cls._FIELDS, label="voice_local_announcement")
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class VoiceLocalPlayoutEvent(_VoiceLocalCommon):
    type: str = "voice_local_playout_event"
    announcement_id: str = ""
    announcement_sequence: int = 0
    turn_id: Optional[str] = None
    kind: str = ""
    phase: str = ""
    client_sequence: int = 0
    observed_at: str = ""
    reason: Optional[str] = None
    _REQUIRED: ClassVar[set[str]] = _VOICE_LOCAL_COMMON_FIELDS | {
        "announcement_id", "announcement_sequence", "turn_id", "kind", "phase",
        "client_sequence", "observed_at",
    }

    def validate(self) -> None:
        self._validate_common("voice_local_playout_event")
        _require_uuid4(self.announcement_id, "announcement_id")
        _require_positive_integer(self.announcement_sequence, "announcement_sequence")
        if self.kind not in _VOICE_LOCAL_KINDS or self.phase not in {"started", "finished", "interrupted", "failed"}:
            raise ProtocolValidationError("voice_local_playout_event is malformed")
        if self.kind == "greeting":
            if self.turn_id is not None:
                raise ProtocolValidationError("greeting turn_id must be null")
        else:
            _require_uuid4(self.turn_id, "turn_id")
        _require_positive_integer(self.client_sequence, "client_sequence")
        _require_voice_timestamp(self.observed_at, "observed_at")
        if self.reason not in {
            None, "local_synthesis_failed", "local_audio_interrupted",
            "local_announcement_expired", "stopped_by_user",
        }:
            raise ProtocolValidationError("playout reason is not canonical")

    def _wire(self) -> Dict[str, Any]:
        value = asdict(self)
        if value["reason"] is None:
            value.pop("reason")
        return value

    def to_json(self) -> str:
        self.validate()
        return _voice_json(self._wire())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceLocalPlayoutEvent":
        _require_exact_fields(
            data,
            cls._REQUIRED,
            optional={"reason"},
            label="voice_local_playout_event",
        )
        value = cls(**dict(data))
        value.validate()
        return value


@dataclass
class UserMessageAcknowledged(Message):
    """Durable message acceptance with complete retry correlation."""

    type: str = "user_message_acked"
    schema_version: str = _VOICE_SCHEMA_VERSION
    chat_id: str = ""
    message_id: int = 0
    submission_id: str = ""
    request_generation: str = ""
    connection_generation: str = ""
    voice_turn_id: Optional[str] = None

    _FIELDS: ClassVar[set[str]] = {
        "type",
        "schema_version",
        "chat_id",
        "message_id",
        "submission_id",
        "request_generation",
        "connection_generation",
        "voice_turn_id",
    }

    def validate(self) -> None:
        if self.type != "user_message_acked":
            raise ProtocolValidationError("type must be user_message_acked")
        _require_voice_schema_version(self.schema_version)
        for name in (
            "chat_id",
            "submission_id",
            "request_generation",
            "connection_generation",
        ):
            _require_uuid4(getattr(self, name), name)
        _require_positive_integer(self.message_id, "message_id")
        if self.voice_turn_id is not None:
            _require_uuid4(self.voice_turn_id, "voice_turn_id")

    def to_json(self) -> str:
        self.validate()
        return _voice_json(asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UserMessageAcknowledged":
        _require_exact_fields(data, cls._FIELDS, label="user_message_acked")
        frame = cls(**dict(data))
        frame.validate()
        return frame


@dataclass
class VoiceSubmissionRejected(Message):
    """Terminal, fully correlated refusal of one voice-origin submission."""

    type: str = "voice_submission_rejected"
    schema_version: str = _VOICE_SCHEMA_VERSION
    session_id: str = ""
    connection_generation: str = ""
    generation: int = 0
    media_grant_revision: int = 0
    turn_id: str = ""
    client_turn_id: str = ""
    submission_id: str = ""
    request_generation: str = ""
    chat_id: str = ""
    reason: str = ""
    retry_policy: str = ""
    occurred_at: str = ""
    message: Optional[str] = None

    _REQUIRED_FIELDS: ClassVar[set[str]] = {
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
    _REASONS: ClassVar[set[str]] = {
        "capacity_exhausted",
        "chat_unavailable",
        "invalid_binding",
        "invalid_proof",
        "proof_expired",
        "permission_denied",
        "stale_session",
        "malformed_final",
    }

    def validate(self) -> None:
        if self.type != "voice_submission_rejected":
            raise ProtocolValidationError("type must be voice_submission_rejected")
        _require_voice_schema_version(self.schema_version)
        for name in (
            "session_id",
            "connection_generation",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
        ):
            _require_uuid4(getattr(self, name), name)
        _require_positive_integer(self.generation, "generation")
        _require_positive_integer(
            self.media_grant_revision, "media_grant_revision"
        )
        if self.reason not in self._REASONS:
            raise ProtocolValidationError("reason is not canonical")
        if self.retry_policy not in {"explicit_user_retry", "none"}:
            raise ProtocolValidationError("retry_policy is not canonical")
        if self.message is not None:
            _require_bounded_string(self.message, "message", maximum=240)
        _require_voice_timestamp(self.occurred_at, "occurred_at")

    def to_json(self) -> str:
        self.validate()
        data = asdict(self)
        if data["message"] is None:
            data.pop("message")
        return _voice_json(data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoiceSubmissionRejected":
        _require_exact_fields(
            data,
            cls._REQUIRED_FIELDS,
            optional={"message"},
            label="voice_submission_rejected",
        )
        frame = cls(**dict(data))
        frame.validate()
        return frame


@dataclass
class VoicePlayoutEvent(Message):
    """Content-free local playout observation; never a UI action or task."""

    type: str = "voice_playout_event"
    schema_version: str = _VOICE_SCHEMA_VERSION
    device_id: str = ""
    connection_generation: str = ""
    session_id: str = ""
    generation: int = 0
    media_grant_revision: int = 0
    announcement_id: str = ""
    announcement_sequence: int = 0
    turn_id: Optional[str] = None
    kind: str = ""
    quantum_role: str = ""
    quantum_index: int = 0
    phase: str = ""
    client_sequence: int = 0
    observed_at: str = ""
    result_reserved_samples_after: Optional[int] = None

    _REQUIRED_FIELDS: ClassVar[set[str]] = {
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
    _KINDS: ClassVar[set[str]] = {
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

    def validate(self) -> None:
        if self.type != "voice_playout_event":
            raise ProtocolValidationError("type must be voice_playout_event")
        _require_voice_schema_version(self.schema_version)
        for name in (
            "device_id",
            "connection_generation",
            "session_id",
            "announcement_id",
        ):
            _require_uuid4(getattr(self, name), name)
        _require_positive_integer(self.generation, "generation")
        _require_positive_integer(
            self.media_grant_revision, "media_grant_revision"
        )
        _require_positive_integer(
            self.announcement_sequence, "announcement_sequence"
        )
        _require_positive_integer(
            self.quantum_index,
            "quantum_index",
            minimum=0,
            maximum=31,
        )
        _require_positive_integer(
            self.client_sequence,
            "client_sequence",
            minimum=0,
        )
        if self.kind not in self._KINDS:
            raise ProtocolValidationError("kind is not canonical")
        if self.phase not in {"started", "finished", "interrupted"}:
            raise ProtocolValidationError("phase is not canonical")
        if self.kind == "greeting":
            if self.turn_id is not None:
                raise ProtocolValidationError("greeting turn_id must be null")
        else:
            _require_uuid4(self.turn_id, "turn_id")

        if self.quantum_role == "single":
            if self.kind == "result" or self.quantum_index != 0:
                raise ProtocolValidationError("single playout quantum is malformed")
            if self.result_reserved_samples_after is not None:
                raise ProtocolValidationError(
                    "single playout cannot carry a result reservation"
                )
        elif self.quantum_role == "result_opening":
            if self.kind != "result" or self.quantum_index != 0:
                raise ProtocolValidationError("result opening quantum is malformed")
            _require_positive_integer(
                self.result_reserved_samples_after,
                "result_reserved_samples_after",
                maximum=36_000,
            )
        elif self.quantum_role == "result_continuation":
            if self.kind != "result" or self.quantum_index < 1:
                raise ProtocolValidationError(
                    "result continuation quantum is malformed"
                )
            _require_positive_integer(
                self.result_reserved_samples_after,
                "result_reserved_samples_after",
                maximum=720_000,
            )
        else:
            raise ProtocolValidationError("quantum_role is not canonical")
        _require_voice_timestamp(self.observed_at, "observed_at")
        if len(self.to_wire_json().encode("utf-8")) > _VOICE_PLAYOUT_MAX_BYTES:
            raise ProtocolValidationError(
                "voice_playout_event exceeds the 2048-byte UTF-8 envelope limit"
            )

    def _wire_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if data["result_reserved_samples_after"] is None:
            data.pop("result_reserved_samples_after")
        return data

    def to_wire_json(self) -> str:
        """Serialize without recursively invoking validation."""

        return _voice_json(self._wire_dict())

    def to_json(self) -> str:
        self.validate()
        return self.to_wire_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VoicePlayoutEvent":
        _require_exact_fields(
            data,
            cls._REQUIRED_FIELDS,
            optional={"result_reserved_samples_after"},
            label="voice_playout_event",
        )
        frame = cls(**dict(data))
        frame.validate()
        return frame


# --- Feature 004 UI event action names ---------------------------------
# The UIEvent message above is open (action is a free string; payload is
# a free dict) so adding new actions does not require a new dataclass.
# These constants exist purely to make the wire contract greppable and to
# document the valid set in one place.

# Client → server
UI_ACTION_COMPONENT_FEEDBACK = "component_feedback"
UI_ACTION_FEEDBACK_RETRACT = "feedback_retract"
UI_ACTION_FEEDBACK_AMEND = "feedback_amend"

# Server → client (delivered as ui_event messages on the same socket).
UI_ACTION_COMPONENT_FEEDBACK_ACK = "component_feedback_ack"
UI_ACTION_COMPONENT_FEEDBACK_ERROR = "component_feedback_error"
UI_ACTION_FEEDBACK_RETRACT_ACK = "feedback_retract_ack"
UI_ACTION_FEEDBACK_AMEND_ACK = "feedback_amend_ack"

# Optional metadata key attached to each component dict in UIRender.components
# when the component originated from a tool dispatch. The value is the
# audit-log correlation_id of that dispatch (string). Frontend consumers
# treat this as an opaque identifier used to scope feedback submissions.
UI_RENDER_META_CORRELATION_ID = "_source_correlation_id"

@dataclass
class UIRender(Message):
    type: str = "ui_render"
    components: List[Dict[str, Any]] = field(default_factory=list)
    target: str = "canvas"  # "canvas" for SDUI main area, "chat" for floating chat panel
    # Feature 026: server-rendered HTML for web clients (orchestrator renders
    # astralprims primitives via webrender). Structured `components` remain on
    # the wire for programmatic/non-web consumers (FR-018).
    html: Optional[str] = None
    # Feature 051: spoken rendition attached ONLY for watch-profile sockets
    # ({"ssml": ..., "text": ...}); ABSENT — not null — everywhere else
    # (contracts/spoken-rendition.md).
    speech: Optional[Dict[str, str]] = None

    def to_json(self) -> str:
        data = asdict(self)
        if data.get("speech") is None:
            data.pop("speech", None)
        return json.dumps(data)

@dataclass
class UIUpdate(Message):
    type: str = "ui_update"
    components: List[Dict[str, Any]] = field(default_factory=list)
    html: Optional[str] = None  # Feature 026: server-rendered HTML (see UIRender)

@dataclass
class UIAppend(Message):
    type: str = "ui_append"
    target_id: str = ""
    data: Any = None


@dataclass
class UIUpsert(Message):
    """Feature 028 — partial workspace update (additive to the 026 contract).

    Each op carries BOTH the ROTE-adapted structured component dict and the
    web renderer's HTML projection of exactly that dict, mirroring the
    ``ui_stream_data`` dual shape so non-web targets consume the structured
    layer (026 FR-018). ``op`` is ``"upsert"`` (replace node by
    ``data-component-id``, else append) or ``"remove"``.
    """
    type: str = "ui_upsert"
    chat_id: str = ""
    ops: List[Dict[str, Any]] = field(default_factory=list)
    # Feature 051: spoken rendition of this delivery's upserted content for
    # watch-profile sockets; absent elsewhere (contracts/spoken-rendition.md).
    speech: Optional[Dict[str, str]] = None

    def to_json(self) -> str:
        data = asdict(self)
        if data.get("speech") is None:
            data.pop("speech", None)
        return json.dumps(data)


@dataclass
class AuthRequired(Message):
    """Feature 028 — server→client auth recovery signal.

    Replaces the dead-end in-chat error Alert on ``register_ui`` validation
    failure. The client re-fetches ``/auth/session`` (which silently
    refreshes server-side) and retries ``register_ui``; if the session is
    truly gone it redirects to ``/auth/login?next=…``.
    """
    type: str = "auth_required"
    reason: str = "invalid"  # "expired" | "invalid" | "hard_cap"

@dataclass
class ChromeRender(Message):
    """Feature 027 — server-rendered application chrome push.

    Additive to the 026 protocol: carries trusted, server-rendered chrome
    HTML (top bar / settings-surface modal) for the web shell's named
    regions. Canvas/chat content continues to flow as UIRender/UIUpdate
    with components+html (FR-018 untouched). Empty ``html`` for the modal
    region clears it (close).
    """
    type: str = "chrome_render"
    region: str = "modal"  # "modal" | "topbar"
    html: str = ""
    mode: str = "replace"  # reserved; only "replace" in 027

@dataclass
class ChromeMenu(Message):
    """Feature 042 — the server-owned chrome model pushed to native clients.

    Carries the same ``ChromeModel.to_dict()`` the web shell renders and
    ``GET /api/chrome/menu`` returns, so every client (web, Windows, Android,
    future iOS) renders identical chrome from one definition (Constitution
    XII). Emitted right after ``register_ui`` for native SDUI targets, and
    re-emitted on a role/flag change. Web clients ignore it (their shell is
    already server-rendered from the same model).
    """
    type: str = "chrome_menu"
    model: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ChromeSurface(Message):
    """Feature 043 — a settings surface delivered to a native SDUI client.

    The structured twin of ``ChromeRender``'s web-HTML modal: carries the
    surface's ``astralprims`` component dicts (ROTE-adapted for the device) so
    the Windows/Android clients render it through the SAME component renderer
    they use for the chat canvas (Constitution II/XII) — no web view, no
    per-client hand-built surface. Web clients keep receiving ``ChromeRender``
    HTML; a native client that receives this renders ``components`` into its
    modal/sheet and wires the components' ``chrome_*`` actions back over the
    existing ``ui_event`` path. Empty ``components`` clears/closes the modal.
    """
    type: str = "chrome_surface"
    region: str = "modal"          # "modal" (parity with ChromeRender.region)
    surface_key: str = ""
    title: str = ""
    admin_only: bool = False
    components: List[Dict[str, Any]] = field(default_factory=list)
    mode: str = "replace"          # reserved; only "replace" today


# --- Feature 060: canonical reliability protocol ----------------------
@dataclass
class ConversationCommitReady(Message):
    """Prelude that opens a commit fence for a server-originated update.

    Client-originated turns already open their request generation before the
    request is sent. Detached work (for example, a long-running tool result)
    has no such live client request, so the server advertises one fresh UUID4
    immediately before the corresponding complete commit snapshot.
    """

    type: str = "conversation_commit_ready"
    schema_version: int = 1
    chat_id: str = ""
    connection_generation: str = ""
    request_generation: str = ""
    render_revision: int = 0

    def validate(self) -> None:
        if self.type != "conversation_commit_ready":
            raise ProtocolValidationError("type must be conversation_commit_ready")
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ProtocolValidationError("schema_version must be exactly 1")
        _require_uuid4(self.chat_id, "chat_id")
        _require_uuid4(self.connection_generation, "connection_generation")
        _require_uuid4(self.request_generation, "request_generation")
        _require_uint64(self.render_revision, "render_revision")
        if self.render_revision == 0:
            raise ProtocolValidationError("render_revision must be positive")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConversationCommitReady":
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "conversation_commit_ready must contain exactly its canonical fields"
            )
        frame = cls(**dict(data))
        frame.validate()
        return frame


# Feature 066 T023 (deliberate cross-client contract extension): the closed
# set of variants a canonical text part may carry. A lifted caption keeps its
# weight through commit/hydration; every other authoring variant still
# normalizes away. Mirrored by the web (client.js validateSnapshotShape),
# Windows (protocol.py), Android (Wire.kt) and Apple (ConversationContinuity)
# decoders and documented in specs/060-…/contracts/conversation-continuity.md.
CANONICAL_TEXT_PART_VARIANTS = frozenset({"caption"})


@dataclass
class ConversationSnapshot(Message):
    """One complete authoritative committed conversation projection.

    Transcript and canvas travel in the same frame so clients can validate
    them off-thread and replace both in one reducer action.
    """

    type: str = "conversation_snapshot"
    schema_version: int = 1
    snapshot_id: str = ""
    chat_id: str = ""
    connection_generation: str = ""
    request_generation: str = ""
    snapshot_purpose: str = ""
    render_revision: int = 0
    committed_at: str = ""
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    canvas: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate the complete snapshot without mutating client state."""

        if self.type != "conversation_snapshot":
            raise ProtocolValidationError("type must be conversation_snapshot")
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ProtocolValidationError("schema_version must be exactly 1")
        _require_uuid4(self.snapshot_id, "snapshot_id")
        _require_uuid4(self.chat_id, "chat_id")
        _require_uuid4(self.connection_generation, "connection_generation")
        _require_uuid4(self.request_generation, "request_generation")
        if self.snapshot_purpose not in {"hydration", "commit"}:
            raise ProtocolValidationError(
                "snapshot_purpose must be hydration or commit"
            )
        _require_uint64(self.render_revision, "render_revision")
        _require_rfc3339_utc(self.committed_at, "committed_at")
        self._validate_transcript()
        if not isinstance(self.canvas, dict) or set(self.canvas) != {
            "target",
            "components",
        }:
            raise ProtocolValidationError(
                "canvas must contain exactly target and components"
            )
        if self.canvas.get("target") != "canvas" or not isinstance(
            self.canvas.get("components"), list
        ):
            raise ProtocolValidationError(
                "canvas must target canvas and contain a components array"
            )
        try:
            json.dumps(asdict(self), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError("snapshot must be valid JSON") from exc

    def _validate_transcript(self) -> None:
        if not isinstance(self.transcript, list):
            raise ProtocolValidationError("transcript must be an array")
        for index, message in enumerate(self.transcript):
            prefix = f"transcript[{index}]"
            if not isinstance(message, dict) or set(message) != {
                "message_id",
                "role",
                "created_at",
                "parts",
                "attachments",
            }:
                raise ProtocolValidationError(
                    f"{prefix} must contain every canonical message field"
                )
            if not isinstance(message["message_id"], str) or not message[
                "message_id"
            ]:
                raise ProtocolValidationError(f"{prefix}.message_id is required")
            if message["role"] not in {"user", "assistant", "system", "tool"}:
                raise ProtocolValidationError(f"{prefix}.role is invalid")
            _require_rfc3339_utc(message["created_at"], f"{prefix}.created_at")
            if not isinstance(message["attachments"], list):
                raise ProtocolValidationError(f"{prefix}.attachments must be an array")
            parts = message["parts"]
            if not isinstance(parts, list) or not parts:
                raise ProtocolValidationError(f"{prefix}.parts must be non-empty")
            for part_index, part in enumerate(parts):
                self._validate_part(part, f"{prefix}.parts[{part_index}]")

    @staticmethod
    def _validate_part(part: object, prefix: str) -> None:
        if not isinstance(part, dict):
            raise ProtocolValidationError(f"{prefix} must be an object")
        part_type = part.get("type")
        if part_type == "text":
            # 066 T023: exactly {type, text} plus an OPTIONAL bounded variant.
            valid = (
                set(part) in ({"type", "text"}, {"type", "text", "variant"})
                and isinstance(part.get("text"), str)
                and (
                    "variant" not in part
                    or part.get("variant") in CANONICAL_TEXT_PART_VARIANTS
                )
            )
        elif part_type == "components":
            valid = set(part) == {"type", "components"} and isinstance(
                part.get("components"), list
            )
        elif part_type == "structured":
            # The rendition must be VISIBLE, not merely a string: every Apple
            # client guards `continuityNonBlank(plain_text)` (which trims
            # first) and decodes a snapshot all-or-nothing, so a blank part
            # discards the whole conversation and that rail never hydrates.
            # The canonical contract therefore holds the strictest client's
            # rule — a part no client can render is not canonical.
            valid = (
                set(part) == {"type", "value", "plain_text"}
                and isinstance(part.get("plain_text"), str)
                and bool(part["plain_text"].strip())
            )
        elif part_type == "recovery":
            valid = set(part) == {"type", "code", "message"} and all(
                isinstance(part.get(key), str) and bool(part.get(key))
                for key in ("code", "message")
            )
        else:
            valid = False
        if not valid:
            raise ProtocolValidationError(f"{prefix} has an invalid canonical shape")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConversationSnapshot":
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "conversation_snapshot must contain exactly its canonical fields"
            )
        frame = cls(**dict(data))
        frame.validate()
        return frame


@dataclass
class OperationStatus(Message):
    """Server-owned durable operation progress and terminal projection."""

    type: str = "operation_status"
    operation_id: str = ""
    action: str = ""
    surface: str = ""
    chat_id: Optional[str] = None
    connection_generation: str = ""
    request_generation: str = ""
    sequence: int = 0
    state: str = ""
    phase: str = ""
    label: str = ""
    terminal: bool = False
    retryable: bool = False
    error: Optional[Dict[str, Any]] = None
    retry_after_ms: Optional[int] = None
    updated_at: str = ""

    _STATE_FLAGS: ClassVar[Dict[str, tuple[bool, bool]]] = {
        "accepted": (False, False),
        "validating": (False, False),
        "persisting": (False, False),
        "running": (False, False),
        "completed": (True, False),
        "failed": (True, False),
        "cancelled": (True, False),
        "retryable": (True, True),
    }
    _ERROR_CODES: ClassVar[set[str]] = {
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

    def validate(self) -> None:
        if self.type != "operation_status":
            raise ProtocolValidationError("type must be operation_status")
        _require_uuid4(self.operation_id, "operation_id")
        _require_snake_case(self.action, "action")
        _require_snake_case(self.surface, "surface")
        if self.chat_id is not None:
            _require_uuid4(self.chat_id, "chat_id")
        _require_uuid4(self.connection_generation, "connection_generation")
        _require_uuid4(self.request_generation, "request_generation")
        _require_uint64(self.sequence, "sequence")
        _require_snake_case(self.phase, "phase")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ProtocolValidationError("label must be non-empty")
        flags = self._STATE_FLAGS.get(self.state)
        if flags is None or (self.terminal, self.retryable) != flags:
            raise ProtocolValidationError("state, terminal, and retryable disagree")
        error_required = self.state in {"failed", "cancelled", "retryable"}
        if error_required:
            if not isinstance(self.error, dict) or set(self.error) != {
                "code",
                "message",
            }:
                raise ProtocolValidationError("terminal error is required")
            if self.error.get("code") not in self._ERROR_CODES:
                raise ProtocolValidationError("error.code is not canonical")
            if not isinstance(self.error.get("message"), str) or not self.error[
                "message"
            ].strip():
                raise ProtocolValidationError("error.message must be non-empty")
        elif self.error is not None:
            raise ProtocolValidationError("error must be null for this state")
        if self.retry_after_ms is not None:
            _require_uint64(self.retry_after_ms, "retry_after_ms")
            if self.state != "retryable":
                raise ProtocolValidationError(
                    "retry_after_ms is valid only for retryable state"
                )
        _require_rfc3339_utc(self.updated_at, "updated_at")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationStatus":
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "operation_status must contain exactly its canonical fields"
            )
        frame = cls(**dict(data))
        frame.validate()
        return frame


AGENT_LIFECYCLE_REASON_CODES = frozenset(
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


@dataclass
class AgentLifecycle(Message):
    """Canonical user-facing projection of one authoritative agent runtime."""

    type: str = "agent_lifecycle"
    agent_id: str = ""
    revision_id: Optional[str] = None
    runtime_instance_id: Optional[str] = None
    lifecycle_generation: int = 0
    state_revision: int = 0
    state: str = ""
    reason_code: Optional[str] = None
    label: str = ""
    updated_at: str = ""

    _REASON_CODES: ClassVar[frozenset[str]] = AGENT_LIFECYCLE_REASON_CODES

    def validate(self) -> None:
        if self.type != "agent_lifecycle":
            raise ProtocolValidationError("type must be agent_lifecycle")
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ProtocolValidationError("agent_id must be non-empty")
        if self.revision_id is not None:
            _require_uuid4(self.revision_id, "revision_id")
        if self.runtime_instance_id is not None:
            _require_uuid4(self.runtime_instance_id, "runtime_instance_id")
        _require_uint64(self.lifecycle_generation, "lifecycle_generation")
        _require_uint64(self.state_revision, "state_revision")
        if self.state not in {"starting", "online", "updating", "failed", "offline"}:
            raise ProtocolValidationError("state is not a canonical lifecycle state")
        if self.state in {"starting", "online", "updating"} and (
            self.revision_id is None or self.runtime_instance_id is None
        ):
            raise ProtocolValidationError(
                "active lifecycle states require revision and runtime instance"
            )
        if self.reason_code is not None:
            _require_snake_case(self.reason_code, "reason_code")
            if self.reason_code not in self._REASON_CODES:
                raise ProtocolValidationError("reason_code is not canonical")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ProtocolValidationError("label must be non-empty")
        _require_rfc3339_utc(self.updated_at, "updated_at")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentLifecycle":
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "agent_lifecycle must contain exactly its canonical fields"
            )
        frame = cls(**dict(data))
        frame.validate()
        return frame


class FrameDisposition(str, Enum):
    """Deterministic client-reducer decision for a scoped server frame."""

    APPLY = "apply"
    REPLAY = "replay"
    REVISION_CONFLICT = "revision_conflict"
    UNEXPECTED_EQUAL_COMMIT = "unexpected_equal_commit"
    STALE = "stale"
    WRONG_SCOPE = "wrong_scope"
    WRONG_PURPOSE = "wrong_purpose"
    APPLY_OVERLAY = "apply_overlay"
    OUT_OF_ORDER = "out_of_order"
    WRONG_BASE_REVISION = "wrong_base_revision"


@dataclass(frozen=True)
class TransientFrameScope:
    """Generation and sequence fence carried by disposable preview frames."""

    chat_id: str
    connection_generation: str
    request_generation: str
    base_render_revision: int
    frame_sequence: int

    def validate(self) -> None:
        _require_uuid4(self.chat_id, "chat_id")
        _require_uuid4(self.connection_generation, "connection_generation")
        _require_uuid4(self.request_generation, "request_generation")
        _require_uint64(self.base_render_revision, "base_render_revision")
        _require_uint64(self.frame_sequence, "frame_sequence")


@dataclass
class ConversationFrameFence:
    """Purpose-aware reducer fence for committed and transient chat frames."""

    chat_id: str
    connection_generation: str
    request_generation: str
    request_purpose: str
    last_committed_render_revision: int
    _hydration_applied: bool = field(default=False, init=False, repr=False)
    _accepted_snapshot_id: Optional[str] = field(default=None, init=False, repr=False)
    _accepted_snapshot_json: Optional[str] = field(default=None, init=False, repr=False)
    _last_frame_sequence: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        _require_uuid4(self.chat_id, "chat_id")
        _require_uuid4(self.connection_generation, "connection_generation")
        _require_uuid4(self.request_generation, "request_generation")
        if self.request_purpose not in {"hydration", "commit"}:
            raise ProtocolValidationError("request_purpose must be hydration or commit")
        _require_uint64(
            self.last_committed_render_revision,
            "last_committed_render_revision",
        )

    def accept_snapshot(self, frame: ConversationSnapshot) -> FrameDisposition:
        """Return the canonical reducer disposition and advance only on apply."""

        frame.validate()
        if (
            frame.chat_id != self.chat_id
            or frame.connection_generation != self.connection_generation
            or frame.request_generation != self.request_generation
        ):
            return FrameDisposition.WRONG_SCOPE
        if frame.snapshot_purpose != self.request_purpose:
            return FrameDisposition.WRONG_PURPOSE
        if frame.render_revision < self.last_committed_render_revision:
            return FrameDisposition.STALE

        canonical = json.dumps(
            asdict(frame), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if frame.render_revision == self.last_committed_render_revision:
            if self.request_purpose != "hydration":
                return FrameDisposition.UNEXPECTED_EQUAL_COMMIT
            if not self._hydration_applied:
                self._remember_snapshot(frame, canonical)
                self._hydration_applied = True
                return FrameDisposition.APPLY
            if (
                frame.snapshot_id == self._accepted_snapshot_id
                and canonical == self._accepted_snapshot_json
            ):
                return FrameDisposition.REPLAY
            return FrameDisposition.REVISION_CONFLICT

        self.last_committed_render_revision = frame.render_revision
        self._remember_snapshot(frame, canonical)
        if self.request_purpose == "hydration":
            self._hydration_applied = True
        return FrameDisposition.APPLY

    def accept_transient(self, frame: TransientFrameScope) -> FrameDisposition:
        """Accept only current-base, strictly increasing preview overlays."""

        frame.validate()
        if (
            frame.chat_id != self.chat_id
            or frame.connection_generation != self.connection_generation
            or frame.request_generation != self.request_generation
        ):
            return FrameDisposition.WRONG_SCOPE
        if frame.base_render_revision != self.last_committed_render_revision:
            return FrameDisposition.WRONG_BASE_REVISION
        if frame.frame_sequence <= self._last_frame_sequence:
            return FrameDisposition.OUT_OF_ORDER
        self._last_frame_sequence = frame.frame_sequence
        return FrameDisposition.APPLY_OVERLAY

    def _remember_snapshot(
        self, frame: ConversationSnapshot, canonical: str
    ) -> None:
        self._accepted_snapshot_id = frame.snapshot_id
        self._accepted_snapshot_json = canonical


@dataclass(frozen=True)
class RuntimeFence:
    """Complete durable host/runtime generation fence for personal agents."""

    agent_id: str
    host_id: str
    host_session_id: str
    delivery_id: str
    revision_id: str
    runtime_instance_id: str
    process_id: Optional[str]
    lifecycle_generation: int

    def validate(self, *, allow_prelaunch: bool) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ProtocolValidationError("agent_id must be non-empty")
        for name in (
            "host_id",
            "host_session_id",
            "delivery_id",
            "revision_id",
            "runtime_instance_id",
        ):
            _require_uuid4(getattr(self, name), name)
        if self.process_id is None:
            if not allow_prelaunch:
                raise ProtocolValidationError("process_id is required after launch")
        else:
            _require_uuid4(self.process_id, "process_id")
        _require_uint64(self.lifecycle_generation, "lifecycle_generation")

    def bind_process(self, process_id: str) -> "RuntimeFence":
        """Return the one legal post-launch fence; an existing bind is final."""

        if self.process_id is not None:
            raise ProtocolValidationError("process_id is already bound")
        _require_uuid4(process_id, "process_id")
        return replace(self, process_id=process_id)

    def to_dict(self) -> Dict[str, Any]:
        self.validate(allow_prelaunch=self.process_id is None)
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeFence":
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "runtime fence must contain exactly its canonical fields"
            )
        fence = cls(**dict(data))
        fence.validate(allow_prelaunch=fence.process_id is None)
        return fence

# --- Agent2Agent Protocol ---
@dataclass
class AgentSkill:
    name: str
    description: str
    id: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    tags: List[str] = field(default_factory=list)
    # Required scope — one of orchestrator.tool_permissions.VALID_SCOPES, which
    # is authoritative: tools:read, tools:write, tools:search, tools:system,
    # tools:files (027), tools:execute (039). A scope outside that set has no
    # grantable permission surface and its tool is denied at dispatch.
    scope: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)  # Optional metadata (e.g. streamable config)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class AgentCard:
    name: str
    description: str
    agent_id: str
    version: str = "0.1.0"
    skills: List[AgentSkill] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'AgentCard':
        if not isinstance(data, Mapping):
            raise ProtocolValidationError("agent card must be an object")
        skills_data = data.get('skills', [])
        if not isinstance(skills_data, list):
            raise ProtocolValidationError("agent card skills must be a list")
        skills = [
            AgentSkill(**_known_dataclass_fields(AgentSkill, skill))
            if isinstance(skill, Mapping)
            else skill
            for skill in skills_data
        ]
        card_data = _known_dataclass_fields(
            AgentCard,
            {key: value for key, value in data.items() if key != 'skills'},
        )
        return AgentCard(skills=skills, **card_data)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "name": self.name,
            "description": self.description,
            "agent_id": self.agent_id,
            "version": self.version,
            "skills": [skill.to_dict() for skill in self.skills] if self.skills else []
        }
        if self.metadata:
            result["metadata"] = self.metadata
        return result

@dataclass
class RegisterAgent(Message):
    type: str = "register_agent"
    agent_card: Optional[AgentCard] = None
    # 028 FR-016 (additive): shared-secret presented at registration; the
    # orchestrator refuses keyless registrations outside dev mode when
    # AGENT_API_KEY is configured/required (fail closed).
    api_key: Optional[str] = None

    def to_json(self) -> str:
        data = asdict(self)
        if self.agent_card:
            data['agent_card'] = asdict(self.agent_card)
        return json.dumps(data)

    @staticmethod
    def from_json(json_str: str) -> 'RegisterAgent':
        data = json.loads(json_str)
        if 'agent_card' in data and data['agent_card']:
            data['agent_card'] = AgentCard.from_dict(data['agent_card'])
        return RegisterAgent(**data)

# --- Agent Creation Protocol ---
@dataclass
class AgentCreationProgress(Message):
    """Progress update during agent creation/refinement/approval."""
    type: str = "agent_creation_progress"
    draft_id: str = ""
    step: str = ""          # e.g., "generating_template", "generating_tools", "security_scan", "writing_files"
    message: str = ""       # human-readable progress message
    status: str = ""        # pending | generating | generated | testing | analyzing | approved | rejected | live | error
    detail: Optional[Dict[str, Any]] = None  # optional extra data (e.g., security report)

@dataclass
class ToolProgress(Message):
    """Real-time progress update during tool execution.

    Agents can send these messages during long-running tool calls so the
    orchestrator can forward them to the UI client immediately.
    """
    type: str = "tool_progress"
    tool_name: str = ""
    agent_id: str = ""
    message: str = ""
    percentage: Optional[int] = None  # 0-100, or None if indeterminate
    metadata: Dict[str, Any] = field(default_factory=dict)

# --- Tool Streaming (001-tool-stream-ui) ---
@dataclass
class ToolStreamData(Message):
    """One streaming chunk from an agent tool back to the orchestrator.

    Sent by an agent in response to an ``MCPRequest`` whose ``params._stream``
    is ``True``. The orchestrator forwards (after ROTE adaptation and per-
    subscriber authorization) to every websocket subscribed to the
    corresponding ``StreamSubscription`` as a ``ui_stream_data`` message.

    See specs/001-tool-stream-ui/contracts/protocol-messages.md §B2.
    """
    type: str = "tool_stream_data"
    request_id: str = ""
    stream_id: str = ""
    agent_id: str = ""
    tool_name: str = ""
    seq: int = 0
    components: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[Any] = None
    terminal: bool = False
    error: Optional[Dict[str, Any]] = None  # see §A5: code, message, phase, attempt, next_retry_at_ms, retryable


@dataclass
class ToolStreamEnd(Message):
    """Sent by an agent when a streaming tool's async generator returns
    naturally (no more data, no error). Orchestrator forwards as a final
    ``ui_stream_data`` chunk with ``terminal: true`` and removes the
    subscription.

    See specs/001-tool-stream-ui/contracts/protocol-messages.md §B4.
    """
    type: str = "tool_stream_end"
    request_id: str = ""
    stream_id: str = ""


@dataclass
class ToolStreamCancel(Message):
    """Sent by the orchestrator to an agent to ask it to stop a streaming
    tool. Triggered by user-leaves-chat, explicit unsubscribe, token
    revocation, or dormant TTL expiry. The agent MUST close the underlying
    async generator (which propagates ``GeneratorExit`` to any ``finally``
    cleanup) within 1 second.

    See specs/001-tool-stream-ui/contracts/protocol-messages.md §B3.
    """
    type: str = "tool_stream_cancel"
    request_id: str = ""
    stream_id: str = ""


@dataclass
class AuditAppend(Message):
    """Server→client live audit-log append (feature 003-agent-audit-log).

    Sent on the user's existing WebSocket immediately after a new audit
    row is inserted. The ``event`` payload matches the ``AuditEventDTO``
    JSON Schema (see backend/audit/schemas.py and
    specs/003-agent-audit-log/contracts/audit-event-schema.json) — it
    is a strict subset of the underlying database row that omits
    internal AU-9 fields. Server-side filtering by user_id is mandatory:
    a connection authenticated as user A MUST NEVER receive an
    ``audit_append`` whose event belongs to user B (FR-007 / FR-019).
    """
    type: str = "audit_append"
    event: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentHostRegistration:
    """Structured runtime-contract advertisement from a desktop agent host."""

    host_id: str
    supported_runtime_contract_versions: tuple[int, ...]
    runtime_lock_sha256: str
    platform: str
    client_version: str

    def validate(self) -> None:
        _require_uuid4(self.host_id, "host_id")
        versions = self.supported_runtime_contract_versions
        if (
            not isinstance(versions, tuple)
            or not versions
            or any(
                isinstance(version, bool)
                or not isinstance(version, int)
                or version <= 0
                for version in versions
            )
            or len(set(versions)) != len(versions)
            or tuple(sorted(versions)) != versions
        ):
            raise ProtocolValidationError(
                "supported_runtime_contract_versions must be sorted, unique, "
                "positive integers"
            )
        if _LOWER_SHA256.fullmatch(self.runtime_lock_sha256) is None:
            raise ProtocolValidationError(
                "runtime_lock_sha256 must be 64 lowercase hexadecimal characters"
            )
        if self.platform not in {"windows", "macos"}:
            raise ProtocolValidationError("platform must be windows or macos")
        if _STRICT_SEMVER.fullmatch(self.client_version) is None:
            raise ProtocolValidationError("client_version must be strict SemVer")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        data = asdict(self)
        data["supported_runtime_contract_versions"] = list(
            self.supported_runtime_contract_versions
        )
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentHostRegistration":
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "agent_host must contain exactly its structured v2 fields"
            )
        values = dict(data)
        versions = values.get("supported_runtime_contract_versions")
        if not isinstance(versions, (list, tuple)):
            raise ProtocolValidationError(
                "supported_runtime_contract_versions must be an array"
            )
        values["supported_runtime_contract_versions"] = tuple(versions)
        registration = cls(**values)
        registration.validate()
        return registration


@dataclass
class AgentHostRegistered(Message):
    """Server-issued acknowledgement that makes a host session eligible."""

    type: str = "agent_host_registered"
    host_id: str = ""
    host_session_id: str = ""
    inventory_required: bool = True
    accepted_at: str = ""

    def validate(self) -> None:
        if self.type != "agent_host_registered":
            raise ProtocolValidationError("type must be agent_host_registered")
        _require_uuid4(self.host_id, "host_id")
        _require_uuid4(self.host_session_id, "host_session_id")
        if not isinstance(self.inventory_required, bool):
            raise ProtocolValidationError("inventory_required must be boolean")
        _require_rfc3339_utc(self.accepted_at, "accepted_at")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentHostRegistered":
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "agent_host_registered must contain exactly its canonical fields"
            )
        acknowledgement = cls(**dict(data))
        acknowledgement.validate()
        return acknowledgement


@dataclass(frozen=True)
class PersonalAgentHostCapability:
    """Immutable candidate-owned host applicability for one platform."""

    supported: bool = False
    runtime_contract_versions: tuple[int, ...] = ()
    source_feature: Optional[str] = None

    def validate(self) -> None:
        if not isinstance(self.supported, bool):
            raise ProtocolValidationError("supported must be boolean")
        versions = self.runtime_contract_versions
        if (
            not isinstance(versions, tuple)
            or any(
                isinstance(version, bool)
                or not isinstance(version, int)
                or version <= 0
                for version in versions
            )
            or tuple(sorted(set(versions))) != versions
        ):
            raise ProtocolValidationError(
                "runtime_contract_versions must be sorted unique positive integers"
            )
        if self.supported:
            if 2 not in versions or self.source_feature != "059":
                raise ProtocolValidationError(
                    "supported macOS hosting requires runtime v2 from feature 059"
                )
        elif versions or self.source_feature is not None:
            raise ProtocolValidationError(
                "unsupported macOS hosting requires empty versions and null source"
            )

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "supported": self.supported,
            "runtime_contract_versions": list(self.runtime_contract_versions),
            "source_feature": self.source_feature,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PersonalAgentHostCapability":
        if set(data) != {
            "supported",
            "runtime_contract_versions",
            "source_feature",
        }:
            raise ProtocolValidationError("personal-agent host capability is malformed")
        versions = data.get("runtime_contract_versions")
        if not isinstance(versions, (list, tuple)):
            raise ProtocolValidationError("runtime_contract_versions must be an array")
        capability = cls(
            supported=data.get("supported"),
            runtime_contract_versions=tuple(versions),
            source_feature=data.get("source_feature"),
        )
        capability.validate()
        return capability


@dataclass(frozen=True)
class PersonalAgentHostCapabilities:
    """Immutable personal-agent host capability partition."""

    macos: PersonalAgentHostCapability = field(
        default_factory=PersonalAgentHostCapability
    )

    def __post_init__(self) -> None:
        if not isinstance(self.macos, PersonalAgentHostCapability):
            raise ProtocolValidationError("macos host capability is malformed")
        self.macos.validate()

    def to_dict(self) -> Dict[str, Any]:
        return {"macos": self.macos.to_dict()}


@dataclass(frozen=True)
class CandidateCapabilities:
    """Immutable top-level candidate capability partition."""

    personal_agent_host: PersonalAgentHostCapabilities = field(
        default_factory=PersonalAgentHostCapabilities
    )

    def __post_init__(self) -> None:
        if not isinstance(self.personal_agent_host, PersonalAgentHostCapabilities):
            raise ProtocolValidationError("personal-agent host capabilities are malformed")

    def to_dict(self) -> Dict[str, Any]:
        return {"personal_agent_host": self.personal_agent_host.to_dict()}


@dataclass(frozen=True)
class CandidateCapabilityMap:
    """Candidate-owned applicability map shared by dashboard and UI config."""

    capabilities: CandidateCapabilities = field(default_factory=CandidateCapabilities)

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, CandidateCapabilities):
            raise ProtocolValidationError("candidate capabilities are malformed")

    def to_dict(self) -> Dict[str, Any]:
        return {"capabilities": self.capabilities.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateCapabilityMap":
        if set(data) != {"capabilities"} or not isinstance(
            data.get("capabilities"), Mapping
        ):
            raise ProtocolValidationError("candidate capability map is malformed")
        capabilities = data["capabilities"]
        if set(capabilities) != {"personal_agent_host"} or not isinstance(
            capabilities.get("personal_agent_host"), Mapping
        ):
            raise ProtocolValidationError("candidate capabilities are malformed")
        host = capabilities["personal_agent_host"]
        if set(host) != {"macos"} or not isinstance(host.get("macos"), Mapping):
            raise ProtocolValidationError("personal-agent host capabilities are malformed")
        macos = PersonalAgentHostCapability.from_dict(host["macos"])
        return cls(
            capabilities=CandidateCapabilities(
                personal_agent_host=PersonalAgentHostCapabilities(macos=macos)
            )
        )


#: Feature 076 — the closed verb vocabulary a computer host may announce. The
#: server never sends a verb outside this set; the host never executes one
#: outside the subset it announced (FR-014).
COMPUTER_HOST_VERBS: frozenset = frozenset({
    "screenshot", "list_windows", "get_clipboard", "read_file", "list_dir", "wait",
    "click", "double_click", "right_click", "move", "drag", "scroll", "type_text",
    "press_keys", "focus_window", "open_app", "set_clipboard",
    "write_file", "delete_path",
})
COMPUTER_HOST_PLATFORMS: frozenset = frozenset({"windows", "macos", "linux"})
COMPUTER_HOST_PROTOCOL = 1
_COMPUTER_HOST_NAME_MAX = 64
_COMPUTER_HOST_MAX_SCREENS = 8


@dataclass(frozen=True)
class ComputerHostDescriptor:
    """Feature 076: what a desktop client announces when its owner has switched
    on "Allow remote control". Additive on ``register_ui`` (``computer_host``);
    absent means the socket is not a controllable host. Validation is strict and
    fail-closed — a malformed descriptor is dropped (the UI still registers) and
    the socket is simply not a host."""

    host_id: str
    name: str
    platform: str
    client_version: str
    screens: tuple  # of dicts {index, width, height, scale, primary}
    verbs: tuple    # of str, subset of COMPUTER_HOST_VERBS
    protocol: int = COMPUTER_HOST_PROTOCOL

    def validate(self) -> None:
        _require_uuid4(self.host_id, "computer_host.host_id")
        if (not isinstance(self.name, str) or not self.name.strip()
                or len(self.name) > _COMPUTER_HOST_NAME_MAX
                or any(ord(ch) < 32 for ch in self.name)):
            raise ProtocolValidationError(
                f"computer_host.name must be 1-{_COMPUTER_HOST_NAME_MAX} printable characters")
        if self.platform not in COMPUTER_HOST_PLATFORMS:
            raise ProtocolValidationError("computer_host.platform must be windows, macos or linux")
        if not isinstance(self.client_version, str) or _STRICT_SEMVER.fullmatch(self.client_version) is None:
            raise ProtocolValidationError("computer_host.client_version must be strict SemVer")
        if isinstance(self.protocol, bool) or not isinstance(self.protocol, int) or self.protocol != COMPUTER_HOST_PROTOCOL:
            raise ProtocolValidationError("computer_host.protocol must be 1")
        screens = self.screens
        if (not isinstance(screens, tuple) or not screens
                or len(screens) > _COMPUTER_HOST_MAX_SCREENS):
            raise ProtocolValidationError(
                f"computer_host.screens must list 1-{_COMPUTER_HOST_MAX_SCREENS} screens")
        primaries = 0
        for index, screen in enumerate(screens):
            if not isinstance(screen, Mapping):
                raise ProtocolValidationError("computer_host.screens entries must be objects")
            if set(screen) != {"index", "width", "height", "scale", "primary"}:
                raise ProtocolValidationError(
                    "computer_host.screens entries must contain exactly index, width, height, scale, primary")
            if screen["index"] != index:
                raise ProtocolValidationError("computer_host.screens must be indexed 0..n-1 in order")
            for dim in ("width", "height"):
                value = screen[dim]
                if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 16384):
                    raise ProtocolValidationError(f"computer_host.screens[{index}].{dim} out of range")
            scale = screen["scale"]
            if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not (0.5 <= float(scale) <= 8.0):
                raise ProtocolValidationError(f"computer_host.screens[{index}].scale out of range")
            if not isinstance(screen["primary"], bool):
                raise ProtocolValidationError(f"computer_host.screens[{index}].primary must be boolean")
            primaries += int(screen["primary"])
        if primaries != 1:
            raise ProtocolValidationError("computer_host.screens must mark exactly one primary screen")
        verbs = self.verbs
        if (not isinstance(verbs, tuple) or not verbs
                or any(not isinstance(v, str) for v in verbs)
                or len(set(verbs)) != len(verbs)):
            raise ProtocolValidationError("computer_host.verbs must be a non-empty list of unique strings")
        unknown = sorted(set(verbs) - COMPUTER_HOST_VERBS)
        if unknown:
            raise ProtocolValidationError(f"computer_host.verbs contains unknown verbs: {unknown}")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "host_id": self.host_id,
            "name": self.name,
            "platform": self.platform,
            "client_version": self.client_version,
            "screens": [dict(s) for s in self.screens],
            "verbs": list(self.verbs),
            "protocol": self.protocol,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ComputerHostDescriptor":
        if not isinstance(data, Mapping):
            raise ProtocolValidationError("computer_host must be an object")
        expected = {item.name for item in fields(cls)}
        if set(data) != expected:
            raise ProtocolValidationError(
                "computer_host must contain exactly host_id, name, platform, client_version, "
                "screens, verbs, protocol")
        screens = data.get("screens")
        verbs = data.get("verbs")
        if not isinstance(screens, (list, tuple)) or not isinstance(verbs, (list, tuple)):
            raise ProtocolValidationError("computer_host.screens and computer_host.verbs must be arrays")
        descriptor = cls(
            host_id=data["host_id"],
            name=data["name"],
            platform=data["platform"],
            client_version=data["client_version"],
            screens=tuple(dict(s) if isinstance(s, Mapping) else s for s in screens),
            verbs=tuple(verbs),
            protocol=data["protocol"],
        )
        descriptor.validate()
        return descriptor


@dataclass
class RegisterUI(Message):
    type: str = "register_ui"
    capabilities: List[str] = field(default_factory=list)
    session_id: Optional[str] = None
    token: Optional[str] = None
    # Feature 065: stable, non-secret installation identity. It identifies a
    # client but grants no authority without the authenticated connection and
    # short-lived control binding.
    device_id: Optional[str] = None
    device: Optional[Dict[str, Any]] = None  # ROTE: frontend device capabilities
    # Feature 006: optional initial LLM config carried from the user's
    # browser localStorage at register time. Shape: {api_key, base_url, model}.
    # Stored only in per-WebSocket memory on the server (never persisted).
    llm_config: Optional[Dict[str, Any]] = None
    # Feature 016-persistent-login (FR-015): True when the client reached
    # the authenticated state via a silent resume from a stored credential
    # (i.e., the OIDC `onSigninCallback` did NOT fire on this page load).
    # False (default) for fresh interactive logins and for older clients
    # that pre-date this feature. Drives the audit action_type selection.
    resumed: bool = False
    # Feature 060: one fresh UUID4 per fenced connection plus an optional
    # account-scoped resume payload. Legacy registrations may omit both, but a
    # resume locator is never accepted without its connection fence.
    connection_generation: Optional[str] = None
    resume: Optional[Dict[str, Any]] = None
    # Feature 058 (BYO agents): this socket belongs to a DESKTOP HOST able to
    # write a delivered agent bundle to disk and supervise it as a child
    # process. Additive + default False, so a browser tab (which must never be
    # handed a code bundle) is a non-host by omission, exactly like every
    # pre-058 client. `host_session_id` identifies the host instance across
    # reconnects; it is echoed on the agent_tunnel frames the host relays.
    agent_host: AgentHostRegistration | bool | None = False
    host_session_id: Optional[str] = None
    # Feature 076 (remote computer control): this socket belongs to a desktop
    # whose owner switched on "Allow remote control". Additive + default None,
    # so every pre-076 client (and every browser tab) is a non-host by
    # omission. A malformed descriptor never blocks registration: from_json
    # drops it and the socket simply is not a host (fail-closed).
    computer_host: Optional[ComputerHostDescriptor] = None

    def to_json(self) -> str:
        self.validate()
        data = asdict(self)
        if self.resume is not None:
            data["resume"] = dict(self.resume)
        if self.device_id is None:
            # Preserve legacy RegisterUI wire bytes: this field is additive
            # and absent, rather than null, until a client adopts feature 065.
            data.pop("device_id", None)
        if isinstance(self.agent_host, AgentHostRegistration):
            data["agent_host"] = self.agent_host.to_dict()
            data.pop("host_session_id", None)
        if self.computer_host is None:
            data.pop("computer_host", None)
        else:
            data["computer_host"] = self.computer_host.to_dict()
        return json.dumps(data)

    def validate(self) -> None:
        if self.type != "register_ui":
            raise ProtocolValidationError("type must be register_ui")
        if self.device_id is not None:
            _require_uuid4(self.device_id, "device_id")
            if self.connection_generation is None:
                raise ProtocolValidationError(
                    "connection_generation is required when device_id is present"
                )
        if self.connection_generation is not None:
            _require_uuid4(self.connection_generation, "connection_generation")
        elif self.resume is not None:
            raise ProtocolValidationError(
                "connection_generation is required when resume is present"
            )
        if self.resume is not None:
            if not isinstance(self.resume, Mapping):
                raise ProtocolValidationError("resume must be a mapping")
            expected_resume_fields = {
                "schema_version",
                "active_chat_id",
                "request_generation",
            }
            if set(self.resume) != expected_resume_fields:
                raise ProtocolValidationError(
                    "resume must contain exactly schema_version, active_chat_id, "
                    "and request_generation"
                )
            schema_version = self.resume["schema_version"]
            if (
                isinstance(schema_version, bool)
                or not isinstance(schema_version, int)
                or schema_version != 1
            ):
                raise ProtocolValidationError("resume.schema_version must be exactly 1")
            _require_uuid4(self.resume["active_chat_id"], "resume.active_chat_id")
            _require_uuid4(
                self.resume["request_generation"], "resume.request_generation"
            )
        if isinstance(self.agent_host, AgentHostRegistration):
            self.agent_host.validate()
            if self.host_session_id is not None:
                raise ProtocolValidationError(
                    "structured agent hosts cannot propose host_session_id"
                )
        elif self.agent_host not in (False, True, None):
            raise ProtocolValidationError("agent_host must be structured or boolean")
        if self.computer_host is not None:
            if not isinstance(self.computer_host, ComputerHostDescriptor):
                raise ProtocolValidationError("computer_host must be a structured descriptor")
            self.computer_host.validate()

    @staticmethod
    def from_json(json_str: str) -> 'RegisterUI':
        data = json.loads(json_str)
        # Filter unknown keys so older servers parsing newer payloads (and
        # vice versa) don't crash on additive fields.
        valid_fields = {f.name for f in RegisterUI.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in valid_fields}
        if isinstance(data.get("agent_host"), dict):
            data["agent_host"] = AgentHostRegistration.from_dict(data["agent_host"])
        raw_computer_host = data.pop("computer_host", None)
        if raw_computer_host is not None:
            # Feature 076: a malformed descriptor must not cost the client its
            # session — it only costs it host eligibility (FR-002, fail-closed).
            try:
                data["computer_host"] = ComputerHostDescriptor.from_dict(raw_computer_host)
            except ProtocolValidationError:
                data["computer_host"] = None
        registration = RegisterUI(**data)
        registration.validate()
        return registration


# --- Feature 006: User-Configurable LLM Subscription -------------------
@dataclass
class LLMConfigSet(Message):
    """Client→server: user saved/updated their personal LLM configuration.

    The ``config`` dict carries ``api_key``, ``base_url``, ``model`` — all
    three required and non-empty. Server-side validation rejects any
    malformed payload with an ``error`` reply (code ``llm_config_invalid``)
    and does NOT mutate the per-WebSocket credential store.
    """
    type: str = "llm_config_set"
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMConfigClear(Message):
    """Client→server: user cleared their personal LLM configuration.

    Pops the per-WebSocket credential entry. Subsequent LLM-dependent
    calls fall back to the operator's ``.env`` default credentials (or
    fail closed if those are also unavailable).
    """
    type: str = "llm_config_clear"


@dataclass
class LLMConfigAck(Message):
    """Server→client: acknowledgement for ``llm_config_set`` / ``llm_config_clear``."""
    type: str = "llm_config_ack"
    ok: bool = True


@dataclass
class LLMUsageReport(Message):
    """Server→client: per-call token-usage report.

    Emitted ONLY when the LLM call was served using the user's personal
    credentials (``credential_source == 'user'``). Calls served using
    the operator default are NOT reported, so the per-user token-usage
    counters in the browser only reflect the user's own spend.

    ``total_tokens`` / ``prompt_tokens`` / ``completion_tokens`` may be
    ``None`` when the upstream response omitted the ``usage`` block.
    """
    type: str = "llm_usage_report"
    feature: str = ""           # call-site identifier, e.g. "tool_dispatch"
    model: str = ""
    total_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    outcome: str = "success"     # "success" | "failure"
    at: str = ""                 # ISO 8601 timestamp


# --- Streaming tool metadata validation (001-tool-stream-ui) ---
def validate_streaming_metadata(metadata: Dict[str, Any]) -> None:
    """Validate the streaming-related fields of an ``AgentSkill.metadata``.

    Called by the orchestrator at ``RegisterAgent`` time for any tool whose
    metadata declares ``streamable: True``. Raises ``ValueError`` with a
    human-readable message on the first invariant violation.

    Required when streamable=True:
    - ``streaming_kind`` MUST be ``"push"`` or ``"poll"``.

    Optional but constrained:
    - ``min_fps`` and ``max_fps`` (default 5/30) MUST satisfy
      ``1 <= min_fps <= max_fps <= 60``.
    - ``max_chunk_bytes`` (default 65536) MUST be ``<= 1 << 20`` (1 MiB hard
      ceiling — anything larger indicates a tool that should be paginated,
      not streamed).
    - ``default_interval_s`` (poll only) MUST be a positive number when
      present.

    See specs/001-tool-stream-ui/contracts/protocol-messages.md §B5.
    """
    if not metadata.get("streamable"):
        return  # nothing to validate
    kind = metadata.get("streaming_kind")
    if kind not in ("push", "poll"):
        raise ValueError(
            f"streamable tool metadata must set streaming_kind to 'push' or "
            f"'poll', got {kind!r}"
        )
    min_fps = metadata.get("min_fps", 5)
    max_fps = metadata.get("max_fps", 30)
    if not isinstance(min_fps, int) or not isinstance(max_fps, int):
        raise ValueError(
            f"min_fps and max_fps must be integers, got {min_fps!r}, {max_fps!r}"
        )
    if not (1 <= min_fps <= max_fps <= 60):
        raise ValueError(
            f"streamable tool fps clamp invalid: must satisfy "
            f"1 <= min_fps <= max_fps <= 60, got min={min_fps}, max={max_fps}"
        )
    max_chunk_bytes = metadata.get("max_chunk_bytes", 65536)
    if not isinstance(max_chunk_bytes, int) or max_chunk_bytes <= 0:
        raise ValueError(
            f"max_chunk_bytes must be a positive integer, got {max_chunk_bytes!r}"
        )
    if max_chunk_bytes > (1 << 20):
        raise ValueError(
            f"max_chunk_bytes={max_chunk_bytes} exceeds 1 MiB hard ceiling; "
            f"streaming is not the right pattern for chunks this large"
        )
    if kind == "poll":
        interval = metadata.get("default_interval_s")
        if interval is not None and (not isinstance(interval, (int, float)) or interval <= 0):
            raise ValueError(
                f"default_interval_s must be a positive number, got {interval!r}"
            )
