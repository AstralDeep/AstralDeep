"""
FastAPI router exposing the owner‑managed A2A publication workflow.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

# Dependency that extracts the current user id from the request (Keycloak JWT)
# In the real codebase this is provided by `backend.auth.dependencies`.
from ..auth.dependencies import get_current_user_id

from ..orchestrator.a2a_publication_service import (
    A2APublicationService,
    PublicationNotFound,
    OwnerMismatch,
    InvalidTransition,
    A2APublicationError,
)

router = APIRouter(prefix="/a2a/publication", tags=["A2A Publication"])


@router.post(
    "/propose",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    summary="Propose a new A2A capability for publication",
)
def propose_capability(
    payload: dict,
    user_id: str = Depends(get_current_user_id),
):
    """
    Payload must contain:
    - capability_name: str
    - capability_version: str
    - expires_in_seconds (optional): int
    """
    try:
        publication = A2APublicationService.propose(
            owner_id=user_id,
            capability_name=payload["capability_name"],
            capability_version=payload["capability_version"],
            expires_in_seconds=payload.get("expires_in_seconds"),
        )
        return {"id": str(publication.id), "state": publication.state}
    except A2APublicationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/{pub_id}/confirm",
    response_model=dict,
    summary="Confirm a previously proposed capability",
)
def confirm_capability(pub_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        publication = A2APublicationService.confirm(uuid.UUID(pub_id), user_id)
        return {"id": str(publication.id), "state": publication.state}
    except PublicationNotFound:
        raise HTTPException(status_code=404, detail="Publication not found")
    except (OwnerMismatch, InvalidTransition) as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{pub_id}/publish",
    response_model=dict,
    summary="Publish a confirmed capability so it becomes discoverable",
)
def publish_capability(pub_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        publication = A2APublicationService.publish(uuid.UUID(pub_id), user_id)
        return {"id": str(publication.id), "state": publication.state}
    except PublicationNotFound:
        raise HTTPException(status_code=404, detail="Publication not found")
    except (OwnerMismatch, InvalidTransition) as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post(
    "/{pub_id}/withdraw",
    response_model=dict,
    summary="Withdraw a published capability",
)
def withdraw_capability(
    pub_id: str,
    payload: dict | None = None,
    user_id: str = Depends(get_current_user_id),
):
    try:
        reason = payload.get("revocation_reason") if payload else None
        publication = A2APublicationService.withdraw(
            uuid.UUID(pub_id), user_id, revocation_reason=reason
        )
        return {"id": str(publication.id), "state": publication.state}
    except PublicationNotFound:
        raise HTTPException(status_code=404, detail="Publication not found")
    except (OwnerMismatch, InvalidTransition) as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get(
    "/my",
    response_model=List[dict],
    summary="List all publications owned by the caller",
)
def list_my_publications(user_id: str = Depends(get_current_user_id)):
    pubs = A2APublicationService.list_by_owner(user_id)
    return [
        {
            "id": str(p.id),
            "capability_name": p.capability_name,
            "capability_version": p.capability_version,
            "state": p.state,
            "created_at": p.created_at.isoformat(),
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        }
        for p in pubs
    ]
