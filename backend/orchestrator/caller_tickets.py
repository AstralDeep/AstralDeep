"""Verifies the owner-minted caller ticket and holder proof a script or outside agent presents to a personal node, through the Kernel's verify_caller_request off the event loop, and turns the verified caller into the claims the MCP and A2A doors dispatch under.
mcp_authz.py and a2a_orchestrator_executor.py call it, node_mesh.py runs the Kernel call and its development-mesh refusal, and one process verifies for a node because the replay store is not shared across processes."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Dict, Optional, Tuple

from starlette.requests import Request

from orchestrator import node_mesh
from shared import node_identity

BEARER_REFUSED = "node.bearer_refused"
MESH_UNAVAILABLE = "node.mesh_unavailable"
OWNER_UNBOUND = "node.owner_unbound"
TOOL_UNAVAILABLE = "caller.tool_unavailable"
TOOL_CHANGED = "caller.tool_changed"
TOOL_NOT_COVERED = "caller.tool_not_covered"
MESH_MISMATCH = "caller.mesh_mismatch"
A2A_MESSAGE = "a2a.message"
ISSUER = "astral-caller"
PROOF_HEADER = "astral-caller-proof"
CALLER_ID = "_caller_id"
CALLER_LABEL = "_caller_label"
CALLER_TOOLS = "_caller_tools"
QUERY_TOKENS = frozenset({"token", "access_token"})

UNAUTHORIZED = frozenset({
    "caller.malformed", "caller.proof_malformed", "caller.bad_signature", "caller.proof_bad_signature",
})
FORBIDDEN = frozenset({
    "caller.mesh_mismatch", "caller.wrong_node", "caller.device_removed", "caller.wrong_minter",
    "caller.stopped", "caller.expired", "caller.not_yet_valid", "caller.revoked", "caller.proof_mismatch",
    "caller.proof_stale", "caller.replayed", "caller.tool_not_covered", TOOL_CHANGED, TOOL_UNAVAILABLE,
})
UNAVAILABLE = frozenset({"caller.replay_full", "mesh.not_enrolled", node_mesh.DEVELOPMENT_MESH})


class CallerRefused(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


REASONS = {
    BEARER_REFUSED: "This node does not accept bearer tokens. Send an owner-minted caller ticket in the Authorization header with the astral-caller-proof header, and never put a token in the URL.",
    MESH_UNAVAILABLE: "The node's mesh is not running yet. Retry shortly, or ask the owner to check the node.",
    OWNER_UNBOUND: "This node has no owner bound to it. The owner must pair it in the AstralDeep app before any caller can use it.",
    MESH_MISMATCH: "The ticket was minted for a different mesh. Ask the owner to mint a ticket for this node.",
    TOOL_UNAVAILABLE: "The tool is not published by this node or is not available to this caller. List the node's tools and call one of those.",
    TOOL_CHANGED: "The tool's definition changed after the ticket was minted. Ask the owner to mint a new ticket.",
    TOOL_NOT_COVERED: "The ticket does not cover this tool or method. Ask the owner to mint a ticket that includes it.",
    "caller.malformed": "The ticket or request is malformed, or the body is over the size limit. Send the ticket text exactly as minted and keep the body under 1 MiB.",
    "caller.proof_malformed": "The holder proof is missing or malformed. Sign every request with the caller key and send it in the astral-caller-proof header.",
    "caller.bad_signature": "The ticket signature does not verify. Ask the owner to mint a new ticket.",
    "caller.proof_bad_signature": "The holder proof was not signed by the ticket's holder key. Use the caller key whose public holder key the owner minted the ticket for.",
    "caller.wrong_node": "The ticket was minted for another node. Ask the owner to mint one for this node.",
    "caller.device_removed": "The device that minted the ticket was removed from the mesh. Ask the owner to mint a new ticket.",
    "caller.wrong_minter": "The ticket was not minted by a device that may mint caller tickets. Ask the owner to mint it from their own device.",
    "caller.stopped": "The owner stopped caller access. Wait for the owner to lift the stop, then retry.",
    "caller.expired": "The ticket has expired. Ask the owner to mint a new one.",
    "caller.not_yet_valid": "The ticket is not valid yet. Check this machine's clock, then retry.",
    "caller.revoked": "The owner revoked this ticket. Ask the owner to mint a new one.",
    "caller.proof_mismatch": "The holder proof does not match this request's method, path and body. Build a fresh proof for each request.",
    "caller.proof_stale": "The holder proof is too old. Build a fresh proof for each request and check this machine's clock.",
    "caller.replayed": "This holder proof was already used. Build a fresh proof for each request.",
    "caller.replay_full": "The node is busy checking proofs. Retry shortly.",
    node_mesh.DEVELOPMENT_MESH: "This node runs on a development mesh, which never accepts caller tickets. Pair it with a real owner mesh first.",
    "mesh.not_enrolled": "The node has not joined a mesh yet. The owner must pair it in the AstralDeep app first.",
}
DEFAULT_REASON = "The node refused the caller. Ask the owner to mint a new caller ticket for this node."


def reason_for(code: str) -> str:
    if code.startswith("gate."):
        return "A node safety gate refused the call. Ask the owner to review the node's approvals."
    return REASONS.get(code, DEFAULT_REASON)


def status_for(code: str) -> int:
    if code in UNAUTHORIZED or code == BEARER_REFUSED:
        return 401
    if code in FORBIDDEN:
        return 403
    if code in UNAVAILABLE or code.startswith("gate.") or code in {MESH_UNAVAILABLE, OWNER_UNBOUND}:
        return 503
    return 500


def refusal(code: str) -> CallerRefused:
    return CallerRefused(code, status_for(code))


def request_target(request: Request) -> str:
    raw = request.scope.get("raw_path")
    path = raw.decode("latin-1") if isinstance(raw, bytes) and raw else request.url.path
    query = request.scope.get("query_string") or b""
    return f"{path}?{query.decode('latin-1')}" if query else path


def _presented(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if not authorization or any(key.lower() in QUERY_TOKENS for key in request.query_params):
        raise refusal(BEARER_REFUSED)
    if authorization.partition(" ")[0].lower() == "bearer":
        raise refusal(BEARER_REFUSED)
    return authorization


async def _bound(mesh: Any) -> str:
    try:
        owner = await asyncio.to_thread(node_identity.bound_mesh_owner)
    except node_identity.NodeIdentityError:
        raise refusal(OWNER_UNBOUND) from None
    if owner is None or mesh.personal_owner_id() != owner:
        raise refusal(OWNER_UNBOUND)
    return owner


def claims_for(owner: str, verified: Any, audience: str) -> Dict[str, Any]:
    return {
        "sub": owner,
        "iss": ISSUER,
        "aud": audience,
        "realm_access": {"roles": ["user"]},
        CALLER_ID: verified.id,
        CALLER_LABEL: verified.label,
        CALLER_TOOLS: tuple(verified.tools or ()),
    }


async def verify(request: Request, body: bytes, *, tool: Optional[str], audience: str) -> Dict[str, Any]:
    authorization = _presented(request)
    mesh = node_mesh.current()
    if mesh is None or not mesh.started:
        raise refusal(MESH_UNAVAILABLE)
    owner = await _bound(mesh)
    try:
        verified = await mesh.verify_caller(
            authorization=authorization,
            proof=request.headers.get(PROOF_HEADER),
            method=request.method,
            target=request_target(request),
            body_sha256="sha256:" + hashlib.sha256(body).hexdigest(),
            tool=tool,
        )
    except node_mesh.MeshUnavailable:
        raise refusal(MESH_UNAVAILABLE) from None
    except node_mesh.NodeMeshError as exc:
        raise refusal(exc.code) from None
    if verified.mesh != node_identity.mesh_id_for_owner(owner):
        raise refusal(MESH_MISMATCH)
    return claims_for(owner, verified, audience)


def covered(claims: Dict[str, Any]) -> Optional[Tuple[str, ...]]:
    return claims.get(CALLER_TOOLS) if claims.get(CALLER_ID) else None


async def read_bounded(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > limit):
        raise refusal("caller.malformed")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise refusal("caller.malformed")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # why: the A2A SDK reads this same request next
    return body


async def dispatch_refusal(orchestrator: Any, claims: Dict[str, Any], projected: Any) -> Optional[str]:
    from orchestrator.node_mesh_calls import OrchestratorCalls

    mesh = node_mesh.current()
    if projected is None or mesh is None:
        return TOOL_UNAVAILABLE
    # why: the kernel covers the name the caller sent, so a collision-qualified projected name can never match a published tool
    entry = (await asyncio.to_thread(OrchestratorCalls(orchestrator).catalog, claims["sub"])).get(
        (projected.agent_id, projected.skill_id)
    )
    if entry is None:
        return TOOL_UNAVAILABLE
    return None if mesh.published_definition(projected.skill_id) == entry.definition else TOOL_CHANGED
