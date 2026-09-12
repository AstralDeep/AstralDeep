"""Synthetic consent selections; never used by product dispatch or IAM."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from astralplane.repositories.history import SessionConsentObservation, SessionCredentialFence
from orchestrator.session_consent import ConsentSession


def synthetic_consent(owner="owner", *, sid="consent-session", incarnation=None):
    """Typed fake for service policy tests whose stores are explicit doubles."""
    now = datetime.now(timezone.utc)
    fence = SessionCredentialFence(
        owner_id=owner, session_id=sid, incarnation_id=incarnation or str(uuid4()),
        created_at=int(now.timestamp()), interactive_anchor=int(now.timestamp()),
        hard_expires_at=int(now.timestamp()) + 3600, refresh_generation=int(now.timestamp()),
        encrypted_state_binding="a" * 64)
    return ConsentSession(SessionConsentObservation(fence, now, now + timedelta(seconds=15)))


def consent_from_store(sessions, owner, sid):
    """Capture an explicit real test session; no owner-latest or token search."""
    state = sessions.capture_execution_reference(owner_id=owner, session_id=sid).state
    return ConsentSession(SessionConsentObservation(
        state.credential, state.observed_at,
        min(state.observed_at + timedelta(seconds=15),
            datetime.fromtimestamp(state.credential.hard_expires_at, timezone.utc))))
