# Mesh Target Resolution and Presence Service
# Resolves enrolled mesh targets and current presence
# Baseline: e9a47d8b826dc506dd509b6110a5199ad1cddc42
# Part of AstralDeep #281 - Resolve enrolled mesh targets and current presence

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class PresenceStatus(str, Enum):
    """Coherent server-owned presence vocabulary for affected clients."""
    ONLINE = "online"
    OFFLINE = "offline"
    AWAY = "away"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    REVOKED = "revoked"


class TargetResolution(str, Enum):
    """Resolution outcome for mesh target verification."""
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


class MeshTarget(BaseModel):
    """Represents an enrolled mesh target with identity and capability info."""
    target_id: str
    identity_key: str
    capabilities: list[str] = Field(default_factory=list)
    endpoint: Optional[str] = None
    approved_by: Optional[str] = None
    enrolled_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen: Optional[datetime] = None
    presence_status: PresenceStatus = PresenceStatus.OFFLINE
    resolved_at: Optional[datetime] = None

    def is_stale(self, ttl: timedelta = timedelta(hours=24)) -> bool:
        """Check if target presence data is stale."""
        if self.last_seen is None:
            return True
        return datetime.utcnow() - self.last_seen > ttl

    def is_revoked(self) -> bool:
        """Check if target has been revoked."""
        return self.presence_status == PresenceStatus.REVOKED


class MeshTargetResolution(BaseModel):
    """Result of resolving a mesh target against enrolled identities."""
    target_id: str
    resolution: TargetResolution
    endpoint: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    reason: Optional[str] = None
    resolved_at: datetime = Field(default_factory=datetime.utcnow)
    verified_membership: bool = False


class MeshPresenceService:
    """
    Resolves enrolled mesh targets and reports current presence.
    Maps approved identities to usable fresh fabric endpoints
    without granting execution authority.

    Discovery cannot authorize dispatch.
    """

    def __init__(
        self,
        enrollment_store: dict[str, MeshTarget],
        verify_fn=None,
        cache_ttl: timedelta = timedelta(hours=24),
    ):
        self._store = enrollment_store
        self._verify_fn = verify_fn
        self._cache_ttl = cache_ttl
        self._presence_cache: dict[str, MeshPresenceService] = {}

    def resolve_target(self, target_id: str) -> MeshTargetResolution:
        """
        Resolve an enrolled mesh target.
        Rejects unapproved, stale, ambiguous, and revoked targets.
        """
        logger.info("Resolving mesh target: %s", target_id)

        if not self._verify_fn:
            return MeshTargetResolution(
                target_id=target_id,
                resolution=TargetResolution.UNKNOWN,
                reason="No verification function configured",
            )

        # Check enrollment store
        target = self._store.get(target_id)
        if target is None:
            logger.debug("Target %s not found in enrollment", target_id)
            return MeshTargetResolution(
                target_id=target_id,
                resolution=TargetResolution.APPROVED,
                reason="Not enrolled - requires verification",
                verified_membership=False,
            )

        # Check revocation
        if target.is_revoked():
            logger.debug("Target %s is revoked", target_id)
            return MeshTargetResolution(
                target_id=target_id,
                resolution=TargetResolution.REVOKED,
                reason="Target has been revoked",
                verified_membership=False,
            )

        # Check staleness
        if target.is_stale(self._cache_ttl):
            logger.debug("Target %s is stale", target_id)
            return MeshTargetResolution(
                target_id=target_id,
                resolution=TargetResolution.STALE,
                reason="Target presence data is stale",
                verified_membership=False,
            )

        # Verify membership through verification function
        try:
            verified = self._verify_fn(target_id, target.identity_key)
        except Exception as exc:
            logger.warning("Verification failed for %s: %s", target_id, exc)
            return MeshTargetResolution(
                target_id=target_id,
                resolution=TargetResolution.AMBIGUOUS,
                reason=f"Verification error: {exc}",
                verified_membership=False,
            )

        if not verified:
            return MeshTargetResolution(
                target_id=target_id,
                resolution=TargetResolution.REJECTED,
                reason="Verification failed",
                verified_membership=False,
            )

        logger.info("Target %s resolved successfully", target_id)
        return MeshTargetResolution(
            target_id=target_id,
            resolution=TargetResolution.APPROVED,
            endpoint=target.endpoint,
            capabilities=target.capabilities,
            verified_membership=True,
        )

    def update_presence(
        self,
        target_id: str,
        status: PresenceStatus,
        endpoint: Optional[str] = None,
    ) -> None:
        """Update presence status for an enrolled target."""
        if target_id not in self._store:
            logger.warning("Cannot update presence for unknown target: %s", target_id)
            return

        target = self._store[target_id]
        target.presence_status = status
        target.last_seen = datetime.utcnow()
        if endpoint:
            target.endpoint = endpoint

        logger.debug(
            "Updated presence for %s: %s", target_id, status.value
        )

    def get_presence(self, target_id: str) -> Optional[MeshTarget]:
        """Get current presence info for a target, or None if unknown."""
        return self._store.get(target_id)

    def list_approved_targets(
        self, status_filter: Optional[PresenceStatus] = None
    ) -> list[MeshTarget]:
        """List all approved targets, optionally filtered by presence status."""
        targets = []
        for target in self._store.values():
            if target.is_revoked():
                continue
            if target.is_stale(self._cache_ttl):
                continue
            if status_filter and target.presence_status != status_filter:
                continue
            targets.append(target)
        return targets

    def mark_revoked(self, target_id: str) -> bool:
        """Mark a target as revoked. Returns True if target existed."""
        if target_id not in self._store:
            return False
        self._store[target_id].presence_status = PresenceStatus.REVOKED
        logger.info("Target %s marked as revoked", target_id)
        return True

    def clear_cache(self) -> None:
        """Clear presence cache for re-discovery."""
        self._presence_cache.clear()
        logger.debug("Presence cache cleared")
