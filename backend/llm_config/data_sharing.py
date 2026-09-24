"""Gates every credential save (LLM, TypeSafe, and the legacy WS path) on an
acknowledged third-party data-sharing notice, recording acceptance or refusal via
audit/recorder.py. Never interrupts an existing user mid-conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any, Optional
from uuid import uuid4

from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from

logger = logging.getLogger("LLMConfig.DataSharing")

# Bump on any title/body change — a test pins this
NOTICE_VERSION = "2026-09-17.1"

NOTICE_TITLE = "Your data may be shared"

NOTICE_BODY = (
    "If you use a third-party model or a TypeSafe model, the content of your "
    "requests — which can include your messages and conversation context — is "
    "sent to that provider and may be shared with them."
)

CHECKBOX_LABEL = (
    "I understand that my data may be shared with third-party model providers "
    "and TypeSafe through requests."
)

FIELD_NAME = "data_sharing_acknowledged"

FIELD_ERROR = "Check this box to confirm you understand how your data is shared."

LEGACY_ERROR = (
    "Confirm the data-sharing notice in Settings → LLM settings, then save again."
)

WARNING_ELEMENT_ID = "data-sharing-warning"
CHECKBOX_ELEMENT_ID = "data-sharing-ack"


@dataclass(frozen=True, slots=True)
class AckResult:
    allowed: bool
    acknowledged_at: Optional[datetime] = None
    newly_acknowledged: bool = False
    error: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass(frozen=True, slots=True)
class AcknowledgmentState:
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    notice_version: Optional[str] = None


class DataSharingStore:
    def __init__(
        self,
        db: Any = None,
        *,
        plane_runtime: Any = None,
        plane_repositories: Any = None,
        acknowledgment_repository: Any = None,
    ) -> None:
        self.db = db
        repository, runtime = repository_from(
            "preferences",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        resolved = acknowledgment_repository
        if resolved is None:
            resolved = getattr(repository, "data_sharing", None)
        if resolved is None:  # pragma: no cover
            raise ValueError("the Plane data-sharing acknowledgment store is required")
        self._repository = PlaneRepositoryContext(
            repository=resolved,
            plane_runtime=runtime,
            legacy_database=db,
        )

    def state_sync(self, user_id: str) -> AcknowledgmentState:
        try:
            record = self._repository.call(
                self._repository.repository.get_user, owner_id=user_id
            )
        except Exception:
            logger.warning(
                "data-sharing acknowledgment read failed; treating as unacknowledged",
                exc_info=True,
            )
            return AcknowledgmentState()
        if record is None:
            return AcknowledgmentState()
        return AcknowledgmentState(
            acknowledged=record.notice_version == NOTICE_VERSION,
            acknowledged_at=record.acknowledged_at,
            notice_version=record.notice_version,
        )

    async def state(self, user_id: str) -> AcknowledgmentState:
        import asyncio

        return await asyncio.to_thread(self.state_sync, user_id)

    def acknowledge_sync(self, user_id: str, at: Optional[datetime] = None) -> datetime:
        moment = at or datetime.now(UTC)
        record = self._repository.call(
            self._repository.repository.acknowledge,
            owner_id=user_id,
            notice_version=NOTICE_VERSION,
            at=moment,
        )
        return record.acknowledged_at

    async def acknowledge(self, user_id: str, at: Optional[datetime] = None) -> datetime:
        import asyncio

        return await asyncio.to_thread(self.acknowledge_sync, user_id, at)


def require_acknowledgment(
    store: DataSharingStore,
    user_id: str,
    submitted: Optional[bool],
    *,
    legacy: bool = False,
) -> AckResult:
    error = LEGACY_ERROR if legacy else FIELD_ERROR

    if submitted is True:
        state = store.state_sync(user_id)
        acknowledged_at = store.acknowledge_sync(user_id)
        return AckResult(
            allowed=True,
            acknowledged_at=acknowledged_at,
            newly_acknowledged=not state.acknowledged,
        )

    if submitted is False:
        return AckResult(allowed=False, error=error)

    state = store.state_sync(user_id)
    if state.acknowledged:
        return AckResult(allowed=True, acknowledged_at=state.acknowledged_at)
    return AckResult(allowed=False, error=error)


async def record_acknowledged(
    recorder: Any,
    *,
    actor_user_id: str,
    auth_principal: str,
    transport: str = "ws",
    correlation_id: Optional[str] = None,
) -> None:
    await _record(
        recorder,
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        action="acknowledged",
        outcome="success",
        description=(
            "User acknowledged the third-party data-sharing notice "
            f"(version {NOTICE_VERSION})"
        ),
        transport=transport,
        correlation_id=correlation_id,
    )


async def record_save_blocked(
    recorder: Any,
    *,
    actor_user_id: str,
    auth_principal: str,
    target: str,
    transport: str = "ws",
    correlation_id: Optional[str] = None,
) -> None:
    await _record(
        recorder,
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        action="save_blocked",
        outcome="failure",
        description=(
            f"Credential save to {target} was blocked: the data-sharing notice "
            f"(version {NOTICE_VERSION}) was not acknowledged"
        ),
        transport=transport,
        correlation_id=correlation_id,
        target=target,
    )


async def _record(
    recorder: Any,
    *,
    actor_user_id: str,
    auth_principal: str,
    action: str,
    outcome: str,
    description: str,
    transport: str,
    correlation_id: Optional[str],
    target: Optional[str] = None,
) -> None:
    if recorder is None:
        return
    from audit.schemas import AuditEventCreate

    from .audit_events import _assert_no_api_key

    inputs_meta: dict[str, Any] = {
        "action": action,
        "notice_version": NOTICE_VERSION,
        "transport": transport,
    }
    if target is not None:
        inputs_meta["target"] = target
    _assert_no_api_key(inputs_meta)

    started = datetime.now(UTC)
    try:
        await recorder.record(
            AuditEventCreate(
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                event_class="llm_data_sharing",
                action_type=f"llm_data_sharing.{action}",
                description=description,
                correlation_id=correlation_id or str(uuid4()),
                outcome=outcome,
                inputs_meta=inputs_meta,
                outputs_meta={},
                started_at=started,
                completed_at=started,
            )
        )
    except Exception:  # pragma: no cover
        logger.warning("data-sharing audit event failed", exc_info=True)


__all__ = (
    "CHECKBOX_ELEMENT_ID",
    "CHECKBOX_LABEL",
    "FIELD_ERROR",
    "FIELD_NAME",
    "LEGACY_ERROR",
    "NOTICE_BODY",
    "NOTICE_TITLE",
    "NOTICE_VERSION",
    "WARNING_ELEMENT_ID",
    "AckResult",
    "AcknowledgmentState",
    "DataSharingStore",
    "record_acknowledged",
    "record_save_blocked",
    "require_acknowledgment",
)
