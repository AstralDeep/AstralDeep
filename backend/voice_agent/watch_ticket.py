"""Host-tree re-export of backend/shared/watch_ticket.py's ticket contract, imported by
watch_bridge.py and its tests so both trees share one canonical ticket
implementation.
"""

from shared.watch_ticket import (
    WatchTicketClaims,
    WatchTicketError,
    derive_watch_nonce,
    issue_watch_ticket,
    verify_watch_ticket,
    watch_participant_identity,
)

__all__ = [
    "WatchTicketClaims",
    "WatchTicketError",
    "derive_watch_nonce",
    "issue_watch_ticket",
    "verify_watch_ticket",
    "watch_participant_identity",
]
