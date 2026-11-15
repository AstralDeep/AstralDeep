"""Runs shared composed gate cases through the paired native Kernel request route and the node's protected tool dispatch.
The scripted model and sandbox executor supply controlled responses while the mesh, grants, tickets, authorization, and journal are real."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import vessel_executor as ve
from orchestrator.node_mesh_calls import OrchestratorCalls, definition_hash, grant_name
from shared.protocol import AgentSkill
from tests.test_node_mesh_kernel_integration import ak, paired, until  # noqa: F401
from tests.test_node_mesh_kernel_request_tools import ask, setup_native
from tests.test_node_mesh_peer_tools import FakeModel, model_server, node_host  # noqa: F401
from tests.test_node_mesh_turn_tools import OWNER, SANDBOXED, grant, turn_host  # noqa: F401

FIXTURE = Path(__file__).resolve().parents[2] / "specs/105-decentralized-personal-mesh/contracts/conformance/composed_gate.json"
CASES = {case["name"]: case for case in json.loads(FIXTURE.read_text())["cases"]}


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
    host.orch.tool_permissions.set_agent_scopes(OWNER, SANDBOXED, {"tools:read": True, "tools:write": True})
    return grant_name(name, definition)


@pytest.mark.parametrize("name", ["all-clear-dispatches"])
async def test_native_host_requires_interactive_confirmation_for_shared_outside_effect(turn_host, model_server, tmp_path, monkeypatch, name):  # noqa: F811
    case = CASES[name]
    host = turn_host
    grant_tool = install_effect(host, case)
    FakeModel.tool = case["call"]["tool"]
    FakeModel.arguments = case["call"]["arguments"]
    async with paired(tmp_path, monkeypatch, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True) as (node, laptop, device, bridge):
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        recorded = []
        record_turn = node.record_turn

        async def observed_record(*args, **kwargs):
            outcome = await record_turn(*args, **kwargs)
            recorded.extend(kwargs["steps"])
            return outcome

        monkeypatch.setattr(node, "record_turn", observed_record)
        await grant(node, laptop, grant_tool, case["call"]["effect"]["kind"])
        chat, request = await ask(node, laptop, device)
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
        assert host.sent == []
        assert len(recorded) == 1
        assert recorded[0].tool == case["call"]["tool"]
        assert json.loads(recorded[0].arguments_json) == case["call"]["arguments"]
        assert recorded[0].status == "error" and recorded[0].code == "node.tool_failed"
        assert "Confirmation requires an interactive signed-in conversation" in recorded[0].content
        assert subject[0].state == ak.RequestState.ANSWERED
        assert result.status == case["expect"]["turn_status"]
        assert parent.state == ak.RequestState.ANSWERED
        assert steps[0].kind == "chat.tool_result"
