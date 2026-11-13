"""Runs an institution node with the real kernel binding and mailbox: two owners are served by separate peers that cannot see each other's chats, a device whose identity attestation is missing is refused with a dotted code until it attests again, and a fresh device is served.
It runs only where the pinned kernel wheel and the mailbox binary are installed, using test token issuers, software keys and loopback networking."""

from __future__ import annotations

import shutil

import pytest

pytest.importorskip("astral_kernel")
pytestmark = pytest.mark.skipif(shutil.which("astral-mailbox") is None, reason="mailbox binary not installed")

from types import SimpleNamespace  # noqa: E402

from orchestrator import institution_runtime, node_mesh, node_mesh_chat  # noqa: E402
from orchestrator.institution_meshes import Owner  # noqa: E402
from orchestrator.node_mailbox import MailboxSettings  # noqa: E402
from shared import institution_profile as ip  # noqa: E402
from tests.test_node_mesh_kernel_integration import (  # noqa: E402
    ISS,
    NODE_TEXT,
    History,
    ak,
    enrolled,
    free_port,
    model_turn,
    open_device,
    request_state,
    test_issuers as issuers_for_tests,
    token,
    until,
)

from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

KEY = ec.derive_private_key(0x1D2C3B4A5968778695A4B3C2D1E0F00F, ec.SECP256R1())


def make_runtime(tmp_path):
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
        runner=model_turn,
        calls=None,
        settings=settings,
        issuers=issuers_for_tests,
        phi_active=lambda owner: False,
    )


async def join_owner(runtime, tmp_path, subject, marker):
    owner = Owner.verified(ISS, subject)
    node = await runtime.serve_owner(owner)
    mailbox = runtime.meshes._mailbox.address
    laptop = open_device(tmp_path / f"{subject}-laptop", f"{subject}-laptop", marker, mailbox)
    created = laptop.begin_mesh_creation()
    laptop.complete_mesh_creation(token(created.nonce))
    await laptop.start_network()
    offer = await node.pairing_offer()
    await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
    await until(lambda: enrolled(node), f"the node to join {subject}'s mesh")
    return node, laptop, (await node.status())["member_id"]


async def test_two_owners_are_served_by_separate_peers_and_never_see_each_others_chats(tmp_path):
    runtime = make_runtime(tmp_path)
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    laptops = []
    try:
        alice_node, alice, alice_device = await join_owner(runtime, tmp_path, "alice", 1)
        bob_node, bob, bob_device = await join_owner(runtime, tmp_path, "bob", 2)
        laptops += [alice, bob]
        assert alice_node is not bob_node and alice_device != bob_device
        assert (await alice_node.status())["mesh_id"] != (await bob_node.status())["mesh_id"]
        assert runtime.meshes.owner_dir(Owner.verified(ISS, "alice")) != runtime.meshes.owner_dir(Owner.verified(ISS, "bob"))

        secret_chat = bob.create_chat("bob private")
        bob.send_message(secret_chat, "bob's note", None)
        chat = alice.create_chat("ask the node")
        alice.send_message(chat, "hello", None)
        request = alice.send_request(chat, "summarize", alice_device, None, None)
        await until(request_state(alice, request, "ANSWERED"), "the node to answer alice")
        await until(lambda: any((m.text or "") == NODE_TEXT + "summarize" for m in alice.chat(chat).view.messages), "alice to read the answer")
        await until(lambda: any(c.chat_id == secret_chat for c in bob_node.kernel.list_chats()), "bob's peer to hold bob's chat")
        assert all(c.chat_id != secret_chat for c in alice_node.kernel.list_chats())
        assert all(c.chat_id != chat for c in bob_node.kernel.list_chats())
        assert alice_node.kernel.pending_requests_for_me() == [] and bob_node.kernel.pending_requests_for_me() == []
    finally:
        node_mesh_chat.install(None)
        for laptop in laptops:
            await laptop.stop_network()
        await runtime.stop()


async def test_a_device_without_a_current_attestation_is_refused_until_it_attests_again(tmp_path):
    runtime = make_runtime(tmp_path)
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
        await until(lambda: len(tablet.roster().members) == 3, "the tablet to join")
        tablet_device = (await tablet.mesh_status()).device_id
        await until(lambda: any(m.device_id == tablet_device and m.unattested for m in node.kernel.roster().members), "the node to see the tablet unattested")

        chat = tablet.create_chat("from the tablet")
        tablet.send_message(chat, "hello", None)
        first = tablet.send_request(chat, "run before attesting", node_device, None, None)
        await until(request_state(tablet, first, "REFUSED"), "the node to refuse the unattested tablet")
        assert tablet.request(first).code == "institution.attestation_stale"
        assert not any((m.text or "").startswith(NODE_TEXT) for m in tablet.chat(chat).view.messages)

        tablet.reattest(token(tablet.begin_attestation().nonce))
        await until(lambda: any(m.device_id == tablet_device and not m.unattested for m in node.kernel.roster().members), "the node to see the new attestation")
        second = tablet.send_request(chat, "run after attesting", node_device, None, None)
        await until(request_state(tablet, second, "ANSWERED"), "the node to serve the attested tablet")
    finally:
        node_mesh_chat.install(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()


async def test_the_chat_bridge_entry_point_runs_an_institution_node_on_the_real_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "institution")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", ISS)
    monkeypatch.setenv(ip.KEY_ENV, ip.key_text(KEY))
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "off")
    monkeypatch.setenv("ASTRAL_NODE_MESH_LOOPBACK", "1")
    await node_mesh_chat.start_for(SimpleNamespace(history=History()))
    try:
        runtime = institution_runtime.current()
        assert runtime is not None and runtime.descriptor_fields["profile"] == "institution"
        node = await runtime.serve_owner(Owner.verified(ISS, "alice"))
        assert (await node.status())["state"] == "not_enrolled" and node.requests_supported
    finally:
        await node_mesh_chat.stop_all()
