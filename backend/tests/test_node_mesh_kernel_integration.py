"""Pairs the node into a mesh created by a second runtime through the real kernel binding, syncs a chat both ways, delivers events between devices that are never online together through the node's own supervised mailbox, and answers or refuses requests the second runtime sends to the node.
It runs only where the pinned kernel wheel and the mailbox binary are installed, which is the node image, using software keys, a test token issuer and loopback networking."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import socket
import time

import pytest

ak = pytest.importorskip("astral_kernel")
pytestmark = pytest.mark.skipif(shutil.which("astral-mailbox") is None, reason="mailbox binary not installed")

from contextlib import asynccontextmanager  # noqa: E402

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from orchestrator import node_api, node_mesh, node_mesh_chat, node_mesh_keys  # noqa: E402
from orchestrator.node_mailbox import MailboxSettings  # noqa: E402

ISS = "https://auth.example.test/realms/astral"
CLIENT = "astral-desktop"
ISSUER_KEY = ec.derive_private_key(7654321, ec.SECP256R1())
ORDER = node_mesh_keys.P256_ORDER


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(key, message: bytes) -> bytes:
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    if s > ORDER // 2:
        s = ORDER - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def point(key) -> bytes:
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


class Signer(ak.HostSigner):
    def __init__(self):
        self.identity = ec.generate_private_key(ec.SECP256R1())
        self.presence = ec.generate_private_key(ec.SECP256R1())

    def identity_public_key(self):
        return point(self.identity)

    def identity_sign(self, message):
        return sign(self.identity, message)

    def presence_public_key(self):
        return point(self.presence)

    def presence_sign(self, prompt, message):
        return sign(self.presence, message)

    def hardware_backed(self):
        return False


def token(nonce: str) -> str:
    now = int(time.time())
    header = b64(json.dumps({"alg": "ES256", "kid": "k1", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    claims = {"iss": ISS, "aud": CLIENT, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": nonce}
    payload = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{payload}"
    return f"{signing_input}.{b64(sign(ISSUER_KEY, signing_input.encode()))}"


def issuer_json() -> dict:
    raw = point(ISSUER_KEY)
    jwks = {"keys": [{"kid": "k1", "kty": "EC", "crv": "P-256", "x": b64(raw[1:33]), "y": b64(raw[33:])}]}
    return {"issuer": ISS, "client_id": CLIENT, "jwks_json": json.dumps(jwks)}


async def test_issuers():
    return [issuer_json()]


class Listener(ak.KernelListener):
    def on_change(self, change):
        pass


def open_device(directory, label: str, marker: int, mailbox: str):
    config = ak.KernelConfig(
        data_dir=str(directory),
        operations_secret=ak.new_operations_secret(),
        wrap_secret=ak.new_operations_secret(),
        restore_marker=bytes([marker]) * 32,
        device_label=label,
        device_kind=ak.DeviceKind.DESKTOP,
        issuers=[ak.IssuerConfig(**issuer_json())],
        network=ak.NetworkConfig(
            relay=ak.RelayChoice.DISABLED(),
            local_discovery=False,
            internet_lookup=False,
            loopback_only=True,
            sync_interval_ms=500,
            mailbox=mailbox,
        ),
    )
    return ak.Kernel.open(config, Signer(), None, None, Listener())


async def until(condition, what, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = condition()
            if asyncio.iscoroutine(result):
                result = await result
        except ak.KernelError:
            result = False
        if result:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def enrolled(node):
    return (await node.status())["enrolled"]


async def node_has(node, chat_id, count):
    return len((await node.chat(chat_id)).view.messages) == count


async def test_the_node_joins_a_mesh_syncs_chats_and_relays_through_its_mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "personal")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path / "node"))
    settings = node_mesh.MeshSettings(
        label="node",
        relay=("none",),
        loopback_only=True,
        local_discovery=False,
        internet_lookup=False,
        sync_interval_ms=500,
        mailbox=MailboxSettings(loopback=True, relay=("none",), bind=(f"127.0.0.1:{free_port()}",)),
    )
    node = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=settings, issuers=test_issuers)
    await node.start()
    node_mesh.install(node)
    running = []
    try:
        status = await node.status()
        assert status["state"] == "not_enrolled" and status["hardware_backed"] is False
        mailbox_address = status["mailbox"]["address"]
        assert mailbox_address and status["endpoint_address"]
        before = await node_api.mesh_descriptor_fields({"capabilities": [], "transport_addresses": []})
        assert before["mesh"]["member_id"] is None and "mailbox" not in before

        laptop = open_device(tmp_path / "laptop", "laptop", 1, mailbox_address)
        request = laptop.begin_mesh_creation()
        created = laptop.complete_mesh_creation(token(request.nonce))
        await laptop.start_network()
        running.append(laptop)

        offer = await node.pairing_offer()
        assert offer["mailbox"]["address"] == mailbox_address
        preview = laptop.preview_pairing(offer["payload"])
        assert (preview.label, preview.kind) == ("node", "node")
        approved = await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        assert approved.kind == "node"
        await until(lambda: enrolled(node), "the node to join")
        joined = await node.status()
        assert joined["mesh_id"] == created.mesh_id and joined["members"] == 2
        assert joined["member_id"] == approved.device_id
        descriptor = await node_api.mesh_descriptor_fields({"capabilities": [], "transport_addresses": []})
        assert descriptor["mesh"]["mesh_id"] == created.mesh_id and descriptor["mesh"]["member_id"] == approved.device_id
        assert descriptor["mailbox"]["address"] == mailbox_address
        with pytest.raises(node_mesh.NodeMeshError):
            await node.pairing_offer()
        assert joined["requests"] is True and node.requests_supported

        chat = laptop.create_chat("from the laptop")
        laptop.send_message(chat, "hello node", None)
        await until(lambda: node_has(node, chat, 1), "the node to read the laptop's message")
        node_message = await node.send_message(chat, "hello laptop")
        await until(lambda: len(laptop.chat(chat).view.messages) == 2, "the laptop to read the node's message")
        assert node_message in [m.id for m in laptop.chat(chat).view.messages]

        tablet = open_device(tmp_path / "tablet", "tablet", 2, mailbox_address)
        await tablet.start_network()
        running.append(tablet)
        tablet_offer = await tablet.create_pairing_offer()
        await laptop.approve_pairing(tablet_offer.payload, ak.HistoryGrant.ALL)
        await until(lambda: len(tablet.roster().members) == 3, "the tablet to join")
        await until(lambda: len(tablet.chat(chat).view.messages) == 2, "the tablet to read the chat")

        await node.kernel.stop_network()
        await tablet.stop_network()
        deposited = laptop.mailbox_state().deposited_through
        later = laptop.create_chat("written while the tablet is away")
        laptop.send_message(later, "left in the mailbox", None)
        await laptop.sync_now()
        await until(lambda: laptop.mailbox_state().deposited_through > deposited, "the laptop to deposit")
        await laptop.stop_network()
        running.remove(laptop)

        collected = tablet.mailbox_state().collected_through
        await tablet.start_network()
        await until(lambda: any(c.chat_id == later for c in tablet.list_chats()), "the tablet to collect from the mailbox")
        await until(lambda: len(tablet.chat(later).view.messages) == 1, "the tablet to read the deposited message")
        assert tablet.mailbox_state().collected_through > collected
        assert tablet.chat(later).view.messages[0].text == "left in the mailbox"
    finally:
        for kernel in running:
            await kernel.stop_network()
        node_mesh.install(None)
        await node.stop()


OWNER = "oidc.abc.owner"
NODE_TEXT = "node says: "


class History:
    def __init__(self):
        self.messages = []

    def create_chat(self, user_id):
        return f"local-{len(self.messages)}-{user_id[-5:]}"

    def add_message(self, chat_id, role, content, user_id):
        self.messages.append((chat_id, role, content))


async def model_turn(owner_id, local_chat, prompt):
    node_mesh_chat.observe_message(owner_id, local_chat, "user", prompt)
    node_mesh_chat.observe_step(
        owner_id, local_chat, {"name": "search", "args": '{"q":"x"}', "status": "completed", "result": "three hits", "error": None}
    )
    node_mesh_chat.observe_message(owner_id, local_chat, "assistant", NODE_TEXT + prompt)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@asynccontextmanager
async def paired(tmp_path, monkeypatch, *, phi_protected=False, calls=None):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", "personal")
    monkeypatch.setenv("ASTRAL_NODE_DATA_DIR", str(tmp_path / "node"))
    settings = node_mesh.MeshSettings(
        label="node", relay=("none",), loopback_only=True, local_discovery=False, internet_lookup=False, sync_interval_ms=500,
        mailbox=MailboxSettings(loopback=True, relay=("none",), bind=(f"127.0.0.1:{free_port()}",)),
    )
    node = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=settings, issuers=test_issuers)
    await node.start()
    bridge = None
    laptop = None
    try:
        status = await node.status()
        laptop = open_device(tmp_path / "laptop", "laptop", 1, status["mailbox"]["address"])
        request = laptop.begin_mesh_creation()
        laptop.complete_mesh_creation(token(request.nonce))
        await laptop.start_network()
        offer = await node.pairing_offer()
        await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        await until(lambda: enrolled(node), "the node to join")
        bridge = node_mesh_chat.MeshChatBridge(
            node, owner_id=OWNER, history=History(), runner=model_turn, calls=calls, state_path=tmp_path / "chats.json",
            phi_active=lambda owner: phi_protected, poll_seconds=0.2,
        )
        node_mesh_chat.install(bridge)
        await bridge.start()
        yield node, laptop, (await node.status())["member_id"], bridge
    finally:
        node_mesh_chat.install(None)
        if bridge is not None:
            await bridge.stop()
        if laptop is not None:
            await laptop.stop_network()
        await node.stop()


def request_state(laptop, request_id, state):
    return lambda: laptop.request(request_id).state == getattr(ak.RequestState, state)


async def test_the_node_claims_runs_and_answers_a_request_from_another_member(tmp_path, monkeypatch):
    async with paired(tmp_path, monkeypatch) as (node, laptop, node_device, bridge):
        chat = laptop.create_chat("ask the node")
        laptop.send_message(chat, "what is on my calendar?", None)
        request = laptop.send_request(chat, "summarize my calendar", node_device, None, None)
        await until(request_state(laptop, request, "ANSWERED"), "the node to answer")
        answered = laptop.request(request)
        assert answered.turn and answered.claim and answered.to_device == node_device
        await until(
            lambda: any((m.text or "") == NODE_TEXT + "summarize my calendar" for m in laptop.chat(chat).view.messages),
            "the node's answer to reach the laptop",
        )
        assert laptop.pending_requests_for_me() == []
        assert [r.from_device for r in laptop.chat_requests(chat)] == [(await laptop.mesh_status()).device_id]
        labels = [m.labels for m in laptop.chat(chat).view.messages if (m.text or "").startswith(NODE_TEXT)]
        assert labels and all(item.confidentiality.lower() == "private" for item in labels)


async def test_a_request_with_a_forged_ticket_is_refused_and_never_run(tmp_path, monkeypatch):
    ran = []

    class Calls:
        async def resolve(self, owner_id, tool):
            from orchestrator.node_mesh_calls import ResolvedTool

            return ResolvedTool("agent-1", tool, "tools:read")

        async def authorize(self, owner_id, resolved):
            return "authority"

        async def run(self, *args):
            ran.append(args)

    async with paired(tmp_path, monkeypatch, calls=Calls()) as (node, laptop, node_device, bridge):
        chat = laptop.create_chat("forged")
        laptop.send_message(chat, "hello", None)
        arguments = canonical({"q": "weather"})
        digest = "sha256:" + hashlib.sha256(arguments.encode()).hexdigest()
        action = {
            "v": 1, "mesh": (await laptop.mesh_status()).mesh_id, "device": (await laptop.mesh_status()).device_id,
            "turn": "sha256:" + "1" * 64, "agent": "kernel", "tool": "lookup", "tool_def": "sha256:" + "2" * 64,
            "args_digest": digest, "influence": [],
        }
        forged = ak.PeerCall(
            call_id="call-1", tool="lookup", arguments_json=arguments, action_json=canonical(action),
            ticket_json=canonical({"v": 1, "capability": "AAAA", "receipt": None, "consent": None}),
        )
        request = laptop.send_request(chat, "run it", node_device, None, forged)
        await until(request_state(laptop, request, "REFUSED"), "the node to refuse the forged ticket")
        code = laptop.request(request).code
        assert code.startswith("ticket.") and code != "ticket.consent_required"
        assert not ran

        mismatched = ak.PeerCall(
            call_id="call-2", tool="lookup", arguments_json=arguments,
            action_json=canonical({**action, "device": "sha256:" + "3" * 64}), ticket_json=forged.ticket_json,
        )
        second = laptop.send_request(chat, "run it as someone else", node_device, None, mismatched)
        await until(request_state(laptop, second, "REFUSED"), "the node to refuse the mismatched device")
        assert laptop.request(second).code == "ticket.device_mismatch" and not ran


async def test_phi_work_is_refused_by_a_node_the_roster_does_not_list_as_a_phi_member(tmp_path, monkeypatch):
    async with paired(tmp_path, monkeypatch, phi_protected=True) as (node, laptop, node_device, bridge):
        assert await node.phi_member() is False
        chat = laptop.create_chat("chart")
        laptop.send_message(chat, "patient note", None)
        request = laptop.send_request(chat, "summarize the chart", node_device, None, None)
        await until(request_state(laptop, request, "REFUSED"), "the node to refuse PHI work")
        assert laptop.request(request).code == "node.phi_not_approved"
        assert not any((m.text or "").startswith(NODE_TEXT) for m in laptop.chat(chat).view.messages)
