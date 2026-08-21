"""Authorized self-service account-retirement attachment boundary.

The initiating identity is the verified Keycloak ``sub``.  Logout never calls
this module.  Plane atomically retires the owner's attachment namespace and
reconciles physical absence asynchronously.  Keycloak account removal must be
performed only after this status reaches ``purged``.  A ``manual_review`` state
is intentionally recoverable only through Plane's evidence-bound operator
procedure.
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
    """Durably accept owner cleanup without waiting for physical deletion."""

    return await purge_coordinator.aschedule_owner(owner_id=user_id)


async def account_retirement_status(
    purge_coordinator: AttachmentPurgeCoordinator,
    user_id: str,
    cleanup_id: str,
) -> AttachmentPurgeStatus | None:
    """Return only the authenticated owner's cleanup status."""

    return await purge_coordinator.aowner_cleanup_status(
        owner_id=user_id,
        cleanup_id=cleanup_id,
    )


def purge_user_attachments(
    purge_coordinator: AttachmentPurgeCoordinator,
    user_id: str,
) -> AttachmentPurgeOutcome:
    """Schedule one owner namespace and report its actual physical state.

    Args:
        purge_coordinator: The application-scoped durable purge boundary.
        user_id: The Keycloak ``sub`` of the deleted account.

    Returns:
        The committed logical-deletion and physical-purge outcome.  A caller
        MUST NOT report account purge complete unless ``outcome.completed`` is
        true.
    """

    return purge_coordinator.schedule_owner(owner_id=user_id)


__all__ = [
    "account_retirement_status",
    "initiate_account_retirement",
    "purge_user_attachments",
]
