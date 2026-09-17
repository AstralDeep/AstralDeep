"""The third-party data-sharing notice and its acknowledgment (feature 089, US7).

Saving a credential for a third-party model provider means the content of the
user's requests will be sent to that provider. US7's requirement is that the
user is told so, in the same place and at the same moment they hand over the
key, and that they say they understand before the key is saved.

Three properties make that more than a checkbox.

**It gates the save, not the chat.** Acknowledgment is required as the *first*
step of every credential save -- the LLM save, the TypeSafe save and the legacy
WebSocket path -- before any validation probe. It is never consulted during a
turn, so an existing user is never interrupted mid-conversation by a consent
prompt.

**The version is part of the record.** Acknowledgment means the stored version
equals :data:`NOTICE_VERSION`. Changing the wording without bumping the version
would silently reuse consent the user gave to different text, so a test fails
if the strings move and the version does not.

**An explicit no is a no.** A submitted ``False`` -- the user unchecking a box
that was checked -- rejects the save. Only an *absent* field falls back to the
stored acknowledgment, which is what lets a client that does not send the field
at all keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any, Optional
from uuid import uuid4

from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from

logger = logging.getLogger("LLMConfig.DataSharing")

#: Bump this whenever :data:`NOTICE_TITLE` or :data:`NOTICE_BODY` changes.
#: ``test_the_notice_text_is_pinned_to_its_version`` fails otherwise.
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

#: The legacy WebSocket path has no field to attach an error to, so it says
#: where to go instead.
LEGACY_ERROR = (
    "Confirm the data-sharing notice in Settings → LLM settings, then save again."
)

WARNING_ELEMENT_ID = "data-sharing-warning"
CHECKBOX_ELEMENT_ID = "data-sharing-ack"


@dataclass(frozen=True, slots=True)
class AckResult:
    """The outcome of the acknowledgment check for one save attempt."""

    allowed: bool
    acknowledged_at: Optional[datetime] = None
    newly_acknowledged: bool = False
    error: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass(frozen=True, slots=True)
class AcknowledgmentState:
    """What a settings surface needs to render the checkbox."""

    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    notice_version: Optional[str] = None


class DataSharingStore:
    """Deep's facade over the Plane acknowledgment repository."""

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
        if resolved is None:  # pragma: no cover - a catalog without the store
            raise ValueError("the Plane data-sharing acknowledgment store is required")
        self._repository = PlaneRepositoryContext(
            repository=resolved,
            plane_runtime=runtime,
            legacy_database=db,
        )

    def state_sync(self, user_id: str) -> AcknowledgmentState:
        """Read the owner's acknowledgment. A durable failure reads as absent."""
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
    """Decide whether a credential save may proceed.

    ``submitted`` is tri-state on purpose:

    * ``True``  -- the user ticked the box now. Record it and allow the save.
    * ``False`` -- the user explicitly unticked it. Reject, even if they had
      acknowledged before: the most recent statement wins.
    * ``None``  -- the client did not send the field. Fall back to the stored
      acknowledgment, which is what keeps a client that has not been updated
      working.
    """
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
    """Emit ``llm_data_sharing.acknowledged``, once per notice version."""
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
    """Emit ``llm_data_sharing.save_blocked`` when a save is refused."""
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
    """Record one acknowledgment audit event. Never raises into a save path."""
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
    except Exception:  # pragma: no cover - auditing must not break a save
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
