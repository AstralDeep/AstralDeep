"""Moves a mesh from one test token issuer to another through the real kernel binding: devices attested under the old issuer and under the new one are served side by side by an institution node, a device that attests under an issuer the roster does not accept is refused by the kernel, and nothing in the mesh is orphaned.
It runs only where the pinned kernel wheel and the mailbox binary are installed, using two test issuers with their own keys, software keys and loopback networking."""

from __future__ import annotations

import json
import shutil
import time

import pytest

pytest.importorskip("astral_kernel")
pytestmark = pytest.mark.skipif(shutil.which("astral-mailbox") is None, reason="mailbox binary not installed")

from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from orchestrator import node_mesh_chat  # noqa: E402
from orchestrator.institution_meshes import Owner  # noqa: E402
from tests.test_institution_kernel_integration import ISS, make_runtime  # noqa: E402
from tests.test_node_mesh_kernel_integration import (  # noqa: E402
    CLIENT,
    NODE_TEXT,
    Listener,
    Signer,
    ak,
    b64,
    enrolled,
    point,
    request_state,
    sign,
    until,
)

NEW_ISS = "https://auth.new.example.test/realms/astral"
OTHER_ISS = "https://auth.other.example.test/realms/astral"
OLD_KEY = ec.derive_private_key(7654321, ec.SECP256R1())
NEW_KEY = ec.derive_private_key(1234567, ec.SECP256R1())
OTHER_KEY = ec.derive_private_key(2345678, ec.SECP256R1())


def issuer(url: str, key) -> dict:
    raw = point(key)
    jwks = {"keys": [{"kid": "k1", "kty": "EC", "crv": "P-256", "x": b64(raw[1:33]), "y": b64(raw[33:])}]}
    return {"issuer": url, "client_id": CLIENT, "jwks_json": json.dumps(jwks)}


def token(url: str, key, subject: str, nonce: str) -> str:
    now = int(time.time())
    header = b64(json.dumps({"alg": "ES256", "kid": "k1", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    claims = {"iss": url, "aud": CLIENT, "sub": subject, "iat": now, "exp": now + 300, "nonce": nonce}
    payload = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{header}.{payload}.{b64(sign(key, f'{header}.{payload}'.encode()))}"


def open_with(directory, label, marker, mailbox, issuers):
    config = ak.KernelConfig(
        data_dir=str(directory),
        operations_secret=ak.new_operations_secret(),
        wrap_secret=ak.new_operations_secret(),
        restore_marker=bytes([marker]) * 32,
        device_label=label,
        device_kind=ak.DeviceKind.DESKTOP,
        issuers=[ak.IssuerConfig(**item) for item in issuers],
        network=ak.NetworkConfig(
            relay=ak.RelayChoice.DISABLED(), local_discovery=False, internet_lookup=False, loopback_only=True,
            sync_interval_ms=500, mailbox=mailbox,
        ),
    )
    return ak.Kernel.open(config, Signer(), None, None, Listener(), peer_tools=None)


async def both_issuers():
    return [issuer(ISS, OLD_KEY), issuer(NEW_ISS, NEW_KEY)]


async def test_a_mesh_keeps_working_while_devices_attest_under_the_old_and_the_new_issuer(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.meshes._issuers = both_issuers
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    running = []
    try:
        node = await runtime.serve_owner(Owner.verified(ISS, "alice"))
        mailbox = runtime.meshes._mailbox.address
        laptop = open_with(tmp_path / "laptop", "laptop", 1, mailbox, [issuer(ISS, OLD_KEY), issuer(NEW_ISS, NEW_KEY)])
        created = laptop.begin_mesh_creation()
        assert created.issuer == ISS
        laptop.complete_mesh_creation(token(ISS, OLD_KEY, "alice-old", created.nonce))
        await laptop.start_network()
        running.append(laptop)
        assert sorted(laptop.roster().issuers) == sorted([ISS, NEW_ISS])
        offer = await node.pairing_offer()
        await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        await until(lambda: enrolled(node), "the node to join")
        node_device = (await node.status())["member_id"]

        tablet = open_with(tmp_path / "tablet", "tablet", 2, mailbox, [issuer(NEW_ISS, NEW_KEY), issuer(ISS, OLD_KEY)])
        await tablet.start_network()
        running.append(tablet)
        tablet_offer = await tablet.create_pairing_offer()
        await laptop.approve_pairing(tablet_offer.payload, ak.HistoryGrant.ALL)
        await until(lambda: len(tablet.roster().members) == 3, "the tablet to join")
        tablet_device = (await tablet.mesh_status()).device_id
        await until(lambda: any(m.device_id == tablet_device and m.unattested for m in node.kernel.roster().members), "the node to see the tablet unattested")

        moved = tablet.begin_attestation()
        assert moved.issuer == NEW_ISS
        tablet.reattest(token(NEW_ISS, NEW_KEY, "alice-new-subject", moved.nonce))
        await until(lambda: all(not m.unattested for m in node.kernel.roster().members if m.kind != "node"), "every device to be attested")

        chat = laptop.create_chat("through the move")
        laptop.send_message(chat, "hello", None)
        await until(lambda: any(c.chat_id == chat for c in tablet.list_chats()), "the tablet to receive the laptop's chat")
        for sender, text in ((laptop, "old issuer device"), (tablet, "new issuer device")):
            request = sender.send_request(chat, text, node_device, None, None)
            await until(request_state(sender, request, "ANSWERED"), f"the node to serve the {text}")
            await until(lambda: any((m.text or "") == NODE_TEXT + text for m in laptop.chat(chat).view.messages), "the answer to sync")
        for kernel in (laptop, tablet):
            assert {m.text for m in kernel.chat(chat).view.messages if m.text} >= {"hello", NODE_TEXT + "old issuer device", NODE_TEXT + "new issuer device"}
    finally:
        node_mesh_chat.install(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()


async def test_a_device_cannot_attest_under_an_issuer_the_roster_does_not_accept(tmp_path):
    runtime = make_runtime(tmp_path)
    node_mesh_chat.install(runtime.bridges)
    await runtime.start()
    running = []
    try:
        node = await runtime.serve_owner(Owner.verified(ISS, "alice"))
        mailbox = runtime.meshes._mailbox.address
        laptop = open_with(tmp_path / "laptop", "laptop", 1, mailbox, [issuer(ISS, OLD_KEY)])
        created = laptop.begin_mesh_creation()
        laptop.complete_mesh_creation(token(ISS, OLD_KEY, "alice-old", created.nonce))
        await laptop.start_network()
        running.append(laptop)
        assert laptop.roster().issuers == [ISS]
        offer = await node.pairing_offer()
        await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        await until(lambda: enrolled(node), "the node to join")
        node_device = (await node.status())["member_id"]

        tablet = open_with(tmp_path / "tablet", "tablet", 2, mailbox, [issuer(OTHER_ISS, OTHER_KEY)])
        await tablet.start_network()
        running.append(tablet)
        tablet_offer = await tablet.create_pairing_offer()
        await laptop.approve_pairing(tablet_offer.payload, ak.HistoryGrant.ALL)
        await until(lambda: len(tablet.roster().members) == 3, "the tablet to join")
        request = tablet.begin_attestation()
        with pytest.raises(ak.KernelError) as refused:
            tablet.reattest(token(OTHER_ISS, OTHER_KEY, "alice-other", request.nonce))
        assert refused.value.code == "attestation.unknown_issuer"

        chat = tablet.create_chat("still unattested")
        tablet.send_message(chat, "hello", None)
        blocked = tablet.send_request(chat, "run it", node_device, None, None)
        await until(request_state(tablet, blocked, "REFUSED"), "the node to refuse the unattested tablet")
        assert tablet.request(blocked).code == "institution.attestation_stale"

        own = laptop.send_request(laptop.create_chat("the laptop is fine"), "run it", node_device, None, None)
        await until(request_state(laptop, own, "ANSWERED"), "the node to keep serving the laptop")
    finally:
        node_mesh_chat.install(None)
        for kernel in running:
            await kernel.stop_network()
        await runtime.stop()
