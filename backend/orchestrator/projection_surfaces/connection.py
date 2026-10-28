"""Composes host-authorized connection status through approved shared chrome components.
Web rendering and native serialization consume the same component view using pinned Projection.
"""

from __future__ import annotations

from typing import Any, Dict, List

from webrender.chrome.surfaces import _sdui

from orchestrator.connection_guide import (
    STATUS_DEGRADED,
    STATUS_OFF,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    SURFACE_KEY,
    guide_payload,
)

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


async def render_from_payload(payload: Dict[str, Any]) -> str:
    from astralprojection.chrome import render_html
    from astralprojection.models import ChromeViewModel, ComponentView

    components = await components_from_payload(payload)
    view = ChromeViewModel(SURFACE_KEY, TITLE, tuple(ComponentView(item["type"],
        {key: value for key, value in item.items() if key != "type"}) for item in components))
    return render_html(view)


def _check_components(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for check in checks:
        status = _unknown_status(check.get("status"))
        label = str(check.get("label") or check.get("key") or "")
        detail = str(check.get("detail") or "")
        remediation = str(check.get("remediation") or "")
        message = f"{label}: {_STATUS_LABEL[status]}. {detail}"
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
