"""Private request selection for unregistered Work acceptance, never dispatch.

Accepted receipts need current normal owner authentication, not a fresh execution
session. Only a new admission forces refresh of its original signed-cookie row.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re

from astralplane.repositories.history import SessionConsentObservation, SessionExecutionObservation
from fastapi import HTTPException, Request

from orchestrator import auth, web_auth
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


def _signed_selection(request):
    cookies = [part.strip() for header in request.headers.getlist("cookie")
               for part in header.split(";")]
    values = [part.split("=", 1)[1] for part in cookies
              if "=" in part and part.split("=", 1)[0].strip() == web_auth.COOKIE_NAME]
    if len(values) != 1 or len(values[0]) > 289:
        return None
    sid = web_auth._unsign(values[0])
    return sid if isinstance(sid, str) and _SID.fullmatch(sid) else None


@dataclass(frozen=True, slots=True)
class AuthenticatedWorkRequest:
    """Server-private normal IAM snapshot; no serialized caller can supply it."""

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
                or (now or datetime.now(timezone.utc)) >= self.principal_expires_at
                or os.getenv("USE_MOCK_AUTH", "").strip().lower() in {"true", "1", "yes"}):
            _refuse()


@dataclass(frozen=True, slots=True)
class WorkSubmissionAuthority:
    observation: SessionExecutionObservation = field(repr=False)
    _claims_json: str = field(repr=False)
    plane_runtime: object = field(repr=False)

    @property
    def claims(self):
        return json.loads(self._claims_json)


async def _authenticate_work_request(
    request: Request, *, sessions: WebSessionStore, plane_runtime, methods, read_only=False,
) -> tuple[AuthenticatedWorkRequest, str | None]:
    """Reuse ordinary IAM once on frozen transport for closed Work write adapters."""
    try:
        if (not isinstance(request, Request) or request.method not in methods
                or (read_only and methods != ("GET",))
                or not isinstance(sessions, WebSessionStore)
                or sessions._sessions.plane_runtime is not plane_runtime
                or os.getenv("USE_MOCK_AUTH", "").strip().lower() in {"true", "1", "yes"}):
            _refuse()
        snapshot = freeze_work_request(request)
        sid = _signed_selection(snapshot)
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
            json.dumps(claims, allow_nan=False, separators=(",", ":")), sid,
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
    """Capture one original caller issuance after ordinary IAM, without refresh.

    Shared by Work controls and independent metadata requests. A supplied but
    unselected cookie cannot downgrade into a bare-Bearer command.
    """
    supplied = [part.strip().split("=", 1)[0] for header in snapshot.headers.getlist("cookie")
                for part in header.split(";")]
    if web_auth.COOKIE_NAME in supplied and context.session_id is None:
        _refuse("work_authentication_required", 401)
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
    """Freeze a POST before normal IAM; accepted retry needs no new session."""
    context, _token = await _authenticate_work_request(
        request, sessions=sessions, plane_runtime=plane_runtime, methods=("POST",))
    return context


async def refresh_work_submission_authority(
    context: AuthenticatedWorkRequest, *, sessions: WebSessionStore,
) -> WorkSubmissionAuthority:
    """Refresh one captured generation once; no owner/latest or retry fallback."""
    try:
        async with asyncio.timeout(15):
            if (type(context) is not AuthenticatedWorkRequest
                    or not isinstance(sessions, WebSessionStore)
                    or sessions._sessions.plane_runtime is not context.plane_runtime
                    or context.session_id is None):
                _refuse()
            context.assert_current(context.plane_runtime)
            reference = await asyncio.to_thread(sessions.capture_execution_reference,
                owner_id=context.owner_id, session_id=context.session_id)
            context.assert_current(context.plane_runtime)
            # Cookie IAM already selected an issuance before verifying its JWT.
            # Bearer IAM resolves no cookie row: its first selection is this
            # post-receipt capture. Accepted replay consumes neither selection.
            if (context.cookie_session is not None and context.cookie_session != (
                    reference.state.credential.session_id, reference.state.credential.incarnation_id)):
                _refuse()
            candidate = await sessions.refresh_for_execution(reference, exchange=web_auth._exchange_session_refresh,
                bound_exchange=web_auth._exchange_bound_session_refresh)
            claims = await auth.verify_user(await auth.verify_production_token(candidate.access_token))
            expiry = _expiry(claims, context.owner_id)
            if candidate.credential.incarnation_id != reference.state.credential.incarnation_id:
                _refuse()
            observation = SessionExecutionObservation(candidate.credential, candidate.started_at,
                min(candidate.started_at + timedelta(seconds=15), expiry, context.principal_expires_at,
                    datetime.fromtimestamp(candidate.credential.hard_expires_at, timezone.utc)))
            await asyncio.to_thread(sessions.assert_execution_observation, observation)
            context.assert_current(context.plane_runtime)
            return WorkSubmissionAuthority(observation,
                json.dumps(claims, allow_nan=False, separators=(",", ":")), context.plane_runtime)
    except Exception:
        _refuse()
