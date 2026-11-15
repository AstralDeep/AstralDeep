"""Runs shared composed gate cases through the paired native Kernel request route and the node's protected tool dispatch.
The scripted model and sandbox executor supply controlled responses while the mesh, grants, tickets, authorization, and journal are real."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import vessel_executor as ve
from orchestrator.node_mesh_calls import OrchestratorCalls, definition_hash, grant_name
from shared.protocol import AgentSkill
from tests.test_node_mesh_kernel_integration import ak, paired, until  # noqa: F401
from tests.test_node_mesh_kernel_request_tools import ask, setup_native
from tests.test_node_mesh_peer_tools import FakeModel, model_server, node_host  # noqa: F401
from tests.test_node_mesh_turn_tools import SANDBOXED, grant, turn_host  # noqa: F401

FIXTURE = Path(__file__).resolve().parents[2] / "specs/105-decentralized-personal-mesh/contracts/conformance/composed_gate.json"
CASES = {case["name"]: case for case in json.loads(FIXTURE.read_text())["cases"]}


async def test_receiver_capability_and_effect_audit_fail_closed_without_native_binding(monkeypatch):
    from orchestrator import hitl_confirmation, node_mesh_admission
    from orchestrator.node_mesh import MeshUnavailable, NodeMesh, NodeMeshError, RequestReference
    from shared import node_identity

    monkeypatch.setattr(node_identity, "is_personal", lambda: True)
    assert await node_mesh_admission.source_bound_capability(None, "owner", "chat", "agent", "tool", {}) is None
    assert await node_mesh_admission.source_bound_capability(None, "", "chat", "agent", "tool", {}) is None
    with pytest.raises(NodeMeshError, match="request.malformed"):
        RequestReference.capture(SimpleNamespace(
            id="request", chat_id="chat", from_device="peer", to_device="local", text="work",
            continues=None, call=None, turn=1))
    mesh = object.__new__(NodeMesh)
    mesh._kernel = object()
    with pytest.raises(MeshUnavailable) as check:
        await mesh.check_host_call("request", "definition", "outside_effect")
    assert check.value.code == "host_call_check_unsupported"
    with pytest.raises(MeshUnavailable) as claim:
        await mesh.claim_host_call("request", "definition", "outside_effect")
    assert claim.value.code == "host_call_claim_unsupported"
    with pytest.raises(ValueError, match="invalid Kernel effect audit transition"):
        await hitl_confirmation.audit_kernel_effect(None, None, "invalid")


def install_effect(host, case):
    call = case["call"]
    name = call["tool"]
    description = f"Controlled {name} effect"
    schema = {"type": "object", "properties": {key: {"type": "string"} for key in call["arguments"]}}
    definition = definition_hash(name, description, schema, {ve.AGENT_ANNOTATION: SANDBOXED})
    card = host.orch.agent_cards[SANDBOXED]
    card.skills.append(AgentSkill(name=name, id=name, description=description, input_schema=schema, scope="tools:write"))
    host.orch.vessel_definitions[(SANDBOXED, name)] = definition
    host.orch.tool_permissions.register_tool_scopes(SANDBOXED, {name: "tools:write"})
    host.orch.tool_permissions.set_agent_scopes(host.owner, SANDBOXED, {"tools:read": True, "tools:write": True})
    return grant_name(name, definition)


@pytest.mark.parametrize("name", ["all-clear-dispatches"])
@pytest.mark.parametrize("audit_failure", [None, "pre", "post", "replay", "typesafe", "concurrent", "receiver-revoke", "receiver-revoke-before-capability", "runner-unknown", "runner-throws"])
async def test_native_host_dispatches_clear_kernel_allowed_shared_outside_effect(
        turn_host, model_server, tmp_path, monkeypatch, name, audit_failure):  # noqa: F811
    case = CASES[name]
    host = turn_host
    grant_tool = install_effect(host, case)
    FakeModel.tool = case["call"]["tool"]
    FakeModel.arguments = case["call"]["arguments"]
    async with paired(tmp_path, monkeypatch, creator=host.mesh_creator, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        recorded = []
        record_turn = node.record_turn

        async def observed_record(*args, **kwargs):
            outcome = await record_turn(*args, **kwargs)
            recorded.extend(kwargs["steps"])
            return outcome

        monkeypatch.setattr(node, "record_turn", observed_record)
        replay_results = []
        concurrent_results = []
        concurrent_invocations = []
        concurrent_capabilities = []
        if audit_failure == "typesafe":
            from orchestrator import hitl

            monkeypatch.setattr(hitl, "hitl_enabled", lambda: True)
            monkeypatch.setattr(host.orch, "_typesafe_turn_requires_confirmation", lambda _chat: True)
        if audit_failure == "replay":
            from orchestrator import node_mesh_admission
            physical_dispatch = node_mesh_admission.dispatch

            async def replay_dispatch(*args, **kwargs):
                first = await physical_dispatch(*args, **kwargs)
                replay_results.append(await physical_dispatch(*args, **kwargs))
                return first

            monkeypatch.setattr(node_mesh_admission, "dispatch", replay_dispatch)
        audit_observations = []
        uncertain_audits = []
        record_audit = host.orch.audit_recorder.record

        async def controlled_audit(event):
            if event.action_type.startswith("mesh.kernel_effect_"):
                audit_observations.append((event.auth_principal, event.inputs_meta))
                if event.action_type == "mesh.kernel_effect_uncertain":
                    uncertain_audits.append(event)
                if (audit_failure == "pre" and event.action_type == "mesh.kernel_effect_started"
                        or audit_failure == "post" and event.action_type == "mesh.kernel_effect_response_success"):
                    return None
            return await record_audit(event)

        monkeypatch.setattr(host.orch.audit_recorder, "record", controlled_audit)
        if audit_failure == "concurrent":
            from orchestrator import node_mesh_admission
            capability_check = node_mesh_admission.source_bound_capability

            async def check_with_competitor(*args, **kwargs):
                capability = await capability_check(*args, **kwargs)
                if capability is not None:
                    async def competitor():
                        async def forbidden():
                            concurrent_invocations.append(True)

                        concurrent_capabilities.append(await capability_check(*args, **kwargs))
                        concurrent_results.append(await node_mesh_admission.dispatch(
                            host.orch, None, args[1], args[2], args[3], args[4], args[5], forbidden))

                    await asyncio.create_task(competitor())
                return capability

            monkeypatch.setattr(node_mesh_admission, "source_bound_capability", check_with_competitor)
        if audit_failure == "receiver-revoke":
            from orchestrator import node_mesh_admission
            physical_dispatch = node_mesh_admission.dispatch

            async def revoke_before_dispatch(*args, **kwargs):
                node.kernel.revoke_tool(grant_tool, case["call"]["effect"]["kind"])
                return await physical_dispatch(*args, **kwargs)

            monkeypatch.setattr(node_mesh_admission, "dispatch", revoke_before_dispatch)
        if audit_failure == "receiver-revoke-before-capability":
            from orchestrator import node_mesh_admission
            capability_check = node_mesh_admission.source_bound_capability

            async def revoked_capability(*args, **kwargs):
                node.kernel.revoke_tool(grant_tool, case["call"]["effect"]["kind"])
                return await capability_check(*args, **kwargs)

            monkeypatch.setattr(node_mesh_admission, "source_bound_capability", revoked_capability)
        unknown_attempts = []
        if audit_failure == "runner-unknown":
            import httpx

            async def accepted_then_timeout(*args, **kwargs):
                unknown_attempts.append(kwargs["json"])
                raise httpx.ReadTimeout("controlled lost response")

            monkeypatch.setattr(host.orch.vessel_executor._client, "post", accepted_then_timeout)
        if audit_failure == "runner-throws":
            async def invoked_then_raises(*args, **kwargs):
                unknown_attempts.append((args, kwargs))
                raise RuntimeError("controlled runner failure after admission")

            monkeypatch.setattr(host.orch.vessel_executor, "invoke", invoked_then_raises)
        await grant(node, laptop, grant_tool, case["call"]["effect"]["kind"])
        chat, request = await ask(node, laptop, device)
        if audit_failure == "runner-throws":
            await until(lambda: bool(unknown_attempts) and bool(uncertain_audits),
                        "the uncertain effect audit", seconds=60)
            assert node.kernel.request(request).state == ak.RequestState.ANSWERED
            assert await bridge.scan() == 0
            assert len(unknown_attempts) == 1
            return
        await until(lambda: bool(completed), "the composed native answer")
        result = completed[0][1]
        subject = [item for item in node.kernel.chat_requests(chat) if item.call is not None]
        parent = node.kernel.request(request)
        steps = [message for message in node.kernel.chat(chat).view.messages
                 if message.role == ak.Role.TOOL and message.kind == "chat.tool_result" and message.turn == parent.turn]
        assert len(subject) == 1 and len(steps) == 1, [
            (message.kind, message.turn, message.status) for message in node.kernel.chat(chat).view.messages
            if message.role == ak.Role.TOOL
        ]
        assert case["expect"]["dispatched"] == 1
        expected_effects = 0 if audit_failure in {"pre", "typesafe", "receiver-revoke", "receiver-revoke-before-capability", "runner-unknown", "runner-throws"} else 1
        assert len(host.sent) == expected_effects
        if expected_effects:
            assert host.sent[0]["tool"] == case["call"]["tool"]
            assert json.loads(host.sent[0]["arguments_json"]) == case["call"]["arguments"]
        assert len(recorded) == 1
        assert recorded[0].tool == case["call"]["tool"]
        assert json.loads(recorded[0].arguments_json) == case["call"]["arguments"]
        assert recorded[0].status == ("error" if audit_failure in {"pre", "post", "typesafe", "receiver-revoke", "receiver-revoke-before-capability", "runner-unknown", "runner-throws"} else "ok")
        assert recorded[0].content
        if audit_failure == "replay":
            assert len(replay_results) == 1 and replay_results[0].error["code"] == "node.ticket_used"
        if audit_failure == "concurrent":
            assert concurrent_capabilities and all(value is None for value in concurrent_capabilities)
            assert concurrent_results and all(
                result.error["code"] == "node.ticket_in_use" for result in concurrent_results)
            assert concurrent_invocations == []
        local_chat = bridge.state.local_for(chat)
        events = host.orch.audit_repo.list_for_user(host.owner, limit=100, conversation_id=local_chat)[0]
        kernel_events = [event for event in events if event.action_type.startswith("mesh.kernel_effect_")]
        expected_audits = (
            {"mesh.kernel_effect_started", "mesh.kernel_effect_response_success"}
            if audit_failure in {None, "replay", "concurrent"} else
            {"mesh.kernel_effect_started"} if audit_failure == "post" else
            {"mesh.kernel_effect_started", "mesh.kernel_effect_uncertain"}
            if audit_failure in {"runner-unknown", "runner-throws"} else
            {"mesh.kernel_effect_started", "mesh.kernel_effect_failed_no_effect"}
            if audit_failure == "receiver-revoke" else set()
        )
        assert {event.action_type for event in kernel_events} == expected_audits
        assert all(event.inputs_meta["approval_source"] == "kernel_ticket" for event in kernel_events)
        assert all(principal == "machine:mesh_request" and meta["approval_source"] == "kernel_ticket"
                   and meta["device_id"] and meta["request_device"]
                   for principal, meta in audit_observations)
        assert subject[0].state == ak.RequestState.ANSWERED
        assert result.status == case["expect"]["turn_status"]
        assert parent.state == ak.RequestState.ANSWERED
        assert steps[0].kind == "chat.tool_result"
        if audit_failure in {"runner-unknown", "runner-throws"}:
            assert len(unknown_attempts) == 1
            await bridge._answer(parent)
            await bridge.scan()
            assert len(unknown_attempts) == 1
            assert {event.action_type for event in kernel_events} == {
                "mesh.kernel_effect_started", "mesh.kernel_effect_uncertain"}


async def test_receiver_refuses_reissued_action_in_new_request_context(
        turn_host, model_server, tmp_path, monkeypatch):  # noqa: F811
    case = CASES["all-clear-dispatches"]
    host = turn_host
    grant_tool = install_effect(host, case)
    FakeModel.tool = case["call"]["tool"]
    FakeModel.arguments = case["call"]["arguments"]
    async with paired(tmp_path, monkeypatch, creator=host.mesh_creator, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        await grant(node, laptop, grant_tool, case["call"]["effect"]["kind"])
        chat, first_request = await ask(node, laptop, device)
        await until(lambda: len(completed) == 1, "the original signed effect", seconds=60)
        original = next(item for item in node.kernel.chat_requests(chat) if item.call is not None)
        assert original.call is not None and len(host.sent) == 1
        await bridge.stop()
        replay_request = laptop.send_request(
            chat, "repeat the previous action", device, None, original.call)
        assert replay_request != original.id
        await until(lambda: node.kernel.request(replay_request).state == ak.RequestState.PENDING,
                    "the duplicate request to reach the receiver")
        node.kernel.claim_request(replay_request)
        with pytest.raises(ak.KernelError.State) as denied:
            await node.kernel.check_host_call(
                replay_request, host.orch.vessel_definitions[(SANDBOXED, case["call"]["tool"])],
                case["call"]["effect"]["kind"])
        assert denied.value.code == "request.call_mismatch"
        assert len(host.sent) == 1
