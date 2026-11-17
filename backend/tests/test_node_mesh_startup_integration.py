"""Qualifies production personal receiver startup with a genuinely approved mesh pairing and sealed model turn.
Kernel membership, durable owner binding, SQLite authority, signed tickets and protected tool dispatch remain real.
"""

import os
from unittest.mock import AsyncMock

from orchestrator import node_mesh, node_mesh_chat
from orchestrator.session_store import assert_production_posture
from shared import node_identity
from tests.test_node_mesh_kernel_integration import Listener, ak, enrolled, free_port, until
from tests.test_node_mesh_kernel_integration import adopt_phi_group
from tests.test_node_mesh_kernel_request_tools import ask, setup_native
from tests.test_node_mesh_peer_tools import FakeModel, REPLY, model_server, node_host  # noqa: F401
from tests.test_node_mesh_turn_tools import dice_grant, grant


async def test_fresh_production_startup_binds_the_approved_mesh_and_answers_a_sealed_turn(
    node_host, model_server, monkeypatch,  # noqa: F811
):
    host = node_host
    for name, value in {
        node_mesh.RELAY_ENV: "none",
        node_mesh.LOOPBACK_ENV: "1",
        node_mesh.DISCOVERY_ENV: "0",
        node_mesh.LOOKUP_ENV: "0",
        node_mesh.SYNC_ENV: "200",
        "ASTRAL_NODE_MAILBOX": "1",
        "ASTRAL_NODE_MAILBOX_BIND": f"127.0.0.1:{free_port()}",
    }.items():
        monkeypatch.setenv(name, value)
    assert node_identity.bound_owner() is None
    assert node_identity.OWNER_ENV not in os.environ
    host.agent.handle_mcp_request = AsyncMock(wraps=host.agent.handle_mcp_request)
    laptop = None
    assert_production_posture()
    await node_mesh_chat.start_for(host.orch)
    try:
        node, bridge = node_mesh.current(), node_mesh_chat.active()
        await bridge.scan()
        assert (await node.status())["owner_binding"]["state"] == "setup_required"
        host.mesh_creator.config.network.mailbox = (await node.status())["mailbox"]["address"]
        laptop = ak.Kernel.open(host.mesh_creator.config, host.mesh_creator.signer, None, None, Listener())
        await laptop.start_network()
        offer = await node.pairing_offer()
        await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        await until(lambda: enrolled(node), "the production node to accept owner-approved pairing")
        await until(lambda: node.personal_owner_id() == host.owner, "the production roster callback to bind ownership")
        assert node_identity.bound_owner() == host.owner
        assert (await node.status())["owner_binding"] == {"state": "ready", "code": None}
        device = (await node.status())["member_id"]
        await adopt_phi_group(node, laptop)
        completed = await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        await grant(node, laptop, dice_grant(host))
        chat, request = await ask(node, laptop, device)
        await until(lambda: bool(completed), "the production node's Kernel-owned answer")
        assert completed[0][1].status == "completed" and completed[0][1].text == REPLY
        assert laptop.request(request).state == ak.RequestState.ANSWERED
        assert host.agent.handle_mcp_request.await_count == 1, {
            "result": completed[0][1],
            "requests": [(str(item.state), item.code) for item in node.kernel.chat_requests(chat)],
            "offered": [item.get("tools") for item in FakeModel.seen],
        }
        host.orch.derive_machine_authority.assert_not_awaited()
        host.orch.delegation.exchange_token_for_agent.assert_not_awaited()
    finally:
        if laptop is not None:
            await laptop.stop_network()
        await node_mesh_chat.stop_all()
