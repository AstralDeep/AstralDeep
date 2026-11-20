"""Tests for the orchestrator's component action guards over Postgres-backed
workspace.py: retired/merged source guards and the contextual chat narrative.
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
    for name in ("_send_or_replace_components", "send_ui_upsert", "_canvas_components"):
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


def _assert_flat_fallback(fake, chat_id, user_id, expect_layouts=0):
    assert len(fake.workspace.live_layouts(chat_id, user_id)) == expect_layouts
    upserts = [m for _, m in fake._sent if m.get("type") == "ui_upsert"]
    assert upserts, "legacy ui_upsert delivery used"
    assert fake._renders == [], "no designed canvas render"


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
            return False

    fake.tool_permissions = _Perms()
    fake._workspace_locks = {}
    for name in ("_handle_component_action", "_component_action_allowed",
                 "_audit_workspace_denial"):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._register_ui_operation_refusal = (
        Orchestrator._register_ui_operation_refusal)
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
