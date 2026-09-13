"""Authorized Work read presentation through the shared Projection builder.

This registered adapter owns its complete render-and-send lifetime. Generic
surface callbacks must not deliver its snapshots after the caller guard exits.
Only closed read navigation is accepted; no command, token or owner comes from
params. Reads remain available without a configured model provider.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging

from fastapi import HTTPException

from orchestrator.work_service import _identity
from orchestrator.work_surface_authority import WorkSurfaceRead
from persistent_agents.models import AssignmentError
from shared.protocol import ChromeRender, ChromeSurface

TITLE = "Recent work"
HANDLERS = {}
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


async def _state(read, params):
    mode = params["mode"]
    state = {"mode": mode, "status": "ready"}
    if mode == "list":
        state["page"] = await read.service.list(read.owner_id, read.captured,
                                                after_id=params.get("after_id"))
    elif mode == "detail":
        state["operation"] = await read.service.get(read.owner_id, read.captured,
                                                   params["operation_id"])
    else:
        state.update(await read.service.result_view(read.owner_id, read.captured,
                                                   params["operation_id"]))
    return state


async def deliver(orch, websocket, user_id, params, request_generation, *, work_read=None):
    """Deliver one correlated Work read, refusing a changed caller or navigation."""
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
            # No await separates this final registration/composition check from
            # starting delivery. Clients also reject old request generations.
            read.assert_current()
            _note_open_surface(orch, websocket, "work")
            return bool(await orch._safe_send(websocket, frame))
    except (AssignmentError, HTTPException, ValueError, TypeError, AttributeError, TimeoutError):
        logger.info("Work view refused or unavailable")
        return False
    finally:
        if type(read) is WorkSurfaceRead:
            read.close()
