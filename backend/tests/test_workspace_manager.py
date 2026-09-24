"""Tests for WorkspaceManager identity and ordering (backend/orchestrator/workspace.py):
fingerprint stability, author-id/fingerprint resolution, in-place update vs. append,
and legacy NULL-position ordering.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.workspace import WorkspaceManager, canonical_params, fingerprint  # noqa: E402
from tests.helpers.voice_plane_runtime import (  # noqa: E402
    history_manager,
    isolated_voice_plane_runtime,
)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("workspace_manager") as runtime:
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
    user_id = f"pytest-ws-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    yield chat_id, user_id
    history.delete_chat(chat_id, user_id)


def _comp(agent, tool, params, **extra):
    c = {
        "type": "card",
        "_source_agent": agent,
        "_source_tool": tool,
        "_source_params": params,
    }
    c.update(extra)
    return c


def test_fingerprint_stable_across_calls():
    a = fingerprint("agentX", "toolY", {"region": "north", "year": 2026})
    b = fingerprint("agentX", "toolY", {"region": "north", "year": 2026})
    assert a == b
    assert a.startswith("wc_") and len(a) == len("wc_") + 16
    c = fingerprint("agentX", "toolY", {"year": 2026, "region": "north"})
    assert a == c


def test_fingerprint_differs_for_different_params():
    a = fingerprint("agentX", "toolY", {"region": "north"})
    b = fingerprint("agentX", "toolY", {"region": "south"})
    assert a != b
    assert fingerprint("agentZ", "toolY", {"region": "north"}) != a
    assert fingerprint("agentX", "toolQ", {"region": "north"}) != a


def test_fingerprint_excludes_private_underscore_params():
    base = fingerprint("agentX", "toolY", {"region": "north"})
    with_private = fingerprint(
        "agentX", "toolY", {"region": "north", "_credentials": "s3cret", "_trace_id": "t1"}
    )
    assert base == with_private
    assert canonical_params({"region": "north", "_credentials": "x"}) == canonical_params(
        {"region": "north"}
    )


def test_resolve_identity_author_id_namespaced(ws):
    comp = {"type": "card", "id": "abc"}
    assert ws.resolve_identity(comp) == "au_abc"
    assert comp["component_id"] == "au_abc"


def test_resolve_identity_wc_echo_honored_verbatim(ws):
    comp = {"type": "card", "id": "wc_deadbeef"}
    assert ws.resolve_identity(comp) == "wc_deadbeef"


def test_resolve_identity_falls_back_to_fingerprint(ws):
    comp = _comp("agentX", "toolY", {"q": 1})
    cid = ws.resolve_identity(comp)
    assert cid == fingerprint("agentX", "toolY", {"q": 1})
    assert comp["component_id"] == cid


def test_resolve_identity_existing_component_id_short_circuits(ws):
    comp = {"type": "card", "id": "abc", "component_id": "wc_1234567890abcdef"}
    assert ws.resolve_identity(comp) == "wc_1234567890abcdef"


def test_upsert_create_then_update_in_place(ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1}, title="V1", body="one")])
    assert len(ops) == 1
    assert ops[0]["op"] == "upsert" and ops[0]["created"] is True
    cid = ops[0]["component_id"]

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 1
    row_id = rows[0]["id"]
    first_updated_at = rows[0]["updated_at"]
    assert rows[0]["position"] == 1
    assert rows[0]["component_id"] == cid

    time.sleep(0.05)
    ops2 = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1}, title="V2", body="two")])
    assert len(ops2) == 1
    assert ops2[0]["created"] is False
    assert ops2[0]["component_id"] == cid

    rows2 = ws.live_rows(chat_id, user_id)
    assert len(rows2) == 1, "same identity must not create a second row"
    assert rows2[0]["id"] == row_id, "DB row id is stable across in-place updates"
    assert rows2[0]["component_data"]["body"] == "two"
    assert rows2[0]["title"] == "V2"
    assert rows2[0]["updated_at"] > first_updated_at
    assert rows2[0]["position"] == 1, "position must not change on update"


def test_upsert_distinct_component_appends_position_2(ws, chat):
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1})])
    ops = ws.upsert(chat_id, user_id, [_comp("agentB", "toolB", {"k": "v"})])
    assert ops[0]["created"] is True
    rows = ws.live_rows(chat_id, user_id)
    assert [r["position"] for r in rows] == [1, 2]
    assert rows[1]["component_id"] == fingerprint("agentB", "toolB", {"k": "v"})


def test_same_tool_different_params_coexist_in_one_batch(ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(
        chat_id,
        user_id,
        [
            _comp("agentX", "toolY", {"region": "north"}, body="north"),
            _comp("agentX", "toolY", {"region": "south"}, body="south"),
        ],
    )
    assert len(ops) == 2
    assert all(op["created"] for op in ops)
    assert ops[0]["component_id"] != ops[1]["component_id"]
    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 2
    assert [r["position"] for r in rows] == [1, 2]
    assert {r["component_data"]["body"] for r in rows} == {"north", "south"}


def test_single_source_supersede_updates_existing(ws, chat):
    chat_id, user_id = chat
    ops1 = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1}, body="old")])
    cid = ops1[0]["component_id"]

    ops2 = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 2}, body="new")])
    assert len(ops2) == 1
    assert ops2[0]["created"] is False
    assert ops2[0]["component_id"] == cid, "supersede keeps the existing identity"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 1, "supersede must not add a new row"
    assert rows[0]["component_id"] == cid
    assert rows[0]["component_data"]["body"] == "new"
    assert rows[0]["component_data"]["_source_params"] == {"q": 2}


def test_supersede_ambiguity_appends_as_new(ws, chat):
    chat_id, user_id = chat
    ws.upsert(
        chat_id,
        user_id,
        [
            _comp("agentX", "toolY", {"region": "north"}),
            _comp("agentX", "toolY", {"region": "south"}),
        ],
    )
    ops = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"region": "east"}, body="east")])
    assert ops[0]["created"] is True, "ambiguous supersede must append, not update"
    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 3
    assert rows[2]["component_id"] == fingerprint("agentX", "toolY", {"region": "east"})
    assert rows[2]["position"] == 3
    assert {r["component_data"]["_source_params"]["region"] for r in rows[:2]} == {"north", "south"}


def test_force_component_id_pins_target_identity(ws, chat):
    chat_id, user_id = chat
    ops1 = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1}, body="orig")])
    cid = ops1[0]["component_id"]

    result = _comp("otherAgent", "otherTool", {"completely": "different"}, body="acted")
    ops2 = ws.upsert(chat_id, user_id, [result], force_component_id=cid)
    assert ops2[0]["component_id"] == cid
    assert ops2[0]["created"] is False

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 1, "pinned result replaces the target, no new row"
    assert rows[0]["component_id"] == cid
    assert rows[0]["component_data"]["body"] == "acted"


def test_remove_deletes_and_flips_flag(ws, history, chat):
    chat_id, user_id = chat
    ops = ws.upsert(
        chat_id,
        user_id,
        [_comp("agentA", "toolA", {"a": 1}), _comp("agentB", "toolB", {"b": 2})],
    )
    cid_a, cid_b = ops[0]["component_id"], ops[1]["component_id"]
    assert history.chat_has_saved_components(chat_id, user_id) is True

    assert ws.remove(chat_id, user_id, cid_a) is True
    assert len(ws.live_rows(chat_id, user_id)) == 1
    assert history.chat_has_saved_components(chat_id, user_id) is True, "one component remains"

    assert ws.remove(chat_id, user_id, cid_b) is True
    assert ws.live_rows(chat_id, user_id) == []
    assert history.chat_has_saved_components(chat_id, user_id) is False

    assert ws.remove(chat_id, user_id, "wc_nonexistent000") is False


def test_live_components_ordering_and_legacy_rows_sort_last(
    ws, history, chat, plane_runtime
):
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("agentA", "toolA", {"n": 1}, marker="pos1")])
    ws.upsert(chat_id, user_id, [_comp("agentB", "toolB", {"n": 2}, marker="pos2")])

    now = int(time.time() * 1000)
    for marker, created_at in (("legacy1", now - 100_000), ("legacy2", now - 50_000)):
        plane_runtime.execute(
            "INSERT INTO saved_components (id, chat_id, user_id, component_data, "
            "component_type, title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), chat_id, user_id,
             json.dumps({"type": "card", "marker": marker}), "card", marker, created_at),
        )

    comps = ws.live_components(chat_id, user_id)
    assert [c["marker"] for c in comps] == ["pos1", "pos2", "legacy1", "legacy2"]

    rows = ws.live_rows(chat_id, user_id)
    assert [r["position"] for r in rows] == [1, 2, None, None]
    assert comps[0]["component_id"] == fingerprint("agentA", "toolA", {"n": 1})

    saved_id = history.save_component(
        chat_id,
        {"type": "card", "marker": "post-legacy"},
        "card",
        user_id=user_id,
    )
    saved = next(row for row in ws.live_rows(chat_id, user_id)
                 if row["component_id"] == saved_id)
    assert saved["position"] == 3
