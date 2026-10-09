# Mesh Target Resolution and Presence API Handlers
# Part of AstralDeep #281

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .mesh_targets import (
    MeshPresenceService,
    MeshTarget,
    MeshTargetResolution,
    PresenceStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mesh/targets", tags=["mesh-targets"])


class UpdatePresenceRequest(BaseModel):
    """Request body for updating mesh target presence."""
    status: PresenceStatus
    endpoint: Optional[str] = None


class ResolveTargetResponse(BaseModel):
    """Response for mesh target resolution."""
    target_id: str
    resolution: str
    endpoint: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    reason: Optional[str] = None
    resolved_at: str
    verified_membership: bool


@router.get(
    "/{target_id}/resolve",
    response_model=ResolveTargetResponse,
    summary="Resolve a mesh target",
    description="Resolve an enrolled mesh target and return its capabilities",
)
async def resolve_target(
    target_id: str,
    service: MeshPresenceService,
) -> ResolveTargetResponse:
    """Resolve a mesh target against enrolled identities."""
    result: MeshTargetResolution = service.resolve_target(target_id)
    return ResolveTargetResponse(
        target_id=result.target_id,
        resolution=result.resolution.value,
        endpoint=result.endpoint,
        capabilities=result.capabilities,
        reason=result.reason,
        resolved_at=result.resolved_at.isoformat(),
        verified_membership=result.verified_membership,
    )


@router.get(
    "/{target_id}/presence",
    response_model=MeshTarget,
    summary="Get mesh target presence",
    description="Get current presence status for a mesh target",
)
async def get_presence(
    target_id: str,
    service: MeshPresenceService,
) -> MeshTarget:
    """Get current presence info for a target."""
    target: Optional[MeshTarget] = service.get_presence(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    return target


@router.put(
    "/{target_id}/presence",
    summary="Update mesh target presence",
    description="Update presence status for an enrolled mesh target",
)
async def update_presence(
    target_id: str,
    body: UpdatePresenceRequest,
    service: MeshPresenceService,
) -> dict:
    """Update presence status for a mesh target."""
    service.update_presence(target_id, body.status, body.endpoint)
    return {"status": "updated", "target_id": target_id}


@router.get(
    "/",
    response_model=list[MeshTarget],
    summary="List approved mesh targets",
    description="List all approved mesh targets with optional presence filter",
)
async def list_targets(
    status: Optional[PresenceStatus] = Query(None, description="Filter by presence status"),
    service: MeshPresenceService,
) -> list[MeshTarget]:
    """List all approved targets with optional status filter."""
    return service.list_approved_targets(status_filter=status)


@router.post(
    "/{target_id}/revoke",
    summary="Revoke a mesh target",
    description="Mark a target as revoked",
)
async def revoke_target(
    target_id: str,
    service: MeshPresenceService,
) -> dict:
    """Revoke a mesh target."""
    success: bool = service.mark_revoked(target_id)
    if not success:
        raise HTTPException(status_code=404, detail="Target not found")
    return {"status": "revoked", "target_id": target_id}
