"""
REST API for the audit log (feature 003-agent-audit-log).

Endpoints:

* ``GET /api/audit`` — list the authenticated user's audit entries with
  optional filters and cursor pagination.
* ``GET /api/audit/{event_id}`` — fetch one entry by id, scoped to the
  authenticated user.

Per FR-007 / FR-019 the API NEVER accepts an external ``actor_user_id``
or any equivalent path/query parameter. The owning user is exclusively
derived from the validated JWT (``require_user_id``). Every successful
list call also produces an ``audit_view`` audit event in the caller's
own log (closing the AU-2 / AU-12 loop).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from orchestrator.auth import require_user_id, get_current_user_payload

from .recorder import get_recorder, make_correlation_id, now_utc
from .schemas import AuditEventCreate, AuditEventDTO, EVENT_CLASSES, OUTCOMES

logger = logging.getLogger("Audit.API")

audit_router = APIRouter(prefix="/api/audit", tags=["Audit"])


_FORBIDDEN_QUERY_PARAMS = frozenset({
    "actor_user_id", "user_id", "user", "sub", "as_user", "owner_id",
})


class AuditListResponse(BaseModel):
    items: List[AuditEventDTO]
    next_cursor: Optional[str]
    filters_echo: dict


def _get_orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orch


def _availability_resolver(orch, user_id: str):
    """Return a callable that checks whether an artifact pointer still resolves.

    For artifacts in the ``user_attachments`` store the resolver consults
    Plane's owner-scoped attachment repository for a non-deleted row; for unknown stores the
    pointer is reported as available by default (FR-017 only requires
    that *known* stores be checked — unknown integrations are opaque).
    """
    from orchestrator.attachments.repository import AttachmentRepository
    from orchestrator.plane_repository_context import plane_source_from_orchestrator

    injected = getattr(orch, "attachment_repository", None)
    attachments = injected or AttachmentRepository.from_plane_source(
        plane_source_from_orchestrator(orch)
    )

    def resolve(pointer: dict) -> bool:
        store = (pointer or {}).get("store")
        artifact_id = (pointer or {}).get("artifact_id")
        if not store or not artifact_id:
            return True
        if store == "user_attachments":
            try:
                return attachments.get_by_id(str(artifact_id), user_id) is not None
            except Exception:
                return True
        return True
    return resolve


def _reject_forbidden_params(request: Request) -> None:
    """Reject any forbidden query/body parameter that names another user.

    Defends FR-007 / FR-019 even against future refactors that might
    accidentally bind such a parameter.
    """
    bad = [k for k in request.query_params.keys() if k.lower() in _FORBIDDEN_QUERY_PARAMS]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"audit API does not accept {bad[0]!r}; results are always scoped to the authenticated user",
        )


@audit_router.get(
    "",
    response_model=AuditListResponse,
    summary="List the authenticated user's audit entries",
    description=(
        "Returns the user's audit entries in reverse chronological order. "
        "Filtering by event_class, outcome, date range, and keyword is "
        "supported; pagination is cursor-based. The owning user is "
        "derived from the JWT — no parameter accepts another user's id."
    ),
)
async def list_audit(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = None,
    event_class: List[str] = Query(default_factory=list),
    outcome: List[str] = Query(default_factory=list),
    from_ts: Optional[datetime] = Query(default=None, alias="from"),
    to_ts: Optional[datetime] = Query(default=None, alias="to"),
    q: Optional[str] = None,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    _reject_forbidden_params(request)

    for ec in event_class:
        if ec not in EVENT_CLASSES:
            raise HTTPException(status_code=400, detail=f"unknown event_class: {ec!r}")
    for oc in outcome:
        if oc not in OUTCOMES:
            raise HTTPException(status_code=400, detail=f"unknown outcome: {oc!r}")

    orch = _get_orchestrator(request)
    repo = orch.audit_repo  # set during orchestrator startup
    recorder = get_recorder()

    try:
        # The audit query invokes the availability resolver synchronously for
        # every pointer in its bounded page. Keep the complete query + READY
        # evaluation on one worker so neither PostgreSQL call path touches the
        # ASGI event loop and the repository retains one coherent evaluation.
        items, next_cursor = await asyncio.to_thread(
            repo.list_for_user,
            user_id,
            limit=limit,
            cursor=cursor,
            event_classes=event_class or None,
            outcomes=outcome or None,
            from_ts=from_ts,
            to_ts=to_ts,
            keyword=q,
            availability_resolver=_availability_resolver(orch, user_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Self-record this read (AU-2 / AU-12). Never let recording failure
    # break the read itself.
    if recorder is not None:
        try:
            await recorder.record(AuditEventCreate(
                actor_user_id=user_id,
                auth_principal=payload.get("sub", user_id),
                event_class="audit_view",
                action_type="audit_view.list",
                description="Viewed audit log list",
                correlation_id=make_correlation_id(),
                outcome="success",
                inputs_meta={
                    "limit": limit,
                    "filters": {
                        "event_class": list(event_class),
                        "outcome": list(outcome),
                        "from": from_ts.isoformat() if from_ts else None,
                        "to": to_ts.isoformat() if to_ts else None,
                        "has_q": bool(q),
                        "has_cursor": bool(cursor),
                    },
                },
                outputs_meta={"returned_count": len(items)},
                started_at=now_utc(),
            ))
        except Exception as exc:  # pragma: no cover
            logger.debug("audit_view self-record failed: %s", exc)

    return AuditListResponse(
        items=items,
        next_cursor=next_cursor,
        filters_echo={
            "limit": limit,
            "event_class": list(event_class),
            "outcome": list(outcome),
            "from": from_ts.isoformat() if from_ts else None,
            "to": to_ts.isoformat() if to_ts else None,
            "q": q,
        },
    )


@audit_router.get(
    "/{event_id}",
    response_model=AuditEventDTO,
    summary="Fetch a single audit entry by id",
    description=(
        "Returns the audit entry identified by ``event_id`` if it belongs "
        "to the authenticated user. Returns 404 for both non-existent "
        "ids and ids belonging to other users — the indistinguishability "
        "is intentional (FR-007 / FR-019)."
    ),
    responses={404: {"description": "Not found, or not owned by the caller"}},
)
async def get_audit_event(
    request: Request,
    event_id: str,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    _reject_forbidden_params(request)
    orch = _get_orchestrator(request)
    repo = orch.audit_repo
    recorder = get_recorder()

    try:
        uuid.UUID(event_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="not found")

    dto = repo.get_for_user(
        user_id, event_id,
        availability_resolver=_availability_resolver(orch, user_id),
    )
    if dto is None:
        raise HTTPException(status_code=404, detail="not found")

    if recorder is not None:
        try:
            await recorder.record(AuditEventCreate(
                actor_user_id=user_id,
                auth_principal=payload.get("sub", user_id),
                event_class="audit_view",
                action_type="audit_view.detail",
                description=f"Viewed audit detail {event_id}",
                correlation_id=make_correlation_id(),
                outcome="success",
                inputs_meta={"event_id": event_id},
                started_at=now_utc(),
            ))
        except Exception as exc:  # pragma: no cover
            logger.debug("audit_view detail self-record failed: %s", exc)

    return dto


# ---------------------------------------------------------------------------
# Feature 016 — Persistent-login: session-resume-failed audit endpoint
# ---------------------------------------------------------------------------

class SessionResumeFailedBody(BaseModel):
    """Body of POST /api/audit/session-resume-failed.

    Posted by the frontend (a) after the FR-011 retry budget is
    exhausted on the silent-resume path, (b) when the FR-013 hard-max
    365-day clear path discards a stored credential, and (c) when the
    FR-007 deployment-origin check fails. In all three cases the WS is
    not yet authenticated, so the audit row cannot be written via the
    normal WS-register path.
    """
    reason: Literal[
        "retry-budget-exhausted",
        "definitive-4xx",
        "token-expired",
        "deployment-mismatch",
    ]
    attempts: int = Field(default=0, ge=0, le=3)
    last_error: str = Field(default="", max_length=500)


@audit_router.post(
    "/session-resume-failed",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record an auth.session_resume_failed audit event",
    description=(
        "Records `event_class='auth', action_type='auth.session_resume_failed'`."
        " Accepts an unauthenticated body — when no bearer token is present, the"
        " row is attributed to `actor_user_id='anonymous'`. Used by the frontend"
        " after the silent-resume retry budget is exhausted, after a hard-max"
        " 365-day clear, or after a deployment-mismatch clear."
    ),
    responses={
        204: {"description": "Recorded"},
        400: {"description": "Malformed body"},
    },
)
async def post_session_resume_failed(
    request: Request,
    body: SessionResumeFailedBody,
):
    # Best-effort identity attribution. The caller's bearer token is
    # almost certainly invalid (that is *why* the resume failed), so we
    # do NOT cryptographically validate it — we only decode the payload
    # claims to attribute the audit row. If decoding fails the row is
    # attributed to "anonymous".
    actor_user_id = "anonymous"
    auth_principal = "anonymous"
    try:
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        if auth_header.lower().startswith("bearer "):
            raw_token = auth_header.split(" ", 1)[1].strip()
            if raw_token:
                import base64
                import json as _json
                parts = raw_token.split(".")
                if len(parts) == 3:
                    payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                    decoded = _json.loads(base64.b64decode(payload_b64).decode("utf-8"))
                    sub = decoded.get("sub")
                    if sub:
                        actor_user_id = sub
                        auth_principal = decoded.get("preferred_username") or sub
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("session-resume-failed identity lookup failed: %s", exc)

    recorder = get_recorder()
    if recorder is None:
        # No recorder wired (tests, degraded mode). Acknowledge the request
        # silently so the frontend's fire-and-forget call doesn't retry.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        await recorder.record(AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="auth",
            action_type="auth.session_resume_failed",
            description=(
                "Silent session resume failed; frontend reported "
                f"reason={body.reason!r}, attempts={body.attempts}"
            ),
            correlation_id=make_correlation_id(),
            outcome="failure",
            outcome_detail=body.last_error or body.reason,
            inputs_meta={
                "reason": body.reason,
                "attempts": body.attempts,
                "resumed": True,
            },
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.debug("session-resume-failed self-record failed: %s", exc)
        # Still return 204 — the frontend cannot do anything with a server
        # error here, and the audit gap will surface in metrics anyway.

    return Response(status_code=status.HTTP_204_NO_CONTENT)
