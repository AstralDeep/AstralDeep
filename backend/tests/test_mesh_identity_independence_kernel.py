"""Verifies local mesh creation and membership stay independent of outside identity-provider configuration.
The real Kernel and a loopback mailbox carry signed history between owner-approved devices.
"""

from __future__ import annotations

from orchestrator import node_mesh_chat
from tests.test_institution_kernel_integration import make_runtime
from tests.test_node_mesh_kernel_integration import ak, open_device, until


async def test_local_mesh_creation_does_not_depend_on_an_identity_provider(tmp_path):
    kernel = open_device(tmp_path / "offline", "offline", 1, None)
    kit = kernel.create_mesh()
    pending = await kernel.mesh_status()
    assert pending.recovery_pending
    assert pending.mesh_id == kit.mesh_id
    assert len(kit.words.split()) == 24
    kernel.confirm_mesh_recovery()
    status = await kernel.mesh_status()
    assert not status.recovery_pending and status.mesh_id == kit.mesh_id
    assert kernel.mesh_principal().mesh_id == kit.mesh_id
    chat = kernel.create_chat("local history")
    kernel.send_message(chat, "saved without sign-in", None)
    assert any(message.text == "saved without sign-in" for message in kernel.chat(chat).view.messages)


async def test_changing_outside_identity_settings_does_not_change_mesh_ownership(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    await runtime.start()
    running = []
    try:
        mailbox = runtime.meshes._mailbox.address
        laptop = open_device(tmp_path / "laptop", "laptop", 1, mailbox)
        kit = laptop.create_mesh()
        laptop.confirm_mesh_recovery()
        await laptop.start_network()
        running.append(laptop)
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://unavailable.invalid/other")
        tablet = open_device(tmp_path / "tablet", "tablet", 2, mailbox)
        await tablet.start_network()
        running.append(tablet)
        offer = await tablet.create_pairing_offer()
        await laptop.approve_pairing(offer.payload, ak.HistoryGrant.ALL)
        await until(lambda: len(tablet.roster().members) == 2, "approved membership without sign-in")
        assert tablet.mesh_principal().mesh_id == laptop.mesh_principal().mesh_id == kit.mesh_id
        chat = laptop.create_chat("shared local history")
        laptop.send_message(chat, "same mesh after outside identity change", None)
        await until(lambda: any(c.chat_id == chat for c in tablet.list_chats()), "signed history convergence")
        assert any(message.text == "same mesh after outside identity change" for message in tablet.chat(chat).view.messages)
    finally:
        node_mesh_chat.install(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()
