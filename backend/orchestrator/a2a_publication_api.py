"""Authenticated owner/admin REST controls for A2A publication: approved catalog,
proposal intent, explicit confirmation, expiry-aware inventory, and withdrawal. Wired
into the orchestrator API and enforced by the shared publication policy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.auth import get_current_user_payload
from shared.a2a_publication import (
    A2APublicationError,
    A2APublicationRegistry,
    AlreadyWithdrawnError,
    ConfirmationMismatchError,
    InvalidAudienceError,
    InvalidLifetimeError,
    LIFETIMES,
    NotEligibleError,
    PublicationForbiddenError,
    PublicationStoreConflict,
    PublicationStoreUnavailable,
    StaleIntentError,
    UnknownCapabilityError,
    UnknownPublicationError,
)
from shared.a2a_security import token_roles

logger = logging.getLogger("Orchestrator.A2APublicationAPI")

publication_router = APIRouter(prefix="/api/a2a/publications", tags=["A2A"])

_A2A_DISABLED = "A2A server is not enabled on this deployment"


def registry_for(orch) -> Optional[A2APublicationRegistry]:
    registry = getattr(orch, "a2a_publications", None)
    return registry if isinstance(registry, A2APublicationRegistry) else None


def _registry_or_409(orch) -> A2APublicationRegistry:
    registry = registry_for(orch)
    if registry is None:
        raise HTTPException(status_code=409, detail=_A2A_DISABLED)
    if not registry.refresh():
        raise HTTPException(
            status_code=503, detail="A2A publication state is unavailable"
        )
    return registry


def _policy(orch):
    from orchestrator.a2a_publication_policy import A2APublicationPolicy

    policy = getattr(orch, "a2a_publication_policy", None)
    if isinstance(policy, A2APublicationPolicy):
        return policy
    registry = registry_for(orch)
    if registry is None:
        return None
    from orchestrator.a2a_orchestrator_executor import public_catalog_agent_ids
    from orchestrator.a2a_publication_wiring import owner_lookup
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS

    policy = A2APublicationPolicy(
        orch,
        registry,
        catalog=lambda: public_catalog_agent_ids(orch),
        owner_lookup=owner_lookup(orch),
        managed=lambda: FIRST_PARTY_PUBLIC_AGENT_IDS,
    )
    orch.a2a_publication_policy = policy
    return policy


async def record_publication_event(
    *, user_id: str, action: str, description: str = "",
    outcome: str = "success", detail: Optional[Dict[str, Any]] = None,
) -> None:
    from audit.recorder import get_recorder, make_correlation_id, now_utc
    from audit.schemas import AuditEventCreate

    recorder = get_recorder()
    if recorder is None or not user_id:
        return
    inputs_meta: Dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if isinstance(value, (str, int, float, bool)):
            inputs_meta[key] = value
    try:
        await recorder.record(AuditEventCreate(
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


def _actor(payload: dict) -> tuple[str, set[str], bool]:
    sub = str((payload or {}).get("sub") or "")
    emails = {
        str(value) for value in (
            sub,
            (payload or {}).get("email"),
            (payload or {}).get("preferred_username"),
        ) if value
    }
    roles = set(token_roles(payload or {}))
    return sub, emails, "admin" in roles


def _error_status(exc: A2APublicationError) -> int:
    if isinstance(exc, UnknownPublicationError):
        return 404
    if isinstance(exc, (ConfirmationMismatchError, PublicationForbiddenError)):
        return 403
    if isinstance(exc, (StaleIntentError, AlreadyWithdrawnError, PublicationStoreConflict)):
        return 409
    if isinstance(exc, PublicationStoreUnavailable):
        return 503
    if isinstance(exc, (UnknownCapabilityError, NotEligibleError, InvalidAudienceError,
                        InvalidLifetimeError)):
        return 400
    return 400


@publication_router.get(
    "",
    summary="List A2A publications",
    description="Owner inventory of current and recent A2A capability publications and pending confirmations.",
)
async def list_publications(
    request: Request,
    payload: dict = Depends(get_current_user_payload),
):
    sub, _emails, is_admin = _actor(payload)
    registry = _registry_or_409(_orchestrator(request))
    actor = None if is_admin else sub
    return {
        "publications": list(await asyncio.to_thread(registry.inventory, actor=actor)),
        "pending": list(await asyncio.to_thread(registry.pending_intents, actor=actor)),
        "lifetimes": [{"key": key, "seconds": seconds} for key, seconds in LIFETIMES],
    }


@publication_router.get(
    "/catalog",
    summary="List A2A-publishable capabilities",
    description="Approved capabilities the caller may publish, with their registered audiences.",
)
async def get_catalog(
    request: Request,
    payload: dict = Depends(get_current_user_payload),
):
    _sub, emails, is_admin = _actor(payload)
    orch = _orchestrator(request)
    registry = _registry_or_409(orch)
    policy = _policy(orch)
    if policy is None:
        raise HTTPException(status_code=409, detail=_A2A_DISABLED)
    cards = getattr(orch, "agent_cards", {}) or {}
    catalog = []
    for agent_id, audiences, published in await asyncio.to_thread(registry.eligible_catalog):
        if not policy.can_manage(emails, agent_id, is_admin=is_admin):
            continue
        card = cards.get(agent_id)
        catalog.append({
            "agent_id": agent_id,
            "name": str(getattr(card, "name", None) or agent_id),
            "description": str(getattr(card, "description", None) or ""),
            "audiences": list(audiences),
            "published": published,
            "owner": policy.owner_of(agent_id),
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
    payload: dict = Depends(get_current_user_payload),
):
    sub, emails, is_admin = _actor(payload)
    orch = _orchestrator(request)
    registry = _registry_or_409(orch)
    policy = _policy(orch)
    if not is_admin and (
        policy is None or not policy.can_manage(emails, body.agent_id, is_admin=False)
    ):
        await record_publication_event(
            user_id=sub, action="intent_denied",
            description="A2A publication intent refused: capability is not managed by this owner",
            outcome="failure", detail={"agent_id": body.agent_id},
        )
        raise HTTPException(status_code=403, detail="Capability is not managed by this owner")
    try:
        owner = policy.owner_of(body.agent_id)
        intent = await asyncio.to_thread(
            registry.create_intent,
            agent_id=body.agent_id,
            audiences=body.audiences,
            lifetime=body.lifetime,
            actor=sub,
            owner=owner,
            authorize=lambda agent_id: (policy.can_manage(emails, agent_id, is_admin=is_admin)
                and policy.owner_matches(agent_id, owner)),
        )
    except A2APublicationError as exc:
        await record_publication_event(
            user_id=sub, action="intent_denied",
            description=f"A2A publication intent refused: {exc}",
            outcome="failure", detail={"agent_id": body.agent_id},
        )
        raise HTTPException(status_code=_error_status(exc), detail=str(exc))

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
    payload: dict = Depends(get_current_user_payload),
):
    sub, emails, is_admin = _actor(payload)
    orch = _orchestrator(request)
    registry = _registry_or_409(orch)
    policy = _policy(orch)
    intent = await asyncio.to_thread(registry.get_intent, intent_id)
    owner_allowed = bool(
        intent is not None
        and policy is not None
        and policy.can_manage(emails, intent.agent_id, is_admin=False)
    )
    if intent is not None and not is_admin and not owner_allowed:
        await record_publication_event(
            user_id=sub, action="confirm_denied",
            description="A2A publication confirmation refused: foreign owner",
            outcome="failure", detail={"intent_id": intent_id},
        )
        raise HTTPException(status_code=403, detail="Only the proposing owner can confirm")
    try:
        record = await asyncio.to_thread(
            registry.confirm,
            intent_id=intent_id,
            confirmation_token=body.confirmation_token,
            actor=sub,
            privileged=is_admin or owner_allowed,
            authorize=lambda agent_id: (policy.can_manage(emails, agent_id, is_admin=is_admin)
                and (intent is not None and policy.owner_matches(agent_id, intent.owner))),
        )
    except A2APublicationError as exc:
        await record_publication_event(
            user_id=sub, action="confirm_denied",
            description=f"A2A publication confirmation refused: {exc}",
            outcome="failure", detail={"intent_id": intent_id},
        )
        raise HTTPException(status_code=_error_status(exc), detail=str(exc))

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
    payload: dict = Depends(get_current_user_payload),
):
    sub, emails, is_admin = _actor(payload)
    orch = _orchestrator(request)
    registry = _registry_or_409(orch)
    policy = _policy(orch)
    existing = await asyncio.to_thread(registry.get, publication_id)
    owner_allowed = bool(
        existing is not None
        and policy is not None
        and any(policy.owner_matches(existing.agent_id, email) for email in emails)
    )
    if existing is not None and not is_admin and not owner_allowed:
        await record_publication_event(
            user_id=sub, action="withdraw_denied",
            description="A2A withdrawal refused: foreign owner",
            outcome="failure", detail={"publication_id": publication_id},
        )
        raise HTTPException(status_code=403, detail="Only the publishing owner can withdraw")
    try:
        record = await asyncio.to_thread(
            registry.withdraw,
            publication_id=publication_id,
            actor=sub,
            reason=str(reason or "")[:200],
            privileged=is_admin or owner_allowed,
            authorize=lambda agent_id: is_admin or any(policy.owner_matches(agent_id, email) for email in emails),
        )
    except A2APublicationError as exc:
        await record_publication_event(
            user_id=sub, action="withdraw_denied",
            description=f"A2A withdrawal refused: {exc}",
            outcome="failure", detail={"publication_id": publication_id},
        )
        raise HTTPException(status_code=_error_status(exc), detail=str(exc))

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
        "withdrawn_at": record.withdrawn_at,
        "created_by": record.created_by,
        "owner": record.owner,
    }


def _orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator is not ready")
    return orch