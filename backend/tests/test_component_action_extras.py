"""Feature 028 — component-action gaps not covered by test_component_action.py.

Reuses the fake-orchestrator builder from tests.test_component_action (real,
unbound ``Orchestrator`` methods bound onto a SimpleNamespace-style fake with
a REAL Postgres-backed WorkspaceManager/HistoryManager).

Covers:
- kind validation (contracts/component-action.md): unknown kinds are refused
  with a chat-target Alert + an ``unsupported_kind:<k>`` denial audit and NO
  tool execution; ``invoke`` and an omitted kind both execute normally.
- the FR-038 ``table_paginate`` → ``component_action`` alias, driven through
  the REAL ``Orchestrator.handle_ui_message`` (bound onto the fake with
  ``ui_sessions`` registered) — both the payload mapping and end-to-end.
- EC-7 concurrency: two simultaneous actions on the same chat serialize on
  the per-chat workspace lock (no overlap; final state = last completed).
- FR-040/T038 device adaptation: ``send_ui_upsert`` adapts each op per
  receiving socket's ROTE profile (browser passthrough vs. watch degrade)
  while preserving the component identity and the dual component+html shape.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import types
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.orchestrator import Orchestrator
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_plane_runtime,
)
from tests.test_component_action import (
    _FakeWS,
    _alerts,
    _make_fake,
    _run,
    _seed_component,
)


# ---------------------------------------------------------------------------
# Fixtures (same shape as tests.test_component_action — defined locally so
# fixture names don't shadow imports, keeping ruff F811-clean)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def plane_runtime():
    """One managed application Plane runtime for this integration module."""
    with isolated_plane_runtime("component_action_extra") as runtime:
        yield runtime


@pytest.fixture
def chat_env(plane_runtime):
    """Real HistoryManager + a unique user/chat pair; chat deleted on teardown
    (FK CASCADE clears messages, saved_components and workspace_snapshot)."""
    history = history_manager(plane_runtime)
    user_id = f"test-user-{uuid.uuid4()}"
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


def _exec_result(rows):
    return types.SimpleNamespace(
        ui_components=[{"type": "table", "title": "Patients",
                        "headers": ["Name"], "rows": rows}],
        error=None,
    )


# ---------------------------------------------------------------------------
# Kind validation (contracts/component-action.md)
# ---------------------------------------------------------------------------

def test_component_action_unsupported_kind_refused(chat_env, audit_events):
    """An unknown action kind is refused explicitly: chat-target error Alert,
    an ``unsupported_kind:<k>`` denial audit, and NO tool execution (the
    refusal happens before the component is even resolved)."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, exec_result=_exec_result([["Bob"]]))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid,
        "kind": "delete-everything", "params_patch": {"page": 2},
    }))

    # No execution, no workspace mutation.
    assert fake._exec_calls == []
    assert [m for _, m in fake._sent if m["type"] == "ui_upsert"] == []
    row = fake.workspace.get_by_component_id(chat_id, user_id, cid)
    assert row["component_data"]["rows"] == [["Alice"]]

    # User-visible refusal on the chat target.
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "error"
    assert "Unsupported component action kind" in alerts[0]["message"]
    assert "delete-everything" in alerts[0]["message"]

    # Denial audited with the structured unsupported_kind reason.
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["detail"]["reason"] == "unsupported_kind:delete-everything"
    assert denials[0]["chat_id"] == chat_id
    assert denials[0]["component_id"] == cid


@pytest.mark.parametrize("kind", ["invoke", None], ids=["kind-invoke", "kind-omitted"])
def test_component_action_valid_kinds_execute(chat_env, audit_events, kind):
    """``kind='invoke'`` and an OMITTED kind (defaults to refresh) both pass
    validation and re-execute the source capability normally."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, exec_result=_exec_result([["Bob"]]))
    cid = _seed_component(fake.workspace, chat_id, user_id, params={"page": 1})

    payload = {"chat_id": chat_id, "component_id": cid, "params_patch": {"page": 5}}
    if kind is not None:
        payload["kind"] = kind
    _run(fake._handle_component_action(_FakeWS(), user_id, payload))

    # Executed exactly once with merged params; result pinned onto the
    # original identity; no denial was audited.
    assert len(fake._exec_calls) == 1
    agent_id, tool_name, args = fake._exec_calls[0]
    assert (agent_id, tool_name) == ("agent-x", "list_patients")
    assert args == {"page": 5}
    row = fake.workspace.get_by_component_id(chat_id, user_id, cid)
    assert row["component_data"]["rows"] == [["Bob"]]
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 1
    assert upserts[0]["ops"][0]["component_id"] == cid
    assert [e for e in audit_events if e.get("action") == "action_denied"] == []


# ---------------------------------------------------------------------------
# table_paginate alias (FR-038) — driven through the REAL handle_ui_message
# ---------------------------------------------------------------------------

def _bind_handle_ui_message(fake, ws, user_id, monkeypatch):
    """Make the real Orchestrator.handle_ui_message dispatchable on the fake:
    register the socket as an authenticated UI session and silence the
    per-action WS audit hook (it writes to the live audit table otherwise)."""
    ws_actions = []

    async def _record_ws_action(**kwargs):
        ws_actions.append(kwargs)

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_ws_action", _record_ws_action)
    fake.ui_sessions = {ws: {"sub": user_id, "preferred_username": "tester"}}
    # The fake intentionally binds the production dispatcher rather than
    # constructing a full Orchestrator. Keep its new structural parser seam
    # explicit instead of weakening production parsing for the test double.
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)
    return ws_actions


def test_table_paginate_alias_maps_to_component_action(chat_env, audit_events, monkeypatch):
    """FR-038: a ``table_paginate`` ui_event that carries the table's
    component identity is rewritten into the standardized component_action
    payload — kind='refresh', params under params_patch — and dispatched to
    ``_handle_component_action`` (asserted via a recording stub).

    NOTE: this drives the REAL ``Orchestrator.handle_ui_message`` bound onto
    the fake (per the audit suggestion), not an extracted transform."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, exec_result=_exec_result([["Bob"]]))
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS("paginator")
    ws_actions = _bind_handle_ui_message(fake, ws, user_id, monkeypatch)

    calls = []

    async def _recording_handler(websocket, uid, payload):
        calls.append((websocket, uid, payload))

    fake._handle_component_action = _recording_handler

    _run(fake.handle_ui_message(ws, json.dumps({
        "type": "ui_event", "action": "table_paginate",
        "payload": {"chat_id": chat_id, "component_id": cid,
                    "params": {"page": 7}},
    })))

    assert len(calls) == 1
    got_ws, got_uid, got_payload = calls[0]
    assert got_ws is ws
    assert got_uid == user_id
    assert got_payload == {
        "chat_id": chat_id,
        "component_id": cid,
        "kind": "refresh",
        "params_patch": {"page": 7},
    }
    # The legacy raw-params re-invoke path was NOT taken.
    assert fake._exec_calls == []
    # The WS action itself was audited under its original action name.
    assert any(a.get("action") == "table_paginate" for a in ws_actions)


def test_table_paginate_alias_end_to_end(chat_env, audit_events, monkeypatch):
    """FR-038 end-to-end: the alias rides the full standardized pipeline —
    source tool re-executed with merged params, result pinned onto the SAME
    component identity (single ui_upsert, no canvas replacement)."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, exec_result=_exec_result([["Paged"]]))
    cid = _seed_component(fake.workspace, chat_id, user_id,
                          params={"page": 1, "filter": "all"})
    ws = _FakeWS("paginator")
    _bind_handle_ui_message(fake, ws, user_id, monkeypatch)

    _run(fake.handle_ui_message(ws, json.dumps({
        "type": "ui_event", "action": "table_paginate",
        "payload": {"chat_id": chat_id, "component_id": cid,
                    "params": {"page": 2}},
    })))

    assert len(fake._exec_calls) == 1
    agent_id, tool_name, args = fake._exec_calls[0]
    assert (agent_id, tool_name) == ("agent-x", "list_patients")
    assert args == {"page": 2, "filter": "all"}

    # In-place morph of the SAME component, not a full-canvas re-render.
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 1
    assert upserts[0]["ops"][0]["component_id"] == cid
    assert fake._renders == []  # no send_ui_render fallback fired
    row = fake.workspace.get_by_component_id(chat_id, user_id, cid)
    assert row["component_data"]["rows"] == [["Paged"]]
    assert len(fake.workspace.live_rows(chat_id, user_id)) == 1
    # Pipeline completion signal still sent.
    statuses = [m for _, m in fake._sent if m["type"] == "chat_status"]
    assert statuses and statuses[-1]["status"] == "done"


# ---------------------------------------------------------------------------
# EC-7 — concurrent actions on the same chat serialize
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_component_action_concurrent_calls_serialize(chat_env, audit_events):
    """EC-7 (contract §Concurrency): two simultaneous component actions on
    the same chat are serialized by the per-chat workspace lock — the second
    execution begins only after the first one (and its upsert) completed —
    and the final workspace state is the LAST completed result."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = await asyncio.to_thread(
        _seed_component, fake.workspace, chat_id, user_id, params={"page": 1})

    gate = asyncio.Event()
    order = []
    seq = itertools.count()

    async def _blocking_exec(websocket, agent_id, tool_name, args):
        n = next(seq)
        order.append(("enter", n))
        if n == 0:
            await gate.wait()  # hold the lock until the test releases it
        order.append(("exit", n))
        return _exec_result([[f"call-{n}"]])

    fake._execute_with_retry = _blocking_exec

    async def _settle(condition, timeout=5.0):
        """Poll until condition() holds (the handler hops through worker
        threads, so plain zero-sleep drains are not enough)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

    t1 = asyncio.create_task(fake._handle_component_action(
        _FakeWS("first"), user_id,
        {"chat_id": chat_id, "component_id": cid, "kind": "refresh",
         "params_patch": {"page": 2}}))
    await _settle(lambda: order == [("enter", 0)])
    assert order == [("enter", 0)]  # first call inside execution, lock held

    t2 = asyncio.create_task(fake._handle_component_action(
        _FakeWS("second"), user_id,
        {"chat_id": chat_id, "component_id": cid, "kind": "refresh",
         "params_patch": {"page": 3}}))
    await asyncio.sleep(0.2)
    # Second call is parked on the per-chat lock — it has NOT entered.
    assert order == [("enter", 0)]

    gate.set()
    await asyncio.gather(t1, t2)
    for _ in range(3):
        await asyncio.sleep(0)  # drain fire-and-forget audit tasks

    # Strict serialization: second enters only after the first exited.
    assert order == [("enter", 0), ("exit", 0), ("enter", 1), ("exit", 1)]

    # Final state = last completed call; still a single workspace row.
    row = await fake.workspace.aget_by_component_id(chat_id, user_id, cid)
    assert row["component_data"]["rows"] == [["call-1"]]
    assert row["component_data"]["_source_params"]["page"] == 3
    assert len(await fake.workspace.alive_rows(chat_id, user_id)) == 1

    # Both turns completed (one chat_status done per call) and each pushed
    # its own in-place upsert of the same component.
    statuses = [m for _, m in fake._sent if m["type"] == "chat_status"]
    assert len(statuses) == 2 and all(s["status"] == "done" for s in statuses)
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 2
    assert all(u["ops"][0]["component_id"] == cid for u in upserts)


# ---------------------------------------------------------------------------
# FR-040 / T038 — per-device adaptation of ui_upsert ops
# ---------------------------------------------------------------------------

def test_send_ui_upsert_adapts_per_device(chat_env):
    """FR-040/T038: ``send_ui_upsert`` adapts every op per receiving device.
    A browser socket gets the chart passthrough while a watch socket (ROTE
    profile: supports_charts=False) gets the degraded component — both under
    the SAME component_id and both with a rendered html fragment."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    ws_browser, ws_watch = _FakeWS("browser"), _FakeWS("watch")
    fake.ui_clients = [ws_browser, ws_watch]
    fake._ws_active_chat = {id(ws_browser): chat_id, id(ws_watch): chat_id}
    # Browser socket stays on the default profile; the watch registers its
    # capabilities exactly as register_ui would.
    profile = fake.rote.register_device(
        ws_watch, {"device_type": "watch", "viewport_width": 180})
    assert profile.supports_charts is False

    # NOTE: the adapter's chart-value extraction expects the ``datasets``
    # shape for bar charts (a raw ``data: [int, ...]`` list trips its plotly
    # fallback) — use the supported shape.
    ops = fake.workspace.upsert(chat_id, user_id, [{
        "type": "bar_chart", "title": "Ages by decade",
        "labels": ["20s", "30s"],
        "datasets": [{"label": "Patients", "data": [4, 9]}],
        "_source_agent": "agent-x", "_source_tool": "graph_ages",
        "_source_params": {"bucket": 10},
    }])
    assert len(ops) == 1
    cid = ops[0]["component_id"]

    asyncio.run(fake.send_ui_upsert(ws_browser, chat_id, user_id, ops))

    by_ws = {}
    for ws, msg in fake._sent:
        by_ws.setdefault(id(ws), []).append(msg)

    browser_upserts = [m for m in by_ws.get(id(ws_browser), []) if m["type"] == "ui_upsert"]
    watch_upserts = [m for m in by_ws.get(id(ws_watch), []) if m["type"] == "ui_upsert"]
    assert len(browser_upserts) == 1
    assert len(watch_upserts) == 1

    browser_op = browser_upserts[0]["ops"][0]
    watch_op = watch_upserts[0]["ops"][0]

    # Browser: passthrough — the chart arrives untouched.
    assert browser_op["component"]["type"] == "bar_chart"
    assert browser_op["component_id"] == cid

    # Watch: the chart was ADAPTED (degraded to a non-chart component) but
    # keeps the same identity so the in-place morph still targets the row.
    assert watch_op["component_id"] == cid
    assert watch_op["component"] != browser_op["component"]
    assert watch_op["component"]["type"] != "bar_chart"
    assert watch_op["component"]["component_id"] == cid

    # Dual shape on BOTH sockets: structured component + web html fragment
    # anchored on the component identity.
    for op in (browser_op, watch_op):
        assert isinstance(op["html"], str) and op["html"]
        assert f'data-component-id="{cid}"' in op["html"]
