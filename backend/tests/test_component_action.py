"""Feature 028 — deterministic component actions (FR-034..FR-040).

Exercises the real, unbound ``Orchestrator`` methods over a fake ``self``
(the full Orchestrator needs the whole stack) plus a REAL Postgres-backed
``WorkspaceManager``/``HistoryManager``, per
specs/028-workspace-auth-revival/contracts/component-action.md.

Covers: provenance re-execution with merged params, force-pin of the result
onto the original component identity, ui_upsert delivery + fan-out (FR-040),
the component_action snapshot (FR-039), permission/security-flag denial with
audit (FR-036), missing/unknown components (FR-037), and the read-only
timeline guard (FR-031).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.workspace import WorkspaceManager
from orchestrator.orchestrator import Orchestrator, PreparedDispatch
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_plane_runtime,
)


class _FakeWS:
    """Hashable, identity-compared stand-in for a websocket (SimpleNamespace
    is unhashable and compares by __dict__, which breaks ROTE's profile map
    and send_ui_upsert's ``websocket not in targets`` check)."""

    def __init__(self, label: str = ""):
        self.label = label


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def plane_runtime():
    """One managed application Plane runtime for this integration module."""
    with isolated_plane_runtime("component_action") as runtime:
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


def _make_fake(history, user_id, *, allowed=True, security_flags=None, exec_result=None):
    """Fake orchestrator ``self`` carrying ONLY what the methods under test
    touch, with the real 028 implementations bound onto it."""
    from rote.rote import ROTE

    sent = []        # (ws, parsed-json) for every _safe_send
    renders = []     # (ws, components, target) for every send_ui_render
    exec_calls = []  # (agent_id, tool_name, args) for every _execute_with_retry

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((ws, components, target))

    async def _execute_with_retry(ws, agent_id, tool_name, args):
        exec_calls.append((agent_id, tool_name, args))
        return exec_result

    async def _execute_with_retry_audited(
        ws,
        agent_id,
        tool_name,
        args,
        chat_id=None,
        user_id=None,
        **_dispatch_context,
    ):
        """Test seam for the production audited-dispatch wrapper."""
        del chat_id, user_id
        return await fake._execute_with_retry(ws, agent_id, tool_name, args)

    # A deterministic component action now routes its dispatch through the
    # shared authorizer (FR-036 gate parity). The full gate stack needs the
    # whole orchestrator; this passthrough grants and returns the prepared
    # args, so the flow under test (re-execute → upsert → snapshot) is
    # unchanged while a real deployment still runs policy/taint/HITL/hooks.
    async def _authorize_and_prepare(ws, agent_id, tool_name, args,
                                     chat_id=None, user_id=None, **kw):
        return PreparedDispatch(args=args, stream_params=dict(args),
                                cap_job_id=None, delegation_token=None)

    fake = types.SimpleNamespace(
        workspace=WorkspaceManager(history),
        history=history,
        _ws_active_chat={},
        _ws_timeline_mode={},
        _workspace_locks={},
        ui_clients=[],
        ui_sessions={},
        rote=ROTE(),
        security_flags=security_flags or {},
        tool_permissions=types.SimpleNamespace(is_tool_allowed=lambda u, a, t: allowed),
        credential_manager=types.SimpleNamespace(
            get_agent_credentials_encrypted=lambda u, a: None),
        _get_user_id=lambda ws: user_id,
        _safe_send=_safe_send,
        send_ui_render=send_ui_render,
        _execute_with_retry=_execute_with_retry,
        _execute_with_retry_audited=_execute_with_retry_audited,
        _authorize_and_prepare=_authorize_and_prepare,
    )
    for name in ("_send_or_replace_components", "send_ui_upsert",
                 "_handle_component_action", "_audit_workspace_denial",
                 "_component_action_allowed"):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._sent = sent
    fake._renders = renders
    fake._exec_calls = exec_calls
    return fake


def _seed_component(workspace, chat_id, user_id, *, agent="agent-x",
                    tool="list_patients", params=None, title="Patients",
                    rows=None):
    comp = {
        "type": "table",
        "title": title,
        "headers": ["Name"],
        "rows": rows if rows is not None else [["Alice"]],
        "_source_agent": agent,
        "_source_tool": tool,
        "_source_params": params if params is not None else {"page": 1},
    }
    ops = workspace.upsert(chat_id, user_id, [comp])
    assert len(ops) == 1
    return ops[0]["component_id"]


def _run(coro):
    """asyncio.run + a few zero-sleeps so fire-and-forget audit tasks
    (asyncio.create_task in _send_or_replace_components) complete."""
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapper())


def _alerts(fake, target="chat"):
    """All alert dicts sent through send_ui_render to the given target."""
    out = []
    for _, comps, tgt in fake._renders:
        if tgt == target:
            out.extend(c for c in comps if isinstance(c, dict) and c.get("type") == "alert")
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_component_action_happy_path(chat_env, audit_events):
    """028 FR-034/FR-035/FR-038/FR-039: a deterministic refresh re-executes the
    component's source capability with merged params, pins the result onto the
    ORIGINAL component identity, pushes a ui_upsert, snapshots the workspace
    with cause='component_action', and finishes with chat_status done."""
    history, user_id, chat_id = chat_env
    exec_result = types.SimpleNamespace(
        ui_components=[{"type": "table", "title": "Patients",
                        "headers": ["Name"], "rows": [["Bob"]]}],
        error=None,
    )
    fake = _make_fake(history, user_id, exec_result=exec_result)
    cid = _seed_component(fake.workspace, chat_id, user_id,
                          params={"page": 1, "filter": "all"})
    ws = _FakeWS("origin")

    _run(fake._handle_component_action(ws, user_id, {
        "chat_id": chat_id, "component_id": cid,
        "kind": "refresh", "params_patch": {"page": 2},
    }))

    # Source capability re-executed exactly once with merged params.
    assert len(fake._exec_calls) == 1
    agent_id, tool_name, args = fake._exec_calls[0]
    assert agent_id == "agent-x"
    assert tool_name == "list_patients"
    assert args == {"page": 2, "filter": "all"}

    # Result inherited the ORIGINAL component_id (force pin) — same row,
    # updated content + provenance, no duplicate row appended.
    rows = fake.workspace.live_rows(chat_id, user_id)
    assert len(rows) == 1
    row = fake.workspace.get_by_component_id(chat_id, user_id, cid)
    assert row is not None
    assert row["component_data"]["rows"] == [["Bob"]]
    assert row["component_data"]["_source_params"] == {"page": 2, "filter": "all"}
    assert row["component_data"]["component_id"] == cid

    # ui_upsert reached the socket recorder, dual shape (component + html).
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 1
    assert upserts[0]["chat_id"] == chat_id
    op = upserts[0]["ops"][0]
    assert op["op"] == "upsert"
    assert op["component_id"] == cid
    assert op["component"]["rows"] == [["Bob"]]
    assert op["html"] and f'data-component-id="{cid}"' in op["html"]

    # Workspace snapshot recorded with cause='component_action' (FR-039).
    snaps = history.plane_runtime.fetch_all(
        "SELECT * FROM workspace_snapshot WHERE chat_id = ? AND user_id = ? "
        "AND cause = 'component_action'", (chat_id, user_id))
    assert len(snaps) == 1
    snap_components = json.loads(snaps[0]["components"])
    assert any(c.get("component_id") == cid for c in snap_components)

    # Mutation audited as an in-place update; final chat_status done sent.
    updated = [e for e in audit_events if e.get("action") == "component_updated"]
    assert updated and updated[0]["component_id"] == cid
    statuses = [m for _, m in fake._sent if m["type"] == "chat_status"]
    assert statuses and statuses[-1]["status"] == "done"


# ---------------------------------------------------------------------------
# Denials (FR-036)
# ---------------------------------------------------------------------------

def test_component_action_permission_denied(chat_env, audit_events):
    """028 FR-036: a per-user tool-permission denial blocks execution, surfaces
    a user-visible chat-target error, and is audited as action_denied."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, allowed=False,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS()

    _run(fake._handle_component_action(ws, user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    # No execution happened.
    assert fake._exec_calls == []
    # Chat-target error render with the deny reason.
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "error"
    assert "Action not permitted" in alerts[0]["message"]
    assert "permissions" in alerts[0]["message"]
    # Denial audited with outcome=failure and the reason in detail.
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["outcome"] == "failure"
    assert denials[0]["chat_id"] == chat_id
    assert denials[0]["component_id"] == cid
    assert "permissions" in denials[0]["detail"]["reason"]
    # Workspace untouched — no upsert, no snapshot.
    assert [m for _, m in fake._sent if m["type"] == "ui_upsert"] == []
    snaps = history.plane_runtime.fetch_all(
        "SELECT id FROM workspace_snapshot WHERE chat_id = ? AND cause = 'component_action'",
        (chat_id,))
    assert snaps == ()


def test_component_action_security_flag_block(chat_env, audit_events):
    """028 FR-036: a security-review block on the source tool denies the
    action exactly like the chat path (the pre-028 table_paginate skipped
    this gate)."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(
        history, user_id, allowed=True,
        security_flags={"agent-x": {"list_patients": {"blocked": True}}},
        exec_result=types.SimpleNamespace(ui_components=[], error=None),
    )
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    assert fake._exec_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "error"
    assert "blocked by a security review" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert "security review" in denials[0]["detail"]["reason"]


# ---------------------------------------------------------------------------
# Graceful failure modes (FR-037)
# ---------------------------------------------------------------------------

def test_component_action_missing_component_id(chat_env, audit_events):
    """028 FR-034/FR-037: an action without a component identity is refused
    gracefully (chat-target alert), with no execution and no crash."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "kind": "refresh",  # no component_id
    }))

    assert fake._exec_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "error"
    assert "missing its component context" in alerts[0]["message"]
    assert audit_events == []  # nothing to audit — no identity, no denial event


def test_component_action_unknown_component(chat_env, audit_events):
    """028 FR-037: a stale/unknown target component produces a graceful,
    user-visible warning instead of a crash or silent no-op."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": "wc_deadbeefdeadbeef", "kind": "refresh",
    }))

    assert fake._exec_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "warning"
    assert "no longer available" in alerts[0]["message"]


# ---------------------------------------------------------------------------
# Timeline guard (FR-031)
# ---------------------------------------------------------------------------

def test_component_action_refused_in_timeline_mode(chat_env, audit_events):
    """028 FR-031: historical workspace views are strictly read-only —
    mutating component actions are refused, user-visibly, and audited."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS("timeline")
    fake._ws_timeline_mode[id(ws)] = True

    _run(fake._handle_component_action(ws, user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    assert fake._exec_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "warning"
    assert "past workspace state" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["detail"]["reason"] == "timeline_readonly"


# ---------------------------------------------------------------------------
# Cross-component targeting (FR-037)
# ---------------------------------------------------------------------------

def test_component_action_cross_component_target(chat_env, audit_events):
    """028 FR-037: a deterministic action may target a DIFFERENT component —
    the emitter's provenance is re-executed but the result lands on the
    target's identity (its row updated in place), leaving the emitter
    untouched."""
    history, user_id, chat_id = chat_env
    exec_result = types.SimpleNamespace(
        ui_components=[{"type": "table", "title": "Linked",
                        "headers": ["Name"], "rows": [["Carol"]]}],
        error=None,
    )
    fake = _make_fake(history, user_id, exec_result=exec_result)
    cid_a = _seed_component(fake.workspace, chat_id, user_id,
                            agent="agent-x", tool="list_patients",
                            params={"page": 1}, title="Patients")
    cid_b = _seed_component(fake.workspace, chat_id, user_id,
                            agent="agent-y", tool="graph_ages",
                            params={"bucket": 10}, title="Ages",
                            rows=[["old-b"]])
    assert cid_a != cid_b

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid_a,
        "target_component_id": cid_b, "kind": "refresh",
        "params_patch": {"page": 3},
    }))

    # Execution used the EMITTER's (A's) provenance.
    assert len(fake._exec_calls) == 1
    agent_id, tool_name, args = fake._exec_calls[0]
    assert (agent_id, tool_name) == ("agent-x", "list_patients")
    assert args == {"page": 3}

    # Result landed on B's identity: B's row updated in place …
    row_b = fake.workspace.get_by_component_id(chat_id, user_id, cid_b)
    assert row_b["component_data"]["rows"] == [["Carol"]]
    assert row_b["component_data"]["component_id"] == cid_b
    # … while A is untouched and no third row appeared.
    row_a = fake.workspace.get_by_component_id(chat_id, user_id, cid_a)
    assert row_a["component_data"]["rows"] == [["Alice"]]
    assert len(fake.workspace.live_rows(chat_id, user_id)) == 2

    # The ui_upsert addressed the TARGET's identity.
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert upserts and upserts[0]["ops"][0]["component_id"] == cid_b


# ---------------------------------------------------------------------------
# Fan-out (FR-040)
# ---------------------------------------------------------------------------

def test_send_ui_upsert_fans_out_to_same_chat_sockets_only(chat_env):
    """028 FR-040: a workspace change propagates to every socket of the user
    viewing the SAME chat (each with the dual component+html shape); sockets
    on a different chat receive nothing."""
    history, user_id, chat_id = chat_env
    other_chat = history.create_chat(user_id=user_id)
    try:
        fake = _make_fake(history, user_id)
        ws1, ws2, ws3 = _FakeWS("a"), _FakeWS("b"), _FakeWS("other")
        fake.ui_clients = [ws1, ws2, ws3]
        fake._ws_active_chat = {id(ws1): chat_id, id(ws2): chat_id,
                                id(ws3): other_chat}

        ops = fake.workspace.upsert(chat_id, user_id, [{
            "type": "alert", "message": "fan-out check", "variant": "info",
            "_source_agent": "agent-x", "_source_tool": "list_patients",
            "_source_params": {"page": 9},
        }])
        assert len(ops) == 1
        cid = ops[0]["component_id"]

        asyncio.run(fake.send_ui_upsert(ws1, chat_id, user_id, ops))

        by_ws = {}
        for ws, msg in fake._sent:
            by_ws.setdefault(id(ws), []).append(msg)

        for ws in (ws1, ws2):
            msgs = by_ws.get(id(ws), [])
            upserts = [m for m in msgs if m["type"] == "ui_upsert"]
            assert len(upserts) == 1, f"socket {ws.label} missed the upsert"
            op = upserts[0]["ops"][0]
            assert op["component_id"] == cid
            assert op["component"]["message"] == "fan-out check"
            assert op["html"] and f'data-component-id="{cid}"' in op["html"]
        # The socket on a DIFFERENT chat got nothing at all.
        assert id(ws3) not in by_ws
        # Exactly one delivery per same-chat socket (origin not double-sent).
        assert len(fake._sent) == 2
    finally:
        history.delete_chat(other_chat, user_id=user_id)
