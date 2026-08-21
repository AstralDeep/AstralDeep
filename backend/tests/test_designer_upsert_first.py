"""Feature 052 (T029) — designer delivery is upsert-first on the web path.

Drives Orchestrator._deliver_round_components with a real workspace/DB and a
patched ui_designer.design_round: the flat ui_upsert always reaches the
client BEFORE any designed ui_render; the designed render still arrives as a
later in-place refinement on success; a designer crash leaves exactly the
upsert (fail-open, FR-013/FR-014); and a designed render is never forced to
a socket that switched chats mid-design (stale-chat guard).
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://fake.api")
os.environ.setdefault("LLM_MODEL", "test-model")

pytestmark = pytest.mark.asyncio


def _fresh_socket():
    """A VirtualWebSocket capturing every delivered frame."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
    return VirtualWebSocket(task)


def _round_components():
    """Two rich components — enough to trigger the designer path."""
    return [
        {"type": "card", "title": "Alpha", "content": [
            {"type": "text", "content": "first"}]},
        {"type": "card", "title": "Beta", "content": [
            {"type": "text", "content": "second"}]},
    ]


def _frame_types(ws):
    return [f.get("type") for f in ws.task.outputs]


def _canvas_renders(ws):
    return [f for f in ws.task.outputs
            if f.get("type") == "ui_render" and f.get("target") != "chat"]


@pytest.fixture()
async def env(monkeypatch):
    """A real Orchestrator + registered socket + fresh chat for one user."""
    monkeypatch.setenv("FF_UI_DESIGNER", "true")
    from orchestrator.orchestrator import Orchestrator
    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    user_id = f"designer-test-{uuid.uuid4().hex[:8]}"
    ws = _fresh_socket()
    orch.ui_sessions[ws] = {"sub": user_id}
    orch.ui_clients.append(ws)
    orch.rote.register_device(ws, {})
    chat_id = await asyncio.to_thread(
        orch.history.create_chat,
        user_id=user_id,
    )
    orch._ws_active_chat[id(ws)] = chat_id
    try:
        yield orch, ws, chat_id, user_id
    finally:
        try:
            await asyncio.to_thread(
                orch.history.delete_chat,
                chat_id,
                user_id=user_id,
            )
        except Exception:
            pass
        await orch._close_started_services()


def _ref_layout_from_rows(canvas_rows):
    """A minimal valid layout: one container of refs to the live components."""
    refs = [{"type": "ref", "component_id": r["component_id"]}
            for r in canvas_rows if r.get("component_id")]
    return [{"type": "container", "content": refs}]


async def test_upsert_precedes_designed_render(env, monkeypatch):
    """Frame order: ui_upsert first, designed ui_render afterwards."""
    orch, ws, chat_id, user_id = env
    from orchestrator import ui_designer

    async def _fake_design(**kwargs):
        assert ws.task.outputs, "design must start only after delivery began"
        assert _frame_types(ws).count("ui_upsert") == 1, \
            "the flat ui_upsert must be on the wire before any design pass"
        return _ref_layout_from_rows(kwargs["canvas_rows"])

    monkeypatch.setattr(ui_designer, "design_round", _fake_design)
    ops = await orch._deliver_round_components(
        ws, _round_components(), chat_id, user_id, user_request="dashboard")
    assert ops, "ops must be returned for the turn snapshot"

    types = _frame_types(ws)
    assert "ui_upsert" in types
    renders = _canvas_renders(ws)
    assert renders, "the designed full-canvas ui_render must still arrive"
    assert types.index("ui_upsert") < types.index("ui_render")


async def test_designed_render_preserves_component_identity(env, monkeypatch):
    """The refinement re-renders the same persisted component identities."""
    orch, ws, chat_id, user_id = env
    from orchestrator import ui_designer

    async def _fake_design(**kwargs):
        return _ref_layout_from_rows(kwargs["canvas_rows"])

    monkeypatch.setattr(ui_designer, "design_round", _fake_design)
    ops = await orch._deliver_round_components(
        ws, _round_components(), chat_id, user_id, user_request="dashboard")
    cids = {op["component_id"] for op in ops}
    render = _canvas_renders(ws)[-1]
    html = render.get("html") or ""
    for cid in cids:
        assert cid in html, f"designed render lost component identity {cid}"


async def test_designer_failure_leaves_exactly_the_upsert(env, monkeypatch):
    """A designer crash means the refinement never arrives — nothing else."""
    orch, ws, chat_id, user_id = env
    from orchestrator import ui_designer

    async def _boom(**kwargs):
        raise RuntimeError("designer exploded")

    monkeypatch.setattr(ui_designer, "design_round", _boom)
    ops = await orch._deliver_round_components(
        ws, _round_components(), chat_id, user_id, user_request="dashboard")
    assert ops, "persistence must survive a designer failure"
    types = _frame_types(ws)
    assert types.count("ui_upsert") == 1
    assert not _canvas_renders(ws), "no designed render after a failure"


async def test_stale_chat_drops_the_designed_push(env, monkeypatch):
    """A socket that left the chat mid-design never gets the refinement."""
    orch, ws, chat_id, user_id = env
    from orchestrator import ui_designer

    async def _fake_design(**kwargs):
        orch._ws_active_chat[id(ws)] = "some-other-chat"
        return _ref_layout_from_rows(kwargs["canvas_rows"])

    monkeypatch.setattr(ui_designer, "design_round", _fake_design)
    await orch._deliver_round_components(
        ws, _round_components(), chat_id, user_id, user_request="dashboard")
    assert "ui_upsert" in _frame_types(ws)
    assert not _canvas_renders(ws), \
        "designed render must not be forced onto a socket that changed chats"


async def test_designer_status_updates_do_not_block_upsert(env, monkeypatch):
    """Even a slow design pass cannot delay the already-sent components."""
    orch, ws, chat_id, user_id = env
    from orchestrator import ui_designer
    upsert_seen_at = {}

    async def _slow_design(**kwargs):
        upsert_seen_at["count"] = _frame_types(ws).count("ui_upsert")
        await asyncio.sleep(0.2)
        return None

    monkeypatch.setattr(ui_designer, "design_round", _slow_design)
    await orch._deliver_round_components(
        ws, _round_components(), chat_id, user_id, user_request="dashboard")
    assert upsert_seen_at.get("count") == 1
