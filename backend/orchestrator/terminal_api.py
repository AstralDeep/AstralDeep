"""Routes owner terminal reads and cleanup through the ordinary authorized tool dispatcher.
Execution uses the same governed tools and explicit isolated SSH profiles as human agent dispatch.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orchestrator.auth import get_web_or_bearer_user_payload, security, verify_user
from orchestrator import terminal_approvals
from orchestrator.session_store import WebSessionStore
from orchestrator.terminal_security import (
    AGENT_ID,
    TerminalError,
    assert_human_owner,
    assert_terminal_delivery,
    require_terminal_enabled,
    require_terminal_execution,
    terminal_capability,
)

terminal_router = APIRouter(prefix="/api/terminals", tags=["Terminals"])


class TerminalCreateRequest(BaseModel):
    cols: int = Field(default=80, ge=2, le=1000)
    rows: int = Field(default=24, ge=2, le=300)
    command: Optional[str] = Field(default=None, max_length=512)


class TerminalInputRequest(BaseModel):
    data_b64: str = Field(min_length=1, max_length=21848)


class TerminalResizeRequest(BaseModel):
    cols: int = Field(ge=2, le=1000)
    rows: int = Field(ge=2, le=300)


async def _owner_claims(payload: dict = Depends(get_web_or_bearer_user_payload)) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str) or not payload["sub"].strip():
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        assert_human_owner(payload, payload.get("sub"))
    except TerminalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
    await verify_user(payload)
    return payload


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    return orch


def _json(value: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def _refused(exc: TerminalError) -> JSONResponse:
    return _json({"error": exc.code}, exc.status_code)


async def _dispatch(request: Request, claims: dict, tool_name: str, arguments: dict, *,
                    unwrap_session: bool = False, fields: tuple[str, ...] | None = None):
    pending = None
    review_sockets = []
    try:
        require_terminal_enabled()
        orch = _orchestrator(request)
        execute = getattr(orch, "execute_authorized_tool", None)
        if not callable(execute):
            raise TerminalError("terminal_unavailable", 503)
        if tool_name in {"start_terminal", "terminal_write"}:
            await asyncio.to_thread(require_terminal_execution, getattr(orch, "terminal_sessions", None), claims["sub"])
        original = deepcopy(claims)
        owner_id = original["sub"]
        selection = getattr(request.state, "_authenticated_cookie_session", None)
        sessions = getattr(orch, "web_sessions", None)
        connections = getattr(orch, "_connection_contexts", None)
        scope_maps = (getattr(orch, "_conversation_scopes", None), getattr(orch, "_ws_active_chat", None),
                      getattr(orch, "_open_chrome_surface", None), getattr(orch, "_terminal_review_scoped", None),
                      (connections, dict(connections) if type(connections) is dict else {}))

        def current_selection():
            assert_human_owner(original, owner_id)
            if getattr(request.state, "_authenticated_cookie_session", None) != selection:
                raise TerminalError("terminal_authority_changed", 401)
            if selection is None:
                return
            if (type(selection) is not tuple or len(selection) != 2
                    or not all(isinstance(value, str) and value for value in selection)
                    or type(sessions) is not WebSessionStore
                    or getattr(orch, "web_sessions", None) is not sessions):
                raise TerminalError("terminal_authority_changed", 401)
            try:
                current = sessions.is_current_incarnation(
                    owner_id, session_id=selection[0], incarnation_id=selection[1],
                )
            except Exception:
                raise TerminalError("terminal_authority_unavailable", 503) from None
            if current is not True:
                raise TerminalError("terminal_authority_changed", 401)

        def pending_authority():
            current_selection()
            assert_terminal_delivery(orch, original, owner_id, tool_name)
            current_selection()
            if (pending is None or pending.status in {"pending", "running"}) and not any(
                    terminal_approvals.review_context_current(orch, socket, owner_id, registration, scope_maps)
                    for socket, registration in review_sockets):
                raise TerminalError("terminal_review_context_required", 409)

        try:
            if tool_name in {"start_terminal", "terminal_write"}:
                review_sockets = [(socket, registration) for socket, registration in list(
                    getattr(orch, "ui_sessions", {}).items()) if terminal_approvals.review_context_current(
                        orch, socket, owner_id, registration, scope_maps)]
                if not review_sockets:
                    raise TerminalError("terminal_review_context_required", 409)
                pending = terminal_approvals.PendingTerminalRequest(orch, owner_id, tool_name, pending_authority)
                with terminal_approvals.request_scope(pending):
                    response = await execute(
                        claims=claims, user_id=owner_id, agent_id=AGENT_ID,
                        tool_name=tool_name, arguments=arguments, channel="rest")
            else:
                response = await execute(
                    claims=claims, user_id=owner_id, agent_id=AGENT_ID,
                    tool_name=tool_name, arguments=arguments, channel="rest")
        except TerminalError:
            raise
        except Exception:
            raise TerminalError("terminal_dispatch_unavailable", 503) from None
        current = await get_web_or_bearer_user_payload(request, await security(request))
        assert_human_owner(current, owner_id)
        await verify_user(current)

        def delivery():
            current_selection()
            assert_human_owner(current, owner_id)
            assert_terminal_delivery(orch, original, owner_id, tool_name)
            current_selection()
            assert_human_owner(current, owner_id)

        await asyncio.to_thread(delivery)
        if response is None:
            raise TerminalError("terminal_unavailable", 503)
        if response.error:
            detail = response.error.get("data")
            if not isinstance(detail, dict):
                detail = {}
            code = detail.get("terminal_code") or response.error.get("code")
            if not isinstance(code, str) or not code.startswith("terminal_"):
                code = "terminal_dispatch_refused"
            status = detail.get("http_status", 403)
            if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
                status = 403
            raise TerminalError(code, status)
        if pending is not None and pending.proposal_id is not None:
            from orchestrator.remote_confirmation import is_approval_card_result

            if not is_approval_card_result(response.ui_components):
                raise TerminalError("terminal_invalid_result", 503)
            delivered = 0
            for socket, socket_claims in review_sockets:
                if not terminal_approvals.review_context_current(orch, socket, owner_id, socket_claims, scope_maps):
                    continue
                try:
                    assert_human_owner(socket_claims, owner_id)
                    await asyncio.to_thread(pending.assert_current)
                    sent = await orch.send_ui_render(socket, response.ui_components, target="chat")
                    if sent is not True:
                        continue
                    await asyncio.to_thread(pending.assert_current)
                    if not terminal_approvals.review_context_current(orch, socket, owner_id, socket_claims, scope_maps):
                        continue
                    assert_human_owner(socket_claims, owner_id)
                    delivered += 1
                except Exception:
                    continue
            if not delivered:
                pending.assert_current()
                pending.status, pending.error = "failed", "terminal_human_session_required"
                raise TerminalError("terminal_human_session_required", 409)
            return _json(await asyncio.to_thread(pending.public, owner_id), 202)
        data = response.result
        if not isinstance(data, dict):
            raise TerminalError("terminal_invalid_result", 503)
        if unwrap_session:
            data = data.get("session")
            if not isinstance(data, dict):
                raise TerminalError("terminal_invalid_result", 503)
        if fields is not None:
            if any(key not in data for key in fields):
                raise TerminalError("terminal_invalid_result", 503)
            data = {key: data[key] for key in fields}
        return _json(data)
    except TerminalError as exc:
        if pending is not None and pending.status == "pending":
            pending.status, pending.error = "failed", exc.code
        return _refused(exc)
    except BaseException:
        if pending is not None and pending.status == "pending":
            pending.status, pending.error = "failed", "terminal_dispatch_unavailable"
        raise


@terminal_router.get("/capabilities")
async def capabilities(request: Request, claims: dict = Depends(_owner_claims)):
    return _json(await asyncio.to_thread(
        terminal_capability, getattr(_orchestrator(request), "terminal_sessions", None), claims["sub"],
    ))


@terminal_router.post("")
async def create_session(request: Request, body: TerminalCreateRequest, claims: dict = Depends(_owner_claims)):
    return await _dispatch(request, claims, "start_terminal", body.model_dump(), unwrap_session=True)


@terminal_router.get("")
async def list_sessions(request: Request, claims: dict = Depends(_owner_claims)):
    return await _dispatch(request, claims, "list_terminals", {})


@terminal_router.get("/approvals/{proposal_id}")
async def approval_status(request: Request, proposal_id: str, claims: dict = Depends(_owner_claims)):
    try:
        pending = await asyncio.to_thread(
            terminal_approvals.get_request, _orchestrator(request), claims["sub"], proposal_id)
        await asyncio.to_thread(assert_terminal_delivery, pending.orch, claims, claims["sub"], pending.tool_name)
        return _json(await asyncio.to_thread(pending.public, claims["sub"]))
    except TerminalError as exc:
        return _refused(exc)


@terminal_router.post("/{session_id}/input")
async def write_input(request: Request, session_id: str, body: TerminalInputRequest,
                      claims: dict = Depends(_owner_claims)):
    return await _dispatch(request, claims, "terminal_write",
                           {"session_id": session_id, "data_b64": body.data_b64}, unwrap_session=True)


@terminal_router.post("/{session_id}/resize")
async def resize_session(request: Request, session_id: str, body: TerminalResizeRequest,
                         claims: dict = Depends(_owner_claims)):
    return await _dispatch(
        request, claims, "terminal_resize", {"session_id": session_id, "cols": body.cols, "rows": body.rows},
        unwrap_session=True,
    )


@terminal_router.post("/{session_id}/interrupt")
async def interrupt_session(request: Request, session_id: str, claims: dict = Depends(_owner_claims)):
    return await _dispatch(request, claims, "terminal_interrupt", {"session_id": session_id}, unwrap_session=True)


@terminal_router.get("/{session_id}/output")
async def read_output(request: Request, session_id: str, offset: int = Query(default=0, ge=0),
                      wait_seconds: float = Query(default=0.0, ge=0, le=25), claims: dict = Depends(_owner_claims)):
    return await _dispatch(
        request, claims, "terminal_read", {"session_id": session_id, "offset": offset, "wait_seconds": wait_seconds},
        fields=("session_id", "offset", "next_offset", "data_b64", "bytes", "truncated", "eof", "session"),
    )


@terminal_router.delete("/{session_id}")
async def close_session(request: Request, session_id: str, claims: dict = Depends(_owner_claims)):
    return await _dispatch(request, claims, "close_terminal", {"session_id": session_id})


__all__ = ["terminal_router"]
