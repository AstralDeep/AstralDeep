"""Feature 028 — turn-boundary workspace snapshots (FR-030 / FR-018).

The orchestrator closes every workspace-changing turn with
``workspace.snapshot(chat_id, user_id, cause="turn",
turn_message_id=history.get_latest_message_id(...))`` at two sites
(tool-output turn and final rich-response turn in
``handle_chat_message``), each guarded by ``if ws_ops`` /
``if final_ops`` from ``_send_or_replace_components``.

These tests exercise that seam: the real, unbound
``Orchestrator._send_or_replace_components`` bound onto a fake ``self``
(pattern from test_component_action.py) over a REAL Postgres-backed
``WorkspaceManager``, then the snapshot-write contract itself — the
exact call shape the turn sites use, the one-snapshot-per-turn batching,
the empty-ops guard, and both CASCADE directions (chat delete and the
``fk_workspace_snapshot_turn_message`` NOT VALID message-delete FK).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.workspace import WorkspaceManager  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


class _FakeWS:
    """Hashable, identity-compared websocket stand-in (SimpleNamespace is
    unhashable, which breaks ROTE's profile map and send_ui_upsert's
    ``websocket not in targets`` check)."""

    def __init__(self, label: str = ""):
        self.label = label


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("snapshot_turn") as runtime:
        yield runtime


@pytest.fixture
def chat_env(plane_runtime):
    """Real HistoryManager + a unique user/chat pair; chat deleted on teardown
    (FK CASCADE clears messages, saved_components and workspace_snapshot)."""
    from orchestrator.history import HistoryManager

    history = HistoryManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    user_id = f"pytest-snapturn-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


@pytest.fixture
def audit_events(monkeypatch):
    """Capture audit.hooks.record_workspace_event calls (the orchestrator
    imports it at call time, so patching the module attribute is enough)."""
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_workspace_event", _record)
    return events


def _make_fake(history, user_id):
    """Fake orchestrator ``self`` carrying ONLY what the methods under test
    touch, with the real 028 implementations bound onto it."""
    from rote.rote import ROTE

    sent = []     # (ws, parsed-json) for every _safe_send
    renders = []  # (ws, components, target) for every send_ui_render

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((ws, components, target))

    fake = types.SimpleNamespace(
        workspace=WorkspaceManager(history),
        history=history,
        _ws_active_chat={},
        ui_clients=[],
        rote=ROTE(),
        _get_user_id=lambda ws: user_id,
        _safe_send=_safe_send,
        send_ui_render=send_ui_render,
    )
    for name in ("_send_or_replace_components", "send_ui_upsert"):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._sent = sent
    fake._renders = renders
    return fake


def _comp(agent, tool, params, **extra):
    """A rich, source-tagged component as the chat loop's _tag_source emits."""
    c = {
        "type": "table",
        "headers": ["Name"],
        "rows": [["Alice"]],
        "_source_agent": agent,
        "_source_tool": tool,
        "_source_params": params,
    }
    c.update(extra)
    return c


def _run(coro):
    """asyncio.run + a few zero-sleeps so fire-and-forget audit tasks
    (asyncio.create_task in _send_or_replace_components) complete."""

    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    return asyncio.run(_wrapper())


def _snapshot_rows(history, chat_id, user_id):
    workspace = WorkspaceManager(history)
    summaries = workspace.list_snapshots(chat_id, user_id, limit=200)
    return [
        workspace.get_snapshot(summary["id"], user_id)
        for summary in sorted(summaries, key=lambda value: value["id"])
    ]


# ---------------------------------------------------------------------------
# _send_or_replace_components — the op source the turn sites snapshot from
# ---------------------------------------------------------------------------


def test_send_or_replace_returns_ops_and_persists_rows(chat_env, audit_events):
    """028 FR-018/FR-030: two rich components persist into saved_components
    under stable identities, the returned op list reflects both (the turn
    site's ``if ws_ops`` snapshot trigger), and a dual-shape ui_upsert
    reaches the originating socket."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    ws = _FakeWS("origin")

    comps = [
        _comp("agent-x", "list_patients", {"page": 1}, title="Patients"),
        _comp("agent-y", "graph_ages", {"bucket": 10}, title="Ages",
              rows=[["30-39"]]),
    ]
    ops = _run(fake._send_or_replace_components(ws, comps, chat_id, user_id=user_id))

    # Ops returned — this truthy list is exactly what arms the turn-site
    # snapshot guard (``if ws_ops`` / ``if final_ops``).
    assert len(ops) == 2
    assert all(op["op"] == "upsert" and op["created"] for op in ops)
    cids = [op["component_id"] for op in ops]
    assert len(set(cids)) == 2

    # Rows persisted in the live workspace, in order, content intact.
    rows = fake.workspace.live_rows(chat_id, user_id)
    assert [r["component_id"] for r in rows] == cids
    assert [r["position"] for r in rows] == [1, 2]
    assert rows[0]["component_data"]["title"] == "Patients"
    assert rows[1]["component_data"]["rows"] == [["30-39"]]

    # The originating socket received ONE ui_upsert carrying both ops in
    # the dual component+html shape.
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 1
    assert upserts[0]["chat_id"] == chat_id
    assert [op["component_id"] for op in upserts[0]["ops"]] == cids
    for op in upserts[0]["ops"]:
        assert op["component"]["type"] == "table"
        assert op["html"] and f'data-component-id="{op["component_id"]}"' in op["html"]

    # Both creations audited (FR-023).
    added = [e for e in audit_events if e.get("action") == "component_added"]
    assert sorted(e["component_id"] for e in added) == sorted(cids)

    # No snapshot yet — the turn sites write it AFTER this call returns.
    assert _snapshot_rows(history, chat_id, user_id) == []


def test_empty_components_return_no_ops_so_turn_guard_skips_snapshot(chat_env, audit_events):
    """028 FR-030: a turn that produced no workspace ops must not snapshot —
    both empty input and the no-chat fallback return [] (falsy), so the
    ``if ws_ops`` guard at the turn sites never fires."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    ws = _FakeWS()

    assert _run(fake._send_or_replace_components(ws, [], chat_id, user_id=user_id)) == []

    # chat_id-less call: transient render fallback, still no ops.
    ops = _run(fake._send_or_replace_components(
        ws, [_comp("agent-x", "list_patients", {"page": 1})], "", user_id=user_id))
    assert ops == []
    assert fake._renders, "no-chat path must fall back to a transient ui_render"

    assert fake.workspace.live_rows(chat_id, user_id) == []
    assert _snapshot_rows(history, chat_id, user_id) == []


# ---------------------------------------------------------------------------
# The turn-site snapshot contract (cause='turn' + real turn_message_id)
# ---------------------------------------------------------------------------


def test_turn_snapshot_written_with_real_turn_message_id(chat_env, audit_events):
    """028 FR-030: the exact turn-site choreography — upsert ops, persist the
    assistant message, then snapshot(cause='turn',
    turn_message_id=get_latest_message_id(...)). The row must carry that
    message id and a components payload equal to live_components."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)

    comps = [
        _comp("agent-x", "list_patients", {"page": 1}, title="Patients"),
        _comp("agent-y", "graph_ages", {"bucket": 10}, title="Ages"),
    ]
    ops = _run(fake._send_or_replace_components(_FakeWS(), comps, chat_id, user_id=user_id))
    assert ops

    # Turn site persists the assistant message first, then snapshots.
    history.add_message(chat_id, "assistant", comps, user_id=user_id)
    mid = history.get_latest_message_id(chat_id, user_id=user_id)
    assert isinstance(mid, int)

    sid = fake.workspace.snapshot(chat_id, user_id, cause="turn", turn_message_id=mid)
    assert isinstance(sid, int)

    rows = _snapshot_rows(history, chat_id, user_id)
    assert len(rows) == 1
    snap = rows[0]
    assert snap["id"] == sid
    assert snap["cause"] == "turn"
    assert snap["turn_message_id"] == mid
    assert snap["created_at"] > 0

    # Full-state capture: the JSON payload IS the live workspace, ids included.
    assert snap["components"] == fake.workspace.live_components(chat_id, user_id)


def test_one_snapshot_per_turn_batches_all_ops(chat_env, audit_events):
    """028 FR-030: a turn with MULTIPLE workspace mutations still closes with
    exactly ONE cause='turn' snapshot row, and that single row already
    contains every component the turn produced."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    ws = _FakeWS()

    # Two upserts within one logical turn (e.g. two tool dispatches).
    ops1 = _run(fake._send_or_replace_components(
        ws, [_comp("agent-x", "list_patients", {"page": 1})], chat_id, user_id=user_id))
    ops2 = _run(fake._send_or_replace_components(
        ws, [_comp("agent-y", "graph_ages", {"bucket": 10})], chat_id, user_id=user_id))
    assert ops1 and ops2

    history.add_message(chat_id, "assistant", "turn output", user_id=user_id)
    mid = history.get_latest_message_id(chat_id, user_id=user_id)

    # ONE turn boundary -> ONE snapshot call.
    fake.workspace.snapshot(chat_id, user_id, cause="turn", turn_message_id=mid)

    rows = _snapshot_rows(history, chat_id, user_id)
    assert len(rows) == 1, "one turn must leave exactly one new snapshot row"
    snapped_ids = {c["component_id"] for c in rows[0]["components"]}
    assert snapped_ids == {ops1[0]["component_id"], ops2[0]["component_id"]}


# ---------------------------------------------------------------------------
# CASCADE — both directions (chat delete; message delete via NOT VALID FK)
# ---------------------------------------------------------------------------


def test_chat_delete_cascades_turn_snapshots(chat_env):
    """028 FR-033: deleting the chat removes its turn snapshots (and live
    workspace rows) via the chat_id FK CASCADE."""
    history, user_id, chat_id = chat_env
    workspace = WorkspaceManager(history)
    workspace.upsert(chat_id, user_id, [_comp("agent-x", "list_patients", {"page": 1})])
    history.add_message(chat_id, "assistant", "done", user_id=user_id)
    mid = history.get_latest_message_id(chat_id, user_id=user_id)
    workspace.snapshot(chat_id, user_id, cause="turn", turn_message_id=mid)
    assert len(_snapshot_rows(history, chat_id, user_id)) == 1

    history.delete_chat(chat_id, user_id=user_id)

    assert _snapshot_rows(history, chat_id, user_id) == []
    assert workspace.live_rows(chat_id, user_id) == []


def test_message_delete_cascades_only_that_turns_snapshot(chat_env):
    """028 FR-030 / data-model: fk_workspace_snapshot_turn_message is ON
    DELETE CASCADE. It was added NOT VALID, which only skips validating
    pre-existing rows — the cascade trigger still fires, so deleting the
    turn's message row deletes ITS snapshot while a sibling snapshot with
    no turn_message_id survives."""
    history, user_id, chat_id = chat_env
    workspace = WorkspaceManager(history)
    workspace.upsert(chat_id, user_id, [_comp("agent-x", "list_patients", {"page": 1})])

    history.add_message(chat_id, "assistant", "turn one", user_id=user_id)
    mid = history.get_latest_message_id(chat_id, user_id=user_id)
    sid_turn = workspace.snapshot(chat_id, user_id, cause="turn", turn_message_id=mid)
    sid_action = workspace.snapshot(chat_id, user_id, cause="component_action")
    assert len(_snapshot_rows(history, chat_id, user_id)) == 2

    with history.plane_runtime.transaction() as transaction:
        transaction.execute("DELETE FROM messages WHERE id = %s", (mid,))

    remaining = _snapshot_rows(history, chat_id, user_id)
    assert [r["id"] for r in remaining] == [sid_action], (
        "deleting the turn's message must cascade-delete exactly the snapshot "
        f"FK'd to it (expected only {sid_action} to survive, "
        f"snapshot {sid_turn} to be gone)"
    )
    assert remaining[0]["turn_message_id"] is None
