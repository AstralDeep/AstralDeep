"""
Pydantic schemas for the audit log (write-side and read-side DTOs).

The write-side ``AuditEventCreate`` enforces FR-004 / FR-015 / FR-016 at
application input boundaries: it rejects payload-shaped keys, caps
serialized metadata size, and applies filename stripping. Any code that
records into the audit log SHOULD construct an ``AuditEventCreate``
rather than passing dicts directly to the repository.

The read-side ``AuditEventDTO`` matches the public-facing JSON Schema in
``contracts/audit-event-schema.json`` and is what REST and WebSocket
clients receive. It deliberately omits internal AU-9 fields
(``prev_hash``, ``entry_hash``, ``key_id``, ``schema_version``) and the
forensic-only ``auth_principal``.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .pii import normalize_extension, strip_filename

# ---------------------------------------------------------------------------
# Constants — also exported for use in repository / API code
# ---------------------------------------------------------------------------

EVENT_CLASSES = (
    "auth",
    "conversation",
    "file",
    "settings",
    "agent_tool_call",
    "agent_ui_render",
    "agent_external_call",
    "audit_view",
    # Feature 004 — component feedback & tool-improvement loop
    "component_feedback",
    "tool_quality",
    "proposal_review",
    "quarantine",
    # Feature 005 — tool tips and getting started tutorial
    "onboarding_started",
    "onboarding_completed",
    "onboarding_skipped",
    "onboarding_replayed",
    "tutorial_step_edited",
    # Feature 006 — user-configurable LLM subscription
    "llm_config_change",
    "llm_unconfigured",
    "llm_call",
    # Feature 025 — agentic soul integration
    "personalization",   # profile / personality ("soul") changes
    "memory",            # durable-memory create/view/update/delete/promote
    "skill",             # skill (agent tool) enable/disable
    "schedule",          # scheduled-job lifecycle + each run
    "dreaming",          # background consolidation sweeps + toggles
    # Feature 027 — agentic agent/tool creation. Action types:
    # lifecycle.gap_detected / .auto_created / .self_test / .refined /
    # .approved / .rejected / .discarded / .revision_applied /
    # .revision_rolled_back — one correlation_id per capability gap.
    "agent_lifecycle",
    # Feature 056 — delegated agent chaining. Per-hop provenance records
    # (delegation.hop.mint / delegation.hop.enforce pairs sharing the turn's
    # correlation_id) ride the hash chain so a full delegation chain is
    # reconstructable from the audit log alone (048 T018 / 056 FR-026).
    "delegation",
    # Feature 089 — TypeSafe System One routing.
    # ``typesafe_credential``: the user saving, replacing, removing or having
    # an unreadable TypeSafe key discarded. Carries the 12-character key
    # fingerprint, never the key.
    "typesafe_credential",
    # ``typesafe``: the per-turn routing record. ``typesafe.security_verdict``
    # carries the three scores and the verdict; ``typesafe.routing_fallback``
    # carries the outcome class, attempt count and elapsed time. Neither
    # carries request text.
    "typesafe",
    # ``llm_data_sharing``: the third-party data-sharing notice.
    # ``.acknowledged`` when a user accepts a notice version; ``.save_blocked``
    # when a credential save was refused for want of one.
    "llm_data_sharing",
)

OUTCOMES = ("in_progress", "success", "failure", "interrupted")

# Hard size cap on JSON-serialized metadata payloads (FR-004 enforcement).
# The cap is generous enough for normal action descriptors but small
# enough that anyone who tries to inline a payload will hit it.
MAX_META_SERIALIZED_BYTES = 4096
MAX_META_PROPERTIES = 32
MAX_ARTIFACT_POINTERS = 32


# ---------------------------------------------------------------------------
# Public DTO (read-side) — matches contracts/audit-event-schema.json
# ---------------------------------------------------------------------------

class ArtifactPointer(BaseModel):
    """Pointer to a source artifact (FR-004 / FR-017).

    The audit row never copies the artifact's bytes — only the
    identifier and the metadata required for the user to navigate
    back to the artifact (subject to its own access control, FR-018).
    ``available`` is recomputed at read time and flips to ``False``
    once the artifact's own retention has elapsed.
    """
    artifact_id: str
    store: str
    extension: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    available: bool = True


class AuditEventDTO(BaseModel):
    """Public-facing audit event shape returned by REST and WebSocket."""
    event_id: str
    event_class: str
    action_type: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    agent_id: Optional[str] = None
    conversation_id: Optional[str] = None
    correlation_id: str
    outcome: str
    outcome_detail: Optional[str] = Field(default=None, max_length=2048)
    inputs_meta: Dict[str, Any] = Field(default_factory=dict)
    outputs_meta: Dict[str, Any] = Field(default_factory=dict)
    artifact_pointers: List[ArtifactPointer] = Field(default_factory=list)
    started_at: datetime
    completed_at: Optional[datetime] = None
    recorded_at: datetime

    @field_validator("event_class")
    @classmethod
    def _check_event_class(cls, v: str) -> str:
        if v not in EVENT_CLASSES:
            raise ValueError(f"unknown event_class: {v!r}")
        return v

    @field_validator("outcome")
    @classmethod
    def _check_outcome(cls, v: str) -> str:
        if v not in OUTCOMES:
            raise ValueError(f"unknown outcome: {v!r}")
        return v


# ---------------------------------------------------------------------------
# Write-side schema — what callers pass to Recorder.record(...)
# ---------------------------------------------------------------------------

def _validate_meta(value: Dict[str, Any]) -> Dict[str, Any]:
    """Apply FR-004 invariants to an ``inputs_meta`` / ``outputs_meta`` dict.

    Strips filename-shaped fields, rejects oversize payloads, and rejects
    raw bytes / payload-shaped keys. Returns the cleaned dict.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("inputs_meta / outputs_meta must be a dict")
    if len(value) > MAX_META_PROPERTIES:
        raise ValueError(
            f"audit metadata exceeds {MAX_META_PROPERTIES} properties — "
            f"payload-shaped data must not be inlined"
        )
    cleaned = strip_filename(value)
    # Reject any value that is bytes — those are payload-shaped by definition.
    for k, v in cleaned.items():
        if isinstance(v, (bytes, bytearray)):
            raise ValueError(
                f"audit metadata field {k!r} contains raw bytes; "
                f"only non-PHI metadata is allowed (FR-004)"
            )
    # Cap on serialized size — catches oversized strings and nested dicts.
    serialized = json.dumps(cleaned, default=str)
    if len(serialized.encode("utf-8")) > MAX_META_SERIALIZED_BYTES:
        raise ValueError(
            f"audit metadata exceeds {MAX_META_SERIALIZED_BYTES} bytes "
            f"serialized — store the payload externally and reference its "
            f"artifact_id instead (FR-004)"
        )
    return cleaned


class AuditEventCreate(BaseModel):
    """Input model passed to ``Recorder.record(...)``.

    Strict by design — silent acceptance of payload-shaped fields would
    defeat the whole point of the audit log's data-minimization posture.
    """
    actor_user_id: str = Field(min_length=1)
    auth_principal: str = Field(min_length=1)
    agent_id: Optional[str] = None
    event_class: str
    action_type: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    conversation_id: Optional[str] = None
    correlation_id: str = Field(min_length=1)
    outcome: str
    outcome_detail: Optional[str] = Field(default=None, max_length=2048)
    inputs_meta: Dict[str, Any] = Field(default_factory=dict)
    outputs_meta: Dict[str, Any] = Field(default_factory=dict)
    artifact_pointers: List[ArtifactPointer] = Field(default_factory=list)
    started_at: datetime
    completed_at: Optional[datetime] = None

    @field_validator("event_class")
    @classmethod
    def _check_event_class(cls, v: str) -> str:
        if v not in EVENT_CLASSES:
            raise ValueError(f"unknown event_class: {v!r}")
        return v

    @field_validator("outcome")
    @classmethod
    def _check_outcome(cls, v: str) -> str:
        if v not in OUTCOMES:
            raise ValueError(f"unknown outcome: {v!r}")
        return v

    @field_validator("inputs_meta", "outputs_meta", mode="before")
    @classmethod
    def _meta_validator(cls, value: Any) -> Dict[str, Any]:
        return _validate_meta(value)

    @field_validator("artifact_pointers", mode="before")
    @classmethod
    def _pointers_validator(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("artifact_pointers must be a list")
        if len(value) > MAX_ARTIFACT_POINTERS:
            raise ValueError(
                f"artifact_pointers exceeds {MAX_ARTIFACT_POINTERS} entries"
            )
        normalized: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, ArtifactPointer):
                normalized.append(item.model_dump())
                continue
            if not isinstance(item, dict):
                raise TypeError("artifact_pointers items must be dicts")
            cleaned = dict(item)
            # Always normalize extension; never carry a raw filename.
            cleaned["extension"] = normalize_extension(
                cleaned.get("extension") or cleaned.get("filename") or cleaned.get("name")
            )
            for forbidden in ("filename", "name", "original_name"):
                cleaned.pop(forbidden, None)
            normalized.append(cleaned)
        return normalized

    @model_validator(mode="after")
    def _check_outcome_completion(self) -> "AuditEventCreate":
        if self.outcome != "in_progress" and self.completed_at is None:
            # Default completed_at to started_at when caller forgot it
            # for atomic operations — preserves the invariant that any
            # terminal row has a completion timestamp.
            object.__setattr__(self, "completed_at", self.started_at)
        return self
