"""Feature 037 / 040 — server-driven chat-history surface tests.

Covers the history component builders (skeleton + the recent-chats
``chat_history`` primitive), their web rendering, the relative-time and
saved-components enrichment, and ROTE adaptation (watch condense + voice
collapse). Pure Python — no DB, no socket.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.history_surface import (  # noqa: E402
    _relative_time,
    history_skeleton_components,
    history_surface_components,
)
from rote.adapter import ComponentAdapter  # noqa: E402
from rote.capabilities import DeviceProfile  # noqa: E402
from webrender.renderer import render  # noqa: E402

NOW_MS = 1_700_000_000_000  # fixed "now" in epoch ms for deterministic times
NOW_S = NOW_MS / 1000.0


def _row(**kw):
    base = {"id": "c1", "title": "Trip to Rome", "updated_at": NOW_MS}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# Skeleton (loading) state — feature 037
# --------------------------------------------------------------------------

def test_skeleton_is_the_placeholder_alone():
    # No heading of its own: the surface that hosts the list already has one.
    comps = history_skeleton_components()
    assert [c["type"] for c in comps] == ["skeleton"]
    assert comps[0]["variant"] == "chat-history"
    assert "astral-skeleton" in render(comps)


# --------------------------------------------------------------------------
# Loaded state — the chat_history primitive (feature 040)
# --------------------------------------------------------------------------

def test_surface_builds_chat_history_primitive():
    comps = history_surface_components([
        _row(id="c1", title="Trip to Rome"),
        _row(id="c2", title=""),  # blank title → fallback
    ])
    assert len(comps) == 1
    ch = comps[0]
    assert ch["type"] == "chat_history"
    items = ch["items"]
    assert [it["chat_id"] for it in items] == ["c1", "c2"]
    assert items[0]["title"] == "Trip to Rome"
    assert items[1]["title"] == "Untitled chat"  # blank → fallback
    html = render(comps)
    # the row is a real button carrying the load_chat dispatch contract
    assert "load_chat" in html and 'data-action="load_chat"' in html
    assert "astral-action astral-history-item" in html
    assert "Trip to Rome" in html
    assert '"chat_id": "c1"' in html or "chat_id&quot;: &quot;c1" in html


def test_surface_accepts_chat_id_key():
    comps = history_surface_components([{"chat_id": "x9", "title": "Alt key", "updated_at": NOW_MS}])
    assert comps[0]["items"][0]["chat_id"] == "x9"


def test_surface_enriches_preview_time_and_saved():
    comps = history_surface_components([
        _row(agent_id="weather", preview="Clear skies today",
             updated_at=NOW_MS - 2 * 3600 * 1000, has_saved_components=True),
    ])
    it = comps[0]["items"][0]
    assert it["preview"] == "Clear skies today"
    assert it["saved"] is True
    # rendered HTML surfaces the saved marker + preview text
    html = render(comps)
    assert "astral-history-saved" in html
    assert "Clear skies today" in html


def test_rows_carry_no_picture_and_the_list_carries_no_heading():
    # The avatar was a tag for the agent that answered, which most chats do not
    # have, so the list showed a column of blanks; and the heading repeated
    # whatever heading already hosts the list.
    comps = history_surface_components([_row(agent_id="weather"), _row(chat_id="c2")])
    assert all("icon" not in it for it in comps[0]["items"])
    html = render(comps)
    assert "astral-history-avatar" not in html
    assert "astral-history-head" not in html
    assert "astral-history-count" not in html
    assert "astral-history-item" in html


def test_surface_empty_and_idless_render_empty_state():
    empty = history_surface_components([])
    assert empty[0]["type"] == "chat_history"
    assert empty[0]["items"] == []
    assert "No conversations yet." in render(empty)
    # a chat with no id cannot be opened → skipped → empty state
    idless = history_surface_components([{"title": "no id"}])
    assert idless[0]["items"] == []


def test_render_escapes_titles():
    comps = history_surface_components([_row(title="<script>alert(1)</script>")])
    html = render(comps)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------
# Relative-time helper
# --------------------------------------------------------------------------

def test_relative_time_buckets():
    assert _relative_time(NOW_MS, now=NOW_S) == "just now"
    assert _relative_time(NOW_MS - 5 * 60 * 1000, now=NOW_S) == "5m"
    assert _relative_time(NOW_MS - 3 * 3600 * 1000, now=NOW_S) == "3h"
    assert _relative_time(NOW_MS - 2 * 86400 * 1000, now=NOW_S) == "2d"
    assert _relative_time(NOW_MS - 14 * 86400 * 1000, now=NOW_S) == "2w"


def test_relative_time_tolerates_bad_values():
    assert _relative_time(None) == ""
    assert _relative_time("") == ""
    assert _relative_time("not-a-number") == ""
    # epoch SECONDS (not ms) are also handled
    assert _relative_time(NOW_S, now=NOW_S) == "just now"


# --------------------------------------------------------------------------
# ROTE adaptation
# --------------------------------------------------------------------------

def test_watch_condenses_rows_and_drops_preview():
    rows = [_row(id=f"c{i}", title=f"Chat {i}", preview="some preview") for i in range(8)]
    out = ComponentAdapter.adapt(history_surface_components(rows),
                                 DeviceProfile.from_dict({"device_type": "watch"}))
    items = out[0]["items"]
    assert len(items) == 4  # trimmed for the watch
    assert all("preview" not in it for it in items)  # preview dropped
    assert all(it.get("title") for it in items)  # titles kept


def test_browser_passes_through_unchanged():
    comps = history_surface_components([_row(preview="keep me")])
    out = ComponentAdapter.adapt(comps, DeviceProfile.from_dict({"device_type": "browser"}))
    assert out[0]["items"][0]["preview"] == "keep me"


def test_voice_speaks_chat_titles():
    out = ComponentAdapter.adapt(
        history_surface_components([_row(title="Budget review")]),
        DeviceProfile.from_dict({"device_type": "voice"}))
    joined = " ".join(c.get("content", "") for c in out)
    assert "Budget review" in joined and "Recent chats" in joined


def test_voice_collapses_loading_state():
    out = ComponentAdapter.adapt(history_skeleton_components(),
                                 DeviceProfile.from_dict({"device_type": "voice"}))
    assert all(c["type"] == "text" for c in out)
    joined = " ".join(c["content"] for c in out)
    assert "Loading" in joined
