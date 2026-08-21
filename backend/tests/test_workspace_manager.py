"""Feature 028 — WorkspaceManager identity, upsert, ordering (FR-018..FR-022).

Exercises the live-workspace half of backend/orchestrator/workspace.py against
a real Postgres: fingerprint stability and private-param exclusion, the
three-step identity resolution (author id -> fingerprint -> single-source
supersede), in-place updates vs appends, the same-batch coexist edge case,
force_component_id pinning, remove()/has_saved_components, and position-based
ordering with legacy NULL-position rows sorting last (research D11).
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


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


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
    """A fresh chat with a unique user per test; CASCADE cleans children."""
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


# ----------------------------------------------------------------------
# fingerprint()
# ----------------------------------------------------------------------


def test_fingerprint_stable_across_calls():
    """028 FR-019: identity is stable — same inputs always fingerprint alike."""
    a = fingerprint("agentX", "toolY", {"region": "north", "year": 2026})
    b = fingerprint("agentX", "toolY", {"region": "north", "year": 2026})
    assert a == b
    assert a.startswith("wc_") and len(a) == len("wc_") + 16
    # key order must not matter (canonical form)
    c = fingerprint("agentX", "toolY", {"year": 2026, "region": "north"})
    assert a == c


def test_fingerprint_differs_for_different_params():
    """028 FR-019: same capability with different parameters gets a distinct identity."""
    a = fingerprint("agentX", "toolY", {"region": "north"})
    b = fingerprint("agentX", "toolY", {"region": "south"})
    assert a != b
    # agent and tool are part of the basis too
    assert fingerprint("agentZ", "toolY", {"region": "north"}) != a
    assert fingerprint("agentX", "toolQ", {"region": "north"}) != a


def test_fingerprint_excludes_private_underscore_params():
    """028 FR-019 / research D11: '_'-prefixed (private/system) params are not identity."""
    base = fingerprint("agentX", "toolY", {"region": "north"})
    with_private = fingerprint(
        "agentX", "toolY", {"region": "north", "_credentials": "s3cret", "_trace_id": "t1"}
    )
    assert base == with_private
    assert canonical_params({"region": "north", "_credentials": "x"}) == canonical_params(
        {"region": "north"}
    )


# ----------------------------------------------------------------------
# resolve_identity()
# ----------------------------------------------------------------------


def test_resolve_identity_author_id_namespaced(ws):
    """028 FR-019: explicit astralprims id wins and is namespaced au_<id>."""
    comp = {"type": "card", "id": "abc"}
    assert ws.resolve_identity(comp) == "au_abc"
    assert comp["component_id"] == "au_abc"  # stamped onto the component


def test_resolve_identity_wc_echo_honored_verbatim(ws):
    """028 FR-019: an author echoing back a workspace identity keeps it verbatim."""
    comp = {"type": "card", "id": "wc_deadbeef"}
    assert ws.resolve_identity(comp) == "wc_deadbeef"


def test_resolve_identity_falls_back_to_fingerprint(ws):
    """028 FR-018: components without an author id get the source fingerprint."""
    comp = _comp("agentX", "toolY", {"q": 1})
    cid = ws.resolve_identity(comp)
    assert cid == fingerprint("agentX", "toolY", {"q": 1})
    assert comp["component_id"] == cid


def test_resolve_identity_existing_component_id_short_circuits(ws):
    """028 FR-019: a pre-stamped component_id is authoritative over everything else."""
    comp = {"type": "card", "id": "abc", "component_id": "wc_1234567890abcdef"}
    assert ws.resolve_identity(comp) == "wc_1234567890abcdef"


# ----------------------------------------------------------------------
# upsert(): create / update-in-place / append
# ----------------------------------------------------------------------


def test_upsert_create_then_update_in_place(ws, chat):
    """028 FR-018/FR-019: first upsert creates at position 1; same identity updates in place."""
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

    time.sleep(0.05)  # ensure updated_at can visibly advance (ms resolution)
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
    """028 FR-019: a new identity appends after existing components (position 2)."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1})])
    ops = ws.upsert(chat_id, user_id, [_comp("agentB", "toolB", {"k": "v"})])
    assert ops[0]["created"] is True
    rows = ws.live_rows(chat_id, user_id)
    assert [r["position"] for r in rows] == [1, 2]
    assert rows[1]["component_id"] == fingerprint("agentB", "toolB", {"k": "v"})


def test_same_tool_different_params_coexist_in_one_batch(ws, chat):
    """028 FR-019 / research D11: parallel same-tool calls in ONE batch coexist, never supersede."""
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
    """028 FR-019 / research D11 rule 3: lone same-(agent,tool) re-call with new params updates in place."""
    chat_id, user_id = chat
    ops1 = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1}, body="old")])
    cid = ops1[0]["component_id"]

    # Later turn: ONE component from the same (agent, tool) with DIFFERENT params.
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
    """028 FR-019 / research D11: with two existing same-source components, a new one appends (no clobber)."""
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
    # the two originals are untouched (FR-020: unrelated components undisturbed)
    assert {r["component_data"]["_source_params"]["region"] for r in rows[:2]} == {"north", "south"}


def test_force_component_id_pins_target_identity(ws, chat):
    """028 FR-019 / contracts/component-action.md: force_component_id wins regardless of params."""
    chat_id, user_id = chat
    ops1 = ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1}, body="orig")])
    cid = ops1[0]["component_id"]

    # Deterministic component action result: totally different source/params.
    result = _comp("otherAgent", "otherTool", {"completely": "different"}, body="acted")
    ops2 = ws.upsert(chat_id, user_id, [result], force_component_id=cid)
    assert ops2[0]["component_id"] == cid
    assert ops2[0]["created"] is False

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 1, "pinned result replaces the target, no new row"
    assert rows[0]["component_id"] == cid
    assert rows[0]["component_data"]["body"] == "acted"


# ----------------------------------------------------------------------
# remove() and has_saved_components
# ----------------------------------------------------------------------


def test_remove_deletes_and_flips_flag(ws, history, chat):
    """028 FR-018/FR-021: remove deletes by component_id; flag goes FALSE with the last one."""
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


# ----------------------------------------------------------------------
# ordering — positions first, legacy NULL-position rows last by created_at
# ----------------------------------------------------------------------


def test_live_components_ordering_and_legacy_rows_sort_last(
    ws, history, chat, plane_runtime
):
    """028 FR-019/FR-021 / research D11: position order; legacy NULL-position rows last by created_at."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("agentA", "toolA", {"n": 1}, marker="pos1")])
    ws.upsert(chat_id, user_id, [_comp("agentB", "toolB", {"n": 2}, marker="pos2")])

    # Legacy pre-028 rows: no component_id / position / updated_at. Give them
    # created_at EARLIER than the positioned rows so a pure created_at sort
    # would put them first — they must still sort LAST.
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
    # positioned rows carry their component_id through to the structured dicts
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
