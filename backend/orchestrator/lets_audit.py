"""Redacted correlation of Astral, Plane, LETS, claim, and effect evidence."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final


logger = logging.getLogger("AstralDeep.LETS.Audit")

_ALLOWED_FIELDS: Final = frozenset(
    {
        "operation_id",
        "audit_correlation_id",
        "agent_id",
        "runtime_id",
        "tool_id",
        "scope",
        "channel",
        "binding_id",
        "enforced",
        "code",
        "receipt_sha256",
        "resulting_sequence",
        "plane_operation_version",
        "claim_sequence",
        "effect_result_sha256",
    }
)
_FAILURE_EVENTS: Final = frozenset(
    {"denied", "would_deny", "outcome_uncertain", "effect_failed"}
)


class LetsAuditError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _redacted_metadata(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) - _ALLOWED_FIELDS:
        raise LetsAuditError("unsafe_lets_audit_metadata")
    result: dict[str, object] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, bool) or type(item) is int:
            result[key] = item
            continue
        if isinstance(item, str) and 0 < len(item) <= 512:
            result[key] = item
            continue
        raise LetsAuditError("unsafe_lets_audit_metadata")
    return result


@dataclass(frozen=True, slots=True)
class LetsAuditObserver:
    """Gateway observer that appends one value-free hash-chained event."""

    actor_user_id: str
    auth_principal: str
    agent_id: str
    conversation_id: str | None = None
    strict: bool = True

    async def __call__(self, event: str, evidence: Mapping[str, object]) -> None:
        if not isinstance(event, str) or not event or len(event) > 64:
            raise LetsAuditError("invalid_lets_audit_event")
        metadata = _redacted_metadata(evidence)
        correlation_id = metadata.get("audit_correlation_id")
        if not isinstance(correlation_id, str):
            raise LetsAuditError("missing_lets_audit_correlation")
        try:
            from audit.recorder import get_recorder
            from audit.schemas import AuditEventCreate

            recorder = get_recorder()
            if recorder is None:
                if self.strict:
                    raise LetsAuditError("audit_recorder_unavailable")
                return
            outcome = "failure" if event in _FAILURE_EVENTS else "success"
            now = datetime.now(UTC)
            await recorder.record(
                AuditEventCreate(
                    actor_user_id=self.actor_user_id,
                    auth_principal=self.auth_principal,
                    agent_id=self.agent_id,
                    event_class="agent_tool_call",
                    action_type=f"lets.{event}",
                    description=(
                        "LETS protected-effect checkpoint recorded with redacted "
                        "cross-system correlation metadata."
                    ),
                    conversation_id=self.conversation_id,
                    correlation_id=correlation_id,
                    outcome=outcome,
                    inputs_meta=metadata,
                    started_at=now,
                    completed_at=now,
                )
            )
        except LetsAuditError:
            raise
        except Exception:
            logger.warning("LETS audit append failed", exc_info=True)
            if self.strict:
                raise LetsAuditError("audit_append_failed") from None


__all__ = ("LetsAuditError", "LetsAuditObserver")
