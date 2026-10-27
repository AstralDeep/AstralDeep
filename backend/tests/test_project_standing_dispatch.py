"""Exercises project standing grants through both real consent gates and governed dispatch.
Synthetic physical receivers record effects independently from approval UI and audit output.
"""
# ruff: noqa: F811

from types import SimpleNamespace

import pytest

from orchestrator import project_authority, remote_confirmation
from orchestrator.computer_use_policy import SCOPES
from shared.project_workspaces import ProjectManifest
from tests.test_computer_grant_flow_102 import _mac, _call, _fresh_pending_cards  # noqa: F401
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401

PROJECT = "12345678-1234-1234-9234-123456789abc"


async def project_runtime(task_runtime, monkeypatch):
    orch, ws, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    host.project_mode = True
    host.descriptor["workspace_roots"] = []
    host.project_manifest = ProjectManifest.from_dict({"generation": 1, "projects": [{"project_id": PROJECT,
        "grant_revision": 1, "roots": [{"name": "Primary", "path": "/tmp/project-a"},
                                      {"name": "Second", "path": "/tmp/second"}]}]})
    orch.ui_sessions[host.websocket] = {"sub": "owner-a", "_project_workspaces_v1": True}
    orch._connection_contexts[id(host.websocket)] = SimpleNamespace(websocket=host.websocket, registered=True,
        closing=False, connection_generation="host-connection")
    host.project_manifest_highwater = 1
    class Service:
        def __init__(self, history):
            pass
        def require_chat(self, owner, chat):
            assert owner == "owner-a" and chat == "chat-a"
            return PROJECT
    monkeypatch.setattr(project_authority, "ProjectService", Service)
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    return session, host


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "hello.md", "content": "hello"}),
    ("delete_path", {"path": "hello.md"}),
    ("run_command", {"argv": ["pwd"]}),
])
async def test_project_grant_executes_with_bypass_off_and_zero_cards(task_runtime, monkeypatch, tool, args):
    orch, ws, effects, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, tool, **args)
    assert response.error is None, response.error
    assert response.result["_data"]["approval_source"] == "project_grant"
    assert len(effects) == 1
    assert not getattr(orch, "_hitl_pending_calls", {}) and not remote_confirmation._PENDING_CARDS
    dispatched = effects[0][1]
    if tool == "run_command":
        assert dispatched["cwd"] == "/tmp/project-a"
    else:
        assert dispatched["path"] == "/tmp/project-a/hello.md"
    assert not orch.computer_sessions.bypass_on("owner-a", host.host_id)
    assert any(call.args[0].inputs_meta.get("approval_source") == "project_grant"
               for call in orch.audit_recorder.record.await_args_list)
    await orch.computer_sessions.end(session, "consent_revoked")


async def test_origin_grant_survives_navigation_without_lending_other_roots(task_runtime, monkeypatch):
    orch, ws, effects, *_ = task_runtime
    session, _ = await project_runtime(task_runtime, monkeypatch)
    orch._ws_active_chat[id(ws)] = "chat-b"
    response = await _call(task_runtime, "write_file", path="hello.md", content="hello")
    assert response.error is None, response.error
    assert effects[0][1]["path"] == "/tmp/project-a/hello.md"
    await orch.computer_sessions.end(session, "consent_revoked")

@pytest.mark.parametrize("tool,args", [("read_file", {"path": "hello.md"}), ("list_dir", {"path": "."})])
async def test_project_reads_record_standing_source_without_cards(task_runtime, monkeypatch, tool, args):
    orch, ws, effects, *_ = task_runtime
    session, _ = await project_runtime(task_runtime, monkeypatch)
    response = await _call(task_runtime, tool, **args)
    assert response.error is None, response.error
    assert response.result["_data"]["approval_source"] == "project_grant"
    assert not getattr(orch, "_hitl_pending_calls", {}) and not remote_confirmation._PENDING_CARDS
    assert any(call.args[0].inputs_meta.get("approval_source") == "project_grant"
               for call in orch.audit_recorder.record.await_args_list)
    await orch.computer_sessions.end(session, "consent_revoked")


@pytest.mark.parametrize("tool,args", [("write_file", {"path": "a", "content": "x"}), ("read_file", {"path": "a"})])
async def test_omitted_computer_keeps_origin_session_with_two_project_hosts(task_runtime, monkeypatch, tool, args):
    orch, ws, effects, *_ = task_runtime
    session, first = await project_runtime(task_runtime, monkeypatch)
    other_ws = object()
    second, _ = orch.computer_hosts.register("owner-a", other_ws,
        {**first.descriptor, "host_id": "99999999-9999-4999-8999-999999999999", "name": "Other Mac"})
    second.project_mode = True
    second.project_manifest = ProjectManifest.from_dict({"generation": 1, "projects": []})
    orch.ui_sessions[other_ws] = {"sub": "owner-a", "_project_workspaces_v1": True}
    orch._connection_contexts[id(other_ws)] = SimpleNamespace(websocket=other_ws, registered=True, closing=False,
                                                            connection_generation="other")
    response = await _call(task_runtime, tool, **args)
    assert response.error is None, response.error
    assert len(effects) == 1 and effects[0][1]["path"] == "/tmp/project-a/a"
    assert response.result["_data"]["approval_source"] == "project_grant"
    await orch.computer_sessions.end(session, "consent_revoked")

async def test_standing_write_crosses_both_gates_worker_bridge_and_native_rpc(task_runtime, monkeypatch):
    import asyncio
    import json
    from agents.computer_use import mcp_tools
    from shared.protocol import MCPResponse

    orch, ws, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)
    orch.ui_clients = [ws, host.websocket]
    orch.computer_hosts._orch = orch
    orch.computer_sessions._orch = orch
    host.descriptor["verbs"] = ["write_file"]
    monkeypatch.setattr(mcp_tools, "_ORCH", orch)
    monkeypatch.setattr(mcp_tools, "_LOOP", asyncio.get_running_loop())
    delivered = []
    async def send(socket, raw):
        frame = json.loads(raw)
        if frame.get("type") == "computer_request":
            delivered.append(frame)
            orch.computer_hosts.handle_response(socket, "owner-a", {"request_id": frame["request_id"],
                "ok": True, "result": {"path": frame["args"]["path"], "bytes_written": 5}})
        return True
    orch._safe_send = send
    async def execute(websocket, agent, tool, args, **kwargs):
        async def invoke(_):
            return MCPResponse(result=await asyncio.to_thread(mcp_tools.write_file, user_id="owner-a", session_id="chat-a", **args))
        return await orch._execute_governed_attempt(websocket, agent, tool, args, user_id="owner-a", channel="web",
            audit_correlation_id="test", actor_user_id="owner-a", auth_principal="owner-a", conversation_id="chat-a", invoke=invoke)
    orch._execute_with_retry = execute
    response = await _call(task_runtime, "write_file", path="note.md", content="hello")
    assert response.error is None and "code" not in response.result["_data"], response
    assert response.result["_data"]["approval_source"] == "project_grant"
    assert len(delivered) == 1 and delivered[0]["args"]["path"] == "/tmp/project-a/note.md"
    assert delivered[0]["project_context"] == session.project_origin.context.to_dict()
    assert not getattr(orch, "_hitl_pending_calls", {}) and not remote_confirmation._PENDING_CARDS
    await orch.computer_sessions.end(session, "consent_revoked")

@pytest.mark.parametrize("tool", ["start_session", "end_session", "resume_session"])
@pytest.mark.parametrize("bypass", [False, True])
async def test_text_only_project_lifecycle_uses_ordinary_bypass_or_review(task_runtime, monkeypatch, tool, bypass):
    from orchestrator import hitl_confirmation as hc

    orch, ws, effects, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)
    host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": []})
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    await orch.computer_sessions.set_bypass(host, bypass)
    async with project_authority.project_dispatch_scope(orch, ws, "owner-a", "chat-a", "computer-use-1", tool, {}):
        decision, _ = hc.consent_decision(orch, ws, "owner-a", "chat-a", "computer-use-1", tool, {}, ())
        assert decision == ("standing" if bypass else "review")
        assert hc.standing_source(orch, "owner-a", "chat-a", host, tool) == "bypass"
    response = await _call(task_runtime, tool)
    if bypass:
        assert response.error is None and response.result["_data"]["approval_source"] == "bypass", response
        assert len(effects) == 1
        assert not getattr(orch, "_hitl_pending_calls", {}) and not remote_confirmation._PENDING_CARDS
    else:
        assert not effects
        assert getattr(orch, "_hitl_pending_calls", {}) or remote_confirmation._PENDING_CARDS
    await orch.computer_sessions.end(session, "consent_revoked")

@pytest.mark.parametrize("tool,args", [("read_file", {"path": "/tmp/project-a/a"}), ("run_command", {"argv": ["pwd"]})])
async def test_text_only_project_file_tools_refuse_even_with_bypass(task_runtime, monkeypatch, tool, args):
    orch, _, effects, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)
    host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": []})
    session.project_origin = await project_authority.resolve_project_origin(orch, "owner-a", "chat-a", host)
    await orch.computer_sessions.set_bypass(host, True)
    response = await _call(task_runtime, tool, **args)
    assert response.error["code"] == "project_grant_required"
    assert not effects and not getattr(orch, "_hitl_pending_calls", {}) and not remote_confirmation._PENDING_CARDS
    await orch.computer_sessions.end(session, "consent_revoked")

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["write_file", "read_file"])
async def test_post_delivery_error_is_not_a_pre_effect_refusal(task_runtime, monkeypatch, tool):
    from orchestrator.computer_hosts import ComputerHostError

    orch, ws, effects, *_ = task_runtime
    session, _ = await project_runtime(task_runtime, monkeypatch)

    async def execute(websocket, agent, tool, args, **kwargs):
        async def invoke(_):
            effects.append((tool, args))
            raise ComputerHostError('outcome_uncertain', 'Native delivery completed; reply was lost')

        return await orch._execute_governed_attempt(
            websocket, agent, tool, args, user_id='owner-a', channel='web',
            audit_correlation_id='review-probe', actor_user_id='owner-a',
            auth_principal='owner-a', conversation_id='chat-a', invoke=invoke,
        )

    orch._execute_with_retry = execute
    response = await _call(task_runtime, tool, path='hello.md', **({'content': 'hello'} if tool == 'write_file' else {}))
    await orch.computer_sessions.end(session, 'consent_revoked')
    assert len(effects) == 1
    assert response.error['code'] == 'outcome_uncertain'
    assert response.error.get('effect_state') != 'not_started', response.error


async def test_gate_wrapper_does_not_reclassify_body_exception_as_admission_denial(task_runtime, monkeypatch):
    from unittest.mock import AsyncMock
    from orchestrator.computer_hosts import ComputerHostError
    orch, ws, *_ = task_runtime
    session, _ = await project_runtime(task_runtime, monkeypatch)
    async def body(*args, **kwargs):
        raise ComputerHostError("outcome_uncertain", "Body effect may have completed")
    monkeypatch.setattr(orch, "_run_project_bound_gate_stack", body)
    denial = AsyncMock()
    monkeypatch.setattr(orch, "_audit_gate_denial", denial)
    with pytest.raises(ComputerHostError, match="may have completed"):
        await orch._run_gate_stack(ws, "computer-use-1", "write_file", {"path": "a", "content": "x"}, "chat-a", "owner-a")
    denial.assert_not_called()
    await orch.computer_sessions.end(session, "consent_revoked")


async def test_text_only_sessionless_review_is_bound_and_withdrawn_before_late_decision(task_runtime, monkeypatch):
    from orchestrator import hitl_confirmation as hitl
    from shared.project_workspaces import ProjectManifest
    from tests.test_computer_grant_flow_102 import _payload
    orch, ws, effects, *_ = task_runtime
    session, host = await project_runtime(task_runtime, monkeypatch)
    await orch.computer_sessions.end(session, "consent_revoked")
    orch.computer_sessions._orch = orch
    host.project_manifest = ProjectManifest.from_dict({"generation": 2, "projects": []})
    card = await _call(task_runtime, "start_session")
    call, = orch._hitl_pending_calls.values()
    assert call.review_host is host and call.review_connection is orch._connection_contexts[id(ws)]
    assert call.project_origin is not None and call.computer_authority is None
    async def presentation(*args, **kwargs):
        pass
    monkeypatch.setattr(hitl, "_finish_card", presentation)
    await orch.computer_sessions.set_bypass(host, True)
    assert not orch._hitl_pending_calls
    assert not hitl._current_session(orch, ws, call)
    await orch.computer_sessions.set_bypass(host, False)
    await hitl.handle_decision(orch, ws, call.owner, _payload(card, "approve"))
    assert not effects and not orch.computer_sessions.live_for_owner(call.owner)
