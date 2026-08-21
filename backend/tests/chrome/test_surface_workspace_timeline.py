"""Feature 028 — workspace timeline chrome surface (FR-031/FR-032/FR-033/EC-5).

Exercises ``backend/orchestrator/projection_surfaces/workspace_timeline.py`` over a
REAL Postgres-backed ``WorkspaceManager``/``HistoryManager`` (uuid-unique
user/chat per test, FK CASCADE cleanup) and a fake orchestrator that captures
``_safe_send`` / ``send_ui_render`` traffic, mirroring
``backend/tests/test_component_action.py``.

Covers: the snapshot list view (newest-first numbering, cause labels, paging
via a monkeypatched ``_PAGE_SIZE``, empty states), ``_view`` pushing the EXACT
historical canvas with the read-only banner + ``workspace_timeline_mode``
flag + ``timeline_viewed`` audit (FR-031/FR-033), ``_live`` restoring the
EXACT current workspace including changes made while the past was open
(FR-032), and EC-5: a live upsert during timeline mode still persists,
snapshots and fans out — without touching the socket's server-side
timeline state.

Note on actual behavior: ``_view`` stores ``True`` (not the snapshot id) in
``orch._ws_timeline_mode[id(ws)]``; tests assert the implemented behavior.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.history import HistoryManager  # noqa: E402
from orchestrator.workspace import WorkspaceManager  # noqa: E402
from orchestrator.projection_surfaces import workspace_timeline as wt  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


class _FakeWS:
    """Hashable, identity-compared websocket stand-in (SimpleNamespace is
    unhashable, which breaks ROTE's profile map and fan-out targeting)."""

    def __init__(self, label: str = ""):
        self.label = label


def _make_orch(history, user_id):
    """Fake orchestrator exposing ONLY what the surface touches."""
    sent = []     # (ws, parsed-json) for every _safe_send
    renders = []  # (ws, components, target) for every send_ui_render

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((ws, list(components), target))

    orch = types.SimpleNamespace(
        workspace=WorkspaceManager(history),
        history=history,
        _ws_timeline_mode={},
        _ws_active_chat={},
        ui_clients=[],
        _get_user_id=lambda ws: user_id,
        _safe_send=_safe_send,
        send_ui_render=send_ui_render,
    )
    orch._sent = sent
    orch._renders = renders
    return orch


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("workspace_timeline") as runtime:
        yield runtime


@pytest.fixture
def env(plane_runtime):
    """Real HistoryManager + uuid-unique user/chat; chat deleted on teardown
    (FK CASCADE clears saved_components and workspace_snapshot rows)."""
    history = HistoryManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    user_id = f"pytest-wt-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    orch = _make_orch(history, user_id)
    yield orch, history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


@pytest.fixture
def audit_events(monkeypatch):
    """Capture audit.hooks.record_workspace_event kwargs — the surface imports
    it at call time, so patching the module attribute is enough (FR-033)."""
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_workspace_event", _record)
    return events


def _comp(body, *, agent="agent-wt", tool="show_table"):
    return {
        "type": "table",
        "title": f"Table {body}",
        "headers": ["v"],
        "rows": [[body]],
        "_source_agent": agent,
        "_source_tool": tool,
        "_source_params": {"q": body},
    }


async def _snap(orch, chat_id, user_id, cause="turn"):
    """Take a snapshot off the event loop (loop-guard clean test setup)."""
    await asyncio.sleep(0.002)  # distinct created_at ms (id DESC tiebreak also holds)
    snap_id = await asyncio.to_thread(orch.workspace.snapshot, chat_id, user_id, cause)
    assert snap_id is not None
    return snap_id


async def _upsert(orch, chat_id, user_id, comps):
    """Workspace upsert off the event loop (loop-guard clean test setup)."""
    return await asyncio.to_thread(orch.workspace.upsert, chat_id, user_id, comps)


def _msgs(orch, mtype):
    return [m for _, m in orch._sent if m.get("type") == mtype]


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------

def test_module_contract():
    assert wt.TITLE == "Workspace timeline"
    assert wt.ADMIN_ONLY is False
    assert set(wt.HANDLERS) == {
        "chrome_workspace_timeline_view",
        "chrome_workspace_timeline_live",
    }
    assert all(callable(fn) for fn in wt.HANDLERS.values())


# ---------------------------------------------------------------------------
# render() — snapshot list (FR-031 entry point)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_without_chat_prompts_to_open_one(env):
    orch, _history, user_id, _chat_id = env
    html = await wt.render(orch, user_id, ["user"], {})
    assert "Open a chat first" in html


@pytest.mark.asyncio
async def test_render_empty_history_message(env):
    orch, _history, user_id, chat_id = env
    html = await wt.render(orch, user_id, ["user"], {"chat_id": chat_id})
    assert "No workspace history yet" in html
    assert "chrome_workspace_timeline_view" not in html


@pytest.mark.asyncio
async def test_render_lists_snapshots_newest_first_with_cause_labels(env):
    orch, _history, user_id, chat_id = env
    await _upsert(orch, chat_id, user_id, [_comp("a")])
    id1 = await _snap(orch, chat_id, user_id, "turn")
    id2 = await _snap(orch, chat_id, user_id, "component_action")
    id3 = await _snap(orch, chat_id, user_id, "remove")

    html = await wt.render(orch, user_id, ["user"], {"chat_id": chat_id})

    # Every row is a view button carrying {chat_id, snapshot_id}.
    assert 'data-ui-action="chrome_workspace_timeline_view"' in html
    p1 = html.index(f"&quot;snapshot_id&quot;: {id1}")
    p2 = html.index(f"&quot;snapshot_id&quot;: {id2}")
    p3 = html.index(f"&quot;snapshot_id&quot;: {id3}")
    assert p3 < p2 < p1, "snapshots must list newest-first"

    # Turn numbering counts back from the total; causes get friendly labels.
    assert "<span>#3 · Component removed</span>" in html
    assert "<span>#2 · Component action</span>" in html
    assert "<span>#1 · Assistant turn</span>" in html

    # Read-only framing + the back-to-live affordance.
    assert "Viewing the past never changes the live workspace." in html
    assert 'data-ui-action="chrome_workspace_timeline_live"' in html
    assert f"&quot;chat_id&quot;: &quot;{chat_id}&quot;" in html

    # 3 snapshots fit one default page: no pager.
    assert ">Older</button>" not in html and ">Newer</button>" not in html


@pytest.mark.asyncio
async def test_render_paging_older_newer(env, monkeypatch):
    orch, _history, user_id, chat_id = env
    monkeypatch.setattr(wt, "_PAGE_SIZE", 2)
    await _upsert(orch, chat_id, user_id, [_comp("a")])
    id1 = await _snap(orch, chat_id, user_id)
    id2 = await _snap(orch, chat_id, user_id)
    id3 = await _snap(orch, chat_id, user_id)

    page0 = await wt.render(orch, user_id, ["user"], {"chat_id": chat_id})
    assert f"&quot;snapshot_id&quot;: {id3}" in page0
    assert f"&quot;snapshot_id&quot;: {id2}" in page0
    assert f"&quot;snapshot_id&quot;: {id1}" not in page0
    assert "<span>#3 · " in page0 and "<span>#2 · " in page0
    assert ">Older</button>" in page0 and ">Newer</button>" not in page0
    assert 'data-ui-action="chrome_open"' in page0
    assert "&quot;page&quot;: 1" in page0  # Older → page 1

    page1 = await wt.render(orch, user_id, ["user"], {"chat_id": chat_id, "page": 1})
    assert f"&quot;snapshot_id&quot;: {id1}" in page1
    assert f"&quot;snapshot_id&quot;: {id3}" not in page1
    assert "<span>#1 · " in page1
    assert ">Newer</button>" in page1 and ">Older</button>" not in page1
    assert "&quot;page&quot;: 0" in page1  # Newer → page 0


# ---------------------------------------------------------------------------
# _view — historical, read-only canvas (FR-031/FR-033)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_view_pushes_exact_historical_canvas_and_mode_flag(env, audit_events):
    orch, _history, user_id, chat_id = env
    ws = _FakeWS("viewer")
    await _upsert(orch, chat_id, user_id, [_comp("v1")])
    snap_id = await _snap(orch, chat_id, user_id, "turn")
    # Mutate the live workspace AFTER the snapshot so historical != live.
    await _upsert(orch, chat_id, user_id, [_comp("v2"), _comp("extra", tool="other_tool")])
    snap = await asyncio.to_thread(orch.workspace.get_snapshot, snap_id, user_id)
    live_now = await asyncio.to_thread(orch.workspace.live_components, chat_id, user_id)
    assert snap["components"] != live_now

    handler = wt.HANDLERS["chrome_workspace_timeline_view"]
    result = await handler(orch, ws, user_id, ["user"],
                           {"chat_id": chat_id, "snapshot_id": snap_id})
    assert result is None, "handled in place — no surface re-render tuple"

    # Socket enters timeline mode (implementation stores True, not the id).
    assert orch._ws_timeline_mode.get(id(ws)) is True

    # workspace_timeline_mode {active:true} announced over _safe_send.
    modes = _msgs(orch, "workspace_timeline_mode")
    assert modes == [{"type": "workspace_timeline_mode", "active": True,
                      "chat_id": chat_id, "snapshot_id": snap_id}]

    # One canvas render: read-only banner + the snapshot's components EXACTLY.
    assert len(orch._renders) == 1
    render_ws, comps, target = orch._renders[0]
    assert render_ws is ws and target == "canvas"
    assert comps[2:] == snap["components"], "historical canvas must equal the stored snapshot"
    assert comps[2]["rows"] == [["v1"]], "pre-mutation content, not the live v2"
    banner, back = comps[0], comps[1]
    assert banner["type"] == "alert" and banner["variant"] == "warning"
    assert "read-only" in banner["message"] and "Assistant turn" in banner["message"]
    assert back["type"] == "button"
    assert back["action"] == "chrome_workspace_timeline_live"
    assert back["payload"] == {"chat_id": chat_id}

    # Modal closed via an empty chrome_render so the canvas is visible.
    chrome = _msgs(orch, "chrome_render")
    assert chrome and chrome[-1]["region"] == "modal" and chrome[-1]["html"] == ""

    # FR-033: viewing history is audited as workspace.timeline_viewed.
    viewed = [e for e in audit_events if e.get("action") == "timeline_viewed"]
    assert len(viewed) == 1
    assert viewed[0]["user_id"] == user_id
    assert viewed[0]["chat_id"] == chat_id
    assert viewed[0]["detail"]["snapshot_id"] == snap_id
    assert viewed[0]["detail"]["cause"] == "turn"


@pytest.mark.asyncio
async def test_view_invalid_snapshot_id_returns_notice(env, audit_events):
    orch, _history, user_id, chat_id = env
    ws = _FakeWS()
    handler = wt.HANDLERS["chrome_workspace_timeline_view"]
    result = await handler(orch, ws, user_id, ["user"],
                           {"chat_id": chat_id, "snapshot_id": "not-an-int"})
    surface, params, notice = result
    assert surface == "workspace_timeline"
    assert params == {"chat_id": chat_id}
    assert "invalid" in notice
    # Nothing happened: no mode flag, no sends, no audit.
    assert orch._ws_timeline_mode == {}
    assert orch._sent == [] and orch._renders == []
    assert audit_events == []


@pytest.mark.asyncio
async def test_view_missing_or_mismatched_snapshot(env, audit_events):
    orch, history, user_id, chat_id = env
    ws = _FakeWS()
    await _upsert(orch, chat_id, user_id, [_comp("a")])
    snap_id = await _snap(orch, chat_id, user_id)
    handler = wt.HANDLERS["chrome_workspace_timeline_view"]

    # Nonexistent snapshot id (user-scoped lookup ⇒ guaranteed miss).
    surface, params, notice = await handler(
        orch, ws, user_id, ["user"],
        {"chat_id": chat_id, "snapshot_id": snap_id + 999_999})
    assert surface == "workspace_timeline"
    assert params == {"chat_id": chat_id}
    assert "no longer exists" in notice

    # Snapshot exists but belongs to a DIFFERENT chat than the payload claims.
    other_chat = await asyncio.to_thread(history.create_chat, user_id=user_id)
    try:
        surface, params, notice = await handler(
            orch, ws, user_id, ["user"],
            {"chat_id": other_chat, "snapshot_id": snap_id})
        assert surface == "workspace_timeline"
        assert params == {"chat_id": other_chat}
        assert "no longer exists" in notice
    finally:
        await asyncio.to_thread(history.delete_chat, other_chat, user_id=user_id)

    assert orch._ws_timeline_mode == {}
    assert orch._renders == []
    assert audit_events == []


# ---------------------------------------------------------------------------
# _live — back to the present, exactly as it now stands (FR-032)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_restores_exact_current_workspace(env, audit_events):
    orch, _history, user_id, chat_id = env
    ws = _FakeWS("traveler")
    await _upsert(orch, chat_id, user_id, [_comp("v1")])
    snap_id = await _snap(orch, chat_id, user_id)
    view = wt.HANDLERS["chrome_workspace_timeline_view"]
    await view(orch, ws, user_id, ["user"], {"chat_id": chat_id, "snapshot_id": snap_id})
    assert orch._ws_timeline_mode.get(id(ws)) is True
    audits_after_view = len(audit_events)
    orch._sent.clear()
    orch._renders.clear()

    # A component lands in the live workspace WHILE the past is open.
    await _upsert(orch, chat_id, user_id, [_comp("while-away", tool="new_tool")])

    live = wt.HANDLERS["chrome_workspace_timeline_live"]
    result = await live(orch, ws, user_id, ["user"], {"chat_id": chat_id})
    assert result is None

    # Timeline mode popped; deactivation announced.
    assert id(ws) not in orch._ws_timeline_mode
    modes = _msgs(orch, "workspace_timeline_mode")
    assert modes == [{"type": "workspace_timeline_mode", "active": False,
                      "chat_id": chat_id}]

    # FR-032: the re-render is the CURRENT workspace exactly — including the
    # component added while the historical view was open, and no banner.
    assert len(orch._renders) == 1
    render_ws, comps, target = orch._renders[0]
    assert render_ws is ws and target == "canvas"
    assert comps == await asyncio.to_thread(
        orch.workspace.live_components, chat_id, user_id)
    assert [c["rows"] for c in comps] == [[["v1"]], [["while-away"]]]
    assert all(c.get("type") != "alert" for c in comps)

    # Modal closed; returning to live adds no new timeline_viewed audit
    # (only the earlier _view recorded one).
    chrome = _msgs(orch, "chrome_render")
    assert chrome and chrome[-1]["region"] == "modal" and chrome[-1]["html"] == ""
    assert len(audit_events) == audits_after_view
    assert sum(1 for e in audit_events if e.get("action") == "timeline_viewed") == 1


@pytest.mark.asyncio
async def test_live_falls_back_to_active_chat(env):
    orch, _history, user_id, chat_id = env
    ws = _FakeWS()
    await _upsert(orch, chat_id, user_id, [_comp("v1")])
    orch._ws_active_chat[id(ws)] = chat_id
    orch._ws_timeline_mode[id(ws)] = True

    live = wt.HANDLERS["chrome_workspace_timeline_live"]
    await live(orch, ws, user_id, ["user"], {})  # no chat_id in payload

    assert id(ws) not in orch._ws_timeline_mode
    modes = _msgs(orch, "workspace_timeline_mode")
    assert modes == [{"type": "workspace_timeline_mode", "active": False,
                      "chat_id": chat_id}]
    assert len(orch._renders) == 1
    assert orch._renders[0][1] == await asyncio.to_thread(
        orch.workspace.live_components, chat_id, user_id)


@pytest.mark.asyncio
async def test_live_prefers_the_orchestrator_materializer(env):
    """052: when the orchestrator exposes _canvas_components, back-to-live
    renders the DESIGNED canvas from it (off the event loop) instead of the
    flat live_components fallback."""
    orch, _history, user_id, chat_id = env
    ws = _FakeWS()
    orch._ws_timeline_mode[id(ws)] = True
    designed = [{"type": "text", "content": "materialized canvas"}]
    calls = []

    def _canvas(c, u):
        calls.append((c, u))
        return list(designed)

    orch._canvas_components = _canvas

    live = wt.HANDLERS["chrome_workspace_timeline_live"]
    await live(orch, ws, user_id, ["user"], {"chat_id": chat_id})

    assert calls == [(chat_id, user_id)]
    assert len(orch._renders) == 1
    assert orch._renders[0][1] == designed


@pytest.mark.asyncio
async def test_live_without_any_chat_skips_canvas_render(env):
    orch, _history, user_id, _chat_id = env
    ws = _FakeWS()
    orch._ws_timeline_mode[id(ws)] = True

    live = wt.HANDLERS["chrome_workspace_timeline_live"]
    await live(orch, ws, user_id, ["user"], {})

    assert id(ws) not in orch._ws_timeline_mode
    modes = _msgs(orch, "workspace_timeline_mode")
    assert modes == [{"type": "workspace_timeline_mode", "active": False, "chat_id": ""}]
    assert orch._renders == [], "no chat ⇒ nothing to re-render"
    chrome = _msgs(orch, "chrome_render")
    assert chrome and chrome[-1]["html"] == ""  # modal still closed


# ---------------------------------------------------------------------------
# EC-5 — live updates keep flowing while a socket views the past
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ec5_live_upsert_during_timeline_mode(env, audit_events):
    """EC-5: while a socket is in timeline mode, a live workspace change still
    persists via Workspace.upsert, still snapshots, and still fans the
    ui_upsert out to that socket (the CLIENT defers rendering) — without
    altering the server-side timeline state or the stored snapshot."""
    from rote.rote import ROTE
    from orchestrator.orchestrator import Orchestrator

    orch, _history, user_id, chat_id = env
    orch.rote = ROTE()
    orch.send_ui_upsert = types.MethodType(Orchestrator.send_ui_upsert, orch)
    ws = _FakeWS("in-the-past")
    orch.ui_clients = [ws]
    orch._ws_active_chat[id(ws)] = chat_id

    await _upsert(orch, chat_id, user_id, [_comp("v1")])
    snap_id = await _snap(orch, chat_id, user_id)
    view = wt.HANDLERS["chrome_workspace_timeline_view"]
    await view(orch, ws, user_id, ["user"], {"chat_id": chat_id, "snapshot_id": snap_id})
    assert orch._ws_timeline_mode.get(id(ws)) is True
    frozen = (await asyncio.to_thread(
        orch.workspace.get_snapshot, snap_id, user_id))["components"]
    orch._sent.clear()
    orch._renders.clear()
    count_before = await asyncio.to_thread(
        orch.workspace.count_snapshots, chat_id, user_id)

    # Live mutation while the historical view is open.
    ops = await _upsert(orch, chat_id, user_id, [_comp("live-update", tool="other_tool")])
    assert len(ops) == 1 and ops[0]["created"] is True
    cid = ops[0]["component_id"]
    new_snap = await asyncio.to_thread(orch.workspace.snapshot, chat_id, user_id, "turn")
    assert new_snap is not None and new_snap != snap_id

    # The snapshot list grew — the turn was recorded normally.
    assert (await asyncio.to_thread(
        orch.workspace.count_snapshots, chat_id, user_id)) == count_before + 1

    # Drive the real fan-out: the timeline-mode socket still RECEIVES it.
    await orch.send_ui_upsert(None, chat_id, user_id, ops)
    upserts = [(w, m) for w, m in orch._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 1 and upserts[0][0] is ws
    msg = upserts[0][1]
    assert msg["chat_id"] == chat_id
    op = msg["ops"][0]
    assert op["op"] == "upsert" and op["component_id"] == cid
    assert op["component"]["rows"] == [["live-update"]]
    assert op["html"] and f'data-component-id="{cid}"' in op["html"]

    # Server-side historical state is untouched by the live update.
    assert orch._ws_timeline_mode.get(id(ws)) is True
    assert (await asyncio.to_thread(
        orch.workspace.get_snapshot, snap_id, user_id))["components"] == frozen
    # … and the live workspace really does carry both components now.
    live_now = await asyncio.to_thread(orch.workspace.live_components, chat_id, user_id)
    assert [c["rows"] for c in live_now] == [[["v1"]], [["live-update"]]]
