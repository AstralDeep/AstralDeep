"""
Data model for A2A capability publication lifecycle.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional

PublicationState = Literal[
    "PROPOSED", "CONFIRMED", "PUBLISHED", "WITHDRAWN", "EXPIRED", "REVOKED"
]


@dataclass
class A2APublication:
    """
    Represents a single A2A capability that an owner can propose, confirm,
    publish and later withdraw.
    """

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    owner_id: str  # Keycloak user id of the owner
    capability_name: str
    capability_version: str
    state: PublicationState = "PROPOSED"
    created_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    confirmed_at: Optional[datetime.datetime] = None
    published_at: Optional[datetime.datetime] = None
    withdrawn_at: Optional[datetime.datetime] = None
    expires_at: Optional[datetime.datetime] = None
    # Optional revocation reason
    revocation_reason: Optional[str] = None

    def transition(self, new_state: PublicationState) -> None:
        """
        Safely transition the publication to a new state, applying timestamps.
        """
        now = datetime.datetime.utcnow()
        if new_state == "CONFIRMED":
            self.confirmed_at = now
        elif new_state == "PUBLISHED":
            self.published_at = now
        elif new_state == "WITHDRAWN":
            self.withdrawn_at = now
        self.state = new_state

    def is_active(self) -> bool:
        """
        Returns True if the capability is currently published and not expired.
        """
        if self.state != "PUBLISHED":
            return False
        if self.expires_at and self.expires_at <= datetime.datetime.utcnow():
            self.state = "EXPIRED"
            return False
        return True
