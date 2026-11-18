"""Verifies local mesh creation and membership stay independent of outside identity-provider configuration.
The real Kernel and a loopback mailbox carry signed history between owner-approved devices.
"""

from __future__ import annotations

from tests.test_node_mesh_kernel_integration import open_device


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


