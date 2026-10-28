"""Builds truthful first-connection status over the existing service readiness seams.
The authenticated REST resource and registration delivery share the payload consumed
by projection_surfaces/connection.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from copy import deepcopy
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
PROBE_TIMEOUT_SECONDS = 4

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
    if not raw or len(value) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}", scheme="http")
        scheme = (parts.scheme or "http").lower()
        host = (parts.hostname or "").lower()
        if scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
            return ""
        port = parts.port
    except ValueError:
        return ""
    if port is None:
        port = 443 if scheme == "https" else 80
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    if ":" in host:
        host = f"[{host}]"
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
            "The configured identity provider is unreachable.",
            "Verify the Keycloak URL and network path; first sign-in cannot complete "
            "until it is reachable.",
            required=True,
        )
    return _check(
        "auth", "Authentication", STATUS_READY,
        "Sign-in is served by the configured identity provider.", "",
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
    from orchestrator.lets_health_api import lets_snapshot, refresh_lets_reachability

    try:
        refresh_lets_reachability(orch)
        snapshot = lets_snapshot(orch)
    except Exception:
        logger.warning("connection guide: LETS posture read failed", exc_info=True)
        return _check(
            "lets", "LETS warden", STATUS_UNAVAILABLE,
            "The LETS posture could not be read.",
            "Check LETS configuration; governed effects fail closed while the "
            "posture is unreadable.",
            required=True,
        )
    if snapshot.application_ready is not True:
        return _check("lets", "LETS warden", STATUS_UNAVAILABLE,
            f"LETS application readiness is blocked ({snapshot.operator_code}).",
            "Check LETS configuration and warden health before depending on governed dispatch.", required=True)
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
    probes = (("database", "Database", True, lambda: asyncio.to_thread(_database_check, orch)),
        ("auth", "Authentication", True, lambda: _auth_check(orch)),
        ("worker", "Voice workers", False, lambda: asyncio.to_thread(_worker_check, orch)),
        ("publication", "Agent publication", True, lambda: _publication_check(orch)),
        ("lets", "LETS warden", True, lambda: asyncio.to_thread(_lets_check, orch)))
    checks = []
    for key, label, required, probe in probes:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                check = await probe()
        except Exception:
            logger.warning("connection guide: readiness check unavailable: %s", key)
            check = _check(key, label, STATUS_UNAVAILABLE,
                f"{label} readiness could not be verified.",
                "Check the configured service and retry connection checks.", required=required)
        checks.append(check)
    return checks


def _overall(checks: list[dict[str, Any]]) -> str:
    worst = STATUS_READY
    for check in checks:
        if not (check.get("required", False) or check["key"] in REQUIRED_KEYS) or check["status"] == STATUS_READY:
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
    current = _delivery_guard(orch, websocket, user_id)
    if not current():
        return False
    if payload is None:
        payload = await guide_payload(orch)
    if not current(ready=True):
        return False
    from orchestrator.chrome_events import _surface_delivery
    delivery = _surface_delivery(orch, websocket, SURFACE_KEY)
    if delivery is None:
        logger.warning("connection guide: device profile unavailable")
        return False
    title = "Connection status"
    if delivery in {"native", "watch"}:
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
        frame = ChromeSurface(
            region="modal",
            surface_key=SURFACE_KEY,
            title=title,
            admin_only=False,
            components=components,
            mode="mandatory",
        ).to_json()
    else:
        from orchestrator.projection_surfaces import connection as connection_surface
        from shared.protocol import ChromeRender
        from webrender.chrome import render_modal_shell

        body = await connection_surface.render_from_payload(payload)
        frame = ChromeRender(
            region="modal",
            html=render_modal_shell(title, body, SURFACE_KEY, mandatory=True,
                                    subtitle="First-connection checks for this "
                                             "AstralDeep server"),
        ).to_json()
    if not current(ready=True):
        return False
    delivered = await orch._safe_send(websocket, frame)
    if delivered and current(ready=True):
        from orchestrator.chrome_events import _note_open_surface

        _note_open_surface(orch, websocket, SURFACE_KEY)
        return True
    return False


def _delivery_guard(orch, websocket, user_id):
    from orchestrator import llm_gate

    context = (getattr(orch, "_connection_contexts", None) or {}).get(id(websocket))
    registration = (getattr(orch, "ui_sessions", None) or {}).get(websocket)
    if context is None or type(registration) is not dict or registration.get("sub") != user_id or not user_id:
        return lambda **kwargs: False
    captured = deepcopy(registration)
    scope = getattr(context, "connection_scope_id", None)
    generation = getattr(context, "connection_generation", None)
    pending = getattr(context, "work_registrations_pending", None)
    initial = generation is None and not getattr(context, "registered", False) and pending == 1

    def current(*, ready=False):
        nonlocal generation, initial
        count = getattr(context, "work_registrations_pending", None)
        active_generation = getattr(context, "connection_generation", None)
        valid = bool(scope is not None and type(pending) is int and type(count) is int and 0 <= count <= pending <= 1
            and (getattr(orch, "_connection_contexts", None) or {}).get(id(websocket)) is context
            and getattr(context, "websocket", None) is websocket and not getattr(context, "closing", True)
            and not getattr(websocket, "closed", False) and getattr(context, "connection_scope_id", None) == scope
            and ((initial and (not ready or active_generation is not None))
                 or generation is not None and active_generation == generation)
            and (getattr(context, "registered", False) or initial and not ready)
            and (getattr(orch, "ui_sessions", None) or {}).get(websocket) is registration and registration == captured
            and not llm_gate.is_gated(orch, websocket)
            and not (getattr(orch, "_ws_active_chat", None) or {}).get(id(websocket)))
        if valid and ready and initial:
            generation, initial = active_generation, False
        return valid
    return current


async def maybe_push_on_register(orch, websocket, user_id: str) -> bool:
    current = _delivery_guard(orch, websocket, user_id)
    if not current():
        return False
    try:
        payload = await guide_payload(orch)
    except Exception:
        logger.exception("connection guide: readiness evaluation failed")
        return False
    if not current(ready=True) or payload.get("overall") == STATUS_READY:
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
