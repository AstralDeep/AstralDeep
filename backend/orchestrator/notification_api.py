"""Authenticated owner REST boundary for notification routing preferences, quiet
hours, and the durable inbox: Keycloak-authenticated via verify_user, delegating all
policy and persistence decisions to notification_routing.NotificationRoutingCoordinator.
Mounted by orchestrator.py alongside the other routers.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from orchestrator.api import _get_orchestrator
from orchestrator.auth import verify_user
from orchestrator.notification_routing import (
    INBOX_LIST_LIMIT,
    NotificationRoutingError,
)

logger = logging.getLogger("Orchestrator.NotificationAPI")

notification_router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


class PreferenceUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    in_app_completion: bool
    in_app_failure: bool
    in_app_required_action: bool
    inbox_completion: bool
    inbox_failure: bool
    inbox_required_action: bool
    push_completion: bool
    push_failure: bool
    push_required_action: bool


class QuietHoursUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    enabled: bool
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=0, le=1439)
    tz_offset_minutes: int = Field(ge=-840, le=840)


class AcknowledgeRequest(BaseModel):
    expected_revision: int = Field(ge=0)


def _owner_id(payload: dict = Depends(verify_user)) -> str:
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _coordinator(request: Request):
    return _get_orchestrator(request).notification_routing


def _routing_error(exc: NotificationRoutingError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.code)


def _entry_id(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=404, detail="notification_entry_not_found"
        ) from None


@notification_router.get("/preferences")
async def get_preferences(
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    preferences = coordinator.preferences(owner_id)
    quiet = coordinator.quiet_hours(owner_id)
    return {"routing": preferences, "quiet_hours": quiet}


@notification_router.put("/preferences")
async def put_preferences(
    body: PreferenceUpdateRequest,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    try:
        updated = coordinator.update_preferences(
            owner_id,
            expected_revision=body.expected_revision,
            in_app_completion=body.in_app_completion,
            in_app_failure=body.in_app_failure,
            in_app_required_action=body.in_app_required_action,
            inbox_completion=body.inbox_completion,
            inbox_failure=body.inbox_failure,
            inbox_required_action=body.inbox_required_action,
            push_completion=body.push_completion,
            push_failure=body.push_failure,
            push_required_action=body.push_required_action,
        )
    except NotificationRoutingError as exc:
        raise _routing_error(exc) from None
    logger.info(
        "notification_api.preferences_updated",
        extra={"user_id": owner_id, "revision": updated.get("revision")},
    )
    return updated


@notification_router.put("/quiet-hours")
async def put_quiet_hours(
    body: QuietHoursUpdateRequest,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    try:
        updated = coordinator.update_quiet_hours(
            owner_id,
            expected_revision=body.expected_revision,
            enabled=body.enabled,
            start_minute=body.start_minute,
            end_minute=body.end_minute,
            tz_offset_minutes=body.tz_offset_minutes,
        )
    except NotificationRoutingError as exc:
        raise _routing_error(exc) from None
    logger.info(
        "notification_api.quiet_hours_updated",
        extra={"user_id": owner_id, "enabled": body.enabled},
    )
    return updated


@notification_router.get("/inbox")
async def list_inbox(
    request: Request,
    owner_id: str = Depends(_owner_id),
    unacknowledged_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=INBOX_LIST_LIMIT),
) -> list[dict[str, Any]]:
    coordinator = _coordinator(request)
    try:
        return coordinator.list_inbox(
            owner_id,
            limit=limit,
            unacknowledged_only=unacknowledged_only,
        )
    except NotificationRoutingError as exc:
        raise _routing_error(exc) from None


@notification_router.get("/inbox/{entry_id}")
async def get_entry(
    entry_id: str,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    try:
        return coordinator.get_entry(owner_id, _entry_id(entry_id))
    except NotificationRoutingError as exc:
        raise _routing_error(exc) from None


@notification_router.post("/inbox/{entry_id}/acknowledge", status_code=204)
async def acknowledge_entry(
    entry_id: str,
    body: AcknowledgeRequest,
    request: Request,
    owner_id: str = Depends(_owner_id),
) -> None:
    coordinator = _coordinator(request)
    try:
        coordinator.acknowledge(
            owner_id, _entry_id(entry_id), expected_revision=body.expected_revision
        )
    except NotificationRoutingError as exc:
        raise _routing_error(exc) from None
    logger.info(
        "notification_api.entry_acknowledged",
        extra={"user_id": owner_id, "entry_id": entry_id},
    )


@notification_router.delete("/inbox/{entry_id}", status_code=204)
async def delete_entry(
    entry_id: str,
    request: Request,
    owner_id: str = Depends(_owner_id),
    expected_revision: int = Query(ge=0),
) -> None:
    coordinator = _coordinator(request)
    try:
        coordinator.delete_entry(
            owner_id, _entry_id(entry_id), expected_revision=expected_revision
        )
    except NotificationRoutingError as exc:
        raise _routing_error(exc) from None
    logger.info(
        "notification_api.entry_deleted",
        extra={"user_id": owner_id, "entry_id": entry_id},
    )
