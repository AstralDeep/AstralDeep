"""Exposes owner-authenticated publisher mesh registration and capability-only empty wake requests.
Bounded strict bodies contain no conversation content; ordinary push session policy remains in push_api.py."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.api import _get_orchestrator
from orchestrator.auth import verify_user
from orchestrator.mesh_push_registry import MAX_REVISION, MeshPushError
from shared import node_identity

mesh_push_router = APIRouter(prefix="/api/push/mesh", tags=["Mesh push"])


class WakeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mesh_id: str = Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$", max_length=71)
    device_id: str = Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$", max_length=71)


class WakeRegistration(WakeTarget):
    token: str = Field(strict=True, min_length=1, max_length=4096)
    expected_revision: int = Field(strict=True, ge=0, le=MAX_REVISION)


class WakeRevocation(WakeTarget):
    expected_revision: int = Field(strict=True, ge=1, le=MAX_REVISION)


async def _owner(principal: dict = Depends(verify_user)) -> str:
    raise HTTPException(401, "Not authenticated")


def _publisher(request: Request):
    if node_identity.is_personal():
        raise HTTPException(503, "publisher_unavailable")
    publisher = getattr(_get_orchestrator(request), "mesh_push_publisher", None)
    if publisher is None or not publisher.available:
        raise HTTPException(503, "publisher_unavailable")
    return publisher


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def _body(request: Request, schema):
    chunks = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > 8192:
            raise HTTPException(413, "request_too_large")
        chunks.append(part)
    try:
        value = json.loads(b"".join(chunks), object_pairs_hook=_object)
        return schema.model_validate(value)
    except (ValueError, UnicodeError, ValidationError):
        raise HTTPException(422, "request_invalid") from None


def _failure(error: MeshPushError):
    return HTTPException(error.status, error.code)


@mesh_push_router.post("/destinations")
async def register(request: Request, response: Response, owner: str = Depends(_owner)):
    response.headers["Cache-Control"] = "no-store"
    body = await _body(request, WakeRegistration)
    publisher = _publisher(request)
    try:
        registration = await asyncio.to_thread(publisher.registry.register, owner, **body.model_dump())
    except MeshPushError as error:
        raise _failure(error) from None
    return {"mesh_id": registration.mesh_id, "device_id": registration.device_id, "revision": registration.revision,
            "route": {"publisher": registration.publisher, "capability": registration.capability,
                      "expires_at_ms": registration.expires_at_ms}}


@mesh_push_router.get("/destinations")
async def status(request: Request, response: Response, owner: str = Depends(_owner)):
    response.headers["Cache-Control"] = "no-store"
    try:
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise ValueError
        body = WakeTarget.model_validate(dict(request.query_params))
    except (ValueError, ValidationError):
        raise HTTPException(422, "request_invalid") from None
    publisher = _publisher(request)
    try:
        return asdict(await asyncio.to_thread(publisher.registry.status, owner, **body.model_dump()))
    except MeshPushError as error:
        raise _failure(error) from None


@mesh_push_router.delete("/destinations")
async def revoke(request: Request, response: Response, owner: str = Depends(_owner)):
    response.headers["Cache-Control"] = "no-store"
    body = await _body(request, WakeRevocation)
    publisher = _publisher(request)
    try:
        return asdict(await asyncio.to_thread(publisher.registry.revoke, owner, **body.model_dump()))
    except MeshPushError as error:
        raise _failure(error) from None


@mesh_push_router.post("/wake", status_code=204)
async def wake(request: Request):
    body = await _body(request, WakeTarget)
    publisher = _publisher(request)
    caller = request.client.host if request.client else "unknown"
    if not publisher.limiter.admit(caller):
        raise HTTPException(429, "rate_limited")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or not 32 <= len(authorization) - 7 <= 2048:
        raise HTTPException(403, "capability_invalid")
    try:
        await publisher.wake(authorization[7:], **body.model_dump())
    except MeshPushError as error:
        raise _failure(error) from None
    return Response(status_code=204)
