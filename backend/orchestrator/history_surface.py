"""Server-driven chat-history surface components.

Builds the astralprims components for the recent-chats list and its loading
skeleton. The orchestrator delivers them via ``send_ui_render(target="history")``
so ROTE adapts them per device (browser/tablet/TV full list, mobile/watch
condensed, voice spoken) — the history surface is server-driven and
cross-platform, never a web-only client render.

The loaded state is a single ``chat_history`` primitive (rendered by
``webrender.render_chat_history``) — scannable conversation rows with a title,
a last-message preview, a relative time and a saved-components marker — instead
of a bare stack of title-only buttons. All enrichment (relative time, saved
flag) is derived here from the recent-chats rows the orchestrator already
supplies, so no new query or schema is needed.

Pure builders (no orchestrator/DB dependency) so they are unit-testable on
their own; the orchestrator only supplies the recent-chats rows.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from webrender.renderer import skeleton_component

#: Cap the rendered list; older chats stay reachable via search/scroll.
MAX_HISTORY_ITEMS = 20


def history_skeleton_components(label: str = "Loading your chats…") -> List[Dict[str, Any]]:
    """The loading state: a chat-history skeleton.

    No heading: whatever hosts this list already has one, and a second one
    under it was the sidebar saying "History" and then "Recent chats"."""
    return [skeleton_component(variant="chat-history", count=6, label=label)]


def _chat_id(chat: Dict[str, Any]) -> Optional[str]:
    cid = chat.get("id") or chat.get("chat_id")
    return str(cid) if cid else None


def _relative_time(value: Any, *, now: Optional[float] = None) -> str:
    """A compact relative-time label ("just now", "3m", "2h", "5d", "3w") from
    an epoch-millisecond timestamp (how ``chats.updated_at`` is stored). Epoch
    seconds and numeric strings are tolerated; anything unparseable yields ""
    (the row simply shows no time) so a bad value never breaks the surface.
    """
    if value is None or value == "":
        return ""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    # Heuristic: values >= ~10^11 are milliseconds; smaller are seconds.
    if ts >= 1e11:
        ts /= 1000.0
    current = time.time() if now is None else now
    delta = current - ts
    if delta < 0:
        delta = 0
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 604800:
        return f"{int(delta // 86400)}d"
    if delta < 2629800:  # ~1 month
        return f"{int(delta // 604800)}w"
    if delta < 31557600:  # ~1 year
        return f"{int(delta // 2629800)}mo"
    return f"{int(delta // 31557600)}y"


def history_surface_components(chats: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The loaded state: a single ``chat_history`` primitive.

    Each recent-chats row becomes an item with its title, last-message preview,
    relative time and saved-components marker. A chat with no id is skipped (it
    cannot be opened). With no openable chats the surface renders a friendly
    empty state (handled by the renderer).

    Rows carry no picture. The only thing there was to draw was a tag for the
    agent that answered, and most chats have no single agent, so what the list
    actually showed was a column of blanks.
    """
    items: List[Dict[str, Any]] = []
    for chat in list(chats or [])[:MAX_HISTORY_ITEMS]:
        if not isinstance(chat, dict):
            continue
        cid = _chat_id(chat)
        if not cid:
            continue
        title = str(chat.get("title") or "Untitled chat").strip() or "Untitled chat"
        items.append({
            "chat_id": cid,
            "title": title,
            "preview": str(chat.get("preview") or "").strip(),
            "time": _relative_time(chat.get("updated_at")),
            "saved": bool(chat.get("has_saved_components")),
        })
    return [{"type": "chat_history", "title": "Recent chats", "items": items}]
