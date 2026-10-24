"""REST boundary for owner-bound mobile push: Keycloak-authenticated destination
enrollment, listing, revocation, client acknowledgment, and per-notification
delivery-outcome introspection, composing push_destinations.py and
push_delivery.py. Mounted by orchestrator.py alongside the other routers.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.api import _get_orchestrator
from orchestrator.auth import verify_user
from orchestrator.push_delivery import (
    AckDestinationRevoked,
    AckInvalid,
    AckRejected,
    PushDeliveryError,
)
from orchestrator.push_destinations import PushDestinationError

logger = logging.getLogger("Orchestrator.PushAPI")

push_router = APIRouter(prefix="/api/push", tags=["Push"])

_MAX_OUTCOME_EVENTS = 200
_MAX_OUTCOME_RETURNED = 64


class DestinationEnrollRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=16)
    token: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=64)


class PushAckRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    notification_id: str = Field(min_length=1, max_length=128)


async def _owner_id(payload: dict = Depends(verify_user)) -> str:
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _coordinator(request: Request):
    return _get_orchestrator(request).push_delivery


def _destination_error(exc: PushDestinationError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.code)


def _delivery_error(exc: PushDeliveryError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.code)


@push_router.post("/destinations")
async def enroll_destination(
    body: DestinationEnrollRequest,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    from orchestrator.push_destinations import PROVIDER_FOR_PLATFORM

    provider = PROVIDER_FOR_PLATFORM.get(body.platform)
    if provider is None:
        raise HTTPException(status_code=400, detail="destination_invalid")
    if not coordinator.provider_configured(provider):
        raise HTTPException(
            status_code=503,
            detail="provider_not_configured",
        )
    store = coordinator.destinations
    try:
        destination = store.enroll(
            owner_id,
            device_id=body.device_id,
            platform=body.platform,
            token=body.token,
            label=body.label,
        )
    except PushDestinationError as exc:
        raise _destination_error(exc) from None
    logger.info(
        "push_api.destination_enrolled",
        extra={"user_id": owner_id, "device_id": body.device_id},
    )
    return destination


@push_router.get("/destinations")
async def list_destinations(
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> list[dict[str, Any]]:
    store = _coordinator(request).destinations
    try:
        return store.list_destinations(owner_id)
    except PushDestinationError as exc:
        raise _destination_error(exc) from None


@push_router.get("/destinations/{device_id}")
async def get_destination(
    device_id: str,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> dict[str, Any]:
    store = _coordinator(request).destinations
    try:
        return store.get_destination(owner_id, device_id)
    except PushDestinationError as exc:
        raise _destination_error(exc) from None


@push_router.delete("/destinations/{device_id}", status_code=204)
async def revoke_destination(
    device_id: str,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> None:
    store = _coordinator(request).destinations
    try:
        store.revoke(owner_id, device_id)
    except PushDestinationError as exc:
        raise _destination_error(exc) from None
    logger.info(
        "push_api.destination_revoked",
        extra={"user_id": owner_id, "device_id": device_id},
    )


@push_router.post("/ack", status_code=204)
async def acknowledge_delivery(
    body: PushAckRequest,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> None:
    coordinator = _coordinator(request)
    try:
        await coordinator.acknowledge(owner_id, body.device_id, body.notification_id)
    except (AckInvalid, AckRejected, AckDestinationRevoked) as exc:
        raise _delivery_error(exc) from None


@push_router.get("/deliveries/{notification_id}")
async def delivery_outcomes(
    notification_id: str,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> list[dict[str, Any]]:
    coordinator = _coordinator(request)
    audit = coordinator.audit_repository
    if audit is None:
        raise HTTPException(status_code=503, detail="delivery_outcomes_unavailable")
    try:
        events, _cursor = audit.list_for_user(
            owner_id,
            limit=_MAX_OUTCOME_EVENTS,
            event_classes=["agent_lifecycle"],
        )
    except Exception:
        raise HTTPException(
            status_code=503, detail="delivery_outcomes_unavailable"
        ) from None
    outcomes = []
    for event in events:
        meta = event.inputs_meta or {}
        if not str(event.action_type).startswith("push."):
            continue
        if meta.get("notification_id") != notification_id:
            continue
        outcomes.append(
            {
                "action": event.action_type,
                "outcome": event.outcome,
                "delivery_outcome": meta.get("delivery_outcome"),
                "device_id": meta.get("device_id"),
                "error_code": meta.get("error_code"),
                "attempt": meta.get("attempt"),
                "recorded_at": event.recorded_at.isoformat(),
            }
        )
        if len(outcomes) >= _MAX_OUTCOME_RETURNED:
            break
    return outcomes
