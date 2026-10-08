"""Owner-authenticated REST surface for the emergency stop: read truthful status,
engage one labeled local stop, re-verify responder acknowledgments, and resume
only explicitly with the current revision and the engaging owner's identity.
Mounted by orchestrator.py next to the other /api routers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orchestrator.auth import get_current_user_payload, verify_user
from orchestrator.emergency_stop import EmergencyStopRefused
from persistent_agents.models import AssignmentError

emergency_stop_router = APIRouter(prefix="/api/emergency-stop", tags=["Safety"])


class EmergencyStopRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=280)


class EmergencyResumeRequest(BaseModel):
    expected_revision: int = Field(ge=1, le=2**63 - 1)


async def _owner(claims: dict = Depends(get_current_user_payload)) -> tuple[str, dict]:
    owner_id = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(owner_id, str) or not owner_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if claims.get("act") or claims.get("machine_class"):
        raise HTTPException(status_code=403, detail="Emergency stop requires the owner's own session")
    await verify_user(claims)
    return owner_id, claims


def _coordinator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    coordinator = getattr(orch, "emergency_stop", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="Emergency stop is unavailable")
    return coordinator


def _json(value: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def _refused(exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "")
    if code == "emergency_stop_active":
        return _json({"error": code}, 423)
    if code in {"emergency_stop_not_engaged", "emergency_stop_stale_revision"}:
        return _json({"error": code}, 409)
    if code == "emergency_stop_resume_denied":
        return _json({"error": code}, 403)
    if code == "emergency_stop_invalid":
        return _json({"error": code}, 422)
    return _json({"error": "emergency_stop_unavailable"}, 503)


@emergency_stop_router.get("")
async def read_status(request: Request, owner: tuple[str, dict] = Depends(_owner)):
    owner_id, _claims = owner
    try:
        return _json(_coordinator(request).status(owner_id))
    except (AssignmentError, EmergencyStopRefused) as exc:
        return _refused(exc)


@emergency_stop_router.post("/stop")
async def engage_stop(body: EmergencyStopRequest, request: Request,
                      owner: tuple[str, dict] = Depends(_owner)):
    owner_id, claims = owner
    try:
        return _json(await _coordinator(request).engage(owner_id, reason=body.reason, claims=claims))
    except (AssignmentError, EmergencyStopRefused) as exc:
        return _refused(exc)


@emergency_stop_router.post("/resume")
async def resume_stop(body: EmergencyResumeRequest, request: Request,
                      owner: tuple[str, dict] = Depends(_owner)):
    owner_id, claims = owner
    try:
        return _json(await _coordinator(request).resume(
            owner_id, expected_revision=body.expected_revision,
            actor_id=owner_id, claims=claims))
    except (AssignmentError, EmergencyStopRefused) as exc:
        return _refused(exc)


@emergency_stop_router.post("/verify")
async def verify_stop(request: Request, owner: tuple[str, dict] = Depends(_owner)):
    owner_id, _claims = owner
    try:
        return _json(await _coordinator(request).verify(owner_id))
    except (AssignmentError, EmergencyStopRefused) as exc:
        return _refused(exc)
