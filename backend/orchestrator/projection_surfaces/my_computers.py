"""Renders the 'My computers' surface listing the owner's remote-control-enabled
desktops and their session controls (start/pause/resume/stop/forget), and revokes one task
grant named on a computer_session frame. Shares handler keys between web render() and native
components(), gated by FF_COMPUTER_USE.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from webrender.chrome import esc, notice_block

from orchestrator.computer_hosts import ComputerHostError
from orchestrator.computer_sessions import LOCAL_RESUME_REFUSALS

TITLE = "My computers"
SURFACE_KEY = "my_computers"

_DISABLED_MSG = "Remote control is not enabled on this server."
_GRANT_BINDING = ("session_id", "host_id", "chat_id", "grant_id")

_BTN = "px-3 py-2 rounded-lg text-sm font-medium bg-white/5 text-astral-text border border-white/10 hover:bg-white/10"
_BTN_PRIMARY = ("px-3 py-2 rounded-lg text-sm font-medium bg-astral-primary/20 "
                "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30")


def _enabled() -> bool:
    from shared.feature_flags import flags
    return flags.is_enabled("computer_use")


def _ago(ts: int) -> str:
    delta = max(0, int(time.time()) - int(ts or 0))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} d ago"


def _this_computer(orch, user_id: str, params: Any) -> Optional[Dict[str, Any]]:
    from orchestrator.chrome_events import current_surface_socket
    ws = current_surface_socket.get()
    if ws is None:
        return None
    claims = orch.ui_sessions.get(ws) or {}
    if "computer_host_capable" not in (claims.get("_client_capabilities") or []):
        return None
    host = orch.computer_hosts.host_for_socket(ws)
    return {"enabled": host is not None, "name": host.name if host else None,
            "host_id": host.host_id if host else None}


def _rows(orch, user_id: str) -> List[Dict[str, Any]]:
    hosts = orch.computer_hosts.list_for_owner(user_id)
    live = {s.host_id: s for s in orch.computer_sessions.live_for_owner(user_id)}
    for h in hosts:
        s = live.get(h["host_id"])
        h["session"] = s.public() if s else None
    return hosts


def _remotely_resumable(s: Dict[str, Any]) -> bool:
    return s["state"] == "paused" and s.get("pause_reason") not in LOCAL_RESUME_REFUSALS


def _session_line(h: Dict[str, Any]) -> str:
    s = h.get("session")
    if not s:
        return "no session"
    if s["state"] == "paused":
        return f"paused ({s.get('pause_reason') or 'someone is using it'}) · from your {s['controller_label']}"
    return f"session active · from your {s['controller_label']} · {s['verbs_run']} actions"


def _host_html(h: Dict[str, Any]) -> str:
    hid = esc(h["host_id"])
    screens = ", ".join(f'{s["width"]}×{s["height"]}' for s in h.get("screens") or []) or "?"
    status = "Online" if h["online"] else "Offline"
    status_class = "success" if h["online"] else "neutral"
    last_seen = "Ready to connect" if h["online"] else f"Last seen {_ago(h['last_seen'])}"
    s = h.get("session")
    buttons: List[str] = []
    if h["online"]:
        if s is None:
            buttons.append(f'<button type="button" class="{_BTN_PRIMARY}" '
                           f'data-ui-action="chrome_computer_session_start" '
                           f"data-ui-payload='{{\"host_id\":\"{hid}\"}}'>Control this computer</button>")
        else:
            sid = esc(s["session_id"])
            if _remotely_resumable(s):
                buttons.append(f'<button type="button" class="{_BTN_PRIMARY}" '
                               f'data-ui-action="chrome_computer_session_resume" '
                               f"data-ui-payload='{{\"session_id\":\"{sid}\"}}'>Resume</button>")
            elif s["state"] != "paused":
                buttons.append(f'<button type="button" class="{_BTN}" '
                               f'data-ui-action="chrome_computer_session_pause" '
                               f"data-ui-payload='{{\"session_id\":\"{sid}\"}}'>Pause</button>")
            buttons.append(f'<button type="button" class="{_BTN}" '
                           f'data-ui-action="chrome_computer_session_stop" '
                           f"data-ui-payload='{{\"session_id\":\"{sid}\"}}'>Stop</button>")
    else:
        buttons.append(f'<button type="button" class="{_BTN}" data-ui-action="chrome_computer_forget" '
                       f"data-ui-payload='{{\"host_id\":\"{hid}\"}}'>Forget</button>")
    return (
        '<tr>'
        '<td data-label="Computer"><div>'
        f'<div class="text-astral-text font-medium">{esc(h["name"])}</div>'
        f'<div class="text-xs text-astral-muted">{esc(h["platform"])} · {esc(screens)}</div></div></td>'
        '<td data-label="Status"><div>'
        f'<span class="astral-badge astral-badge-{status_class}">{status}</span>'
        f'<div class="text-xs text-astral-muted">{esc(last_seen)}</div></div></td>'
        f'<td data-label="Session" class="text-sm text-astral-muted">{esc(_session_line(h))}</td>'
        '<td data-label="Actions">'
        f'<div class="astral-computers-actions flex flex-wrap gap-2">{"".join(buttons)}</div></td></tr>')


def _this_computer_html(tc: Optional[Dict[str, Any]]) -> str:
    if tc is None:
        return ""
    if tc["enabled"]:
        state = f'Remote control is <b>on</b> — this computer ({esc(tc["name"] or "")}) can be driven from your other devices.'
        btn = (f'<button type="button" class="{_BTN}" data-ui-action="computer_host_consent" '
               f"data-ui-payload='{{\"enabled\":false}}'>Stop allowing</button>")
    else:
        state = 'Remote control is <b>off</b> for this computer.'
        btn = (f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="computer_host_consent" '
               f"data-ui-payload='{{\"enabled\":true}}'>Allow remote control</button>")
    return ('<div class="bg-white/5 border border-astral-primary/30 rounded-lg p-4">'
            '<div class="flex flex-wrap items-center justify-between gap-3">'
            f'<div class="text-sm"><div class="text-astral-text font-medium">This computer</div>'
            f'<div class="text-astral-muted">{state} While a session runs, a banner with Pause and Stop '
            'stays on screen, and using the mouse or keyboard here pauses it.</div></div>'
            f'<div class="flex gap-2 shrink-0">{btn}</div></div></div>')


async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    if not _enabled():
        return f'<p class="text-sm text-astral-muted">{esc(_DISABLED_MSG)}</p>'
    rows = _rows(orch, user_id)
    online_count = sum(bool(h["online"]) for h in rows)
    summary = f"{online_count} online · {len(rows)} total" if rows else "Connect your first desktop"
    body = (
        '<div class="astral-computers-table-wrap">'
        '<table class="astral-computers-table">'
        '<caption class="astral-sr-only">Your connected computers and remote control sessions</caption>'
        '<thead><tr><th scope="col">Computer</th><th scope="col">Status</th>'
        '<th scope="col">Session</th><th scope="col">Actions</th></tr></thead>'
        f'<tbody>{"".join(_host_html(h) for h in rows)}</tbody></table></div>'
        if rows else
        '<div class="astral-computers-empty bg-white/5 border border-white/10 rounded-lg p-4 space-y-3">'
        '<svg class="astral-computers-empty-icon" width="32" height="32" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">'
        '<rect x="3" y="3" width="18" height="13" rx="2"/>'
        '<path d="M8 21h8m-4-5v5"/></svg>'
        '<h3 class="text-astral-text font-semibold">No computers connected</h3>'
        '<p class="text-sm text-astral-muted">Connect a desktop to work with its screen, apps, '
        'and files from this device.</p>'
        '<ol class="astral-computers-setup text-sm text-astral-muted">'
        '<li>Open the AstralDeep desktop client and sign in to this account.</li>'
        '<li>Go to Settings → Remote control and turn on Allow remote control.</li>'
        '<li>Refresh this list, choose your computer, then ask for what you need in chat.</li>'
        '</ol></div>'
    )
    return (
        '<div class="astral-computers space-y-4">'
        '<div class="astral-computers-heading flex flex-wrap items-center justify-between gap-3">'
        '<div><h3 class="text-astral-text font-semibold">Connected computers</h3>'
        f'<p class="text-sm text-astral-muted">{esc(summary)}</p></div>'
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_open" '
        'data-ui-payload=\'{"surface":"my_computers"}\'>Refresh</button></div>'
        f'{_this_computer_html(_this_computer(orch, user_id, params))}{body}'
        '<div class="astral-computers-safety bg-white/5 border border-white/10 rounded-lg p-4 space-y-2">'
        '<h3 class="text-sm text-astral-text font-medium">You stay in control</h3>'
        '<p class="text-sm text-astral-muted">Commands and file changes require your approval. '
        'Anyone at the computer can pause or stop a session; using its mouse or keyboard pauses it.</p>'
        '</div></div>'
    )


async def components(orch: Any, user_id: str, roles: Any, params: Any):
    from webrender.chrome.surfaces import _sdui

    if not _enabled():
        return [_sdui.text(_DISABLED_MSG, "caption")]
    rows = _rows(orch, user_id)
    out = []
    tc = _this_computer(orch, user_id, params)
    if tc is not None:
        if tc["enabled"]:
            out.append(_sdui.card("This computer", [
                _sdui.badge("remote control on", "success"),
                _sdui.button("Stop allowing", "computer_host_consent", payload={"enabled": False},
                             variant="secondary"),
            ]))
        else:
            out.append(_sdui.card("This computer", [
                _sdui.badge("remote control off", "default"),
                _sdui.text("You stay in control: a banner with Pause and Stop is always visible "
                           "during a session, commands and file changes ask you to approve, and "
                           "touching the mouse or keyboard here pauses the session.", "caption"),
                _sdui.button("Allow remote control", "computer_host_consent", payload={"enabled": True}),
            ]))
    for h in rows:
        screens = ", ".join(f'{s["width"]}×{s["height"]}' for s in h.get("screens") or []) or "?"
        facts = _sdui.key_value([
            {"label": "Status", "value": "online" if h["online"] else f"offline · last seen {_ago(h['last_seen'])}"},
            {"label": "Platform", "value": h["platform"]},
            {"label": "Screens", "value": screens},
            {"label": "Session", "value": _session_line(h)},
        ], columns=2)
        buttons = []
        s = h.get("session")
        if h["online"]:
            if s is None:
                buttons.append(_sdui.button("Control this computer", "chrome_computer_session_start",
                                            payload={"host_id": h["host_id"]}))
            else:
                if _remotely_resumable(s):
                    buttons.append(_sdui.button("Resume", "chrome_computer_session_resume",
                                                payload={"session_id": s["session_id"]}))
                elif s["state"] != "paused":
                    buttons.append(_sdui.button("Pause", "chrome_computer_session_pause",
                                                payload={"session_id": s["session_id"]}, variant="secondary"))
                buttons.append(_sdui.button("Stop", "chrome_computer_session_stop",
                                            payload={"session_id": s["session_id"]}, variant="secondary"))
        else:
            buttons.append(_sdui.button("Forget", "chrome_computer_forget",
                                        payload={"host_id": h["host_id"]}, variant="secondary"))
        badge = _sdui.badge("online" if h["online"] else "offline",
                            "success" if h["online"] else "default")
        out.append(_sdui.card(h["name"], [badge, facts, _sdui.container(buttons, direction="row")]))
    return out


def _owned_session(orch, user_id: str, payload):
    session_id = str((payload or {}).get("session_id") or "")
    session = orch.computer_sessions.get(session_id)
    if session is None or session.owner_sub != user_id:
        return None
    return session


async def _h_session_start(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    host_id = str((payload or {}).get("host_id") or "")
    host = orch.computer_hosts.get(user_id, host_id)
    if host is None:
        return (SURFACE_KEY, {}, notice_block("error", "That computer is not online."))
    chat_id = orch._ws_active_chat.get(id(websocket))
    try:
        await orch.computer_sessions.start(user_id, host, websocket, chat_id)
    except ComputerHostError as exc:
        return (SURFACE_KEY, {}, notice_block("error", esc(exc.message)))
    return (SURFACE_KEY, {}, notice_block(
        "success", f"Controlling {esc(host.name)}. Ask in chat for what you want done — for "
                   "example “take a screenshot” or “open Notepad and type hello”."))


async def _h_session_stop(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    session = _owned_session(orch, user_id, payload)
    if session is None:
        return (SURFACE_KEY, {}, notice_block("error", "That session is not running."))
    await orch.computer_sessions.end(session, "user_stop")
    return (SURFACE_KEY, {}, notice_block("success", f"Stopped controlling {esc(session.host_name)}."))


async def _h_session_pause(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    session = _owned_session(orch, user_id, payload)
    if session is None:
        return (SURFACE_KEY, {}, notice_block("error", "That session is not running."))
    await orch.computer_sessions.pause(session, "user_pause")
    return (SURFACE_KEY, {}, notice_block("success", f"Paused {esc(session.host_name)}."))


async def _h_session_resume(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    session = _owned_session(orch, user_id, payload)
    if session is None:
        return (SURFACE_KEY, {}, notice_block("error", "That session is not running."))
    try:
        await orch.computer_sessions.resume(session)
    except ComputerHostError as exc:
        if exc.code == "local_resume_required":
            return (SURFACE_KEY, {}, notice_block(
                "error", f"{esc(session.host_name)} was paused at that computer. Resume it there."))
        if exc.code != "local_pause_requires_local_resume":
            raise
        return (SURFACE_KEY, {}, notice_block(
            "error", f"{esc(session.host_name)} was paused on that Mac. Resume it there."))
    return (SURFACE_KEY, {}, notice_block("success", f"Resumed {esc(session.host_name)}."))


async def _h_grant_revoke(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    values = [payload.get(name) for name in _GRANT_BINDING] if isinstance(payload, dict) else []
    if (not values or set(payload) - set(_GRANT_BINDING) - {"surface"}
            or not all(isinstance(value, str) and 0 < len(value) <= 256 for value in values)
            or not await orch.computer_sessions.revoke_grant(user_id, *values)):
        return (SURFACE_KEY, {}, notice_block("error", "That grant is not active."))
    return (SURFACE_KEY, {}, notice_block("success", "Grant revoked."))


async def _h_forget(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    host_id = str((payload or {}).get("host_id") or "")
    if orch.computer_hosts.forget(user_id, host_id):
        return (SURFACE_KEY, {}, notice_block("success", "Forgotten. It reappears if the client announces again."))
    return (SURFACE_KEY, {}, notice_block("error", "Only an offline computer can be forgotten."))


HANDLERS = {
    "chrome_computer_session_start": _h_session_start,
    "chrome_computer_session_stop": _h_session_stop,
    "chrome_computer_session_pause": _h_session_pause,
    "chrome_computer_session_resume": _h_session_resume,
    "chrome_computer_forget": _h_forget,
    "chrome_computer_grant_revoke": _h_grant_revoke,
}
