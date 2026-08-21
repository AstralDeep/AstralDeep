"""Feature 029 — adaptive designer orchestrator integration (T013).

The real, unbound orchestrator methods bound onto a fake ``self`` (the
established test_snapshot_turn_sites.py pattern) over a real Postgres-backed
WorkspaceManager: the designed-round delivery path end-to-end (stubbed LLM),
every fallback trigger (flag off, single component, timeout, LLM error),
materialized canvas reads, in-place refresh under an arrangement, the
retired/merged source guards, and the FR-027 contextual chat narrative.
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

from orchestrator import ui_designer  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.workspace import WorkspaceManager, layout_key_for  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


class _FakeWS:
    def __init__(self, label: str = ""):
        self.label = label


@pytest.fixture
def chat_env(plane_runtime):
    from orchestrator.history import HistoryManager

    history = HistoryManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    user_id = f"pytest-uid-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    history.add_message(chat_id, "user", "compare things", user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("ui_designer") as runtime:
        yield runtime


@pytest.fixture
def audit_events(monkeypatch):
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_workspace_event", _record)
    return events


def _make_fake(history, user_id, llm_content=None, llm_exc=None, llm_delay=0.0):
    """Fake orchestrator self with the real 028/029 methods bound on."""
    from rote.rote import ROTE

    sent, renders, llm_calls = [], [], []

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas", speak=True):
        renders.append((ws, components, target))

    async def _call_llm(websocket, messages, tools_desc=None, temperature=None, feature="tool_dispatch"):
        llm_calls.append({"feature": feature, "messages": messages})
        if llm_delay:
            await asyncio.sleep(llm_delay)
        if llm_exc:
            raise llm_exc
        return types.SimpleNamespace(content=llm_content), {}

    fake = types.SimpleNamespace(
        workspace=WorkspaceManager(history),
        history=history,
        _ws_active_chat={},
        _ws_timeline_mode={},
        ui_clients=[],
        rote=ROTE(),
        _get_user_id=lambda ws: user_id,
        _safe_send=_safe_send,
        send_ui_render=send_ui_render,
        _call_llm=_call_llm,
    )
    for name in ("_deliver_round_components", "_send_or_replace_components",
                 "send_ui_upsert", "_push_canvas", "_canvas_components",
                 "_run_designer"):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._sent = sent
    fake._renders = renders
    fake._llm_calls = llm_calls
    return fake


def _comp(agent, tool, params, **extra):
    c = {"type": "table", "headers": ["A"], "rows": [["1"]],
         "_source_agent": agent, "_source_tool": tool, "_source_params": params}
    c.update(extra)
    return c


def _run(coro):
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    return asyncio.run(_wrapper())


def _design_json_for(fake, chat_id, user_id, comps):
    """A stub design referencing the components' (precomputed) identities."""
    from orchestrator.workspace import fingerprint

    ids = [fingerprint(c["_source_agent"], c["_source_tool"], c["_source_params"])
           for c in comps]
    return json.dumps({"layout": [
        {"type": "metric", "title": "Headline", "value": "2 results"},
        {"type": "grid", "columns": 2, "children": [
            {"type": "ref", "component_id": ids[0]},
            {"type": "ref", "component_id": ids[1]},
        ]},
    ]}), ids


# ---------------------------------------------------------------------------
# Designed delivery path
# ---------------------------------------------------------------------------


def test_designed_round_persists_layout_and_renders_canvas(chat_env, audit_events, monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    # A host .env can re-leak these after conftest's ambient strip (load_dotenv
    # runs at orchestrator import); both change the deterministic pass count.
    monkeypatch.delenv("FF_UI_DESIGNER_TASKMODEL", raising=False)
    monkeypatch.delenv("UI_DESIGNER_MAX_ROUNDS", raising=False)
    history, user_id, chat_id = chat_env
    comps = [_comp("agent-x", "tool_a", {"p": 1}, title="A"),
             _comp("agent-y", "tool_b", {"q": 2}, title="B")]
    design, ids = _design_json_for(None, chat_id, user_id, comps)
    fake = _make_fake(history, user_id, llm_content=design)
    ws = _FakeWS("origin")
    # 052 stale-chat guard: the designed render is only forced onto the
    # originating socket while it still views this chat (production marks
    # the active chat before every turn).
    fake._ws_active_chat[id(ws)] = chat_id

    ops = _run(fake._deliver_round_components(ws, comps, chat_id, user_id,
                                              user_request="compare things"))
    assert [op["component_id"] for op in ops] == ids
    # Every designer pass runs under the ui_designer audit feature. With the
    # default single round (052) that is one draft pass; with more rounds the
    # stub regurgitates the same layout, deterministically converging at
    # draft + one stable refinement = 2 passes.
    assert {c["feature"] for c in fake._llm_calls} == {"ui_designer"}
    assert len(fake._llm_calls) == min(2, ui_designer.designer_max_rounds())
    # Arrangement persisted with the deterministic round key.
    live = fake.workspace.live_layouts(chat_id, user_id)
    assert len(live) == 1
    expected_key = layout_key_for(
        chat_id, str(history.get_latest_message_id(chat_id, user_id=user_id)))
    assert live[0]["layout_key"] == expected_key
    # 052 upsert-first delivery: the flat components go out immediately, then
    # the designed full canvas lands as the in-place refinement.
    assert any(m.get("type") == "ui_upsert" for _, m in fake._sent), \
        "flat ui_upsert must precede the design pass (FR-013)"
    assert len(fake._renders) == 1
    _, rendered, target = fake._renders[0]
    assert target == "canvas"
    rendered_json = json.dumps(rendered)
    for cid in ids:
        assert cid in rendered_json, "every round component present in the designed render"
    assert rendered[0]["type"] == "metric", "garnish leads the arrangement"
    # Audit parity with the flat path.
    assert {e["action"] for e in audit_events} == {"component_added"}


def test_canvas_components_materializes_and_orders(chat_env, monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    history, user_id, chat_id = chat_env
    fake0 = _make_fake(history, user_id)
    # An older, never-designed component stays ahead of the designed round.
    old_ops = fake0.workspace.upsert(chat_id, user_id, [_comp("agent-z", "old_tool", {})])
    comps = [_comp("agent-x", "tool_a", {"p": 1}), _comp("agent-y", "tool_b", {"q": 2})]
    design, ids = _design_json_for(None, chat_id, user_id, comps)
    fake = _make_fake(history, user_id, llm_content=design)
    _run(fake._deliver_round_components(_FakeWS(), comps, chat_id, user_id))

    canvas = fake._canvas_components(chat_id, user_id)
    assert canvas[0]["component_id"] == old_ops[0]["component_id"], "unclaimed first (position order)"
    assert canvas[1]["type"] == "metric"
    grid = canvas[2]
    assert grid["type"] == "grid"
    nested_ids = [c.get("component_id") for c in grid["children"]]
    assert nested_ids == ids
    assert grid["children"][0]["attributes"]["data-component-id"] == ids[0]


def test_refresh_inside_arrangement_morphs_in_place(chat_env, monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    history, user_id, chat_id = chat_env
    comps = [_comp("agent-x", "tool_a", {"p": 1}), _comp("agent-y", "tool_b", {"q": 2})]
    design, ids = _design_json_for(None, chat_id, user_id, comps)
    fake = _make_fake(history, user_id, llm_content=design)
    _run(fake._deliver_round_components(_FakeWS(), comps, chat_id, user_id))

    # component_action-style re-execution pins the refreshed output onto the
    # same identity (force_component_id) — the arrangement is untouched.
    refreshed = _comp("agent-x", "tool_a", {"p": 1}, rows=[["FRESH"]])
    fake.workspace.upsert(chat_id, user_id, [refreshed], force_component_id=ids[0])
    canvas = fake._canvas_components(chat_id, user_id)
    grid = next(c for c in canvas if c.get("type") == "grid")
    assert grid["children"][0]["rows"] == [["FRESH"]], "leaf morphed inside the designed layout"
    assert len(fake.workspace.live_layouts(chat_id, user_id)) == 1


# ---------------------------------------------------------------------------
# Fallback triggers (FR-022 / SC-002)
# ---------------------------------------------------------------------------


def _assert_flat_fallback(fake, chat_id, user_id, expect_layouts=0):
    assert len(fake.workspace.live_layouts(chat_id, user_id)) == expect_layouts
    upserts = [m for _, m in fake._sent if m.get("type") == "ui_upsert"]
    assert upserts, "legacy ui_upsert delivery used"
    assert fake._renders == [], "no designed canvas render"


def test_flag_off_restores_legacy_path(chat_env, audit_events, monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER", "false")
    history, user_id, chat_id = chat_env
    comps = [_comp("agent-x", "tool_a", {}), _comp("agent-y", "tool_b", {})]
    fake = _make_fake(history, user_id, llm_content="{}")
    ops = _run(fake._deliver_round_components(_FakeWS(), comps, chat_id, user_id))
    assert len(ops) == 2
    assert fake._llm_calls == [], "designer LLM never invoked when disabled"
    _assert_flat_fallback(fake, chat_id, user_id)


def test_single_component_round_skips_designer(chat_env, audit_events, monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_content="{}")
    ops = _run(fake._deliver_round_components(
        _FakeWS(), [_comp("agent-x", "tool_a", {})], chat_id, user_id))
    assert len(ops) == 1
    assert fake._llm_calls == []
    _assert_flat_fallback(fake, chat_id, user_id)


def test_llm_error_falls_back_with_components_intact(chat_env, audit_events, monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    history, user_id, chat_id = chat_env
    comps = [_comp("agent-x", "tool_a", {}), _comp("agent-y", "tool_b", {})]
    fake = _make_fake(history, user_id, llm_exc=RuntimeError("LLM down"))
    ops = _run(fake._deliver_round_components(_FakeWS(), comps, chat_id, user_id))
    assert len(ops) == 2, "components persisted despite designer failure"
    _assert_flat_fallback(fake, chat_id, user_id)


def test_timeout_falls_back(chat_env, audit_events, monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    monkeypatch.setenv("UI_DESIGNER_TIMEOUT_SECONDS", "0.05")
    history, user_id, chat_id = chat_env
    comps = [_comp("agent-x", "tool_a", {}), _comp("agent-y", "tool_b", {})]
    fake = _make_fake(history, user_id, llm_content="{}", llm_delay=0.5)
    ops = _run(fake._deliver_round_components(_FakeWS(), comps, chat_id, user_id))
    assert len(ops) == 2
    _assert_flat_fallback(fake, chat_id, user_id)


def test_timeline_mode_never_designs(chat_env, audit_events, monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    history, user_id, chat_id = chat_env
    comps = [_comp("agent-x", "tool_a", {}), _comp("agent-y", "tool_b", {})]
    fake = _make_fake(history, user_id, llm_content="{}")
    ws = _FakeWS()
    fake._ws_timeline_mode[id(ws)] = True
    _run(fake._deliver_round_components(ws, comps, chat_id, user_id))
    assert fake._llm_calls == []


# ---------------------------------------------------------------------------
# Retired / merged source guards (FR-004, T019)
# ---------------------------------------------------------------------------


def test_merged_source_remap():
    from orchestrator.orchestrator import remap_merged_source

    assert remap_merged_source("classify-1", "submit_dataset") == \
        ("ml-services-1", "classify_submit_dataset")
    assert remap_merged_source("forecaster-1", "get_results") == \
        ("ml-services-1", "forecaster_get_results")
    assert remap_merged_source("llm-factory-1", "chat_with_model") == \
        ("ml-services-1", "chat_with_model")
    assert remap_merged_source("classify-1", "set_column_types") == \
        ("ml-services-1", "set_column_types"), "non-colliding names unchanged"
    assert remap_merged_source("weather-1", "get_current_weather") == \
        ("weather-1", "get_current_weather"), "unrelated agents untouched"


def _make_action_fake(history, user_id):
    fake = _make_fake(history, user_id)
    fake.security_flags = {}
    permission_calls = []

    class _Perms:
        def is_tool_allowed(self, uid, agent_id, tool_name):
            permission_calls.append((agent_id, tool_name))
            return False  # stop before dispatch — we only test the guard/remap

    fake.tool_permissions = _Perms()
    fake._workspace_locks = {}
    for name in ("_handle_component_action", "_component_action_allowed",
                 "_audit_workspace_denial"):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._permission_calls = permission_calls
    return fake


def test_component_action_on_retired_agent_yields_retirement_alert(chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_action_fake(history, user_id)
    ops = fake.workspace.upsert(chat_id, user_id,
                                [_comp("grants-1", "search_grants", {"q": "x"})])
    cid = ops[0]["component_id"]
    _run(fake._handle_component_action(_FakeWS(), user_id,
                                       {"chat_id": chat_id, "component_id": cid}))
    alerts = [c for _, comps, target in fake._renders for c in comps
              if c.get("type") == "alert" and target == "chat"]
    assert any("retired" in (a.get("message") or "") for a in alerts)
    assert any(e.get("action") == "action_denied" and
               e.get("detail", {}).get("reason") == "agent_retired"
               for e in audit_events)
    assert fake._permission_calls == [], "guard fires before permission/dispatch"


def test_component_action_on_merged_agent_reroutes_to_ml_services(chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_action_fake(history, user_id)
    ops = fake.workspace.upsert(chat_id, user_id,
                                [_comp("classify-1", "submit_dataset", {"f": "x.csv"})])
    cid = ops[0]["component_id"]
    _run(fake._handle_component_action(_FakeWS(), user_id,
                                       {"chat_id": chat_id, "component_id": cid}))
    assert fake._permission_calls == [("ml-services-1", "classify_submit_dataset")], \
        "pre-merge provenance transparently rerouted"


# ---------------------------------------------------------------------------
# FR-027 — contextual chat narrative
# ---------------------------------------------------------------------------


def _narrative(text):
    host = types.SimpleNamespace(_derive_chat_title=Orchestrator._derive_chat_title)
    return Orchestrator._chat_narrative(host, text)


def test_short_answer_renders_bare_markdown():
    out = _narrative("It is 72°F and sunny in Lexington.")
    assert out == [{"type": "text",
                    "content": "It is 72°F and sunny in Lexington.",
                    "variant": "markdown"}]


def test_long_answer_gets_contextual_heading_title():
    text = "# Forecast comparison\n\n" + ("Detail line.\n" * 40)
    out = _narrative(text)
    assert out[0]["type"] == "card"
    assert out[0]["title"] == "Forecast comparison"
    assert out[0]["title"] != "Analysis"


def test_long_answer_without_heading_uses_default():
    text = ("This is a long paragraph. " * 30) + "\n\nSecond paragraph."
    out = _narrative(text)
    assert out[0]["type"] == "card"
    assert out[0]["title"] == "Response"
