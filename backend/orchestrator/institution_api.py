"""REST surface of an institution node: the descriptor fields that declare its PHI approval and fresh-attestation rule, and the owner-authenticated call that opens a pairing offer in that owner's own mesh.
node_api.py includes the router and builds the descriptor through it, and institution_runtime.py starts the per-owner peer behind the pairing call.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from fastapi import APIRouter, Depends, HTTPException, Response

from orchestrator import institution_runtime, mesh_api, node_mesh
from orchestrator.institution_meshes import Owner
from shared import institution_profile, node_identity, token_verify

institution_router = APIRouter(tags=["Institution"])

EXTRA_CAPABILITIES = ("institution", "multi-owner", "fresh-attestation", "peer-phi-group")


def _config() -> institution_profile.InstitutionConfig:
    runtime = institution_runtime.current()
    if runtime is not None:
        return runtime.config
    try:
        return institution_profile.load()
    except (institution_profile.InstitutionConfigError, node_identity.NodeIdentityError):
        raise HTTPException(503, "institution_config_invalid") from None


def descriptor(
    identity: node_identity.NodeIdentity, document_format: str, version: str, capabilities: Sequence[str]
) -> Dict[str, Any]:
    config = _config()
    approval = config.current_approval()
    return {
        "format": document_format,
        "version": version,
        "profile": node_identity.INSTITUTION,
        "node_id": identity.node_id,
        "node_key": identity.public_jwk(),
        "accepted_issuers": list(config.issuers),
        "owner_hash": None,
        "institution_key": config.key,
        "phi_approval": approval,
        "capabilities": [*capabilities, *EXTRA_CAPABILITIES],
        "transport_addresses": [],
    }


@institution_router.post("/api/institution/pairing")
async def create_pairing(
    response: Response,
    actor: Dict[str, Any] = Depends(mesh_api._read_actor),
    _guard: None = mesh_api._WRITE_GUARD,
):
    from orchestrator import node_api

    response.headers["Cache-Control"] = "no-store"
    if not node_identity.is_institution():
        raise HTTPException(404, "node_profile_inactive")
    mesh_api._require_confirm_authority(actor)
    if actor["kind"] != "owner":
        raise HTTPException(403, "institution_owner_required")
    claims = actor.get("claims") or {}
    try:
        owner = Owner.verified(claims.get("iss"), claims.get(token_verify.IDP_SUBJECT_CLAIM))
        if owner.partition != claims.get("sub"):
            raise node_mesh.NodeMeshError("owner_invalid", 400)
    except node_mesh.NodeMeshError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    runtime = institution_runtime.current()
    if runtime is None:
        raise HTTPException(503, "institution_mesh_unavailable")
    try:
        mesh = await runtime.serve_owner(owner)
    except node_mesh.NodeMeshError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    pairing = await node_api._mesh_pairing(mesh)
    return {
        "owner_hash": node_identity.owner_principal_hash(owner.partition),
        "mesh": pairing,
        "node": node_api.descriptor(),
    }
