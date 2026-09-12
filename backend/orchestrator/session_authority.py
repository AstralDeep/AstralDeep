"""088 request-local web authority prerequisite; no route or runner activation.

Only a private signed-cookie reference selects the session. This helper must be
called after normal authentication; future write callers must retain their origin
and CSRF gates. Its version-2 credential fence retains the initially observed
issued incarnation through refresh and normal IAM verification. It grants no
work by itself; committing Plane operations must recheck that exact observation.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from datetime import datetime, timedelta, timezone

from astralplane.repositories.history import SessionExecutionObservation
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

from orchestrator import auth, web_auth
from orchestrator.session_store import WebSessionStore

_SID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_TIME_LIMIT_SECONDS = 15


class SessionAuthorityUnavailable(Exception):
    """Closed, data-free refusal; never evidence that an issued permit was unused."""


def _unavailable() -> None:
    raise SessionAuthorityUnavailable("session_authority_unavailable")


def _cookie_reference(request: Request) -> str:
    if (request.method == "OPTIONS" or "authorization" in request.headers
            or "token" in request.query_params
            or os.getenv("USE_MOCK_AUTH", "").strip().lower() in {"true", "1", "yes"}):
        _unavailable()
    # Ambiguous duplicate cookies must not let parser ordering select authority.
    cookies = [part.strip() for header in request.headers.getlist("cookie")
               for part in header.split(";")]
    selected = [part.split("=", 1)[1] for part in cookies
                if "=" in part and part.split("=", 1)[0].strip() == web_auth.COOKIE_NAME]
    if len(selected) != 1 or len(selected[0]) > 289:
        _unavailable()
    sid = web_auth._unsign(selected[0])
    if not isinstance(sid, str) or _SID.fullmatch(sid) is None:
        _unavailable()
    return sid


async def refresh_web_execution_authority(
    request: Request, *, principal: dict,
) -> SessionExecutionObservation:
    """Force refresh, verify normal IAM, then fence the exact persisted generation.

    No token/claims are returned or added to the caller's request state. Remote
    calls run outside Plane transactions. The returned observation is bounded by
    the original database-clock sample and must be checked again in the mutation
    transaction. Missing authority must never bypass authentic permit settlement.

    Plane caps each request-only SQL statement/lock wait, so cancellation of a
    thread await does not leave ordinary database contention unbounded. Pool
    checkout (default 30 seconds), connection establishment (default 10 seconds),
    and server/network failures are separate: this coroutine's 15-second timeout
    is not a physical worker-termination guarantee.
    """
    try:
        async with asyncio.timeout(_TIME_LIMIT_SECONDS):
            sid = _cookie_reference(request)
            owner = principal.get("sub") if isinstance(principal, dict) else None
            if not isinstance(owner, str) or not owner or len(owner) > 256:
                _unavailable()
            store = web_auth._get_store()
            if not isinstance(store, WebSessionStore):
                _unavailable()
            reference = await asyncio.to_thread(
                store.capture_execution_reference, owner_id=owner, session_id=sid)
            candidate = await store.refresh_for_execution(
                reference, exchange=web_auth._exchange_session_refresh)
            # Reuse the actual IAM verifier without altering the original request's
            # delegation subject token/audit claims, including on a late refusal.
            verification_request = Request({**request.scope, "state": {}})
            payload = await auth.get_current_user_payload(
                verification_request, HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=candidate.access_token))
            payload = await auth.verify_user(payload)
            if not payload or payload.get("sub") != owner or _cookie_reference(request) != sid:
                _unavailable()
            expiry = payload.get("exp")
            if type(expiry) not in (int, float) or not math.isfinite(expiry):
                _unavailable()
            observation = SessionExecutionObservation(
                credential=candidate.credential, started_at=candidate.started_at,
                valid_until=min(candidate.started_at + timedelta(seconds=_TIME_LIMIT_SECONDS),
                                datetime.fromtimestamp(candidate.credential.hard_expires_at, timezone.utc),
                                datetime.fromtimestamp(expiry, timezone.utc)))
            await asyncio.to_thread(store.assert_execution_observation, observation)
            return observation
    except Exception:
        # Network/JWT/repository errors can carry sensitive transport diagnostics.
        # Cancellation is BaseException and propagates, with no authority returned.
        raise SessionAuthorityUnavailable("session_authority_unavailable") from None
