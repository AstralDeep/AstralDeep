"""Self-service account-retirement entry point keyed to the verified Keycloak sub;
delegates to attachments/purge.py to atomically retire the owner's namespace, with
physical reconciliation happening asynchronously.
"""

from __future__ import annotations

from orchestrator.attachments.purge import (
    AttachmentPurgeAcceptance,
    AttachmentPurgeCoordinator,
    AttachmentPurgeOutcome,
    AttachmentPurgeStatus,
)


async def initiate_account_retirement(
    purge_coordinator: AttachmentPurgeCoordinator,
    user_id: str,
) -> AttachmentPurgeAcceptance:
    return await purge_coordinator.aschedule_owner(owner_id=user_id)


async def account_retirement_status(
    purge_coordinator: AttachmentPurgeCoordinator,
    user_id: str,
    cleanup_id: str,
) -> AttachmentPurgeStatus | None:
    return await purge_coordinator.aowner_cleanup_status(
        owner_id=user_id,
        cleanup_id=cleanup_id,
    )


def purge_user_attachments(
    purge_coordinator: AttachmentPurgeCoordinator,
    user_id: str,
) -> AttachmentPurgeOutcome:
    return purge_coordinator.schedule_owner(owner_id=user_id)


__all__ = [
    "account_retirement_status",
    "initiate_account_retirement",
    "purge_user_attachments",
]
