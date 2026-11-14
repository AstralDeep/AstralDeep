"""Proves personal-node turns and peer tools run from real signed Kernel enrollment with IAM disabled.
The fake model is loopback-only; principal proof, ticket minting, SQLite state, dispatch and audit are real."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock


from orchestrator.node_mesh_calls import OrchestratorCalls
from shared import node_identity
from tests.test_node_mesh_kernel_integration import ISS, OWNER, ak, paired, until  # noqa: F401
from tests.test_node_mesh_peer_tools import (  # noqa: F401
    TOOL, model_server, node_host, provide_model, published_spec,
)
from tests.test_node_mesh_turn_tools import (  # noqa: F401
    AGENT, bridge_for, diag, dice_grant, grant, run_turn, turn_host,
)


def forbid_iam(host, monkeypatch):
    class NoGrants:
        def __getattr__(self, _name):
            raise AssertionError("Offline IAM grants are not personal mesh authority")

    monkeypatch.setattr(host.orch, "offline_grants", NoGrants())
    monkeypatch.setattr(host.orch, "derive_machine_authority", AsyncMock(side_effect=AssertionError("No per-request IAM")))
    monkeypatch.setattr(host.orch.delegation, "exchange_token_for_agent", AsyncMock(side_effect=AssertionError("No Keycloak exchange")))


def verify_actual_principal(node):
    proof = node.kernel.mesh_principal()
    assert proof.mesh_id == node.kernel.roster().mesh_id
    assert {(identity.issuer, identity.subject) for identity in proof.identities} == {(ISS, "user-1")}
    assert OWNER == node_identity.canonical_owner_id(ISS, "user-1")


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
    async with paired(tmp_path, monkeypatch, calls=calls, peer_tools=published_spec(host, calls), history=host.orch.history) as (
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


async def test_an_environment_owner_without_the_real_principal_is_refused(turn_host, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    forbid_iam(host, monkeypatch)
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, device, bridge):
        verify_actual_principal(node)
        wrong = "oidc.abc.owner"
        monkeypatch.setenv("ASTRAL_NODE_OWNER", wrong)
        bridge._owner_of = lambda: wrong
        result = await run_turn(host, node, laptop, device, bridge, [])
    assert result.request.state == ak.RequestState.REFUSED and result.request.code == "node.owner_mismatch"
    assert not any(host.dispatched.values()) and not result.minted
    host.orch.derive_machine_authority.assert_not_awaited()


async def test_binding_without_the_principal_accessor_refuses_instead_of_using_iam(turn_host, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    forbid_iam(host, monkeypatch)
    async with bridge_for(host, tmp_path, monkeypatch) as (node, laptop, device, bridge):
        monkeypatch.setattr(node.kernel, "mesh_principal", None, raising=False)
        result = await run_turn(host, node, laptop, device, bridge, [])
    assert result.request.state == ak.RequestState.REFUSED and result.request.code == "node.principal_unsupported"
    assert not any(host.dispatched.values()) and not result.minted
    host.orch.derive_machine_authority.assert_not_awaited()
