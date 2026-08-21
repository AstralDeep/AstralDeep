"""Feature 054 — T042: watch guidance while unconfigured (US5 / FR-017).

The watch is chrome-free by design and never receives the mandatory setup
dialog. When an unconfigured user attempts AI use on the watch, the chat
pre-flight sends the exact phone/web guidance Alert (spoken via the normal
alert-render path) and audits ``llm_unconfigured``. Once the user configures
a provider on ANY other client, the same watch socket works with no
watch-side action.

References: specs/054-byo-llm-setup/spec.md US5, FR-017.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

WATCH_GUIDE = "Set up your AI provider on your phone or the web first."

SECRET = "sk-supersecret-watch-key-123456789012345"


def _uid() -> str:
    return f"watch054-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def orch_module(orchestrator_module_factory):
    return orchestrator_module_factory()


@pytest.fixture
def orch(orch_module):
    o = orch_module
    o.ui_sessions = {}
    o._ws_llm_gated = {}
    o._safe_send = AsyncMock()
    o.send_ui_render = AsyncMock()
    o._record_llm_unconfigured = AsyncMock()
    # Downstream seams for a full (faked-LLM) turn — same set the
    # test_chat_text_only fixture patches.
    heartbeat = MagicMock()
    heartbeat.cancel = MagicMock()
    o._start_heartbeat = AsyncMock(return_value=heartbeat)
    o._send_or_replace_components = AsyncMock()
    o._emit_llm_usage_report = AsyncMock()
    return o


def _watch_socket(orch, uid):
    ws = MagicMock()
    orch.ui_sessions[ws] = {"sub": uid, "preferred_username": f"{uid}@example"}
    orch.rote.register_device(ws, {"device_type": "watch"})
    return ws


def _rendered_alerts(orch):
    alerts = []
    for call in orch.send_ui_render.call_args_list:
        if len(call.args) >= 2:
            for comp in call.args[1]:
                if isinstance(comp, dict) and comp.get("type") == "alert":
                    alerts.append(comp)
    return alerts


async def test_unconfigured_watch_gets_exact_spoken_guidance(orch):
    uid = _uid()
    ws = _watch_socket(orch, uid)
    chat_id = f"watch-gate-{uuid.uuid4().hex[:8]}"

    called = {"n": 0}

    async def fake_call_llm(*args, **kwargs):
        called["n"] += 1
        return None, None

    orch._call_llm = fake_call_llm

    await orch.handle_chat_message(ws, "what's the weather", chat_id, user_id=uid)

    # The turn was refused up-front — no LLM call, no silent loss into a
    # broken turn (US5-AS1).
    assert called["n"] == 0
    alerts = _rendered_alerts(orch)
    assert alerts, "the watch must receive guidance, not silence"
    # EXACT watch copy — phone/web pointer, not the generic settings copy.
    assert alerts[-1]["message"] == WATCH_GUIDE
    assert alerts[-1]["variant"] == "error"
    # Audited llm_unconfigured refusal.
    assert orch._record_llm_unconfigured.await_count == 1
    assert orch._record_llm_unconfigured.call_args.kwargs["feature"] == "chat_dispatch"
    # The watch is never pushed the mandatory dialog (FR-017): no chrome
    # frame went out on this socket.
    for send_call in orch._safe_send.call_args_list:
        payload = send_call.args[1] if len(send_call.args) > 1 else ""
        assert "chrome_render" not in str(payload)
        assert "chrome_surface" not in str(payload)


async def test_watch_works_after_configuring_on_another_client(orch):
    uid = _uid()
    ws = _watch_socket(orch, uid)
    chat_id = f"watch-ok-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id=uid)

    # The user configures on ANOTHER client (server-persisted record).
    await orch._llm_store.set(
        uid, provider="openai", base_url="https://api.openai.com/v1",
        model="gpt-4o-mini", api_key=SECRET)

    called = {"n": 0}

    async def fake_call_llm(websocket, messages, tools_desc=None,
                            temperature=None, feature="tool_dispatch", **kwargs):
        called["n"] += 1
        return (
            SimpleNamespace(content="It is 72 and clear.", tool_calls=None,
                            reasoning_content=None),
            SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )

    orch._call_llm = fake_call_llm
    try:
        await orch.handle_chat_message(ws, "what's the weather", chat_id, user_id=uid)

        # The same watch socket now sails past the pre-flight — no
        # watch-side steps were required (US5-AS2).
        assert called["n"] >= 1
        assert orch._record_llm_unconfigured.await_count == 0
        assert all(a["message"] != WATCH_GUIDE for a in _rendered_alerts(orch))
    finally:
        await orch._llm_store.clear(uid)
        await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id=uid)
