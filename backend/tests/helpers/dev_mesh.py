"""Pairs a personal node with a seeded development owner device on the real kernel binding over loopback with no mailbox, so personal-profile tests authenticate with real signatures, and the node is a PHI member unless a test opts out.
It builds on devtools/dev_mesh.py and orchestrator/node_mesh.py, and test_dev_mesh.py proves it before node tests use it for mesh requests, caller tickets and holder proofs."""

from __future__ import annotations

import asyncio
import json as jsonlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence

import astral_kernel as ak
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from devtools import dev_mesh
from orchestrator import node_mesh
from orchestrator.node_mailbox import MailboxSettings
from shared import node_identity

CALLER_PAUSE_MS = 5000
WAIT_SECONDS = 40.0
MCP_BASE = "http://mcp.test"
MCP_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test-host", "version": "1"},
}


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


class Door:
    def __init__(self, mesh: DevMesh, client: httpx.AsyncClient, host: Any):
        self.mesh, self.client, self.host = mesh, client, host
        self.holder = dev_mesh.CallerHolder.generate()

    def mint(self, tools, *, label="Dev script", a2a=False, lifetime_s=dev_mesh.DEFAULT_LIFETIME_S, node=None):
        return self.mesh.owner.mint_caller_ticket(
            node or self.mesh.node_device, self.holder, tools, label=label, a2a=a2a, lifetime_s=lifetime_s)

    async def post(self, path: str, body: bytes, ticket, extra: Optional[Dict[str, str]] = None, *,
                   signed_body: Optional[bytes] = None, signed_target: Optional[str] = None, holder=None,
                   headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        proof = headers if headers is not None else self.mesh.owner.caller_headers(
            holder or self.holder, ticket, "POST", signed_target or path, body if signed_body is None else signed_body)
        merged = {"Content-Type": "application/json", **(extra or {}), **proof}
        return await self.client.post(path, content=body, headers=merged)

    def mcp(self, method: str, params: Optional[dict] = None):
        body = jsonlib.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": dict(MCP_META)}})
        extra = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
        if method == "tools/call":
            extra["Mcp-Name"] = (params or {})["name"]
        return body.encode(), extra

    async def call_mcp(self, ticket, method: str, params: Optional[dict] = None, **options) -> httpx.Response:
        body, extra = self.mcp(method, params)
        return await self.post("/mcp", body, ticket, extra, **options)

    async def call_tool(self, ticket, name: str, **options) -> httpx.Response:
        return await self.call_mcp(ticket, "tools/call", {"name": name, "arguments": {"value": "v"}}, **options)

    async def send_a2a(self, ticket, parts: Optional[list] = None, **options) -> httpx.Response:
        message = {"message_id": "m1", "role": "ROLE_USER", "parts": parts or [{"text": ""}]}
        body = jsonlib.dumps({"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": message}})
        return await self.post("/a2a", body.encode(), ticket, {"A2A-Version": "1.0"}, **options)


class SignedClient:
    def __init__(self, door: Door, ticket: Any):
        self.door, self.ticket = door, ticket

    async def request(self, method: str, path: str, *, json: Any = None, content: Any = None, headers: Optional[Dict[str, str]] = None, **options) -> httpx.Response:
        raw = content if content is not None else (b"" if json is None else jsonlib.dumps(json).encode())
        raw = raw.encode() if isinstance(raw, str) else bytes(raw)
        pairs = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
        extra = [(key, value) for key, value in pairs if key.lower() != "authorization"]
        proof = self.door.mesh.owner.caller_headers(self.door.holder, self.ticket, method, path, raw)
        default = [] if any(key.lower() == "content-type" for key, _ in extra) else [("Content-Type", "application/json")]
        merged = [*default, *extra, *proof.items()]
        return await self.door.client.request(method, path, content=raw, headers=merged, **options)

    async def post(self, path: str, **options) -> httpx.Response:
        return await self.request("POST", path, **options)

    async def get(self, path: str, **options) -> httpx.Response:
        return await self.door.client.get(path, **options)

    async def delete(self, path: str, **options) -> httpx.Response:
        return await self.door.client.delete(path, **options)

    async def options(self, path: str, **options) -> httpx.Response:
        return await self.door.client.options(path, **options)


@asynccontextmanager
async def shared_node(tmp_path_factory: Any, publish_host: Any) -> AsyncIterator[DevMesh]:
    from orchestrator import node_mesh
    from orchestrator.node_mesh_calls import OrchestratorCalls

    patch = pytest.MonkeyPatch()
    previous = node_mesh.current()
    try:
        async with dev_node(tmp_path_factory.mktemp("dev-mesh"), patch) as mesh:
            try:
                node_mesh.install(mesh.node)
                node_identity.load_node_identity()
                owner = node_identity.owner_id_for_mesh((await mesh.node.status())["mesh_id"])
                await publish_catalog(mesh, OrchestratorCalls(publish_host).published_tools(owner))
                await wait_for_caller_pause(mesh)
                yield mesh
            finally:
                node_mesh.install(previous)
    finally:
        patch.undo()


@asynccontextmanager
async def door_for(mesh: DevMesh, host: Any, install: Callable[[Any, Any], None]) -> AsyncIterator[Door]:
    from fastapi import FastAPI

    app = FastAPI()
    install(app, host)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=MCP_BASE) as client:
        yield Door(mesh, client, host)


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
