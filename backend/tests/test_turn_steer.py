"""Tests for owner steering of a running turn (orchestrator/turn_steer.py): negotiation and
acknowledgment, refusals, and delivery of a steer to the model at the next tool boundary."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import turn_steer
from tests.helpers.registered_human import registered_chat
from tests.test_analysis_render_targets import (
    USER, _chat, _cleanup, _msg, _register, _tc, _usage, _ws,
    generated_agent_bundles as generated_agent_bundles, orch as orch, plane_runtime as plane_runtime,
)

pytestmark = pytest.mark.asyncio


def _profile(device_type="macos", contract="console/v2"):
    return SimpleNamespace(device_type=SimpleNamespace(value=device_type), console_contract=contract)


async def test_negotiation_needs_the_mac_console_device_flag_and_capability():
    device = {"turn_steer_v1": True}
    assert turn_steer.negotiated(_profile(), device, ["turn_steer_v1"])
    assert not turn_steer.negotiated(_profile("ios"), device, ["turn_steer_v1"])
    assert not turn_steer.negotiated(_profile(contract="console/v1"), device, ["turn_steer_v1"])
    assert not turn_steer.negotiated(_profile(), {"turn_steer_v1": "yes"}, ["turn_steer_v1"])
    assert not turn_steer.negotiated(_profile(), device, [])


async def test_acknowledgment_offers_steering_without_the_queue():
    assert turn_steer.acknowledge({"console": {}}, True) == {
        "console": {"turn_steer": {"version": 1, "queue_capable": False}}}
    assert turn_steer.acknowledge({"console": {}}, False) == {"console": {}}
    assert turn_steer.acknowledge({}, True) == {}


def _sent(orch):
    return [json.loads(call.args[1]) for call in orch._safe_send.await_args_list]


def _steer(text="Use metric units"):
    # The client repeats the envelope identity inside the payload.
    return {"message": text, "client_message_id": str(uuid.uuid4()),
            "submission_id": str(uuid.uuid4()), "request_generation": str(uuid.uuid4())}


async def _negotiated(orch):
    ws = _ws(orch)
    orch.ui_sessions[ws] = {"sub": USER, "_turn_steer_v1": True}
    return ws


@pytest.mark.parametrize("payload", [
    {"message": "   ", "client_message_id": str(uuid.uuid4())},
    {"message": "x" * 20001, "client_message_id": str(uuid.uuid4())},
    {"message": "ok", "client_message_id": str(uuid.uuid4()), "approve": True},
])
async def test_invalid_steers_are_refused(orch, payload):
    ws = await _negotiated(orch)
    turn_steer.open_turn(orch, ws, "c1")
    await turn_steer.receive(orch, ws, USER, "c1", payload)
    assert _sent(orch)[-1]["code"] == "invalid_input"


async def test_a_steer_without_negotiation_is_refused(orch):
    ws = _ws(orch)
    orch.ui_sessions[ws] = {"sub": USER}
    turn_steer.open_turn(orch, ws, "c1")
    await turn_steer.receive(orch, ws, USER, "c1", _steer())
    assert _sent(orch)[-1]["code"] == "invalid_input"


async def test_a_steer_with_no_running_turn_is_refused(orch):
    ws = await _negotiated(orch)
    await turn_steer.receive(orch, ws, USER, "c1", _steer())
    assert _sent(orch)[-1] | {"client_message_id": None} == {
        "type": "chat_steer_refused", "chat_id": "c1", "client_message_id": None,
        "code": "not_running", "message": "No turn is running in this chat."}


async def test_a_steer_never_answers_a_pending_approval(orch):
    ws = await _negotiated(orch)
    turn_steer.open_turn(orch, ws, "c1")
    orch._hitl_pending_calls = {"r1": SimpleNamespace(owner=USER, chat="c1", websocket=ws)}
    await turn_steer.receive(orch, ws, USER, "c1", _steer("approve it"))
    assert _sent(orch)[-1]["code"] == "approval_pending"
    assert orch._turn_steers[(id(ws), "c1")] == []


async def test_steers_beyond_the_bound_are_refused(orch):
    ws = await _negotiated(orch)
    turn_steer.open_turn(orch, ws, "c1")
    for _ in range(17):
        await turn_steer.receive(orch, ws, USER, "c1", _steer())
    assert _sent(orch)[-1]["code"] == "capacity_exceeded"


async def test_an_unapplied_steer_is_handed_back_when_the_turn_ends(orch):
    ws = await _negotiated(orch)
    turn_steer.open_turn(orch, ws, "c1")
    steer = _steer()
    await turn_steer.receive(orch, ws, USER, "c1", steer)
    await turn_steer.close_turn(orch, ws, "c1")
    assert _sent(orch)[-1]["code"] == "stale_turn"
    assert _sent(orch)[-1]["client_message_id"] == steer["client_message_id"]


async def test_a_steer_reaches_the_model_at_the_next_tool_boundary(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    tool_started, release_tool = asyncio.Event(), asyncio.Event()
    calls = []

    async def model(websocket, messages, tools_desc=None, **_kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _msg(tool_calls=[_tc()]), _usage()
        return _msg(content="Done."), _usage()

    async def slow_tool(*_args, **_kwargs):
        tool_started.set()
        await release_tool.wait()
        return SimpleNamespace(result={"_data": {}}, error=None, ui_components=[], correlation_id=None)

    orch._call_llm = model
    orch.execute_single_tool = slow_tool
    turn = asyncio.create_task(registered_chat(orch, ws, "forecast please", chat_id, user_id=USER))
    await asyncio.wait_for(tool_started.wait(), 5)
    orch.ui_sessions[ws]["_turn_steer_v1"] = True
    steer = _steer("Use metric units")
    await turn_steer.receive(orch, ws, USER, chat_id, steer)
    release_tool.set()
    await asyncio.wait_for(turn, 10)
    assert {"role": "user", "content": "Use metric units"} in calls[1]
    assert {"type": "chat_steer_recorded", "chat_id": chat_id,
            "client_message_id": steer["client_message_id"], "skipped_calls": []} in _sent(orch)
    saved = (await asyncio.to_thread(orch.history.get_chat, chat_id, USER))["messages"]
    assert "Use metric units" in json.dumps([m["content"] for m in saved])
    assert (id(ws), chat_id) not in orch._turn_steers
    await _cleanup(orch, chat_id)
