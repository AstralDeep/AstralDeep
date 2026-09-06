"""Authenticated owner API for persistent assignment policy and controls."""
from __future__ import annotations

from functools import wraps

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from orchestrator.auth import get_current_user_payload, require_user_id

from .models import (
    ApprovalDecisionRequest,
    AssignmentError,
    ControlRequest,
    CreateAssignmentRequest,
    ReviseAssignmentRequest,
)
from .service import public_action, public_record, thaw

_NO_STORE = {"Cache-Control": "no-store"}
_OWNER = Depends(require_user_id)
_CLAIMS = Depends(get_current_user_payload)


def _json(content, status=200):
    return JSONResponse(content=content, status_code=status, headers=_NO_STORE)


class OwnerRoute(APIRoute):
    """Do not reflect source arguments, token-shaped input, or internal errors."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        @wraps(handler)
        async def safe_handler(request):
            try:
                response = await handler(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except AssignmentError as exc:
                return _json({"error": exc.code, "detail": "The assignment request could not be accepted."},
                             exc.status_code)
            except RequestValidationError:
                return _json({"error": "assignment_request_invalid", "detail": "Review the request fields."}, 422)
            except HTTPException as exc:
                return _json({"error": "assignment_authentication_required" if exc.status_code == 401
                              else "assignment_request_refused"}, exc.status_code)
        return safe_handler


assignment_router = APIRouter(prefix="/api/persistent-agents", tags=["Persistent agents"], route_class=OwnerRoute)
# Descriptive alias for composition callers.
persistent_agents_router = assignment_router


def _service(request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root = getattr(request.app, "_root_app", request.app)
        orch = getattr(root.state, "orchestrator", None)
    service = getattr(orch, "persistent_assignments", None)
    if service is None:
        raise AssignmentError("assignment_runtime_unavailable", 503)
    return service


def _control_result(result):
    if hasattr(result, "assignment"):
        return {"assignment": public_record(result.assignment), "applied": result.applied,
                "invalidated_action_ids": list(result.invalidated_action_ids),
                "begun_action_ids": list(result.begun_action_ids)}
    return {"assignment": public_record(result)}


@assignment_router.get("")
async def list_assignments(request: Request, limit: int = Query(50, ge=1, le=100),
                           after_id: str | None = None, owner_id: str = _OWNER,
                           claims: dict = _CLAIMS):
    records = await _service(request).list(owner_id, claims, limit=limit, after_id=after_id)
    return _json({"assignments": [public_record(record) for record in records],
                  "next_cursor": records[-1].assignment_id if len(records) == limit else None})


@assignment_router.post("", status_code=201)
async def create_assignment(body: CreateAssignmentRequest, request: Request,
                            owner_id: str = _OWNER,
                            claims: dict = _CLAIMS):
    record = await _service(request).create(owner_id, claims, body)
    return _json({"assignment": public_record(record)}, 201)


@assignment_router.get("/{assignment_id}")
async def get_assignment(assignment_id: str, request: Request, owner_id: str = _OWNER,
                         claims: dict = _CLAIMS):
    service = _service(request)
    record = await service.get(owner_id, claims, assignment_id)
    actions = await service.proposals(owner_id, claims, assignment_id)
    return _json({"assignment": public_record(record), "proposals": [public_action(action) for action in actions]})


@assignment_router.patch("/{assignment_id}")
async def revise_assignment(assignment_id: str, body: ReviseAssignmentRequest, request: Request,
                            owner_id: str = _OWNER,
                            claims: dict = _CLAIMS):
    return _json(_control_result(await _service(request).revise(owner_id, claims, assignment_id, body)))


@assignment_router.get("/{assignment_id}/activity")
async def assignment_activity(assignment_id: str, request: Request,
                              after_sequence: int = Query(0, ge=0, le=9_223_372_036_854_775_807),
                              limit: int = Query(100, ge=1, le=100), owner_id: str = _OWNER,
                              claims: dict = _CLAIMS):
    records = await _service(request).activity(owner_id, claims, assignment_id,
                                               after_sequence=after_sequence, limit=limit)
    return _json({"activity": thaw(records), "next_cursor": records[-1].sequence if len(records) == limit else None})


@assignment_router.get("/{assignment_id}/tasks")
async def assignment_tasks(assignment_id: str, request: Request, owner_id: str = _OWNER,
                           claims: dict = _CLAIMS):
    return _json({"tasks": thaw(await _service(request).tasks(owner_id, claims, assignment_id))})


@assignment_router.get("/{assignment_id}/actions")
async def assignment_actions(assignment_id: str, request: Request,
                             limit: int = Query(100, ge=1, le=100), after_id: str | None = None,
                             owner_id: str = _OWNER, claims: dict = _CLAIMS):
    records = await _service(request).actions(owner_id, claims, assignment_id,
                                              limit=limit, after_id=after_id)
    return _json({"actions": [public_action(record) for record in records],
                  "next_cursor": records[-1].action_id if len(records) == limit else None})


@assignment_router.get("/{assignment_id}/events")
async def assignment_events(assignment_id: str, request: Request,
                            limit: int = Query(100, ge=1, le=100), after_id: str | None = None,
                            owner_id: str = _OWNER, claims: dict = _CLAIMS):
    records = await _service(request).events(owner_id, claims, assignment_id,
                                             limit=limit, after_id=after_id)
    safe = [{key: getattr(record, key) for key in
             ("event_id", "identity_digest", "context_digest", "disposition", "result_digest")}
            for record in records]
    return _json({"events": safe,
                  "next_cursor": records[-1].event_id if len(records) == limit else None})


@assignment_router.post("/{assignment_id}/approvals/{action_id}/decision")
async def decide_assignment_action(assignment_id: str, action_id: str, body: ApprovalDecisionRequest,
                                   request: Request, owner_id: str = _OWNER,
                                   claims: dict = _CLAIMS):
    # This capability may only be attached by trusted server integration. A
    # bearer token or a caller-provided websocket identifier is not attendance.
    interaction = getattr(request.state, "persistent_assignment_interaction", None)
    result = await _service(request).decide(owner_id, claims, assignment_id, action_id, body,
                                           interaction=interaction)
    if hasattr(result, "intent"):
        data = public_action(result)
    else:
        raw = thaw(result)
        data = {key: raw[key] for key in ("state", "action_id", "assignment_id", "safe_error_code")
                if key in raw}
    return _json({"action": data}, 202 if data.get("state") in ("approved", "running", "reserved") else 200)


@assignment_router.post("/{assignment_id}/{command}")
async def control_assignment(assignment_id: str, command: str, body: ControlRequest, request: Request,
                             owner_id: str = _OWNER,
                             claims: dict = _CLAIMS):
    result = await _service(request).control(owner_id, claims, assignment_id, command, body)
    return _json(_control_result(result), 202 if command == "run-now" else 200)
