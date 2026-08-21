"""Feature 028 — workspace snapshots / read-only timeline (FR-030..FR-033).

Exercises the snapshot half of backend/orchestrator/workspace.py against a
real Postgres: full-state capture and immutability (a later live mutation
does not rewrite history), cause + turn_message_id recording, newest-first
listing with limit/offset, count, per-user scoping, and chat-delete CASCADE
removing both snapshots and live workspace rows (research D14).
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.workspace import WorkspaceManager  # noqa: E402
from tests.helpers.voice_plane_runtime import (  # noqa: E402
    history_manager,
    isolated_voice_plane_runtime,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("workspace_snapshots") as runtime:
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
    user_id = f"pytest-snap-{uuid.uuid4().hex[:12]}"
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
# snapshot() / get_snapshot() — full state, immutability
# ----------------------------------------------------------------------


def test_snapshot_records_full_state_and_is_immutable(ws, chat):
    """028 FR-030/FR-032: snapshot reproduces the workspace exactly; later live changes never mutate it."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [
        _comp("agentX", "toolY", {"q": 1}, body="alpha"),
        _comp("agentB", "toolB", {"k": 2}, body="beta"),
    ])
    before = ws.live_components(chat_id, user_id)
    assert len(before) == 2 and all(c.get("component_id") for c in before)

    sid = ws.snapshot(chat_id, user_id, cause="turn")
    assert isinstance(sid, int)

    snap = ws.get_snapshot(sid, user_id)
    assert snap is not None
    assert snap["chat_id"] == chat_id
    assert snap["components"] == before, "snapshot is the exact component list, ids included"

    # Mutate live state AFTER snapshotting — same identity, new content.
    ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1}, body="GAMMA")])
    live = ws.live_components(chat_id, user_id)
    assert any(c.get("body") == "GAMMA" for c in live)

    snap_again = ws.get_snapshot(sid, user_id)
    assert snap_again["components"] == before, "historical snapshot must still show the OLD state"
    assert all(c.get("body") != "GAMMA" for c in snap_again["components"])


def test_snapshot_causes_and_turn_message_id(ws, history, chat):
    """028 FR-030/FR-039: causes 'turn' and 'component_action' recorded; turn_message_id stored when given."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1})])

    history.add_message(chat_id, "user", "show me the data", user_id=user_id)
    mid = history.get_latest_message_id(chat_id, user_id)
    assert mid is not None

    sid_turn = ws.snapshot(chat_id, user_id, cause="turn", turn_message_id=mid)
    sid_action = ws.snapshot(chat_id, user_id, cause="component_action")

    turn = ws.get_snapshot(sid_turn, user_id)
    assert turn["cause"] == "turn"
    assert turn["turn_message_id"] == mid

    action = ws.get_snapshot(sid_action, user_id)
    assert action["cause"] == "component_action"
    assert action["turn_message_id"] is None


# ----------------------------------------------------------------------
# list_snapshots() / count_snapshots()
# ----------------------------------------------------------------------


def test_list_snapshots_newest_first_with_limit_offset_and_count(ws, chat):
    """028 FR-031: timeline lists snapshots newest-first with paging; count matches."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1})])

    ids = []
    for i in range(3):
        ids.append(ws.snapshot(chat_id, user_id, cause="turn"))
        time.sleep(0.005)
    s1, s2, s3 = ids

    assert ws.count_snapshots(chat_id, user_id) == 3

    listed = ws.list_snapshots(chat_id, user_id)
    assert [s["id"] for s in listed] == [s3, s2, s1], "newest first"
    assert all("components" not in s for s in listed), "metadata only — no payloads"
    assert all(s["cause"] == "turn" and s["chat_id"] == chat_id for s in listed)

    assert [s["id"] for s in ws.list_snapshots(chat_id, user_id, limit=2)] == [s3, s2]
    assert [s["id"] for s in ws.list_snapshots(chat_id, user_id, limit=2, offset=1)] == [s2, s1]
    assert ws.list_snapshots(chat_id, user_id, limit=2, offset=3) == []


# ----------------------------------------------------------------------
# user scoping
# ----------------------------------------------------------------------


def test_snapshot_user_scoping(ws, chat):
    """028 FR-033: another user cannot read someone else's snapshot."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [_comp("agentX", "toolY", {"q": 1})])
    sid = ws.snapshot(chat_id, user_id, cause="turn")

    other_user = f"pytest-snap-other-{uuid.uuid4().hex[:12]}"
    assert ws.get_snapshot(sid, other_user) is None
    assert ws.get_snapshot(sid, user_id) is not None
    # listing/counting under the wrong user sees nothing either
    assert ws.list_snapshots(chat_id, other_user) == []
    assert ws.count_snapshots(chat_id, other_user) == 0


# ----------------------------------------------------------------------
# CASCADE on chat delete
# ----------------------------------------------------------------------


def test_delete_chat_cascades_snapshots_and_workspace(ws, history, chat):
    """028 FR-033: deleting a chat deletes its snapshots and workspace rows."""
    chat_id, user_id = chat
    ws.upsert(chat_id, user_id, [
        _comp("agentX", "toolY", {"q": 1}),
        _comp("agentB", "toolB", {"k": 2}),
    ])
    ws.snapshot(chat_id, user_id, cause="turn")
    ws.snapshot(chat_id, user_id, cause="component_action")
    assert ws.count_snapshots(chat_id, user_id) == 2
    assert len(ws.live_rows(chat_id, user_id)) == 2

    history.delete_chat(chat_id, user_id)

    assert ws.count_snapshots(chat_id, user_id) == 0
    assert ws.live_rows(chat_id, user_id) == []
