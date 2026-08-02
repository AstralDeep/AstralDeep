"""Host-test import shim for the worker-copied shared watch ticket contract."""

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
