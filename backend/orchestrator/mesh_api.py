"""REST endpoints for personal-mesh enrollment: owner/member-authenticated
invitation and membership controls plus the unauthenticated one-time redemption
endpoint, composing mesh_enrollment.py. Mounted by orchestrator.py.
"""

from __future__ import annotations

import os
import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials

from orchestrator import mesh_enrollment as me
from orchestrator import mesh_admission as ma
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
        raise HTTPException(
            401, "Keycloak or possession-bound delegated identity required"
        )
    if credentials is not None:
        from orchestrator.delegation import decode_token_payload

        unverified = decode_token_payload(credentials.credentials)
        if isinstance(unverified, dict) and ma.MEMBER_CLAIM in unverified:
            return None
    authorization = request.headers.get("authorization", "").split(None, 1)
    if len(authorization) == 2 and authorization[0].lower() == "dpop":
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
    return me.MeshEnrollmentStore(
        source, audit_repository=getattr(_get_orchestrator(request), "audit_repo", None)
    )


def _admission(request: Request, store: me.MeshEnrollmentStore = Depends(_store)):
    return ma.MeshAdmission(_get_orchestrator(request), store=store)


def _member_token(request: Request):
    authorization = request.headers.getlist("authorization")
    if len(authorization) != 1:
        raise ma.MeshAdmissionError()
    parts = authorization[0].split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "dpop":
        raise ma.MeshAdmissionError()
    return parts[1]


async def _prepare_iam(service, owner_id, record, agent_id):
    try:
        async with asyncio.timeout(15):
            return await service.prepare_iam(owner_id, record, agent_id)
    except TimeoutError:
        raise ma.MeshIAMUnavailable() from None


async def _read_actor(
    request: Request,
    owner_claims: dict | None = Depends(_owner_claims_optional),
    store: me.MeshEnrollmentStore = Depends(_store),
) -> dict[str, Any]:
    if owner_claims is not None:
        owner_id = owner_claims.get("sub") if isinstance(owner_claims, dict) else None
        if not isinstance(owner_id, str) or not owner_id:
            raise HTTPException(401, "Not authenticated")
        await verify_user(owner_claims)
        return {"kind": "owner", "id": owner_id, "member": None, "claims": owner_claims}
    try:
        service = ma.MeshAdmission(_get_orchestrator(request), store=store)
        token = _member_token(request)
        claims = ma.verify_member_token(token)
        proofs, nonces = (
            request.headers.getlist("dpop"),
            request.headers.getlist("dpop-nonce"),
        )
        if len(proofs) != 1 or len(nonces) != 1:
            raise ma.MeshAdmissionError()

        def authenticate():
            with store._transaction() as transaction:
                member = service.consume_proof(
                    transaction,
                    claims["sub"],
                    claims[ma.MEMBER_CLAIM]["member_id"],
                    proofs[0],
                    nonces[0],
                    method=request.method,
                    path=request.url.path,
                    access_token=token,
                    claims=claims,
                )
                store._audit_transition(
                    transaction,
                    claims["sub"],
                    "mesh.member.authenticate",
                    actor_kind="member",
                    actor_id=member["member_id"],
                    meta={"member_id": member["member_id"], "method": request.method},
                )
                service.current(transaction, claims)
                return member

        member = await asyncio.to_thread(authenticate)
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return {
        "kind": "member",
        "id": member["member_id"],
        "member": member,
        "claims": claims,
    }


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
    if len(authorization) == 2 and authorization[0].lower() in {"bearer", "dpop"}:
        return
    if _MEMBER_KEY_HEADER in request.headers:
        raise HTTPException(
            401, "Keycloak or possession-bound delegated identity required"
        )
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
    owner_id = actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    try:
        result = await asyncio.to_thread(
            store.create_invitation,
            owner_id,
            label=body.get("label"),
            device_key=body.get("device_key"),
            scopes=body.get("scopes"),
            creator_kind=actor["kind"],
            creator_id=actor["id"],
            ttl_seconds=body.get("ttl_seconds"),
            creator_revision=(
                actor["member"]["updated_at"] if actor["kind"] == "member" else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    payload = result["payload"]
    public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL") or ""
    link = f"{public_base.rstrip('/')}/enroll#{payload}" if public_base else None
    return {"invitation": result["invitation"], "payload": payload, "link": link}


@mesh_router.get("/invitations")
async def list_invitations(
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
):
    _require_confirm_authority(actor)
    owner_id = actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    return {"invitations": await asyncio.to_thread(store.list_invitations, owner_id)}


@mesh_router.post("/invitations/{invite_id}/confirm")
async def confirm_invitation(
    invite_id: str,
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    _require_confirm_authority(actor)
    owner_id = actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    try:
        custody, consent = None, None
        if actor["kind"] == "owner":
            custody, consent = await ma.MeshAdmission(
                _get_orchestrator(request), store=store
            ).owner_custody(request, actor["claims"])
        record = await asyncio.to_thread(
            store.decide_invitation,
            owner_id,
            invite_id,
            decision="confirmed",
            decider_kind=actor["kind"],
            decider_id=actor["id"],
            decider_revision=(
                actor["member"]["updated_at"] if actor["kind"] == "member" else None
            ),
            custody=custody,
            consent_observation=consent,
        )
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
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
    owner_id = actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    try:
        record = await asyncio.to_thread(
            store.decide_invitation,
            owner_id,
            invite_id,
            decision="rejected",
            decider_kind=actor["kind"],
            decider_id=actor["id"],
            decider_revision=(
                actor["member"]["updated_at"] if actor["kind"] == "member" else None
            ),
        )
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return {"invitation": me.public_invitation(record)}


@mesh_router.get("/members")
async def list_members(
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
    store: me.MeshEnrollmentStore = Depends(_store),
):
    owner_id = actor["member"]["owner_id"] if actor["kind"] == "member" else actor["id"]
    return {"members": await asyncio.to_thread(store.list_members, owner_id)}


@mesh_router.post("/members/{member_id}/revoke")
async def revoke_member(
    member_id: str,
    request: Request,
    owner_id: str = Depends(_read_owner),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    try:
        record = await asyncio.to_thread(store.revoke_member, owner_id, member_id)
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return {"member": me.public_member(record)}


@mesh_router.post("/members/{member_id}/label")
async def rename_member(
    member_id: str,
    request: Request,
    body: dict[str, Any],
    owner_id: str = Depends(_read_owner),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    label = body.get("label") if isinstance(body, dict) else None
    if not isinstance(label, str):
        raise HTTPException(400, "label is required")
    try:
        record = await asyncio.to_thread(
            store.rename_member, owner_id, member_id, label=label
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return {"member": me.public_member(record)}


@mesh_router.post("/invitations/{invite_id}/remove")
async def remove_invitation(
    invite_id: str,
    request: Request,
    owner_id: str = Depends(_read_owner),
    store: me.MeshEnrollmentStore = Depends(_store),
    _guard: None = _WRITE_GUARD,
):
    try:
        await asyncio.to_thread(store.remove_invitation, owner_id, invite_id)
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return {"removed": invite_id}


@mesh_router.get("/me")
async def member_self(
    request: Request,
    actor: dict[str, Any] = Depends(_read_actor),
):
    if actor["kind"] != "member":
        raise HTTPException(403, "member identity required")
    return {"member": me.public_member(actor["member"])}


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
        service = ma.MeshAdmission(_get_orchestrator(request), store=store)
        record = await asyncio.to_thread(
            store.prepare_redemption,
            owner_id=parsed["owner_id"],
            invite_id=parsed["invite_id"],
            token=parsed["token"],
            challenge=parsed["challenge"],
            signature=signature,
        )
        prepared = await _prepare_iam(
            service, parsed["owner_id"], record, body.get("agent_id")
        )

        def activate():
            with store._transaction() as transaction:
                store._lock_mesh(transaction, parsed["owner_id"])
                service.runtime.repositories.history.sessions.assert_current_execution(
                    transaction, observation=prepared.observation
                )
                result = store.redeem_invitation(
                    owner_id=parsed["owner_id"],
                    invite_id=parsed["invite_id"],
                    token=parsed["token"],
                    challenge=parsed["challenge"],
                    signature=signature,
                    transaction=transaction,
                )
                session = service.mint(transaction, prepared, result["member"])
                if me._now_ms() >= record["expires_at"]:
                    raise me.InvitationExpired(parsed["invite_id"])
                return session

        session = await asyncio.to_thread(activate)
    except me.MeshEnrollmentError as exc:
        try:
            await asyncio.to_thread(
                store.record_redemption_denial,
                parsed["owner_id"],
                parsed["invite_id"],
                exc.code,
            )
        except me.MeshEnrollmentError as audit_exc:
            raise HTTPException(audit_exc.status, audit_exc.code) from audit_exc
        raise HTTPException(exc.status, exc.code) from exc
    return session


@mesh_router.post("/nonce")
async def member_nonce(
    request: Request,
    body: dict[str, Any],
    response: Response,
    service=Depends(_admission),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await asyncio.to_thread(
            service.nonce, body.get("owner_id"), body.get("member_id")
        )
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc


@mesh_router.post("/session")
async def member_session(
    request: Request,
    body: dict[str, Any],
    response: Response,
    service=Depends(_admission),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        owner_id, member_id = body.get("owner_id"), body.get("member_id")

        def possess():
            with service.store._transaction() as transaction:
                return service.consume_proof(
                    transaction,
                    owner_id,
                    member_id,
                    body.get("proof"),
                    body.get("nonce"),
                    method="POST",
                    path=request.url.path,
                )

        member = await asyncio.to_thread(possess)
        prepared = await _prepare_iam(service, owner_id, member, body.get("agent_id"))

        def issue():
            with service.store._transaction() as transaction:
                return service.mint(transaction, prepared, member)

        return await asyncio.to_thread(issue)
    except me.MeshEnrollmentError as exc:
        raise HTTPException(exc.status, exc.code) from exc


@mesh_router.post("/tools/{agent_id}/{tool_name}")
async def invoke_member_tool(
    agent_id: str,
    tool_name: str,
    request: Request,
    body: dict[str, Any],
    actor=Depends(_read_actor),
):
    if actor["kind"] != "member":
        raise HTTPException(403, "member identity required")
    claims = actor["claims"]
    if claims[ma.MEMBER_CLAIM]["agent_id"] != agent_id:
        raise HTTPException(403, ma.MeshAdmissionError.code)
    arguments = body.get("arguments")
    if not isinstance(arguments, dict) or set(body) != {"arguments"}:
        raise HTTPException(400, "arguments object required")
    result = await _get_orchestrator(request).execute_authorized_tool(
        claims=claims,
        user_id=claims["sub"],
        agent_id=agent_id,
        tool_name=tool_name,
        arguments=arguments,
        channel="rest",
    )
    return json.loads(result.to_json())
