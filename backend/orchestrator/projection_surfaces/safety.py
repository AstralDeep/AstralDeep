"""Render shared emergency stop controls through Projection and astralprims.
Current human request authority binds each owner stop, responder check, and
explicit resume to the same coordinator used by REST and CLI clients.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from webrender.chrome import esc, notice_block
from webrender.chrome.surfaces import _sdui
from orchestrator.emergency_stop import EmergencyStopRefused
from persistent_agents.models import AssignmentError

logger = logging.getLogger("Orchestrator.Chrome.Safety")

TITLE = "Emergency stop"
SURFACE_KEY = "safety"

_STATE_LINES = {
    "running": "Local effects are enabled. Stop blocks new local work and interrupts "
               "supported active work. Remote machines require a separate acknowledgment.",
    "stopped": "Local stop is durably active. Supported active work is interrupted; "
               "effects already dispatched can remain uncertain. Resume admits new work.",
    "partial": "Partially acknowledged — the stop is active, and some responders have "
               "not acknowledged yet.",
    "unreachable": "Unreachable — new local work is blocked, but at least one responder "
                   "could not confirm interruption. Unfinished or remote effects remain "
                   "uncertain until verified.",
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
_ACTION_GUIDANCE = "Stop applies locally immediately. Resume requires your current signed-in owner session."


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
        revision = status.get("revision")
        if type(revision) is int and 0 < revision <= 9007199254740991:
            rows.append(f"Current revision for resume: {revision}")
        else:
            rows.append("Resume is unavailable for the current revision on this client.")
    return rows


def _badge_html(state):
    label, variant = _BADGES.get(state, _BADGES["running"])
    cls = _BADGE_CLASSES.get(variant, _BADGE_CLASSES["default"])
    return (f'<span class="inline-block px-2 py-0.5 rounded-full border text-[10px] '
            f'font-medium uppercase tracking-wide {cls}">{esc(label)}</span>')


async def render(orch, user_id, roles, params) -> str:
    from webrender.renderer import render_children

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
        + render_children(_controls(status))
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
    content.extend(_controls(status))
    return [_sdui.card(f"Emergency stop — {label}", content, variant="default")]


def _controls(status):
    controls = []
    revision = status.get("revision")
    if status.get("engaged") and type(revision) is int and 0 < revision <= 9007199254740991:
        controls.append(_sdui.button("Resume explicitly", "chrome_safety_resume",
            {"surface": SURFACE_KEY, "expected_revision": revision}, "primary"))
    elif not status.get("engaged"):
        controls.append(_sdui.button("Stop everything now", "chrome_safety_stop",
                                    {"surface": SURFACE_KEY}, "primary"))
    controls.append(_sdui.button("Recheck responders", "chrome_safety_verify",
                                {"surface": SURFACE_KEY}, "primary"))
    for control in controls:
        control.update(disabled=False, local=False)
    return controls


async def _control(orch, websocket, user_id, payload, action):
    from orchestrator.human_request_authority import current_human_caller

    caller = current_human_caller(expected_orchestrator=orch)
    if (caller is None or caller.owner_id != user_id or caller._binding.socket_request is None
            or caller._binding.socket_request.websocket is not websocket):
        raise AssignmentError("human_authentication_required", 401)
    stop = _coordinator(orch)
    if stop is None:
        raise EmergencyStopRefused("emergency_stop_unavailable", 503)
    if type(payload) is not dict or set(payload) - {"surface", "expected_revision", "reason"}:
        raise EmergencyStopRefused("emergency_stop_invalid", 422)
    if action == "verify":
        await caller.verify_delivery()
        await stop.verify(caller.owner_id)
    else:
        caller.require_write()
        if action == "stop":
            await stop.engage(caller.owner_id, reason=payload.get("reason"), claims=caller.claims, caller=caller)
        else:
            await stop.resume(caller.owner_id, expected_revision=payload.get("expected_revision"),
                actor_id=caller.owner_id, claims=caller.claims, caller=caller)
    await caller.verify_delivery()
    return SURFACE_KEY, {}, ""


async def _stop(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "stop")


async def _resume(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "resume")


async def _verify(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "verify")


HANDLERS = {"chrome_safety_stop": _stop, "chrome_safety_resume": _resume, "chrome_safety_verify": _verify}
