"""REST routers for component feedback: feedback_user_router
(list/get/submit/retract/amend, owner always from the JWT) and feedback_admin_router
(quality, proposals, quarantine). Built on feedback/proposals.py and
feedback/recorder.py.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from orchestrator.auth import (
    get_current_user_payload,
    require_user_id,
    verify_admin,
)

from .proposals import (
    InvalidArtifactPath,
    StaleProposalError,
    apply_accepted,
    emit_quarantine_audit,
    reject_proposal,
)
from .recorder import (
    EditWindowExpired,
    FeedbackNotFound,
    Recorder,
)
from .repository import FeedbackRepository
from .schemas import (
    RATIONALE_MAX_CHARS,
    FeedbackAmendRequest,
    FeedbackSubmitRequest,
)

logger = logging.getLogger("Feedback.API")

feedback_user_router = APIRouter(prefix="/api/feedback", tags=["Feedback"])
feedback_admin_router = APIRouter(prefix="/api/admin/feedback", tags=["Feedback Admin"])


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orch


def _repo(request: Request) -> FeedbackRepository:
    orch = _orchestrator(request)
    repo = getattr(orch, "feedback_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Feedback subsystem not initialized")
    return repo


def _recorder(request: Request) -> Recorder:
    orch = _orchestrator(request)
    rec = getattr(orch, "feedback_recorder", None)
    if rec is None:
        raise HTTPException(status_code=503, detail="Feedback subsystem not initialized")
    return rec


def _principal_of(payload: dict) -> str:
    return payload.get("preferred_username") or payload.get("sub") or "unknown"


class UserSubmitResponse(BaseModel):
    feedback_id: str
    status: str
    deduped: bool


class UserListResponse(BaseModel):
    items: List[Dict[str, Any]]
    next_cursor: Optional[str] = None


@feedback_user_router.get(
    "",
    response_model=UserListResponse,
    summary="List the authenticated user's own feedback",
)
async def list_my_feedback(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = None,
    lifecycle: str = Query("active", pattern="^(active|superseded|retracted)$"),
    source_tool: Optional[str] = None,
    source_agent: Optional[str] = None,
    from_ts: Optional[datetime] = Query(default=None, alias="from"),
    to_ts: Optional[datetime] = Query(default=None, alias="to"),
    user_id: str = Depends(require_user_id),
):
    repo = _repo(request)
    items, next_cursor = repo.list_for_user(
        user_id, lifecycle=lifecycle, source_tool=source_tool,
        source_agent=source_agent, from_ts=from_ts, to_ts=to_ts,
        limit=limit, cursor=cursor,
    )
    return UserListResponse(
        items=[i.to_user_view() for i in items],
        next_cursor=next_cursor,
    )


@feedback_user_router.get(
    "/{feedback_id}",
    summary="Get one of the authenticated user's feedback entries",
)
async def get_my_feedback(
    feedback_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
):
    repo = _repo(request)
    dto = repo.get_for_user(user_id, feedback_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="not found")
    return dto.to_user_view()


@feedback_user_router.post(
    "",
    response_model=UserSubmitResponse,
    summary="Submit feedback for a rendered component",
)
async def submit_feedback(
    body: FeedbackSubmitRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    rec = _recorder(request)
    result = await rec.submit(
        actor_user_id=user_id,
        auth_principal=_principal_of(payload),
        conversation_id=None,
        correlation_id=body.correlation_id,
        source_agent=body.source_agent,
        source_tool=body.source_tool,
        component_id=body.component_id,
        sentiment=body.sentiment,
        category=body.category,
        comment=body.comment,
    )
    return UserSubmitResponse(
        feedback_id=str(result.feedback.id),
        status=result.status,
        deduped=result.deduped,
    )


class RetractResponse(BaseModel):
    feedback_id: str
    lifecycle: str


@feedback_user_router.post(
    "/{feedback_id}/retract",
    response_model=RetractResponse,
    summary="Retract one of your own feedback entries (within 24 h)",
)
async def retract_feedback(
    feedback_id: str,
    request: Request,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    rec = _recorder(request)
    try:
        updated = await rec.retract(user_id, _principal_of(payload), feedback_id)
    except FeedbackNotFound:
        raise HTTPException(status_code=404, detail="not found")
    except EditWindowExpired:
        raise HTTPException(status_code=409, detail="EDIT_WINDOW_EXPIRED")
    return RetractResponse(feedback_id=str(updated.id), lifecycle=updated.lifecycle)


class AmendResponse(BaseModel):
    feedback_id: str
    prior_id: str
    lifecycle: str
    comment_safety: str


@feedback_user_router.patch(
    "/{feedback_id}",
    response_model=AmendResponse,
    summary="Amend one of your own feedback entries (within 24 h)",
)
async def amend_feedback(
    feedback_id: str,
    body: FeedbackAmendRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    rec = _recorder(request)
    body_keys = body.model_fields_set if hasattr(body, "model_fields_set") else set()
    comment_explicit = "comment" in body_keys
    try:
        new_row = await rec.amend(
            user_id, _principal_of(payload), feedback_id,
            sentiment=body.sentiment, category=body.category,
            comment=body.comment, comment_explicit=comment_explicit,
        )
    except FeedbackNotFound:
        raise HTTPException(status_code=404, detail="not found")
    except EditWindowExpired:
        raise HTTPException(status_code=409, detail="EDIT_WINDOW_EXPIRED")
    return AmendResponse(
        feedback_id=str(new_row.id),
        prior_id=feedback_id,
        lifecycle=new_row.lifecycle,
        comment_safety=new_row.comment_safety,
    )


class AdminFlaggedResponse(BaseModel):
    items: List[Dict[str, Any]]
    next_cursor: Optional[str] = None


@feedback_admin_router.get(
    "/quality/flagged",
    response_model=AdminFlaggedResponse,
    summary="List currently-underperforming tools (admin)",
)
async def list_flagged(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = None,
    _admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    snaps, next_cursor = repo.list_underperforming(limit=limit, cursor=cursor)
    items: List[Dict[str, Any]] = []
    for s in snaps:
        cb = repo.category_breakdown(s.agent_id, s.tool_name, s.window_start, s.window_end)
        props, _ = repo.list_proposals(
            status="pending", agent_id=s.agent_id, tool_name=s.tool_name, limit=1,
        )
        items.append(s.to_admin_view(
            flagged_at=s.computed_at,
            pending_proposal_id=str(props[0].id) if props else None,
        ) | {"category_breakdown": cb})
    return AdminFlaggedResponse(items=items, next_cursor=next_cursor)


class AdminEvidenceResponse(BaseModel):
    agent_id: str
    tool_name: str
    window_start: str
    window_end: str
    audit_event_ids: List[str]
    component_feedback_ids: List[str]
    category_breakdown: Dict[str, int]


@feedback_admin_router.get(
    "/quality/flagged/{agent_id}/{tool_name}/evidence",
    response_model=AdminEvidenceResponse,
    summary="Supporting evidence for an underperforming-tool flag (admin)",
)
async def flagged_evidence(
    agent_id: str,
    tool_name: str,
    request: Request,
    _admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    snap = repo.latest_quality_signal(agent_id, tool_name)
    if snap is None:
        raise HTTPException(status_code=404, detail="not found")
    audit_ids, fb_ids = repo.evidence_ids(
        agent_id, tool_name, snap.window_start, snap.window_end,
    )
    cb = repo.category_breakdown(agent_id, tool_name, snap.window_start, snap.window_end)
    return AdminEvidenceResponse(
        agent_id=agent_id, tool_name=tool_name,
        window_start=snap.window_start.isoformat(),
        window_end=snap.window_end.isoformat(),
        audit_event_ids=audit_ids,
        component_feedback_ids=fb_ids,
        category_breakdown=cb,
    )


class ProposalSummaryItem(BaseModel):
    id: str
    agent_id: str
    tool_name: str
    artifact_path: str
    status: str
    generated_at: str
    reviewer_user_id: Optional[str] = None
    reviewed_at: Optional[str] = None
    evidence_summary: Dict[str, int] = Field(default_factory=dict)


class AdminProposalsResponse(BaseModel):
    items: List[ProposalSummaryItem]
    next_cursor: Optional[str] = None


@feedback_admin_router.get(
    "/proposals",
    response_model=AdminProposalsResponse,
    summary="List knowledge-update proposals (admin)",
)
async def list_proposals(
    request: Request,
    status: str = Query("pending"),
    agent_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = None,
    _admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    items, next_cursor = repo.list_proposals(
        status=status, agent_id=agent_id, tool_name=tool_name,
        limit=limit, cursor=cursor,
    )
    return AdminProposalsResponse(
        items=[ProposalSummaryItem(
            id=str(p.id), agent_id=p.agent_id, tool_name=p.tool_name,
            artifact_path=p.artifact_path, status=p.status,
            generated_at=p.generated_at.isoformat(),
            reviewer_user_id=p.reviewer_user_id,
            reviewed_at=p.reviewed_at.isoformat() if p.reviewed_at else None,
            evidence_summary={
                "audit_events": len(p.evidence.get("audit_event_ids") or []),
                "component_feedback": len(p.evidence.get("component_feedback_ids") or []),
            },
        ) for p in items],
        next_cursor=next_cursor,
    )


@feedback_admin_router.get(
    "/proposals/{proposal_id}",
    summary="Full detail for a proposal (admin)",
)
async def get_proposal(
    proposal_id: str,
    request: Request,
    _admin: dict = Depends(verify_admin),
):
    from .proposals import KNOWLEDGE_ROOT, _sha256_of_path
    repo = _repo(request)
    dto = repo.get_proposal(proposal_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="not found")
    artifact_abs = (KNOWLEDGE_ROOT / dto.artifact_path).resolve()
    try:
        artifact_abs.relative_to(KNOWLEDGE_ROOT)
        current_sha = _sha256_of_path(artifact_abs)
    except ValueError:
        raise HTTPException(status_code=400, detail="INVALID_PATH")

    return {
        "id": str(dto.id),
        "agent_id": dto.agent_id,
        "tool_name": dto.tool_name,
        "artifact_path": dto.artifact_path,
        "diff_payload": dto.diff_payload,
        "artifact_sha_at_gen": dto.artifact_sha_at_gen,
        "current_artifact_sha": current_sha,
        "is_current": current_sha == dto.artifact_sha_at_gen,
        "evidence": dto.evidence,
        "status": dto.status,
        "reviewer_user_id": dto.reviewer_user_id,
        "reviewed_at": dto.reviewed_at.isoformat() if dto.reviewed_at else None,
        "reviewer_rationale": dto.reviewer_rationale,
        "applied_at": dto.applied_at.isoformat() if dto.applied_at else None,
        "generated_at": dto.generated_at.isoformat(),
    }


class AcceptProposalBody(BaseModel):
    edited_diff: Optional[str] = None


@feedback_admin_router.post(
    "/proposals/{proposal_id}/accept",
    summary="Accept and apply a pending proposal (admin)",
)
async def accept_proposal(
    proposal_id: str,
    body: AcceptProposalBody,
    request: Request,
    admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    user_id = admin.get("sub") or "admin"
    try:
        applied = await apply_accepted(
            repo, proposal_id,
            reviewer_user_id=user_id,
            auth_principal=_principal_of(admin),
            edited_diff=body.edited_diff,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except StaleProposalError:
        raise HTTPException(status_code=409, detail="STALE_PROPOSAL")
    except InvalidArtifactPath:
        raise HTTPException(status_code=400, detail="INVALID_PATH")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"INVALID_INPUT: {exc}")

    return {
        "id": str(applied.id),
        "status": applied.status,
        "applied_at": applied.applied_at.isoformat() if applied.applied_at else None,
    }


class RejectProposalBody(BaseModel):
    rationale: str = Field(min_length=1, max_length=RATIONALE_MAX_CHARS)


@feedback_admin_router.post(
    "/proposals/{proposal_id}/reject",
    summary="Reject a pending proposal (admin)",
)
async def reject_proposal_endpoint(
    proposal_id: str,
    body: RejectProposalBody,
    request: Request,
    admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    user_id = admin.get("sub") or "admin"
    try:
        rejected = await reject_proposal(
            repo, proposal_id,
            reviewer_user_id=user_id,
            auth_principal=_principal_of(admin),
            rationale=body.rationale,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"INVALID_INPUT: {exc}")

    return {
        "id": str(rejected.id),
        "status": rejected.status,
        "reviewed_at": rejected.reviewed_at.isoformat() if rejected.reviewed_at else None,
    }


class QuarantineListResponse(BaseModel):
    items: List[Dict[str, Any]]
    next_cursor: Optional[str] = None


@feedback_admin_router.get(
    "/quarantine",
    response_model=QuarantineListResponse,
    summary="List quarantined feedback items (admin)",
)
async def list_quarantine(
    request: Request,
    status: str = Query("held", pattern="^(held|released|dismissed)$"),
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = None,
    _admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    items, next_cursor = repo.list_quarantine(status=status, limit=limit, cursor=cursor)
    return QuarantineListResponse(items=items, next_cursor=next_cursor)


@feedback_admin_router.post(
    "/quarantine/{feedback_id}/release",
    summary="Release a quarantined feedback item back to the synthesizer pool (admin)",
)
async def release_quarantine(
    feedback_id: str,
    request: Request,
    admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    user_id = admin.get("sub") or "admin"
    dto = repo.quarantine_action(feedback_id, status="released", actor_user_id=user_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="not found")
    await emit_quarantine_audit(
        action_type="quarantine.release",
        feedback_id=feedback_id, reason=dto.reason, detector=dto.detector,
        actor_user_id=user_id, auth_principal=_principal_of(admin),
    )
    return {"feedback_id": feedback_id, "status": "released"}


@feedback_admin_router.post(
    "/quarantine/{feedback_id}/dismiss",
    summary="Dismiss a quarantined feedback item (text stays held; admin)",
)
async def dismiss_quarantine(
    feedback_id: str,
    request: Request,
    admin: dict = Depends(verify_admin),
):
    repo = _repo(request)
    user_id = admin.get("sub") or "admin"
    dto = repo.quarantine_action(feedback_id, status="dismissed", actor_user_id=user_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="not found")
    await emit_quarantine_audit(
        action_type="quarantine.dismiss",
        feedback_id=feedback_id, reason=dto.reason, detector=dto.detector,
        actor_user_id=user_id, auth_principal=_principal_of(admin),
    )
    return {"feedback_id": feedback_id, "status": "dismissed"}
