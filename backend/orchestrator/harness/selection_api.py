"""Harness owner APIs reuse current human authority and strict bounded JSON for CAS selection.
The orchestrator mounts this router; selection_service.py owns typed persistence and audit.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator.harness.production_profiles import HarnessSelectionError
from orchestrator.harness.selection_service import HarnessSelectionService
from orchestrator.human_request_authority import authenticate_current_human_request
from orchestrator.work_write_boundary import read_work_body, work_content_length
from persistent_agents.models import AssignmentError

harness_router = APIRouter(prefix="/api/harness", tags=["Harness"])


def _json(value, status=200):
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def _service(request):
    app = request.app
    orch = getattr(app.state, "orchestrator", None)
    if orch is None:
        orch = getattr(getattr(app, "_root_app", app).state, "orchestrator", None)
    service = getattr(orch, "harness_selection", None)
    if type(service) is not HarnessSelectionService or service.orch is not orch:
        raise HarnessSelectionError()
    return service


async def _caller(request, service):
    if request.query_params:
        raise HarnessSelectionError("harness_selection_invalid", 422)
    return await authenticate_current_human_request(request,
        boundary=getattr(service.orch, "human_request_boundary", None))


def _parse(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def invalid_constant(_):
        raise ValueError

    try:
        if len(raw) > 4096:
            raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=invalid_constant)
        if (type(value) is not dict or set(value) != {"harness_id", "expected_revision"}
                or (value["harness_id"] is not None and (type(value["harness_id"]) is not str
                    or not 1 <= len(value["harness_id"]) <= 64))
                or type(value["expected_revision"]) is not int
                or not 0 <= value["expected_revision"] <= 2**53 - 1):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise HarnessSelectionError("harness_selection_invalid", 422) from None


@harness_router.get("/profiles")
async def profiles(request: Request):
    try:
        service = _service(request)
        caller = await _caller(request, service)
        return _json(await service.status(caller))
    except AssignmentError as error:
        return _json({"error": error.code}, error.status_code)


@harness_router.post("/selection")
async def select_profile(request: Request):
    try:
        service = _service(request)
        caller = await _caller(request, service)
        raw = await read_work_body(request, work_content_length(request))
        value = _parse(raw)
        return _json(await service.select(caller, **value))
    except AssignmentError as error:
        return _json({"error": error.code}, error.status_code)
