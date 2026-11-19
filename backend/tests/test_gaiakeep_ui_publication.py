"""Checks Gaia result publication through the UI handler and owner-scoped conversation stages. Transient denials never reach the restored snapshot."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from astralprims import Card, CodeBlock

from orchestrator.orchestrator import GateRefusal, PreparedDispatch, _CONNECTION_OPERATION_CONTEXT
from shared.protocol import MCPResponse
from tests.test_connection_publication_owner import (
    FOREIGN, OWNER, admitted, attach_plane, database as database,
    frame, postgres_database as postgres_database, runtime,
)


def card(proposal=None):
    proposal = proposal or uuid4().hex
    return {"type": "card", "id": "au_approval_" + proposal,
            "content": [{"type": "button", "label": label, "action": "remote_op_decision",
                         "payload": {"proposal_id": proposal, "decision": decision}}
                        for label, decision in (("Approve", "approve"), ("Decline", "decline"))]}


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
async def test_real_connection_stage_commits_gaia_canvas_and_fresh_owned_hydration(database, monkeypatch):
    host, socket, context = runtime()
    attach_plane(host, database)
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    host.runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=database, repositories=database.repositories))
    host._execute_with_retry_audited = AsyncMock(return_value=response())
    tool = "gaiakeep_connection_info"
    host._authorize_and_prepare = AsyncMock(return_value=PreparedDispatch(
        args={"machine_id": "authorized-machine", "params": {}}, stream_params={}, cap_job_id=None, delegation_token=None))
    host.send_ui_render = AsyncMock()
    monkeypatch.setattr("audit.hooks.record_ws_action", AsyncMock())
    monkeypatch.setattr("audit.hooks.record_workspace_event", AsyncMock())
    ingress = frame(host, context, chat_id=chat, agent_id="gaiakeep-1", tool_name=tool,
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
