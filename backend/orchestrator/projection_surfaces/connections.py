"""Renders the Connections surface for issuing and revoking the owner's framework
credentials, using astralprojection.chrome.connections builders. Issuing a key
requires a fresh live session via human_request_authority.py, never a bare-bearer
one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from webrender.chrome import esc, notice_block

from orchestrator.framework_credentials import FrameworkCredentialService
from persistent_agents.models import AssignmentError

TITLE = "Connections"
SURFACE_KEY = "connections"

logger = logging.getLogger("Orchestrator.Chrome.Connections")

_DISABLED_MSG = "Connections are not enabled on this server."
_UNAVAILABLE_MSG = "Connections are unavailable right now. Try again shortly."
_SECRET_TTL_SECONDS = 120.0

_ISSUE_ERRORS = {
    "framework_credential_invalid": "Check the name, scopes, expiry and admissions and try again.",
    "framework_credential_authority_unavailable":
        "Your session changed while issuing this key. Sign in again and retry.",
    "framework_credential_authority_required": "Sign in again to issue a key.",
}
_REVOKE_ERRORS = {
    "framework_credential_not_found": "That key no longer exists.",
    "framework_credential_authority_unavailable": "That key could not be revoked. Try again.",
}

_PENDING_SECRETS: dict[tuple[int, str], tuple[str, float]] = {}


def _enabled() -> bool:
    from shared.feature_flags import flags
    return flags.is_enabled("framework_credentials")


def _sweep_pending_secrets() -> None:
    now = time.monotonic()
    stale = [key for key, (_secret, deadline) in _PENDING_SECRETS.items() if deadline <= now]
    for key in stale:
        _PENDING_SECRETS.pop(key, None)


def _stash_secret(websocket, credential_id: str, secret: str) -> None:
    _sweep_pending_secrets()
    _PENDING_SECRETS[(id(websocket), credential_id)] = (secret, time.monotonic() + _SECRET_TTL_SECONDS)


def _pop_secret(websocket, credential_id: Optional[str]) -> Optional[str]:
    _sweep_pending_secrets()
    if websocket is None or not credential_id:
        return None
    entry = _PENDING_SECRETS.pop((id(websocket), credential_id), None)
    if entry is None:
        return None
    secret, deadline = entry
    return secret if time.monotonic() < deadline else None


def _service(orch) -> Optional[FrameworkCredentialService]:
    return getattr(orch, "framework_credentials", None)


def _scope_options() -> list[str]:
    from astralplane.repositories.framework_credentials import FRAMEWORK_CREDENTIAL_SCOPES
    return sorted(FRAMEWORK_CREDENTIAL_SCOPES)


def _form_state(*, name: str = "", scopes=None, expires_in_seconds: int = 30 * 86400,
                 max_admissions: int = 1000, error: Optional[str] = None) -> dict:
    return {
        "scope_options": _scope_options(),
        "defaults": {
            "name": name,
            "scopes": list(scopes or []),
            "expires_in_seconds": expires_in_seconds,
            "max_admissions": max_admissions,
        },
        "error": error,
    }


def _current_human_caller(orch, user_id: str):
    from orchestrator.human_request_authority import current_human_caller
    caller = current_human_caller(expected_orchestrator=orch)
    if caller is None or caller.owner_id != user_id:
        return None
    return caller


async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    from astralprojection.chrome import render_html
    from astralprojection.chrome.connections import build_connections_view

    from orchestrator.chrome_events import current_surface_socket

    if not _enabled():
        return f'<p class="text-sm text-astral-muted">{esc(_DISABLED_MSG)}</p>'
    service = _service(orch)
    if service is None:
        return render_html(build_connections_view(error=_UNAVAILABLE_MSG))
    params = params if isinstance(params, dict) else {}
    just_issued = params.get("issued_credential_id")
    websocket = current_surface_socket.get()
    secret = _pop_secret(websocket, just_issued) if isinstance(just_issued, str) else None
    try:
        rows = await asyncio.to_thread(service.list, owner_id=user_id)
    except AssignmentError:
        logger.exception("connections: list failed")
        return render_html(build_connections_view(error=_UNAVAILABLE_MSG))
    if secret is not None:
        issued_row = next((row for row in rows if row["credential_id"] == just_issued), None)
        if issued_row is not None:
            from astralprojection.chrome.connections import build_issued_secret_view
            return render_html(build_issued_secret_view(secret, issued_row))
    return render_html(build_connections_view(rows, _form_state()))


async def components(orch: Any, user_id: str, roles: Any, params: Any):
    from astralprojection.chrome.connections import build_connections_view

    from orchestrator.chrome_events import current_surface_socket

    if not _enabled():
        return [{"type": "alert", "variant": "info", "message": _DISABLED_MSG}]
    service = _service(orch)
    if service is None:
        return [c.to_dict() for c in build_connections_view(error=_UNAVAILABLE_MSG).components]
    params = params if isinstance(params, dict) else {}
    just_issued = params.get("issued_credential_id")
    websocket = current_surface_socket.get()
    secret = _pop_secret(websocket, just_issued) if isinstance(just_issued, str) else None
    try:
        rows = await asyncio.to_thread(service.list, owner_id=user_id)
    except AssignmentError:
        logger.exception("connections: list failed")
        return [c.to_dict() for c in build_connections_view(error=_UNAVAILABLE_MSG).components]
    if secret is not None:
        issued_row = next((row for row in rows if row["credential_id"] == just_issued), None)
        if issued_row is not None:
            from astralprojection.chrome.connections import build_issued_secret_view
            return [c.to_dict() for c in build_issued_secret_view(secret, issued_row).components]
    return [c.to_dict() for c in build_connections_view(rows, _form_state()).components]


def _fields(payload: Any) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("fields"), dict):
        return payload["fields"]
    return payload if isinstance(payload, dict) else {}


async def _h_issue(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG)
    service = _service(orch)
    if service is None:
        return SURFACE_KEY, {}, notice_block("error", _UNAVAILABLE_MSG)
    fields = _fields(payload)
    name = str(fields.get("name") or "").strip()
    scopes = fields.get("scopes") or []
    if isinstance(scopes, str):
        scopes = [scopes]
    scopes = [str(s) for s in scopes] if isinstance(scopes, (list, tuple)) else []
    fail_state = _form_state(name=name, scopes=scopes)
    try:
        expires_in = int(fields.get("expires_in_seconds"))
        max_admissions = int(fields.get("max_admissions"))
    except (TypeError, ValueError):
        return (SURFACE_KEY, {},
                _issue_form_notice("Expiry and maximum admissions must be whole numbers.", fail_state))
    caller = _current_human_caller(orch, user_id)
    if caller is None:
        return SURFACE_KEY, {}, notice_block(
            "error", "Sign in again (a live session is required to issue a key).")
    session_observation = caller.require_session()
    try:
        view_row, secret = await asyncio.to_thread(
            service.issue, owner_id=caller.owner_id, caller=session_observation, name=name,
            scopes=scopes, expires_in_seconds=expires_in, max_admissions=max_admissions,
        )
    except AssignmentError as exc:
        message = _ISSUE_ERRORS.get(exc.code, "That key could not be issued. Check the form and try again.")
        return SURFACE_KEY, {}, _issue_form_notice(message, fail_state)
    _stash_secret(websocket, view_row["credential_id"], secret)
    return SURFACE_KEY, {"issued_credential_id": view_row["credential_id"]}, ""


def _issue_form_notice(message: str, _fail_state: dict) -> str:
    return notice_block("error", message)


async def _h_revoke(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG)
    service = _service(orch)
    if service is None:
        return SURFACE_KEY, {}, notice_block("error", _UNAVAILABLE_MSG)
    caller = _current_human_caller(orch, user_id)
    if caller is None:
        return SURFACE_KEY, {}, notice_block(
            "error", "Sign in again (a live session is required to revoke a key).")
    credential_id = str((payload or {}).get("credential_id") or "")
    if not credential_id:
        return SURFACE_KEY, {}, notice_block("error", "Missing key identifier.")
    try:
        await asyncio.to_thread(service.revoke, owner_id=caller.owner_id, credential_id=credential_id)
    except AssignmentError as exc:
        message = _REVOKE_ERRORS.get(exc.code, "That key could not be revoked.")
        return SURFACE_KEY, {}, notice_block("error", message)
    return SURFACE_KEY, {}, notice_block("success", "Key revoked.")


HANDLERS = {
    "chrome_connection_issue": _h_issue,
    "chrome_connection_revoke": _h_revoke,
}
