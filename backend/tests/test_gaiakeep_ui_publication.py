"""Exercises Gaia pagination and proposal publication through the ordinary UI handler and owner-scoped conversation stages. It preserves transient denials and checks restored snapshots, normalized provenance and result ownership."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from astralprims import Card, CodeBlock

from orchestrator import remote_confirmation as rc
from orchestrator.orchestrator import GateRefusal, PreparedDispatch, _CONNECTION_OPERATION_CONTEXT
from shared.protocol import MCPResponse
from tests.test_connection_publication_owner import (
    FOREIGN, OWNER, admitted, attach_plane, database as database,
    frame, postgres_database as postgres_database, runtime,
)
from tests.test_remote_confirmation_063 import _FakeDB, _orch


def card(proposal=None):
    proposal = proposal or str(uuid4())
    return {"type": "card", "id": "au_approval_" + proposal,
            "content": [{"type": "button", "label": "Approve", "action": "remote_op_decision",
                         "payload": {"proposal_id": proposal, "decision": "approve"}}]}


def response():
    value = MCPResponse(result={"verdict": "ok"}, ui_components=[
        Card(content=[CodeBlock(language="json", code='{"connection":"fresh"}')]).to_dict()])
    value.correlation_id = "current-tool-correlation"
    return value


@pytest.fixture
def host(monkeypatch):
    host, socket, _context = runtime()
    host.send_ui_render = AsyncMock()
    host._safe_send = AsyncMock(return_value=True)
    host._send_or_replace_components = AsyncMock(return_value=[{"created": True}])
    host._authorize_and_prepare = AsyncMock(return_value=PreparedDispatch(
        args={"machine_id": "authorized-machine", "params": {"normalized": True},
              "user_id": OWNER, "session_id": "private-session", "_credentials": {"secret": "never-store"}},
        stream_params={}, cap_job_id=None, delegation_token=None))
    host._execute_with_retry_audited = AsyncMock(return_value=response())
    monkeypatch.setattr("audit.hooks.record_ws_action", AsyncMock())
    return host, socket


async def paginate(host, socket, *, agent="gaiakeep-1", chat="owned-chat", tool="gaiakeep_connection_info"):
    payload = {"agent_id": agent, "tool_name": tool,
               "params": {"machine_id": "original-machine", "user_id": "untrusted-owner"}}
    if chat is not None:
        payload["chat_id"] = chat
    await host.handle_ui_message(socket, json.dumps({"type": "ui_event", "action": "table_paginate", "payload": payload}))


@pytest.mark.asyncio
async def test_normalized_owned_gaia_result_is_published_without_mutating_response_or_authority(host):
    host, socket = host
    original = copy.deepcopy(host._execute_with_retry_audited.return_value.ui_components)
    authorized = host._authorize_and_prepare.return_value.args
    before = copy.deepcopy(authorized)
    await paginate(host, socket)
    host._send_or_replace_components.assert_awaited_once()
    args, keywords = host._send_or_replace_components.await_args
    assert args[0] is socket and args[2] == "owned-chat" and keywords == {"user_id": OWNER}
    assert args[1][0]["_source_params"] == {"machine_id": "authorized-machine", "params": {"normalized": True}}
    assert args[1][0]["_source_correlation_id"] == "current-tool-correlation"
    assert args[1][0]["content"][0]["_source_tool"] == "gaiakeep_connection_info"
    assert args[1] is not host._execute_with_retry_audited.return_value.ui_components
    assert host._execute_with_retry_audited.return_value.ui_components == original and authorized == before
    assert host._execute_with_retry_audited.await_args.args[3] is authorized
    assert "never-store" not in json.dumps(args[1]) and "private-session" not in json.dumps(args[1])
    host.send_ui_render.assert_not_awaited()


@pytest.mark.asyncio
async def test_issued_gaia_confirmation_is_published_without_dispatch_or_source_arguments(host):
    host, socket = host
    issued = card()
    host._authorize_and_prepare.return_value = GateRefusal(
        response=MCPResponse(error={"message": "confirmation_required", "retryable": False}),
        render_components=[issued], render_target="chat")
    before = copy.deepcopy(issued)
    await paginate(host, socket, tool="gaiakeep_upload")
    host._execute_with_retry_audited.assert_not_awaited()
    host._send_or_replace_components.assert_awaited_once()
    published = host._send_or_replace_components.await_args.args[1][0]
    assert published["id"] == issued["id"] and published["_source_agent"] == "gaiakeep-1"
    assert "_source_params" not in published and issued == before
    host.send_ui_render.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("components", [[{"type": "alert", "message": "permission denied"}],
                                      [{"type": "card", "id": "unrelated-card"}],
                                      [card(), {"type": "alert", "message": "owner denied"}]])
async def test_nonproposal_gate_refusal_stays_transient_without_dispatch(host, components):
    host, socket = host
    host._authorize_and_prepare.return_value = GateRefusal(
        response=MCPResponse(error={"message": "not permitted", "retryable": False}),
        render_components=components, render_target="chat")
    await paginate(host, socket)
    host._execute_with_retry_audited.assert_not_awaited()
    host._send_or_replace_components.assert_not_awaited()
    host.send_ui_render.assert_awaited_once_with(socket, components, target="chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,chat,error", [("other-agent", "owned-chat", None),
                                           ("gaiakeep-1", None, None),
                                           ("gaiakeep-1", "owned-chat", {"message": "native refusal"})])
async def test_unrelated_unscoped_or_failed_tool_result_retains_transient_delivery(host, agent, chat, error):
    host, socket = host
    host._execute_with_retry_audited.return_value.error = error
    await paginate(host, socket, agent=agent, chat=chat)
    host._send_or_replace_components.assert_not_awaited()
    host.send_ui_render.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_agent_confirmation_remains_transient(host):
    host, socket = host
    components = [card()]
    host._authorize_and_prepare.return_value = GateRefusal(
        response=MCPResponse(error={"message": "confirmation_required"}), render_components=components, render_target="chat")
    await paginate(host, socket, agent="remote-compute-1")
    host._send_or_replace_components.assert_not_awaited()
    host._execute_with_retry_audited.assert_not_awaited()
    host.send_ui_render.assert_awaited_once_with(socket, components, target="chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["result", "proposal"])
async def test_real_connection_stage_commits_gaia_canvas_and_fresh_owned_hydration(database, monkeypatch, kind):
    host, socket, context = runtime()
    attach_plane(host, database)
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    host._authorize_and_prepare = AsyncMock(return_value=PreparedDispatch(
        args={"machine_id": "authorized-machine", "params": {}}, stream_params={}, cap_job_id=None, delegation_token=None))
    host._execute_with_retry_audited = AsyncMock(return_value=response())
    if kind == "proposal":
        host._authorize_and_prepare.return_value = GateRefusal(
            response=MCPResponse(error={"message": "confirmation_required"}), render_components=[card()], render_target="chat")
    host.send_ui_render = AsyncMock()
    monkeypatch.setattr("audit.hooks.record_ws_action", AsyncMock())
    monkeypatch.setattr("audit.hooks.record_workspace_event", AsyncMock())
    ingress = frame(host, context, chat_id=chat, agent_id="gaiakeep-1", tool_name="gaiakeep_connection_info",
                    params={"machine_id": "authorized-machine", "params": {}})
    operation, claim, _before = await asyncio.to_thread(admitted, host, context, ingress)
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": claim.operation, "owner": operation.owner,
                                               "execution_fence": claim.fence})
    try:
        await host._run_connection_ui_operation(context, operation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    committed = [row for row in socket.frames if row.get("type") == "conversation_snapshot"]
    assert len(committed) == 1 and committed[0]["render_revision"] == 1
    restored = await asyncio.to_thread(host.conversation_commits.build_snapshot,
        chat_id=chat, owner_user_id=OWNER, connection_generation=str(context.connection_generation),
        request_generation=str(uuid4()), snapshot_purpose="hydration")
    assert restored["render_revision"] == 1
    adapted = host._adapt_conversation_snapshot(socket, copy.deepcopy(restored))
    assert adapted["canvas"]["components"] == committed[0]["canvas"]["components"]
    assert len(restored["canvas"]["components"]) == 1
    assert restored["canvas"]["components"][0]["_source_agent"] == "gaiakeep-1"
    assert await asyncio.to_thread(host.history.get_chat, chat, user_id=FOREIGN) is None
    assert all(call.kwargs.get("target") == "history" for call in host.send_ui_render.await_args_list)
    if kind == "proposal":
        host._execute_with_retry_audited.assert_not_awaited()


def test_gaia_proposal_gets_a_stable_replaceable_identity_without_policy_change():
    host = _orch(_FakeDB())
    pid, component = rc._create_proposal(host, OWNER, "owned-chat", "gaiakeep-1", "gaiakeep_core_repair",
                                       {"machine_id": "owned-machine", "params": {}})
    assert component["id"] == rc.card_component_id(pid)
    assert rc.policy_for("gaiakeep-1").card_as_result is False
    _other_pid, other = rc._create_proposal(host, OWNER, "owned-chat", "remote-compute-1", "remove_path",
                                           {"machine_id": "owned-machine", "path": "/synthetic"})
    assert other.get("id") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "decline", "foreign"])
async def test_actual_gaia_decision_restores_only_owned_committed_result_or_decline(database, monkeypatch, decision):
    host, socket, context = runtime()
    attach_plane(host, database)
    host.runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=database, repositories=database.repositories))
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    args = {"machine_id": "owned-machine", "params": {"request_id": "original-native-request"}, "user_id": OWNER}
    proposal, issued = rc._create_proposal(host, OWNER, chat, "gaiakeep-1", "gaiakeep_core_repair", args)
    host._authorize_and_prepare = AsyncMock(return_value=GateRefusal(
        response=MCPResponse(error={"message": "confirmation_required"}), render_components=[issued], render_target="chat"))
    host.send_ui_render = AsyncMock()
    monkeypatch.setattr("audit.hooks.record_ws_action", AsyncMock())
    monkeypatch.setattr("audit.hooks.record_workspace_event", AsyncMock())
    ingress = frame(host, context, chat_id=chat, agent_id="gaiakeep-1", tool_name="gaiakeep_core_repair", params=args)
    operation, claim, _before = await asyncio.to_thread(admitted, host, context, ingress)
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": claim.operation, "owner": operation.owner,
                                               "execution_fence": claim.fence})
    try:
        await host._run_connection_ui_operation(context, operation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    policy = copy.copy(rc.policy_for("gaiakeep-1"))
    policy.auto_continue = False
    original = rc.policy_for
    monkeypatch.setattr(rc, "policy_for", lambda agent: policy if agent == "gaiakeep-1" else original(agent))

    async def execute(socket, tool_call, mapping, conversation_id, *, user_id):
        stored = json.loads(tool_call.function.arguments)
        assert user_id == OWNER and conversation_id == chat and mapping == {"gaiakeep_core_repair": "gaiakeep-1"}
        assert rc._consume_if_valid(host, proposal, user_id, "gaiakeep_core_repair", stored) is True
        return response()

    host.execute_single_tool = AsyncMock(side_effect=execute)
    await rc.handle_decision(host, socket, FOREIGN if decision == "foreign" else OWNER,
                             {"proposal_id": proposal, "decision": "decline" if decision == "decline" else "approve"})
    restored = await asyncio.to_thread(host.conversation_commits.build_snapshot,
        chat_id=chat, owner_user_id=OWNER, connection_generation=str(context.connection_generation),
        request_generation=str(uuid4()), snapshot_purpose="hydration")
    components = restored["canvas"]["components"]
    approved = [component for component in components if component.get("_source_correlation_id") == "current-tool-correlation"]
    if decision == "approve":
        assert restored["render_revision"] > 1 and len(approved) == 1
        assert approved[0]["content"][0]["code"] == '{"connection":"fresh"}'
        assert approved[0]["_source_params"] == {"machine_id": "owned-machine", "params": {"request_id": "original-native-request"}}
        host.execute_single_tool.assert_awaited_once()
    elif decision == "decline":
        assert restored["render_revision"] == 2 and not approved
        declined = [component for component in components if component["component_id"] == rc.card_component_id(proposal)]
        assert len(declined) == 1 and declined[0]["title"] == "Declined"
        assert all(child["type"] != "button" for child in declined[0]["content"])
        host.execute_single_tool.assert_not_awaited()
    else:
        assert restored["render_revision"] == 1 and not approved
        host.execute_single_tool.assert_not_awaited()
    assert await asyncio.to_thread(host.history.get_chat, chat, user_id=FOREIGN) is None
    prior = copy.deepcopy(restored["canvas"]["components"])
    await rc.handle_decision(host, socket, FOREIGN if decision == "foreign" else OWNER,
                             {"proposal_id": proposal, "decision": "approve"})
    fresh = await asyncio.to_thread(host.conversation_commits.build_snapshot,
        chat_id=chat, owner_user_id=OWNER, connection_generation=str(context.connection_generation),
        request_generation=str(uuid4()), snapshot_purpose="hydration")
    assert fresh["render_revision"] == restored["render_revision"] and fresh["canvas"]["components"] == prior
    assert host.execute_single_tool.await_count == int(decision == "approve")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,expected", [("gaiakeep-1", True), ("remote-compute-1", False)])
async def test_decline_replaces_only_the_issued_owned_gaia_card(agent, expected):
    host = _orch(_FakeDB())
    host.workspace = SimpleNamespace(aupsert=AsyncMock(return_value=[{"created": False}]))
    host.send_ui_upsert = AsyncMock()
    async def mutate(**args):
        return await args["mutation"]()
    host.run_detached_conversation_mutation = AsyncMock(side_effect=mutate)
    row = SimpleNamespace(agent_id=agent, conversation_id="owned-chat", owner_id=OWNER, proposal_id=str(uuid4()))
    await rc._replace_card(host, row, "Declined", "Nothing changed.")
    assert host.run_detached_conversation_mutation.await_count == int(expected)
    if expected:
        host.run_detached_conversation_mutation.assert_awaited_once()
        assert host.run_detached_conversation_mutation.await_args.kwargs["user_id"] == OWNER
        args, kwargs = host.workspace.aupsert.await_args
        assert args[:2] == ("owned-chat", OWNER) and args[2][0]["title"] == "Declined"
        assert kwargs == {"force_component_id": rc.card_component_id(row.proposal_id)}
        host.send_ui_upsert.assert_awaited_once()
    else:
        host.workspace.aupsert.assert_not_awaited()
        host.send_ui_upsert.assert_not_awaited()
