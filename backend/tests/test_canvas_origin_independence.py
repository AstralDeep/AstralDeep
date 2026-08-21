"""055-uniform-artifacts US3 (T032) — canvas origin independence.

The SAME multi-component tool turn driven once from a browser-profile socket
and once from an android-profile socket (fresh chats each) persists a
``workspace_layout`` row in BOTH chats with equal arrangement trees —
component identities are content-derived fingerprints (agent|tool|params),
so equality across chats is exact, not merely structural. The materialized
canvases are equivalent per profile capability (same identity sets, same
layout tree shape modulo profile degradation); only the delivery point
differs by contract (wire-contract §5): web receives the designed render
mid-turn, natives receive it once after ``chat_status done``.
"""
from __future__ import annotations

import asyncio
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
    """Two rich components — enough to trigger the designer on every origin."""
    return [
        {"type": "table", "title": "Alpha", "headers": ["A"], "rows": [["1"]]},
        {"type": "metric", "title": "Beta", "value": "42"},
    ]


def _expected_family():
    """The identity family both chats must mint: same agent/tool/params ⇒
    same fingerprint base, batch sibling gets the deterministic ordinal."""
    from orchestrator.workspace import fingerprint, ordinal_identity
    base = fingerprint(AGENT, TOOL, {})
    return [base, ordinal_identity(base, 1)]


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


def _identities(nodes):
    """Every component identity in a (materialized) tree, depth-first."""
    found = []

    def walk(n):
        if not isinstance(n, dict):
            return
        cid = n.get("component_id") or (n.get("attributes") or {}).get("data-component-id")
        if cid:
            found.append(str(cid))
        for key in ("content", "children"):
            v = n.get(key)
            if isinstance(v, list):
                for c in v:
                    walk(c)

    for n in nodes or []:
        walk(n)
    return found


def _shape(node):
    """Structural skeleton (type + child skeletons) — tolerant of the
    profile-specific keys ROTE adaptation adds or rewrites."""
    if not isinstance(node, dict):
        return None
    kids = []
    for key in ("content", "children"):
        v = node.get(key)
        if isinstance(v, list):
            kids += [s for s in (_shape(c) for c in v) if s is not None]
    return (str(node.get("type") or ""), tuple(kids))


def _shapes(nodes):
    return [s for s in (_shape(n) for n in nodes or []) if s is not None]


@pytest.fixture()
async def env(monkeypatch):
    """A real Orchestrator + one user on two sockets — browser and android —
    each viewing its own fresh chat."""
    monkeypatch.setenv("FF_UI_DESIGNER", "true")
    monkeypatch.setitem(flags._flags, "designer_all_devices", True)
    # Canvas-origin parity is independent of compatibility TaskManager
    # admission; do not let unrelated shared-database queue state gate a turn.
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

    async def _tool_result(*args, **kwargs):
        # Fresh dicts per call: identity stamping mutates the components, and
        # the two turns must not share (already-stamped) objects.
        return SimpleNamespace(result={"ok": True}, error=None,
                               ui_components=_rich_components(),
                               correlation_id=None)

    orch.execute_single_tool = AsyncMock(side_effect=_tool_result)

    user_id = f"coi-test-{uuid.uuid4().hex[:8]}"
    sockets, chats = {}, {}
    for device in ("browser", "android"):
        ws = _fresh_socket()
        orch.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
        orch.ui_clients.append(ws)
        orch.rote.register_device(ws, {"device_type": device})
        chat_id = await asyncio.to_thread(
            orch.history.create_chat,
            user_id=user_id,
        )
        orch._ws_active_chat[id(ws)] = chat_id
        sockets[device], chats[device] = ws, chat_id
    try:
        yield orch, sockets, chats, user_id
    finally:
        for chat_id in chats.values():
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
    """One tool round then a short final answer; designer passes answered
    through the same _call_llm seam (feature audit intact). Re-install
    before each turn — the round counter is per-turn."""
    state = {"n": 0}

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        if feature == "ui_designer":
            return _msg(content="DONE"), _usage()
        state["n"] += 1
        if state["n"] == 1:
            return _msg(tool_calls=[_tc()]), _usage()
        return _msg(content=final_text), _usage()

    orch._call_llm = fake_llm


def _install_designer(monkeypatch):
    """Deterministic design_round shared by BOTH origins: exercises llm_call
    once, then arranges the round's components into a 2-column grid."""
    from orchestrator import ui_designer
    calls = []

    async def _fake_design(**kwargs):
        calls.append(kwargs)
        await kwargs["llm_call"]([{"role": "user", "content": "arrange"}])
        refs = [{"type": "ref", "component_id": c["component_id"]}
                for c in kwargs["round_components"] if c.get("component_id")]
        return [{"type": "grid", "columns": 2, "children": refs}]

    monkeypatch.setattr(ui_designer, "design_round", _fake_design)
    return calls


async def _drive_both(orch, sockets, chats, user_id):
    """The identical turn, once per origin, each on its own fresh chat."""
    for device in ("browser", "android"):
        _install_llm(orch)
        await orch.handle_chat_message(
            sockets[device], "make a dashboard", chats[device], user_id=user_id)


async def test_both_origins_persist_equal_layout_rows(env, monkeypatch):
    """Identical turn from web and Android → a workspace_layout row in BOTH
    chats, claiming the same content-derived identities in the same tree."""
    orch, sockets, chats, user_id = env
    design_calls = _install_designer(monkeypatch)

    await _drive_both(orch, sockets, chats, user_id)

    assert len(design_calls) == 2, "the designer ran for BOTH origins"
    from orchestrator.workspace import iter_layout_refs
    layouts = {d: await asyncio.to_thread(orch.workspace.live_layouts, chats[d], user_id)
               for d in ("browser", "android")}
    for device, rows in layouts.items():
        assert len(rows) == 1, f"{device}-origin turn persists a workspace_layout row"
    web_tree = layouts["browser"][0]["layout"]
    native_tree = layouts["android"][0]["layout"]
    assert web_tree == native_tree, \
        "persisted arrangement is identical regardless of originating device"
    assert list(iter_layout_refs(web_tree)) == _expected_family(), \
        "the arrangement claims the deterministic fingerprint family"


async def test_materialized_canvases_equivalent_per_profile(env, monkeypatch):
    """Persisted materialization equal across chats; the wire canvases carry
    the same identity set and tree shape on both profiles."""
    orch, sockets, chats, user_id = env
    _install_designer(monkeypatch)

    await _drive_both(orch, sockets, chats, user_id)

    canvases = {d: await asyncio.to_thread(orch._canvas_components, chats[d], user_id)
                for d in ("browser", "android")}
    assert _identities(canvases["browser"]) == _identities(canvases["android"]) \
        == _expected_family(), "persisted canvas identities are origin-independent"
    assert _shapes(canvases["browser"]) == _shapes(canvases["android"]), \
        "persisted canvas materializes to the same tree for both chats"

    wire = {}
    for device in ("browser", "android"):
        renders = _canvas_renders(sockets[device])
        assert len(renders) == 1, f"exactly one designed canvas render on {device}"
        wire[device] = renders[0].get("components") or []
    assert set(_identities(wire["browser"])) == set(_identities(wire["android"])) \
        == set(_expected_family()), "delivered identity sets equal across profiles"
    assert _shapes(wire["browser"]) == _shapes(wire["android"]), \
        "delivered layout tree shape equal modulo profile adaptation"
    assert _shapes(wire["browser"])[0][0] == "grid", \
        "the designed grid frames both delivered canvases"


async def test_delivery_point_differs_by_contract(env, monkeypatch):
    """Same designed canvas, per-profile delivery point (wire-contract §5):
    flat ui_upsert first on both; web render mid-turn, native render
    post-done."""
    orch, sockets, chats, user_id = env
    _install_designer(monkeypatch)

    await _drive_both(orch, sockets, chats, user_id)

    for device, after_done in (("browser", False), ("android", True)):
        ws = sockets[device]
        types = [f.get("type") for f in _frames(ws)]
        renders = _canvas_renders(ws)
        assert len(renders) == 1
        i_render = _frames(ws).index(renders[0])
        assert types.index("ui_upsert") < i_render, \
            f"{device}: flat upsert-first delivery precedes the designed render"
        i_done = _last_done_index(ws)
        if after_done:
            assert i_render > i_done, "native designed render lands after done"
        else:
            assert i_render < i_done, "web designed render lands mid-turn"
