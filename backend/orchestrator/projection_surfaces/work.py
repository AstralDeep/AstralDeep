"""Delivers authorized Work-surface reads through the shared Projection builder and
handles the WS-driven Save-result mutation, both bound by the same
WorkCallerAuthority/WorkPublicationService boundary the HTTP work routes enforce.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
import os
import time
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from orchestrator.work_service import _identity
from orchestrator.work_surface_authority import WorkSurfaceRead
from persistent_agents.models import AssignmentError
from shared.protocol import ChromeRender, ChromeSurface

TITLE = "Recent work"
logger = logging.getLogger("Orchestrator.WorkSurface")


def _params(value):
    if not isinstance(value, dict):
        raise AssignmentError("work_query_invalid", 422)
    value = deepcopy(value)
    mode = value.get("mode", "list")
    allowed = {"mode", "after_id"} if mode == "list" else {"mode", "operation_id"}
    if mode not in {"list", "detail", "result"} or set(value) - allowed:
        raise AssignmentError("work_query_invalid", 422)
    if mode != "list":
        _identity(value.get("operation_id"))
    elif "after_id" in value:
        _identity(value["after_id"])
    return {**value, "mode": mode}


async def _conversation_title(orch, owner_id, conversation_id):
    if not conversation_id:
        return None

    def read(tx, _repository):
        history = orch.persistent_assignments.store.plane_runtime.repositories.history
        return history.conversations.get(tx, owner_id=owner_id, conversation_id=conversation_id)

    try:
        chat = await orch.persistent_assignments.store.transaction(read)
    except Exception:
        return None
    return getattr(chat, "title", None) if chat is not None else None


async def _save_bindings(orch, websocket, owner_id, row):
    chat_id = (getattr(orch, "_ws_active_chat", None) or {}).get(id(websocket), "")
    if not chat_id:
        return None

    def read(tx, _repository):
        history = orch.persistent_assignments.store.plane_runtime.repositories.history
        return history.conversations.get(tx, owner_id=owner_id, conversation_id=chat_id)

    try:
        chat = await orch.persistent_assignments.store.transaction(read)
    except Exception:
        return None
    if chat is None:
        return None
    return {
        "submission_id": str(uuid4()), "publication_id": str(uuid4()),
        "expected_revision": row["revision"], "conversation_id": chat_id,
        "conversation_title": chat.title or "",
        "expected_workspace_revision": chat.render_revision,
        "expected_workspace_publication_id": chat.publication_id,
    }


async def _augmented_result_state(service, orch, websocket, owner_id, claims, operation_id):
    state = {"mode": "result", "status": "ready"}
    state.update(await service.result_view(owner_id, claims, operation_id))
    if ((state.get("result") or {}).get("result") or {}).get("available") is True:
        state["save"] = await _save_bindings(orch, websocket, owner_id, state["operation"])
    return state


async def _state(read, params):
    mode = params["mode"]
    if mode == "list":
        return {"mode": mode, "status": "ready",
                "page": await read.service.list(read.owner_id, read.captured,
                                                after_id=params.get("after_id"))}
    if mode == "detail":
        return {"mode": mode, "status": "ready",
                "operation": await read.service.get(read.owner_id, read.captured,
                                                   params["operation_id"])}
    return await _augmented_result_state(read.service, read.orch, read.websocket,
                                         read.owner_id, read.captured, params["operation_id"])


async def deliver(orch, websocket, user_id, params, request_generation, *, work_read=None):
    from astralprojection.chrome import render_html
    from astralprojection.chrome.work import build_work_view
    from astralprojection.models import LayoutView
    from orchestrator.chrome_events import _device_type, _note_open_surface
    from rote.adapter import ComponentAdapter
    from shared.protocol import _require_uuid4
    from webrender.chrome import render_modal_shell

    read = work_read
    try:
        _require_uuid4(request_generation, "request_generation")
        if read is None:
            read = WorkSurfaceRead(orch, websocket, user_id)
        else:
            if type(read) is not WorkSurfaceRead or read.context is None or not read._session_captured:
                raise AssignmentError("work_authentication_required", 401)
            read.assert_request(orch, websocket, user_id, request_generation)
        device = _device_type(orch, websocket)
        if device != "browser" and "work_read_v1" not in read.captured.get("_client_capabilities", []):
            raise AssignmentError("work_read_unavailable", 503)
        async with asyncio.timeout_at(read.deadline):
            await read.authenticate()
            try:
                state = await _state(read, _params(params))
            except (AssignmentError, ValueError, TypeError, AttributeError):
                state = {"mode": "list", "status": "unavailable"}
            read.assert_current()
            view = build_work_view(state, layout=LayoutView(mode="watch" if device == "watch" else "standard"))
            if device == "browser":
                frame = ChromeRender(html=render_modal_shell(view.title, render_html(view), "work"),
                    surface_key="work", request_generation=request_generation).to_json()
            else:
                components = ComponentAdapter.adapt_work_surface(
                    [item.to_dict() for item in view.components], orch.rote.get_profile(websocket))
                frame = ChromeSurface(surface_key="work", title=view.title, components=components,
                                      request_generation=request_generation).to_json()
            await read.verify()
            # No await before send, so this check can't go stale
            read.assert_current()
            _note_open_surface(orch, websocket, "work")
            return bool(await orch._safe_send(websocket, frame))
    except (AssignmentError, HTTPException, ValueError, TypeError, AttributeError, TimeoutError):
        logger.info("Work view refused or unavailable")
        return False
    finally:
        if type(read) is WorkSurfaceRead:
            read.close()


class _WsPublicationBinding:
    __slots__ = ("orch", "assignments", "store", "runtime", "sessions")

    def __init__(self, orch, assignments):
        self.orch = orch
        self.assignments = assignments
        self.store = assignments.store
        self.runtime = self.store.plane_runtime
        self.sessions = getattr(orch, "web_sessions", None)

    def assert_current(self, assignments):
        if (assignments is not self.assignments or self.assignments.orch is not self.orch
                or getattr(self.orch, "persistent_assignments", None) is not self.assignments
                or self.assignments.store is not self.store
                or self.store.plane_runtime is not self.runtime
                or getattr(self.orch, "web_sessions", None) is not self.sessions):
            raise AssignmentError("work_control_unavailable", 503)


async def _ws_publication_caller(orch, websocket, user_id):
    import json

    from orchestrator import auth
    from orchestrator.session_store import WebSessionStore
    from orchestrator.work_api import _origin
    from orchestrator.work_control_authority import WorkCallerAuthority
    from orchestrator.work_submit_authority import (
        AuthenticatedWorkRequest, _expiry, _signed_selection, capture_human_caller,
    )
    from orchestrator.work_surface_authority import _transport_headers
    from persistent_agents.service import AssignmentService

    claims = (getattr(orch, "ui_sessions", None) or {}).get(websocket)
    if (websocket is None or getattr(websocket, "closed", False)
            or not isinstance(claims, dict) or claims.get("sub") != user_id
            or claims.get("act") or any(claims.get(key) for key in
                ("machine_class", "machine_turn_class", "_machine_turn", "delegated"))):
        raise AssignmentError("work_authentication_required", 401)
    token = claims.get("_raw_token")
    if not isinstance(token, str) or not token:
        raise AssignmentError("work_authentication_required", 401)
    assignments = getattr(orch, "persistent_assignments", None)
    if type(assignments) is not AssignmentService:
        raise AssignmentError("work_control_unavailable", 503)
    sessions = getattr(orch, "web_sessions", None)
    if type(sessions) is not WebSessionStore:
        raise AssignmentError("work_control_unavailable", 503)

    headers = _transport_headers(websocket)
    transport = HTTPConnection({"type": "websocket", "headers": headers})
    origins = transport.headers.getlist("origin")
    base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL")
    if not base or len(origins) != 1:
        raise AssignmentError("work_origin_refused", 403)
    if _origin(origins[0]) != _origin(base, base=True):
        raise AssignmentError("work_origin_refused", 403)

    session_id = _signed_selection(transport)
    verified = await auth.verify_user(await auth.verify_production_token(token))
    if verified.get("sub") != user_id:
        raise AssignmentError("work_authentication_required", 401)
    expiry = _expiry(verified, user_id)
    deadline = time.monotonic() + 15
    until = datetime.now(timezone.utc) + timedelta(seconds=15)
    context = AuthenticatedWorkRequest(
        user_id, expiry, json.dumps(verified, allow_nan=False, separators=(",", ":")),
        session_id, None, assignments.store.plane_runtime)
    context.assert_current(assignments.store.plane_runtime)
    caller = await capture_human_caller(transport, context=context, sessions=sessions, until=until)
    binding = _WsPublicationBinding(orch, assignments)
    guard = WorkCallerAuthority(context, caller, binding, token, deadline, until)
    await assignments.store.transaction(
        lambda tx, _repository: guard.assert_current(tx, assignments=assignments),
        bound_session_waits=True)
    guard._assert_local(assignments)
    return guard


async def render(orch, user_id, roles, params) -> str:
    from astralprojection.chrome import render_html
    from astralprojection.chrome.work import build_work_view

    state = params if isinstance(params, dict) else {"mode": "list", "status": "unavailable"}
    return render_html(build_work_view(state))


async def components(orch, user_id, roles, params):
    from astralprojection.chrome.work import build_work_view

    state = params if isinstance(params, dict) else {"mode": "list", "status": "unavailable"}
    return [item.to_dict() for item in build_work_view(state).components]


async def _handle_result_save(orch, websocket, user_id, roles, payload):
    from rote.work import validate_work_save_command
    from webrender.chrome import notice_block

    from orchestrator.work_publication import (
        WorkPublicationService, WorkResultProposalRequest, WorkResultSaveRequest,
    )
    from orchestrator.work_service import WorkService

    payload = payload if isinstance(payload, dict) else {}
    try:
        validate_work_save_command(payload)
    except ValueError:
        return None
    operation_id = payload.get("operation_id")
    command = payload.get("command")
    message = "The save outcome could not be confirmed. Reload the result before retrying."
    try:
        caller = await _ws_publication_caller(orch, websocket, user_id)
        assignments = orch.persistent_assignments
        service = WorkPublicationService(assignments)
        claims = caller.context.claims
        if command == "propose":
            body = WorkResultProposalRequest(
                version=1, submission_id=payload["submission_id"],
                expected_revision=payload["expected_revision"],
                publication_id=payload["publication_id"],
                conversation_id=payload["conversation_id"],
                expected_workspace_revision=payload["expected_workspace_revision"],
                expected_workspace_publication_id=payload["expected_workspace_publication_id"])
            review = await service.propose(operation_id, body, caller=caller)
            row = await WorkService(assignments).get(user_id, claims, operation_id)
            conversation_title = await _conversation_title(orch, user_id, payload["conversation_id"])
            state = {"mode": "review", "status": "ready", "operation": row, "review": review,
                     "approve": {"submission_id": str(uuid4()), "expired": False},
                     "conversation_title": conversation_title}
            return "work", state, ""
        body = WorkResultSaveRequest(
            version=1, submission_id=payload["submission_id"],
            expected_revision=payload["expected_revision"],
            proposal_digest=payload["proposal_digest"])
        await service.save(operation_id, payload["action_id"], body, caller=caller)
        state = await _augmented_result_state(
            WorkService(assignments), orch, websocket, user_id, claims, operation_id)
        return "work", state, notice_block("success", "Result saved.")
    except AssignmentError as exc:
        message = f"Save request refused ({exc.code}). Reload the result and try again."
    except (KeyError, TypeError, ValueError):
        message = "Invalid save request. Reload the result and try again."
    except Exception:
        logger.exception("work: result-save action failed")
    try:
        fallback_claims = (getattr(orch, "ui_sessions", None) or {}).get(websocket) or {}
        state = await _augmented_result_state(
            WorkService(orch.persistent_assignments), orch, websocket, user_id,
            fallback_claims, operation_id)
    except Exception:
        state = {"mode": "list", "status": "unavailable"}
    return "work", state, notice_block("error", message)


HANDLERS = {
    "chrome_work_result_save": _handle_result_save,
}
