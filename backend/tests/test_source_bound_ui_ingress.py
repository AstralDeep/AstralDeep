"""Exercises rendered approval decisions through registered socket admission.
The real Plane audit and an independent effect callback witness accepted decisions.
"""

import asyncio
from copy import deepcopy
from html.parser import HTMLParser
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from audit.recorder import Recorder
from audit.repository import AuditRepository
from orchestrator import hitl_confirmation as hc
from orchestrator.history import ConversationCommitRepository, HistoryManager
from orchestrator.workspace import WorkspaceManager
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from tests.test_work_surface_ingress_088 import (  # noqa: F401
    ingress, surface, command, context, fixture, runtime, service, signing_key, registered,
)
from webrender.renderer import render


class ApprovalButton(HTMLParser):
    def __init__(self):
        super().__init__()
        self.payload = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "button" and values.get("data-action") == "authorize_action":
            payload = json.loads(values["data-payload"])
            if payload.get("decision") == "approve":
                self.payload = payload


def envelope(state, chat, component, payload):
    submission, request = str(uuid4()), str(uuid4())
    return {"type": "ui_event", "action": "authorize_action", "session_id": chat,
        "submission_id": submission, "request_generation": request,
        "connection_generation": state.connection_generation,
        "payload": {**payload, "chat_id": chat, "component_id": component,
                    "submission_id": submission, "request_generation": request}}


async def delivered(state, frame):
    state.socket.feed(json.dumps(frame))
    request = frame.get("request_generation", frame["payload"].get("request_generation"))
    return await state.arrived(lambda value: value.get("type") in {"error", "operation_rejected"}
        or value.get("type") == "operation_status" and value.get("terminal") is True
        and value.get("request_generation") == request)


async def finished(state, request):
    async with asyncio.timeout(5):
        while (await state.finished.get()).get("request_generation") != request:
            pass


@pytest.fixture
async def approval_ingress(ingress, runtime, fixture, monkeypatch, tmp_path):  # noqa: F811
    state = ingress
    await registered(state)
    orch, ws, owner = state.orch, state.socket, fixture[1]
    history = HistoryManager(str(tmp_path), plane_runtime=runtime)
    chat = await asyncio.to_thread(history.create_chat, user_id=owner)
    orch.history = history
    orch.workspace = WorkspaceManager(history, plane_runtime=runtime)
    orch.conversation_commits = ConversationCommitRepository(plane_runtime=runtime,
        operation_coordinator=orch.work_admission)
    orch._conversation_scopes = {}
    orch._workspace_locks = {}
    orch._ws_active_chat[id(ws)] = chat
    orch.agent_cards["approval-fixture"] = AgentCard(
        agent_id="approval-fixture", name="Approval fixture", description="Synthetic effect",
        skills=[AgentSkill(id="commit_fixture", name="Commit fixture", scope="tools:write",
            description="Synthetic effect", input_schema={"type": "object", "properties": {}})])
    orch.agents["approval-fixture"] = object()
    orch.local_agents = {}
    orch.a2a_clients = {}
    orch.security_flags = {}
    orch.tool_permissions.register_tool_scopes("approval-fixture", {"commit_fixture": "tools:write"})
    await asyncio.to_thread(orch.tool_permissions.set_agent_scopes, owner, "approval-fixture", {"tools:write": True})
    orch.credential_manager = SimpleNamespace(get_agent_credentials_encrypted=lambda *_: None)
    orch._get_delegation_token = AsyncMock(return_value="synthetic-fixture-delegation")
    orch._delegation_required = lambda: False
    orch._delegation_denied_for_permissions = AsyncMock(return_value=False)
    orch._is_long_running_tool = lambda *_: False
    orch._tool_accepts_context_arg = lambda *_: False
    orch._auto_subscribe_stream_artifacts = AsyncMock()
    orch._map_file_paths = lambda _chat, args, **_kwargs: args
    orch.send_ui_render = AsyncMock()
    orch.send_ui_upsert = AsyncMock()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
                        lambda: SimpleNamespace(contains_phi=lambda _: False))
    repository = AuditRepository(plane_runtime=runtime)
    recorder = Recorder(repository, retry_queue=tmp_path / "approval-retry.jsonl")
    orch.audit_recorder = recorder
    effects = []

    class Adapter:
        mode = "off"

        async def execute(self, **kwargs):
            return await kwargs["invoke"](None)

    orch.governed_final_dispatch = Adapter()

    async def dispatch(websocket, agent, tool, args, **kwargs):
        async def physical(_capabilities):
            effects.append(dict(args))
            return MCPResponse(result={"ok": True})

        return await orch._execute_governed_attempt(websocket, agent, tool, args,
            user_id=owner, channel="web", audit_correlation_id=None,
            actor_user_id=owner, auth_principal=owner, conversation_id=chat, invoke=physical)

    orch._execute_with_retry = dispatch
    try:
        call = SimpleNamespace(id="propose", function=SimpleNamespace(name="commit_fixture", arguments="{}"))
        response = await orch.execute_single_tool(ws, call, {"commit_fixture": "approval-fixture"}, chat, user_id=owner)
        assert response.error is None
        card = response.ui_components[0]
        await orch.workspace.aupsert(chat, owner, [card])
        stored = await orch.workspace.alive_components(chat, owner)
        selected = next(value for value in stored if value.get("id") == card["id"])
        parser = ApprovalButton()
        parser.feed(render([selected]))
        assert parser.payload is not None
        yield state, owner, chat, selected["component_id"], parser.payload, effects, repository
    finally:
        await recorder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["web", "top_only", "payload_only"])
@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_rendered_approval_normal_envelope_reaches_one_effect(approval_ingress, caplog, shape, decision):
    caplog.set_level("DEBUG", logger=hc.__name__)
    state, owner, chat, component, payload, effects, repository = approval_ingress
    payload = {**payload, "decision": decision}
    frame = envelope(state, chat, component, payload)
    if shape == "top_only":
        frame["payload"].pop("submission_id")
        frame["payload"].pop("request_generation")
    elif shape == "payload_only":
        for key in ("submission_id", "request_generation", "connection_generation"):
            frame["payload"][key] = frame.pop(key)
    await delivered(state, frame)
    await delivered(state, envelope(state, chat, component, payload))
    assert len(effects) == (1 if decision == "approve" else 0)
    rows, _ = repository.list_for_user(owner)
    expected = {
        "hitl.proposed", "hitl.approved", "hitl.consumed", "hitl.effect_started", "hitl.effect_response_success"}
    assert {row.action_type for row in rows if row.correlation_id == payload["hitl_request_id"]} == (
        expected if decision == "approve" else {"hitl.proposed", "hitl.declined"})
    assert repository.verify_chain(owner) is None
    current = await state.orch.workspace.alive_components(chat, owner)
    assert len(current) == 1 and current[0]["component_id"] == component
    assert not any(node.get("action") == "authorize_action" for node in current[0]["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unknown", "args", "chat", "component", "raw_component", "null_component",
    "submission", "request", "connection", "null_submission", "missing_card", "wrong_author", "retired_offer", "store_failure"])
async def test_forged_envelope_never_consumes_pending_offer(approval_ingress, monkeypatch, change):
    state, owner, chat, component, payload, effects, repository = approval_ingress
    frame = envelope(state, chat, component, payload)
    business = frame["payload"]
    if change in {"unknown", "args"}:
        business[change] = {"material": "unapproved"}
    elif change == "chat":
        business["chat_id"] = str(uuid4())
        frame["session_id"] = business["chat_id"]
    elif change == "component":
        business["component_id"] = "au_unrelated"
    elif change == "raw_component":
        business["component_id"] = "hitl_" + payload["hitl_request_id"]
    elif change == "null_component":
        business["component_id"] = None
    elif change in {"submission", "request", "connection", "null_submission"}:
        field = {"submission": "submission_id", "request": "request_generation",
                 "connection": "connection_generation", "null_submission": "submission_id"}[change]
        business[field] = None if change == "null_submission" else str(uuid4())
    elif change == "missing_card":
        await state.orch.workspace.aremove(chat, owner, component)
    elif change == "store_failure":
        async def unavailable(*_):
            raise RuntimeError("current offer unavailable")
        monkeypatch.setattr(state.orch.workspace, "aget_by_component_id", unavailable)
    else:
        row = await state.orch.workspace.aget_by_component_id(chat, owner, component)
        card = deepcopy(row["component_data"])
        if change == "wrong_author":
            card["id"] = "unrelated"
        else:
            card["content"] = [{"type": "text", "content": "Retired"}]
        await state.orch.workspace.aupsert(chat, owner, [card], force_component_id=component)
    await delivered(state, frame)
    assert not effects and payload["hitl_request_id"] in state.orch._hitl_pending_calls
    rows, _ = repository.list_for_user(owner)
    assert not any(row.action_type == "hitl.approved" for row in rows)
    assert repository.verify_chain(owner) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["same_owner_rebind", "claims", "chat", "connection", "closing", "lost_lease", "pending"])
async def test_offer_lookup_cannot_adopt_changed_admission(approval_ingress, monkeypatch, change):
    from dataclasses import replace

    state, owner, chat, component, payload, effects, _ = approval_ingress
    entered, release = asyncio.Event(), asyncio.Event()
    lookup = state.orch.workspace.aget_by_component_id

    async def held(*args):
        row = await lookup(*args)
        entered.set()
        await release.wait()
        return row

    monkeypatch.setattr(state.orch.workspace, "aget_by_component_id", held)
    frame = envelope(state, chat, component, payload)
    state.socket.feed(json.dumps(frame))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        context = state.orch._connection_contexts[id(state.socket)]
        if change == "same_owner_rebind":
            state.orch.ui_sessions[state.socket] = dict(state.orch.ui_sessions[state.socket])
        elif change == "claims":
            state.orch.ui_sessions[state.socket]["_client_capabilities"].append("changed")
        elif change == "chat":
            state.orch._ws_active_chat[id(state.socket)] = str(uuid4())
        elif change == "connection":
            context.connection_generation = uuid4()
        elif change == "closing":
            context.closing = True
        elif change == "lost_lease":
            next(work for work in context.operations.values()
                 if str(work.frame.request_generation) == frame["request_generation"]).lease_lost = True
        else:
            key = payload["hitl_request_id"]
            state.orch._hitl_pending_calls[key] = replace(state.orch._hitl_pending_calls[key])
    finally:
        release.set()
    await finished(state, frame["request_generation"])
    assert not effects and payload["hitl_request_id"] in state.orch._hitl_pending_calls


@pytest.mark.asyncio
async def test_concurrent_current_offer_lookups_have_one_winner(approval_ingress, monkeypatch):
    state, owner, chat, component, payload, effects, repository = approval_ingress
    entered, release = asyncio.Event(), asyncio.Event()
    lookup = state.orch.workspace.aget_by_component_id
    reads = []

    async def held(*args):
        row = await lookup(*args)
        reads.append(row)
        if len(reads) == 2:
            entered.set()
        await release.wait()
        return row

    monkeypatch.setattr(state.orch.workspace, "aget_by_component_id", held)
    decision = {**payload, "chat_id": chat, "component_id": component}
    attempts = [asyncio.create_task(hc.handle_decision(state.orch, state.socket, owner, decision)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        release.set()
    await asyncio.gather(*attempts)
    assert len(effects) == 1
    rows, _ = repository.list_for_user(owner)
    assert sum(row.action_type == "hitl.approved" for row in rows) == 1
