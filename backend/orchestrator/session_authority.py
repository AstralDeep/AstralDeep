"""Private prerequisites for refreshing an operation's or web request's execution
session without granting work by themselves; callers such as work_publication.py and
persistent_agents/runner.py must still recheck authority inside their own Plane
transaction.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

from astralplane.repositories.assignment_models import AssignmentOperationRead, AssignmentRecord
from astralplane.repositories.history import SessionCredentialFence, SessionExecutionObservation
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

from orchestrator import auth, web_auth
from orchestrator.session_store import WebSessionStore

_SID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_TIME_LIMIT_SECONDS = 15


class SessionAuthorityUnavailable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OperationExecutionAuthority:
    record: AssignmentRecord = field(repr=False)
    observation: SessionExecutionObservation = field(repr=False)
    _claims_json: str = field(repr=False)
    subject_token: str = field(repr=False)
    plane_runtime: object = field(repr=False)

    @property
    def claims(self) -> dict:
        return json.loads(self._claims_json)


@dataclass(frozen=True, slots=True)
class _OperationRefresh:
    record: AssignmentRecord = field(repr=False)
    observation: SessionExecutionObservation = field(repr=False)
    claims_json: str = field(repr=False)
    subject_token: str = field(repr=False)
    plane_runtime: object = field(repr=False)


def _unavailable() -> None:
    raise SessionAuthorityUnavailable("session_authority_unavailable")


def _uuid4(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


def _operation_reference(value, owner_id, assignment_id):
    if not isinstance(value, AssignmentOperationRead) or value.continuation_supported is not True:
        _unavailable()
    record = value.assignment
    if (not isinstance(record, AssignmentRecord) or record.owner_id != owner_id
            or record.assignment_id != assignment_id
            or record.execution_profile != "one_shot" or not isinstance(record.operation, Mapping)):
        _unavailable()
    operation = record.operation
    authority = operation.get("authority")
    if (type(operation.get("version")) is not int or operation["version"] != 2
            or not isinstance(authority, Mapping) or authority.get("owner_id") != owner_id
            or authority.get("origin") != "interactive"
            or authority.get("reference_kind") != "session_incarnation"
            or not _uuid4(authority.get("reference_id"))):
        _unavailable()
    expiry = datetime.fromisoformat(authority["expires_at"])
    deadline = datetime.fromisoformat(operation["deadline_at"])
    if (expiry.utcoffset() is None or deadline.utcoffset() is None
            or min(expiry, deadline) <= datetime.now(timezone.utc)):
        _unavailable()
    return record, authority["reference_id"], expiry, deadline


def _operation_context(value, owner_id, assignment_id):
    selected = _operation_reference(value, owner_id, assignment_id)
    if selected[0].lifecycle != "active":
        _unavailable()
    return selected


def _same_operation(current, original):
    return replace(current, updated_at=original.updated_at) == original


async def _refresh_operation_session(
    *, owner_id: str, assignment_id: str, sessions: WebSessionStore, plane_runtime,
    operation_context, expected_record=None, request_expires_at=None, request_check=None,
    expected_session_credential: SessionCredentialFence | None = None,
) -> _OperationRefresh:
    try:
        async with asyncio.timeout(_TIME_LIMIT_SECONDS):
            if (os.getenv("USE_MOCK_AUTH", "").strip().lower() in {"true", "1", "yes"}
                    or not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 256
                    or not _uuid4(assignment_id) or not isinstance(sessions, WebSessionStore)
                    or sessions._sessions.plane_runtime is not plane_runtime):
                _unavailable()
            assignments = plane_runtime.repositories.assignments

            def read_original():
                with sessions._request_execution_transaction() as transaction:
                    return assignments.get_operation(transaction, owner_id=owner_id,
                                                     assignment_id=assignment_id)

            original, incarnation, expiry, deadline = operation_context(
                await asyncio.to_thread(read_original), owner_id, assignment_id)
            if expected_record is not None and not _same_operation(original, expected_record):
                _unavailable()
            if request_expires_at is not None:
                expiry = min(expiry, request_expires_at)
            reference = await asyncio.to_thread(sessions.capture_incarnation_execution_reference,
                owner_id=owner_id, incarnation_id=incarnation)
            if expected_session_credential is not None and (
                    type(expected_session_credential) is not SessionCredentialFence
                    or json.dumps(asdict(reference.state.credential), sort_keys=True, allow_nan=False)
                    != json.dumps(asdict(expected_session_credential), sort_keys=True, allow_nan=False)):
                _unavailable()
            if request_check is not None:
                request_check(reference.state.observed_at)
            if min(expiry, deadline) <= reference.state.observed_at:
                _unavailable()
            candidate = await sessions.refresh_for_execution(
                reference, exchange=web_auth._exchange_session_refresh,
                bound_exchange=web_auth._exchange_bound_session_refresh)
            payload = await auth.verify_user(await auth.verify_production_token(candidate.access_token))
            jwt_expiry = payload.get("exp")
            if (payload.get("sub") != owner_id or type(jwt_expiry) not in (int, float)
                    or not math.isfinite(jwt_expiry)):
                _unavailable()
            observation = SessionExecutionObservation(
                credential=candidate.credential, started_at=candidate.started_at,
                valid_until=min(candidate.started_at + timedelta(seconds=_TIME_LIMIT_SECONDS),
                    datetime.fromtimestamp(candidate.credential.hard_expires_at, timezone.utc),
                    datetime.fromtimestamp(jwt_expiry, timezone.utc), expiry, deadline))

            def final_check():
                with sessions._request_execution_transaction() as transaction:
                    repository = plane_runtime.repositories.history.sessions
                    repository.assert_current_execution(transaction, observation=observation)
                    current, _, _, _ = operation_context(assignments.get_operation(
                        transaction, owner_id=owner_id, assignment_id=assignment_id), owner_id, assignment_id)
                    if not _same_operation(current, original):
                        _unavailable()
                    state = repository.assert_current_execution(transaction, observation=observation)
                    if request_check is not None:
                        request_check(state.observed_at)

            await asyncio.to_thread(final_check)
            return _OperationRefresh(original, observation,
                json.dumps(payload, allow_nan=False, separators=(",", ":")),
                candidate.access_token, plane_runtime)
    except Exception:
        raise SessionAuthorityUnavailable("session_authority_unavailable") from None


async def refresh_operation_execution_authority(
    *, owner_id: str, assignment_id: str, sessions: WebSessionStore, plane_runtime,
) -> OperationExecutionAuthority:
    result = await _refresh_operation_session(owner_id=owner_id,
        assignment_id=assignment_id, sessions=sessions, plane_runtime=plane_runtime,
        operation_context=_operation_context)
    return OperationExecutionAuthority(result.record, result.observation,
        result.claims_json, result.subject_token, result.plane_runtime)


def _cookie_reference(request: Request) -> str:
    if (request.method == "OPTIONS" or "authorization" in request.headers
            or "token" in request.query_params
            or os.getenv("USE_MOCK_AUTH", "").strip().lower() in {"true", "1", "yes"}):
        _unavailable()
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
                reference, exchange=web_auth._exchange_session_refresh,
                bound_exchange=web_auth._exchange_bound_session_refresh)
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
        # from None: transport errors may carry sensitive diagnostics
        raise SessionAuthorityUnavailable("session_authority_unavailable") from None
