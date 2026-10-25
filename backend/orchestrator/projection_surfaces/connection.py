"""Renders the first-connection guide surface from connection_guide.py: endpoint
attestation, per-service readiness with remediation, and sign-in/key guidance, shared
by the web modal and native SDUI delivery; the re-check control re-opens this surface
through the standard chrome_open action.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from webrender.chrome import esc
from webrender.chrome.surfaces import _sdui

from orchestrator.connection_guide import (
    STATUS_DEGRADED,
    STATUS_OFF,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    SURFACE_KEY,
    guide_payload,
)

logger = logging.getLogger("Orchestrator.Chrome.Connection")

TITLE = "Connection status"
SUBTITLE = "First-connection checks for this AstralDeep server"
SURFACE_KEY = SURFACE_KEY
NO_NAV = True

_STATUS_LABEL = {
    STATUS_READY: "Ready",
    STATUS_DEGRADED: "Degraded",
    STATUS_UNAVAILABLE: "Unavailable",
    STATUS_OFF: "Off",
}
_STATUS_VARIANT = {
    STATUS_READY: "success",
    STATUS_DEGRADED: "warning",
    STATUS_UNAVAILABLE: "error",
    STATUS_OFF: "default",
}

_ROW_CLS = (
    "flex items-start justify-between gap-3 rounded-lg bg-white/5 "
    "border border-white/10 px-4 py-3"
)


def _unknown_status(status: Any) -> str:
    return status if status in _STATUS_LABEL else STATUS_UNAVAILABLE


def _endpoint_rows(endpoint: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    connected = endpoint.get("connected") or "not reported"
    rows.append({"label": "Connected endpoint", "value": str(connected)})
    configured = endpoint.get("configured") or []
    rows.append({
        "label": "Published endpoints",
        "value": ", ".join(str(item) for item in configured) if configured else "none configured",
    })
    rows.append({
        "label": "Endpoint verified",
        "value": "yes" if endpoint.get("verified") else "not verified",
    })
    return rows


def _endpoint_html(endpoint: Dict[str, Any]) -> str:
    rows = "".join(
        f'<div class="flex justify-between gap-3 text-sm">'
        f'<span class="text-astral-muted">{esc(row["label"])}</span>'
        f'<span class="text-astral-text">{esc(row["value"])}</span></div>'
        for row in _endpoint_rows(endpoint)
    )
    guidance = f'<p class="text-xs text-astral-muted">{esc(endpoint.get("guidance", ""))}</p>'
    return (
        f'<div class="rounded-lg bg-white/5 border border-white/10 p-4 space-y-2">'
        f'<div class="text-sm font-medium text-astral-text">Endpoint</div>'
        f'{rows}{guidance}</div>'
    )


def _checks_html(checks: List[Dict[str, Any]]) -> str:
    rows = []
    for check in checks:
        status = _unknown_status(check.get("status"))
        remediation = check.get("remediation") or ""
        extra = (
            f'<p class="text-xs text-astral-muted mt-1">{esc(remediation)}</p>'
            if remediation else ""
        )
        rows.append(
            f'<div class="{_ROW_CLS}">'
            f'<div class="min-w-0"><div class="text-sm font-medium text-astral-text">'
            f'{esc(str(check.get("label") or check.get("key") or ""))}</div>'
            f'<div class="text-xs text-astral-muted">{esc(str(check.get("detail") or ""))}'
            f'</div>{extra}</div>'
            f'<span class="text-xs rounded-full px-2 py-1 border border-white/10 '
            f'text-astral-text whitespace-nowrap">{esc(_STATUS_LABEL[status])}</span></div>'
        )
    return f'<div class="space-y-2">{"".join(rows)}</div>'


def _sign_in_html(sign_in: Dict[str, Any]) -> str:
    items = "".join(
        f'<li class="text-xs text-astral-muted">{esc(str(step))}</li>'
        for step in (sign_in.get("steps") or [])
    )
    return (
        f'<div class="rounded-lg bg-white/5 border border-white/10 p-4 space-y-2">'
        f'<div class="text-sm font-medium text-astral-text">'
        f'{esc(str(sign_in.get("title") or ""))}</div>'
        f'<ul class="list-disc pl-4 space-y-1">{items}</ul></div>'
    )


async def render_from_payload(payload: Dict[str, Any]) -> str:
    overall = _unknown_status(payload.get("overall"))
    if overall == STATUS_UNAVAILABLE:
        banner = ("A required service is not ready: fix the failing checks below "
                  "before depending on this server.")
    elif overall == STATUS_DEGRADED:
        banner = "This server is usable, but one or more checks need attention."
    else:
        banner = ""
    notice = (
        f'<div class="rounded-lg border border-white/10 p-4 text-sm text-astral-text '
        f'bg-white/5 mb-4">{esc(banner)}</div>' if banner else ""
    )
    return (
        f'<div class="space-y-4">{notice}'
        f'{_endpoint_html(payload.get("endpoint") or {})}'
        f'{_checks_html(payload.get("checks") or [])}'
        f'{_sign_in_html(payload.get("sign_in") or {})}</div>'
    )


def _check_components(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for check in checks:
        status = _unknown_status(check.get("status"))
        label = str(check.get("label") or check.get("key") or "")
        detail = str(check.get("detail") or "")
        remediation = str(check.get("remediation") or "")
        message = f"{label}: {detail}"
        if remediation:
            message = f"{message} {remediation}"
        out.append(_sdui.alert(message, _STATUS_VARIANT[status], title=label))
    return out


async def components_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    overall = _unknown_status(payload.get("overall"))
    out: List[Dict[str, Any]] = []
    if overall == STATUS_UNAVAILABLE:
        out.append(_sdui.alert(
            "A required service is not ready: fix the failing checks below before "
            "depending on this server.",
            "error",
            title="Connection status",
        ))
    elif overall == STATUS_DEGRADED:
        out.append(_sdui.alert(
            "This server is usable, but one or more checks need attention.",
            "warning",
            title="Connection status",
        ))
    endpoint = payload.get("endpoint") or {}
    out.append(_sdui.card("Endpoint", _endpoint_component_rows(endpoint)))
    out.extend(_check_components(payload.get("checks") or []))
    sign_in = payload.get("sign_in") or {}
    out.append(_sdui.card(
        str(sign_in.get("title") or "Sign in and connect your model"),
        [_sdui.text(str(step), "caption") for step in (sign_in.get("steps") or [])],
    ))
    out.append(_sdui.button(
        "Re-run checks", "chrome_open", {"surface": SURFACE_KEY}))
    return out


def _endpoint_component_rows(endpoint: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        _sdui.text(f"{row['label']}: {row['value']}", "caption")
        for row in _endpoint_rows(endpoint)
    ] + [_sdui.text(str(endpoint.get("guidance") or ""), "caption")]


async def render(orch, user_id, roles, params) -> str:
    origin = params.get("origin") if isinstance(params, dict) else None
    payload = await guide_payload(orch, origin if isinstance(origin, str) else None)
    return await render_from_payload(payload)


async def components(orch, user_id, roles, params) -> List[Dict[str, Any]]:
    origin = params.get("origin") if isinstance(params, dict) else None
    payload = await guide_payload(orch, origin if isinstance(origin, str) else None)
    return await components_from_payload(payload)
