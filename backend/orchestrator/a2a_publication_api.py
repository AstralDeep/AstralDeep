"""Admin REST controls for selective A2A publication: approved-capability catalog,
publish intent/confirmation, sanitized inventory, and withdrawal; wired into the
orchestrator's FastAPI app and the Admin Tools chrome surface.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.auth import verify_admin
from shared.a2a_publication import (
    A2APublicationError,
    A2APublicationRegistry,
    AlreadyWithdrawnError,
    ConfirmationMismatchError,
    InvalidAudienceError,
    LIFETIMES,
    StaleIntentError,
    UnknownPublicationError,
)

logger = logging.getLogger("Orchestrator.A2APublicationAPI")

publication_router = APIRouter(prefix="/api/a2a/publications", tags=["A2A"])

_A2A_DISABLED = "A2A server is not enabled on this deployment"


def registry_for(orch) -> Optional[A2APublicationRegistry]:
    registry = getattr(orch, "a2a_publications", None)
    return registry if isinstance(registry, A2APublicationRegistry) else None


def _registry_or_404(orch) -> A2APublicationRegistry:
    registry = registry_for(orch)
    if registry is None:
        raise HTTPException(status_code=409, detail=_A2A_DISABLED)
    return registry


async def record_publication_event(
    *, user_id: str, action: str, description: str = "",
    outcome: str = "success", detail: Optional[Dict[str, Any]] = None,
) -> None:
    from audit.recorder import get_recorder, make_correlation_id, now_utc
    from audit.schemas import AuditEventCreate

    rec = get_recorder()
    if rec is None or not user_id:
        return
    inputs_meta: Dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if isinstance(value, (str, int, float, bool)):
            inputs_meta[key] = value
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=user_id,
            auth_principal=user_id,
            event_class="settings",
            action_type=f"a2a_publication.{action}",
            description=description or f"A2A publication {action.replace('_', ' ')}",
            correlation_id=make_correlation_id(),
            outcome=outcome,
            inputs_meta=inputs_meta,
            started_at=now_utc(),
        ))
    except Exception:
        logger.exception("a2a publication audit record failed")


class PublishIntentRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=256)
    audiences: List[str] = Field(min_length=1, max_length=8)
    lifetime: str = Field(min_length=1, max_length=16)


class ConfirmPublishRequest(BaseModel):
    confirmation_token: str = Field(min_length=1, max_length=256)


def _actor(admin: dict) -> str:
    return str((admin or {}).get("sub") or "unknown")


def _error_status(exc: A2APublicationError) -> int:
    if isinstance(exc, UnknownPublicationError):
        return 404
    if isinstance(exc, (ConfirmationMismatchError,)):
        return 403
    if isinstance(exc, (StaleIntentError, AlreadyWithdrawnError)):
        return 409
    if isinstance(exc, InvalidAudienceError):
        return 400
    return 400


@publication_router.get(
    "",
    summary="List A2A publications",
    description="Admin inventory of current and recent A2A capability publications and pending confirmations.",
)
async def list_publications(
    request: Request,
    admin: dict = Depends(verify_admin),
):
    orch = _orchestrator(request)
    registry = _registry_or_404(orch)
    return {
        "publications": list(registry.inventory()),
        "pending": list(registry.pending_intents()),
        "lifetimes": [{"key": key, "seconds": seconds} for key, seconds in LIFETIMES],
    }


@publication_router.get(
    "/catalog",
    summary="List A2A-publishable capabilities",
    description="Approved capabilities eligible for A2A publication with their registered audiences.",
)
async def get_catalog(
    request: Request,
    admin: dict = Depends(verify_admin),
):
    orch = _orchestrator(request)
    registry = _registry_or_404(orch)
    catalog = []
    cards = getattr(orch, "agent_cards", {}) or {}
    for agent_id, audiences, published in registry.eligible_catalog():
        card = cards.get(agent_id)
        catalog.append({
            "agent_id": agent_id,
            "name": str(getattr(card, "name", None) or agent_id),
            "description": str(getattr(card, "description", None) or ""),
            "audiences": list(audiences),
            "published": published,
        })
    return {"capabilities": catalog, "lifetimes": [key for key, _ in LIFETIMES]}


@publication_router.post(
    "/intents",
    summary="Create an A2A publication intent",
    description="Stage a capability/audience/lifetime choice; publishing requires the follow-up confirmation call.",
)
async def create_intent(
    request: Request,
    body: PublishIntentRequest,
    admin: dict = Depends(verify_admin),
):
    user_id = _actor(admin)
    orch = _orchestrator(request)
    registry = _registry_or_404(orch)
    try:
        intent = registry.create_intent(
            agent_id=body.agent_id,
            audiences=body.audiences,
            lifetime=body.lifetime,
            actor=user_id,
        )
    except A2APublicationError as exc:
        await record_publication_event(
            user_id=user_id, action="intent_denied",
            description=f"A2A publication intent refused: {exc}",
            outcome="failure", detail={"agent_id": body.agent_id},
        )
        raise HTTPException(status_code=_error_status(exc), detail=str(exc))
    await record_publication_event(
        user_id=user_id, action="intent_created",
        detail={"agent_id": intent.agent_id, "audiences": ",".join(intent.audiences),
                "lifetime": intent.lifetime, "intent_id": intent.intent_id},
    )
    return {
        "intent_id": intent.intent_id,
        "agent_id": intent.agent_id,
        "audiences": list(intent.audiences),
        "lifetime": intent.lifetime,
        "fingerprint": intent.fingerprint,
        "expires_at": intent.expires_at,
        "confirmation_token": intent.confirmation_token,
    }


@publication_router.post(
    "/intents/{intent_id}/confirm",
    summary="Confirm an A2A publication intent",
    description="Activate the staged publication with the confirmation token returned when the intent was created.",
)
async def confirm_intent(
    request: Request,
    intent_id: str,
    body: ConfirmPublishRequest,
    admin: dict = Depends(verify_admin),
):
    user_id = _actor(admin)
    orch = _orchestrator(request)
    registry = _registry_or_404(orch)
    try:
        record = registry.confirm(
            intent_id=intent_id,
            confirmation_token=body.confirmation_token,
            actor=user_id,
        )
    except A2APublicationError as exc:
        await record_publication_event(
            user_id=user_id, action="confirm_denied",
            description=f"A2A publication confirmation refused: {exc}",
            outcome="failure", detail={"intent_id": intent_id},
        )
        raise HTTPException(status_code=_error_status(exc), detail=str(exc))
    await record_publication_event(
        user_id=user_id, action="published",
        detail={"publication_id": record.publication_id, "agent_id": record.agent_id,
                "audiences": ",".join(record.audiences), "lifetime": record.lifetime},
    )
    return _publication_body(record)


@publication_router.delete(
    "/{publication_id}",
    summary="Withdraw an A2A publication",
    description="Remove a published capability from the A2A advertisement and refuse further A2A dispatch for it.",
)
async def withdraw_publication(
    request: Request,
    publication_id: str,
    reason: str = "",
    admin: dict = Depends(verify_admin),
):
    user_id = _actor(admin)
    orch = _orchestrator(request)
    registry = _registry_or_404(orch)
    try:
        record = registry.withdraw(
            publication_id=publication_id,
            actor=user_id,
            reason=str(reason or "")[:200],
        )
    except A2APublicationError as exc:
        await record_publication_event(
            user_id=user_id, action="withdraw_denied",
            description=f"A2A withdrawal refused: {exc}",
            outcome="failure", detail={"publication_id": publication_id},
        )
        raise HTTPException(status_code=_error_status(exc), detail=str(exc))
    await record_publication_event(
        user_id=user_id, action="withdrawn",
        detail={"publication_id": record.publication_id, "agent_id": record.agent_id},
    )
    return _publication_body(record)


def _publication_body(record) -> Dict[str, Any]:
    return {
        "publication_id": record.publication_id,
        "agent_id": record.agent_id,
        "audiences": list(record.audiences),
        "lifetime": record.lifetime,
        "status": record.status,
        "published_at": record.published_at,
        "expires_at": record.expires_at,
    }


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator is not ready")
    return orch
