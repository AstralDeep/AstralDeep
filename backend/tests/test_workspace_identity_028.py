"""Feature 028 — workspace identity EDGE cases (FR-019 / FR-037).

Companion to test_workspace_manager.py, focused on the boundaries of
``WorkspaceManager.upsert`` identity resolution in
backend/orchestrator/workspace.py:

* FR-019 — an explicit author id is authoritative: a NEW explicit identity
  from the same (agent, tool) APPENDS and never steals (supersedes) the
  existing fingerprint-identity component's place.
* FR-019 — an author ECHOING an existing workspace identity (``wc_…`` or
  ``au_…``) updates that exact component in place.
* Regression — the id-less single-source supersede (docstring rule 3)
  still updates in place when params change.
* FR-037 — a stale/absent ``force_component_id`` target degrades gracefully:
  the result is INSERTED under that identity (created=True, no exception).
* FR-019 — two distinct explicit identities from the same tool coexist.
"""
from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.workspace import (  # noqa: E402
    WorkspaceManager,
    family_base_identity,
    fingerprint,
    iter_layout_refs,
)
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


# ----------------------------------------------------------------------
# Fixtures (mirroring test_workspace_manager.py)
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("workspace_identity") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def history(plane_runtime):
    from orchestrator.history import HistoryManager

    return HistoryManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture(scope="module")
def ws(history):
    return WorkspaceManager(history)


@pytest.fixture
def chat(history):
    """A fresh chat with a unique user per test; CASCADE cleans children."""
    user_id = f"pytest-wsid-{uuid.uuid4().hex[:12]}"
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
# FR-019: explicit author id never supersedes a fingerprint identity
# ----------------------------------------------------------------------


def test_explicit_id_from_same_source_appends_never_supersedes(ws, chat):
    """028 FR-019: a NEW explicit id from the same (agent, tool) appends; the
    existing fingerprint-identity component is left completely untouched."""
    chat_id, user_id = chat

    ops1 = ws.upsert(
        chat_id, user_id,
        [_comp("agentA", "tool1", {"q": 1}, title="Orig", body="original")],
    )
    assert len(ops1) == 1 and ops1[0]["created"] is True
    fp_cid = ops1[0]["component_id"]
    assert fp_cid == fingerprint("agentA", "tool1", {"q": 1})

    before = ws.live_rows(chat_id, user_id)
    assert len(before) == 1
    orig = before[0]

    time.sleep(0.05)  # so updated_at would visibly change if (wrongly) touched
    ops2 = ws.upsert(
        chat_id, user_id,
        [_comp("agentA", "tool1", {"q": 2}, id="au_other", title="New", body="explicit")],
    )
    assert len(ops2) == 1
    assert ops2[0]["created"] is True, "explicit identity must append, not supersede"
    assert ops2[0]["component_id"] == "au_other"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 2, "explicit-id component appends as a second row"
    assert [r["component_id"] for r in rows] == [fp_cid, "au_other"]
    assert [r["position"] for r in rows] == [1, 2]

    # The original fingerprint-identity row is byte-for-byte unchanged.
    after_orig = rows[0]
    assert after_orig["id"] == orig["id"]
    assert after_orig["component_id"] == fp_cid
    assert after_orig["component_data"]["body"] == "original"
    assert after_orig["component_data"]["_source_params"] == {"q": 1}
    assert after_orig["title"] == "Orig"
    assert after_orig["updated_at"] == orig["updated_at"]
    assert after_orig["position"] == 1


# ----------------------------------------------------------------------
# FR-019: echoed workspace identity updates that exact row in place
# ----------------------------------------------------------------------


def test_echoed_wc_identity_updates_in_place(ws, chat):
    """028 FR-019: a component whose 'id' echoes an existing row's wc_
    component_id updates THAT row in place (one row, new content)."""
    chat_id, user_id = chat

    ops1 = ws.upsert(chat_id, user_id, [_comp("agentA", "tool1", {"q": 1}, body="v1")])
    wc_cid = ops1[0]["component_id"]
    assert wc_cid.startswith("wc_")
    row_id = ws.live_rows(chat_id, user_id)[0]["id"]

    # Echo the workspace identity back as the author 'id' — different params.
    echo = _comp("agentA", "tool1", {"q": 99}, id=wc_cid, body="v2-echoed")
    ops2 = ws.upsert(chat_id, user_id, [echo])
    assert len(ops2) == 1
    assert ops2[0]["created"] is False
    assert ops2[0]["component_id"] == wc_cid

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 1, "echoed identity must not create a second row"
    assert rows[0]["id"] == row_id
    assert rows[0]["component_id"] == wc_cid
    assert rows[0]["component_data"]["body"] == "v2-echoed"


def test_echoed_au_identity_updates_in_place(ws, chat):
    """028 FR-019: same echo contract for an au_-namespaced explicit identity."""
    chat_id, user_id = chat

    ops1 = ws.upsert(
        chat_id, user_id, [_comp("agentA", "tool1", {"q": 1}, id="mine", body="v1")]
    )
    assert ops1[0]["component_id"] == "au_mine"

    echo = _comp("agentA", "tool1", {"q": 2}, id="au_mine", body="v2")
    ops2 = ws.upsert(chat_id, user_id, [echo])
    assert ops2[0]["created"] is False
    assert ops2[0]["component_id"] == "au_mine"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 1
    assert rows[0]["component_id"] == "au_mine"
    assert rows[0]["component_data"]["body"] == "v2"


# ----------------------------------------------------------------------
# Regression: rule-3 single-source supersede unaffected by the id guards
# ----------------------------------------------------------------------


def test_idless_single_source_supersede_still_updates_in_place(ws, chat):
    """028 FR-019 rule 3 (regression): a lone id-less same-(agent, tool)
    re-call with different params still updates the existing row in place."""
    chat_id, user_id = chat

    ops1 = ws.upsert(chat_id, user_id, [_comp("agentA", "tool1", {"q": 1}, body="old")])
    cid = ops1[0]["component_id"]

    ops2 = ws.upsert(chat_id, user_id, [_comp("agentA", "tool1", {"q": 2}, body="new")])
    assert len(ops2) == 1
    assert ops2[0]["created"] is False
    assert ops2[0]["component_id"] == cid, "supersede keeps the existing identity"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 1, "supersede must not add a row"
    assert rows[0]["component_id"] == cid
    assert rows[0]["component_data"]["body"] == "new"
    assert rows[0]["component_data"]["_source_params"] == {"q": 2}


# ----------------------------------------------------------------------
# Multi-component tool results: ordinal identities, no sibling supersede
# ----------------------------------------------------------------------


def test_multicomponent_batch_keeps_every_component(ws, chat):
    """A single tool result carrying many id-less components must persist
    them ALL — without ordinal identities they share one fingerprint and
    supersede each other down to a single surviving row (the bug that
    collapsed dashboard tool output to its last caption)."""
    chat_id, user_id = chat
    batch = [_comp("agentA", "dash", {"q": 1}, type=t, body=f"w{n}")
             for n, t in enumerate(["hero", "metric", "table", "text"])]

    ops = ws.upsert(chat_id, user_id, batch)
    ids = [op["component_id"] for op in ops]
    assert len(set(ids)) == 4, "every component keeps its own identity"
    assert all(op["created"] for op in ops)

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 4
    assert [r["component_data"]["body"] for r in rows] == ["w0", "w1", "w2", "w3"]


def test_duplicate_explicit_ids_in_one_batch_coexist(ws, chat):
    """Parallel calls of a tool that hardcodes an author id (the general
    agent's chart-card) land in one round — they must all survive, not
    supersede each other onto au_<id>."""
    chat_id, user_id = chat
    batch = [_comp("general-1", "generate_dynamic_chart", {"q": n},
                   id="chart-card", body=f"chart{n}") for n in range(3)]
    ops = ws.upsert(chat_id, user_id, batch)
    ids = [op["component_id"] for op in ops]
    assert len(set(ids)) == 3
    assert ids[0] == "au_chart-card", "first occurrence keeps the plain identity"
    assert all(i.startswith("au_chart-card") for i in ids), "ordinal ids keep the au_ prefix"
    assert len(ws.live_rows(chat_id, user_id)) == 3


def test_family_member_refresh_reassigns_slot_for_slot(ws, chat):
    """Regression (verified corruption): a component_action refresh on a
    NON-base member of a multi-component family re-executes the source tool,
    which returns ALL members. Pinning only batch index 0 onto the clicked id
    while the siblings ran the zero-based ordinal enumeration shifted every
    output one slot and double-targeted the clicked id (the hero vanished,
    the family was permanently corrupted). The fix re-assigns the batch
    slot-for-slot onto the family's ordinal identities."""
    chat_id, user_id = chat
    types = ["hero", "line_chart", "metric", "metric", "timeline", "text"]

    seed = ws.upsert(chat_id, user_id, [
        _comp("agentA", "dash", {"q": 1}, type=t, body=f"old-{n}-{t}")
        for n, t in enumerate(types)
    ])
    family = [op["component_id"] for op in seed]
    base = fingerprint("agentA", "dash", {"q": 1})
    assert family == [base] + [f"{base}~{n}" for n in range(1, 6)]
    assert len(ws.live_rows(chat_id, user_id)) == 6

    # User clicked the SECOND member (~1) — fresh dicts, like a re-executed tool.
    refresh = ws.upsert(
        chat_id, user_id,
        [_comp("agentA", "dash", {"q": 1}, type=t, body=f"new-{n}-{t}")
         for n, t in enumerate(types)],
        force_component_id=f"{base}~1",
    )
    assert [op["component_id"] for op in refresh] == family, \
        "outputs must land slot-for-slot on the family identities, no shift"
    assert len(set(op["component_id"] for op in refresh)) == 6, "no duplicate targets"
    assert all(op["created"] is False for op in refresh), "every member morphs in place"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 6, "row count unchanged — no phantom appends"
    assert [r["component_id"] for r in rows] == family
    assert [r["position"] for r in rows] == [1, 2, 3, 4, 5, 6]
    # Slot-for-slot content: hero stays at base, line_chart at ~1, etc.
    for n, (row, t) in enumerate(zip(rows, types)):
        assert row["component_data"]["type"] == t
        assert row["component_data"]["body"] == f"new-{n}-{t}"


def test_family_base_refresh_also_reassigns_slot_for_slot(ws, chat):
    """Same contract when the clicked member IS the family base (no ~N
    suffix to strip) — pre-fix this case double-targeted the base id."""
    chat_id, user_id = chat
    types = ["hero", "metric", "text"]
    seed = ws.upsert(chat_id, user_id, [
        _comp("agentA", "dash", {"q": 2}, type=t, body=f"old-{t}") for t in types
    ])
    family = [op["component_id"] for op in seed]
    base = fingerprint("agentA", "dash", {"q": 2})
    assert family == [base, f"{base}~1", f"{base}~2"]
    assert family_base_identity(f"{base}~2") == base
    assert family_base_identity(base) == base

    refresh = ws.upsert(
        chat_id, user_id,
        [_comp("agentA", "dash", {"q": 2}, type=t, body=f"new-{t}") for t in types],
        force_component_id=base,
    )
    assert [op["component_id"] for op in refresh] == family
    assert all(op["created"] is False for op in refresh)
    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 3
    assert [r["component_data"]["body"] for r in rows] == [f"new-{t}" for t in types]


def test_duplicate_target_guard_appends_never_overwrites(ws, chat, caplog):
    """Within ONE batch the same resolved identity must never be written
    twice: the collision falls back to appending under a free ordinal
    identity with a structured warning (never a silent overwrite)."""
    chat_id, user_id = chat
    fp = fingerprint("agentA", "dash", {"q": 1})

    # comp1 arrives pre-stamped with the ~1 family identity; comp2/comp3 are
    # id-less siblings whose ordinal enumeration would ALSO produce fp~1.
    comp1 = _comp("agentA", "dash", {"q": 1}, component_id=f"{fp}~1", body="stamped")
    comp2 = _comp("agentA", "dash", {"q": 1}, body="sibling-0")
    comp3 = _comp("agentA", "dash", {"q": 1}, body="sibling-1")

    with caplog.at_level(logging.WARNING, logger="orchestrator.workspace"):
        ops = ws.upsert(chat_id, user_id, [comp1, comp2, comp3])

    ids = [op["component_id"] for op in ops]
    assert ids == [f"{fp}~1", fp, f"{fp}~2"], \
        "collision on fp~1 must divert to the next free ordinal, not overwrite"
    assert len(set(ids)) == 3
    assert "duplicate target" in caplog.text

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 3, "guard appends — nothing dropped"
    by_id = {r["component_id"]: r["component_data"]["body"] for r in rows}
    assert by_id[f"{fp}~1"] == "stamped", "first writer of fp~1 is untouched"
    assert by_id[fp] == "sibling-0"
    assert by_id[f"{fp}~2"] == "sibling-1"


def test_multicomponent_rerun_supersedes_slot_for_slot(ws, chat):
    chat_id, user_id = chat
    first = ws.upsert(chat_id, user_id, [
        _comp("agentA", "dash", {"q": 1}, type="hero", body="old-hero"),
        _comp("agentA", "dash", {"q": 1}, type="metric", body="old-metric"),
    ])
    rerun = ws.upsert(chat_id, user_id, [
        _comp("agentA", "dash", {"q": 1}, type="hero", body="new-hero"),
        _comp("agentA", "dash", {"q": 1}, type="metric", body="new-metric"),
    ])
    assert [op["component_id"] for op in rerun] == [op["component_id"] for op in first], \
        "ordinal identities are deterministic — re-runs morph in place"
    assert all(op["created"] is False for op in rerun)
    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 2
    assert sorted(r["component_data"]["body"] for r in rows) == ["new-hero", "new-metric"]


# ----------------------------------------------------------------------
# Rule 4 (030, S7 regression): slot-matched family supersede
# ----------------------------------------------------------------------

S7_TYPES = ["hero", "line_chart", "metric", "metric", "timeline", "text"]


def _family_batch(params, types=S7_TYPES, tag="old", agent="agentA", tool="dash"):
    return [_comp(agent, tool, params, type=t, body=f"{tag}-{n}-{t}")
            for n, t in enumerate(types)]


def test_s7_param_change_supersedes_family_slot_for_slot(ws, chat, caplog):
    """S7 walkthrough regression: 'update the dashboard for week 17' re-runs
    the SAME (agent, tool) with changed params. The new fingerprint family
    must re-use the prior family's identities slot-for-slot — updating the
    dashboard in place — instead of appending 6 duplicates above stale data."""
    chat_id, user_id = chat

    seed = ws.upsert(chat_id, user_id, _family_batch({"week": 16}, tag="old"))
    family = [op["component_id"] for op in seed]
    base = fingerprint("agentA", "dash", {"week": 16})
    assert family == [base] + [f"{base}~{n}" for n in range(1, 6)]
    assert len(ws.live_rows(chat_id, user_id)) == 6

    with caplog.at_level(logging.INFO, logger="orchestrator.workspace"):
        rerun = ws.upsert(chat_id, user_id, _family_batch({"week": 17}, tag="new"))

    assert [op["component_id"] for op in rerun] == family, \
        "re-run must land slot-for-slot on the existing family identities"
    assert all(op["created"] is False for op in rerun), "update in place, no appends"
    assert "family_supersede" in caplog.text, "structured info line on supersede"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 6, "row count unchanged — the dashboard did not duplicate"
    assert [r["component_id"] for r in rows] == family
    for n, (row, t) in enumerate(zip(rows, S7_TYPES)):
        assert row["component_data"]["type"] == t
        assert row["component_data"]["body"] == f"new-{n}-{t}", "fresh (week 17) data"
        assert row["component_data"]["_source_params"] == {"week": 17}


def test_family_supersede_divergent_count_appends(ws, chat):
    """5 incoming vs 6 live: shape divergence means no guessing — append."""
    chat_id, user_id = chat
    seed = ws.upsert(chat_id, user_id, _family_batch({"week": 16}, tag="old"))
    assert len(seed) == 6

    rerun = ws.upsert(
        chat_id, user_id, _family_batch({"week": 17}, types=S7_TYPES[:5], tag="new"))
    assert len(rerun) == 5
    assert all(op["created"] is True for op in rerun), "count mismatch ⇒ append"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 11
    # The original family is untouched.
    by_id = {r["component_id"]: r["component_data"] for r in rows}
    for op in seed:
        assert by_id[op["component_id"]]["_source_params"] == {"week": 16}


def test_family_supersede_divergent_ordered_types_appends(ws, chat):
    """Same count but different ordered types: not the same dashboard — append."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, _family_batch({"week": 16}, tag="old"))

    swapped = list(S7_TYPES)
    swapped[1], swapped[4] = swapped[4], swapped[1]  # line_chart <-> timeline
    rerun = ws.upsert(
        chat_id, user_id, _family_batch({"week": 17}, types=swapped, tag="new"))
    assert all(op["created"] is True for op in rerun), "type divergence ⇒ append"
    assert len(ws.live_rows(chat_id, user_id)) == 12


def test_family_supersede_two_prior_families_appends(ws, chat):
    """TWO live families from the same (agent, tool): ambiguous — append."""
    chat_id, user_id = chat
    fam1 = ws.upsert(chat_id, user_id,
                     _family_batch({"q": 1}, types=["hero", "metric"], tag="f1"))
    # Different ordered types, so this second family APPENDS (and stays).
    fam2 = ws.upsert(chat_id, user_id,
                     _family_batch({"q": 2}, types=["table", "text"], tag="f2"))
    assert all(op["created"] for op in fam1 + fam2)
    assert len(ws.live_rows(chat_id, user_id)) == 4

    # 4 live rows from (agentA, dash) but TWO distinct family bases ⇒ append.
    third = ws.upsert(chat_id, user_id, _family_batch(
        {"q": 3}, types=["hero", "metric", "table", "text"], tag="f3"))
    assert all(op["created"] is True for op in third), "two prior families ⇒ append"
    assert len(ws.live_rows(chat_id, user_id)) == 8


def test_family_supersede_never_touches_explicit_id_batches(ws, chat):
    """A batch carrying explicit author ids never steals the live family's
    identities (FR-019: an explicit id never supersedes a different identity)."""
    chat_id, user_id = chat
    seed = ws.upsert(chat_id, user_id,
                     _family_batch({"q": 1}, types=["hero", "metric"], tag="old"))
    family = [op["component_id"] for op in seed]

    batch = _family_batch({"q": 2}, types=["hero", "metric"], tag="new")
    for n, c in enumerate(batch):
        c["id"] = f"mine-{n}"
    ops = ws.upsert(chat_id, user_id, batch)
    assert [op["component_id"] for op in ops] == ["au_mine-0", "au_mine-1"]
    assert all(op["created"] is True for op in ops), "explicit ids append, never remap"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 4
    by_id = {r["component_id"]: r["component_data"] for r in rows}
    for cid in family:
        assert by_id[cid]["_source_params"] == {"q": 1}, "live family untouched"


def test_family_supersede_never_steals_explicit_id_rows(ws, chat):
    """A live 'family' built from explicit au_ ids is never superseded by an
    id-less fingerprint-new batch — wc_* identities only."""
    chat_id, user_id = chat
    seeded = _family_batch({"q": 1}, types=["hero", "metric"], tag="old")
    for n, c in enumerate(seeded):
        c["id"] = f"theirs-{n}"
    seed = ws.upsert(chat_id, user_id, seeded)
    assert [op["component_id"] for op in seed] == ["au_theirs-0", "au_theirs-1"]

    rerun = ws.upsert(chat_id, user_id,
                      _family_batch({"q": 2}, types=["hero", "metric"], tag="new"))
    assert all(op["created"] is True for op in rerun), "au_ rows are never remapped onto"
    assert all(op["component_id"].startswith("wc_") for op in rerun)

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 4
    by_id = {r["component_id"]: r["component_data"] for r in rows}
    assert by_id["au_theirs-0"]["_source_params"] == {"q": 1}
    assert by_id["au_theirs-1"]["_source_params"] == {"q": 1}


def test_layout_refs_survive_family_supersede(ws, chat):
    """029 arrangements reference components by id. Because family supersede
    REUSES the prior ids, a designed layout's refs stay valid and now resolve
    to the fresh data — no pruning, no dangling refs."""
    chat_id, user_id = chat
    seed = ws.upsert(chat_id, user_id, _family_batch({"week": 16}, tag="old"))
    family = [op["component_id"] for op in seed]

    layout = [
        {"type": "metric", "title": "Garnish", "value": "headline"},
        {"type": "grid", "columns": 3,
         "children": [{"type": "ref", "component_id": cid} for cid in family]},
    ]
    assert ws.upsert_layout(chat_id, user_id, "ly_s7test", layout) is True
    assert list(iter_layout_refs(ws.live_layouts(chat_id, user_id)[0]["layout"])) == family

    rerun = ws.upsert(chat_id, user_id, _family_batch({"week": 17}, tag="new"))
    assert [op["component_id"] for op in rerun] == family

    layouts = ws.live_layouts(chat_id, user_id)
    assert len(layouts) == 1
    refs = list(iter_layout_refs(layouts[0]["layout"]))
    assert refs == family, "arrangement refs unchanged after supersede"
    live = {r["component_id"]: r["component_data"] for r in ws.live_rows(chat_id, user_id)}
    assert set(refs) <= set(live), "every ref still resolves to a live component"
    for cid in refs:
        assert live[cid]["_source_params"] == {"week": 17}, \
            "the arrangement now shows the fresh (week 17) data"


# ----------------------------------------------------------------------
# FR-037: stale force_component_id target degrades to a graceful append
# ----------------------------------------------------------------------


def test_force_component_id_absent_target_inserts_new_row(ws, chat):
    """028 FR-037: pinning onto an identity ABSENT from the workspace (e.g. a
    component-action target removed mid-flight) inserts a new row under that
    identity — created=True, no exception (graceful-append contract)."""
    chat_id, user_id = chat

    ws.upsert(chat_id, user_id, [_comp("agentA", "tool1", {"q": 1}, body="bystander")])

    result = _comp("agentA", "tool1", {"page": 2}, body="acted")
    ops = ws.upsert(chat_id, user_id, [result], force_component_id="wc_deadbeef")
    assert len(ops) == 1
    assert ops[0]["created"] is True, "stale target must append, not raise/drop"
    assert ops[0]["component_id"] == "wc_deadbeef"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 2
    assert rows[1]["component_id"] == "wc_deadbeef"
    assert rows[1]["component_data"]["body"] == "acted"
    assert rows[1]["position"] == 2
    # bystander untouched
    assert rows[0]["component_data"]["body"] == "bystander"


# ----------------------------------------------------------------------
# FR-019: distinct explicit identities from the same tool coexist
# ----------------------------------------------------------------------


def test_two_explicit_ids_same_tool_coexist(ws, chat):
    """028 FR-019: two components from the same (agent, tool) carrying
    distinct explicit ids land as two rows (no supersede between them)."""
    chat_id, user_id = chat

    ops1 = ws.upsert(
        chat_id, user_id, [_comp("agentA", "tool1", {"q": 1}, id="au_a", body="A")]
    )
    ops2 = ws.upsert(
        chat_id, user_id, [_comp("agentA", "tool1", {"q": 2}, id="au_b", body="B")]
    )
    assert ops1[0]["created"] is True and ops1[0]["component_id"] == "au_a"
    assert ops2[0]["created"] is True and ops2[0]["component_id"] == "au_b"

    rows = ws.live_rows(chat_id, user_id)
    assert len(rows) == 2
    assert [r["component_id"] for r in rows] == ["au_a", "au_b"]
    assert [r["position"] for r in rows] == [1, 2]
    assert [r["component_data"]["body"] for r in rows] == ["A", "B"]
