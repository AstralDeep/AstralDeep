"""Builds the first-connection guide payload: server-attested endpoint identity plus
honest database, authentication, worker, and optional-service checks with actionable
remediation, and pushes the connection surface at registration when a required service
is unready. Consumed by projection_surfaces/connection.py, orchestrator.py, and the
/api/connection/status route defined here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from orchestrator.auth import get_current_user_payload

logger = logging.getLogger("Orchestrator.ConnectionGuide")

SURFACE_KEY = "connection"
CONTRACT = "astraldeep.connection-guide/v1"

STATUS_READY = "ready"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_OFF = "off"

REQUIRED_KEYS = ("database", "auth", "publication")

SIGN_IN_TITLE = "Sign in and connect your model"
SIGN_IN_STEPS = (
    "Open this server's published address and complete sign-in yourself on its own "
    "sign-in page — never enter your credentials anywhere else.",
    "Connect your AI provider in the encrypted in-product LLM setup when prompted; "
    "the key is stored per user and encrypted at rest, and never read from the "
    "environment.",
    "Agents never see or type your provider keys; they run under their own delegated "
    "authority.",
)

connection_router = APIRouter(tags=["System"])


def _check(key: str, label: str, status: str, detail: str, remediation: str = "",
           required: bool = False) -> dict[str, str | bool]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "remediation": remediation,
        "required": required,
    }


def _origin_of(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}", scheme="http")
        scheme = (parts.scheme or "http").lower()
        host = (parts.hostname or "").lower()
        if not host:
            return ""
        port = parts.port
    except ValueError:
        return ""
    if port is None:
        port = 443 if scheme == "https" else 80
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    return f"{scheme}://{host}" if default_port else f"{scheme}://{host}:{port}"


def configured_public_endpoints() -> list[str]:
    out: list[str] = []
    for name in ("PUBLIC_BASE_URL", "BACKEND_PUBLIC_URL"):
        origin = _origin_of(os.getenv(name, ""))
        if origin and origin not in out:
            out.append(origin)
    return out


def endpoint_report(connected_origin: Optional[str] = None) -> dict[str, Any]:
    configured = configured_public_endpoints()
    connected = _origin_of(connected_origin or "")
    verified = bool(configured and connected and connected in configured)
    if verified:
        guidance = (
            f"You are connected to {connected}, an address this server publishes. "
            "Complete sign-in only on this server's own sign-in page."
        )
    elif connected and configured:
        guidance = (
            f"This server publishes {', '.join(configured)}, but the connection "
            f"arrived from {connected}. Connect using the published address before "
            "signing in."
        )
    elif configured:
        guidance = (
            f"This server publishes {', '.join(configured)}. Connect to that address "
            "and complete sign-in only on its own sign-in page."
        )
    else:
        guidance = (
            "No public endpoint is configured (PUBLIC_BASE_URL / BACKEND_PUBLIC_URL). "
            "Connect to the address your operator published — the Compose default is "
            "http://localhost:8001 — and complete sign-in only on that server's own "
            "sign-in page."
        )
    return {
        "connected": connected,
        "configured": configured,
        "verified": verified,
        "guidance": guidance,
    }


def _database_check(orch) -> dict[str, str | bool]:
    try:
        orch._probe_plane_readiness()
    except Exception:
        logger.warning("connection guide: database readiness probe failed", exc_info=True)
        return _check(
            "database", "Database", STATUS_UNAVAILABLE,
            "The database is not answering readiness probes, so conversations, "
            "settings, and audit records cannot be stored.",
            "Verify PostgreSQL is running and reachable (docker compose ps postgres, "
            "make logs), then restart the app.",
            required=True,
        )
    return _check(
        "database", "Database", STATUS_READY,
        "Database migrations are current and durable storage is answering.", "",
        required=True,
    )


async def _keycloak_reachable(authority: str) -> bool:
    from orchestrator.web_auth import _idp_reachable

    return bool(await _idp_reachable(authority))


async def _auth_check(orch) -> dict[str, str | bool]:
    from orchestrator.web_auth import _is_mock, _keycloak_config

    if _is_mock():
        return _check(
            "auth", "Authentication", STATUS_READY,
            "Development mock authentication is active; it is refused outside "
            "development and never used in production.", "",
            required=True,
        )
    authority, _client_id, _secret = _keycloak_config()
    if not authority:
        return _check(
            "auth", "Authentication", STATUS_UNAVAILABLE,
            "No identity provider is configured, so sign-in cannot complete.",
            "Set KEYCLOAK_AUTHORITY and the Keycloak client settings, then restart "
            "the app.",
            required=True,
        )
    if not await _keycloak_reachable(authority):
        return _check(
            "auth", "Authentication", STATUS_UNAVAILABLE,
            f"The identity provider at {authority} is unreachable.",
            "Verify the Keycloak URL and network path; first sign-in cannot complete "
            "until it is reachable.",
            required=True,
        )
    return _check(
        "auth", "Authentication", STATUS_READY,
        f"Sign-in is served by the configured identity provider at {authority}.", "",
        required=True,
    )


def _worker_check(orch) -> dict[str, str | bool]:
    from shared.feature_flags import flags

    if not flags.is_enabled("conversational_voice"):
        return _check(
            "worker", "Voice workers", STATUS_OFF,
            "Conversational voice is disabled on this deployment; typed chat is "
            "unaffected.",
            "Optional: enable FF_CONVERSATIONAL_VOICE and start the voice-worker "
            "service for voice sessions.",
        )
    services = getattr(orch, "voice_services", None)
    if services is None:
        return _check(
            "worker", "Voice workers", STATUS_UNAVAILABLE,
            "Voice is enabled but its runtime failed to start on this server.",
            "Check the app startup logs for voice bootstrap failures and restart the "
            "app.",
        )
    selection = getattr(services, "backend_selection", None)
    if selection is not None and not getattr(selection, "valid", True):
        return _check(
            "worker", "Voice workers", STATUS_UNAVAILABLE,
            "The voice speech backend selection is invalid, so voice fails closed; "
            "typed chat is unaffected.",
            "Set VOICE_SPEECH_BACKEND to llm_factory or client_local and restart the "
            "app.",
        )
    if selection is not None and getattr(selection, "value", None) == "client_local":
        return _check(
            "worker", "Voice workers", STATUS_OFF,
            "Voice uses each client's local speech runtime; no server voice worker "
            "is required.",
        )
    pool = getattr(services, "worker_pool", None)
    if pool is None:
        return _check(
            "worker", "Voice workers", STATUS_UNAVAILABLE,
            "Voice is enabled with the server speech backend but no worker pool "
            "started.",
            "Check the app startup logs for voice bootstrap failures and restart the "
            "app.",
        )
    readiness = pool.readiness()
    if readiness.ready:
        return _check(
            "worker", "Voice workers", STATUS_READY,
            f"{readiness.worker_count} voice worker(s) connected; "
            f"{readiness.capacity_available} of {readiness.capacity_total} session "
            "slots available.",
        )
    if readiness.reason == "capacity_exhausted":
        return _check(
            "worker", "Voice workers", STATUS_DEGRADED,
            "Voice workers are connected but at session capacity.",
            "Wait for an active voice session to end or start another voice worker.",
        )
    return _check(
        "worker", "Voice workers", STATUS_DEGRADED,
        "Voice is enabled but no voice worker is connected.",
        "Start the voice-worker service (docker compose up voice-worker) and check "
        "its logs.",
    )


async def _publication_check(orch) -> dict[str, str | bool]:
    service = getattr(orch, "generated_agent_publication_service", None)
    if service is None:
        return _check(
            "publication", "Agent publication", STATUS_UNAVAILABLE,
            "The generated-agent publication service is missing on this server.",
            "Restart the app; publication recovery must complete for normal "
            "readiness.",
            required=True,
        )
    try:
        publication = await service.readiness()
    except Exception:
        logger.warning("connection guide: publication readiness probe failed",
                       exc_info=True)
        publication = None
    if publication is not None and getattr(publication, "ready", False):
        return _check(
            "publication", "Agent publication", STATUS_READY,
            "Generated-agent publication is recovered and current.", "",
            required=True,
        )
    return _check(
        "publication", "Agent publication", STATUS_UNAVAILABLE,
        "Generated-agent publication recovery is unresolved.",
        "Restart the app and check startup logs; publication must complete before "
        "normal readiness.",
        required=True,
    )


def _lets_check(orch) -> dict[str, str | bool]:
    from orchestrator.lets_health_api import lets_snapshot

    try:
        snapshot = lets_snapshot(orch)
    except Exception:
        logger.warning("connection guide: LETS posture read failed", exc_info=True)
        return _check(
            "lets", "LETS warden", STATUS_DEGRADED,
            "The LETS posture could not be read.",
            "Check LETS configuration; governed effects fail closed while the "
            "posture is unreadable.",
        )
    if snapshot.mode == "off" or snapshot.component_status == "disabled":
        return _check(
            "lets", "LETS warden", STATUS_OFF,
            "The LETS warden is disabled; authority receipts are not enforced.",
            "Optional: configure LETS_* settings to enable the external warden.",
        )
    if snapshot.application_ready and snapshot.component_status == "healthy":
        return _check(
            "lets", "LETS warden", STATUS_READY,
            f"The LETS warden is reachable ({snapshot.mode} mode).",
        )
    return _check(
        "lets", "LETS warden", STATUS_UNAVAILABLE,
        f"LETS is in {snapshot.mode} mode but not ready ({snapshot.operator_code}).",
        "Governed effects are refused until the warden is reachable; check LETS_* "
        "settings and warden health.",
    )


async def service_checks(orch) -> list[dict[str, Any]]:
    return [
        await asyncio.to_thread(_database_check, orch),
        await _auth_check(orch),
        await asyncio.to_thread(_worker_check, orch),
        await _publication_check(orch),
        await asyncio.to_thread(_lets_check, orch),
    ]


def _overall(checks: list[dict[str, Any]]) -> str:
    worst = STATUS_READY
    for check in checks:
        if check["key"] not in REQUIRED_KEYS or check["status"] == STATUS_READY:
            continue
        if check["status"] == STATUS_UNAVAILABLE or worst == STATUS_UNAVAILABLE:
            worst = STATUS_UNAVAILABLE
        else:
            worst = STATUS_DEGRADED
    return worst


async def guide_payload(orch, connected_origin: Optional[str] = None) -> dict[str, Any]:
    checks = await service_checks(orch)
    return {
        "contract": CONTRACT,
        "surface_key": SURFACE_KEY,
        "endpoint": endpoint_report(connected_origin),
        "checks": checks,
        "overall": _overall(checks),
        "sign_in": {"title": SIGN_IN_TITLE, "steps": list(SIGN_IN_STEPS)},
    }


@connection_router.get(
    "/api/connection/status",
    summary="Verify the first connection and configured services",
    description=(
        "Honest first-connection verification: the server-attested endpoint "
        "identity, per-service readiness (database, authentication, voice workers, "
        "generated-agent publication, LETS warden) with actionable remediation, and "
        "the user-completed sign-in/key guidance. Returns 503 with the same body "
        "when a required service is not ready."
    ),
)
async def get_connection_status(
    request: Request,
    _user: dict = Depends(get_current_user_payload),
) -> JSONResponse:
    orch = request.app.state.orchestrator
    origin = request.headers.get("origin") or str(request.base_url)
    payload = await guide_payload(orch, origin)
    return JSONResponse(payload, status_code=200 if payload["overall"] == STATUS_READY else 503)


async def push_connection_guide(orch, websocket, user_id: str, *,
                                payload: Optional[dict[str, Any]] = None) -> bool:
    if payload is None:
        payload = await guide_payload(orch)
    try:
        profile = orch.rote.get_profile(websocket)
        device_type = getattr(profile.device_type, "value", str(profile.device_type))
    except Exception:
        device_type = "browser"
    title = "Connection status"
    if device_type in ("windows", "android", "ios", "macos"):
        from orchestrator.projection_surfaces import connection as connection_surface
        from shared.protocol import ChromeSurface

        components = list(await connection_surface.components_from_payload(payload))
        try:
            from rote.adapter import ComponentAdapter

            components = ComponentAdapter.adapt(components, orch.rote.get_profile(websocket))
        except Exception:
            from webrender.chrome.surfaces import _sdui

            logger.exception("connection guide: native adaptation failed")
            components = [_sdui.alert(
                "Connection checks could not be adapted for this device. "
                "Reconnect to retry.", "error")]
        delivered = await orch._safe_send(websocket, ChromeSurface(
            region="modal",
            surface_key=SURFACE_KEY,
            title=title,
            admin_only=False,
            components=components,
            mode="mandatory",
        ).to_json())
    else:
        from orchestrator.projection_surfaces import connection as connection_surface
        from shared.protocol import ChromeRender
        from webrender.chrome import render_modal_shell

        body = await connection_surface.render_from_payload(payload)
        delivered = await orch._safe_send(websocket, ChromeRender(
            region="modal",
            html=render_modal_shell(title, body, SURFACE_KEY, mandatory=True,
                                    subtitle="First-connection checks for this "
                                             "AstralDeep server"),
        ).to_json())
    if delivered:
        from orchestrator.chrome_events import _note_open_surface

        _note_open_surface(orch, websocket, SURFACE_KEY)
    return bool(delivered)


async def maybe_push_on_register(orch, websocket, user_id: str) -> bool:
    from orchestrator import llm_gate

    if llm_gate.is_gated(orch, websocket):
        return False
    if (getattr(orch, "_ws_active_chat", None) or {}).get(id(websocket)):
        return False
    try:
        payload = await guide_payload(orch)
    except Exception:
        logger.exception("connection guide: readiness evaluation failed")
        return False
    if payload.get("overall") == STATUS_READY:
        return False
    try:
        return await push_connection_guide(orch, websocket, user_id, payload=payload)
    except Exception:
        logger.exception("connection guide: push failed")
        return False


__all__ = [
    "CONTRACT", "REQUIRED_KEYS", "SIGN_IN_STEPS", "SIGN_IN_TITLE", "STATUS_DEGRADED",
    "STATUS_OFF", "STATUS_READY", "STATUS_UNAVAILABLE", "SURFACE_KEY",
    "configured_public_endpoints", "connection_router", "endpoint_report",
    "guide_payload", "maybe_push_on_register", "push_connection_guide",
    "service_checks",
]
