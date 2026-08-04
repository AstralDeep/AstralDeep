"""Top bar + static settings menu (server-rendered, role-gated).

Rendered into the shell at ``GET /`` so the menu is *static*: always present,
opens with zero server round-trip. The Admin tools group is rendered ONLY when
the session roles include ``admin`` — absent from the DOM for everyone else
(UX-only gating, server-side checks stay authoritative). Entries whose
availability rule fails are omitted; empty groups hide their heading.

Feature 042: the menu structure is NOT defined here — it comes from the single
server-owned :func:`webrender.chrome.menu_model.build_menu_model`, the same
model the native clients consume over ``chrome_menu`` / ``GET /api/chrome/menu``
(Constitution II/XII: one definition, every client renders it). This module is
purely the *web renderer* of that model — it owns the HTML/CSS + web-specific
presentation details (DOM ids, tooltips, tour targets, icon SVGs), not the
inventory.

Menu markup follows the WAI-ARIA menu pattern; the keyboard and open/close
behavior lives in ``webrender/static/client.js``.
"""
import json

from webrender import esc
from webrender.chrome.menu_model import build_menu_model

_GEAR_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 '
    '0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 '
    '1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 '
    '1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 '
    '1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 '
    '0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 '
    '2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 '
    '0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 '
    '2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'
)

# "history" glyph (clock + counter-clockwise arrow) for the top-bar
# Workspace-timeline button: a recognizable "go back to an earlier version"
# affordance sitting right next to Settings.
_HISTORY_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>'
    '<path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>'
)

# "plus" glyph for the New-chat button — the same core affordance the native
# clients hardcode in their top bars (Windows "＋ New", Android's New-chat
# action). Client-local behavior lives in client.js (#astral-newchat-btn).
_PLUS_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 5v14M5 12h14"/></svg>'
)

# "speech bubble" glyph for the Recent-chats button — the same affordance
# Android hardcodes in its top bar (RootScaffold ic_chat). Only shown by the
# stylesheet on stacked (compact-width) layouts, where the persistent history
# rail is hidden and recent chats open full-screen instead.
_CHATS_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'
)

# "sparkle" glyph for the Pulse digest button — a recognizable "here's what I
# noticed" affordance. Only rendered when FF_PULSE_DIGEST is enabled.
_PULSE_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1'
    'M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/><circle cx="12" cy="12" r="3"/></svg>'
)

# Page-scoped canvas actions (066): export the whole canvas, share the whole
# canvas. These used to live in a sticky bar pinned above the canvas content,
# which cost a permanent strip of the primary surface for two rarely-used
# controls. They now sit in the top bar and are revealed by client.js only
# while the rendered canvas actually carries the matching workspace flag
# (`data-astral-export` / `data-astral-share`), so an unflagged or empty canvas
# shows nothing at all. The native clients expose the same two actions from the
# component context menu (Windows `component_menu`, Android `ArtifactChrome`) —
# neither ever had a persistent bar.
_EXPORT_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
)
_SHARE_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>'
    '<line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>'
    '<line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>'
)

# icon id (from the model) -> SVG glyph
_ICON_SVG = {"gear": _GEAR_SVG, "history": _HISTORY_SVG, "sparkle": _PULSE_SVG}

# Web-specific presentation for the interactive top-bar icon buttons, keyed by
# the model control key. The MODEL owns presence/order/action; the web renderer
# owns these DOM ids / tooltips / tour anchors.
_TOPBAR_BTN_CHROME = {
    "pulse": {
        "id": "astral-pulse-btn",
        "tour": "topbar.pulse",
        "title": "Pulse — what the assistant worked out while you were away",
    },
    "timeline": {
        "id": "astral-timeline-btn",
        "tour": "topbar.timeline",
        "title": "Workspace timeline — revisit an earlier version of this canvas",
    },
}


def _icon_button(control) -> str:
    """Render a top-bar ``action`` control (pulse/timeline) as an icon button
    that fires the generic ``chrome_open`` delegation (client.js injects the
    active chat id into params where needed)."""
    chrome = _TOPBAR_BTN_CHROME.get(control.key, {})
    payload = json.dumps(control.action.to_dict()) if control.action else "{}"
    svg = _ICON_SVG.get(control.icon or "", "")
    btn_id = chrome.get("id", f"astral-{control.key}-btn")
    tour = chrome.get("tour", f"topbar.{control.key}")
    title = chrome.get("title", control.label or "")
    return (
        f'<button type="button" id="{esc(btn_id)}" data-tour-target="{esc(tour)}" '
        'class="flex items-center justify-center p-1.5 rounded-lg text-astral-muted '
        'hover:text-astral-text hover:bg-white/5" '
        f'aria-label="{esc(control.label or "")}" title="{esc(title)}" '
        'data-ui-action="chrome_open" '
        f"data-ui-payload='{esc(payload)}'>{svg}</button>"
    )


def _menu_html(model) -> str:
    """The settings dropdown inner HTML, rendered from the model's groups +
    the sign-out entry."""
    items = []
    for group in model.menu:
        items.append(
            f'<div class="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider '
            f'text-astral-muted" role="presentation">{esc(group.label)}</div>'
        )
        for item in group.items:
            payload = json.dumps({"surface": item.surface, "params": item.params})
            items.append(
                f'<button type="button" role="menuitem" tabindex="-1" '
                f'class="astral-menu-item w-full text-left px-3 py-2 text-sm text-astral-text '
                f'hover:bg-white/5 focus:bg-white/10 focus:outline-none rounded-lg" '
                f'data-menu-key="{esc(item.key)}" data-tour-target="sidebar.{esc(item.key)}" '
                f"data-ui-action=\"chrome_open\" data-ui-payload='{esc(payload)}'>{esc(item.label)}</button>"
            )
    # Session group — Sign out is a plain link so it works without JS
    # (logout semantics live behind GET /auth/logout).
    so = model.signout
    items.append(
        '<div class="border-t border-white/5 mt-1 pt-1" role="presentation"></div>'
        '<a href="/auth/logout" role="menuitem" tabindex="-1" '
        'class="astral-menu-item block px-3 py-2 text-sm text-red-400 hover:bg-white/5 '
        f'focus:bg-white/10 focus:outline-none rounded-lg" data-menu-key="{esc(so.key)}">'
        f"{esc(so.label)}</a>"
    )
    return "".join(items)


def _settings_html(model) -> str:
    """The gear button + its dropdown, rendered from the model."""
    return (
        '<div class="relative" id="astral-settings">'
        '<button type="button" id="astral-settings-btn" data-tour-target="topbar.settings" '
        'class="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm text-astral-muted '
        'hover:text-astral-text hover:bg-white/5" aria-haspopup="menu" aria-expanded="false" '
        f'aria-controls="astral-settings-menu" aria-label="Settings">{_GEAR_SVG}'
        '<span class="hidden sm:inline">Settings</span></button>'
        '<div id="astral-settings-menu" role="menu" aria-label="Settings" hidden '
        'class="absolute right-0 mt-2 w-64 max-h-[70vh] overflow-y-auto rounded-xl border '
        'border-white/10 bg-astral-surface shadow-2xl p-1.5 z-50">'
        f"{_menu_html(model)}</div></div>"
    )


def render_topbar(roles=None) -> str:
    """Inner HTML for ``<header id="astral-topbar">`` — brand, status, Settings.

    Targets the web client only, but is rendered FROM the shared
    :func:`build_menu_model` so it can never diverge from what the native
    clients receive. ``roles`` comes from the server session at shell-render
    time (mock auth ⇒ admin) — see ``web_auth.session_roles``.
    """
    model = build_menu_model(roles)

    # Left cluster: brand. Right cluster: status + New chat + interactive
    # controls + gear, in model order (status, [pulse], timeline, settings).
    # New chat is core client chrome, not a settings surface — every native
    # client hardcodes it in its top bar (Windows TopBar.new_btn, Android
    # RootScaffold onNewChat); this is the web twin of that button.
    new_chat_btn = (
        '<button type="button" id="astral-newchat-btn" data-tour-target="topbar.new-chat" '
        'class="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm '
        'bg-astral-primary/20 border border-astral-primary/30 text-astral-text '
        'hover:bg-astral-primary/30" aria-label="New chat" title="Start a new chat">'
        f'{_PLUS_SVG}<span class="hidden sm:inline">New chat</span></button>'
    )
    # Recent chats — like New chat this is core client chrome the native
    # clients hardcode (Android RootScaffold's speech-bubble button). The
    # stylesheet keeps it hidden except on stacked (compact-width) layouts,
    # where it replaces the always-visible history rail (#astral-chats-btn).
    recent_chats_btn = (
        '<button type="button" id="astral-chats-btn" data-tour-target="topbar.chats" '
        'class="astral-chats-btn items-center justify-center p-1.5 rounded-lg '
        'text-astral-muted hover:text-astral-text hover:bg-white/5" '
        'aria-label="Recent chats" title="Recent chats" aria-expanded="false">'
        f"{_CHATS_SVG}</button>"
    )
    # 066 collapse-trap fix: when the conversation is hidden (collapsed
    # layout), the only reopen affordance was a subtle composer icon whose
    # re-pin action was a DOUBLE-click — owner-reported as "nothing to get
    # it back". This topbar twin restores the conversation in one click.
    # Rendered `hidden`; client.js unhides it exactly while the conversation
    # is hidden, and mirrors the unread badge.
    show_chat_btn = (
        '<button type="button" id="astral-topbar-chat-btn" hidden '
        'class="astral-topbar-chat items-center justify-center p-1.5 rounded-lg '
        'text-astral-muted hover:text-astral-text hover:bg-white/5 relative" '
        'aria-label="Show conversation" title="Show conversation">'
        f"{_CHATS_SVG}"
        '<span id="astral-topbar-chat-unread" class="astral-chat-unread" hidden>0</span>'
        "</button>"
    )
    # The two canvas page actions. Rendered `hidden`; client.js unhides each one
    # only while the canvas carries its flag. They keep the class names the
    # delegated click handlers already dispatch on, so moving them out of the
    # canvas changed their location and nothing about their behavior.
    page_action_btns = (
        '<button type="button" id="astral-export-page-btn" hidden '
        'class="astral-export-canvas astral-page-action items-center justify-center '
        'p-1.5 rounded-lg text-astral-muted hover:text-astral-text hover:bg-white/5" '
        'aria-label="Export page" title="Export page — download this canvas as HTML">'
        f"{_EXPORT_SVG}</button>"
        '<button type="button" id="astral-share-page-btn" hidden data-share-scope="canvas" '
        'class="astral-share-btn astral-page-action items-center justify-center '
        'p-1.5 rounded-lg text-astral-muted hover:text-astral-text hover:bg-white/5" '
        'aria-label="Share page" title="Share page — create a link to this canvas">'
        f"{_SHARE_SVG}</button>"
    )
    right_parts = []
    for control in model.topbar:
        if control.kind == "brand":
            continue  # brand is the left cluster (below)
        if control.kind == "status":
            right_parts.append(
                '<span id="astral-status" class="text-xs text-astral-muted" role="status"></span>'
            )
            right_parts.append(new_chat_btn)
            right_parts.append(recent_chats_btn)
            right_parts.append(show_chat_btn)
            right_parts.append(page_action_btns)
        elif control.kind == "action":
            right_parts.append(_icon_button(control))
        elif control.kind == "menu":  # the Settings gear + dropdown
            right_parts.append(_settings_html(model))

    return (
        '<div class="flex items-center justify-between px-4 py-3 w-full">'
        '<div class="flex items-center gap-2" data-tour-target="topbar.brand">'
        '<img src="/static/img/AstralDeep.png" alt="AstralDeep" '
        'class="h-8 w-auto select-none" draggable="false"></div>'
        '<div class="flex items-center gap-3">'
        + "".join(right_parts) +
        '</div></div>'
    )
