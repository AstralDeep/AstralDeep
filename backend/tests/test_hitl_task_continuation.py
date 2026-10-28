"""Verifies exact owner approvals and continuation through the real gate and effect boundary.
Synthetic dispatch receivers record physical effects independently from UI responses.
"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import hitl_confirmation as hc, remote_confirmation as rc, source_bound
from agents.computer_use import mcp_tools
from orchestrator.source_bound import declaration as strict_declaration
from shared.protocol import MCPResponse
from tests.test_computer_use_076 import _descriptor, _FakeDB, _FakeOrch
from tests.helpers.remote_plane_runtime import make_remote_confirmation_plane_source
from tests.test_hitl_public_research import _decision, _dispatch, runtime  # noqa: F401
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

    async def chat(websocket, message, chat_id, display, **kwargs):
        turns.append(message)
        if message == "original":
            proposals.append(await _dispatch(runtime, args={"to": "a@example.org", "body": "safe"}))

    orch._execute_with_retry = execute
    orch.handle_chat_message = chat
    return orch, ws, effects, turns, proposals, outcome


async def test_successful_exact_approval_resumes_origin_once(task_runtime):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    decision = _decision(proposals[0])
    assert not effects
    await hc.handle_decision(orch, ws, "owner-a", decision)
    await asyncio.sleep(0)
    assert len(effects) == 1
    assert len(turns) == 2
    await hc.handle_decision(orch, ws, "owner-a", decision)
    await asyncio.sleep(0)
    assert len(effects) == 1 and len(turns) == 2


@pytest.mark.parametrize("change", ["new_task", "cancel", "owner", "chat", "expired", "decline"])
async def test_stale_or_declined_approval_has_no_effect_or_continuation(task_runtime, change):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    decision = _decision(proposals[0])
    if change == "new_task":
        await orch._serialized_chat(ws, "original", "chat-a", "same text new task", user_id="owner-a")
    elif change == "cancel":
        orch.cancelled_sessions[id(ws)] = True
    elif change == "owner":
        orch.ui_sessions[ws]["sub"] = "owner-b"
    elif change == "chat":
        orch._ws_active_chat[id(ws)] = "chat-b"
    elif change == "expired":
        key = decision["hitl_request_id"]
        orch._hitl_pending_calls[key] = replace(orch._hitl_pending_calls[key], expires=0)
    else:
        decision["decision"] = "decline"
    before = len(turns)
    await hc.handle_decision(orch, ws, "owner-a", decision)
    await asyncio.sleep(0)
    assert not effects and len(turns) == before


@pytest.mark.parametrize("result", [None, MCPResponse(error={"code": "outcome_uncertain"}),
    MCPResponse(result={"_data": {"code": "paused"}}),
    MCPResponse(result={"_data": {"status": "confirmation_required"}})])
async def test_failed_or_uncertain_effect_does_not_resume(task_runtime, result):
    orch, ws, effects, turns, proposals, outcome = task_runtime
    outcome[0] = result
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    await hc.handle_decision(orch, ws, "owner-a", _decision(proposals[0]))
    await asyncio.sleep(0)
    assert len(effects) == 1
    assert len(turns) == 1


async def test_new_task_invalidates_continuation_waiting_on_workspace_lock(task_runtime):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    lock = orch._workspace_locks["chat-a"]
    await lock.acquire()
    try:
        replacement = asyncio.create_task(orch._serialized_chat(ws, "replacement", "chat-a", "new", user_id="owner-a"))
        await asyncio.sleep(0)
        await hc.handle_decision(orch, ws, "owner-a", _decision(proposals[0]))
        await asyncio.sleep(0)
    finally:
        lock.release()
    await replacement
    assert len(effects) == 1
    assert turns == ["original", "replacement"]


async def test_queued_turn_cannot_donate_its_origin_to_running_turn(task_runtime):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def chat(websocket, message, chat_id, display, **kwargs):
        turns.append(message)
        if message == "first":
            entered.set()
            await release.wait()
            proposals.append(await _dispatch((orch, ws), args={"to": "a@example.org", "body": "safe"}))

    orch.handle_chat_message = chat
    first = asyncio.create_task(orch._serialized_chat(ws, "first", "chat-a", "first", user_id="owner-a"))
    await entered.wait()
    second = asyncio.create_task(orch._serialized_chat(ws, "second", "chat-a", "second", user_id="owner-a"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    await hc.handle_decision(orch, ws, "owner-a", _decision(proposals[0]))
    await asyncio.sleep(0)
    assert effects == [] and turns == ["first", "second"]


async def test_escalated_risk_requires_fresh_review_before_physical_effect(task_runtime, monkeypatch):
    from orchestrator import hitl

    orch, ws, effects, turns, proposals, _outcome = task_runtime
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    monkeypatch.setattr(hitl, "assess_risk", lambda *_args, **_kwargs: ["egress", "irreversible"])
    await hc.handle_decision(orch, ws, "owner-a", _decision(proposals[0]))
    await asyncio.sleep(0)
    assert not effects and len(turns) == 1
    assert any("irreversible" in call.risks for call in orch._hitl_pending_calls.values())


async def test_cancelled_physical_attempt_records_uncertain_outcome(task_runtime):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    original = orch._execute_with_retry

    async def cancelled(*args, **kwargs):
        async def effect(_capabilities):
            effects.append("started")
            raise asyncio.CancelledError

        return await orch._execute_governed_attempt(ws, "agent-a", "send_email", args[3],
            user_id="owner-a", channel="web", audit_correlation_id="test", actor_user_id="owner-a",
            auth_principal="owner-a", conversation_id="chat-a", invoke=effect)

    orch._execute_with_retry = cancelled
    try:
        with pytest.raises(asyncio.CancelledError):
            await hc.handle_decision(orch, ws, "owner-a", _decision(proposals[0]))
    finally:
        orch._execute_with_retry = original
    assert effects == ["started"] and len(turns) == 1
    assert any(call.args[0].action_type == "hitl.effect_uncertain"
               for call in orch.audit_recorder.record.await_args_list)


async def _computer_task(task_runtime, *, self_host=True, target=True, tool="type_text", arguments=None):
    orch, ws, _effects, _turns, proposals, _outcome = task_runtime
    if not hasattr(orch, "_connection_contexts"):
        _registered_controller(orch, ws)
    fake = _FakeOrch()
    host_socket = ws if self_host else object()
    host, _ = fake.computer_hosts.register("owner-a", host_socket, _descriptor(platform="macos", name="Mac"))
    fake.ui_sessions[ws] = {"sub": "owner-a"}
    orch.computer_hosts = fake.computer_hosts
    orch.computer_sessions = fake.computer_sessions
    session = await fake.computer_sessions.start("owner-a", host, ws, "chat-a", wait_for_ack=False)
    orch.agents["computer-use-1"] = object()
    orch.tool_permissions.get_tool_scope_map.return_value = {tool: "tools:write"}

    async def chat(websocket, message, chat_id, display, **kwargs):
        task_runtime[3].append(message)
        if message == "original":
            args = arguments if arguments is not None else {"text": "reviewed synthetic text", **({"hwnd": 42} if target else {})}
            proposals.append(await _dispatch((orch, ws), tool=tool, args=args, agent="computer-use-1"))

    orch.handle_chat_message = chat
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    return session


def _registered_controller(orch, ws):
    orch._connection_contexts = getattr(orch, "_connection_contexts", {})
    orch._registered_events = getattr(orch, "_registered_events", {})
    context = orch._new_connection_context(ws)
    context.registered = True
    context.connection_generation = uuid4()
    return context


async def test_self_host_targeted_review_explicitly_resumes_once(task_runtime):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime)
    card = proposals[0].ui_components[0]
    button = next(item for item in card["content"] if item.get("payload", {}).get("decision") == "approve_and_resume")
    assert "Mac" in button["label"] and "42" in button["label"]
    assert next(item for item in card["content"] if item["type"] == "button") is button
    assert not any(item.get("payload", {}).get("decision") == "approve" for item in card["content"])
    await orch.computer_sessions.pause(session, "local_input")
    await hc.handle_decision(orch, ws, "owner-a", button["payload"])
    await asyncio.sleep(0)
    assert session.state == "active" and len(effects) == 1 and len(turns) == 2
    await hc.handle_decision(orch, ws, "owner-a", button["payload"])
    assert len(effects) == 1
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["manual", "stop", "controller", "multiple", "no_target", "remote", "decline"])
async def test_combined_decision_cannot_resume_unbound_or_unsafe_pause(task_runtime, change):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime, self_host=change != "remote", target=change != "no_target")
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") in {"approve", "approve_and_resume"})
    decision = {**decision, "decision": "approve_and_resume"}
    await orch.computer_sessions.pause(session, "manual_pause" if change == "manual" else "local_input")
    if change == "stop":
        await orch.computer_sessions.end(session, "local_stop")
    elif change == "controller":
        session.controller_ws_id = id(object())
    elif change == "multiple":
        await orch.computer_sessions.resume(session, local=True)
        await orch.computer_sessions.pause(session, "local_input")
    elif change == "decline":
        decision["decision"] = "decline"
    await hc.handle_decision(orch, ws, "owner-a", decision)
    await asyncio.sleep(0)
    assert session.state != "active" and not effects and len(turns) == 1
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("failure", ["permission", "risk", "uncertain", "exception", "cancelled"])
async def test_combined_decision_failure_leaves_local_session_paused(task_runtime, monkeypatch, failure):
    from orchestrator import hitl

    orch, ws, effects, turns, proposals, outcome = task_runtime
    session = await _computer_task(task_runtime)
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve_and_resume")
    await orch.computer_sessions.pause(session, "local_input")
    if failure == "permission":
        orch.tool_permissions.is_tool_allowed.return_value = False
    elif failure == "risk":
        monkeypatch.setattr(hitl, "assess_risk", lambda *_args, **_kwargs: ["irreversible"])
    else:
        outcome[0] = (RuntimeError("synthetic failure") if failure == "exception" else
                      asyncio.CancelledError() if failure == "cancelled" else
                      MCPResponse(error={"code": "outcome_uncertain"}))
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await hc.handle_decision(orch, ws, "owner-a", decision)
    else:
        await hc.handle_decision(orch, ws, "owner-a", decision)
    await asyncio.sleep(0)
    assert session.state == "paused" and len(turns) == 1 and not session.terminal_ok
    assert len(effects) == (1 if failure in {"uncertain", "exception", "cancelled"} else 0)
    if failure == "risk":
        assert not orch._hitl_pending_calls
        rendered = str(orch.send_ui_render.await_args_list)
        assert "Resume" in rendered and "reissue" in rendered
    await orch.computer_sessions.end(session, "user_stop")


async def test_stale_continuation_does_not_remove_another_turn_recorder(task_runtime):
    orch, ws, _effects, _turns, _proposals, _outcome = task_runtime
    recorder = object()
    orch._chat_recorders[id(ws)] = recorder
    await orch._serialized_chat(ws, "stale", "chat-a", "stale", user_id="owner-a", continuation_guard=lambda: False)
    assert orch._chat_recorders[id(ws)] is recorder


async def test_evicted_task_origin_cannot_reactivate_pending_effect(task_runtime):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    for index in range(256):
        hc.begin_task(orch, ws, "owner-a", f"other-{index}")
    await hc.handle_decision(orch, ws, "owner-a", _decision(proposals[0]))
    assert not effects and len(turns) == 1


async def test_host_loss_during_explicit_resume_cannot_dispatch(task_runtime, monkeypatch):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime)
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve_and_resume")
    original = orch.computer_sessions.resume

    async def resume(*args, **kwargs):
        await original(*args, **kwargs)
        orch.computer_hosts.withdraw("owner-a", session.host_id)

    monkeypatch.setattr(orch.computer_sessions, "resume", resume)
    await orch.computer_sessions.pause(session, "local_input")
    await hc.handle_decision(orch, ws, "owner-a", decision)
    assert not effects and len(turns) == 1
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["host", "session"])
async def test_explicit_resume_cannot_adopt_replacement_authority(task_runtime, monkeypatch, change):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime)
    host = orch.computer_hosts.get("owner-a", session.host_id)
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve_and_resume")
    original = orch.computer_sessions.resume

    async def resume(*args, **kwargs):
        await original(*args, **kwargs)
        if change == "host":
            orch.computer_hosts.register("owner-a", ws, _descriptor(platform="macos", host_id=host.host_id))
        else:
            await orch.computer_sessions.end(session, "local_stop")
            await orch.computer_sessions.start("owner-a", host, ws, "chat-a", wait_for_ack=False)

    monkeypatch.setattr(orch.computer_sessions, "resume", resume)
    await orch.computer_sessions.pause(session, "local_input")
    await hc.handle_decision(orch, ws, "owner-a", decision)
    assert not effects and turns == ["original"]
    await orch.computer_sessions.end_all("user_stop")


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_interrupted_remote_combined_decision_repauses_session(task_runtime, monkeypatch, failure):
    orch, ws, _effects, _turns, proposals, _outcome = task_runtime
    source = make_remote_confirmation_plane_source(_FakeDB())
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=source.plane_runtime, repositories=source.plane_repositories))
    session = await _computer_task(task_runtime, tool="open_app", arguments={"app": "Terminal"})
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve_and_resume")
    await orch.computer_sessions.pause(session, "local_input")

    async def interrupted(*_args, **_kwargs):
        raise failure()

    monkeypatch.setattr(orch, "execute_single_tool", interrupted)
    with pytest.raises(failure):
        await rc.handle_decision(orch, ws, "owner-a", decision)
    assert session.state == "paused" and not session.terminal_ok
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("combined", [False, True])
async def test_terminal_launch_passes_remote_and_source_review_once_before_grant(task_runtime, combined):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    source = make_remote_confirmation_plane_source(_FakeDB())
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=source.plane_runtime, repositories=source.plane_repositories))

    async def publish(*_args, **_kwargs):
        return []

    orch._send_or_replace_components = publish
    session = await _computer_task(task_runtime, self_host=combined, tool="open_app", arguments={"app": "com.apple.Terminal"})
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == ("approve_and_resume" if combined else "approve"))
    if combined:
        await orch.computer_sessions.pause(session, "local_input")
    await rc.handle_decision(orch, ws, "owner-a", decision)
    assert not effects and not session.terminal_ok
    pending = next(iter(orch._hitl_pending_calls.values()))
    if combined:
        await orch.computer_sessions.pause(session, "local_input")
    await hc.handle_decision(orch, ws, "owner-a", {"hitl_request_id": pending.request_id,
                                                "decision": "approve_and_resume" if combined else "approve"})
    await asyncio.sleep(0)
    assert len(effects) == 1 and session.terminal_ok and len(turns) == 2
    assert not orch._hitl_pending_calls
    await orch.computer_sessions.end(session, "user_stop")


async def test_exact_terminal_keyboard_review_issues_one_targeted_input_permit(task_runtime, monkeypatch):
    orch, ws, _effects, turns, proposals, _outcome = task_runtime
    args = {"text": "reviewed command", "hwnd": 42, "terminal_app": "Terminal"}
    session = await _computer_task(task_runtime, arguments=args)
    physical = []
    host = orch.computer_hosts.get("owner-a", session.host_id)
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: ("owner-a", "chat-a", host, session))
    monkeypatch.setattr(mcp_tools, "_run", lambda _host, _session, verb, payload, _loop:
                        (physical.append(payload) or {}, None))

    async def execute(websocket, agent, tool, dispatched, **kwargs):
        async def effect(_capabilities):
            changed = mcp_tools.type_text(**{**dispatched, "hwnd": 43})
            assert changed["_data"]["code"] == "confirmation_required"
            first = await asyncio.to_thread(mcp_tools.type_text, **dispatched)
            replay = mcp_tools.type_text(**dispatched)
            assert replay["_data"]["code"] == "confirmation_required"
            return MCPResponse(result={"_data": first["_data"]})

        return await orch._execute_governed_attempt(websocket, agent, tool, dispatched,
            user_id="owner-a", channel="web", audit_correlation_id="test", actor_user_id="owner-a",
            auth_principal="owner-a", conversation_id="chat-a", invoke=effect)

    orch._execute_with_retry = execute
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve_and_resume")
    await orch.computer_sessions.pause(session, "local_input")
    await hc.handle_decision(orch, ws, "owner-a", decision)
    await asyncio.sleep(0)
    assert physical == [{"text": "reviewed command", "hwnd": 42,
                         "terminal_app": "com.apple.Terminal", "terminal_ok": True}]
    assert not session.terminal_ok and len(turns) == 2
    await orch.computer_sessions.end(session, "user_stop")


async def test_effect_worker_cannot_use_authority_changed_before_its_queue_capture(task_runtime, monkeypatch):
    orch, ws, _effects, turns, proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime, self_host=False)
    host = orch.computer_hosts.get("owner-a", session.host_id)
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: ("owner-a", "chat-a", host, session))
    physical = []

    async def request(*_args):
        physical.append(True)
        return {}

    monkeypatch.setattr(orch.computer_hosts, "request", request)
    execution_loop = asyncio.get_running_loop()
    original = mcp_tools._run

    def changed(host, active_session, verb, payload, loop=None):
        session.authority_generation += 1
        return original(host, active_session, verb, payload, loop=execution_loop)

    monkeypatch.setattr(mcp_tools, "_run", changed)

    async def execute(websocket, agent, tool, dispatched, **kwargs):
        async def effect(_capabilities):
            result = await asyncio.to_thread(mcp_tools.type_text, **dispatched)
            assert result["_data"]["code"] == "no_session"
            return MCPResponse(error={"code": result["_data"]["code"], "retryable": False})

        return await orch._execute_governed_attempt(websocket, agent, tool, dispatched,
            user_id="owner-a", channel="web", audit_correlation_id="test", actor_user_id="owner-a",
            auth_principal="owner-a", conversation_id="chat-a", invoke=effect)

    orch._execute_with_retry = execute
    await hc.handle_decision(orch, ws, "owner-a", next(item["payload"] for item in proposals[0].ui_components[0]["content"]
        if item.get("payload", {}).get("decision") == "approve"))
    assert not physical and len(turns) == 1
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["cancel", "task", "chat", "expired", "owner", "host", "controller",
    "closing", "unregistered", "retired", "replaced", "connection_generation"])
async def test_queued_approved_input_rechecks_authority_before_physical_send(task_runtime, monkeypatch, change):
    orch, ws, _effects, turns, proposals, _outcome = task_runtime
    context = _registered_controller(orch, ws)
    session = await _computer_task(task_runtime, self_host=False)
    host = orch.computer_hosts.get("owner-a", session.host_id)
    entered = asyncio.Event()
    physical = []
    loop = asyncio.get_running_loop()
    original_bridge = mcp_tools._bridge
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: ("owner-a", "chat-a", host, session))

    def bridge(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        return original_bridge(*args, **kwargs)

    async def request(*_args):
        physical.append(True)
        return {}

    async def execute(websocket, agent, tool, dispatched, **kwargs):
        async def effect(_capabilities):
            result = await asyncio.to_thread(mcp_tools.type_text, _runtime=SimpleNamespace(loop=loop), **dispatched)
            return MCPResponse(error={"code": result["_data"].get("code")}) if result["_data"].get("code") else MCPResponse(result=result)

        return await orch._execute_governed_attempt(websocket, agent, tool, dispatched,
            user_id="owner-a", channel="web", audit_correlation_id="test", actor_user_id="owner-a",
            auth_principal="owner-a", conversation_id="chat-a", invoke=effect)

    monkeypatch.setattr(mcp_tools, "_bridge", bridge)
    monkeypatch.setattr(orch.computer_hosts, "request", request)
    orch._execute_with_retry = execute
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve")
    await session.lock.acquire()
    pending = asyncio.create_task(hc.handle_decision(orch, ws, "owner-a", decision))
    try:
        await entered.wait()
        if change == "cancel":
            orch.cancelled_sessions[id(ws)] = True
        elif change == "task":
            hc.begin_task(orch, ws, "owner-a", "chat-a")
        elif change == "chat":
            orch._ws_active_chat[id(ws)] = "chat-b"
        elif change == "expired":
            monkeypatch.setattr(hc, "time", SimpleNamespace(monotonic=lambda: float("inf")))
        elif change == "owner":
            orch.ui_sessions[ws]["sub"] = "owner-b"
        elif change == "host":
            orch.computer_hosts.register("owner-a", object(), _descriptor(platform="macos", host_id=host.host_id))
        elif change == "controller":
            session.controller_ws_id = id(object())
        elif change == "closing":
            context.closing = True
        elif change == "unregistered":
            context.registered = False
        elif change == "retired":
            orch._connection_contexts.pop(id(ws))
        elif change == "replaced":
            replacement = _registered_controller(orch, ws)
            replacement.connection_generation = context.connection_generation
        else:
            context.connection_generation = uuid4()
    finally:
        session.lock.release()
    await pending
    assert not physical and turns == ["original"]
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["closing", "unregistered", "retired", "replaced", "generation"])
async def test_direct_approval_requires_original_live_controller_context(task_runtime, change):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    context = _registered_controller(orch, ws)
    session = await _computer_task(task_runtime, self_host=False)
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve")
    if change == "closing":
        context.closing = True
    elif change == "unregistered":
        context.registered = False
    elif change == "retired":
        orch._connection_contexts.pop(id(ws))
    elif change == "replaced":
        replacement = _registered_controller(orch, ws)
        replacement.connection_generation = context.connection_generation
    else:
        context.connection_generation = uuid4()
    await hc.handle_decision(orch, ws, "owner-a", decision)
    assert not effects and turns == ["original"]
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("change", ["expired", "unconsumed", "fingerprint"])
async def test_source_review_cannot_reuse_invalid_remote_authorization(task_runtime, change):
    orch, ws, effects, turns, proposals, _outcome = task_runtime
    database = _FakeDB()
    source = make_remote_confirmation_plane_source(database)
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=source.plane_runtime, repositories=source.plane_repositories))

    async def publish(*_args, **_kwargs):
        return []

    orch._send_or_replace_components = publish
    session = await _computer_task(task_runtime, self_host=False, tool="open_app", arguments={"app": "Terminal"})
    decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                    if item.get("payload", {}).get("decision") == "approve")
    await rc.handle_decision(orch, ws, "owner-a", decision)
    pending = next(iter(orch._hitl_pending_calls.values()))
    row = database.rows[decision["proposal_id"]]
    if change == "expired":
        row["expires_at"] = 0
    elif change == "unconsumed":
        row["status"] = "approved"
        row["consumed_at"] = None
    else:
        row["args_fingerprint"] = "0" * 64
    await hc.handle_decision(orch, ws, "owner-a", {"hitl_request_id": pending.request_id, "decision": "approve"})
    assert not effects and len(turns) == 1 and not session.terminal_ok
    await orch.computer_sessions.end(session, "user_stop")


async def test_missing_computer_session_refuses_source_proposal_before_effect(task_runtime):
    orch, _ws, effects, _turns, _proposals, _outcome = task_runtime
    session = await _computer_task(task_runtime)
    await orch.computer_sessions.end(session, "local_stop")
    result = await _dispatch((orch, _ws), tool="type_text", args={"text": "different", "hwnd": 42}, agent="computer-use-1")
    assert result.error and "session" in result.error["message"] and not effects


@pytest.mark.parametrize("cancel", [False, True])
async def test_self_host_approval_reply_completes_through_actual_socket_admission(task_runtime, monkeypatch, cancel):
    from agents.computer_use.mcp_server import MCPServer
    from orchestrator import orchestrator as module
    from tests.perf.test_runtime_reliability_060 import _MessageProbe, _orchestrator, _socket
    from shared.feature_flags import flags
    from shared.protocol import MCPRequest

    orch, _old, effects, turns, proposals, outcome = task_runtime
    baseline = _orchestrator(module, _MessageProbe())
    for key, value in vars(baseline).items():
        if key not in vars(orch) and key != "handle_ui_message":
            setattr(orch, key, value)
    ws = _socket(module)
    ws.user = "owner-a"
    orch.ui_clients = [ws]
    orch.ui_sessions = {ws: {"sub": "owner-a"}}
    orch._ws_active_chat = {id(ws): "chat-a"}
    session = None
    loop = asyncio.get_running_loop()
    completed, registered, ingress_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    physical = []
    pending_request = []
    generation = str(uuid4())
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setitem(flags._flags, "computer_use", True)
    orch._tunnel_ingress_over_cap = lambda _owner: False

    def frame(action, payload):
        return json.dumps({"type": "ui_event", "action": action, "payload": payload,
            "submission_id": str(uuid4()), "request_generation": str(uuid4()),
            "connection_generation": generation})

    async def send(socket, raw):
        socket.sent.append(raw)
        value = json.loads(raw)
        if value.get("type") == "pong":
            ingress_ready.set()
        if value.get("type") == "computer_request":
            physical.append((value["verb"], value["args"]))
            pending_request.append(value["request_id"])
            socket.feed(frame("cancel_task", {}) if cancel else frame("computer_response", {
                "request_id": value["request_id"], "ok": True, "result": {"typed": True}}))
        elif cancel and value.get("type") == "chat_status" and value.get("message") == "Cancelled" and pending_request:
            socket.feed(frame("computer_response", {"request_id": pending_request.pop(),
                "ok": False, "error": {"code": "outcome_uncertain", "message": "Stopped after delivery"}}))
        return True

    async def execute(websocket, agent, tool, dispatched, **kwargs):
        async def effect(_capabilities):
            return await asyncio.to_thread(MCPServer().process_request, MCPRequest(method="tools/call", params={
                "name": tool, "arguments": {**dispatched, "user_id": "owner-a", "session_id": "chat-a",
                                             "_runtime": SimpleNamespace(loop=loop)}}))

        return await orch._execute_governed_attempt(websocket, agent, tool, dispatched,
            user_id="owner-a", channel="web", audit_correlation_id="test", actor_user_id="owner-a",
            auth_principal="owner-a", conversation_id="chat-a", invoke=effect)

    async def handle(socket, raw):
        value = json.loads(raw)
        if value["type"] == "register_ui":
            orch._registered_events[id(socket)].set()
            registered.set()
            return
        await module.Orchestrator.handle_ui_message(orch, socket, raw)
        if value.get("action") == "authorize_action":
            completed.set()

    orch._safe_send = send
    orch._execute_with_retry = execute
    orch.handle_ui_message = handle
    serve = asyncio.create_task(orch._serve_ui_frames(ws, ws.receive_text))
    try:
        ws.feed(json.dumps({"type": "register_ui", "connection_generation": generation}))
        await asyncio.wait_for(registered.wait(), 5)
        ws.feed(json.dumps({"type": "ping"}))
        await asyncio.wait_for(ingress_ready.wait(), 5)
        session = await _computer_task((orch, ws, effects, turns, proposals, outcome))
        orch.computer_hosts._orch = orch
        orch.computer_sessions._orch = orch
        decision = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                        if item.get("payload", {}).get("decision") == "approve_and_resume")
        ws.feed(frame("computer_event", {"event": "paused", "reason": "local_input", "session_id": session.session_id}))
        ws.feed(frame("authorize_action", decision))
        await asyncio.wait_for(completed.wait(), 5)
        await asyncio.sleep(0)
        assert physical == [("type_text", {"text": "reviewed synthetic text", "hwnd": 42, "terminal_ok": False})]
        if cancel:
            assert turns == ["original"] and session.state == "active" and not session.terminal_ok
            assert any(call.args[0].action_type == "hitl.effect_uncertain"
                       for call in orch.audit_recorder.record.await_args_list)
            assert "inspect current state" in str(orch.send_ui_render.await_args_list).lower()
        else:
            assert turns[-1] != "original" and len(turns) == 2
        assert not orch.computer_hosts._pending
        context = orch._connection_contexts[id(ws)]
        assert any(work.frame.action == "authorize_action" for work in context.operations.values())
    finally:
        if session is not None:
            await orch.computer_sessions.end(session, "user_stop")
        ws.disconnect()
        try:
            await serve
        except module.WebSocketDisconnect:
            pass


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


@pytest.mark.parametrize("origin", [False, True])
async def test_cancelled_task_approval_cannot_reactivate_when_socket_cancel_flag_resets(task_runtime, origin):
    from unittest.mock import AsyncMock
    from orchestrator import orchestrator as module

    orch, ws, effects, _turns, proposals, _outcome = task_runtime
    if origin:
        await orch._serialized_chat(ws, "original", "chat-a", "original", user_id="owner-a")
    else:
        proposals.append(await _dispatch((orch, ws), args={"to": "a@example.org", "body": "safe"}))
    orch._safe_send = AsyncMock(return_value=True)
    orch._connection_contexts = {}
    orch._registered_events = {}
    orch.handle_ui_message = module.Orchestrator.handle_ui_message.__get__(orch)
    context = orch._new_connection_context(ws)
    context.registered = True
    await orch._route_ui_frame(context, json.dumps({"type": "ui_event", "action": "cancel_task", "payload": {}}))
    await asyncio.gather(*context.tracked_tasks)
    orch.cancelled_sessions[id(ws)] = False
    await hc.handle_decision(orch, ws, "owner-a", _decision(proposals[0]))
    assert not effects


@pytest.mark.parametrize("remote", [False, True])
async def test_same_mac_decline_retires_expected_takeover_without_resuming(task_runtime, remote):
    from unittest.mock import AsyncMock

    orch, ws, effects, turns, proposals, _outcome = task_runtime
    database = _FakeDB()
    if remote:
        source = make_remote_confirmation_plane_source(database)
        orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
            runtime=source.plane_runtime, repositories=source.plane_repositories))
        orch._send_or_replace_components = AsyncMock(return_value=[])
    session = await _computer_task(task_runtime, tool="open_app" if remote else "type_text",
        arguments={"app": "Terminal"} if remote else None)
    payload = next(item["payload"] for item in proposals[0].ui_components[0]["content"]
                   if item.get("payload", {}).get("decision") == "approve_and_resume")
    payload = {**payload, "decision": "decline"}
    await orch.computer_sessions.pause(session, "local_input")
    if remote:
        await rc.handle_decision(orch, ws, "owner-a", payload)
        assert database.rows[payload["proposal_id"]]["status"] == "declined"
    else:
        await hc.handle_decision(orch, ws, "owner-a", payload)
        assert any(call.args[0].action_type == "hitl.declined" for call in orch.audit_recorder.record.await_args_list)
        assert "Declined" in str(orch.send_ui_render.await_args_list)
    assert session.state == "paused" and not effects and turns == ["original"] and not session.terminal_ok
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("transition", ["approved", "consumed", "effect_started"])
async def test_cancel_witness_survives_pending_pop_and_new_turn_reset(task_runtime, monkeypatch, transition):
    from unittest.mock import AsyncMock
    from orchestrator import orchestrator as module

    orch, ws, effects, _turns, _proposals, _outcome = task_runtime
    response = await _dispatch((orch, ws), args={"to": "a@example.org", "body": "safe"})
    entered, release = asyncio.Event(), asyncio.Event()
    original = hc._audit

    async def audit(orchestrator, call, stage):
        if stage == transition:
            entered.set()
            await release.wait()
        await original(orchestrator, call, stage)

    monkeypatch.setattr(hc, "_audit", audit)
    approve = asyncio.create_task(hc.handle_decision(orch, ws, "owner-a", _decision(response)))
    await entered.wait()
    assert not orch._hitl_pending_calls
    orch._safe_send = AsyncMock(return_value=True)
    context = _registered_controller(orch, ws)
    orch.handle_ui_message = module.Orchestrator.handle_ui_message.__get__(orch)
    try:
        await orch._route_ui_frame(context, json.dumps({"type": "ui_event", "action": "cancel_task", "payload": {}}))
        await asyncio.gather(*context.tracked_tasks)
        orch.cancelled_sessions[id(ws)] = False
        hc.begin_task(orch, ws, "owner-a", "chat-a")
    finally:
        release.set()
    await approve
    assert not effects
