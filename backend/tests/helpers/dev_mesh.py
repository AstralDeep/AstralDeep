"""Pairs a personal node with a seeded development owner device on the real kernel binding over loopback with no mailbox, so personal-profile tests authenticate with real signatures, and the node is a PHI member unless a test opts out.
It builds on devtools/dev_mesh.py and orchestrator/node_mesh.py, and test_dev_mesh.py proves it before node tests use it for mesh requests, caller tickets and holder proofs."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Sequence

import astral_kernel as ak
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from devtools import dev_mesh
from orchestrator import node_mesh
from orchestrator.node_mailbox import MailboxSettings
from shared import node_identity

CALLER_PAUSE_MS = 5000
WAIT_SECONDS = 40.0


@dataclass
class DevMesh:
    node: node_mesh.NodeMesh
    owner: dev_mesh.DevOwner
    node_device: str
    opened_ms: int


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def node_settings() -> node_mesh.MeshSettings:
    return node_mesh.MeshSettings(
        label="node", relay=("none",), loopback_only=True, local_discovery=False, internet_lookup=False,
        sync_interval_ms=500, mailbox=MailboxSettings(enabled=False),
    )


async def until(condition: Callable[[], Any], what: str, seconds: float = WAIT_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def _enrolled(node: node_mesh.NodeMesh) -> bool:
    return bool((await node.status())["enrolled"])


@asynccontextmanager
async def dev_node(tmp_path: Path, monkeypatch: Any, *, name: str = "owner", bind: bool = True, adopt: bool = True) -> AsyncIterator[DevMesh]:
    monkeypatch.setenv(dev_mesh.ENV, dev_mesh.DEVELOPMENT)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    node_identity.reset_cache()
    node = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=node_settings())
    await node.start()
    opened = now_ms()
    owner = None
    try:
        owner = dev_mesh.DevOwner(name, tmp_path / name)
        owner.create_mesh()
        owner.confirm_recovery()
        await owner.start()
        offer = await node.pairing_offer()
        await owner.approve_offer(offer["payload"])
        await until(lambda: _enrolled(node), "the node to join the development mesh")
        node_device = (await node.status())["member_id"]
        if adopt:
            await until(owner.phi_markers_synced, "the PHI markers to sync")
            owner.adopt_phi_group()
            await until(node.phi_member, "the node to join the PHI group")
        if bind and await node.refresh_personal_owner() is None:
            raise AssertionError(f"the node refused the development mesh: {(await node.status())['owner_binding']}")
        yield DevMesh(node, owner, node_device, opened)
    finally:
        try:
            if owner is not None:
                await owner.stop()
        finally:
            await node.stop()
            node_identity.reset_cache()


async def publish_catalog(mesh: DevMesh, tools: Sequence[Dict[str, Any]]) -> None:
    async def published() -> bool:
        try:
            await mesh.node.publish_tool_catalog(tools)
        except node_mesh.NodeMeshError as exc:
            if exc.code != "log.key_missing":
                raise
            return False
        return True

    def reached() -> bool:
        listed: List[str] = [entry.tool.name for entry in mesh.owner.kernel.peer_tool_catalog() if entry.device == mesh.node_device]
        return {tool["name"] for tool in tools} <= set(listed)

    await until(published, "the node to publish its catalog")
    await until(reached, "the node catalog to reach the owner")


async def wait_for_caller_pause(mesh: DevMesh) -> None:
    await until(lambda: now_ms() > mesh.opened_ms + CALLER_PAUSE_MS, "the node's caller replay pause to pass")


async def connection_reached(mesh: DevMesh, connection_id: str) -> None:
    await until(
        lambda: connection_id in [info.id for info in mesh.node.kernel.connections()],
        "the connection to reach the node",
    )


@dataclass
class ContactMesh:
    owner: dev_mesh.DevOwner
    phone: dev_mesh.DevDevice

    async def pair_phone(self) -> None:
        await self.phone.start()
        await self.owner.approve_offer(await self.phone.pairing_offer())
        await until(self.phone.enrolled, "the second phone to join the second mesh")


@asynccontextmanager
async def contact_mesh(
    tmp_path: Path, monkeypatch: Any, *, pair_phone: bool = True, tools: Any = None,
) -> AsyncIterator[ContactMesh]:
    monkeypatch.setenv(dev_mesh.ENV, dev_mesh.DEVELOPMENT)
    owner = dev_mesh.DevOwner("second-owner", tmp_path / "second-owner", tools)
    phone = dev_mesh.DevDevice("second-phone", tmp_path / "second-phone")
    mesh = ContactMesh(owner, phone)
    try:
        owner.create_mesh()
        owner.confirm_recovery()
        await owner.start()
        if pair_phone:
            await mesh.pair_phone()
        yield mesh
    finally:
        await phone.stop()
        await owner.stop()


async def verified_contacts(mine: dev_mesh.DevOwner, theirs: dev_mesh.DevOwner) -> None:
    invitation = await mine.invite_contact("Mesh A")
    at_theirs = await theirs.accept_contact(invitation.text, "Mesh B")
    await until(lambda: mine.kernel.pending_contact_exchanges(), "the invitation to reach its owner")
    at_mine = mine.kernel.pending_contact_exchanges()[0]
    if at_mine.code != at_theirs.code:
        raise AssertionError("the two meshes disagree on the contact code")
    mine.verify_contact(at_mine.id)
    theirs.verify_contact(at_theirs.id)


class _StrangerSigner(ak.HostSigner):
    def __init__(self) -> None:
        self._identity = ec.generate_private_key(ec.SECP256R1())
        self._presence = ec.generate_private_key(ec.SECP256R1())

    @staticmethod
    def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
        return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def identity_public_key(self):
        return self._point(self._identity)

    def identity_sign(self, message):
        return dev_mesh._sign(self._identity, bytes(message))

    def presence_public_key(self):
        return self._point(self._presence)

    def presence_sign(self, prompt, message):
        return dev_mesh._sign(self._presence, bytes(message))

    def hardware_backed(self):
        return False


@asynccontextmanager
async def stranger_mesh(tmp_path: Path, monkeypatch: Any) -> AsyncIterator[ak.Kernel]:
    monkeypatch.setenv(dev_mesh.ENV, dev_mesh.DEVELOPMENT)
    config = ak.KernelConfig(
        data_dir=str(tmp_path / "stranger"), operations_secret=ak.new_operations_secret(),
        wrap_secret=ak.new_operations_secret(), restore_marker=None, device_label="stranger",
        device_kind=ak.DeviceKind.DESKTOP,
        network=ak.NetworkConfig(
            relay=ak.RelayChoice.DISABLED(), local_discovery=False, internet_lookup=False, loopback_only=True,
            sync_interval_ms=dev_mesh.SYNC_INTERVAL_MS, mailbox=None,
        ),
    )
    kernel = ak.Kernel.open(config, _StrangerSigner())
    try:
        kernel.create_mesh()
        kernel.confirm_mesh_recovery()
        await kernel.start_network()
        yield kernel
    finally:
        await kernel.stop_network()
