"""Regression tests: mid-loop analysis output must never replace the canvas.

A canvas-target ``ui_render`` is a full-canvas replace on every client
(web ``setHTML(canvas, …)``, Windows ``canvas.set_components``), so any
words-only message the ReAct loop emits mid/end-of-turn — reasoning
collapsibles, cancellation/denial alerts, the max-turns summary — must go to
the chat rail. The user-visible bug: a reasoning-model turn ("Analyzing
results") wiped this turn's just-delivered chart components and left only a
"Reasoning" collapsible on the canvas.

Components themselves reach the canvas via ``_deliver_round_components``
(ui_upsert append/supersede semantics), never via a full-replace render.
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
from astralplane import GENERATED_AGENT_BUNDLE_CONTRACT, ImmutableBundleStore

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.runtime_composition import AstralRuntimeComposition  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

USER = "target-user"


class _AttachmentMaterializer:
    """No-I/O materializer for a fixture that never handles attachments."""

    def materialize_bytes(self, **_values):
        raise AssertionError("render-target tests must not materialize attachments")

    async def close(self):
        return None

    def abort(self):
        return None


class _AttachmentMaterializations:
    def close(self):
        return None


class _AttachmentPurges:
    started = False

    async def close(self):
        self.started = False

    def abort(self):
        self.started = False


class _LetsOff:
    config = SimpleNamespace(mode="off")
    authorization_gateway = None
    lifecycle = None
    byo_lifecycle = None
    client = None
    _tasks = ()

    def start_reconcilers(self):
        return ()

    async def stop(self):
        return None


def _runtime_composition(plane_runtime, bundle_store):
    plane = SimpleNamespace(
        runtime=plane_runtime,
        repositories=plane_runtime.repositories,
        blobs=object(),
        generated_agent_bundles=bundle_store,
        attachment_materializer=_AttachmentMaterializer(),
        attachment_materializations=_AttachmentMaterializations(),
        attachment_purges=_AttachmentPurges(),
        # The owning isolated_plane_runtime fixture closes the pool after every
        # orchestrator has released its process bindings.
        close=lambda: None,
    )
    return AstralRuntimeComposition(plane=plane, lets=_LetsOff())


@pytest.fixture(scope="module")
def plane_runtime():
    """One isolated current-schema Plane shared by this focused test module."""

    with isolated_plane_runtime("analysis_render_targets") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def generated_agent_bundles(tmp_path_factory):
    """Real bounded Plane store required by Orchestrator construction."""

    return ImmutableBundleStore(
        tmp_path_factory.mktemp("analysis-render-bundles"),
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
    )


@pytest.fixture
async def orch(plane_runtime, generated_agent_bundles, monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    composition = _runtime_composition(plane_runtime, generated_agent_bundles)
    monkeypatch.setenv("LETS_MODE", "off")
    monkeypatch.setattr(
        "orchestrator.runtime_composition.compose_astral_runtime",
        lambda _manifest: composition,
    )
    # Voice is unrelated to render-target routing. Keep this focused graph
    # typed-chat-only instead of constructing optional media infrastructure.
    monkeypatch.setattr(
        "orchestrator.voice_bootstrap.build_voice_services",
        MagicMock(side_effect=RuntimeError("voice disabled in render-target fixture")),
    )
    o = await asyncio.to_thread(Orchestrator)
    try:
        # Feature 054: chat turns pre-flight the acting user's PERSISTED LLM
        # config (env vars are inert) — seed the fixture user so turns proceed.
        await asyncio.to_thread(
            o._llm_store.set_sync,
            USER,
            provider="custom",
            base_url="http://test.invalid/v1",
            model="test-model",
            api_key="test-key",
        )
        o.audit_recorder = MagicMock()
        o.audit_recorder.record = AsyncMock()
        o._record_llm_call = AsyncMock()
        o._record_llm_unconfigured = AsyncMock()
        o._safe_send = AsyncMock()
        o.send_ui_render = AsyncMock()
        hb = MagicMock()
        hb.cancel = MagicMock()
        o._start_heartbeat = AsyncMock(return_value=hb)
        o._send_or_replace_components = AsyncMock(return_value=[])
        o._emit_llm_usage_report = AsyncMock()
        o._deliver_round_components = AsyncMock(return_value=[])
        yield o
    finally:
        try:
            await asyncio.to_thread(o._llm_store.clear_sync, USER)
        finally:
            await o._close_started_services()
            from audit.recorder import set_recorder
            from orchestrator.artifact_share import set_share_store

            set_recorder(None)
            set_share_store(None)


def _register(o, tool_id="forecast_tool", agent_id="a-1"):
    from shared.protocol import AgentCard, AgentSkill
    o.agent_cards[agent_id] = AgentCard(
        name="t", description="d", agent_id=agent_id,
        skills=[AgentSkill(name="forecast", description="s", id=tool_id,
                           input_schema={"type": "object"})])
    o.agents[agent_id] = MagicMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed.return_value = True


def _ws(o, user_id=USER):
    ws = MagicMock()
    o.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    return ws


def _msg(content=None, tool_calls=None, reasoning=None):
    return SimpleNamespace(role="assistant", content=content,
                           tool_calls=tool_calls, reasoning_content=reasoning)


def _tc(name="forecast_tool", cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments="{}"))


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


async def _chat(o):
    chat_id = f"target-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id=USER)
    return chat_id


async def _cleanup(o, chat_id):
    await asyncio.to_thread(o.history.delete_chat, chat_id, user_id=USER)


def _target_of(call) -> str:
    """The effective ui_render target of a recorded send_ui_render call."""
    if "target" in call.kwargs:
        return call.kwargs["target"]
    if len(call.args) > 2:
        return call.args[2]
    return "canvas"


def _components_json(call) -> str:
    return json.dumps(call.args[1] if len(call.args) > 1 else call.kwargs.get("components"))


# --------------------------------------------------------------------------- #
# The reported bug: reasoning content on an "Analyzing results" turn replaced
# the whole canvas with a Reasoning collapsible.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reasoning_goes_to_chat_and_never_replaces_canvas(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"ok": True}, error=None,
        ui_components=[{"type": "chart", "title": "Daily highs"}],
        correlation_id=None))

    calls = {"n": 0}

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        calls["n"] += 1
        if calls["n"] == 1:
            return _msg(tool_calls=[_tc()],
                        reasoning="I should fetch the forecast first."), _usage()
        return _msg(content="Stable week — highs in the low 80s.",
                    reasoning="All three charts rendered successfully."), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "weather with charts", chat_id, user_id=USER)

    renders = orch.send_ui_render.await_args_list
    reasoning_calls = [c for c in renders if '"Reasoning"' in _components_json(c)]
    assert reasoning_calls, "expected the reasoning collapsible to be rendered"
    assert all(_target_of(c) == "chat" for c in reasoning_calls)
    # No full-canvas replace anywhere in the turn — components reach the
    # canvas only through _deliver_round_components (upsert semantics).
    assert all(_target_of(c) != "canvas" for c in renders)
    orch._deliver_round_components.assert_awaited()
    await _cleanup(orch, chat_id)


# --------------------------------------------------------------------------- #
# Same class of bug: the other words-only mid/end-of-loop renders.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_cancellation_alert_goes_to_chat(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch.cancelled_sessions[id(ws)] = True
    orch._call_llm = AsyncMock(return_value=(_msg(content="unused"), _usage()))

    await orch.handle_chat_message(ws, "anything", chat_id, user_id=USER)

    cancel_calls = [c for c in orch.send_ui_render.await_args_list
                    if "cancelled" in _components_json(c)]
    assert cancel_calls, "expected the cancellation alert to be rendered"
    assert all(_target_of(c) == "chat" for c in cancel_calls)
    await _cleanup(orch, chat_id)


@pytest.mark.asyncio
async def test_denial_loop_warning_goes_to_chat(orch, monkeypatch):
    # The all-tools-denied break requires tools_desc to empty out — keep the
    # default-on meta-tools (create_capability, scheduling, memory, desktop
    # codegen, subtasks) out of the tool list for this test.
    for mod in ("agentic_creation", "scheduling_chat", "memory_chat",
                "desktop_codegen", "subtasks"):
        monkeypatch.setattr(f"orchestrator.{mod}.should_inject",
                            lambda draft_agent_id: False)
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result=None, error={"message": "This tool is restricted by your permissions."},
        ui_components=[], correlation_id=None))

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(tool_calls=[_tc()]), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "keep trying", chat_id, user_id=USER)

    warn_calls = [c for c in orch.send_ui_render.await_args_list
                  if "restricted by your permission settings" in _components_json(c)]
    assert warn_calls, "expected the all-tools-denied warning to be rendered"
    assert all(_target_of(c) == "chat" for c in warn_calls)
    await _cleanup(orch, chat_id)


@pytest.mark.asyncio
async def test_max_turns_summary_goes_to_chat(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"ok": True}, error=None, ui_components=[], correlation_id=None))
    orch._generate_tool_summary = AsyncMock(return_value=[
        {"type": "card", "title": "Round results",
         "content": [{"type": "text", "content": "Summary."}]}])

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(tool_calls=[_tc()]), _usage()  # never a final answer

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "loop forever", chat_id, user_id=USER)

    summary_calls = [c for c in orch.send_ui_render.await_args_list
                     if "Round results" in _components_json(c)]
    assert summary_calls, "expected the max-turns summary to be rendered"
    assert all(_target_of(c) == "chat" for c in summary_calls)
    await _cleanup(orch, chat_id)


@pytest.mark.asyncio
async def test_max_turns_fallback_card_goes_to_chat(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        result={"ok": True}, error=None, ui_components=[], correlation_id=None))
    orch._generate_tool_summary = AsyncMock(return_value=None)  # LLM summary failed

    async def fake_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return _msg(tool_calls=[_tc()]), _usage()

    orch._call_llm = fake_llm
    await orch.handle_chat_message(ws, "loop forever", chat_id, user_id=USER)

    fallback_calls = [c for c in orch.send_ui_render.await_args_list
                      if "Multiple tool operations were completed" in _components_json(c)]
    assert fallback_calls, "expected the fallback summary card to be rendered"
    assert all(_target_of(c) == "chat" for c in fallback_calls)
    await _cleanup(orch, chat_id)
