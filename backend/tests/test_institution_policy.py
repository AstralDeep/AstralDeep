"""Covers orchestrator/institution_policy.py through the request bridge: an unavailable external identity is refused before anything runs, PHI work from a peer outside the PHI group is refused, a served ticket still passes the kernel check and the node's own tool gates, and one owner's bridge never sees another's requests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import node_mesh, node_mesh_chat as nmc
from orchestrator.institution_policy import InstitutionPolicy
from tests import fake_astral_kernel as fake
from tests.test_node_mesh_chat import (
    FakeCalls,
    History,
    StubMailbox,
    answering,
    call_for,
    serve,
    settle,
)

OWNER = "owner-1"


@pytest.fixture(autouse=True)
def clean():
    fake.Kernel.instances.clear()
    yield
    nmc.install(None)
    node_mesh.install(None)


async def build(tmp_path, *, owner=OWNER, runner=None, calls=None, phi_member=True, external_identity=True):
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=fake, mailbox=StubMailbox())
    await mesh.start()
    kernel = fake.Kernel.instances[-1]
    kernel.join()
    kernel.phi_member = phi_member
    if external_identity:
        async def external_standing(device_id):
            roster = kernel.roster()
            member = next((member for member in roster.members if member.device_id == device_id), None)
            if member is not None:
                return SimpleNamespace(device_id=device_id, phi=member.phi, external_identity_available=True)
            return None

        mesh.peer_standing = external_standing
    history = History()
    bridge = nmc.MeshChatBridge(
        mesh,
        owner_id=owner,
        history=history,
        runner=runner or answering("served"),
        calls=calls,
        state_path=tmp_path / "mesh-chats.json",
        policy=nmc.NodePolicy(),
        phi_active=lambda _owner: False,
        admission=InstitutionPolicy(mesh).check,
        poll_seconds=0.05,
        turn_seconds=5.0,
    )
    await bridge.start()
    return mesh, kernel, history, bridge


async def test_a_member_with_separately_verified_external_identity_is_served(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.claimed == ["r1"] and kernel.request("r1").state == fake.RequestState.ANSWERED
    await bridge.stop()


async def test_mesh_membership_alone_cannot_supply_external_institution_identity(tmp_path):
    ran = []

    async def runner(*args):
        ran.append(args)

    mesh, kernel, history, bridge = await build(tmp_path, runner=runner, external_identity=False)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "institution.external_identity_unavailable")]
    assert not ran and not kernel.claimed and not kernel.recorded and not history.created
    await bridge.stop()


async def test_separately_verified_external_identity_is_required_on_every_request(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    mesh.peer_standing = AsyncMock(return_value=SimpleNamespace(phi=True, external_identity_available=False))
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c")
    await serve(bridge, kernel, "r1")
    mesh.peer_standing.return_value.external_identity_available = True
    kernel.add_request("r2", "c")
    await serve(bridge, kernel, "r2")
    assert kernel.refused == [("r1", "institution.external_identity_unavailable")]
    assert kernel.request("r2").state == fake.RequestState.ANSWERED
    await bridge.stop()


async def test_a_request_from_a_device_that_is_not_a_member_is_refused(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", author="dev-stranger")
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "institution.not_a_member")] and not kernel.claimed
    await bridge.stop()


@pytest.mark.parametrize("source", ["request", "chat"])
async def test_phi_work_from_a_peer_outside_the_phi_group_is_refused(tmp_path, source):
    mesh, kernel, history, bridge = await build(tmp_path)
    kernel.phone_phi = False
    kernel.add_chat("c", [fake.message("m1", "chart note", confidentiality="phi" if source == "chat" else "private")])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PHI" if source == "request" else "PRIVATE", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "institution.peer_not_phi")] and not kernel.claimed and not kernel.recorded
    await bridge.stop()


async def test_phi_work_from_a_phi_member_is_served_and_labeled_phi(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path)
    nmc.install(bridge)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PHI", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert kernel.request("r1").state == fake.RequestState.ANSWERED
    assert kernel.recorded[0][1].labels.confidentiality == fake.PeerConfidentiality.PHI
    await bridge.stop()


async def test_a_node_outside_the_phi_group_still_refuses_phi_work_from_a_phi_peer(tmp_path):
    mesh, kernel, history, bridge = await build(tmp_path, phi_member=False)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", labels=fake.peer_labels("PHI", "OWNER"))
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "node.phi_not_approved")]
    await bridge.stop()


async def test_phi_labels_on_a_ticket_from_a_non_phi_peer_are_refused_after_the_ticket_check(tmp_path):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.phone_phi = False
    kernel.add_chat("c", [])
    kernel.ticket_labels = fake.peer_labels("PHI", "TOOL_TRUSTED")
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.verified and kernel.refused == [("r1", "institution.peer_not_phi")]
    assert not calls.ran and not kernel.claimed
    await bridge.stop()


async def test_a_served_ticket_still_passes_the_kernel_check_and_the_nodes_own_gates(tmp_path):
    calls = FakeCalls()
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    nmc.install(bridge)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert len(kernel.verified) == 1 and len(calls.ran) == 1
    await bridge.stop()


async def test_a_ticket_the_nodes_own_gates_deny_is_refused_even_from_a_fresh_member(tmp_path):
    calls = FakeCalls(authorize_error="gate.permission_denied")
    mesh, kernel, history, bridge = await build(tmp_path, calls=calls)
    kernel.add_chat("c", [])
    kernel.add_request("r1", "c", call=call_for())
    await serve(bridge, kernel, "r1")
    assert kernel.refused == [("r1", "gate.permission_denied")] and not calls.ran
    await bridge.stop()


async def test_one_owners_bridge_never_sees_another_owners_activity(tmp_path):
    first = await build(tmp_path / "a", owner="owner-a", runner=answering("a"))
    second = await build(tmp_path / "b", owner="owner-b", runner=answering("b"))
    bridges = nmc.BridgeSet()
    bridges.add("owner-a", first[3])
    bridges.add("owner-b", second[3])
    nmc.install(bridges)
    nmc.observe_message("owner-a", "local-a", "user", "question for a")
    nmc.observe_message("owner-a", "local-a", "assistant", "answer for a")
    await settle(lambda: first[1].recorded)
    await asyncio.sleep(0.1)
    assert [text for _chat, text, _c, _id in first[1].sent] == ["question for a"]
    assert second[1].sent == [] and second[1].recorded == [] and second[2].created == []
    nmc.observe_message("owner-unknown", "local-x", "user", "nobody serves this")
    await asyncio.sleep(0.05)
    assert second[1].sent == []
    await bridges.stop()
