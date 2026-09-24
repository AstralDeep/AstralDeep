"""Typed dataclass views (Operation, Event, Artifact, ControlResult, RetryPolicy) over
Deep's Work-over-MCP wire contract, matching astral_sdk/work_contract.json field for
field, with unknown extra keys preserved on Operation.raw.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Operation:
    id: str
    revision: int
    instruction_revision: int
    control_epoch: int
    title: Optional[str]
    kind: Optional[str]
    disposition: str
    lifecycle: str
    phase: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    next_wake_at: Optional[str]
    deadline_at: Optional[str]
    schema_supported: bool
    safe_error_code: Optional[str]
    usage: dict[str, Any]
    created: Optional[bool] = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Operation":
        return cls(
            id=data["id"],
            revision=data["revision"],
            instruction_revision=data["instruction_revision"],
            control_epoch=data["control_epoch"],
            title=data.get("title"),
            kind=data.get("kind"),
            disposition=data["disposition"],
            lifecycle=data["lifecycle"],
            phase=data.get("phase"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            next_wake_at=data.get("next_wake_at"),
            deadline_at=data.get("deadline_at"),
            schema_supported=bool(data.get("schema_supported")),
            safe_error_code=data.get("safe_error_code"),
            usage=dict(data.get("usage") or {}),
            created=data.get("created"),
            raw=dict(data),
        )

    @property
    def is_terminal(self) -> bool:
        return self.disposition in ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class OperationList:
    operations: tuple[Operation, ...]
    next_cursor: Optional[str]
    page_full: bool
    resync_required: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationList":
        return cls(
            operations=tuple(Operation.from_dict(o) for o in data.get("operations", [])),
            next_cursor=data.get("next_cursor"),
            page_full=bool(data.get("page_full")),
            resync_required=bool(data.get("resync_required", False)),
        )


@dataclass(frozen=True)
class Event:
    revision: int
    changed: bool
    resync_required: bool
    operation: Optional[Operation]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        operation = data.get("operation")
        return cls(
            revision=data["revision"],
            changed=bool(data["changed"]),
            resync_required=bool(data.get("resync_required", False)),
            operation=Operation.from_dict(operation) if operation else None,
        )


@dataclass(frozen=True)
class Artifact:
    operation_id: str
    revision: int
    result: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        return cls(
            operation_id=data["id"],
            revision=data["revision"],
            result=dict(data.get("result") or {}),
        )


@dataclass(frozen=True)
class ControlResult:
    operation: Operation
    applied: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlResult":
        return cls(operation=Operation.from_dict(data["operation"]), applied=bool(data["applied"]))


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    multiplier: float = 2.0
    jitter_seconds: float = 0.1
    retryable_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 409, 429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delays must be non-negative")

    def delay_for(self, attempt: int) -> float:
        raw = self.base_delay_seconds * (self.multiplier ** max(0, attempt - 1))
        bounded = min(raw, self.max_delay_seconds)
        return bounded + random.uniform(0, self.jitter_seconds)

    def should_retry(self, *, attempt: int, status_code: Optional[int], is_network_error: bool) -> bool:
        if attempt >= self.max_attempts:
            return False
        if is_network_error:
            return True
        return status_code is not None and status_code in self.retryable_status_codes


__all__ = [
    "Operation",
    "OperationList",
    "Event",
    "Artifact",
    "ControlResult",
    "RetryPolicy",
]
