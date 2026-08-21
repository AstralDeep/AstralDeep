"""REST router for scheduled jobs (feature 025, US5/T048).

Manages job definitions: list / inspect (+ run history) / create / pause /
resume / delete. Create enforces explicit consent, scope-bounding (consented
scopes ⊆ the user's current scopes), governance (per-user cap + interval
floor), and timezone-aware next-run computation.

NOTE: unattended *execution* (the scheduler loop + offline-grant mint +
delegated run) is gated OFF by default pending the T057 security review;
this router only manages job definitions and run history.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import UUID4, BaseModel, Field

from agentic_settings import SCHEDULE_MAX_ACTIVE_JOBS_PER_USER, SCHEDULE_MIN_INTERVAL_SECONDS
from orchestrator.auth import get_current_user_payload, require_user_id
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from orchestrator.tool_permissions import VALID_SCOPES as _CANONICAL_SCOPES
from audit.hooks import record_generic
from shared.feature_flags import flags

from .cron import ScheduleError, compute_next_run_ms
from .governance import GovernanceError, validate_new_job
from .store import ScheduleActionError, ScheduledJobStore

logger = logging.getLogger("Scheduler.API")

schedule_router = APIRouter(prefix="/api/schedule", tags=["Schedule"])

# Canonical scope vocabulary (six entries) — see scheduler/runner.py. The stale
# four-entry copy that used to live here rejected valid create requests naming
# tools:files or tools:execute with HTTP 400.
_VALID_SCOPES = set(_CANONICAL_SCOPES)


class ScheduleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=4000)
    schedule_kind: str
    schedule_expr: str
    timezone: str = "UTC"
    consented_scopes: List[str] = Field(default_factory=list)
    consent: bool = False
    agent_id: Optional[str] = None
    target_chat_id: Optional[str] = None


class ScheduleRunNowRequest(BaseModel):
    submission_id: UUID4


def _orch(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orch


def _store(request: Request) -> ScheduledJobStore:
    orch = _orch(request)
    injected = getattr(orch, "scheduled_job_store", None)
    if injected is not None:
        return injected
    source = plane_source_from_orchestrator(orch)
    return ScheduledJobStore(
        coordinator=orch.work_admission,
        plane_runtime=source.plane_runtime,
        plane_repositories=source.plane_repositories,
    )


def _rfc3339_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _action_error(error: ScheduleActionError):
    if error.code == "job_not_found":
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse(
        status_code=409,
        content={
            "error": error.code,
            "detail": "The scheduled action could not be accepted.",
        },
        headers={"Cache-Control": "no-store"},
    )


@schedule_router.get("")
async def list_jobs(request: Request, user_id: str = Depends(require_user_id)):
    store = _store(request)
    return {"jobs": await asyncio.to_thread(store.list_jobs, user_id)}


@schedule_router.get("/{job_id}")
async def get_job(job_id: str, request: Request, user_id: str = Depends(require_user_id)):
    store = _store(request)
    job = await asyncio.to_thread(store.get_job, user_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    runs = await asyncio.to_thread(store.list_runs, user_id, job_id)
    return {"job": job, "runs": runs}


@schedule_router.post("", status_code=status.HTTP_201_CREATED)
async def create_job(body: ScheduleCreateRequest, request: Request,
                     user_id: str = Depends(require_user_id),
                     payload: dict = Depends(get_current_user_payload)):
    if not body.consent:
        raise HTTPException(status_code=422, detail="explicit consent is required to schedule unattended work")

    orch = _orch(request)
    store = _store(request)

    # Scope-bounding: consented scopes can never exceed the user's CURRENT scopes.
    bad = [s for s in body.consented_scopes if s not in _VALID_SCOPES]
    if bad:
        raise HTTPException(status_code=400, detail=f"invalid scopes: {bad}")
    if body.agent_id and body.consented_scopes:
        current = orch.tool_permissions.get_agent_scopes(user_id, body.agent_id)
        exceeds = [s for s in body.consented_scopes if not current.get(s, False)]
        if exceeds:
            raise HTTPException(status_code=403,
                                detail=f"consented scopes exceed your current grants: {exceeds}")

    # Governance + schedule validation.
    try:
        active_job_count = await asyncio.to_thread(store.count_active, user_id)
        validate_new_job(
            active_job_count=active_job_count,
            max_active=SCHEDULE_MAX_ACTIVE_JOBS_PER_USER,
            schedule_kind=body.schedule_kind,
            schedule_expr=body.schedule_expr,
            min_interval_seconds=SCHEDULE_MIN_INTERVAL_SECONDS,
        )
        next_run = compute_next_run_ms(body.schedule_kind, body.schedule_expr,
                                       body.timezone, int(time.time() * 1000))
    except GovernanceError as ge:
        return JSONResponse(status_code=409 if ge.code == "job_cap_reached" else 400,
                            content={"error": ge.code, "detail": str(ge), **ge.extra})
    except ScheduleError as se:
        raise HTTPException(status_code=400, detail=str(se))

    job = await asyncio.to_thread(
        store.create_job,
        user_id,
        name=body.name,
        instruction=body.instruction,
        schedule_kind=body.schedule_kind,
        schedule_expr=body.schedule_expr,
        timezone=body.timezone,
        consented_scopes=body.consented_scopes,
        agent_id=body.agent_id,
        target_chat_id=body.target_chat_id,
        next_run_at=next_run,
        offline_grant_id=None,  # set by the consent-capture flow (T042)
    )
    await record_generic(claims=payload, event_class="schedule", action_type="schedule.create",
                         description=f"Created scheduled job '{body.name}'",
                         outputs_meta={"job_id": job["id"], "kind": body.schedule_kind})
    return job


@schedule_router.post("/{job_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
async def run_job_now(
    job_id: uuid.UUID,
    body: ScheduleRunNowRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    """Materialize one idempotent manual occurrence under scheduler authority."""

    if not flags.is_enabled("scheduler_execution"):
        return JSONResponse(
            status_code=409,
            content={
                "error": "scheduler_execution_disabled",
                "detail": "Scheduled execution is currently unavailable.",
            },
            headers={"Cache-Control": "no-store"},
        )

    orch = _orch(request)
    loop = getattr(orch, "_scheduler_loop", None)
    runner = getattr(loop, "runner", None)
    eligibility = getattr(runner, "assess_job", None)
    if not callable(eligibility):
        return JSONResponse(
            status_code=503,
            content={
                "error": "scheduler_unavailable",
                "detail": "Scheduled execution is not ready.",
            },
            headers={"Cache-Control": "no-store"},
        )

    store = _store(request)
    job_identity = str(job_id)
    if await asyncio.to_thread(store.get_job, user_id, job_identity) is None:
        raise HTTPException(status_code=404, detail="job not found")
    submission_id = uuid.UUID(str(body.submission_id))
    try:
        result = await asyncio.to_thread(
            store.materialize_run_now,
            user_id=user_id,
            job_id=job_identity,
            submission_id=submission_id,
            eligibility=eligibility,
        )
    except ScheduleActionError as exc:
        return _action_error(exc)

    if result.created:
        await record_generic(
            claims=payload,
            event_class="schedule",
            action_type="schedule.run_now",
            description="Queued scheduled job to run now",
            outputs_meta={
                "job_id": str(result.job_id),
                "occurrence_id": str(result.occurrence_id),
                "submission_id": str(submission_id),
            },
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "submission_id": str(submission_id),
            "occurrence_id": str(result.occurrence_id),
            "job_id": str(result.job_id),
            "scheduled_for": _rfc3339_z(result.scheduled_for),
            "state": result.state,
            "duplicate": not result.created,
        },
        headers={"Cache-Control": "no-store"},
    )


@schedule_router.post("/{job_id}/pause")
async def pause_job(job_id: str, request: Request, user_id: str = Depends(require_user_id),
                    payload: dict = Depends(get_current_user_payload)):
    store = _store(request)
    changed = await asyncio.to_thread(
        store.set_status_and_cancel_unstarted,
        user_id=user_id,
        job_id=job_id,
        status="paused",
        terminal_code="cancelled_job_paused",
    )
    if not changed:
        raise HTTPException(status_code=404, detail="job not found")
    await record_generic(claims=payload, event_class="schedule", action_type="schedule.pause",
                         description="Paused scheduled job", outputs_meta={"job_id": job_id})
    return {"job_id": job_id, "status": "paused"}


@schedule_router.post("/{job_id}/resume")
async def resume_job(job_id: str, request: Request, user_id: str = Depends(require_user_id),
                     payload: dict = Depends(get_current_user_payload)):
    store = _store(request)
    if not await asyncio.to_thread(store.set_status, user_id, job_id, "active"):
        raise HTTPException(status_code=404, detail="job not found")
    await record_generic(claims=payload, event_class="schedule", action_type="schedule.resume",
                         description="Resumed scheduled job", outputs_meta={"job_id": job_id})
    return {"job_id": job_id, "status": "active"}


@schedule_router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str, request: Request, user_id: str = Depends(require_user_id),
                     payload: dict = Depends(get_current_user_payload)):
    store = _store(request)
    changed = await asyncio.to_thread(
        store.set_status_and_cancel_unstarted,
        user_id=user_id,
        job_id=job_id,
        status="disabled",
        terminal_code="cancelled_job_deleted",
    )
    if not changed:
        raise HTTPException(status_code=404, detail="job not found")
    await record_generic(claims=payload, event_class="schedule", action_type="schedule.delete",
                         description="Deleted scheduled job", outputs_meta={"job_id": job_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
