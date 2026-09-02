"""Deep-owned host adapter for the "My computers" surface (feature 076).

Surface key ``my_computers``. Lists the owner's desktops that announced
"Allow remote control", their presence and any running remote-control
session, and offers the session controls: Control this computer, Pause,
Resume, Stop, Forget. Web ``render()`` + native ``components()`` share the
same handler keys and payload shapes, so every client gets the surface with
zero per-client code (Constitution XII). Owner-scoped throughout; every entry
point re-checks ``FF_COMPUTER_USE`` (flag-off byte-identity, FR-004).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from webrender.chrome import esc, notice_block

from orchestrator.computer_hosts import ComputerHostError

TITLE = "My computers"
SURFACE_KEY = "my_computers"

_DISABLED_MSG = "Remote control is not enabled on this server."
_INTRO = ("Computers that run the AstralDeep desktop client with “Allow remote control” "
          "switched on appear here. Start a session, then ask in chat for what you want done "
          "— you'll see the screen update while it happens. Commands and file changes always "
          "ask you to approve first, and anyone at the computer can pause or stop.")
_EMPTY = ("No computers yet. On your PC, open the AstralDeep desktop client, go to "
          "Settings → Remote control and switch it on.")

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
    """For the desktop that is asking: whether it can host and whether its
    consent switch is on (it is a registered host). Phones/web get None."""
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


def _session_line(h: Dict[str, Any]) -> str:
    s = h.get("session")
    if not s:
        return "no session"
    if s["state"] == "paused":
        return f"paused ({s.get('pause_reason') or 'someone is using it'}) · from your {s['controller_label']}"
    return f"session active · from your {s['controller_label']} · {s['verbs_run']} actions"


# ── web ───────────────────────────────────────────────────────────────────────

def _host_html(h: Dict[str, Any]) -> str:
    hid = esc(h["host_id"])
    screens = ", ".join(f'{s["width"]}×{s["height"]}' for s in h.get("screens") or []) or "?"
    status = "online" if h["online"] else f"offline · last seen {_ago(h['last_seen'])}"
    s = h.get("session")
    buttons: List[str] = []
    if h["online"]:
        if s is None:
            buttons.append(f'<button type="button" class="{_BTN_PRIMARY}" '
                           f'data-ui-action="chrome_computer_session_start" '
                           f"data-ui-payload='{{\"host_id\":\"{hid}\"}}'>Control this computer</button>")
        else:
            sid = esc(s["session_id"])
            if s["state"] == "paused":
                buttons.append(f'<button type="button" class="{_BTN_PRIMARY}" '
                               f'data-ui-action="chrome_computer_session_resume" '
                               f"data-ui-payload='{{\"session_id\":\"{sid}\"}}'>Resume</button>")
            else:
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
        '<div class="bg-white/5 border border-white/10 rounded-lg px-3 py-2">'
        '<div class="flex items-center justify-between gap-3">'
        f'<div class="text-sm"><div class="text-astral-text font-medium">{esc(h["name"])}</div>'
        f'<div class="text-astral-muted">{esc(h["platform"])} · {esc(screens)} · {esc(status)} · '
        f'{esc(_session_line(h))}</div></div>'
        f'<div class="flex gap-2 shrink-0">{"".join(buttons)}</div></div></div>')


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
    return ('<div class="bg-white/5 border border-astral-primary/30 rounded-lg px-3 py-2">'
            '<div class="flex items-center justify-between gap-3">'
            f'<div class="text-sm"><div class="text-astral-text font-medium">This computer</div>'
            f'<div class="text-astral-muted">{state} While a session runs, a banner with Pause and Stop '
            'stays on screen, and using the mouse or keyboard here pauses it.</div></div>'
            f'<div class="flex gap-2 shrink-0">{btn}</div></div></div>')


async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    if not _enabled():
        return f'<p class="text-sm text-astral-muted">{esc(_DISABLED_MSG)}</p>'
    rows = _rows(orch, user_id)
    body = ('<div class="space-y-2">' + "".join(_host_html(h) for h in rows) + '</div>'
            if rows else f'<p class="text-sm text-astral-muted">{esc(_EMPTY)}</p>')
    return (f'<div class="space-y-4"><div class="text-sm text-astral-muted">{esc(_INTRO)}</div>'
            f'{_this_computer_html(_this_computer(orch, user_id, params))}{body}</div>')


# ── native SDUI ───────────────────────────────────────────────────────────────

async def components(orch: Any, user_id: str, roles: Any, params: Any):
    from webrender.chrome.surfaces import _sdui

    if not _enabled():
        return [_sdui.text(_DISABLED_MSG, "caption")]
    rows = _rows(orch, user_id)
    out = [_sdui.text(_INTRO, "caption")]
    tc = _this_computer(orch, user_id, params)
    if tc is not None:
        if tc["enabled"]:
            out.append(_sdui.card("This computer", [
                _sdui.badge("remote control on", "success"),
                _sdui.text(f"{tc['name']} can be driven from your other signed-in devices. While a "
                           "session runs, a banner with Pause and Stop stays on screen, and using the "
                           "mouse or keyboard here pauses it.", "caption"),
                _sdui.button("Stop allowing", "computer_host_consent", payload={"enabled": False},
                             variant="secondary"),
            ]))
        else:
            out.append(_sdui.card("This computer", [
                _sdui.badge("remote control off", "default"),
                _sdui.text("Switch this on to drive this computer from your phone or another "
                           "signed-in device. You stay in control: a banner with Pause and Stop is "
                           "always visible during a session, commands and file changes ask you to "
                           "approve, and touching the mouse or keyboard here pauses the session.", "caption"),
                _sdui.button("Allow remote control", "computer_host_consent", payload={"enabled": True}),
            ]))
    if not rows:
        out.append(_sdui.text(_EMPTY, "caption"))
        return out
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
                if s["state"] == "paused":
                    buttons.append(_sdui.button("Resume", "chrome_computer_session_resume",
                                                payload={"session_id": s["session_id"]}))
                else:
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


# ── handlers ──────────────────────────────────────────────────────────────────

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
    await orch.computer_sessions.resume(session)
    return (SURFACE_KEY, {}, notice_block("success", f"Resumed {esc(session.host_name)}."))


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
}
