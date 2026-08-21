"""Unbound account-retirement service boundary for attachment purges.

The product has not yet selected an account-deletion authority (self-service
endpoint versus an authenticated identity-provider administrative event), so
this module is deliberately not mounted or called from logout.  The eventual
authorized caller must use this boundary: Plane atomically soft-deletes the
owner's attachment metadata with a durable namespace tombstone, then verifies
physical absence outside that database transaction.
"""

from __future__ import annotations

from orchestrator.attachments.purge import (
    AttachmentPurgeCoordinator,
    AttachmentPurgeOutcome,
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


__all__ = ["purge_user_attachments"]
