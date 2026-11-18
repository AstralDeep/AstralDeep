"""Proves personal-node turns and peer tools run from real signed Kernel enrollment with IAM disabled.
The fake model is loopback-only; principal proof, ticket minting, SQLite state, dispatch and audit are real."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


from orchestrator.node_mesh_calls import OrchestratorCalls
from shared import node_identity
from tests.test_node_mesh_kernel_integration import ak, paired, until  # noqa: F401
from tests.test_node_mesh_peer_tools import (  # noqa: F401
    TOOL, model_server, node_host, provide_model, published_spec,
)
from tests.test_node_mesh_turn_tools import (  # noqa: F401
    AGENT, bridge_for, diag, dice_grant, grant, run_turn, turn_host,
)


def forbid_iam(host, monkeypatch):
    monkeypatch.setattr(host.orch, "derive_machine_authority", AsyncMock(side_effect=AssertionError("No per-request IAM")))
    monkeypatch.setattr(host.orch.delegation, "exchange_token_for_agent", AsyncMock(side_effect=AssertionError("No Keycloak exchange")))


def verify_actual_principal(node):
    proof = node.kernel.mesh_principal()
    assert proof.mesh_id == node.kernel.roster().mesh_id
    assert node_identity.owner_id_for_mesh(proof.mesh_id) == node_identity.bound_owner()
    assert not hasattr(proof, "identities")


async def test_a_real_enrolled_node_answers_a_tool_turn_without_any_iam_call(turn_host, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    forbid_iam(host, monkeypatch)
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, device, bridge):
        verify_actual_principal(node)
        await grant(node, laptop, dice_grant(host))
        result = await run_turn(host, node, laptop, device, bridge, [(TOOL, {"n": 2})])
    assert result.request.state == ak.RequestState.ANSWERED, diag(result, host)
    assert len(host.dispatched[AGENT]) == 1 and len(result.minted) == 1, diag(result, host)
    host.orch.derive_machine_authority.assert_not_awaited()
    host.orch.delegation.exchange_token_for_agent.assert_not_awaited()


async def test_a_real_ticketed_peer_call_runs_without_an_offline_grant(node_host, model_server, tmp_path, monkeypatch):  # noqa: F811
    host = node_host
    forbid_iam(host, monkeypatch)
    calls = OrchestratorCalls(host.orch)
    async with paired(tmp_path, monkeypatch, creator=host.mesh_creator, calls=calls, peer_tools=published_spec(host, calls), history=host.orch.history) as (
        node, laptop, device, bridge,
    ):
        verify_actual_principal(node)
        provide_model(laptop, model_server)
        chat = laptop.create_chat("personal peer authority")
        laptop.send_message(chat, "roll two dice on the node", None)
        await until(lambda: any(c.chat_id == chat for c in node.kernel.list_chats()), "the peer chat to reach the node")
        laptop.grant_tool(TOOL, "*")
        result = await asyncio.wait_for(laptop.run_turn(chat), 60)
        requests = [request for request in laptop.chat_requests(chat) if request.call is not None]
        assert result.status == "completed" and len(requests) == 1
        assert requests[0].state == ak.RequestState.ANSWERED and requests[0].to_device == device
    host.orch.derive_machine_authority.assert_not_awaited()
    host.orch.delegation.exchange_token_for_agent.assert_not_awaited()


async def pending_for(node, laptop, device):
    chat = laptop.create_chat("unavailable receiver identity")
    laptop.send_message(chat, "hello node", None)
    await until(lambda: any(item.chat_id == chat for item in node.kernel.list_chats()), "the chat to sync")
    request = laptop.send_request(chat, "must not run", device, None, None)
    await until(lambda: any(item.id == request for item in node.kernel.pending_requests_for_me()), "the pending request to sync")
    return request


@pytest.mark.parametrize("owner,code", [
    ("sha256:" + "7" * 64, "node.owner_mismatch"),
    ("mesh." + "7" * 64, "node.owner_mismatch"),
])
async def test_an_environment_owner_without_the_real_principal_is_refused(turn_host, tmp_path, monkeypatch, owner, code):  # noqa: F811
    host = turn_host
    forbid_iam(host, monkeypatch)
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, device, bridge):
        verify_actual_principal(node)
        monkeypatch.setenv("ASTRAL_NODE_OWNER", owner)
        request = await pending_for(node, laptop, device)
        await bridge.scan()
        assert (await node.status())["owner_binding"] == {"state": "refused", "code": code}
        assert node.kernel.request(request).state == ak.RequestState.PENDING
        assert node.personal_owner_id() is None
    assert not any(host.dispatched.values())
    host.orch.derive_machine_authority.assert_not_awaited()


async def test_binding_without_the_principal_accessor_refuses_instead_of_using_iam(turn_host, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    forbid_iam(host, monkeypatch)
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, device, bridge):
        monkeypatch.setattr(node.kernel, "mesh_principal", None, raising=False)
        request = await pending_for(node, laptop, device)
        await bridge.scan()
        assert (await node.status())["owner_binding"] == {"state": "refused", "code": "principal_unsupported"}
        assert node.kernel.request(request).state == ak.RequestState.PENDING
        assert node.personal_owner_id() is None
    assert not any(host.dispatched.values())
    host.orch.derive_machine_authority.assert_not_awaited()


@pytest.mark.parametrize("field", ["call_id", "continues"])
async def test_mutated_pending_request_identity_is_refused_before_real_dispatch(node_host, model_server, tmp_path, monkeypatch, field):  # noqa: F811
    host = node_host
    forbid_iam(host, monkeypatch)
    dispatched = []
    invoke = host.agent.handle_mcp_request

    async def observed(*args, **kwargs):
        dispatched.append(True)
        return await invoke(*args, **kwargs)

    monkeypatch.setattr(host.agent, "handle_mcp_request", observed)
    calls = OrchestratorCalls(host.orch)
    async with paired(tmp_path, monkeypatch, creator=host.mesh_creator, calls=calls, peer_tools=published_spec(host, calls), history=host.orch.history) as (
        node, laptop, device, bridge,
    ):
        verify_actual_principal(node)
        pending = node.pending_requests

        async def mutated():
            requests = await pending()
            for request in requests:
                if request.call is not None:
                    if field == "call_id":
                        request.call.call_id = "mutated-call"
                    else:
                        request.continues = "sha256:" + "0" * 64
            return requests

        monkeypatch.setattr(node, "pending_requests", mutated)
        provide_model(laptop, model_server)
        chat = laptop.create_chat("request identity")
        laptop.send_message(chat, "roll two dice on the node", None)
        await until(lambda: any(item.chat_id == chat for item in node.kernel.list_chats()), "the chat to sync")
        laptop.grant_tool(TOOL, "*")
        running = asyncio.create_task(laptop.run_turn(chat))

        def settled():
            return dispatched or any(request.state == ak.RequestState.REFUSED for request in node.kernel.chat_requests(chat))

        await until(settled, "the raw identity check")
        if dispatched:
            laptop.interrupt(chat)
        await asyncio.wait_for(running, 60)
        assert not dispatched
        requests = [request for request in node.kernel.chat_requests(chat) if request.call is not None]
        assert len(requests) == 1 and requests[0].state == ak.RequestState.REFUSED
        assert requests[0].code == "request.call_mismatch"
