"""REST router for dreaming status/enable/disable/manual-trigger/review, backed by
dreaming/consolidation.py and dreaming/scheduling.py. The manual trigger runs a
synchronous sweep since it touches no external systems.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.auth import get_current_user_payload, require_user_id
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from audit.hooks import record_generic
from personalization.phi_gate import get_phi_gate
from .consolidation import run_sweep

logger = logging.getLogger("Dreaming.API")

dreaming_router = APIRouter(prefix="/api/dreaming", tags=["Dreaming"])


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orch


def _service(request: Request):
    svc = getattr(_orchestrator(request), "personalization_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Personalization subsystem not initialized")
    return svc


@dreaming_router.get("")
async def get_status(request: Request, user_id: str = Depends(require_user_id)):
    svc = _service(request)
    profile = svc.repo.get_profile(user_id)
    enabled = bool(profile.get("dreaming_enabled", True)) if profile else True
    try:
        from .scheduling import ensure_dreaming_job, remove_dreaming_job
        plane_source = plane_source_from_orchestrator(_orchestrator(request))
        if enabled:
            ensure_dreaming_job(plane_source, user_id)
        else:
            remove_dreaming_job(plane_source, user_id)
    except Exception:
        logger.debug("dreaming: job reconcile on status failed (non-fatal)", exc_info=True)
    return {"enabled": enabled, "recent_sweeps": svc.repo.list_sweeps(user_id)}


@dreaming_router.post("/enable")
async def enable(request: Request, user_id: str = Depends(require_user_id),
                 payload: dict = Depends(get_current_user_payload)):
    svc = _service(request)
    svc.repo.set_dreaming_enabled(user_id, True)
    try:
        from .scheduling import ensure_dreaming_job
        ensure_dreaming_job(
            plane_source_from_orchestrator(_orchestrator(request)), user_id
        )
    except Exception:
        logger.debug("dreaming: ensure_dreaming_job failed (non-fatal)", exc_info=True)
    await record_generic(claims=payload, event_class="dreaming", action_type="dreaming.enable",
                         description="Enabled background consolidation")
    return {"enabled": True}


@dreaming_router.post("/disable")
async def disable(request: Request, user_id: str = Depends(require_user_id),
                  payload: dict = Depends(get_current_user_payload)):
    svc = _service(request)
    svc.repo.set_dreaming_enabled(user_id, False)
    try:
        from .scheduling import remove_dreaming_job
        remove_dreaming_job(
            plane_source_from_orchestrator(_orchestrator(request)), user_id
        )
    except Exception:
        logger.debug("dreaming: remove_dreaming_job failed (non-fatal)", exc_info=True)
    await record_generic(claims=payload, event_class="dreaming", action_type="dreaming.disable",
                         description="Disabled background consolidation")
    return {"enabled": False}


@dreaming_router.post("/trigger")
async def trigger(request: Request, user_id: str = Depends(require_user_id),
                  payload: dict = Depends(get_current_user_payload)):
    svc = _service(request)
    sweep = run_sweep(svc.repo, get_phi_gate(), user_id, trigger="manual")
    await record_generic(claims=payload, event_class="dreaming", action_type="dreaming.sweep",
                         description="Ran a manual consolidation sweep",
                         outputs_meta={"promoted": sweep["promoted_count"],
                                       "considered": sweep["candidates_considered"]})
    return sweep
