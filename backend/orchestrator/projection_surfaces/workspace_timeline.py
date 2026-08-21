"""Deep-owned host adapter for the workspace-timeline Projection surface.

Lists a chat's workspace snapshots (one per turn / component-action
mutation); selecting one pushes a full historical canvas render flagged
read-only, with an explicit "Back to live" affordance (FR-031/FR-032).
Viewing history is audited (FR-033) and mutating component actions are
refused server-side while a socket is in timeline mode.
"""
import asyncio
import json
from datetime import datetime, timezone

from webrender import esc
from webrender.chrome.surfaces import _sdui

TITLE = "Workspace timeline"
ADMIN_ONLY = False

_PAGE_SIZE = 50

_CAUSE_LABELS = {
    "turn": "Assistant turn",
    "component_action": "Component action",
    "combine": "Components combined",
    "condense": "Components condensed",
    "remove": "Component removed",
}


def _fmt_ts(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return ""


async def render(orch, user_id, roles, params) -> str:
    chat_id = str((params or {}).get("chat_id") or "")
    page = max(0, int((params or {}).get("page") or 0))
    if not chat_id:
        return ('<p class="text-sm text-astral-muted">Open a chat first — the timeline '
                'shows that chat’s workspace as it was at each turn.</p>')
    snaps = await asyncio.to_thread(
        orch.workspace.list_snapshots, chat_id, user_id,
        limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
    total = await asyncio.to_thread(orch.workspace.count_snapshots, chat_id, user_id)
    if not snaps:
        return ('<p class="text-sm text-astral-muted">No workspace history yet for this chat. '
                'Snapshots appear as turns produce or update components.</p>')

    rows = []
    # Newest-first list; number entries as turns counting back from total.
    for i, s in enumerate(snaps):
        n = total - (page * _PAGE_SIZE) - i
        label = _CAUSE_LABELS.get(s.get("cause"), s.get("cause", ""))
        view_payload = esc(json.dumps({"chat_id": chat_id, "snapshot_id": s["id"]}))
        rows.append(
            f'<button type="button" data-ui-action="chrome_workspace_timeline_view" '
            f"data-ui-payload='{view_payload}' "
            f'class="w-full flex items-center justify-between px-3 py-2 rounded-lg text-left '
            f'text-sm text-astral-text hover:bg-white/5 focus:bg-white/10 focus:outline-none">'
            f'<span>#{n} · {esc(label)}</span>'
            f'<span class="text-xs text-astral-muted">{esc(_fmt_ts(s.get("created_at")))}</span>'
            f'</button>'
        )

    nav = []
    if page > 0:
        prev_payload = esc(json.dumps({"surface": "workspace_timeline",
                                       "params": {"chat_id": chat_id, "page": page - 1}}))
        nav.append(f'<button type="button" data-ui-action="chrome_open" data-ui-payload=\'{prev_payload}\' '
                   f'class="px-3 py-1.5 rounded-lg text-xs bg-white/5 border border-white/10 '
                   f'text-astral-text">Newer</button>')
    if (page + 1) * _PAGE_SIZE < total:
        next_payload = esc(json.dumps({"surface": "workspace_timeline",
                                       "params": {"chat_id": chat_id, "page": page + 1}}))
        nav.append(f'<button type="button" data-ui-action="chrome_open" data-ui-payload=\'{next_payload}\' '
                   f'class="px-3 py-1.5 rounded-lg text-xs bg-white/5 border border-white/10 '
                   f'text-astral-text">Older</button>')
    live_payload = esc(json.dumps({"chat_id": chat_id}))
    header = (
        '<div class="flex items-center justify-between mb-2">'
        f'<p class="text-xs text-astral-muted">Viewing the past never changes the live workspace.</p>'
        f'<button type="button" data-ui-action="chrome_workspace_timeline_live" '
        f"data-ui-payload='{live_payload}' "
        f'class="px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary text-white">'
        f'Back to live</button></div>'
    )
    nav_html = f'<div class="flex gap-2 justify-end mt-2">{"".join(nav)}</div>' if nav else ""
    return header + '<div class="space-y-0.5">' + "".join(rows) + "</div>" + nav_html


async def components(orch, user_id, roles, params):
    """Feature 044 — the workspace timeline as native SDUI components.

    Mirrors ``render()`` exactly (same data + the SAME actions): a snapshot
    list newest-first ("#n · <cause> · <timestamp>") whose rows fire
    ``chrome_workspace_timeline_view``, Newer/Older paging via ``chrome_open``,
    and a ``chrome_workspace_timeline_live`` "Back to live" button. The web
    ``render()`` HTML is unchanged (contract §3.1); the paging payloads match
    the web nav so history navigation behaves identically on either target.
    """
    chat_id = str((params or {}).get("chat_id") or "")
    page = max(0, int((params or {}).get("page") or 0))
    if not chat_id:
        return [_sdui.alert("Open a chat first — the timeline shows that chat's "
                            "workspace as it was at each turn.", "info")]
    snaps = await asyncio.to_thread(
        orch.workspace.list_snapshots, chat_id, user_id,
        limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
    total = await asyncio.to_thread(orch.workspace.count_snapshots, chat_id, user_id)
    if not snaps:
        return [_sdui.alert("No workspace history yet for this chat. Snapshots appear "
                            "as turns produce or update components.", "info")]
    out = [
        _sdui.text("Viewing the past never changes the live workspace.", "caption"),
        _sdui.button("Back to live", "chrome_workspace_timeline_live",
                     {"chat_id": chat_id}, variant="primary"),
    ]
    # Newest-first list; number entries as turns counting back from total —
    # identical to render().
    for i, s in enumerate(snaps):
        n = total - (page * _PAGE_SIZE) - i
        label = _CAUSE_LABELS.get(s.get("cause"), s.get("cause", ""))
        out.append(_sdui.button(
            f"#{n} · {label} · {_fmt_ts(s.get('created_at'))}".rstrip(" ·"),
            "chrome_workspace_timeline_view",
            {"chat_id": chat_id, "snapshot_id": s["id"]},
            variant="secondary"))
    nav = []
    if page > 0:
        nav.append(_sdui.button("Newer", "chrome_open",
                                {"surface": "workspace_timeline",
                                 "params": {"chat_id": chat_id, "page": page - 1}}))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(_sdui.button("Older", "chrome_open",
                                {"surface": "workspace_timeline",
                                 "params": {"chat_id": chat_id, "page": page + 1}}))
    if nav:
        out.append(_sdui.container(nav, direction="row"))
    return out


def _banner_components(snapshot, chat_id):
    """Read-only banner prepended to a historical canvas render."""
    label = _CAUSE_LABELS.get(snapshot.get("cause"), snapshot.get("cause", ""))
    return [
        {
            "type": "alert",
            "variant": "warning",
            "message": (f"Viewing workspace history ({label}, {_fmt_ts(snapshot.get('created_at'))}) "
                        "— read-only."),
        },
        {
            "type": "button",
            "label": "Back to live",
            "variant": "primary",
            "action": "chrome_workspace_timeline_live",
            "payload": {"chat_id": chat_id},
        },
    ]


async def _view(orch, websocket, user_id, roles, payload):
    """Push a historical, read-only canvas render for one snapshot."""
    chat_id = str((payload or {}).get("chat_id") or "")
    try:
        snapshot_id = int((payload or {}).get("snapshot_id"))
    except (TypeError, ValueError):
        return ("workspace_timeline", {"chat_id": chat_id},
                '<p class="text-xs text-red-400 mb-2">That snapshot link is invalid.</p>')
    snap = await asyncio.to_thread(orch.workspace.get_snapshot, snapshot_id, user_id)
    if snap is None or (chat_id and snap.get("chat_id") != chat_id):
        return ("workspace_timeline", {"chat_id": chat_id},
                '<p class="text-xs text-red-400 mb-2">That snapshot no longer exists.</p>')
    chat_id = snap["chat_id"]

    orch._ws_timeline_mode[id(websocket)] = True
    try:
        from audit.hooks import record_workspace_event
        await record_workspace_event(
            user_id=user_id, action="timeline_viewed", chat_id=chat_id,
            description=f"Viewed workspace snapshot {snapshot_id}",
            detail={"snapshot_id": snapshot_id, "cause": snap.get("cause", "")},
        )
    except Exception:
        pass

    # Tell the client to defer live canvas updates while in the past.
    await orch._safe_send(websocket, json.dumps({
        "type": "workspace_timeline_mode", "active": True, "chat_id": chat_id,
        "snapshot_id": snapshot_id,
    }))
    # Feature 029: snapshots carry the designed arrangements that were live at
    # capture time — materialize them so history looks the way it looked.
    # (Pre-029 snapshots have no layouts and render flat, as before.)
    snap_components = list(snap.get("components") or [])
    layouts = [lay for lay in (snap.get("layouts") or []) if isinstance(lay, dict)]
    if layouts:
        from orchestrator.ui_designer import materialize
        from orchestrator.workspace import iter_layout_refs
        by_id = {c.get("component_id"): c for c in snap_components
                 if isinstance(c, dict) and c.get("component_id")}
        claimed = set()
        for lay in layouts:
            claimed |= set(iter_layout_refs(lay.get("layout") or []))
        body = [c for c in snap_components
                if isinstance(c, dict) and c.get("component_id") not in claimed]
        for lay in sorted(layouts, key=lambda item: item.get("position") or 0):
            body.extend(materialize(lay.get("layout") or [], by_id))
        snap_components = body
    components = _banner_components(snap, chat_id) + snap_components
    await orch.send_ui_render(websocket, components)
    # Close the modal so the historical canvas is visible. Feature 044: the
    # close is device-aware (contract §2/§3.1) — web clears the HTML modal
    # region, native SDUI clients (windows/android) get the documented
    # empty-components ChromeSurface (was a web-only ChromeRender the natives
    # couldn't read). Reuses the same helper the chrome_close action uses.
    await _close_modal(orch, websocket)
    return None


async def _live(orch, websocket, user_id, roles, payload):
    """Return to the live workspace exactly as it now stands (FR-032)."""
    chat_id = str((payload or {}).get("chat_id") or "") or orch._ws_active_chat.get(id(websocket), "")
    orch._ws_timeline_mode.pop(id(websocket), None)
    await orch._safe_send(websocket, json.dumps({
        "type": "workspace_timeline_mode", "active": False, "chat_id": chat_id,
    }))
    if chat_id:
        try:
            # Feature 029: back-to-live restores the designed canvas when the
            # orchestrator provides the materializer (test fakes may not).
            canvas_fn = getattr(orch, "_canvas_components", None)
            if canvas_fn:
                components = await asyncio.to_thread(canvas_fn, chat_id, user_id)
            else:
                components = await asyncio.to_thread(
                    orch.workspace.live_components, chat_id, user_id)
            await orch.send_ui_render(websocket, components or [])
        except Exception:
            import logging
            logging.getLogger("Orchestrator.Chrome").exception("back-to-live render failed")
    # Feature 044: device-aware modal close (see _view).
    await _close_modal(orch, websocket)
    return None


async def _close_modal(orch, websocket):
    """Feature 044 — device-aware modal close shared by ``_view``/``_live``.

    Delegates to the chrome dispatcher's ``push_close`` so the frame is
    IDENTICAL to the ``chrome_close`` action: an empty-HTML ``chrome_render``
    for web, an empty-components ``chrome_surface`` for native SDUI
    (windows/android). Imported lazily to avoid an import cycle (chrome_events
    imports the surface registry)."""
    from orchestrator.chrome_events import push_close
    await push_close(orch, websocket)


HANDLERS = {
    "chrome_workspace_timeline_view": _view,
    "chrome_workspace_timeline_live": _live,
}
