"""Tests for orchestrator/computer_sessions.py and the host descriptor in shared/protocol.py: local
pause resume refusal, grant revocation on every authority change, approval activity, Stop turn
cancellation and the optional workspace_roots descriptor field.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.computer_use import mcp_tools
from orchestrator import computer_grants as cg
from orchestrator.computer_hosts import ComputerHostError
from shared.protocol import ComputerHostDescriptor, ProtocolValidationError
from tests.test_computer_use_076 import OWNER, _descriptor, _FakeOrch, _online_host, _session, _WS

ROOT = {"name": "x", "path": "/Users/sam/Projects/x"}


class _Recorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)
        return object()

    def record_blocking(self, event):
        self.events.append(event)
        return object()

    def actions(self):
        return [event.action_type for event in self.events]


@pytest.fixture
def recorder(monkeypatch):
    from audit import recorder as module

    fake = _Recorder()
    monkeypatch.setattr(module, "get_recorder", lambda: fake)
    return fake


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _started(orch=None, chat="chat-1"):
    orch = orch or _FakeOrch()
    _host_ws, host = await _online_host(orch)
    controller = orch.add(_WS(chat=chat))
    session = await _session(orch, host, controller, chat=chat)
    return orch, host, controller, session


def _grant(orch, session, host, scope=None, chat="chat-1"):
    return orch.computer_sessions.grants.create(
        OWNER, host.host_id, session.session_id, chat, "task-1",
        scope or {"kind": "app", "bundle_id": "com.apple.TextEdit"}, ("input",), now=1_800_000_000.0)


async def test_local_pause_refuses_remote_resume_until_the_host_resumes(recorder):
    orch, host, _controller, session = await _started()
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, "local_pause")
    with pytest.raises(ComputerHostError) as refused:
        await orch.computer_sessions.resume(session)
    assert refused.value.code == "local_pause_requires_local_resume"
    assert session.state == "paused" and session.pause_reason == "local_pause"
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "resumed", session.session_id, None)
    assert session.state == "active"
    await orch.computer_sessions.end(session, "user_stop")


async def test_local_input_pause_refuses_remote_resume_and_audits_the_refusal(recorder):
    orch, host, _controller, session = await _started()
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, "local_input")
    generation = session.authority_generation
    with pytest.raises(ComputerHostError) as refused:
        await orch.computer_sessions.resume(session)
    assert refused.value.code == "local_resume_required"
    assert session.state == "paused" and session.pause_reason == "local_input"
    assert session.authority_generation == generation
    refusal = [e for e in recorder.events if e.action_type == "computer_session.resume_refused"]
    assert len(refusal) == 1 and refusal[0].outcome == "failure"
    assert refusal[0].inputs_meta["reason"] == "local_resume_required"
    assert refusal[0].correlation_id == session.session_id and refusal[0].conversation_id == "chat-1"
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "resumed", session.session_id, None)
    assert session.state == "active" and session.pause_reason is None
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("reason", ["user_pause", "approval_not_completed", "manual_pause"])
async def test_remote_pause_reasons_still_resume_remotely(reason):
    orch, _host, _controller, session = await _started()
    await orch.computer_sessions.pause(session, reason)
    await orch.computer_sessions.resume(session)
    assert session.state == "active"
    await orch.computer_sessions.end(session, "user_stop")


async def test_local_pause_refusal_is_audited_too(recorder):
    orch, _host, _controller, session = await _started()
    await orch.computer_sessions.pause(session, "local_pause")
    with pytest.raises(ComputerHostError):
        await orch.computer_sessions.resume(session)
    refusal = [e for e in recorder.events if e.action_type == "computer_session.resume_refused"]
    assert [e.inputs_meta["reason"] for e in refusal] == ["local_pause_requires_local_resume"]
    await orch.computer_sessions.end(session, "user_stop")


async def test_resume_session_tool_reports_the_local_input_refusal(monkeypatch):
    orch, _host, _controller, session = await _started()
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    await orch.computer_sessions.pause(session, "local_input")
    result = await asyncio.to_thread(mcp_tools.resume_session, user_id=OWNER, session_id="chat-1")
    assert result["_data"]["code"] == "local_resume_required"
    _assert_terminal(result)
    assert session.state == "paused"
    await orch.computer_sessions.end(session, "user_stop")


def _assert_terminal(result):
    alert, = result["_ui_components"]
    assert alert["variant"] == "error" and "Stop now and end your turn" in alert["message"]
    assert "press Resume" in result["_data"]["next_action"]
    assert "do not call resume_session, wait or any other computer tool" in result["_data"]["next_action"]


async def _locally_paused(monkeypatch, reason="local_input"):
    orch, host, controller, session = await _started()
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, reason)
    requests = []

    async def request(*args, **kwargs):
        requests.append(args)
        raise AssertionError("a locally paused session must not reach the host")
    monkeypatch.setattr(orch.computer_hosts, "request", request)
    return orch, host, session, requests


@pytest.mark.parametrize("reason", ["local_input", "local_pause"])
@pytest.mark.parametrize("verb,args", [("screenshot", {}), ("list_windows", {}), ("wait", {"seconds": 5}),
                                       ("click", {"x": 1, "y": 2}), ("read_file", {"path": "/tmp/x"})])
async def test_every_verb_on_a_locally_paused_session_is_a_terminal_refusal(monkeypatch, reason, verb, args):
    orch, _host, session, requests = await _locally_paused(monkeypatch, reason)
    result = await asyncio.to_thread(getattr(mcp_tools, verb), user_id=OWNER, session_id="chat-1", **args)
    assert result["_data"]["code"] == "local_resume_required" and requests == []
    _assert_terminal(result)
    await orch.computer_sessions.end(session, "user_stop")


async def test_start_session_on_a_locally_paused_session_is_a_terminal_refusal(monkeypatch):
    orch, _host, session, _requests = await _locally_paused(monkeypatch)
    monkeypatch.setattr(orch, "socket_for_chat", lambda user, chat: orch.ui_clients[1], raising=False)
    result = await asyncio.to_thread(mcp_tools.start_session, user_id=OWNER, session_id="chat-1")
    assert result["_data"]["code"] == "local_resume_required"
    _assert_terminal(result)
    await orch.computer_sessions.end(session, "user_stop")


async def test_the_dispatcher_does_not_retry_a_local_pause_refusal(monkeypatch):
    from types import SimpleNamespace

    from agents.computer_use.mcp_server import MCPServer
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPRequest

    orch, _host, session, requests = await _locally_paused(monkeypatch)
    server, calls = MCPServer(), []

    async def execute_tool_and_wait(agent, tool, args, **kwargs):
        calls.append(tool)
        request = MCPRequest(request_id="r", method="tools/call", params={"name": tool, "arguments": args})
        return await asyncio.to_thread(server.process_request, request)
    dispatcher = SimpleNamespace(MAX_RETRIES=3, RETRY_BACKOFF=[0.0, 0.0], execute_tool_and_wait=execute_tool_and_wait,
                                 _protected_dispatch_channel=lambda websocket, explicit=None: "web")
    response = await Orchestrator._execute_with_retry_loop(
        dispatcher, None, "computer-use-1", "screenshot", {"user_id": OWNER, "session_id": "chat-1"},
        user_id=OWNER, audit_conversation_id="chat-1")
    assert calls == ["screenshot"] and requests == []
    assert response.error["retryable"] is False
    assert response.error["message"].startswith("local_resume_required:") and "press Resume" in response.error["message"]
    await orch.computer_sessions.end(session, "user_stop")


async def test_a_repeated_host_pause_is_acknowledged_on_the_session_frame(recorder):
    orch, host, controller, session = await _started()
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, "local_input")
    host_socket = orch.ui_clients[0]
    before = len(host_socket.sent)
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, "local_input")
    frames = [json.loads(frame) for frame in host_socket.sent[before:]]
    assert [(f["type"], f["session_id"], f["state"], f["pause_reason"]) for f in frames] == [
        ("computer_session", session.session_id, "paused", "local_input")]
    assert [e.action_type for e in recorder.events].count("computer_session.paused") == 1
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("reported", [None, "user_pause", "something_else"])
async def test_a_host_reported_pause_is_always_local(reported):
    orch, host, _controller, session = await _started()
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "paused", session.session_id, reported)
    assert session.pause_reason == "local_input"
    with pytest.raises(ComputerHostError):
        await orch.computer_sessions.resume(session)
    await orch.computer_sessions.end(session, "user_stop")


async def test_resume_session_tool_reports_the_local_pause_refusal(monkeypatch):
    orch, host, _controller, session = await _started()
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    await orch.computer_sessions.pause(session, "local_pause")
    result = await asyncio.to_thread(mcp_tools.resume_session, user_id=OWNER, session_id="chat-1")
    assert result["_data"]["code"] == "local_pause_requires_local_resume"
    assert session.state == "paused"
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("reason,want", [("local_input", "takeover"), ("local_pause", "pause"),
                                         ("user_pause", "pause"), ("approval_not_completed", "pause")])
async def test_every_pause_revokes_the_session_grants(recorder, reason, want):
    orch, host, _controller, session = await _started()
    _grant(orch, session, host)
    await orch.computer_sessions.pause(session, reason)
    await _settle()
    assert orch.computer_sessions.grants.list(session.session_id) == ()
    revoked = [e for e in recorder.events if e.action_type == cg.EVENT_REVOKED]
    assert revoked and revoked[-1].inputs_meta["reason"] == want
    assert revoked[-1].correlation_id == session.session_id
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("reason,want", [("user_stop", "stop"), ("local_stop", "stop"),
    ("host_superseded", "host_replaced"), ("host_offline", "socket_lost"), ("host_silent", "socket_lost"),
    ("consent_revoked", "authority_changed"), ("idle_timeout", "session_ended"), ("max_duration", "session_ended")])
async def test_session_end_revokes_grants_with_the_mapped_reason(recorder, reason, want):
    orch, host, _controller, session = await _started()
    _grant(orch, session, host)
    await orch.computer_sessions.end(session, reason)
    await _settle()
    assert orch.computer_sessions.grants.list(session.session_id) == ()
    revoked = [e for e in recorder.events if e.action_type == cg.EVENT_REVOKED]
    assert [e.inputs_meta["reason"] for e in revoked] == [want]


async def test_controller_takeover_and_chat_change_revoke_grants(recorder):
    orch, host, controller, session = await _started()
    _grant(orch, session, host)
    await orch.computer_sessions.start(OWNER, host, controller, "chat-2", wait_for_ack=False)
    assert orch.computer_sessions.grants.list(session.session_id) == ()
    _grant(orch, session, host, chat="chat-2")
    orch.ui_clients.remove(controller)
    await orch.computer_sessions.start(OWNER, host, orch.add(_WS(chat="chat-3")), "chat-3", wait_for_ack=False)
    await _settle()
    reasons = [e.inputs_meta["reason"] for e in recorder.events if e.action_type == cg.EVENT_REVOKED]
    assert reasons == ["chat_changed", "authority_changed"]
    await orch.computer_sessions.end(session, "user_stop")


async def test_grant_audit_carries_bounded_metadata_without_full_paths(recorder):
    orch, host, _controller, session = await _started()
    grant = orch.computer_sessions.grants.create(
        OWNER, host.host_id, session.session_id, "chat-1", "task-1", {"kind": "command"}, ("command",),
        {"argv0": "wc", "root": ROOT["path"]}, now=1_800_000_000.0)
    orch.computer_sessions.grants.touch(grant.grant_id, now=1_800_000_001.0, verb="run_command",
                                         target={"argv0": "wc", "cwd": ROOT["path"] + "/notes"})
    await _settle()
    created, used = recorder.events[-2:]
    assert (created.action_type, used.action_type) == (cg.EVENT_CREATED, cg.EVENT_USED)
    assert used.inputs_meta["verb"] == "run_command" and used.inputs_meta["argv0"] == "wc"
    assert used.inputs_meta["folder"] == "notes" and ROOT["path"] not in str(used.inputs_meta)
    assert created.inputs_meta["scope"] == "command" and created.conversation_id == "chat-1"
    await orch.computer_sessions.end(session, "user_stop")


async def test_grant_audit_outside_a_loop_records_synchronously(recorder):
    orch, host, _controller, session = await _started()
    await asyncio.to_thread(_grant, orch, session, host)
    assert recorder.actions()[-1] == cg.EVENT_CREATED
    await orch.computer_sessions.end(session, "user_stop")


async def test_approval_activity_is_recorded_without_pausing(recorder):
    orch, host, _controller, session = await _started()
    _grant(orch, session, host)
    generation = session.authority_generation
    await orch.computer_sessions.on_host_event(OWNER, host.host_id, "approval_activity", session.session_id, None)
    assert session.state == "active" and session.authority_generation == generation
    assert "computer_session.approval_activity" in recorder.actions()
    assert "computer_session.paused" not in recorder.actions()
    assert len(orch.computer_sessions.grants.list(session.session_id)) == 1
    await orch.computer_sessions.end(session, "user_stop")


@pytest.mark.parametrize("reason,cancelled", [("user_stop", True), ("local_stop", True), ("host_offline", False)])
async def test_stop_cancels_the_controllers_turn(reason, cancelled):
    orch, _host, controller, session = await _started()
    calls = []

    async def cancel(stopped):
        calls.append(stopped)

    orch.cancel_computer_turn = cancel
    await orch.computer_sessions.end(session, reason)
    assert calls == ([session] if cancelled else [])


async def test_agent_end_session_does_not_cancel_its_own_turn(monkeypatch):
    orch, _host, _controller, session = await _started()
    calls = []

    async def cancel(stopped):
        calls.append(stopped)

    orch.cancel_computer_turn = cancel
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    result = await asyncio.to_thread(mcp_tools.end_session, user_id=OWNER, session_id="chat-1")
    assert result["_data"]["ended"] is True and session.state == "ended" and calls == []


async def test_session_end_forgets_browser_pages_when_the_extension_is_loaded(monkeypatch):
    import sys

    forgotten = []
    monkeypatch.setitem(sys.modules, "agents.computer_use.browser_tools",
                        SimpleNamespace(forget=forgotten.append))
    orch, _host, _controller, session = await _started()
    await orch.computer_sessions.end(session, "user_stop")
    assert forgotten == [session.session_id]


async def test_session_end_kills_live_command_jobs_on_a_connected_host():
    orch, host, _controller, session = await _started()
    host_ws = host.websocket
    session.command_jobs["job-1"] = {"started_at": "2026-10-25T00:00:00Z"}
    session.command_jobs["job-2"] = {"started_at": "2026-10-25T00:00:01Z"}

    async def reply():
        for _ in range(100):
            await asyncio.sleep(0.001)
            for frame in host_ws.frames("computer_request"):
                orch.computer_hosts.handle_response(host_ws, OWNER, {"request_id": frame["request_id"], "ok": True,
                                                                   "result": {"state": "interrupted"}})

    responder = asyncio.create_task(reply())
    await orch.computer_sessions.end(session, "user_stop")
    responder.cancel()
    kills = [f["args"] for f in host_ws.frames("computer_request") if f["verb"] == "interrupt_command"]
    assert sorted(k["job_id"] for k in kills) == ["job-1", "job-2"]
    assert all(k["signal"] == "KILL" for k in kills) and not session.command_jobs
    ended = host_ws.frames("computer_session")[-1]
    assert ended["state"] == "ended"


async def test_session_end_skips_kills_when_the_host_is_gone_or_stopped_itself():
    orch, host, _controller, session = await _started()
    session.command_jobs["job-1"] = {}
    orch.computer_hosts.on_socket_closed(host.websocket)
    await orch.computer_sessions.end(session, "host_offline")
    assert not [f for f in host.websocket.frames("computer_request") if f["verb"] == "interrupt_command"]
    orch2, host2, _c, session2 = await _started()
    session2.command_jobs["job-1"] = {}
    await orch2.computer_sessions.on_host_event(OWNER, host2.host_id, "stopped", session2.session_id, None)
    assert not host2.websocket.frames("computer_request") and not session2.command_jobs


async def test_session_end_bounds_an_unanswered_kill(monkeypatch):
    from orchestrator import computer_sessions

    monkeypatch.setattr(computer_sessions, "JOB_KILL_TIMEOUT_S", 0.05)
    orch, host, _controller, session = await _started()
    session.command_jobs["job-1"] = {}
    await asyncio.wait_for(orch.computer_sessions.end(session, "user_stop"), 2)
    assert session.state == "ended"


def test_descriptor_accepts_optional_workspace_roots_and_old_hosts_still_validate():
    old = ComputerHostDescriptor.from_dict(_descriptor(platform="macos"))
    assert old.workspace_roots == () and "workspace_roots" not in old.to_dict()
    new = ComputerHostDescriptor.from_dict(_descriptor(platform="macos", workspace_roots=[ROOT]))
    assert new.to_dict()["workspace_roots"] == [ROOT]


@pytest.mark.parametrize("roots", [
    "x", [ROOT, ROOT], [{"name": "x"}], [{"name": "x", "path": "relative"}], [{"name": "x", "path": "/"}],
    [{"name": "", "path": "/a"}], [{"name": "x", "path": "/a/../b"}], [{"name": "x", "path": "/a/"}],
    [{"name": "x\n", "path": "/a"}], [{"name": "x", "path": "/a\x00"}], [{"name": "x", "path": "/a", "extra": 1}],
    [{"name": "x" * 129, "path": "/a"}], [{"name": "x", "path": "/" + "a" * 1100}], [ "nope"],
    [{"name": f"r{i}", "path": f"/r{i}"} for i in range(17)], [{"name": 1, "path": "/a"}],
])
def test_descriptor_rejects_malformed_workspace_roots(roots):
    with pytest.raises(ProtocolValidationError):
        ComputerHostDescriptor.from_dict(_descriptor(platform="macos", workspace_roots=roots))


async def test_host_record_and_agent_results_carry_workspace_roots(monkeypatch):
    orch = _FakeOrch()
    host_ws = orch.add(_WS())
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", workspace_roots=[ROOT])))
    assert host.workspace_roots == [ROOT]
    assert orch.computer_hosts.list_for_owner(OWNER)[0]["workspace_roots"] == [ROOT]
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    listed = await asyncio.to_thread(mcp_tools.list_computers, user_id=OWNER)
    assert listed["_data"]["computers"][0]["workspace_roots"] == [ROOT]
    orch.add(_WS(chat="chat-1"))

    async def ack():
        for _ in range(200):
            await asyncio.sleep(0.005)
            live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
            if live is not None:
                await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
                return

    acker = asyncio.create_task(ack())
    started = await asyncio.to_thread(mcp_tools.start_session, user_id=OWNER, session_id="chat-1")
    await acker
    assert started["_data"]["workspace_roots"] == [ROOT]
    await orch.computer_sessions.end_all("user_stop")


class _Recorded:
    def __init__(self):
        self.cancelled = 0

    async def cancel_all_in_flight(self):
        self.cancelled += 1


async def test_orchestrator_stop_cancels_only_the_owning_controller_turn():
    import json

    from orchestrator import hitl_confirmation as hc
    from orchestrator.orchestrator import Orchestrator

    orch, host, controller, session = await _started()
    recorder = _Recorded()
    orch.cancelled_sessions = {}
    orch._chat_recorders = {id(controller): recorder}
    orch._hitl_task_origins = {(OWNER, "chat-1"): (controller, object())}
    orch._hitl_pending_calls = {"r": SimpleNamespace(owner=OWNER, chat="chat-1", websocket=controller,
                                                     expires=float("inf"))}
    orch._cancel_turn = Orchestrator._cancel_turn.__get__(orch)
    orch.cancel_computer_turn = Orchestrator.cancel_computer_turn.__get__(orch)
    before = len(controller.sent)
    await orch.computer_sessions.end(session, "user_stop")
    assert orch.cancelled_sessions[id(controller)] is True and recorder.cancelled == 1
    assert not orch._hitl_task_origins and not orch._hitl_pending_calls
    frames = [json.loads(frame) for frame in controller.sent[before:]]
    assert {"type": "chat_status", "status": "done", "message": "Cancelled"} in frames
    assert hc.cancellation_witness(orch, controller, OWNER, "chat-1") is None

    orch2, _host2, controller2, session2 = await _started()
    orch2.cancelled_sessions = {}
    orch2._chat_recorders = {}
    orch2._cancel_turn = Orchestrator._cancel_turn.__get__(orch2)
    orch2.cancel_computer_turn = Orchestrator.cancel_computer_turn.__get__(orch2)
    orch2.ui_clients.remove(controller2)
    await orch2.computer_sessions.end(session2, "local_stop")
    assert orch2.cancelled_sessions == {}


@pytest.mark.parametrize("event,called", [("approval_activity", True), ("heartbeat", True), ("forged", False)])
async def test_orchestrator_passes_approval_activity_to_the_session_manager(monkeypatch, event, called):
    from orchestrator.orchestrator import Orchestrator
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "computer_use", True)
    orch, host, _controller, session = await _started()
    seen = []

    async def on_host_event(*args):
        seen.append(args)

    orch._tunnel_ingress_over_cap = lambda _owner: False
    monkeypatch.setattr(orch.computer_sessions, "on_host_event", on_host_event)
    message = SimpleNamespace(payload={"event": event, "session_id": session.session_id})
    await Orchestrator._handle_computer_event(orch, host.websocket, message)
    assert bool(seen) is called
    if called:
        assert seen[0][2] == event
    await orch.computer_sessions.end(session, "user_stop")


WITHDRAWN = "access withdrawn on that Mac; re-enable it in Computer controls there"


async def _withdrawn(monkeypatch):
    orch = _FakeOrch()
    host_ws = orch.add(_WS())
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", name="RyzenRoll Mobile")))
    orch.add(_WS(chat="chat-1"))
    orch.computer_hosts.withdraw(OWNER, host.host_id)
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    return orch, host, host_ws


async def test_withdrawn_host_stays_listed_offline_with_the_reason(monkeypatch):
    orch, host, _ws = await _withdrawn(monkeypatch)
    row, = orch.computer_hosts.list_for_owner(OWNER)
    assert row["online"] is False and row["offline_reason"] == "consent_revoked"
    listed = await asyncio.to_thread(mcp_tools.list_computers, user_id=OWNER)
    computer, = listed["_data"]["computers"]
    assert computer["name"] == "RyzenRoll Mobile" and computer["status"] == f"offline · {WITHDRAWN}"
    assert WITHDRAWN in json.dumps(listed["_ui_components"])


@pytest.mark.parametrize("ref", [None, "RyzenRoll Mobile"])
async def test_start_session_on_a_withdrawn_host_names_it_and_says_how_to_re_enable(monkeypatch, ref):
    orch, host, _ws = await _withdrawn(monkeypatch)
    kwargs = {"user_id": OWNER, "session_id": "chat-1", **({"computer": ref} if ref else {})}
    result = await asyncio.to_thread(mcp_tools.start_session, **kwargs)
    assert result["_data"]["code"] == "computer_unavailable"
    message = result["_data"]["message"]
    assert "RyzenRoll Mobile" in message and "re-enable" in message and "no computer called" not in message


async def test_requests_to_a_withdrawn_host_name_it(monkeypatch):
    orch, host, host_ws = await _withdrawn(monkeypatch)
    with pytest.raises(ComputerHostError) as refused:
        await orch.computer_hosts.request(host, "session", "screenshot", {}, 1)
    assert refused.value.code == "computer_unavailable" and "RyzenRoll Mobile" in refused.value.message
    assert not host_ws.frames("computer_request")


async def test_re_announce_clears_the_withdrawn_record_and_forget_still_removes_it(monkeypatch):
    orch, host, host_ws = await _withdrawn(monkeypatch)
    again, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", name="RyzenRoll Mobile", host_id=host.host_id)))
    row, = orch.computer_hosts.list_for_owner(OWNER)
    assert row["online"] is True and row["offline_reason"] is None
    orch.computer_hosts.withdraw(OWNER, again.host_id)
    assert orch.computer_hosts.forget(OWNER, again.host_id) is True
    assert orch.computer_hosts.list_for_owner(OWNER) == []


async def test_socket_loss_keeps_the_ordinary_offline_message(monkeypatch):
    orch = _FakeOrch()
    host_ws = orch.add(_WS())
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(
        _descriptor(platform="macos", name="Studio")))
    orch.computer_hosts.on_socket_closed(host_ws)
    row, = orch.computer_hosts.list_for_owner(OWNER)
    assert row["offline_reason"] is None
    with pytest.raises(ComputerHostError) as refused:
        orch.computer_hosts.resolve(OWNER, "Studio")
    assert "is offline" in refused.value.message


async def test_turn_cancel_interrupts_running_command_jobs_and_keeps_the_session():
    from orchestrator.orchestrator import Orchestrator

    orch, host, controller, session = await _started()
    session.command_jobs["job-run"] = {"state": "running"}
    session.command_jobs["job-done"] = {"state": "exited"}
    orch.cancelled_sessions = {}
    orch._chat_recorders = {}
    host_ws = host.websocket

    async def reply():
        for _ in range(200):
            await asyncio.sleep(0.001)
            for frame in host_ws.frames("computer_request"):
                orch.computer_hosts.handle_response(host_ws, OWNER, {"request_id": frame["request_id"], "ok": True,
                                                                   "result": {"state": "interrupted"}})

    responder = asyncio.create_task(reply())
    await Orchestrator._cancel_turn(orch, controller, OWNER, "chat-1")
    responder.cancel()
    signals = [(f["args"]["job_id"], f["args"]["signal"]) for f in host_ws.frames("computer_request")]
    assert signals == [("job-run", "INT")] and session.state == "active"
    await Orchestrator._cancel_turn(orch, controller, OWNER, "chat-other")
    assert len(host_ws.frames("computer_request")) == 1
    await orch.computer_sessions.end(session, "user_stop")
    await orch.computer_sessions.interrupt_jobs(session)
    assert len([f for f in host_ws.frames("computer_request") if f["args"].get("signal") == "INT"]) == 1
