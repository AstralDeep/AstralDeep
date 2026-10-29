"""Builds the default-off external-host Work panel view models and resource metadata that
mcp_server_endpoint.py serves through its resource methods; authorization, owner binding,
and PHI screening stay in mcp_work_resources.py, and presentation stays with Projection.
"""

from __future__ import annotations

from typing import Any

from shared.feature_flags import flags

PANEL_FLAG = "mcp_work_panel"
PANEL_KIND = "astral_work_status"
PANEL_URI_SEGMENT = "panel"
PANEL_MIME_TYPE = "application/json"
PANEL_TITLE_LIMIT = 200
PANEL_TEXT_LIMIT = 500
PANEL_STATES = (
    "waiting",
    "running",
    "input_required",
    "failed",
    "cancelled",
    "succeeded",
)

_DISPOSITION_STATES = {
    "queued": "waiting",
    "active": "running",
    "awaiting_event": "running",
    "retry_eligible": "running",
    "paused": "running",
    "budget_blocked": "running",
    "awaiting_approval": "input_required",
    "awaiting_authority": "input_required",
    "reconciliation_required": "input_required",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "unsupported_version": "cancelled",
}

PANEL_RESOURCE_META = {
    "astral/panelKind": PANEL_KIND,
    "astral/panelStates": list(PANEL_STATES),
    "astral/dataFlow": "owner-scoped; no third-party data flow",
    "astral/presentation": "authorized-host view model without embedded authority",
}


def panel_enabled() -> bool:
    return flags.is_enabled("mcp_server") and flags.is_enabled(PANEL_FLAG)


def panel_state(disposition: Any) -> str | None:
    return _DISPOSITION_STATES.get(disposition) if isinstance(disposition, str) else None


def _bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        return None
    return value[:limit] or None


def _bounded_field(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = _bounded_text(value, limit)
    if text is None:
        raise ValueError("panel view is unavailable")
    return text


def panel_view(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("panel view is unavailable")
    state = panel_state(record.get("disposition"))
    if state is None:
        raise ValueError("panel view is unavailable")
    title = _bounded_text(record.get("title"), PANEL_TITLE_LIMIT)
    if not title:
        raise ValueError("panel view is unavailable")
    view: dict[str, Any] = {"kind": PANEL_KIND, "state": state, "title": title}
    if state == "failed":
        message = _bounded_field(record.get("safe_error_code"), PANEL_TEXT_LIMIT)
        if message:
            view["status_message"] = message
    detail = _bounded_field(record.get("phase"), PANEL_TEXT_LIMIT)
    if detail:
        view["detail"] = detail
    return view


def panel_descriptor(work_id: str, title: str) -> dict[str, Any]:
    name = _bounded_text(title, PANEL_TITLE_LIMIT) or "Panel"
    return {
        "uri": f"astral://work/{work_id}/{PANEL_URI_SEGMENT}",
        "name": f"Panel: {name}",
        "mimeType": PANEL_MIME_TYPE,
        "description": "External-host panel view for this work item",
        "_meta": dict(PANEL_RESOURCE_META),
    }


def panel_read_result(uri: str, work_id: str, view: dict[str, Any]) -> dict[str, Any]:
    return {
        "uri": uri,
        "name": f"Panel: {view.get('title', work_id)}",
        "mimeType": PANEL_MIME_TYPE,
        "boundedViewModel": view,
        "_meta": dict(PANEL_RESOURCE_META),
    }
