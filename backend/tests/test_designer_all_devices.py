"""055-uniform-artifacts US3 (T028) — designed canvases for native-origin turns.

With FF_DESIGNER_ALL_DEVICES on, a native-origin turn gets ONE coalesced
designer pass inline in the turn handler AFTER the terminal ``chat_status
done`` — the arrangement persists to ``workspace_layout`` and the materialized
canvas arrives as an out-of-turn full ``ui_render`` (doc/Reasoning-filtered,
never spoken, progress frames suppressed). The push carries the turn marker
and is dropped when a newer turn has started on the chat. Flag off restores
the 052 native skip exactly: no designer call, no layout row, no post-done
render.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.feature_flags import flags  # noqa: E402

pytestmark = pytest.mark.asyncio

AGENT = "dash-1"
TOOL = "make_dashboard"


def _fresh_socket():
    """A VirtualWebSocket capturing every delivered frame (the async-mode
    execution socket — also how natives are simulated frame-for-frame)."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
    return VirtualWebSocket(task)


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content,
                           tool_calls=tool_calls, reasoning_content=None)


def _tc(name=TOOL, cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments="{}"))


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


def _rich_components():
    """Two rich components — enough to trigger the coalesced designer pass."""
    return [
        {"type": "table", "title": "Alpha", "headers": ["A"], "rows": [["1"]]},
        {"type": "metric", "title": "Beta", "value": "42"},
    ]


def _frames(ws):
    return ws.task.outputs


def _canvas_renders(ws):
    return [f for f in _frames(ws)
            if f.get("type") == "ui_render" and f.get("target") == "canvas"]


def _last_done_index(ws):
    idx = [i for i, f in enumerate(_frames(ws))
           if f.get("type") == "chat_status" and f.get("status") == "done"]
    assert idx, "the turn must close with a chat_status done"
    return idx[-1]


@pytest.fixture()
async def env(monkeypatch):
    """A real Orchestrator + a native-profile VirtualWebSocket on a fresh chat."""
    monkeypatch.setenv("FF_UI_DESIGNER", "true")
    # This suite owns designer delivery, not compatibility TaskManager
    # admission.  Isolate it from persistent queue state in a shared live
    # development database while the async-manager test below still exercises
    # that manager's public submit/watcher path.
    monkeypatch.setitem(flags._flags, "task_state_machine", False)
    for mod in ("agentic_creation", "scheduling_chat", "memory_chat",
                "desktop_codegen"):
        monkeypatch.setattr(f"orchestrator.{mod}.should_inject",
                            lambda draft_agent_id: False)
    from orchestrator.orchestrator import Orchestrator
    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    orch.audit_recorder = MagicMock()
    orch.audit_recorder.record = AsyncMock()
    orch._record_llm_call = AsyncMock()
    orch._record_llm_unconfigured = AsyncMock()
    orch._resolve_llm_client_for = AsyncMock(return_value=MagicMock())
    orch._emit_llm_usage_report = AsyncMock()
    hb = MagicMock()
    hb.cancel = MagicMock()
    orch._start_heartbeat = AsyncMock(return_value=hb)

    from shared.protocol import AgentCard, AgentSkill
    orch.agent_cards[AGENT] = AgentCard(
        name="dash", description="d", agent_id=AGENT,
        skills=[AgentSkill(name="dash", description="s", id=TOOL,
                           input_schema={"type": "object"})])
    orch.agents[AGENT] = MagicMock()
    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed.return_value = True
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"ok": True}, error=None, ui_components=_rich_components(),
        correlation_id=None))

    user_id = f"dad-test-{uuid.uuid4().hex[:8]}"
    ws = _fresh_socket()
    orch.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    orch.ui_clients.append(ws)
    orch.rote.register_device(ws, {"device_type": "android"})
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


def _install_llm(orch, final_text="All set."):
    """Tool round then final text; designer calls are answered separately."""
    features = []
    state = {"n": 0}

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        features.append(feature)
        if feature == "ui_designer":
            return _msg(content="DONE"), _usage()
        state["n"] += 1
        if state["n"] == 1:
            return _msg(tool_calls=[_tc()]), _usage()
        return _msg(content=final_text), _usage()

    orch._call_llm = fake_llm
    return features


def _install_designer(monkeypatch, side_effect=None):
    """Deterministic design_round: exercises llm_call once (the progress-
    suppression seam), then returns a container of refs to the round."""
    from orchestrator import ui_designer
    calls = []

    async def _fake_design(**kwargs):
        calls.append(kwargs)
        await kwargs["llm_call"]([{"role": "user", "content": "arrange"}])
        if side_effect is not None:
            await side_effect()
        refs = [{"type": "ref", "component_id": c["component_id"]}
                for c in kwargs["round_components"] if c.get("component_id")]
        return [{"type": "container", "content": refs}]

    monkeypatch.setattr(ui_designer, "design_round", _fake_design)
    return calls


async def test_native_turn_persists_layout_and_renders_after_done(env, monkeypatch):
    """Layout persisted; frame order upsert → done → designed ui_render;
    zero chat_status frames after done (progress suppressed)."""
    orch, ws, chat_id, user_id = env
    monkeypatch.setitem(flags._flags, "designer_all_devices", True)
    features = _install_llm(orch)
    design_calls = _install_designer(monkeypatch)

    await orch.handle_chat_message(ws, "make a dashboard", chat_id, user_id=user_id)

    assert design_calls, "the coalesced post-done pass must run for native origin"
    layouts = await asyncio.to_thread(orch.workspace.live_layouts, chat_id, user_id)
    assert len(layouts) == 1, "native-origin turn persists a workspace_layout row"

    types = [f.get("type") for f in _frames(ws)]
    assert "ui_upsert" in types, "flat per-round delivery unchanged (upsert-first)"
    i_done = _last_done_index(ws)
    renders = _canvas_renders(ws)
    assert len(renders) == 1, "exactly one designed full-canvas render"
    i_render = _frames(ws).index(renders[0])
    assert types.index("ui_upsert") < i_done < i_render, \
        "upsert → done → designed render ordering on the originating socket"
    assert all(f.get("type") != "chat_status" for f in _frames(ws)[i_done + 1:]), \
        "designer progress frames are suppressed after done"
    assert "ui_designer" in features, "designer LLM runs through _call_llm auditing"
    # Every round component appears in the materialized designed canvas.
    rendered = json.dumps(renders[0].get("components") or [])
    for lay_ref in ("Alpha", "Beta"):
        assert lay_ref in rendered


async def test_async_mode_render_sequences_before_task_completed(env, monkeypatch):
    """The designed push lands inside handle_chat_message, so async turns emit
    it before the manager's task_completed notification."""
    orch, ws, chat_id, user_id = env
    monkeypatch.setitem(flags._flags, "designer_all_devices", True)
    _install_llm(orch)
    _install_designer(monkeypatch)
    watcher_frames = []
    managed_socket = {}
    started = asyncio.Event()
    release = asyncio.Event()

    class _Watcher:
        # 055: watcher notification arrives via send_text (encoding fix —
        # send_json would double-encode over a real FastAPI socket).
        async def send_text(self, data):
            watcher_frames.append(
                (json.loads(data), len(_canvas_renders(managed_socket["ws"])))
            )

    async def _coro(vws):
        managed_socket["ws"] = vws
        orch.ui_sessions[vws] = {
            "sub": user_id,
            "preferred_username": user_id,
        }
        orch.ui_clients.append(vws)
        orch.rote.register_device(vws, {"device_type": "android"})
        orch._ws_active_chat[id(vws)] = chat_id
        started.set()
        await release.wait()
        await orch.handle_chat_message(vws, "make a dashboard", chat_id, user_id=user_id)

    task = await orch.async_task_manager.submit(chat_id, user_id, _coro)
    await asyncio.wait_for(started.wait(), timeout=5)
    task.watchers.append(_Watcher())
    release.set()
    assert task.asyncio_task is not None
    await asyncio.wait_for(asyncio.shield(task.asyncio_task), timeout=10)

    assert watcher_frames and watcher_frames[0][0]["type"] == "task_completed"
    assert watcher_frames[0][1] == 1, \
        "the designed render must be on the wire before task_completed"


async def test_stale_guard_drops_late_push(env, monkeypatch):
    """A newer durable turn wins after design but before a native push.

    Feature 060 publishes a normal turn atomically, so a second turn can no
    longer interleave inside ``handle_chat_message``.  Exercise the post-done
    helper boundary directly: persist the design against one exact message
    marker, advance the chat, then prove delivery is dropped.
    """
    orch, ws, chat_id, user_id = env
    monkeypatch.setitem(flags._flags, "designer_all_devices", True)
    _install_llm(orch)

    components = _rich_components()
    await orch._send_or_replace_components(
        ws,
        components,
        chat_id,
        user_id,
    )
    ws.task.outputs.clear()
    await asyncio.to_thread(
        orch.history.add_message,
        chat_id,
        "user",
        "make a dashboard",
        user_id=user_id,
    )
    turn_marker = str(
        await asyncio.to_thread(
            orch.history.get_latest_message_id,
            chat_id,
            user_id,
        )
    )

    async def _newer_turn():
        await asyncio.to_thread(orch.history.add_message, chat_id, "user",
                                "next question", user_id=user_id)

    _install_designer(monkeypatch, side_effect=_newer_turn)
    designed_marker = await orch._design_turn_post_done(
        ws,
        chat_id,
        user_id,
        "make a dashboard",
        components,
        turn_marker=turn_marker,
    )
    assert designed_marker == turn_marker
    await orch._push_designed_native_canvas(
        chat_id,
        user_id,
        ws,
        designed_marker,
    )

    assert await asyncio.to_thread(
        orch.workspace.live_layouts, chat_id, user_id), "layout still persists"
    assert not _canvas_renders(ws), "late designed push dropped by the turn marker"


async def test_flag_off_restores_native_skip(env, monkeypatch):
    """OFF = today's behavior: no designer call, no layout row, no render."""
    orch, ws, chat_id, user_id = env
    monkeypatch.setitem(flags._flags, "designer_all_devices", False)
    features = _install_llm(orch)
    design_calls = _install_designer(monkeypatch)

    await orch.handle_chat_message(ws, "make a dashboard", chat_id, user_id=user_id)

    assert not design_calls, "designer never invoked for native origin when off"
    assert "ui_designer" not in features
    assert await asyncio.to_thread(orch.workspace.live_layouts, chat_id, user_id) == []
    assert not _canvas_renders(ws)
    assert "ui_upsert" in [f.get("type") for f in _frames(ws)], \
        "flat delivery unchanged with the flag off"


async def test_watch_designed_render_carries_no_speech(env, monkeypatch):
    """speak=False threaded through the post-done push — re-presented content
    never fires the watch speech field."""
    orch, ws, chat_id, user_id = env
    monkeypatch.setitem(flags._flags, "designer_all_devices", True)
    orch.rote.register_device(ws, {"device_type": "watch"})
    _install_llm(orch)
    _install_designer(monkeypatch)

    await orch.handle_chat_message(ws, "make a dashboard", chat_id, user_id=user_id)

    renders = _canvas_renders(ws)
    assert renders, "watch still receives the designed canvas"
    assert all(not r.get("speech") for r in renders), \
        "designed push must not carry a spoken rendition"


async def test_doc_cards_excluded_from_native_canvas(env, monkeypatch):
    """A doc_ narrative card on the canvas never enters the native designed
    render (native reducers divert it to the chat rail)."""
    orch, ws, chat_id, user_id = env
    monkeypatch.setitem(flags._flags, "designer_all_devices", True)
    # A long final narrative promotes to a durable doc_ canvas card.
    long_text = "# Full report\n\n" + ("Detail line about the dashboard.\n" * 60)
    _install_llm(orch, final_text=long_text)
    design_calls = _install_designer(monkeypatch)

    await orch.handle_chat_message(ws, "make a dashboard", chat_id, user_id=user_id)

    assert design_calls
    designed_ids = {c.get("component_id")
                    for c in design_calls[0]["round_components"]}
    assert not any(str(i or "").startswith("doc_") for i in designed_ids), \
        "doc_ cards are filtered before the designer sees the turn"
    renders = _canvas_renders(ws)
    assert renders
    rendered = json.dumps(renders[-1].get("components") or [])
    assert "doc_" not in rendered, "materialized native canvas excludes doc_ cards"
