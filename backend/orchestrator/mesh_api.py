"""REST endpoints for personal-mesh enrollment: owner/member-authenticated
invitation and membership controls plus the unauthenticated one-time redemption
endpoint, composing mesh_enrollment.py. Mounted by orchestrator.py.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials

from orchestrator import mesh_enrollment as me
from orchestrator.api import _get_orchestrator
from orchestrator.auth import (
    security,
    verify_user,
)
from orchestrator.plane_repository_context import plane_source_from_orchestrator

mesh_router = APIRouter(prefix="/api/mesh", tags=["Mesh"])

_MEMBER_KEY_HEADER = "x-astral-member-key"


async def _owner_claims_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    if _MEMBER_KEY_HEADER in request.headers:
        return None
    from orchestrator.auth import get_web_or_bearer_user_payload

    return await get_web_or_bearer_user_payload(request, credentials)


async def _read_owner(
    claims: dict = Depends(_owner_claims_optional),
) -> str:
    owner_id = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(owner_id, str) or not owner_id:
        raise HTTPException(401, "Not authenticated")
    await verify_user(claims)
    return owner_id


def _plane_source(request: Request):
    return plane_source_from_orchestrator(_get_orchestrator(request))


def _store(
    request: Request,
    source: Any = Depends(_plane_source),
) -> me.MeshEnrollmentStore:
    return me.MeshEnrollmentStore(source)


def _parse_member_header(request: Request) -> tuple[str, str, str]:
    header = request.headers.get(_MEMBER_KEY_HEADER, "")
    parts = header.split(".")
    if len(parts) != 3 or not all(parts):
        raise HTTPException(401, "Member credential is malformed")
    return parts[0], parts[1], parts[2]


async def _read_actor(
    request: Request,
    owner_claims: dict | None = Depends(_owner_claims_optional),
    store: me.MeshEnrollmentStore = Depends(_store),
) -> dict[str, Any]:
    if owner_claims is not None:
        owner_id = (
            owner_claims.get("sub") if isinstance(owner_claims, dict) else None
        )
        if not isinstance(owner_id, str) or not owner_id:
            raise HTTPException(401, "Not authenticated")
        await verify_user(owner_claims)
        return {"kind": "owner", "id": owner_id, "member": None}
    owner_id, member_id, member_key = _parse_member_header(request)
    try:
        member = store.authenticate_member(owner_id, member_id, member_key)
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return {"kind": "member", "id": member["member_id"], "member": member}


def _require_confirm_authority(actor: dict[str, Any]) -> None:
    if actor["kind"] == "owner":
        return
    member = actor.get("member") or {}
    if me.CONFIRM_SCOPE not in (member.get("scopes") or []):
        raise HTTPException(403, "mesh:confirm scope is required")


def _origin_of(value: str, *, base: bool = False) -> tuple[str, str, int]:
    try:
        if not isinstance(value, str) or value != value.strip():
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.path and not base)
        ):
            raise ValueError
        port = parsed.port
        return (
            parsed.scheme,
            parsed.hostname,
            port if port is not None else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError as exc:
        raise HTTPException(403, "mesh_origin_refused") from exc


def _write_guard(request: Request) -> None:
    if "token" in request.query_params:
        raise HTTPException(403, "mesh_query_token_refused")
    has_body = (request.headers.get("content-length") or "0") not in ("", "0")
    if (
        has_body
        and request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "mesh_json_required")
    authorization = request.headers.get("authorization", "").split(None, 1)
    if len(authorization) == 2 and authorization[0].lower() == "bearer":
        return
    if _MEMBER_KEY_HEADER in request.headers:
        return
    origins = request.headers.getlist("origin")
    if len(origins) != 1:
        raise HTTPException(403, "mesh_origin_refused")
    public_base = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("BACKEND_PUBLIC_URL")
        or str(request.base_url)
    )
    if _origin_of(origins[0]) != _origin_of(public_base, base=True):
        raise HTTPException(403, "mesh_origin_refused")


_WRITE_GUARD = Depends(_write_guard)


@mesh_router.post("/invitations")
async def create_invitation(
    request: Request,
    body: dict[str, Any],
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    _require_confirm_authority(actor)
    owner_id = (
        actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    )
    try:
        result = store.create_invitation(
            owner_id,
            label=body.get("label"),
            device_key=body.get("device_key"),
            scopes=body.get("scopes"),
            creator_kind=actor["kind"],
            creator_id=actor["id"],
            ttl_seconds=body.get("ttl_seconds"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = result["payload"]
    public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL") or ""
    link = f"{public_base.rstrip('/')}/enroll#{payload}" if public_base else None
    me.audit_mesh_event(
        owner_id,
        "mesh.invitation.create",
        f"mesh invitation created for {result['invitation']['label']}",
        meta={
            "invite_id": result["invitation"]["invite_id"],
            "created_by": actor["kind"],
            "fingerprint": result["invitation"]["device_key_fingerprint"],
        },
    )
    return {"invitation": result["invitation"], "payload": payload, "link": link}


@mesh_router.get("/invitations")
async def list_invitations(
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
):
    _require_confirm_authority(actor)
    owner_id = (
        actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    )
    return {"invitations": store.list_invitations(owner_id)}


@mesh_router.post("/invitations/{invite_id}/confirm")
async def confirm_invitation(
    invite_id: str,
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    _require_confirm_authority(actor)
    owner_id = (
        actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    )
    try:
        record = store.decide_invitation(
            owner_id,
            invite_id,
            decision="confirmed",
            decider_kind=actor["kind"],
            decider_id=actor["id"],
        )
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    me.audit_mesh_event(
        owner_id,
        "mesh.invitation.confirm",
        f"mesh invitation {invite_id} confirmed by {actor['kind']}",
        meta={
            "invite_id": invite_id,
            "confirmed_by": actor["kind"],
            "member_id": actor["id"] if actor["kind"] == "member" else None,
        },
    )
    return {"invitation": me.public_invitation(record)}


@mesh_router.post("/invitations/{invite_id}/reject")
async def reject_invitation(
    invite_id: str,
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    _require_confirm_authority(actor)
    owner_id = (
        actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    )
    try:
        record = store.decide_invitation(
            owner_id,
            invite_id,
            decision="rejected",
            decider_kind=actor["kind"],
            decider_id=actor["id"],
        )
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    me.audit_mesh_event(
        owner_id,
        "mesh.invitation.reject",
        f"mesh invitation {invite_id} rejected by {actor['kind']}",
        meta={"invite_id": invite_id},
    )
    return {"invitation": me.public_invitation(record)}


@mesh_router.get("/members")
async def list_members(
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
):
    owner_id = (
        actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    )
    return {"members": store.list_members(owner_id)}


@mesh_router.post("/members/{member_id}/revoke")
async def revoke_member(
    member_id: str,
    request: Request,
    owner_id: str = Depends(_read_owner),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    try:
        record = store.revoke_member(owner_id, member_id)
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    me.audit_mesh_event(
        owner_id,
        "mesh.member.revoke",
        f"mesh member {record['label']} revoked",
        meta={"member_id": member_id},
    )
    return {"member": me.public_member(record)}


@mesh_router.get("/me")
async def member_self(
    request: Request,
    store: me.MeshEnrollmentStore = Depends(_store),
):
    owner_id, member_id, member_key = _parse_member_header(request)
    try:
        member = store.authenticate_member(owner_id, member_id, member_key)
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return {"member": me.public_member(member)}


@mesh_router.post("/enrollment/redeem")
async def redeem_enrollment(
    request: Request,
    body: dict[str, Any],
    response: Response,
    store: me.MeshEnrollmentStore = Depends(_store),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        parsed = me.parse_enrollment_payload(body.get("payload"))
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    signature = body.get("signature")
    if not isinstance(signature, str) or not signature or len(signature) > 256:
        raise HTTPException(400, "signature is malformed")
    try:
        result = store.redeem_invitation(
            owner_id=parsed["owner_id"],
            invite_id=parsed["invite_id"],
            token=parsed["token"],
            challenge=parsed["challenge"],
            signature=signature,
        )
    except me.MeshEnrollmentError as exc:
        me.audit_mesh_event(
            parsed["owner_id"],
            "mesh.member.redeem",
            f"mesh redemption denied: {exc.code}",
            outcome="failure",
            meta={"invite_id": parsed["invite_id"], "reason": exc.code},
        )
        raise HTTPException(exc.status, exc.code) from exc
    member = result["member"]
    me.audit_mesh_event(
        member["owner_id"],
        "mesh.member.redeem",
        f"mesh member {member['label']} activated",
        meta={
            "invite_id": parsed["invite_id"],
            "member_id": member["member_id"],
            "fingerprint": member["device_key_fingerprint"],
        },
    )
    return {
        "member": me.public_member(member),
        "member_key": result["member_key"],
    }
