"""Pydantic request/response schemas for onboarding: the single source of truth for
validation (target/target_key consistency, non-empty title/body, disallowed status
transitions), used by onboarding/api.py and repository.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


STATUS_VALUES = ("not_started", "in_progress", "completed", "skipped")
STATUS_WRITABLE = ("in_progress", "completed", "skipped")
AUDIENCE_VALUES = ("user", "admin")
TARGET_KIND_VALUES = ("static", "sdui", "none")
CHANGE_KIND_VALUES = ("create", "update", "archive", "restore")

TITLE_MAX = 120
BODY_MAX = 1000


class OnboardingStateResponse(BaseModel):
    status: str
    last_step_id: Optional[int] = None
    last_step_slug: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    skipped_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
    dismiss_count: int = 0

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in STATUS_VALUES:
            raise ValueError(f"unknown status: {v!r}")
        return v


class OnboardingStateUpdateRequest(BaseModel):
    status: str
    last_step_id: Optional[int] = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in STATUS_WRITABLE:
            raise ValueError(
                f"status must be one of {STATUS_WRITABLE}; "
                f"clients cannot set 'not_started' or 'replay' here"
            )
        return v


class TutorialStepDTO(BaseModel):
    id: int
    slug: str
    audience: str
    display_order: int
    target_kind: str
    target_key: Optional[str] = None
    title: str
    body: str
    archived_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("audience")
    @classmethod
    def _check_audience(cls, v: str) -> str:
        if v not in AUDIENCE_VALUES:
            raise ValueError(f"unknown audience: {v!r}")
        return v

    @field_validator("target_kind")
    @classmethod
    def _check_target_kind(cls, v: str) -> str:
        if v not in TARGET_KIND_VALUES:
            raise ValueError(f"unknown target_kind: {v!r}")
        return v

    def to_user_view(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "audience": self.audience,
            "display_order": self.display_order,
            "target_kind": self.target_kind,
            "target_key": self.target_key,
            "title": self.title,
            "body": self.body,
        }


class TutorialStepListResponse(BaseModel):
    steps: List[TutorialStepDTO]


class AdminTutorialStepCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=128)
    audience: str
    display_order: int
    target_kind: str
    target_key: Optional[str] = None
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX)

    @field_validator("audience")
    @classmethod
    def _check_audience(cls, v: str) -> str:
        if v not in AUDIENCE_VALUES:
            raise ValueError(f"audience must be one of {AUDIENCE_VALUES}")
        return v

    @field_validator("target_kind")
    @classmethod
    def _check_target_kind(cls, v: str) -> str:
        if v not in TARGET_KIND_VALUES:
            raise ValueError(f"target_kind must be one of {TARGET_KIND_VALUES}")
        return v

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must contain non-whitespace characters")
        return v

    @field_validator("body")
    @classmethod
    def _check_body(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must contain non-whitespace characters")
        return v

    @model_validator(mode="after")
    def _check_target_consistency(self) -> "AdminTutorialStepCreateRequest":
        if self.target_kind == "none" and self.target_key is not None:
            raise ValueError("target_kind='none' requires target_key=null")
        if self.target_kind in ("static", "sdui") and not (self.target_key or "").strip():
            raise ValueError(
                f"target_kind='{self.target_kind}' requires a non-empty target_key"
            )
        return self


# slug omitted on purpose — slugs are stable identifiers
class AdminTutorialStepUpdateRequest(BaseModel):
    audience: Optional[str] = None
    display_order: Optional[int] = None
    target_kind: Optional[str] = None
    target_key: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=TITLE_MAX)
    body: Optional[str] = Field(default=None, max_length=BODY_MAX)

    @field_validator("audience")
    @classmethod
    def _check_audience(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in AUDIENCE_VALUES:
            raise ValueError(f"audience must be one of {AUDIENCE_VALUES}")
        return v

    @field_validator("target_kind")
    @classmethod
    def _check_target_kind(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in TARGET_KIND_VALUES:
            raise ValueError(f"target_kind must be one of {TARGET_KIND_VALUES}")
        return v

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("title must contain non-whitespace characters")
        return v

    @field_validator("body")
    @classmethod
    def _check_body(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("body must contain non-whitespace characters")
        return v


class AdminTutorialStepListResponse(BaseModel):
    steps: List[TutorialStepDTO]


class RevisionDTO(BaseModel):
    id: int
    step_id: int
    editor_user_id: str
    edited_at: datetime
    change_kind: str
    previous: Optional[Dict[str, Any]] = None
    current: Dict[str, Any]

    @field_validator("change_kind")
    @classmethod
    def _check_change_kind(cls, v: str) -> str:
        if v not in CHANGE_KIND_VALUES:
            raise ValueError(f"unknown change_kind: {v!r}")
        return v


class RevisionListResponse(BaseModel):
    revisions: List[RevisionDTO]
