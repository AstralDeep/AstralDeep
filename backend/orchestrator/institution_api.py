"""Serves institution descriptors, owner-scoped pairing offers, and Keycloak reattestation for an institution node.
node_api.py includes this router, while institution_runtime.py supplies the isolated peer for each authenticated owner.
Kernel verifies the submitted ID token and attests only the local node identity in that owner's mesh.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Sequence

from fastapi import APIRouter, Depends, HTTPException, Response
from jose import jwt as jose_jwt

from orchestrator import institution_runtime, mesh_api, node_mesh
from orchestrator.institution_meshes import Owner
from shared import institution_profile, node_identity, token_verify

institution_router = APIRouter(tags=["Institution"])

EXTRA_CAPABILITIES = ("institution", "multi-owner", "fresh-attestation", "peer-phi-group")
MAX_ATTESTATION_TOKEN_BYTES = 65536


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


async def _owner_mesh(actor: Dict[str, Any]):
    if not node_identity.is_institution():
        raise HTTPException(404, "node_profile_inactive")
    mesh_api._require_confirm_authority(actor)
    if actor["kind"] != "owner":
        raise HTTPException(403, "institution_owner_required")
    claims = actor.get("claims") or {}
    try:
        issuer = claims.get(token_verify.IDP_ISSUER_CLAIM) or claims.get("iss")
        owner = Owner.verified(issuer, claims.get(token_verify.IDP_SUBJECT_CLAIM))
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
    return owner, claims, mesh


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


@institution_router.post("/api/institution/attestation/challenge")
async def begin_attestation(
    response: Response,
    actor: Dict[str, Any] = Depends(mesh_api._read_actor),
    _guard: None = mesh_api._WRITE_GUARD,
):
    response.headers["Cache-Control"] = "no-store"
    _, _, mesh = await _owner_mesh(actor)
    try:
        challenge = await asyncio.to_thread(mesh.kernel.begin_attestation)
    except Exception as exc:
        if hasattr(mesh, "_binding") and isinstance(exc, mesh._binding.KernelError):
            raise HTTPException(409, node_mesh._code_of(exc)) from exc
        if isinstance(exc, node_mesh.MeshUnavailable):
            raise HTTPException(503, exc.code) from exc
        raise
    return {
        "nonce": challenge.nonce,
        "issuer": challenge.issuer,
        "client_id": challenge.client_id,
        "mesh_id": challenge.mesh_id,
    }


@institution_router.post("/api/institution/attestation")
async def complete_attestation(
    body: Dict[str, Any],
    response: Response,
    actor: Dict[str, Any] = Depends(mesh_api._read_actor),
    _guard: None = mesh_api._WRITE_GUARD,
):
    response.headers["Cache-Control"] = "no-store"
    if set(body) != {"id_token"} or not isinstance(body.get("id_token"), str) or not body["id_token"]:
        raise HTTPException(400, "institution_id_token_required")
    if not body["id_token"].isascii():
        raise HTTPException(400, "institution_id_token_invalid")
    if len(body["id_token"]) > MAX_ATTESTATION_TOKEN_BYTES:
        raise HTTPException(413, "institution_id_token_too_large")
    _, claims, mesh = await _owner_mesh(actor)
    try:
        token_claims = jose_jwt.get_unverified_claims(body["id_token"])
    except Exception:
        raise HTTPException(400, "institution_id_token_invalid") from None
    try:
        token_owner = Owner.verified(token_claims.get("iss"), token_claims.get("sub"))
    except node_mesh.NodeMeshError:
        raise HTTPException(403, "institution_owner_mismatch") from None
    issuer = claims.get(token_verify.IDP_ISSUER_CLAIM) or claims.get("iss")
    if token_owner != Owner.verified(issuer, claims.get(token_verify.IDP_SUBJECT_CLAIM)):
        raise HTTPException(403, "institution_owner_mismatch")
    try:
        await asyncio.to_thread(mesh.kernel.reattest, body["id_token"])
    except Exception as exc:
        if hasattr(mesh, "_binding") and isinstance(exc, mesh._binding.KernelError):
            raise HTTPException(403, node_mesh._code_of(exc)) from exc
        if isinstance(exc, node_mesh.MeshUnavailable):
            raise HTTPException(503, exc.code) from exc
        raise
    return {"attested": True}
