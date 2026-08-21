"""Feature 029 — product-level workspace layout behavior (T012).

Real-Postgres coverage of the WorkspaceManager layout API
(upsert/claim-stealing/live ordering/remove pruning/shared position space),
snapshot round-trips including legacy no-layout rows, and chat-deletion
cascades through typed AstralPlane repositories. Schema and migration
qualification lives with the owning AstralPlane component.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.workspace import (  # noqa: E402
    WorkspaceManager,
    iter_layout_refs,
    layout_key_for,
    prune_layout_refs,
)
from tests.helpers.voice_plane_runtime import (  # noqa: E402
    history_manager,
    isolated_voice_plane_runtime,
)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("workspace_layout") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def history(plane_runtime):
    return history_manager(plane_runtime)


@pytest.fixture(scope="module")
def ws(history, plane_runtime):
    return WorkspaceManager(
        history,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture
def chat(history):
    user_id = f"pytest-layout-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    yield chat_id, user_id
    history.delete_chat(chat_id, user_id)


def _comp(agent, tool, params, **extra):
    c = {"type": "table", "headers": ["A"], "rows": [["1"]],
         "_source_agent": agent, "_source_tool": tool, "_source_params": params}
    c.update(extra)
    return c


def _ref(cid):
    return {"type": "ref", "component_id": cid}


# ---------------------------------------------------------------------------
# Plane composition boundary
# ---------------------------------------------------------------------------


def test_plane_catalog_exposes_workspace_contracts(plane_runtime):
    workspaces = plane_runtime.repositories.workspaces
    assert workspaces.canvas is not None
    assert workspaces.layouts is not None
    assert workspaces.snapshots is not None


def test_chat_delete_cascades_layouts(history, ws):
    user_id = f"pytest-cascade-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    ops = ws.upsert(chat_id, user_id, [_comp("a", "t", {"p": 1})])
    ws.upsert_layout(chat_id, user_id, layout_key_for(chat_id, "m1"),
                     [_ref(ops[0]["component_id"])])
    assert ws.live_layouts(chat_id, user_id)
    history.delete_chat(chat_id, user_id)
    assert ws.live_layouts(chat_id, user_id) == []


# ---------------------------------------------------------------------------
# Layout API (T012)
# ---------------------------------------------------------------------------


def test_layout_key_deterministic():
    assert layout_key_for("c1", "42") == layout_key_for("c1", "42")
    assert layout_key_for("c1", "42") != layout_key_for("c1", "43")
    assert layout_key_for("c1", "42").startswith("ly_")


def test_upsert_layout_roundtrip_and_update_in_place(ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [_comp("a", "t1", {}), _comp("a", "t2", {})])
    ids = [op["component_id"] for op in ops]
    key = layout_key_for(chat_id, "m1")
    layout_v1 = [{"type": "grid", "columns": 2, "children": [_ref(ids[0]), _ref(ids[1])]}]
    assert ws.upsert_layout(chat_id, user_id, key, layout_v1)
    live = ws.live_layouts(chat_id, user_id)
    assert len(live) == 1 and live[0]["layout_key"] == key
    assert set(iter_layout_refs(live[0]["layout"])) == set(ids)
    # Re-design the same round: same key updates in place, position kept.
    pos_before = live[0]["position"]
    layout_v2 = [_ref(ids[0]), _ref(ids[1]), {"type": "divider"}]
    ws.upsert_layout(chat_id, user_id, key, layout_v2)
    live2 = ws.live_layouts(chat_id, user_id)
    assert len(live2) == 1 and live2[0]["position"] == pos_before
    assert live2[0]["layout"][-1]["type"] == "divider"


def test_later_layout_steals_claimed_refs(ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [_comp("a", "t1", {}), _comp("a", "t2", {})])
    ids = [op["component_id"] for op in ops]
    k1 = layout_key_for(chat_id, "m1")
    k2 = layout_key_for(chat_id, "m2")
    ws.upsert_layout(chat_id, user_id, k1, [_ref(ids[0]), _ref(ids[1])])
    ws.upsert_layout(chat_id, user_id, k2, [_ref(ids[1])])
    by_key = {item["layout_key"]: item for item in ws.live_layouts(chat_id, user_id)}
    assert set(iter_layout_refs(by_key[k1]["layout"])) == {ids[0]}, "k1 lost the stolen ref"
    assert set(iter_layout_refs(by_key[k2]["layout"])) == {ids[1]}


def test_remove_component_prunes_layout_refs(ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [_comp("a", "t1", {}), _comp("a", "t2", {})])
    ids = [op["component_id"] for op in ops]
    key = layout_key_for(chat_id, "m1")
    ws.upsert_layout(chat_id, user_id, key,
                     [{"type": "card", "title": "g", "content": [_ref(ids[0]), _ref(ids[1])]}])
    assert ws.remove(chat_id, user_id, ids[0])
    live = ws.live_layouts(chat_id, user_id)
    assert set(iter_layout_refs(live[0]["layout"])) == {ids[1]}


def test_positions_share_one_ordering_space(ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [_comp("a", "t1", {})])
    before = ws.next_canvas_position(chat_id, user_id)
    ws.upsert_layout(chat_id, user_id, layout_key_for(chat_id, "m1"),
                     [_ref(ops[0]["component_id"])])
    after = ws.next_canvas_position(chat_id, user_id)
    assert after == before + 1, "layout rows consume the shared position counter"


def test_prune_layout_refs_keeps_empty_containers():
    tree = [{"type": "card", "title": "g", "content": [_ref("x")]}]
    pruned = prune_layout_refs(tree, {"x"})
    assert pruned[0]["type"] == "card" and pruned[0]["content"] == []


# ---------------------------------------------------------------------------
# Snapshots carry layouts (T012 / FR-025)
# ---------------------------------------------------------------------------


def test_snapshot_roundtrips_layouts(ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [_comp("a", "t1", {}), _comp("a", "t2", {})])
    ids = [op["component_id"] for op in ops]
    ws.upsert_layout(chat_id, user_id, layout_key_for(chat_id, "m1"),
                     [_ref(ids[0]), _ref(ids[1])])
    sid = ws.snapshot(chat_id, user_id, cause="turn")
    snap = ws.get_snapshot(sid, user_id)
    assert snap["layouts"], "designed state captured"
    assert set(iter_layout_refs(snap["layouts"][0]["layout"])) == set(ids)


def test_pre_029_snapshot_reads_as_no_layouts(ws, chat, plane_runtime):
    chat_id, user_id = chat
    plane_runtime.execute(
        "INSERT INTO workspace_snapshot (chat_id, user_id, cause, components, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, user_id, "turn", json.dumps([]), 1),
    )
    row = plane_runtime.fetch_one(
        "SELECT id FROM workspace_snapshot WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,))
    snap = ws.get_snapshot(row["id"], user_id)
    assert snap["layouts"] == [], "NULL layouts column degrades to flat render"


def test_snapshot_without_layouts_reads_as_empty(ws, chat):
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("a", "t1", {})])
    snapshot_id = ws.snapshot(chat_id, user_id, cause="turn")
    assert ws.get_snapshot(snapshot_id, user_id)["layouts"] == []
