"""Owner-authenticated REST and SSE reads plus lifecycle controls for Work operations,
composing work_service.py, work_controls.py, work_publication.py, and work_resume.py
behind one frozen per-request credential. Mounted by api.py.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import math
import os
import re
import time
from urllib.parse import urlsplit

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from orchestrator import auth
from orchestrator.auth import get_web_or_bearer_user_payload, verify_user
from orchestrator.work_controls import (
    WorkControlRequest, WorkControlService, WorkDecideRequest, WorkDeleteRequest,
)
from orchestrator.work_control_authority import WorkCallerAuthority, authenticate_work_control_request
from orchestrator.work_continuations import WorkContinuationService, WorkOwnerWaitRequest, WorkReconcileRequest
from orchestrator.work_publication import (
    WorkPublicationService, WorkResultProposalRequest, WorkResultSaveRequest,
)
from orchestrator.work_resume import WorkResumeService
from orchestrator.work_service import WorkService
from orchestrator.work_wake import WorkOwnerWakeRequest, WorkWakeService
from orchestrator.work_write_boundary import cache_work_write_body, freeze_work_request
from persistent_agents.models import AssignmentError

_CLAIMS = Depends(get_web_or_bearer_user_payload)


async def _read_owner(claims: dict = _CLAIMS):
    owner_id = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(owner_id, str) or not owner_id:
        raise HTTPException(401, "Not authenticated")
    await verify_user(claims)
    return owner_id


_OWNER = Depends(_read_owner)


def _origin(value, *, base=False):
    try:
        if not isinstance(value, str) or value != value.strip():
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or (parsed.path and not base)):
            raise ValueError
        port = parsed.port
        return parsed.scheme, parsed.hostname, port if port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise AssignmentError("work_origin_refused", 403) from exc


async def _write_owner(request: Request, owner_id: str = _OWNER):
    if "token" in request.query_params:
        raise AssignmentError("work_query_token_refused", 403)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise AssignmentError("work_json_required", 415)
    authorization = request.headers.get("authorization", "").split(None, 1)
    if len(authorization) == 2 and authorization[0].lower() == "bearer":
        return owner_id
    origins = request.headers.getlist("origin")
    if len(origins) != 1:
        raise AssignmentError("work_origin_refused", 403)
    public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL") or str(request.base_url)
    if _origin(origins[0]) != _origin(public_base, base=True):
        raise AssignmentError("work_origin_refused", 403)
    return owner_id


_WRITE_OWNER = Depends(_write_owner)


def _json(value, status=200, headers=None):
    return JSONResponse(value, status_code=status, headers={**(headers or {}), "Cache-Control": "no-store"})


def _read_expiry(claims):
    value = claims.get("exp") if isinstance(claims, dict) else None
    try:
        live = type(value) in (int, float) and math.isfinite(value) and time.time() < value
    except OverflowError:
        live = False
    if not live:
        raise HTTPException(401, "Not authenticated")
    return value


@dataclass(frozen=True, repr=False)
class _ReadDelivery:
    owner_id: str
    expires_at: float
    token: str = field(repr=False)
    cookie_session: tuple[str, str] | None = field(repr=False)

    @classmethod
    def capture(cls, request, owner_id, claims):
        token = getattr(request.state, "delegation_subject_token", None)
        cookie = getattr(request.state, "_authenticated_cookie_session", None)
        if (not isinstance(token, str) or not token or claims.get("sub") != owner_id
                or (cookie is not None and (type(cookie) is not tuple or len(cookie) != 2
                    or any(not isinstance(value, str) or not value for value in cookie)))):
            raise HTTPException(401, "Not authenticated")
        return cls(owner_id, _read_expiry(claims), token, cookie)

    async def verify(self, service):
        if time.time() >= self.expires_at:
            raise HTTPException(401, "Not authenticated")
        # Not ensure_session - that could silently refresh the token
        claims = await verify_user(await auth.verify_production_token(self.token))
        if claims.get("sub") != self.owner_id or _read_expiry(claims) != self.expires_at:
            raise HTTPException(401, "Not authenticated")
        cap = self.expires_at
        if self.cookie_session is not None:
            observed = await service.assert_read_session(self.owner_id, claims, self.cookie_session)
            if type(observed) is float and math.isfinite(observed):
                cap = min(cap, observed)
        service._owner(self.owner_id, claims)
        if time.time() >= self.expires_at:
            raise HTTPException(401, "Not authenticated")
        return cap


class WorkReadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        @wraps(handler)
        async def safe(request):
            original = request
            try:
                if request.method == "GET":
                    request = freeze_work_request(request)
                elif request.method in {"POST", "DELETE"}:
                    request = freeze_work_request(request)
                    assignments = _service(request).assignments
                    caller = await authenticate_work_control_request(request, assignments=assignments,
                        sessions=getattr(assignments.orch, "web_sessions", None))
                    async with asyncio.timeout_at(caller._deadline):
                        await cache_work_write_body(request)
                        request.state._work_control_caller = caller
                        request.state.audit_claims = caller.context.claims
                        return await handler(request)
                return await handler(request)
            except AssignmentError as exc:
                known = {
                    "work_not_found", "work_query_invalid", "work_repository_unavailable",
                    "work_read_unavailable", "assignment_feature_disabled",
                    "work_authentication_required",
                    "assignment_owner_required", "assignment_human_required",
                    "work_control_invalid", "work_control_unavailable", "work_origin_refused",
                    "work_query_token_refused", "work_json_required",
                    "assignment_revision_conflict",
                    "assignment_idempotency_conflict", "assignment_not_active",
                    "assignment_not_terminal", "assignment_action_uncertain",
                    "assignment_version_unsupported", "assignment_owner_retired",
                    "work_authority_unavailable", "work_research_profile_unavailable",
                    "work_research_budget_insufficient", "assignment_scope_changed",
                    "assignment_scope_revoked", "assignment_not_waiting",
                    "assignment_event_key_conflict", "assignment_event_revision_conflict",
                    "assignment_history_capacity_exhausted",
                    "work_body_invalid", "work_body_too_large", "work_body_timeout", "work_disconnected",
                    "assignment_publication_conflict", "assignment_publication_busy",
                    "assignment_guidance_changed", "assignment_precondition_changed",
                    "assignment_authorization_unavailable", "assignment_tool_unavailable",
                    "assignment_scope_unavailable", "assignment_source_not_read_only",
                    "work_result_unavailable", "work_proposal_expired",
                    "assignment_proposal_changed", "assignment_approval_invalid",
                    "assignment_deadline_exceeded",
                }
                unavailable = "work_read_unavailable" if request.method == "GET" else "work_control_unavailable"
                return _json({"error": exc.code if exc.code in known else unavailable}, exc.status_code)
            except RequestValidationError:
                return _json({"error": "work_query_invalid" if request.method == "GET" else "work_control_invalid"}, 422)
            except TimeoutError:
                return _json({"error": "work_control_unavailable"}, 503)
            except HTTPException as exc:
                headers = {key: value for key, value in (exc.headers or {}).items()
                           if key.lower() in {"location", "www-authenticate"}}
                return _json({"error": "work_authentication_required"}, exc.status_code, headers)
            finally:
                if request is not original:
                    claims = getattr(request.state, "audit_claims", None)
                    if isinstance(claims, dict):
                        original.state.audit_claims = deepcopy(claims)
        return safe


work_router = APIRouter(prefix="/work/v1/operations", tags=["Work"], route_class=WorkReadRoute)


def _service(request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root = getattr(request.app, "_root_app", request.app)
        orch = getattr(root.state, "orchestrator", None)
    backing = getattr(orch, "persistent_assignments", None)
    if backing is None:
        raise AssignmentError("work_read_unavailable", 503)
    return WorkService(backing)


def _query_integer(value, maximum=2**63 - 1):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise AssignmentError("work_query_invalid", 422)
    parsed = int(value)
    if parsed > maximum:
        raise AssignmentError("work_query_invalid", 422)
    return parsed


async def _read(request, owner_id, claims, method, **kwargs):
    service = _service(request)
    delivery = _ReadDelivery.capture(request, owner_id, claims)
    value = await getattr(service, method)(owner_id, claims, **kwargs)
    await delivery.verify(service)
    if _service(request).assignments is not service.assignments:
        raise AssignmentError("work_read_unavailable", 503)
    return value


@work_router.get("")
async def list_work(request: Request, limit: str = Query("50"), after_id: str | None = None,
                    owner_id: str = _OWNER, claims: dict = _CLAIMS):
    return _json(await _read(request, owner_id, claims, "list", limit=_query_integer(limit, 100), after_id=after_id))


@work_router.get("/{identity}")
async def get_work(identity: str, request: Request, owner_id: str = _OWNER, claims: dict = _CLAIMS):
    return _json({"operation": await _read(request, owner_id, claims, "get", identity=identity)})


@work_router.get("/{identity}/poll")
async def poll_work(identity: str, request: Request, after_revision: str | None = None,
                    owner_id: str = _OWNER, claims: dict = _CLAIMS):
    revision = None if after_revision is None else _query_integer(after_revision)
    return _json(await _read(request, owner_id, claims, "poll", identity=identity, after_revision=revision))


@work_router.get("/{identity}/measurements")
async def measurements_work(identity: str, request: Request, owner_id: str = _OWNER,
                            claims: dict = _CLAIMS):
    return _json({"measurements": await _read(request, owner_id, claims, "measurements",
                                              identity=identity)})


@work_router.get("/{identity}/result")
async def result_work(identity: str, request: Request, owner_id: str = _OWNER, claims: dict = _CLAIMS):
    return _json(await _read(request, owner_id, claims, "result", identity=identity))


SSE_INTERVAL_SECONDS = 2.0
SSE_MAX_SECONDS = 900
_SSE_ERRORS = frozenset({"work_not_found", "work_authentication_required", "work_read_unavailable"})


def _sse(event, data, revision=None):
    head = "" if revision is None else "id: %d\n" % revision
    body = json.dumps(data, allow_nan=False)
    return (head + "event: " + event + "\ndata: " + body + "\n\n").encode()


def _sse_failure(exc):
    if isinstance(exc, HTTPException):
        return "work_authentication_required"
    if isinstance(exc, AssignmentError) and exc.code in _SSE_ERRORS:
        return exc.code
    return "work_read_unavailable"


async def _events(service, owner_id, claims, delivery, identity, after_revision, credential_cap, bound):
    last = after_revision
    try:
        while True:
            try:
                result = await service.poll(owner_id, claims, identity, after_revision=last)
                credential_cap = min(credential_cap, await delivery.verify(service))
            except (AssignmentError, HTTPException) as exc:
                yield _sse("error", {"error": _sse_failure(exc)}, last)
                return
            if time.time() >= credential_cap:
                yield _sse("error", {"error": "work_authentication_required"}, last)
                return
            if result["changed"]:
                last = result["revision"]
                yield _sse("revision", result, last)
            else:
                yield b": tick\n\n"
            remaining = min(credential_cap, bound) - time.time()
            if remaining <= 0:
                if credential_cap <= bound:
                    yield _sse("error", {"error": "work_authentication_required"}, last)
                else:
                    yield _sse("end", {"reason": "work_stream_bounded", "revision": last}, last)
                return
            await asyncio.sleep(min(SSE_INTERVAL_SECONDS, remaining))
    except Exception:  # noqa: BLE001
        yield _sse("error", {"error": "work_read_unavailable"}, last)


@work_router.get("/{identity}/events")
async def events_work(identity: str, request: Request, after_revision: str | None = None,
                      max_seconds: str | None = None, owner_id: str = _OWNER, claims: dict = _CLAIMS):
    resume = request.headers.get("last-event-id")
    if resume is not None and resume.strip() == "":
        resume = None
    revision = None if resume is None else _query_integer(resume.strip())
    if revision is None and after_revision is not None:
        revision = _query_integer(after_revision)
    limit = SSE_MAX_SECONDS if max_seconds is None else _query_integer(max_seconds, SSE_MAX_SECONDS)
    service = _service(request)
    delivery = _ReadDelivery.capture(request, owner_id, claims)
    await service.get(owner_id, claims, identity)
    credential_cap = await delivery.verify(service)
    if _service(request).assignments is not service.assignments:
        raise AssignmentError("work_read_unavailable", 503)
    bound = time.time() + min(limit, SSE_MAX_SECONDS)
    return StreamingResponse(
        _events(service, owner_id, claims, delivery, identity, revision, credential_cap, bound),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@work_router.post("/{identity}/pause")
async def pause_work(identity: str, body: WorkControlRequest, request: Request):
    caller = _write_caller(request)
    return _json(await WorkControlService(_service(request).assignments).control(
        caller.context.owner_id, caller.context.claims, identity, "pause", body, caller=caller))


@work_router.post("/{identity}/cancel")
async def cancel_work(identity: str, body: WorkControlRequest, request: Request):
    caller = _write_caller(request)
    return _json(await WorkControlService(_service(request).assignments).control(
        caller.context.owner_id, caller.context.claims, identity, "cancel", body, caller=caller))


@work_router.delete("/{identity}")
async def delete_work(identity: str, body: WorkDeleteRequest, request: Request):
    caller = _write_caller(request)
    return _json(await WorkControlService(_service(request).assignments).delete(
        caller.context.owner_id, caller.context.claims, identity, body, caller=caller))


@work_router.post("/{identity}/resume")
async def resume_work(identity: str, body: WorkControlRequest, request: Request):
    return _json(await WorkResumeService(_service(request).assignments).resume(
        identity, body, caller=_write_caller(request)))


@work_router.post("/{identity}/wait")
async def wait_work(identity: str, body: WorkOwnerWaitRequest, request: Request):
    return _json(await WorkContinuationService(_service(request).assignments).wait(
        identity, body, caller=_write_caller(request)))


@work_router.post("/{identity}/actions/{action_id}/reconcile")
async def reconcile_work(identity: str, action_id: str, body: WorkReconcileRequest, request: Request):
    return _json(await WorkContinuationService(_service(request).assignments).reconcile(
        identity, action_id, body, caller=_write_caller(request)))


@work_router.post("/{identity}/actions/{action_id}/decide")
async def decide_work(identity: str, action_id: str, body: WorkDecideRequest, request: Request):
    return _json(await WorkControlService(_service(request).assignments).decide(
        identity, action_id, body, caller=_write_caller(request)))


@work_router.post("/{identity}/wake")
async def wake_work(identity: str, body: WorkOwnerWakeRequest, request: Request):
    return _json(await WorkWakeService(_service(request).assignments).wake(
        identity, body, caller=_write_caller(request)))


@work_router.post("/{identity}/result/proposals")
async def propose_result_work(identity: str, body: WorkResultProposalRequest, request: Request):
    return _json(await WorkPublicationService(_service(request).assignments).propose(
        identity, body, caller=_write_caller(request)))


@work_router.post("/{identity}/result/proposals/{submission_id}/save")
async def save_result_work(identity: str, submission_id: str, body: WorkResultSaveRequest, request: Request):
    return _json(await WorkPublicationService(_service(request).assignments).save(
        identity, submission_id, body, caller=_write_caller(request)))


def _write_caller(request):
    caller = getattr(request.state, "_work_control_caller", None)
    if type(caller) is not WorkCallerAuthority:
        raise AssignmentError("work_authentication_required", 401)
    return caller
