"""Tests for deterministic component actions (backend/orchestrator/orchestrator.py,
workspace.py): re-execution pinned to the original identity, ui_upsert fan-out,
permission/security denial audit, and the read-only timeline guard.
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
    def __init__(self, label: str = ""):
        self.label = label


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("component_action") as runtime:
        yield runtime


@pytest.fixture
def chat_env(plane_runtime):
    history = history_manager(plane_runtime)
    user_id = f"test-user-{uuid.uuid4()}"
    chat_id = history.create_chat(user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


@pytest.fixture
def audit_events(monkeypatch):
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_workspace_event", _record)
    return events


def _make_fake(history, user_id, *, allowed=True, security_flags=None, exec_result=None):
    from rote.rote import ROTE

    sent = []
    renders = []
    exec_calls = []

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
        del chat_id, user_id
        return await fake._execute_with_retry(ws, agent_id, tool_name, args)

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
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapper())


def _alerts(fake, target="chat"):
    out = []
    for _, comps, tgt in fake._renders:
        if tgt == target:
            out.extend(c for c in comps if isinstance(c, dict) and c.get("type") == "alert")
    return out


def test_component_action_happy_path(chat_env, audit_events):
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

    assert len(fake._exec_calls) == 1
    agent_id, tool_name, args = fake._exec_calls[0]
    assert agent_id == "agent-x"
    assert tool_name == "list_patients"
    assert args == {"page": 2, "filter": "all"}

    rows = fake.workspace.live_rows(chat_id, user_id)
    assert len(rows) == 1
    row = fake.workspace.get_by_component_id(chat_id, user_id, cid)
    assert row is not None
    assert row["component_data"]["rows"] == [["Bob"]]
    assert row["component_data"]["_source_params"] == {"page": 2, "filter": "all"}
    assert row["component_data"]["component_id"] == cid

    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 1
    assert upserts[0]["chat_id"] == chat_id
    op = upserts[0]["ops"][0]
    assert op["op"] == "upsert"
    assert op["component_id"] == cid
    assert op["component"]["rows"] == [["Bob"]]
    assert op["html"] and f'data-component-id="{cid}"' in op["html"]

    snaps = history.plane_runtime.fetch_all(
        "SELECT * FROM workspace_snapshot WHERE chat_id = ? AND user_id = ? "
        "AND cause = 'component_action'", (chat_id, user_id))
    assert len(snaps) == 1
    snap_components = json.loads(snaps[0]["components"])
    assert any(c.get("component_id") == cid for c in snap_components)

    updated = [e for e in audit_events if e.get("action") == "component_updated"]
    assert updated and updated[0]["component_id"] == cid
    statuses = [m for _, m in fake._sent if m["type"] == "chat_status"]
    assert statuses and statuses[-1]["status"] == "done"


def test_component_action_permission_denied(chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, allowed=False,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS()

    _run(fake._handle_component_action(ws, user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    assert fake._exec_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "error"
    assert "Action not permitted" in alerts[0]["message"]
    assert "permissions" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["outcome"] == "failure"
    assert denials[0]["chat_id"] == chat_id
    assert denials[0]["component_id"] == cid
    assert "permissions" in denials[0]["detail"]["reason"]
    assert [m for _, m in fake._sent if m["type"] == "ui_upsert"] == []
    snaps = history.plane_runtime.fetch_all(
        "SELECT id FROM workspace_snapshot WHERE chat_id = ? AND cause = 'component_action'",
        (chat_id,))
    assert snaps == ()


def test_component_action_security_flag_block(chat_env, audit_events):
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


def test_component_action_missing_component_id(chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "kind": "refresh",
    }))

    assert fake._exec_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "error"
    assert "missing its component context" in alerts[0]["message"]
    assert audit_events == []


def test_component_action_unknown_component(chat_env, audit_events):
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


def test_component_action_refused_in_timeline_mode(chat_env, audit_events):
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


def test_component_action_cross_component_target(chat_env, audit_events):
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

    assert len(fake._exec_calls) == 1
    agent_id, tool_name, args = fake._exec_calls[0]
    assert (agent_id, tool_name) == ("agent-x", "list_patients")
    assert args == {"page": 3}

    row_b = fake.workspace.get_by_component_id(chat_id, user_id, cid_b)
    assert row_b["component_data"]["rows"] == [["Carol"]]
    assert row_b["component_data"]["component_id"] == cid_b
    row_a = fake.workspace.get_by_component_id(chat_id, user_id, cid_a)
    assert row_a["component_data"]["rows"] == [["Alice"]]
    assert len(fake.workspace.live_rows(chat_id, user_id)) == 2

    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert upserts and upserts[0]["ops"][0]["component_id"] == cid_b


def test_send_ui_upsert_fans_out_to_same_chat_sockets_only(chat_env):
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
        assert id(ws3) not in by_ws
        assert len(fake._sent) == 2
    finally:
        history.delete_chat(other_chat, user_id=user_id)
