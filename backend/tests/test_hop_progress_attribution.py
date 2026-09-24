"""Tests confirming chained-hop and sub-task progress (orchestrator/subtasks.py,
orchestrator.py) rides the existing chat_status frame with per-hop attribution,
introducing no new client-visible frame type.
"""

from __future__ import annotations

import json
import os
import sys
import types
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import subtasks  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from shared.feature_flags import flags  # noqa: E402

MANIFEST = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "components",
    "AstralProjection",
    "contracts",
    "ui_protocol.json",
)


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)


@pytest.mark.asyncio
async def test_subtask_progress_uses_existing_chat_status_frame(monkeypatch):
    from orchestrator import turn_guidance_authority as guidance

    parent = types.SimpleNamespace(origin=types.SimpleNamespace(owner_id="u1"))
    monkeypatch.setattr(guidance, "current_turn_guidance", lambda **kwargs: parent)
    monkeypatch.setattr(guidance, "inherit_turn_guidance", lambda *args, **kwargs: parent)
    monkeypatch.setattr(guidance, "use_turn_guidance", lambda *args, **kwargs: nullcontext())
    sent = []

    async def _safe_send(ws, data):
        sent.append(json.loads(data))
        return True

    o = MagicMock()
    o.history.create_chat = MagicMock(side_effect=lambda user_id=None, **k: "sub-chat")
    o.ui_sessions = {}
    o._safe_send = _safe_send
    o._chain_budgets = {}
    o._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, o)

    async def _turn(vws, message, chat_id, **kw):
        await vws.send_json({"type": "chat_message", "payload": {"text": "done"}})

    o.handle_chat_message = _turn

    await subtasks.handle_meta_tool(
        o, "delegate_subtasks",
        {"subtasks": [{"title": "Program A", "instruction": "audit A"},
                      {"title": "Program B", "instruction": "audit B"}]},
        user_id="u1", chat_id="c1", websocket=MagicMock())

    assert sent, "progress must reach the originating chat"
    assert {f["type"] for f in sent} == {"chat_status"}
    messages = [f["message"] for f in sent]
    assert any("Program A" in m for m in messages)
    assert any("Program B" in m for m in messages)
    assert any("running" in m for m in messages)
    assert any("done" in m for m in messages)


def test_no_new_frame_type_in_the_manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)
    names = {e["name"] for e in manifest["push_types"]}
    assert "agent_hop_request" not in names
    assert "agent_hop_response" not in names
    assert "subtask_progress" not in names
    assert "chat_status" in names


def test_hop_frames_are_agent_channel_only():
    import inspect

    from orchestrator import orchestrator as orch_mod

    src = inspect.getsource(orch_mod.Orchestrator._deliver_hop_response)
    assert "ui_clients" not in src
    assert "send_ui_render" not in src
