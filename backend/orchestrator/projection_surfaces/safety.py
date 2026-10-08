"""Renders the Emergency stop surface: the owner's truthful stopped /
partial-acknowledgment / unreachable status with explicit resume instructions,
composed from astralprims primitives and shared chrome helpers for both the web
and native SDUI dispositions; mutations flow through the owner REST API and CLI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from webrender.chrome import esc, notice_block
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome.Safety")

TITLE = "Emergency stop"
SURFACE_KEY = "safety"

_STATE_LINES = {
    "running": "Everything is running. An emergency stop refuses all new authorized "
               "effects for your account: tool admission, agent dispatch, scheduled "
               "turns, shells, and enrolled remote machines.",
    "stopped": "Stopped — every responder acknowledged. Nothing new is admitted until "
               "you explicitly resume.",
    "partial": "Partially acknowledged — the stop is active, and some responders have "
               "not acknowledged yet.",
    "unreachable": "Unreachable — the stop is active locally, but at least one enrolled "
                   "machine could not be reached. Its state stays unknown until it "
                   "acknowledges.",
}
_BADGES = {
    "running": ("Running", "default"),
    "stopped": ("Stopped", "success"),
    "partial": ("Partially acknowledged", "warning"),
    "unreachable": ("Unreachable responders", "error"),
}
_BADGE_CLASSES = {
    "default": "border-white/10 bg-white/5 text-astral-muted",
    "success": "border-green-500/20 bg-green-500/10 text-green-400",
    "warning": "border-yellow-500/20 bg-yellow-500/10 text-yellow-400",
    "error": "border-red-500/20 bg-red-500/10 text-red-400",
}
_ACTION_GUIDANCE = (
    "To act now: POST /api/emergency-stop/stop (or /resume with the current revision) "
    "with your owner token, or run python -m astral_sdk emergency-stop / "
    "emergency-status / emergency-resume against this server."
)


def _coordinator(orch):
    return getattr(orch, "emergency_stop", None)


def _status(orch, user_id):
    coordinator = _coordinator(orch)
    if coordinator is None:
        return None
    try:
        status = coordinator.status(user_id)
    except Exception:
        logger.debug("emergency stop status unavailable", exc_info=True)
        return None
    return status if isinstance(status, dict) and status.get("state") else None


def _rows(status):
    state = status["state"]
    rows = [_STATE_LINES.get(state, _STATE_LINES["running"])]
    if status.get("engaged"):
        engaged_at = status.get("engaged_at")
        if isinstance(engaged_at, (int, float)):
            rows.append("Engaged " + datetime.fromtimestamp(
                engaged_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
        if status.get("reason"):
            rows.append("Reason: " + str(status["reason"]))
        for responder in status.get("responders") or []:
            rows.append(f"{responder.get('responder')}: {responder.get('state')}")
        rows.append(f"Current revision for resume: {status.get('revision')}")
    return rows


def _badge_html(state):
    label, variant = _BADGES.get(state, _BADGES["running"])
    cls = _BADGE_CLASSES.get(variant, _BADGE_CLASSES["default"])
    return (f'<span class="inline-block px-2 py-0.5 rounded-full border text-[10px] '
            f'font-medium uppercase tracking-wide {cls}">{esc(label)}</span>')


async def render(orch, user_id, roles, params) -> str:
    status = _status(orch, user_id)
    if status is None:
        return notice_block("error", "Emergency stop is unavailable on this instance.")
    state = status["state"]
    body = "".join(f'<p class="text-sm text-astral-muted">{esc(row)}</p>'
                   for row in _rows(status))
    return (
        '<section class="space-y-2 rounded-lg border border-red-500/20 bg-red-500/5 p-4" '
        'aria-label="Emergency stop">'
        f'<div class="flex items-center gap-2"><h2 class="text-sm font-semibold '
        f'text-astral-text">Emergency stop</h2>{_badge_html(state)}</div>'
        + body
        + f'<p class="text-sm text-astral-text">{esc(_ACTION_GUIDANCE)}</p>'
        + "</section>"
    )


async def components(orch, user_id, roles, params):
    status = _status(orch, user_id)
    if status is None:
        return [_sdui.alert("Emergency stop is unavailable on this instance.", "error")]
    state = status["state"]
    label, _variant = _BADGES.get(state, _BADGES["running"])
    content = [_sdui.text(row, "caption") for row in _rows(status)]
    content.append(_sdui.text(_ACTION_GUIDANCE, "caption"))
    return [_sdui.card(f"Emergency stop — {label}", content, variant="default")]
