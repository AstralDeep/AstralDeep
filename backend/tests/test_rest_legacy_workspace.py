"""Tests that the deprecated REST component/chat endpoints (orchestrator/api.py,
workspace.py) keep the persistent workspace coherent: save/delete/combine/condense
route through WorkspaceManager, and deletion notifies other tabs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import uuid

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import api as rest_api
from orchestrator.models import (
    ComponentCombineRequest,
    ComponentCondenseRequest,
    ComponentSaveRequest,
)
from orchestrator.workspace import WorkspaceManager
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_plane_runtime,
)


class _FakeWS:
    def __init__(self, label: str = ""):
        self.label = label


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("rest_legacy_workspace") as runtime:
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


def _make_fake(history, default_user_id, *, user_map=None):
    upserts = []
    sent = []
    reconciles = []
    llm_calls = []
    user_map = user_map or {}

    async def send_ui_upsert(websocket, chat_id, user_id, ops):
        upserts.append((websocket, chat_id, user_id, ops))

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def _reconcile_legacy_replacement(websocket, chat_id, user_id, *, cause):
        reconciles.append((websocket, chat_id, user_id, cause))

    async def _combine_components_llm(components, mode="combine"):
        llm_calls.append((components, mode))
        return {"components": [{
            "component_data": {"type": "card", "title": f"Merged ({mode})"},
            "component_type": "card",
            "title": f"Merged ({mode})",
        }]}

    fake = types.SimpleNamespace(
        history=history,
        workspace=WorkspaceManager(history),
        ui_clients=[],
        _ws_active_chat={},
        _ws_timeline_mode={},
        _get_user_id=lambda ws: user_map.get(id(ws), default_user_id),
        _safe_send=_safe_send,
        send_ui_upsert=send_ui_upsert,
        _reconcile_legacy_replacement=_reconcile_legacy_replacement,
        _combine_components_llm=_combine_components_llm,
    )
    fake._upserts = upserts
    fake._sent = sent
    fake._reconciles = reconciles
    fake._llm_calls = llm_calls
    return fake


def _fake_request(orch):
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(orchestrator=orch))
    )


def _run(coro):
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapper())


def _seed_workspace_component(workspace, chat_id, user_id, *, title="Patients"):
    ops = workspace.upsert(chat_id, user_id, [{
        "type": "table", "title": title, "headers": ["Name"], "rows": [["Alice"]],
        "_source_agent": "agent-x", "_source_tool": "list_patients",
        "_source_params": {"page": 1},
    }])
    assert len(ops) == 1
    return ops[0]["component_id"]


def test_rest_save_component_dict_gets_workspace_identity_and_upsert(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)

    body = ComponentSaveRequest(
        component_data={
            "type": "card", "title": "Vitals",
            "_source_agent": "agent-x", "_source_tool": "get_vitals",
            "_source_params": {"patient": "p1"},
        },
        component_type="card",
        title="Vitals",
    )
    resp = _run(rest_api.save_component(
        _fake_request(fake), chat_id, body, user_id=user_id))

    rows = fake.workspace.live_rows(chat_id, user_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["component_id"], "saved_components row must carry component_id"
    assert row["component_data"]["component_id"] == row["component_id"]
    assert resp.component.id == row["id"]
    assert resp.component.chat_id == chat_id

    assert len(fake._upserts) == 1
    ws, up_chat, up_user, ops = fake._upserts[0]
    assert ws is None
    assert (up_chat, up_user) == (chat_id, user_id)
    assert len(ops) == 1
    assert ops[0]["op"] == "upsert"
    assert ops[0]["component_id"] == row["component_id"]
    assert ops[0]["component"]["title"] == "Vitals"


def test_rest_delete_component_removes_workspace_identity_everywhere(
        chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    ws_component_id = _seed_workspace_component(fake.workspace, chat_id, user_id)
    row = fake.workspace.get_by_component_id(chat_id, user_id, ws_component_id)
    row_id = row["id"]

    resp = _run(rest_api.delete_component(
        _fake_request(fake), row_id, user_id=user_id))
    assert resp.success is True

    assert history.get_component_by_id(row_id, user_id=user_id) is None

    assert len(fake._upserts) == 1
    ws, up_chat, up_user, ops = fake._upserts[0]
    assert ws is None
    assert (up_chat, up_user) == (chat_id, user_id)
    assert ops == [{"op": "remove", "component_id": ws_component_id}]

    snaps = history.plane_runtime.fetch_all(
        "SELECT * FROM workspace_snapshot WHERE chat_id = ? AND user_id = ? "
        "AND cause = 'remove'", (chat_id, user_id))
    assert len(snaps) == 1
    assert json.loads(snaps[0]["components"]) == []

    removed = [e for e in audit_events if e.get("action") == "component_removed"]
    assert len(removed) == 1
    assert removed[0]["chat_id"] == chat_id
    assert removed[0]["component_id"] == ws_component_id
    assert removed[0]["user_id"] == user_id


def test_rest_delete_component_404_when_absent(chat_env, audit_events):
    history, user_id, _chat_id = chat_env
    fake = _make_fake(history, user_id)

    with pytest.raises(HTTPException) as exc_info:
        _run(rest_api.delete_component(
            _fake_request(fake), f"missing-{uuid.uuid4()}", user_id=user_id))
    assert exc_info.value.status_code == 404
    assert fake._upserts == []
    assert audit_events == []


def test_rest_combine_reconciles_with_cause_combine(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    src_id = history.save_component(
        chat_id, {"type": "card", "title": "A"}, "card", "A", user_id=user_id)
    tgt_id = history.save_component(
        chat_id, {"type": "card", "title": "B"}, "card", "B", user_id=user_id)

    resp = _run(rest_api.combine_components(
        _fake_request(fake),
        ComponentCombineRequest(source_id=src_id, target_id=tgt_id),
        user_id=user_id))

    assert len(fake._llm_calls) == 1
    llm_components, mode = fake._llm_calls[0]
    assert mode == "combine"
    assert len({c["id"] for c in llm_components}) == 2
    assert {c["component_data"]["title"] for c in llm_components} == {"A", "B"}

    assert history.get_component_by_id(src_id, user_id=user_id) is None
    assert history.get_component_by_id(tgt_id, user_id=user_id) is None
    assert resp.removed_ids == [src_id, tgt_id]
    assert len(resp.new_components) == 1
    assert history.get_component_by_id(
        resp.new_components[0].id, user_id=user_id) is not None

    assert fake._reconciles == []
    assert any(
        item["cause"] == "combine"
        for item in fake.workspace.list_snapshots(chat_id, user_id)
    )


def test_rest_condense_reconciles_with_cause_condense(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    ids = [
        history.save_component(
            chat_id, {"type": "card", "title": f"C{i}"}, "card", f"C{i}",
            user_id=user_id)
        for i in range(3)
    ]

    resp = _run(rest_api.condense_components(
        _fake_request(fake),
        ComponentCondenseRequest(component_ids=ids),
        user_id=user_id))

    assert len(fake._llm_calls) == 1
    llm_components, mode = fake._llm_calls[0]
    assert mode == "condense"
    assert len({c["id"] for c in llm_components}) == len(ids)
    assert {c["component_data"]["title"] for c in llm_components} == {
        "C0", "C1", "C2"
    }

    for old_id in ids:
        assert history.get_component_by_id(old_id, user_id=user_id) is None
    assert resp.removed_ids == ids

    assert fake._reconciles == []
    assert any(
        item["cause"] == "condense"
        for item in fake.workspace.list_snapshots(chat_id, user_id)
    )


def test_rest_delete_chat_ends_timeline_view_and_notifies_sockets(chat_env):
    history, user_id, chat_id = chat_env
    other_chat = history.create_chat(user_id=user_id)
    other_user = f"test-user-{uuid.uuid4()}"
    try:
        ws_timeline = _FakeWS("timeline-tab")
        ws_plain = _FakeWS("plain-tab")
        ws_other_chat = _FakeWS("other-chat-tab")
        ws_other_user = _FakeWS("other-user-tab")
        fake = _make_fake(history, user_id,
                          user_map={id(ws_other_user): other_user})
        fake.ui_clients = [ws_timeline, ws_plain, ws_other_chat, ws_other_user]
        fake._ws_active_chat = {
            id(ws_timeline): chat_id,
            id(ws_plain): chat_id,
            id(ws_other_chat): other_chat,
            id(ws_other_user): chat_id,
        }
        fake._ws_timeline_mode = {id(ws_timeline): 5}

        resp = _run(rest_api.delete_chat(
            _fake_request(fake), chat_id, user_id=user_id))
        assert resp.success is True

        assert not history.get_chat(chat_id, user_id=user_id)

        by_ws = {}
        for ws, msg in fake._sent:
            by_ws.setdefault(id(ws), []).append(msg)

        timeline_msgs = by_ws.get(id(ws_timeline), [])
        assert timeline_msgs == [
            {"type": "workspace_timeline_mode", "active": False},
            {"type": "chat_deleted", "chat_id": chat_id},
        ]
        assert by_ws.get(id(ws_plain), []) == [
            {"type": "chat_deleted", "chat_id": chat_id},
        ]
        assert id(ws_other_chat) not in by_ws
        assert id(ws_other_user) not in by_ws

        assert id(ws_timeline) not in fake._ws_active_chat
        assert id(ws_timeline) not in fake._ws_timeline_mode
        assert id(ws_plain) not in fake._ws_active_chat
        assert fake._ws_active_chat.get(id(ws_other_chat)) == other_chat
        assert fake._ws_active_chat.get(id(ws_other_user)) == chat_id
    finally:
        history.delete_chat(other_chat, user_id=user_id)
