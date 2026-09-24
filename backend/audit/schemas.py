"""Pydantic DTOs for the audit log: AuditEventCreate enforces size/shape limits at the
write boundary used by Recorder.record, AuditEventDTO is the public read shape
returned over REST and WebSocket.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .pii import normalize_extension, strip_filename

EVENT_CLASSES = (
    "auth",
    "conversation",
    "file",
    "settings",
    "agent_tool_call",
    "agent_ui_render",
    "agent_external_call",
    "audit_view",
    "component_feedback",
    "tool_quality",
    "proposal_review",
    "quarantine",
    "onboarding_started",
    "onboarding_completed",
    "onboarding_skipped",
    "onboarding_replayed",
    "tutorial_step_edited",
    "llm_config_change",
    "llm_unconfigured",
    "llm_call",
    "personalization",
    "memory",
    "skill",
    "schedule",
    "dreaming",
    "agent_lifecycle",
    "delegation",
    "typesafe_credential",
    "typesafe",
    "llm_data_sharing",
)

OUTCOMES = ("in_progress", "success", "failure", "interrupted")

# Small enough to catch anyone inlining a payload
MAX_META_SERIALIZED_BYTES = 4096
MAX_META_PROPERTIES = 32
MAX_ARTIFACT_POINTERS = 32


class ArtifactPointer(BaseModel):
    artifact_id: str
    store: str
    extension: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    available: bool = True


class AuditEventDTO(BaseModel):
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


def _validate_meta(value: Dict[str, Any]) -> Dict[str, Any]:
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
    for k, v in cleaned.items():
        if isinstance(v, (bytes, bytearray)):
            raise ValueError(
                f"audit metadata field {k!r} contains raw bytes; "
                f"only non-PHI metadata is allowed (FR-004)"
            )
    serialized = json.dumps(cleaned, default=str)
    if len(serialized.encode("utf-8")) > MAX_META_SERIALIZED_BYTES:
        raise ValueError(
            f"audit metadata exceeds {MAX_META_SERIALIZED_BYTES} bytes "
            f"serialized — store the payload externally and reference its "
            f"artifact_id instead (FR-004)"
        )
    return cleaned


class AuditEventCreate(BaseModel):
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
            object.__setattr__(self, "completed_at", self.started_at)
        return self
