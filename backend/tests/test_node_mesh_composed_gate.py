"""Runs shared composed gate cases through the paired native Kernel request route and the node's protected tool dispatch.
The scripted model and sandbox executor supply controlled responses while the mesh, grants, tickets, authorization, and journal are real."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import vessel_executor as ve
from orchestrator.node_mesh_calls import definition_hash, grant_name
from shared.protocol import AgentSkill
from tests.test_node_mesh_kernel_integration import ak, paired, until  # noqa: F401
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


