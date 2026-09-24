"""Tests for Orchestrator._canvas_components and _push_canvas
(backend/orchestrator/orchestrator.py): every live keyed component is materialized or
appended flat, so a render never silently drops one.
"""

from __future__ import annotations

import types
from typing import Any, Dict, List

import pytest

from orchestrator.orchestrator import Orchestrator


def _comp(cid: str, pos: int) -> Dict[str, Any]:
    return {"component_id": cid, "position": pos,
            "component_data": {"type": "metric", "title": cid, "value": pos,
                               "component_id": cid}}


class FakeWorkspace:
    def __init__(self, rows: List[Dict[str, Any]], layouts: List[Dict[str, Any]]):
        self._rows = rows
        self._layouts = layouts

    def live_layouts(self, chat_id, user_id):
        return [dict(x) for x in self._layouts]

    def live_rows(self, chat_id, user_id):
        return [{"component_id": r["component_id"], "position": r["position"],
                 "component_data": dict(r["component_data"])} for r in self._rows]

    def live_components(self, chat_id, user_id):
        return [dict(r["component_data"]) for r in
                sorted(self._rows, key=lambda r: r["position"])]


def _orch(workspace: FakeWorkspace) -> Any:
    orch = types.SimpleNamespace(workspace=workspace)
    orch._canvas_components = types.MethodType(Orchestrator._canvas_components, orch)
    orch._push_canvas = types.MethodType(Orchestrator._push_canvas, orch)
    return orch


def _collect_ids(nodes: List[Dict[str, Any]]) -> set:
    found = set()

    def walk(node: Any):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        cid = node.get("component_id")
        if cid:
            found.add(cid)
        for key in ("children", "content"):
            walk(node.get(key) or [])
        for tab in (node.get("tabs") or []):
            if isinstance(tab, dict):
                walk(tab.get("content") or [])

    walk(nodes)
    return found


def _grid_layout(*cids: str) -> List[Dict[str, Any]]:
    return [{"type": "grid", "columns": 2,
             "children": [{"type": "ref", "component_id": c} for c in cids]}]


def test_flat_canvas_returns_all_components_when_no_layouts():
    ws = FakeWorkspace(rows=[_comp("c1", 0), _comp("c2", 1), _comp("c3", 2)], layouts=[])
    out = _orch(ws)._canvas_components("chat", "user")
    stripped = [{k: v for k, v in c.items() if k != "provenance"} for c in out]
    assert stripped == ws.live_components("chat", "user")
    assert _collect_ids(out) == {"c1", "c2", "c3"}


def test_designed_canvas_keeps_every_keyed_component():
    ws = FakeWorkspace(
        rows=[_comp("c1", 0), _comp("c2", 1), _comp("c3", 2)],
        layouts=[{"layout": _grid_layout("c1", "c2"), "position": 0,
                  "layout_key": "chat|turn1"}],
    )
    out = _orch(ws)._canvas_components("chat", "user")
    assert _collect_ids(out) == {"c1", "c2", "c3"}
    assert len(out) == 2
    grid = next(n for n in out if n.get("type") == "grid")
    assert _collect_ids([grid]) == {"c1", "c2"}, "claimed refs materialized in place"
    flat_ids = [n.get("component_id") for n in out if n.get("type") != "grid"]
    assert flat_ids == ["c3"]


def test_stale_layout_ref_never_drops_a_live_component():
    ws = FakeWorkspace(
        rows=[_comp("c1", 0), _comp("c2", 1), _comp("c3", 2)],
        layouts=[{"layout": _grid_layout("c1", "GONE"), "position": 0,
                  "layout_key": "chat|turn1"}],
    )
    out = _orch(ws)._canvas_components("chat", "user")
    assert _collect_ids(out) == {"c1", "c2", "c3"}, "no live component lost to a stale ref"
    assert "GONE" not in _collect_ids(out)


@pytest.mark.asyncio
async def test_push_canvas_sends_the_full_canvas_to_matching_sockets():
    ws_state = FakeWorkspace(
        rows=[_comp("c1", 0), _comp("c2", 1), _comp("c3", 2)],
        layouts=[{"layout": _grid_layout("c1", "c2"), "position": 0,
                  "layout_key": "chat|turn1"}],
    )
    orch = _orch(ws_state)
    sends: List[Any] = []

    async def _capture_send(sock, components, target="canvas", speak=True):
        sends.append((sock, list(components), target))

    orch.send_ui_render = _capture_send
    sock_match = object()
    sock_other_chat = object()
    sock_other_user = object()
    orch.ui_clients = [sock_match, sock_other_chat, sock_other_user]
    orch._get_user_id = lambda s: "user" if s is not sock_other_user else "other"
    orch._ws_active_chat = {id(sock_match): "chat", id(sock_other_chat): "elsewhere"}

    await orch._push_canvas("chat", "user")

    assert len(sends) == 1, "only the matching socket receives the canvas"
    sock, comps, target = sends[0]
    assert sock is sock_match and target == "canvas"
    assert _collect_ids(comps) == {"c1", "c2", "c3"}
    assert comps == orch._canvas_components("chat", "user")
