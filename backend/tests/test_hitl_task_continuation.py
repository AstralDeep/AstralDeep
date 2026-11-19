"""Exercises host reply, event and cancellation fencing using the orchestrator connection controls."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import source_bound
from orchestrator.source_bound import declaration as strict_declaration
from shared.protocol import MCPResponse
from tests.test_computer_use_076 import _descriptor, _FakeOrch
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401


@pytest.fixture
def task_runtime(runtime, monkeypatch):  # noqa: F811
    from shared.feature_flags import flags

    orch, ws = runtime
    monkeypatch.setitem(flags._flags, "user_skills", False)
    monkeypatch.setattr(source_bound, "declaration", strict_declaration)
    orch.tool_permissions.get_tool_scope_map.return_value = {"send_email": "tools:write"}
    orch.audit_recorder.record.return_value = object()
    orch._chat_locks = {}
    orch._workspace_locks = {}
    orch._chat_recorders = {}
    orch.cancelled_sessions = {}
    effects, turns, proposals = [], [], []
    outcome = [MCPResponse(result={"_data": {"ok": True}})]

    class Adapter:
        mode = "off"

        async def execute(self, **kwargs):
            return await kwargs["invoke"](None)

    orch.governed_final_dispatch = Adapter()

    async def execute(websocket, agent, tool, args, **kwargs):
        async def effect(_capabilities):
            effects.append((tool, args))
            if isinstance(outcome[0], BaseException):
                raise outcome[0]
            return outcome[0]

        return await orch._execute_governed_attempt(websocket, agent, tool, args,
            user_id="owner-a", channel="web", audit_correlation_id="test", actor_user_id="owner-a",
            auth_principal="owner-a", conversation_id="chat-a", invoke=effect)

    orch._execute_with_retry = execute
    return orch, ws, effects, turns, proposals, outcome


async def _computer_task(task_runtime):
    orch, ws, *_ = task_runtime
    if not hasattr(orch, "_connection_contexts"):
        _registered_controller(orch, ws)
    fake = _FakeOrch()
    host, _ = fake.computer_hosts.register("owner-a", ws, _descriptor(platform="macos", name="Mac"))
    fake.ui_sessions[ws] = {"sub": "owner-a"}
    orch.computer_hosts = fake.computer_hosts
    orch.computer_sessions = fake.computer_sessions
    session = await fake.computer_sessions.start("owner-a", host, ws, "chat-a", wait_for_ack=False)
    return session


def _registered_controller(orch, ws):
    orch._connection_contexts = getattr(orch, "_connection_contexts", {})
    orch._registered_events = getattr(orch, "_registered_events", {})
    context = orch._new_connection_context(ws)
    context.registered = True
    context.connection_generation = uuid4()
    return context


@pytest.mark.parametrize("change", ["valid", "owner", "socket", "unsolicited", "unregistered", "retired",
    "closing", "flag", "over_cap", "missing_identity", "generation", "malformed", "no_owner"])
async def test_host_reply_control_path_retains_current_socket_owner_and_request_fences(task_runtime, monkeypatch, change):
    from orchestrator import computer_hosts
    from shared.feature_flags import flags

    orch, ws, effects, _turns, _proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime)
    context = SimpleNamespace(websocket=ws, closing=False, registered=True, connection_generation=uuid4())
    orch._connection_contexts = {id(ws): context}
    orch._tunnel_ingress_over_cap = lambda _owner: False
    monkeypatch.setitem(flags._flags, "computer_use", True)
    pending = asyncio.get_running_loop().create_future()
    orch.computer_hosts._pending["known"] = (pending, "owner-a", session.host_id, "type_text", session.session_id)
    value = {"type": "ui_event", "action": "computer_response", "payload": {
        "request_id": "known", "ok": True, "result": {"typed": True}},
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "connection_generation": str(context.connection_generation)}
    if change == "owner":
        orch.ui_sessions[ws]["sub"] = "owner-b"
    elif change == "socket":
        context.websocket = object()
        orch.ui_sessions[context.websocket] = {"sub": "owner-a"}
        orch._connection_contexts = {id(context.websocket): context}
    elif change == "unsolicited":
        value["payload"]["request_id"] = "unknown"
    elif change == "unregistered":
        context.registered = False
    elif change == "retired":
        orch._connection_contexts.clear()
    elif change == "closing":
        context.closing = True
    elif change == "flag":
        monkeypatch.setitem(flags._flags, "computer_use", False)
    elif change == "over_cap":
        monkeypatch.setattr(computer_hosts, "MAX_FRAME_BYTES", 16)
    elif change == "missing_identity":
        value.pop("submission_id")
    elif change == "generation":
        value["connection_generation"] = str(uuid4())
    elif change == "malformed":
        value["payload"] = []
    elif change == "no_owner":
        orch.ui_sessions.clear()
    await orch._route_ui_frame(context, json.dumps(value))
    assert pending.done() is (change == "valid") and not effects
    orch.computer_hosts._pending.clear()
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("event,changed", [("paused", True), ("heartbeat", False), ("unknown", False)])
async def test_host_event_control_path_uses_closed_events_and_selected_host(task_runtime, monkeypatch, event, changed):
    from shared.feature_flags import flags

    orch, ws, _effects, _turns, _proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime)
    context = SimpleNamespace(websocket=ws, closing=False, registered=True, connection_generation=uuid4())
    orch._connection_contexts = {id(ws): context}
    orch._tunnel_ingress_over_cap = lambda _owner: False
    monkeypatch.setitem(flags._flags, "computer_use", True)
    value = {"type": "ui_event", "action": "computer_event", "payload": {
        "event": event, "host_id": "wrong", "session_id": session.session_id, "reason": "local_input"},
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "connection_generation": str(context.connection_generation)}
    await orch._route_ui_frame(context, json.dumps(value))
    assert session.state == "active"
    value["payload"]["host_id"] = session.host_id
    await orch._route_ui_frame(context, json.dumps(value))
    assert (session.state == "paused") is changed
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["current", "other_chat", "other_controller", "other_owner", "no_owner", "no_session"])
async def test_generic_cancel_control_cancels_the_turn_and_keeps_the_computer_session(task_runtime, change):
    from unittest.mock import AsyncMock
    from orchestrator import orchestrator as module

    orch, ws, _effects, _turns, _proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime)
    orch.computer_sessions._orch = orch
    orch.ui_clients = [ws]
    orch._safe_send = AsyncMock(return_value=True)
    orch._connection_contexts = {}
    orch._registered_events = {}
    orch.handle_ui_message = module.Orchestrator.handle_ui_message.__get__(orch)
    context = orch._new_connection_context(ws)
    context.registered = True
    session.grant_terminal(180)
    if change == "other_chat":
        orch._ws_active_chat[id(ws)] = "chat-b"
    elif change == "other_controller":
        session.controller_ws_id = id(object())
    elif change == "other_owner":
        orch.ui_sessions[ws]["sub"] = "owner-b"
    elif change == "no_owner":
        orch.ui_sessions.clear()
    elif change == "no_session":
        await orch.computer_sessions.end(session, "user_stop")
        orch._safe_send.reset_mock()
    await orch._route_ui_frame(context, json.dumps({"type": "ui_event", "action": "cancel_task", "payload": {}}))
    await asyncio.gather(*context.tracked_tasks)
    ended = [json.loads(call.args[1]) for call in orch._safe_send.await_args_list
             if json.loads(call.args[1]).get("type") == "computer_session"]
    assert ended == []
    if change != "no_session":
        assert session.state == "active" and session.terminal_ok
    if change == "current":
        assert orch.cancelled_sessions[id(ws)] is True
        assert any(json.loads(call.args[1]).get("message") == "Cancelled"
                   for call in orch._safe_send.await_args_list)
    await orch.computer_sessions.end(session, "user_stop")
