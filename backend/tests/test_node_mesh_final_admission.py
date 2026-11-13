"""Exercises current mesh authority through paired Kernel requests and real protected first-party dispatch.
A controlled model and audit boundary trigger revocation after initial admission without replacing a gate.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.node_mesh_calls import OrchestratorCalls
from tests.test_node_mesh_kernel_integration import ak, paired, pytestmark, until  # noqa: F401
from tests.test_node_mesh_peer_tools import (  # noqa: F401
    AGENT, OWNER, TOOL, model_server, node_host, provide_model, published_spec,
)


@pytest.mark.parametrize("change,code", [
    ("none", None),
    ("entry_stop", "gate.mesh_stopped"),
    ("entry_collision", "gate.mesh_stopped"),
    ("final_stop", "gate.mesh_stopped"),
    ("final_permission", "gate.permission_denied"),
    ("final_definition", "gate.tool_def_changed"),
    ("verify_owner", "node.owner_mismatch"),
    ("verify_permission", "gate.permission_denied"),
    ("verify_kernel", "node.mesh_admission_unavailable"),
])
async def test_current_mesh_authority_is_required_at_physical_dispatch(
    node_host, model_server, tmp_path, monkeypatch, change, code,  # noqa: F811
):
    host = node_host
    actual = OrchestratorCalls(host.orch)
    physical = []
    outcomes = []
    state = {}
    handler = host.agent.handle_mcp_request

    async def observe_handler(*args, **kwargs):
        physical.append("called")
        return await handler(*args, **kwargs)

    monkeypatch.setattr(host.agent, "handle_mcp_request", observe_handler)
    record = host.orch.audit_recorder.record

    async def mutate_after_audit(event):
        result = await record(event)
        if event.action_type == "tool.roll_dice.start":
            state["audited"] = True
            if change == "final_stop":
                state["node"].kernel.raise_stop()
            elif change == "final_permission":
                host.orch.tool_permissions.set_agent_scopes(OWNER, AGENT, {"tools:read": False})
            elif change == "final_definition":
                host.skill.description += " changed after admission"
        return result

    monkeypatch.setattr(host.orch.audit_recorder, "record", mutate_after_audit)

    class Calls:
        async def resolve(self, owner, tool):
            return await actual.resolve(owner, tool)

        async def authorize(self, *args):
            authority = await actual.authorize(*args)
            if change in {"entry_stop", "entry_collision"}:
                state["node"].kernel.raise_stop()
            if change == "entry_collision":
                host.orch.security_flags = {AGENT: {TOOL: {"blocked": True}}}
            return authority

        async def run(self, *args):
            result = await actual.run(*args)
            outcomes.append(result)
            return result

    async with paired(
        tmp_path, monkeypatch, calls=Calls(), peer_tools=published_spec(host, actual),
        history=host.orch.history,
    ) as (node, laptop, _node_device, _bridge):
        state["node"] = node
        verify = node.verify_ticket

        async def held_verification(*args):
            result = await verify(*args)
            if state.get("audited"):
                state["physical_verifications"] = state.get("physical_verifications", 0) + 1
                if state["physical_verifications"] == 2:
                    await asyncio.sleep(0)
                    if change == "verify_owner":
                        _bridge._owner_of = lambda: "other-owner"
                    elif change == "verify_permission":
                        host.orch.tool_permissions.set_agent_scopes(OWNER, AGENT, {"tools:read": False})
                    elif change == "verify_kernel":
                        kernel = node.kernel
                        class Replacement:
                            def __getattr__(self, name):
                                return getattr(kernel, name)
                        node._kernel = Replacement()
            return result

        monkeypatch.setattr(node, "verify_ticket", held_verification)
        provide_model(laptop, model_server)
        chat = laptop.create_chat("current tool authority")
        laptop.send_message(chat, "roll two dice", None)
        await until(lambda: any(c.chat_id == chat for c in node.kernel.list_chats()), "chat sync")
        laptop.grant_tool(TOOL, "*")
        result = await asyncio.wait_for(laptop.run_turn(chat), 60)
        requests = [r for r in laptop.chat_requests(chat) if r.call is not None]
        assert result.status == "completed" and len(requests) == 1
        assert requests[0].state == ak.RequestState.ANSWERED
        assert len(outcomes) == 1
        assert outcomes[0].code == code
        assert len(physical) == (1 if change == "none" else 0)

        events = host.orch.audit_repo.list_for_user(
            OWNER, limit=100, conversation_id=_bridge.state.local_for(chat),
        )[0]
        if change == "none":
            assert {event.action_type for event in events} >= {"tool.roll_dice.start", "tool.roll_dice.end"}
        else:
            assert any(event.action_type == "tool.roll_dice.denied"
                       and event.outcome_detail == code and event.inputs_meta["gate"] == "node_mesh"
                       for event in events)
