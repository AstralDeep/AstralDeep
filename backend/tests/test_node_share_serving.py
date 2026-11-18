"""Proves a personal node serves a chat its owner shared with a verified contact while the owner's own devices are closed, and that the node itself never invites, verifies, grants or revokes.
It drives the real kernel binding through devtools/dev_mesh.py and tests/helpers/dev_mesh.py with the two seeded development meshes, and checks the node holds no shared item and gains no shared/ folder unless a contact device dials it, when the kernel keeps one roster cache file for that contact."""

from __future__ import annotations

import pytest

import astral_kernel as ak
from devtools import dev_mesh
from orchestrator import node_mesh
from shared import node_identity
from tests.helpers.dev_mesh import contact_mesh, dev_node, node_settings, until, verified_contacts


def _no_shared_folder(root):
    return [path for path in root.rglob("*") if path.name == "shared"] == []


async def _shared_with_phone(a, b):
    await verified_contacts(a.owner, b.owner)
    chat = a.owner.kernel.create_chat("Plan")
    a.owner.kernel.send_message(chat, "first note", None)
    grant = a.owner.grant_view(chat, b.owner.mesh_id)
    await until(lambda: [x for x in b.phone.kernel.contacts()], "the contact to reach the second phone")
    await until(lambda: a.node.kernel.shares(), "the share to reach the node")
    return chat, grant


async def test_the_node_serves_a_shared_chat_to_the_contacts_phone_while_the_owner_is_closed(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as a, contact_mesh(tmp_path, monkeypatch) as b:
        _, grant = await _shared_with_phone(a, b)
        b.phone.kernel.remember_contact_device(a.owner.mesh_id, await a.node.kernel.endpoint_address())
        await a.owner.stop()
        view = await b.phone.read_shared(a.owner.mesh_id, grant.id)
        assert [message.text for message in view.messages] == ["first note"]
        assert [item.state for item in b.phone.kernel.shared_with_me()] == ["ready"]
        assert a.node.kernel.shared_with_me() == []
        cached = sorted((tmp_path / "node").rglob("shared/*"))
        assert len(cached) == 1 and cached[0].suffix == ".sqlite3"


async def test_the_node_refuses_to_invite_verify_grant_or_revoke(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as a, contact_mesh(tmp_path, monkeypatch) as b:
        chat, grant = await _shared_with_phone(a, b)
        kernel = a.node.kernel
        await until(lambda: any(item.chat_id == chat for item in kernel.list_chats()), "the chat to reach the node")
        contact = b.owner.mesh_id
        for code, call in (
            ("contact.device_kind", lambda: kernel.create_contact_invitation("node", 60)),
            ("contact.device_kind", lambda: kernel.accept_contact_invitation("not-an-invitation", "node")),
            ("contact.device_kind", lambda: kernel.verify_contact("exchange")),
            ("contact.device_kind", lambda: kernel.start_contact_verification(contact)),
            ("share.device_kind", lambda: kernel.approve_share(kernel.propose_share(chat, contact))),
            ("share.device_kind", lambda: kernel.revoke_share(grant.id)),
        ):
            with pytest.raises(ak.KernelError) as refused:
                outcome = call()
                if hasattr(outcome, "__await__"):
                    await outcome
            assert refused.value.code == code
        assert [item.id for item in a.owner.kernel.shares()] == [grant.id]
        assert [item.state for item in a.owner.kernel.shares()] == ["in_effect"]
        assert _no_shared_folder(tmp_path / "node")


@pytest.mark.parametrize("value", [None, "production", "Development"])
async def test_the_development_meshes_are_refused_outside_development(tmp_path, monkeypatch, value):
    async with dev_node(tmp_path, monkeypatch) as a, contact_mesh(tmp_path, monkeypatch) as b:
        if value is None:
            monkeypatch.delenv(dev_mesh.ENV)
        else:
            monkeypatch.setenv(dev_mesh.ENV, value)
        refusals = (
            a.owner.invite_contact("Mesh A"),
            b.owner.accept_contact("text", "Mesh B"),
            b.phone.read_shared(a.owner.mesh_id, "grant"),
        )
        for pending in refusals:
            with pytest.raises(dev_mesh.DevMeshRefused):
                await pending
        for call in (
            lambda: a.owner.verify_contact("exchange"),
            lambda: a.owner.grant_view("chat", b.owner.mesh_id),
            lambda: a.owner.revoke("grant"),
            lambda: dev_mesh.DevOwner("owner", tmp_path / "again"),
        ):
            with pytest.raises(dev_mesh.DevMeshRefused):
                call()


async def test_a_node_data_dir_reopens_with_the_same_mesh_and_gains_no_shared_folder(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as a:
        before = await a.node.status()
        root = tmp_path / "node"
        stored = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    node = node_mesh.NodeMesh(data_dir=root, settings=node_settings())
    await node.start()
    try:
        after = await node.status()
        assert (after["mesh_id"], after["member_id"], after["enrolled"]) == (
            before["mesh_id"], before["member_id"], True)
        assert stored <= {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
        assert _no_shared_folder(root)
    finally:
        await node.stop()
        node_identity.reset_cache()
