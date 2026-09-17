"""Typed views over Deep's Work-over-MCP contract.

Every field name here matches ``sdk/astral_sdk/work_contract.json`` (itself
exported from the server's own ``orchestrator.work_service._public``,
``orchestrator.mcp_projection`` and ``orchestrator.work_operations`` —
see ``scripts/export_work_contract.py``). These classes never invent a field
the server does not send; an operation dict may have MORE keys than a given
server revision defines (additive-only wire evolution) and those extra keys
are preserved on ``Operation.raw`` even though a specific dataclass field for
them may not exist yet.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Operation:
    """One Work operation as the server projects it (submit/get/list/poll/cancel/pause)."""

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
    #: True only on a freshly created (not replayed) submission.
    created: Optional[bool] = None
    #: The exact server dict this was built from — never dropped, so a caller
    #: can read a field a newer server added before this SDK release knew its name.
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
    """The result of ``astral_list_operations``."""

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
    """The result of one ``astral_get_operation_events`` poll."""

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
    """The result of ``astral_get_artifact`` — one operation's retained result."""

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
    """The result of ``astral_cancel_operation``/``astral_pause_operation``."""

    operation: Operation
    applied: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlResult":
        return cls(operation=Operation.from_dict(data["operation"]), applied=bool(data["applied"]))


@dataclass(frozen=True)
class RetryPolicy:
    """A bounded exponential backoff with jitter for idempotent SDK calls.

    Only requests the SDK itself knows are safe to repeat are retried: reads
    (get/list/poll/result), and submit/cancel/pause when the caller supplies
    its own idempotency/submission key — never a bare, un-keyed mutation.
    """

    max_attempts: int = 4
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    multiplier: float = 2.0
    jitter_seconds: float = 0.1
    #: HTTP statuses worth a retry — network/timeout errors are always retried.
    retryable_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 409, 429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delays must be non-negative")

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before the given 1-indexed retry attempt."""
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
