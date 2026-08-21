"""Feature 044 (T034/T035/T036, US3) — native SDUI ``components()`` for the
three surfaces that were web-HTML-only (workspace_timeline, pulse, attachments)
plus the device-aware ``workspace_timeline`` ``_view``/``_live`` handlers.

DB-free: fake orchestrators (with a ROTE device profile) + monkeypatched data
sources, mirroring ``backend/tests/chrome/test_chrome_surface.py``. Asserts each
``components()`` returns a non-empty component list with the expected
buttons/actions, and that ``_view``/``_live`` are device-aware (native → an
empty-components ``chrome_surface`` close; web → an empty ``chrome_render``) and
return a re-render tuple on their error paths (contract §2/§3.1).
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from rote.capabilities import DeviceProfile
from orchestrator.projection_surfaces import attachments as att_surface
from orchestrator.projection_surfaces import pulse as pulse_surface
from orchestrator.projection_surfaces import workspace_timeline as wt


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeWS:
    def __init__(self, label=""):
        self.label = label


class FakeRote:
    def __init__(self, device):
        self._p = (DeviceProfile.default() if device == "browser"
                   else DeviceProfile.from_dict({"device_type": device}))

    def get_profile(self, ws):
        return self._p


def _buttons(components):
    """Every button dict anywhere in a component list (top-level + nested)."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "button":
            found.append(node)
        for key in ("children", "content"):
            walk(node.get(key) or [])
        for tab in (node.get("tabs") or []):
            if isinstance(tab, dict):
                walk(tab.get("content") or [])

    walk(components)
    return found


def _action(components, action):
    return [b for b in _buttons(components) if b.get("action") == action]


def _types(components):
    return [c.get("type") for c in components]


# ===========================================================================
# T034 — workspace_timeline.components() + device-aware _view/_live
# ===========================================================================

class FakeWorkspace:
    def __init__(self, snaps=None, snapshot=None, live=None):
        self._snaps = list(snaps or [])
        self._snapshot = snapshot
        self._live = list(live or [])

    def list_snapshots(self, chat_id, user_id, limit, offset):
        return self._snaps[offset:offset + limit]

    def count_snapshots(self, chat_id, user_id):
        return len(self._snaps)

    def get_snapshot(self, snapshot_id, user_id):
        return self._snapshot

    def live_components(self, chat_id, user_id):
        return list(self._live)


class TimelineOrch:
    def __init__(self, device="windows", workspace=None):
        self.rote = FakeRote(device)
        self.workspace = workspace or FakeWorkspace()
        self.sent = []
        self.renders = []
        self._ws_timeline_mode = {}
        self._ws_active_chat = {}

    async def _safe_send(self, ws, payload):
        self.sent.append(json.loads(payload))

    async def send_ui_render(self, ws, components, target="canvas"):
        self.renders.append((ws, list(components), target))


@pytest.fixture
def audit_capture(monkeypatch):
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_workspace_event", _record)
    return events


def test_timeline_components_no_chat_is_a_notice():
    orch = TimelineOrch()
    comps = run(wt.components(orch, "u1", ["user"], {}))
    assert _types(comps) == ["alert"]
    assert "Open a chat first" in comps[0]["message"]


def test_timeline_components_empty_history_is_a_notice():
    orch = TimelineOrch(workspace=FakeWorkspace(snaps=[]))
    comps = run(wt.components(orch, "u1", ["user"], {"chat_id": "c1"}))
    assert _types(comps) == ["alert"]
    assert "No workspace history yet" in comps[0]["message"]


def test_timeline_components_lists_snapshots_with_expected_actions():
    snaps = [{"id": 9, "cause": "remove", "created_at": 1_700_000_002_000},
             {"id": 8, "cause": "turn", "created_at": 1_700_000_001_000}]
    orch = TimelineOrch(workspace=FakeWorkspace(snaps=snaps))
    comps = run(wt.components(orch, "u1", ["user"], {"chat_id": "c1"}))
    assert comps, "non-empty component list"

    # Back to live button.
    live_btns = _action(comps, "chrome_workspace_timeline_live")
    assert live_btns and live_btns[0]["payload"] == {"chat_id": "c1"}

    # One view button per snapshot, carrying {chat_id, snapshot_id}.
    view_btns = _action(comps, "chrome_workspace_timeline_view")
    ids = {b["payload"]["snapshot_id"] for b in view_btns}
    assert ids == {8, 9}
    assert all(b["payload"]["chat_id"] == "c1" for b in view_btns)
    # Human cause label shows in the row label ("Component removed" / "Assistant turn").
    labels = " ".join(b.get("label", "") for b in view_btns)
    assert "Component removed" in labels and "Assistant turn" in labels


def test_timeline_components_paging_offers_older(monkeypatch):
    monkeypatch.setattr(wt, "_PAGE_SIZE", 1)
    snaps = [{"id": 2, "cause": "turn", "created_at": 2}, {"id": 1, "cause": "turn", "created_at": 1}]
    orch = TimelineOrch(workspace=FakeWorkspace(snaps=snaps))
    comps = run(wt.components(orch, "u1", ["user"], {"chat_id": "c1"}))
    older = _action(comps, "chrome_open")
    assert older, "an Older/Newer chrome_open button is present"
    assert any(b["payload"]["params"] == {"chat_id": "c1", "page": 1} for b in older)


def _snapshot(chat_id="c1"):
    return {"id": 7, "chat_id": chat_id, "cause": "turn", "created_at": 1_700_000_000_000,
            "components": [{"type": "text", "content": "hist", "component_id": "wc1"}],
            "layouts": []}


@pytest.mark.parametrize("device", ["windows", "android"])
def test_view_success_pushes_canvas_and_device_aware_close(device, audit_capture):
    orch = TimelineOrch(device=device, workspace=FakeWorkspace(snapshot=_snapshot()))
    ws = FakeWS("viewer")
    result = run(wt.HANDLERS["chrome_workspace_timeline_view"](
        orch, ws, "u1", ["user"], {"chat_id": "c1", "snapshot_id": 7}))
    assert result is None, "success handled in place (device-aware close, not a re-render)"

    # Timeline mode announced + entered.
    assert orch._ws_timeline_mode.get(id(ws)) is True
    modes = [f for f in orch.sent if f.get("type") == "workspace_timeline_mode"]
    assert modes and modes[-1]["active"] is True

    # The historical canvas was pushed (banner + snapshot components).
    assert len(orch.renders) == 1
    _rws, comps, target = orch.renders[0]
    assert target == "canvas" and comps[0]["type"] == "alert"  # read-only banner

    # Device-aware CLOSE: native gets an empty-components chrome_surface, never
    # the web-only chrome_render (contract §2/§3.1).
    surfaces = [f for f in orch.sent if f.get("type") == "chrome_surface"]
    assert surfaces and surfaces[-1]["components"] == []
    assert not any(f.get("type") == "chrome_render" for f in orch.sent)

    # History view is audited.
    assert any(e.get("action") == "timeline_viewed" for e in audit_capture)


def test_view_success_web_still_closes_with_empty_chrome_render(audit_capture):
    orch = TimelineOrch(device="browser", workspace=FakeWorkspace(snapshot=_snapshot()))
    ws = FakeWS("viewer")
    result = run(wt.HANDLERS["chrome_workspace_timeline_view"](
        orch, ws, "u1", ["user"], {"chat_id": "c1", "snapshot_id": 7}))
    assert result is None
    renders = [f for f in orch.sent if f.get("type") == "chrome_render"]
    assert renders and renders[-1]["region"] == "modal" and renders[-1]["html"] == ""
    assert not any(f.get("type") == "chrome_surface" for f in orch.sent)


def test_view_invalid_snapshot_returns_rerender_tuple(audit_capture):
    orch = TimelineOrch(device="windows", workspace=FakeWorkspace(snapshot=None))
    ws = FakeWS()
    result = run(wt.HANDLERS["chrome_workspace_timeline_view"](
        orch, ws, "u1", ["user"], {"chat_id": "c1", "snapshot_id": "not-an-int"}))
    assert isinstance(result, tuple) and len(result) == 3
    surface, params, notice = result
    assert surface == "workspace_timeline"
    assert params == {"chat_id": "c1"}
    assert "invalid" in notice.lower()
    # Nothing pushed, no audit, no mode flip.
    assert orch.sent == [] and orch.renders == []
    assert orch._ws_timeline_mode == {}


@pytest.mark.parametrize("device", ["windows", "android"])
def test_live_success_restores_canvas_and_device_aware_close(device):
    live = [{"type": "text", "content": "now", "component_id": "wc9"}]
    orch = TimelineOrch(device=device, workspace=FakeWorkspace(live=live))
    ws = FakeWS("traveler")
    orch._ws_timeline_mode[id(ws)] = True
    result = run(wt.HANDLERS["chrome_workspace_timeline_live"](
        orch, ws, "u1", ["user"], {"chat_id": "c1"}))
    assert result is None
    assert id(ws) not in orch._ws_timeline_mode
    modes = [f for f in orch.sent if f.get("type") == "workspace_timeline_mode"]
    assert modes and modes[-1]["active"] is False
    # Live canvas restored.
    assert orch.renders and orch.renders[-1][1] == live
    # Device-aware close.
    surfaces = [f for f in orch.sent if f.get("type") == "chrome_surface"]
    assert surfaces and surfaces[-1]["components"] == []
    assert not any(f.get("type") == "chrome_render" for f in orch.sent)


def test_chrome_open_without_chat_id_defaults_to_active_chat():
    """Feature 044 — chrome_open with no chat_id param falls back to the
    socket's active chat server-side (native clients don't inject chat_id the
    way web's client.js does), so the timeline lists snapshots instead of the
    'Open a chat first' notice."""
    from orchestrator import chrome_events

    snaps = [{"id": 3, "cause": "turn", "created_at": 1_700_000_000_000}]
    orch = TimelineOrch(workspace=FakeWorkspace(snaps=snaps))
    ws = FakeWS("native")
    orch.ui_sessions = {ws: {"realm_access": {"roles": ["user"]}}}
    orch._ws_active_chat[id(ws)] = "c-active"
    handled = run(chrome_events.handle_chrome_event(
        orch, ws, "chrome_open", {"surface": "workspace_timeline"}, "u1"))
    assert handled is True
    surfaces = [f for f in orch.sent if f.get("type") == "chrome_surface"]
    assert surfaces, "a chrome_surface frame was pushed"
    comps = surfaces[-1]["components"]
    assert not any(c.get("type") == "alert" and "Open a chat first" in c.get("message", "")
                   for c in comps), "timeline fell back to the no-chat notice"
    view_btns = _action(comps, "chrome_workspace_timeline_view")
    assert view_btns and all(b["payload"]["chat_id"] == "c-active" for b in view_btns)


def test_chrome_open_without_chat_id_or_active_chat_still_notices():
    from orchestrator import chrome_events

    orch = TimelineOrch(workspace=FakeWorkspace(snaps=[]))
    ws = FakeWS("native")
    orch.ui_sessions = {ws: {"realm_access": {"roles": ["user"]}}}
    run(chrome_events.handle_chrome_event(
        orch, ws, "chrome_open", {"surface": "workspace_timeline"}, "u1"))
    surfaces = [f for f in orch.sent if f.get("type") == "chrome_surface"]
    assert surfaces
    comps = surfaces[-1]["components"]
    assert any(c.get("type") == "alert" and "Open a chat first" in c.get("message", "")
               for c in comps)


def test_live_success_web_closes_with_empty_chrome_render():
    orch = TimelineOrch(device="browser", workspace=FakeWorkspace(live=[]))
    ws = FakeWS()
    orch._ws_timeline_mode[id(ws)] = True
    result = run(wt.HANDLERS["chrome_workspace_timeline_live"](
        orch, ws, "u1", ["user"], {"chat_id": "c1"}))
    assert result is None
    renders = [f for f in orch.sent if f.get("type") == "chrome_render"]
    assert renders and renders[-1]["html"] == ""
    assert not any(f.get("type") == "chrome_surface" for f in orch.sent)


# ===========================================================================
# T035 — pulse.components()
# ===========================================================================

class FakePersonalizationRepo:
    def list_memory(self, user_id):
        return []

    def list_signals(self, user_id):
        return []


def _pulse_orch():
    return types.SimpleNamespace(
        personalization_service=types.SimpleNamespace(repo=FakePersonalizationRepo()))


def test_pulse_components_enabled_returns_intro_and_cards(monkeypatch):
    monkeypatch.setattr(pulse_surface, "pulse_enabled", lambda: True)
    monkeypatch.setattr(pulse_surface, "build_digest",
                        lambda items, **kw: [{"type": "card", "title": "Goals",
                                              "content": [{"type": "text", "content": "• ship 044"}]}])
    comps = run(pulse_surface.components(_pulse_orch(), "u1", ["user"], {}))
    assert comps, "non-empty when enabled"
    kinds = _types(comps)
    assert "text" in kinds  # intro caption
    assert "card" in kinds  # the digest card


def test_pulse_components_disabled_returns_off_notice(monkeypatch):
    monkeypatch.setattr(pulse_surface, "pulse_enabled", lambda: False)
    comps = run(pulse_surface.components(_pulse_orch(), "u1", ["user"], {}))
    assert len(comps) == 1 and comps[0]["type"] == "alert"
    assert "turned off" in comps[0]["message"]


def test_pulse_components_enabled_no_digest_shows_empty_notice(monkeypatch):
    monkeypatch.setattr(pulse_surface, "pulse_enabled", lambda: True)
    monkeypatch.setattr(pulse_surface, "build_digest", lambda items, **kw: [])
    comps = run(pulse_surface.components(_pulse_orch(), "u1", ["user"], {}))
    assert any(c["type"] == "alert" and "Nothing to show yet" in c["message"] for c in comps)


# ===========================================================================
# T036 — attachments.components()
# ===========================================================================

def _att(attachment_id, filename, category, size_bytes=1024):
    return types.SimpleNamespace(attachment_id=attachment_id, filename=filename,
                                 category=category, size_bytes=size_bytes)


class _AttRepo:
    def __init__(self, items):
        self._items = items

    def list_for_user(self, user_id, limit=100):
        return list(self._items), None


def test_attachments_components_lists_rows_with_attach_and_delete(monkeypatch):
    items = [_att("a1", "report.csv", "spreadsheet", 2048),
             _att("a2", "notes.txt", "text", 100)]
    monkeypatch.setattr(att_surface, "_repo", lambda orch: _AttRepo(items))
    comps = run(att_surface.components(object(), "u1", ["user"], {}))
    assert comps, "non-empty when the user has uploads"

    # Per-row Attach button uses the client-local attach_existing action with
    # the full chip payload (never dispatched server-side).
    attach = _action(comps, "attach_existing")
    assert {b["payload"]["attachment_id"] for b in attach} == {"a1", "a2"}
    a1 = next(b for b in attach if b["payload"]["attachment_id"] == "a1")
    assert a1["payload"]["filename"] == "report.csv"
    assert a1["payload"]["category"] == "spreadsheet"

    # Per-row Delete button routes through the existing chrome_attachment_delete.
    delete = _action(comps, "chrome_attachment_delete")
    assert {b["payload"]["attachment_id"] for b in delete} == {"a1", "a2"}


def test_attachments_components_empty_state(monkeypatch):
    monkeypatch.setattr(att_surface, "_repo", lambda orch: _AttRepo([]))
    comps = run(att_surface.components(object(), "u1", ["user"], {}))
    assert len(comps) == 1 and comps[0]["type"] == "alert"
    assert "No uploads yet" in comps[0]["message"]


def test_attachments_components_list_failure_is_a_notice(monkeypatch):
    class _Boom:
        def list_for_user(self, user_id, limit=100):
            raise RuntimeError("db down")

    monkeypatch.setattr(att_surface, "_repo", lambda orch: _Boom())
    comps = run(att_surface.components(object(), "u1", ["user"], {}))
    assert len(comps) == 1 and comps[0]["type"] == "alert"
    assert comps[0]["variant"] == "error"
