"""
Service layer implementing the owner‑managed A2A publication workflow.
"""

from __future__ import annotations

import datetime
import uuid
from typing import List, Optional

from ..shared.feature_flags import FeatureFlags
from ..models.a2a_publication import A2APublication, PublicationState

# In‑memory store for the purpose of this bounty. In production this would be
# replaced by a proper persistence layer (e.g., SQLAlchemy model).
_PUBLICATION_STORE: dict[uuid.UUID, A2APublication] = {}


class A2APublicationError(RuntimeError):
    """Base exception for publication workflow errors."""


class PublicationNotFound(A2APublicationError):
    """Raised when a publication cannot be located."""


class InvalidTransition(A2APublicationError):
    """Raised when an illegal state transition is attempted."""


class OwnerMismatch(A2APublicationError):
    """Raised when the caller is not the owner of the publication."""


class A2APublicationService:
    """
    Core business logic for proposing, confirming, publishing and withdrawing
    A2A capabilities.
    """

    @staticmethod
    def _assert_owner(publication: A2APublication, caller_id: str) -> None:
        if publication.owner_id != caller_id:
            raise OwnerMismatch("Caller is not the owner of this publication.")

    @staticmethod
    def _assert_flag_enabled() -> None:
        if not FeatureFlags.OWNER_MANAGED_A2A_PUBLICATION.value.is_enabled():
            raise A2APublicationError(
                "Owner‑managed A2A publication feature flag is disabled."
            )

    @classmethod
    def propose(
        cls,
        owner_id: str,
        capability_name: str,
        capability_version: str,
        expires_in_seconds: Optional[int] = None,
    ) -> A2APublication:
        """
        Owner proposes a new capability. The publication starts in PROPOSED state.
        """
        cls._assert_flag_enabled()
        expires_at = (
            datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in_seconds)
            if expires_in_seconds
            else None
        )
        publication = A2APublication(
            owner_id=owner_id,
            capability_name=capability_name,
            capability_version=capability_version,
            expires_at=expires_at,
        )
        _PUBLICATION_STORE[publication.id] = publication
        return publication

    @classmethod
    def confirm(cls, publication_id: uuid.UUID, caller_id: str) -> A2APublication:
        """
        Owner confirms the proposal after any required internal review.
        """
        cls._assert_flag_enabled()
        publication = cls._get(publication_id)
        cls._assert_owner(publication, caller_id)

        if publication.state != "PROPOSED":
            raise InvalidTransition(
                f"Cannot confirm publication in state {publication.state}"
            )
        publication.transition("CONFIRMED")
        return publication

    @classmethod
    def publish(cls, publication_id: uuid.UUID, caller_id: str) -> A2APublication:
        """
        Publish the capability so that the A2A dispatcher can discover it.
        """
        cls._assert_flag_enabled()
        publication = cls._get(publication_id)
        cls._assert_owner(publication, caller_id)

        if publication.state not in ("CONFIRMED", "PROPOSED"):
            raise InvalidTransition(
                f"Cannot publish publication in state {publication.state}"
            )
        publication.transition("PUBLISHED")
        return publication

    @classmethod
    def withdraw(
        cls, publication_id: uuid.UUID, caller_id: str, revocation_reason: Optional[str] = None
    ) -> A2APublication:
        """
        Owner withdraws a previously published capability.
        """
        cls._assert_flag_enabled()
        publication = cls._get(publication_id)
        cls._assert_owner(publication, caller_id)

        if publication.state != "PUBLISHED":
            raise InvalidTransition(
                f"Cannot withdraw publication in state {publication.state}"
            )
        publication.transition("WITHDRAWN")
        publication.revocation_reason = revocation_reason
        return publication

    @classmethod
    def list_by_owner(cls, owner_id: str) -> List[A2APublication]:
        """
        Return all publications belonging to a given owner.
        """
        return [p for p in _PUBLICATION_STORE.values() if p.owner_id == owner_id]

    @classmethod
    def _get(cls, publication_id: uuid.UUID) -> A2APublication:
        try:
            return _PUBLICATION_STORE[publication_id]
        except KeyError:
            raise PublicationNotFound(f"Publication {publication_id} not found")
