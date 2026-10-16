"""REST surface for owner-operated interactive terminal sessions, mounted by
orchestrator.py next to the other owner APIs; the same endpoints serve the SDK/CLI
bearer path and the web panel's cookie session. Access keeps the emergency-stop owner
boundary: the owner's own Keycloak identity only, with delegated (`act`) and machine
identities refused, and every terminal error returned as its truthful machine code.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orchestrator.auth import get_web_or_bearer_user_payload, verify_user
from orchestrator.terminal_sessions import TerminalError, TerminalSessionManager

terminal_router = APIRouter(prefix="/api/terminals", tags=["Terminals"])


class TerminalCreateRequest(BaseModel):
    cols: int = Field(default=80, ge=2, le=1000)
    rows: int = Field(default=24, ge=2, le=300)
    command: Optional[str] = Field(default=None, max_length=512)


class TerminalInputRequest(BaseModel):
    data_b64: str = Field(min_length=1)


class TerminalResizeRequest(BaseModel):
    cols: int = Field(ge=2, le=1000)
    rows: int = Field(ge=2, le=300)


class TerminalReadRequest(BaseModel):
    offset: int = Field(default=0, ge=0)
    wait_seconds: float = Field(default=0.0, ge=0, le=25)


async def _owner_id(request: Request,
                    payload: dict = Depends(get_web_or_bearer_user_payload)) -> str:
    user_id = payload.get("sub") if isinstance(payload, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if payload.get("act") or payload.get("machine_class"):
        raise HTTPException(status_code=403, detail="Terminal sessions require the owner's own session")
    await verify_user(payload)
    return user_id


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    return orch


def _manager(request: Request) -> TerminalSessionManager:
    manager = getattr(_orchestrator(request), "terminal_sessions", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Terminal sessions are unavailable")
    return manager


def _json(value: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def _refused(exc: TerminalError) -> JSONResponse:
    return _json({"error": exc.code}, exc.status_code)


@terminal_router.post("")
def create_session(request: Request, body: TerminalCreateRequest,
                   owner_id: str = Depends(_owner_id)):
    try:
        return _json(_manager(request).create(owner_id, cols=body.cols, rows=body.rows,
                                              command=body.command))
    except TerminalError as exc:
        return _refused(exc)


@terminal_router.get("")
def list_sessions(request: Request, owner_id: str = Depends(_owner_id)):
    return _json({"sessions": _manager(request).list_sessions(owner_id)})


@terminal_router.post("/{session_id}/input")
def write_input(request: Request, session_id: str, body: TerminalInputRequest,
                owner_id: str = Depends(_owner_id)):
    try:
        return _json(_manager(request).write(owner_id, session_id, body.data_b64))
    except TerminalError as exc:
        return _refused(exc)


@terminal_router.post("/{session_id}/resize")
def resize_session(request: Request, session_id: str, body: TerminalResizeRequest,
                   owner_id: str = Depends(_owner_id)):
    try:
        return _json(_manager(request).resize(owner_id, session_id, cols=body.cols, rows=body.rows))
    except TerminalError as exc:
        return _refused(exc)


@terminal_router.post("/{session_id}/interrupt")
def interrupt_session(request: Request, session_id: str,
                      owner_id: str = Depends(_owner_id)):
    try:
        return _json(_manager(request).interrupt(owner_id, session_id))
    except TerminalError as exc:
        return _refused(exc)


@terminal_router.get("/{session_id}/output")
def read_output(request: Request, session_id: str,
                offset: int = Query(default=0, ge=0),
                wait_seconds: float = Query(default=0.0, ge=0, le=25),
                owner_id: str = Depends(_owner_id)):
    try:
        return _json(_manager(request).read(owner_id, session_id, offset=offset,
                                            wait_seconds=wait_seconds))
    except TerminalError as exc:
        return _refused(exc)


@terminal_router.delete("/{session_id}")
def close_session(request: Request, session_id: str,
                  owner_id: str = Depends(_owner_id)):
    try:
        return _json(_manager(request).close(owner_id, session_id))
    except TerminalError as exc:
        return _refused(exc)


__all__ = ["terminal_router"]
