"""REST surface of a personal node: the public v2 descriptor, which names no identity server and carries only a hash of the owner's mesh id, the node's JWKS, and the loopback pairing offer that only a holder of the node's pairing secret can read.
node_connection_api.py adds the loopback operator routes for the node's outside connections. The offer comes from node_mesh.py, which owns the secret file, the owner approves it on an existing mesh device.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from orchestrator import node_connection_api
from orchestrator import node_mesh
from orchestrator import phi_node_policy
from shared import node_identity

node_router = APIRouter(tags=["Node"])

DESCRIPTOR_FORMAT = "astral.node.descriptor/v2"
NODE_VERSION = "astral.node/v1"
CAPABILITIES = (
    "chat",
    "tool-dispatch",
    "mesh-pairing",
    "delegation-eddsa",
    "emergency-stop",
)
PAIRING_SECRET_HEADER = "astral-pairing-secret"
NO_STORE = {"Cache-Control": "no-store"}


def _require_node() -> node_identity.NodeIdentity:
    if not (node_identity.is_personal() or node_identity.is_institution()):
        raise HTTPException(404, "node_profile_inactive")
    try:
        return node_identity.load_node_identity()
    except node_identity.NodeIdentityError as exc:
        raise HTTPException(503, "node_key_unavailable") from exc


def _owner_hash() -> str | None:
    try:
        owner = node_identity.bound_mesh_owner()
    except node_identity.OwnerMismatch:
        return None
    except node_identity.NodeIdentityError as exc:
        raise HTTPException(503, "node.owner_binding_unavailable", headers=NO_STORE) from exc
    if owner is None:
        return None
    return node_identity.owner_principal_hash(node_identity.mesh_id_for_owner(owner))


def descriptor() -> dict[str, Any]:
    identity = _require_node()
    return {
        "format": DESCRIPTOR_FORMAT,
        "version": NODE_VERSION,
        "node_id": identity.node_id,
        "node_key": identity.public_jwk(),
        "owner_hash": _owner_hash(),
        "capabilities": list(CAPABILITIES),
        "transport_addresses": [],
    }


async def mesh_descriptor_fields(document: dict[str, Any]) -> dict[str, Any]:
    mesh = node_mesh.current()
    if mesh is None or not mesh.started:
        return document
    status = await mesh.status()
    enrolled = bool(status.get("enrolled"))
    document["mesh"] = {
        "state": status["state"],
        "mesh_id": status.get("mesh_id"),
        "member_id": status.get("member_id"),
        "hardware_backed": status["hardware_backed"],
        "requests": status["requests"],
        "owner_binding": status["owner_binding"],
    }
    capabilities = list(document["capabilities"]) + ["mesh-peer"]
    if status.get("endpoint_address"):
        document["transport_addresses"] = [{"kind": "iroh", "address": status["endpoint_address"]}]
    mailbox = status.get("mailbox") or {}
    if enrolled and mailbox.get("address"):
        document["mailbox"] = {"kind": "iroh-mailbox", "address": mailbox["address"]}
        capabilities.append("mesh-mailbox")
    document["capabilities"] = capabilities
    return document


async def _mesh_pairing(mesh: node_mesh.NodeMesh | None = None) -> dict[str, Any] | None:
    mesh = mesh or node_mesh.current()
    if mesh is None or not mesh.started:
        return None
    status = await mesh.status()
    if status.get("enrolled"):
        return {"state": status["state"], "mesh_id": status["mesh_id"], "member_id": status["member_id"]}
    try:
        offer = await mesh.pairing_offer()
    except node_mesh.NodeMeshError as exc:
        return {"state": status["state"], "error": exc.code}
    return {"state": "offer", **offer}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@node_router.get("/api/node/descriptor")
async def get_descriptor(response: Response):
    _no_store(response)
    return await mesh_descriptor_fields(descriptor())


@node_router.get("/api/node/agent-servers", include_in_schema=False)
async def get_agent_server_policy(request: Request, response: Response):
    node_connection_api.require_loopback(request)
    _no_store(response)
    try:
        await phi_node_policy.refuse_on_phi_node()
    except phi_node_policy.AgentServerUnsupported as refused:
        return {"allowed": False, **refused.detail()}
    return {"allowed": True}


@node_router.get("/.well-known/astral-node/jwks.json", include_in_schema=False)
async def get_node_jwks(response: Response):
    response.headers["Cache-Control"] = "public, max-age=300"
    return _require_node().jwks()


def pairing_gate(request: Request) -> str:
    if not node_identity.is_personal():
        raise HTTPException(404, "node_profile_inactive")
    node_connection_api.require_loopback(request)
    presented = request.headers.getlist(PAIRING_SECRET_HEADER)
    if len(presented) != 1 or not presented[0]:
        raise HTTPException(401, "node.pairing_secret_required", headers=NO_STORE)
    return presented[0]


@node_router.post("/api/node/mesh/offer")
async def mesh_offer(response: Response, secret: str = Depends(pairing_gate)):
    _no_store(response)
    mesh = node_mesh.current()
    if mesh is None or not mesh.started:
        raise HTTPException(503, "mesh_unavailable", headers=NO_STORE)
    try:
        return await mesh.serve_offer(secret)
    except node_mesh.NodeMeshError as exc:
        raise HTTPException(exc.status, exc.code, headers=NO_STORE) from exc


node_router.include_router(node_connection_api.connections_router)
