"""Captures the authenticated caller for a new Work submission or control request
without granting dispatch, refreshing only on a genuinely new admission. Used by
work_submit.py, work_control_authority.py, and work_admission_api.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import re

from astralplane.repositories.history import SessionConsentObservation
from fastapi import HTTPException, Request

from orchestrator import auth
from orchestrator.session_store import SessionRefreshUnavailable, WebSessionStore
from orchestrator.work_write_boundary import freeze_work_request
from persistent_agents.models import AssignmentError

_SID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def _refuse(code="work_authority_unavailable", status=403):
    raise AssignmentError(code, status)


def _expiry(claims, owner=None):
    subject = claims.get("sub") if isinstance(claims, dict) else None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    if (not isinstance(subject, str) or not 1 <= len(subject) <= 256
            or (owner is not None and subject != owner)
            or type(expiry) not in (int, float) or not math.isfinite(expiry)
            or claims.get("act") or any(claims.get(key) for key in
                ("machine_class", "machine_turn_class", "_machine_turn", "delegated"))):
        _refuse()
    try:
        return datetime.fromtimestamp(expiry, timezone.utc)
    except (ValueError, OverflowError, OSError):
        _refuse()


SESSION_COOKIE = "astral_session"


def refuse_session_cookie(connection):
    supplied = [part.strip().split("=", 1)[0] for header in connection.headers.getlist("cookie")
                for part in header.split(";")]
    if SESSION_COOKIE in supplied:
        _refuse("work_authentication_required", 401)


@dataclass(frozen=True, slots=True)
class AuthenticatedWorkRequest:
    owner_id: str = field(repr=False)
    principal_expires_at: datetime = field(repr=False)
    _claims_json: str = field(repr=False)
    session_id: str | None = field(repr=False)
    cookie_session: tuple[str | None, str | None] | None = field(repr=False)
    plane_runtime: object = field(repr=False)

    @property
    def claims(self):
        return json.loads(self._claims_json)

    def assert_current(self, runtime, *, now=None):
        if (self.plane_runtime is not runtime
                or _expiry(self.claims, self.owner_id) != self.principal_expires_at
                or (now or datetime.now(timezone.utc)) >= self.principal_expires_at):
            _refuse()


async def _authenticate_work_request(
    request: Request, *, sessions: WebSessionStore, plane_runtime, methods, read_only=False,
) -> tuple[AuthenticatedWorkRequest, str | None]:
    try:
        if (not isinstance(request, Request) or request.method not in methods
                or (read_only and methods != ("GET",))
                or not isinstance(sessions, WebSessionStore)
                or sessions._sessions.plane_runtime is not plane_runtime):
            _refuse()
        snapshot = freeze_work_request(request)
        refuse_session_cookie(snapshot)
        credentials = await auth.security(snapshot)
        claims = await auth.verify_user(await auth.get_web_or_bearer_user_payload(snapshot, credentials))
        expiry = _expiry(claims)
        if read_only:
            if "token" in snapshot.query_params:
                _refuse("work_query_token_refused", 403)
        else:
            from orchestrator.work_api import _write_owner
            await _write_owner(snapshot, claims["sub"])
        context = AuthenticatedWorkRequest(claims["sub"], expiry,
            json.dumps(claims, allow_nan=False, separators=(",", ":")), None,
            getattr(snapshot.state, "_authenticated_cookie_session", None), plane_runtime)
        context.assert_current(plane_runtime)
        return context, getattr(snapshot.state, "delegation_subject_token", None)
    except AssignmentError:
        raise
    except HTTPException as exc:
        _refuse("work_authentication_required" if exc.status_code == 401 else "work_authority_unavailable",
                exc.status_code if exc.status_code in (401, 403) else 503)
    except Exception:
        _refuse("work_authentication_required", 401)


async def capture_human_caller(snapshot, *, context, sessions, until):
    refuse_session_cookie(snapshot)
    if context.session_id is None:
        return None
    try:
        reference = await asyncio.to_thread(sessions.capture_execution_reference,
            owner_id=context.owner_id, session_id=context.session_id)
    except SessionRefreshUnavailable:
        _refuse("work_authentication_required", 401)
    state = reference.state
    credential = state.credential
    if (credential.owner_id != context.owner_id or credential.session_id != context.session_id
            or (context.cookie_session is not None and context.cookie_session != (
                credential.session_id, credential.incarnation_id))):
        _refuse("work_authentication_required", 401)
    return SessionConsentObservation(credential, state.observed_at,
        min(state.observed_at + timedelta(seconds=15), until, context.principal_expires_at,
            datetime.fromtimestamp(credential.hard_expires_at, timezone.utc)))


async def authenticate_work_submission_request(
    request: Request, *, sessions: WebSessionStore, plane_runtime,
) -> AuthenticatedWorkRequest:
    context, _token = await _authenticate_work_request(
        request, sessions=sessions, plane_runtime=plane_runtime, methods=("POST",))
    return context
