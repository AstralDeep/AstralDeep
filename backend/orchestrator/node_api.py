"""REST surface of a personal node: the public descriptor, the node's JWKS, and the owner-authenticated pairing offer with its one-time short-code lookup.
Pairing builds on mesh_enrollment.create_invitation, and the device then finishes through the existing /api/mesh/enrollment/redeem route.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from orchestrator import mesh_api
from orchestrator import mesh_enrollment as me
from shared import node_identity

node_router = APIRouter(tags=["Node"])

DESCRIPTOR_FORMAT = "astral.node.descriptor/v1"
PAIRING_FORMAT = "astral.pairing/v1"
NODE_VERSION = "astral.node/v1"
CAPABILITIES = (
    "chat",
    "tool-dispatch",
    "mesh-pairing",
    "delegation-eddsa",
    "emergency-stop",
)
QR_PREFIX = "astral-pair:"
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
DEFAULT_OFFER_SECONDS = 600
MIN_OFFER_SECONDS = me.MIN_TTL_SECONDS
MAX_OFFER_SECONDS = 900
MAX_OUTSTANDING_OFFERS = 256
LOOKUP_WINDOW_SECONDS = 60
LOOKUP_MAX_PER_WINDOW = 10
_MAX_TRACKED_CLIENTS = 4096

_lock = threading.Lock()
_offers: dict[str, dict[str, Any]] = {}
_lookups: dict[str, list[float]] = {}


def reset_state() -> None:
    with _lock:
        _offers.clear()
        _lookups.clear()


def _require_personal() -> node_identity.NodeIdentity:
    if not node_identity.is_personal():
        raise HTTPException(404, "node_profile_inactive")
    try:
        return node_identity.load_node_identity()
    except node_identity.NodeIdentityError as exc:
        raise HTTPException(503, "node_key_unavailable") from exc


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def descriptor() -> dict[str, Any]:
    identity = _require_personal()
    owner = node_identity.bound_owner()
    return {
        "format": DESCRIPTOR_FORMAT,
        "version": NODE_VERSION,
        "node_id": identity.node_id,
        "node_key": identity.public_jwk(),
        "accepted_issuers": node_identity.accepted_issuers(),
        "owner_hash": node_identity.owner_principal_hash(owner) if owner else None,
        "capabilities": list(CAPABILITIES),
        "transport_addresses": [],
    }


def _normalize_code(raw: Any) -> str:
    if not isinstance(raw, str) or len(raw) > 32:
        raise HTTPException(400, "pairing_code_malformed")
    code = raw.upper().replace("-", "").replace(" ", "")
    code = code.replace("O", "0").replace("I", "1").replace("L", "1").replace("U", "V")
    if len(code) != CODE_LENGTH or not set(code) <= set(CODE_ALPHABET):
        raise HTTPException(400, "pairing_code_malformed")
    return code


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _purge(now: float) -> None:
    for key in [k for k, offer in _offers.items() if offer["expires_at"] <= now]:
        _offers.pop(key, None)
    if len(_lookups) > _MAX_TRACKED_CLIENTS:
        for key in [
            k for k, hits in _lookups.items() if not hits or now - hits[-1] >= LOOKUP_WINDOW_SECONDS
        ]:
            _lookups.pop(key, None)


def _lookup_allowed(client: str, now: float) -> bool:
    hits = [t for t in _lookups.get(client, []) if now - t < LOOKUP_WINDOW_SECONDS]
    if len(hits) >= LOOKUP_MAX_PER_WINDOW:
        _lookups[client] = hits
        return False
    hits.append(now)
    _lookups[client] = hits
    return True


def pairing_document(identity: node_identity.NodeIdentity, payload: str) -> dict[str, Any]:
    owner = node_identity.bound_owner()
    return {
        "format": PAIRING_FORMAT,
        "node_id": identity.node_id,
        "node_key": identity.public_jwk(),
        "accepted_issuers": node_identity.accepted_issuers(),
        "owner_hash": node_identity.owner_principal_hash(owner) if owner else None,
        "payload": payload,
    }


def qr_payload(document: dict[str, Any]) -> str:
    return QR_PREFIX + _b64url(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@node_router.get("/api/node/descriptor")
async def get_descriptor(response: Response):
    _no_store(response)
    return descriptor()


@node_router.get("/.well-known/astral-node/jwks.json", include_in_schema=False)
async def get_node_jwks(response: Response):
    response.headers["Cache-Control"] = "public, max-age=300"
    return _require_personal().jwks()


@node_router.post("/api/node/pairing")
async def create_pairing(
    request: Request,
    body: dict[str, Any],
    response: Response,
    actor: dict[str, Any] = Depends(mesh_api._read_actor),
    store: me.MeshEnrollmentStore = Depends(mesh_api._store),
    _guard: None = mesh_api._WRITE_GUARD,
):
    _no_store(response)
    identity = _require_personal()
    mesh_api._require_confirm_authority(actor)
    owner_id = actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    bound = node_identity.bound_owner()
    if bound is not None and bound != owner_id:
        raise HTTPException(403, "node_owner_mismatch")
    ttl = body.get("ttl_seconds", DEFAULT_OFFER_SECONDS)
    if type(ttl) is not int or not MIN_OFFER_SECONDS <= ttl <= MAX_OFFER_SECONDS:
        raise HTTPException(400, f"ttl_seconds must be {MIN_OFFER_SECONDS}-{MAX_OFFER_SECONDS}")
    with _lock:
        _purge(time.time())
        if len(_offers) >= MAX_OUTSTANDING_OFFERS:
            raise HTTPException(429, "pairing_capacity")
    try:
        result = await asyncio.to_thread(
            store.create_invitation,
            owner_id,
            label=body.get("label"),
            device_key=body.get("device_key"),
            scopes=body.get("scopes"),
            creator_kind=actor["kind"],
            creator_id=actor["id"],
            ttl_seconds=ttl,
            creator_revision=(
                actor["member"]["updated_at"] if actor["kind"] == "member" else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    if bound is None:
        try:
            await asyncio.to_thread(node_identity.bind_owner, owner_id)
        except node_identity.NodeIdentityError as exc:
            raise HTTPException(503, "node_owner_binding_unavailable") from exc
    document = pairing_document(identity, result["payload"])
    expires_at = result["invitation"]["expires_at"]
    code = _new_code()
    with _lock:
        while _digest(code) in _offers:
            code = _new_code()
        _offers[_digest(code)] = {"document": document, "expires_at": expires_at / 1000}
    return {
        "pairing": {
            "code": display_code(code),
            "expires_at": expires_at,
            "document": document,
            "qr": qr_payload(document),
        },
        "invitation": result["invitation"],
    }


@node_router.post("/api/node/pairing/resolve")
async def resolve_pairing(request: Request, body: dict[str, Any], response: Response):
    _no_store(response)
    _require_personal()
    code = _normalize_code(body.get("code"))
    client = request.client.host if request.client else "unknown"
    now = time.time()
    with _lock:
        _purge(now)
        if not _lookup_allowed(client, now):
            raise HTTPException(429, "pairing_rate_limited")
        offer = _offers.pop(_digest(code), None)
    if offer is None:
        raise HTTPException(404, "pairing_code_unknown")
    return {"pairing": {"document": offer["document"], "qr": qr_payload(offer["document"])}}
