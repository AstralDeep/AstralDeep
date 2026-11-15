"""Exercises institution owner isolation and the external-identity boundary with the real Kernel and loopback mailbox.
Signed mesh membership permits local sync but does not establish identity for an outside institution.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import institution_runtime, node_mesh, node_mesh_chat
from orchestrator.institution_meshes import Owner
from orchestrator.node_mailbox import MailboxSettings
from shared import institution_profile as ip
from tests.test_node_mesh_kernel_integration import (
    ISS, History, ak, enrolled, free_port, model_turn, open_device, request_state,
    test_issuers as issuers_for_tests, until,
)

KEY = ec.derive_private_key(0x1D2C3B4A5968778695A4B3C2D1E0F00F, ec.SECP256R1())


def make_runtime(tmp_path, *, runner=model_turn, calls=None):
    settings = node_mesh.MeshSettings(
        label="institution node",
        relay=("none",),
        loopback_only=True,
        local_discovery=False,
        internet_lookup=False,
        sync_interval_ms=500,
        mailbox=MailboxSettings(loopback=True, relay=("none",), bind=(f"127.0.0.1:{free_port()}",), max_meshes=10),
    )
    config = ip.InstitutionConfig(key=ip.key_text(KEY), issuers=(ISS,))
    return institution_runtime.InstitutionRuntime(
        config=config,
        base_dir=tmp_path / "node",
        history=History(),
        runner=runner,
        calls=calls,
        settings=settings,
        issuers=issuers_for_tests,
        phi_active=lambda owner: False,
    )


async def join_owner(runtime, tmp_path, subject, marker):
    owner = Owner.verified(ISS, subject)
    node = await runtime.serve_owner(owner)
    mailbox = runtime.meshes._mailbox.address
    laptop = open_device(tmp_path / f"{subject}-laptop", f"{subject}-laptop", marker, mailbox)
    laptop.create_mesh()
    laptop.confirm_mesh_recovery()
    await laptop.start_network()
    offer = await node.pairing_offer()
    await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
    await until(lambda: enrolled(node), f"the node to join {subject}'s mesh")
    return node, laptop, (await node.status())["member_id"]


async def test_external_owner_partitions_preserve_separate_mesh_histories(tmp_path):
    runtime = make_runtime(tmp_path)
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    laptops = []
    try:
        first, alice, first_device = await join_owner(runtime, tmp_path, "alice", 1)
        laptops.append(alice)
        second, bob, second_device = await join_owner(runtime, tmp_path, "bob", 2)
        laptops.append(bob)
        assert first is not second and first_device != second_device
        assert first.kernel.mesh_principal().mesh_id != second.kernel.mesh_principal().mesh_id
        assert runtime.meshes.owner_dir(Owner.verified(ISS, "alice")) != runtime.meshes.owner_dir(Owner.verified(ISS, "bob"))
        alice_chat = alice.create_chat("alice private")
        alice.send_message(alice_chat, "alice's note", None)
        bob_chat = bob.create_chat("bob private")
        bob.send_message(bob_chat, "bob's note", None)
        await until(lambda: any(c.chat_id == alice_chat for c in first.kernel.list_chats()), "alice's paired history")
        await until(lambda: any(c.chat_id == bob_chat for c in second.kernel.list_chats()), "bob's paired history")
        assert all(c.chat_id != bob_chat for c in first.kernel.list_chats())
        assert all(c.chat_id != alice_chat for c in second.kernel.list_chats())
        for laptop, device, chat in ((alice, first_device, alice_chat), (bob, second_device, bob_chat)):
            request = laptop.send_request(chat, "run outside", device, None, None)
            await until(request_state(laptop, request, "REFUSED"), "outside identity refusal")
            assert laptop.request(request).code == "institution.external_identity_unavailable"
            assert not any((m.text or "").startswith("node:") for m in laptop.chat(chat).view.messages)
    finally:
        node_mesh_chat.install(None)
        for laptop in laptops:
            await laptop.stop_network()
        await runtime.stop()


async def test_an_approved_new_device_still_cannot_claim_external_identity(tmp_path):
    calls = []

    async def observed_turn(*args, **kwargs):
        calls.append((args, kwargs))
        return await model_turn(*args, **kwargs)

    runtime = make_runtime(tmp_path, runner=observed_turn)
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    running = []
    try:
        node, laptop, node_device = await join_owner(runtime, tmp_path, "alice", 1)
        running.append(laptop)
        tablet = open_device(tmp_path / "tablet", "tablet", 3, runtime.meshes._mailbox.address)
        await tablet.start_network()
        running.append(tablet)
        offer = await tablet.create_pairing_offer()
        await laptop.approve_pairing(offer.payload, ak.HistoryGrant.ALL)
        await until(lambda: len(tablet.roster().members) == 3, "approved tablet membership")
        tablet_device = (await tablet.mesh_status()).device_id
        await until(lambda: any(m.device_id == tablet_device for m in node.kernel.roster().members), "node roster convergence")
        standing = await node.peer_standing(tablet_device)
        assert standing is not None and not standing.external_identity_available
        chat = tablet.create_chat("local membership")
        tablet.send_message(chat, "hello", None)
        request = tablet.send_request(chat, "run outside", node_device, None, None)
        await until(request_state(tablet, request, "REFUSED"), "outside identity refusal")
        assert tablet.request(request).code == "institution.external_identity_unavailable"
        assert calls == []
    finally:
        node_mesh_chat.install(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()


async def test_the_chat_bridge_entry_point_runs_an_institution_node_on_the_real_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "institution")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISS)
    configured = (await issuers_for_tests())[0]
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", configured["client_id"])
    monkeypatch.setattr("shared.jwks_cache.get_jwks", AsyncMock(return_value=json.loads(configured["jwks_json"])))
    monkeypatch.setenv(ip.KEY_ENV, ip.key_text(KEY))
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "off")
    monkeypatch.setenv("ASTRAL_NODE_MESH_LOOPBACK", "1")
    await node_mesh_chat.start_for(SimpleNamespace(history=History()))
    try:
        runtime = institution_runtime.current()
        assert runtime is not None and runtime.descriptor_fields["profile"] == "institution"
        owner = Owner.verified(ISS, "alice")
        node = await runtime.serve_owner(owner)
        assert (await node.status())["state"] == "not_enrolled" and node.requests_supported
        assert runtime.bridges.get(owner.partition)._runner is None
    finally:
        await node_mesh_chat.stop_all()
