"""Checks the source-bound effect and audit boundaries on paired Kernel runtimes.
The node runs a benign first-party tool registered with an effectful scope through ordinary mesh dispatch.
"""

import asyncio
import hashlib

import pytest

from audit.recorder import Recorder
from audit.repository import AuditRepository
from orchestrator.node_mesh_calls import OrchestratorCalls, grant_name, skill_definition
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_node_mesh_kernel_integration import paired, until
from tests.test_node_mesh_peer_tools import (
    AGENT, TOOL, model_server, node_host, provide_model,  # noqa: F401
)


@pytest.fixture(scope="module")
def audit_repository():
    with isolated_plane_runtime("ad_source_bound_audit") as plane:
        yield AuditRepository(plane_runtime=plane, plane_repositories=plane.repositories)


@pytest.fixture
async def durable_audit(audit_repository, tmp_path):
    recorder = Recorder(audit_repository, retry_queue=tmp_path / "audit-retry.jsonl")
    try:
        yield recorder
    finally:
        await recorder.close()


@pytest.mark.parametrize("failed_record,expected_effects", [
    (None, 1),
    ("mesh.kernel_effect_started", 0),
    ("mesh.kernel_effect_response_success", 1),
])
async def test_signed_effect_requires_a_durable_audit(
    node_host, model_server, audit_repository, durable_audit, tmp_path, monkeypatch, failed_record, expected_effects,  # noqa: F811
):
    host = node_host
    monkeypatch.setattr(host.orch, "audit_recorder", durable_audit)
    permissions = host.orch.tool_permissions
    permissions.register_tool_scopes(AGENT, {TOOL: "tools:write"})
    permissions.set_agent_scopes(host.owner, AGENT, {"tools:write": True})
    calls = OrchestratorCalls(host.orch)
    effects = []
    outcomes = []
    retries = []
    handler = host.agent.handle_mcp_request
    run = calls.run
    record = host.orch.audit_recorder.record

    async def observe_handler(*args, **kwargs):
        effects.append(TOOL)
        return await handler(*args, **kwargs)

    async def observe_outcome(*args):
        outcome = await run(*args)
        outcomes.append(outcome)
        if failed_record is None:
            retries.append(await run(*args))
        return outcome

    async def write_audit(event):
        if event.action_type == failed_record:
            return None
        return await record(event)

    monkeypatch.setattr(host.agent, "handle_mcp_request", observe_handler)
    monkeypatch.setattr(calls, "run", observe_outcome)
    monkeypatch.setattr(host.orch.audit_recorder, "record", write_audit)
    async with paired(
        tmp_path, monkeypatch, creator=host.mesh_creator, calls=calls,
        history=host.orch.history,
    ) as (node, owner, node_device, bridge):
        provide_model(owner, model_server)
        chat = owner.create_chat("effect audit")
        owner.send_message(chat, "roll the dice", None)
        await until(lambda: any(c.chat_id == chat for c in node.kernel.list_chats()), "chat sync")
        await until(lambda: any(entry.device == node_device and entry.tool.name == TOOL
                                for entry in owner.peer_tool_catalog()), "the owner to see the published tool")
        name = grant_name(TOOL, skill_definition(host.skill))
        owner.grant_tool(name, "outside_effect")
        await until(lambda: any(g.tool == name and g.consented for g in node.kernel.grants()), "grant sync")
        await asyncio.wait_for(owner.run_turn(chat), 50)
        request, = [req for req in owner.chat_requests(chat) if req.call is not None]
        outcome, = outcomes
        expected_code = {None: None, "mesh.kernel_effect_started": "node.effect_audit_unavailable",
                         "mesh.kernel_effect_response_success": "node.outcome_uncertain"}[failed_record]
        assert outcome.code == expected_code
        assert effects == [TOOL] * expected_effects
        if failed_record is None:
            replay, = retries
            assert replay.code == "node.ticket_used"
        events, _ = audit_repository.list_for_user(host.owner, conversation_id=bridge.state.local_for(chat))
        transitions = {event.action_type: event for event in events if event.action_type.startswith("mesh.kernel_")}
        expected = {"mesh.kernel_effect_started", "mesh.kernel_effect_response_success"}
        if failed_record == "mesh.kernel_effect_started":
            expected.clear()
        elif failed_record:
            expected.remove(failed_record)
        assert set(transitions) == expected
        for event in transitions.values():
            assert event.inputs_meta["request_id"] == request.id
            assert event.inputs_meta["approval_source"] == "kernel_ticket"
            assert event.inputs_meta["ticket_sha256"] == "sha256:" + hashlib.sha256(
                request.call.ticket_json.encode()).hexdigest()
            assert event.inputs_meta["arguments_sha256"] == "sha256:" + hashlib.sha256(
                request.call.arguments_json.encode()).hexdigest()
        assert audit_repository.verify_chain(host.owner) is None
