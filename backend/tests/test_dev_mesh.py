"""Proves the seeded development mesh on the real kernel binding: its derived device ids are the roster ids the kernel produces, and its caller tickets and holder proofs verify on the node.
The node binds, serves, syncs and wakes for it only with ASTRAL_ENV exactly development, through devtools/dev_mesh.py, tests/helpers/dev_mesh.py and orchestrator/node_mesh.py, and no orchestrator or shared module imports devtools."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import socket
import weakref
from pathlib import Path
from types import SimpleNamespace as NS

import astral_kernel as ak
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from devtools import dev_mesh
from orchestrator import node_mesh, node_mesh_chat, push_wake
from orchestrator.node_mailbox import MailboxSettings
from shared import dev_mesh_keys, node_identity
from tests import fake_astral_kernel as fake
from tests.helpers.dev_mesh import dev_node, node_settings, publish_catalog, until, wait_for_caller_pause
from tests.test_push_wake import APNS, FakeTransport, Targets, adapter, peer

BACKEND = Path(__file__).resolve().parents[1]
REFERENCE = BACKEND.parent / "specs/105-decentralized-personal-mesh/contracts/reference"
OUTSIDE_DEVELOPMENT = [None, "production", "Development", "DEVELOPMENT", "dev", " development", "development "]
MESH = "sha256:" + "c" * 64
STORED = node_identity.owner_id_for_mesh(MESH)
ECHO = {
    "name": "echo", "description": "echoes text on the node", "effect": "mesh_read", "annotations": {},
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


class SoftwareSigner(ak.HostSigner):
    def __init__(self):
        self._identity = ec.generate_private_key(ec.SECP256R1())
        self._presence = ec.generate_private_key(ec.SECP256R1())

    @staticmethod
    def _point(key):
        return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    @staticmethod
    def _sign(key, message):
        r, s = decode_dss_signature(key.sign(bytes(message), ec.ECDSA(hashes.SHA256())))
        if s > dev_mesh_keys.P256_ORDER // 2:
            s = dev_mesh_keys.P256_ORDER - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def identity_public_key(self):
        return self._point(self._identity)

    def identity_sign(self, message):
        return self._sign(self._identity, message)

    def presence_public_key(self):
        return self._point(self._presence)

    def presence_sign(self, prompt, message):
        return self._sign(self._presence, message)

    def hardware_backed(self):
        return False


def open_laptop(directory):
    return ak.Kernel.open(ak.KernelConfig(
        data_dir=str(directory), operations_secret=ak.new_operations_secret(), wrap_secret=ak.new_operations_secret(),
        restore_marker=None, device_label="laptop", device_kind=ak.DeviceKind.DESKTOP,
        network=ak.NetworkConfig(relay=ak.RelayChoice.DISABLED(), local_discovery=False, internet_lookup=False,
                                 loopback_only=True, sync_interval_ms=500, mailbox=None),
    ), SoftwareSigner())


async def kernel_enrolled(kernel) -> bool:
    return (await kernel.mesh_status()).state == ak.EnrollmentState.ENROLLED


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def mailbox_settings() -> node_mesh.MeshSettings:
    return node_mesh.MeshSettings(
        label="node", relay=("none",), loopback_only=True, local_discovery=False, internet_lookup=False, sync_interval_ms=500,
        mailbox=MailboxSettings(loopback=True, relay=("none",), bind=(f"127.0.0.1:{free_port()}",)),
    )


def production_node(tmp_path, monkeypatch, value):
    set_env(monkeypatch, value)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    node_identity.reset_cache()
    return node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=node_settings())


def set_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", value)


def owner_file() -> Path:
    return node_identity.data_dir() / node_identity.OWNER_FILE


async def pending_on_node(node, request_id) -> bool:
    return any(request.id == request_id for request in await node.pending_requests())


async def assert_no_sync(node) -> None:
    assert node.kernel.endpoint_state().is_stopped()
    with pytest.raises(ak.KernelError) as stopped:
        await node.kernel.sync_now()
    assert stopped.value.code == "net.not_running"
    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await node.sync_now()
    assert refused.value.code == "node.development_mesh"


def test_dev_keys_follow_the_mesh_reference_test_kit_from_public_labels(monkeypatch):
    monkeypatch.syspath_prepend(str(REFERENCE))
    from astral_mesh_ref import crypto, testkit

    for name in ("owner", "phone", "second-owner", "second-phone"):
        keys = dev_mesh_keys.device_keys(name)
        label = f"astral-dev-mesh/v1/{name}/"
        assert dev_mesh_keys.seed(label + "ops") == testkit.seed(label + "ops")
        assert dev_mesh_keys.scalar(label + "identity") == testkit.scalar(label + "identity")
        assert keys.identity_scalar == testkit.scalar(label + "identity")
        assert keys.presence_scalar == testkit.scalar(label + "presence")
        assert keys.identity_public_key == crypto.p256_public(testkit.scalar(label + "identity"))
        assert keys.presence_public_key == crypto.p256_public(testkit.scalar(label + "presence"))
        assert keys.operations_secret == testkit.seed(label + "ops")
        assert keys.wrap_secret == testkit.seed(label + "wrap")
        assert keys.restore_marker == testkit.seed(label + "restore")
        assert keys.device_id == crypto.device_id(keys.identity_public_key)
    ids = dev_mesh_keys.device_ids()
    assert tuple(ids) == dev_mesh_keys.NAMES and len(set(ids.values())) == 4
    assert ids["owner"] == dev_mesh_keys.device_keys("owner").device_id
    with pytest.raises(TypeError):
        ids["owner"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError):
        dev_mesh_keys.device_keys("node")


async def test_dev_device_ids_are_the_roster_ids_the_real_kernel_produces(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    produced, meshes = {}, set()
    for owner_name, phone_name in (("owner", "phone"), ("second-owner", "second-phone")):
        owner = dev_mesh.DevOwner(owner_name, tmp_path / owner_name)
        phone = dev_mesh.DevDevice(phone_name, tmp_path / phone_name)
        try:
            kit = owner.create_mesh()
            owner.confirm_recovery()
            assert owner.mesh_id == kit.mesh_id == owner.kernel.mesh_principal().mesh_id
            assert kit.device_id == owner.device_id and kit.restore_marker == owner.keys.restore_marker
            await owner.start()
            await phone.start()
            approved = await owner.approve_offer(await phone.pairing_offer())
            await until(phone.enrolled, f"{phone_name} to join")
            assert approved.device_id == phone.device_id and approved.kind == "pocket"
            roster = owner.kernel.roster()
            assert roster.mesh_id == kit.mesh_id and len(roster.members) == 2
            for member in roster.members:
                keys = dev_mesh_keys.device_keys(member.label)
                produced[member.label] = member.device_id
                assert member.feeds == [ak.operations_public_key(keys.operations_secret)]
                assert member.kind == ("pocket" if member.label.endswith("phone") else "desktop")
            meshes.add(kit.mesh_id)
        finally:
            await phone.stop()
            await owner.stop()
    assert produced == dict(dev_mesh_keys.device_ids()) and len(meshes) == 2


async def test_a_development_node_binds_and_serves_the_dev_mesh_until_it_leaves_development(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        node, owner = mesh.node, mesh.owner
        assert (await node.status())["owner_binding"] == {"state": "ready", "code": None}
        bound = node_identity.bound_owner()
        assert bound == node.personal_owner_id() == node_identity.owner_id_for_mesh(owner.mesh_id)
        stored = owner_file().read_bytes()
        _, request_id = owner.send_request(mesh.node_device, "what is on my calendar?")
        await until(lambda: pending_on_node(node, request_id), "the request to reach the node")
        request = node_mesh.RequestReference.capture(await node.request(request_id))
        context = await node.personal_request_context(request, bound, node.kernel)
        assert (context.mesh_id, context.member_id) == (owner.mesh_id, mesh.node_device)
        assert request.from_device == owner.device_id == dev_mesh_keys.device_ids()["owner"]
        assert node.kernel.endpoint_state().is_online()

        set_env(monkeypatch, "production")
        assert await node.refresh_personal_owner() is None
        await assert_no_sync(node)
        assert node.personal_owner_id() is None
        assert (await node.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
        with pytest.raises(node_mesh.NodeMeshError) as refused:
            await node.personal_request_context(request, bound, node.kernel)
        assert refused.value.code == "node.development_mesh"
        assert owner_file().read_bytes() == stored
        assert owner.kernel.request(request_id).state == ak.RequestState.PENDING


@pytest.mark.parametrize("value", [None, "production"])
async def test_outside_development_the_node_refuses_the_dev_mesh_and_writes_nothing(tmp_path, monkeypatch, value):
    async with dev_node(tmp_path, monkeypatch, bind=False) as mesh:
        node, owner = mesh.node, mesh.owner
        _, request_id = owner.send_request(mesh.node_device, "summarize my notes")
        await until(lambda: pending_on_node(node, request_id), "the request to reach the node")
        request = node_mesh.RequestReference.capture(await node.request(request_id))

        set_env(monkeypatch, value)
        bridge = node_mesh_chat.MeshChatBridge(
            node, owner_id=node.personal_owner_id, owner_setup=node.refresh_personal_owner,
            history=NS(), state_path=tmp_path / "chats.json",
        )
        assert node.kernel.endpoint_state().is_online()
        assert await bridge.scan() == 0
        await assert_no_sync(node)
        assert await node.refresh_personal_owner() is None
        assert (await node.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
        assert node_identity.bound_owner() is None and not owner_file().exists()
        assert not (tmp_path / "chats.json").exists()
        for serve in (node.personal_request_context, node.personal_authority_context):
            with pytest.raises(node_mesh.NodeMeshError) as refused:
                await serve(request, owner.mesh_id, node.kernel)
            assert refused.value.code == "node.development_mesh"
        assert owner.kernel.request(request_id).state == ak.RequestState.PENDING
        with pytest.raises(dev_mesh.DevMeshRefused):
            owner.send_request(mesh.node_device, "try again")


async def test_dev_owner_mints_caller_tickets_and_signs_proofs_the_node_kernel_verifies(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch) as mesh:
        owner, node_kernel = mesh.owner, mesh.node.kernel
        await publish_catalog(mesh, [ECHO])
        holder = dev_mesh.CallerHolder.generate()
        assert holder.holder_text.startswith("astral-caller-holder:v1:")
        ticket = owner.mint_caller_ticket(mesh.node_device, holder, ["echo"], label="Dev script")
        assert (ticket.node, ticket.tools, ticket.label) == (mesh.node_device, ["echo"], "Dev script")
        assert ticket.expires_ms - ticket.issued_ms == dev_mesh.DEFAULT_LIFETIME_S * 1000
        assert [listed.id for listed in owner.kernel.caller_tickets()] == [ticket.id]
        await wait_for_caller_pause(mesh)
        body = b'{"jsonrpc":"2.0","method":"tools/call"}'

        def call(signer, tool="echo"):
            headers = owner.caller_headers(signer, ticket, "POST", "/mcp", body)
            assert set(headers) == {"Authorization", "Astral-Caller-Proof"}
            return ak.CallerRequest(
                authorization=headers["Authorization"], proof=headers["Astral-Caller-Proof"], method="POST",
                target="/mcp", body_sha256="sha256:" + hashlib.sha256(body).hexdigest(), tool=tool,
            )

        verified = node_kernel.verify_caller_request(call(holder))
        assert (verified.id, verified.mesh, verified.device, verified.tool, verified.label) == (
            ticket.id, owner.mesh_id, owner.device_id, "echo", "Dev script",
        )
        for signer, tool, code in (
            (dev_mesh.CallerHolder.generate(), "echo", "caller.proof_bad_signature"),
            (holder, "other", "caller.tool_not_covered"),
        ):
            with pytest.raises(ak.KernelError) as refused:
                node_kernel.verify_caller_request(call(signer, tool))
            assert refused.value.code == code
        proof = owner.sign_holder_proof(holder, ticket.id, "GET", "/mcp", ms=1, nonce=b"\x00" * 16)
        assert proof == owner.sign_holder_proof(holder, ticket.id, "GET", "/mcp", ms=1, nonce=b"\x00" * 16)


async def test_a_production_node_reopening_a_dev_mesh_store_never_starts_sync_and_keeps_the_store(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch, bind=False) as mesh:
        mesh_id, node_device = mesh.owner.mesh_id, mesh.node_device
        await mesh.node.stop()
        for value, online in ((None, False), ("production", False), ("development", True)):
            set_env(monkeypatch, value)
            reopened = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=mailbox_settings())
            await reopened.start()
            try:
                roster = reopened.kernel.roster()
                assert roster.mesh_id == mesh_id and node_device in {member.device_id for member in roster.members}
                if online:
                    assert reopened.kernel.endpoint_state().is_online()
                    assert reopened.mailbox.snapshot()["running"]
                    assert await reopened.refresh_personal_owner() is not None
                else:
                    assert reopened.mailbox is None and not (tmp_path / "node" / "mailbox").exists()
                    await assert_no_sync(reopened)
                    assert (await reopened.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
                    assert await reopened.refresh_personal_owner() is None
                    assert not owner_file().exists()
            finally:
                await reopened.stop()


@pytest.mark.parametrize("value", [None, "production"])
async def test_a_production_node_cancels_a_pairing_a_dev_device_approves_before_it_joins(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    owner.create_mesh()
    owner.confirm_recovery()
    await owner.start()
    node = production_node(tmp_path, monkeypatch, value)
    await node.start()
    try:
        offer = await node.pairing_offer()
        with pytest.raises(ak.KernelError) as refused:
            await owner.kernel.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        assert refused.value.code == "pairing.mesh_refused"
        status = await node.status()
        assert status["state"] == "not_enrolled" and status["mesh_id"] is None and not status["pairing_open"]
        node_device = (await node.kernel.mesh_status()).device_id
        chat = owner.kernel.create_chat("after the refused pairing")
        owner.kernel.send_message(chat, "never reaches the node", None)
        await owner.kernel.sync_now()
        assert [(p.connected, p.status) for p in await owner.kernel.sync_state() if p.device_id == node_device] in ([], [(False, "never_synced")])
        assert node.kernel.list_chats() == [] and (await node.kernel.mesh_status()).mesh_id is None
        assert node.kernel.endpoint_state().is_online()
        assert await node.refresh_personal_owner() is None and not owner_file().exists()
    finally:
        await node.stop()
        await owner.stop()


@pytest.mark.parametrize("value", [None, "production"])
async def test_a_production_node_refuses_to_join_a_dev_mesh_another_member_approves(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    owner.create_mesh()
    owner.confirm_recovery()
    await owner.start()
    laptop = open_laptop(tmp_path / "laptop")
    node = None
    try:
        await laptop.start_network()
        await owner.approve_offer((await laptop.create_pairing_offer()).payload)
        await until(lambda: kernel_enrolled(laptop), "the laptop to join")
        node = production_node(tmp_path, monkeypatch, value)
        await node.start()
        offer = await node.pairing_offer()
        with pytest.raises(ak.KernelError) as refused:
            await laptop.approve_pairing(offer["payload"], ak.HistoryGrant.ALL)
        assert refused.value.code == "pairing.mesh_refused"
        status = await node.status()
        assert status["state"] == "not_enrolled" and status["mesh_id"] is None
        assert (await node.kernel.mesh_status()).mesh_id is None
        assert node.personal_owner_id() is None and not owner_file().exists()
    finally:
        if node is not None:
            await node.stop()
        await laptop.stop_network()
        await owner.stop()


async def test_a_production_node_with_a_real_store_binds_its_owner_and_holds_one_store_handle(tmp_path, monkeypatch):
    laptop = open_laptop(tmp_path / "laptop")
    laptop.create_mesh()
    laptop.confirm_mesh_recovery()
    await laptop.start_network()
    mesh_id = laptop.mesh_principal().mesh_id
    node = production_node(tmp_path, monkeypatch, "production")
    try:
        await node.start()
        try:
            await laptop.approve_pairing((await node.pairing_offer())["payload"], ak.HistoryGrant.ALL)
            await until(lambda: kernel_enrolled(node.kernel), "the node to join the laptop's mesh")
            owner = await node.refresh_personal_owner()
            assert owner == node_identity.owner_id_for_mesh(mesh_id)
        finally:
            await node.stop()
        store = str(tmp_path / "node" / node_mesh.KERNEL_DIR)
        original = ak.Kernel.open
        record = NS(opened=[], live_at_open=[])

        def counting(config, *args, **kwargs):
            if config.data_dir == store:
                record.live_at_open.append(sum(ref() is not None and not ref().closed for ref in record.opened))
            kernel = original(config, *args, **kwargs)
            if config.data_dir == store:
                record.opened.append(weakref.ref(kernel))
            return kernel

        monkeypatch.setattr(ak.Kernel, "open", staticmethod(counting))
        for settings, opens in ((node_settings(), 1), (mailbox_settings(), 2)):
            record.opened.clear()
            record.live_at_open.clear()
            reopened = node_mesh.NodeMesh(data_dir=tmp_path / "node", settings=settings)
            await reopened.start()
            try:
                assert len(record.opened) == opens and record.live_at_open == [0] * opens
                assert record.opened[-1]() is reopened.kernel
                assert reopened.kernel.endpoint_state().is_online()
                assert (reopened.mailbox is not None and reopened.mailbox.snapshot()["running"]) is (opens == 2)
                assert await reopened.refresh_personal_owner() == owner == node_identity.bound_owner()
                assert (await reopened.status())["owner_binding"] == {"state": "ready", "code": None}
            finally:
                await reopened.stop()
    finally:
        await laptop.stop_network()


@pytest.mark.parametrize("value", OUTSIDE_DEVELOPMENT)
def test_devtools_refuse_to_start_outside_development(tmp_path, monkeypatch, value):
    set_env(monkeypatch, value)
    for build in (
        lambda: dev_mesh.DevOwner("owner", tmp_path / "owner"),
        lambda: dev_mesh.DevDevice("phone", tmp_path / "phone"),
        dev_mesh.CallerHolder.generate,
        dev_mesh.require_development,
    ):
        with pytest.raises(dev_mesh.DevMeshRefused):
            build()
    assert list(tmp_path.iterdir()) == []


async def test_a_dev_device_refuses_every_operation_once_it_leaves_development(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    holder = dev_mesh.CallerHolder.generate()
    set_env(monkeypatch, "production")
    try:
        for operation in (
            owner.create_mesh, owner.confirm_recovery, owner.start, owner.pairing_offer,
            lambda: owner.approve_offer("payload"), lambda: owner.send_request(MESH, "hi"),
            lambda: owner.mint_caller_ticket(MESH, holder, ["echo"]),
            lambda: owner.sign_holder_proof(holder, "ticket", "POST", "/mcp"),
        ):
            with pytest.raises(dev_mesh.DevMeshRefused):
                result = operation()
                if hasattr(result, "__await__"):
                    await result
    finally:
        await owner.stop()


async def test_caller_headers_take_only_the_ticket_text_form_and_waits_give_up(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    holder = dev_mesh.CallerHolder.generate()
    try:
        for text in ("AstralCaller e30", "astral-caller-key:v1:AAAA", "astral-caller-ticket:v2:e30"):
            with pytest.raises(ValueError):
                owner.caller_headers(holder, NS(id="ticket", text=text), "POST", "/mcp")
        with pytest.raises(AssertionError, match="never"):
            await until(lambda: False, "never", seconds=0.2)
    finally:
        await owner.stop()


def test_dev_devices_take_only_their_published_names(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    for build in (
        lambda: dev_mesh.DevOwner("phone", tmp_path / "x"),
        lambda: dev_mesh.DevOwner("node", tmp_path / "x"),
        lambda: dev_mesh.DevDevice("intruder", tmp_path / "x"),
    ):
        with pytest.raises(ValueError):
            build()
    assert list(tmp_path.iterdir()) == []


class Mailbox:
    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.running = True
        self.starts += 1

    async def stop(self):
        self.running = False
        self.stops += 1

    def snapshot(self):
        return {"running": self.running, "address": None}


@pytest.fixture
async def fake_node(tmp_path, monkeypatch):
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    node_identity.reset_cache()
    mailbox = Mailbox()
    mesh = node_mesh.NodeMesh(
        data_dir=tmp_path, settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)), binding=fake,
        mailbox=mailbox,
    )
    await mesh.start()
    kernel = mesh.kernel
    kernel.join(MESH)
    kernel.mesh_principal = lambda: NS(mesh_id=MESH)
    state = NS(members=["sha256:" + "1" * 64], removed=[], malformed=None)

    def roster():
        if state.malformed is not None:
            return state.malformed(kernel)
        return NS(
            mesh_id=MESH, stopped=False,
            members=[NS(device_id=kernel.device_id, is_local=True), *(NS(device_id=d, is_local=False) for d in state.members)],
            removed=[NS(device_id=d, cutoffs=[]) for d in state.removed],
        )

    kernel.roster = roster
    request = kernel.add_request("request-1", "chat-1", author=state.members[0], state="CLAIMED")
    try:
        yield NS(mesh=mesh, kernel=kernel, state=state, mailbox=mailbox, request=node_mesh.RequestReference.capture(request))
    finally:
        await mesh.stop()
        node_identity.reset_cache()


@pytest.mark.parametrize("value", OUTSIDE_DEVELOPMENT)
@pytest.mark.parametrize("where", ["members", "removed"])
@pytest.mark.parametrize("name", ["owner", "phone", "second-owner", "second-phone"])
async def test_a_dev_device_anywhere_on_the_roster_is_refused_outside_development(fake_node, monkeypatch, value, where, name):
    set_env(monkeypatch, value)
    getattr(fake_node.state, where).append(dev_mesh_keys.device_ids()[name])
    assert await fake_node.mesh.refresh_personal_owner() is None
    assert (await fake_node.mesh.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
    assert node_identity.bound_owner() is None and not owner_file().exists()
    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await fake_node.mesh.personal_authority_context(fake_node.request, STORED, fake_node.kernel)
    assert refused.value.code == "node.development_mesh"
    assert fake_node.kernel.network is False and fake_node.mailbox.running is False


@pytest.mark.parametrize("where", ["members", "removed"])
async def test_development_accepts_a_roster_that_lists_dev_devices(fake_node, monkeypatch, where):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    getattr(fake_node.state, where).append(dev_mesh_keys.device_ids()["phone"])
    assert await fake_node.mesh.refresh_personal_owner() == STORED == node_identity.bound_owner()
    context = await fake_node.mesh.personal_authority_context(fake_node.request, STORED, fake_node.kernel)
    assert context.mesh_id == MESH
    assert fake_node.kernel.network is True and fake_node.mailbox.running is True


@pytest.mark.parametrize("value", [None, "production", "development"])
async def test_a_roster_without_dev_devices_binds_in_every_environment(fake_node, monkeypatch, value):
    set_env(monkeypatch, value)
    fake_node.state.removed.append("sha256:" + "2" * 64)
    assert await fake_node.mesh.refresh_personal_owner() == STORED
    assert (await fake_node.mesh.status())["owner_binding"] == {"state": "ready", "code": None}
    context = await fake_node.mesh.personal_authority_context(fake_node.request, STORED, fake_node.kernel)
    assert context.member_id == fake_node.kernel.device_id
    assert fake_node.kernel.network is True and fake_node.mailbox.running is True


CLEAN = "sha256:" + "1" * 64


def stored_node(tmp_path, monkeypatch, value, member, *, supervised):
    set_env(monkeypatch, value)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path))
    node_identity.reset_cache()
    (tmp_path / node_mesh.KERNEL_DIR).mkdir()
    (tmp_path / node_mesh.KERNEL_DIR / "store").write_bytes(b"held")
    record = NS(opened=[], live_at_open=[], started=[])

    class Stored(fake.Kernel):
        @classmethod
        def open(cls, config, signer, model=None, tools=None, listener=None, peer_tools=None, mesh_gate=None):
            record.live_at_open.append(sum(ref() is not None and not ref().closed for ref in record.opened))
            kernel = cls(config, signer, listener)
            cls.instances.remove(kernel)
            kernel.join(MESH)
            kernel.roster = lambda: NS(mesh_id=MESH, stopped=False, removed=[], members=[
                NS(device_id=kernel.device_id, is_local=True), NS(device_id=member, is_local=False)])
            record.opened.append(weakref.ref(kernel))
            return kernel

        async def start_network(self):
            record.started.append(weakref.ref(self))
            return await super().start_network()

    monkeypatch.setattr(fake, "Kernel", Stored)
    record.mailbox = Mailbox() if supervised else None
    record.mesh = node_mesh.NodeMesh(
        data_dir=tmp_path, settings=node_mesh.MeshSettings(mailbox=MailboxSettings(enabled=False)), binding=fake,
        mailbox=record.mailbox,
    )
    return record


@pytest.mark.parametrize("value, member, supervised, online, opens", [
    (None, dev_mesh_keys.device_ids()["owner"], True, False, 1),
    ("production", dev_mesh_keys.device_ids()["second-phone"], True, False, 1),
    ("production", dev_mesh_keys.device_ids()["owner"], False, False, 1),
    ("production", CLEAN, True, True, 2),
    ("production", CLEAN, False, True, 1),
    ("development", dev_mesh_keys.device_ids()["owner"], True, True, 1),
])
async def test_a_node_checks_its_stored_roster_before_it_starts_its_mailbox_or_sync(
    tmp_path, monkeypatch, value, member, supervised, online, opens,
):
    record = stored_node(tmp_path, monkeypatch, value, member, supervised=supervised)
    mesh = record.mesh
    await mesh.start()
    try:
        assert len(record.opened) == opens and record.live_at_open == [0] * opens
        assert mesh.started and bool(record.started) is online and mesh.kernel.network is online
        assert record.opened[-1]() is mesh.kernel
        if supervised:
            assert record.mailbox.starts == (1 if online else 0) and record.mailbox.running is online
        assert (tmp_path / node_mesh.KERNEL_DIR / "store").read_bytes() == b"held"
        binding = (await mesh.status())["owner_binding"]
        if online:
            assert binding == {"state": "setup_required", "code": "mesh.not_enrolled"}
            await mesh.sync_now()
        else:
            assert binding == {"state": "refused", "code": "node.development_mesh"}
            with pytest.raises(node_mesh.NodeMeshError):
                await mesh.sync_now()
    finally:
        await mesh.stop()
        node_identity.reset_cache()


def change(name, progress=None):
    event = type(name, (), {})()
    if progress is not None:
        event.progress = progress
    return event


def offer_seen(peer):
    return change("KernelChange.PAIRING", type("PairingProgress.OFFER_SEEN", (), {"peer": peer})())


@pytest.mark.parametrize("value, peer, cancelled", [
    (None, dev_mesh_keys.device_ids()["owner"], 1),
    ("production", dev_mesh_keys.device_ids()["second-phone"], 1),
    ("production", "sha256:" + "1" * 64, 0),
    ("development", dev_mesh_keys.device_ids()["owner"], 0),
])
async def test_a_pairing_offer_seen_by_a_dev_device_is_cancelled_outside_development(fake_node, monkeypatch, value, peer, cancelled):
    set_env(monkeypatch, value)
    fake_node.mesh._listener.dispatch(offer_seen(peer))
    fake_node.mesh._listener.dispatch(change("KernelChange.PAIRING", type("PairingProgress.APPROVED", (), {})()))
    assert fake_node.kernel.cancelled == cancelled and fake_node.kernel.network is True


@pytest.mark.parametrize("event", [
    change("KernelChange.ROSTER_CHANGED"), change("RosterChanged"), change("KernelChange.ENROLLMENT"),
    change("KernelChange.PAIRING", type("PairingProgress.JOINED", (), {})()),
])
@pytest.mark.parametrize("value, stopped", [(None, True), ("production", True), ("development", False)])
async def test_a_roster_notification_refuses_a_dev_mesh_at_once_without_an_owner_refresh(fake_node, monkeypatch, event, value, stopped):
    set_env(monkeypatch, value)
    fake_node.state.members.append(dev_mesh_keys.device_ids()["owner"])
    fake_node.mesh._listener.dispatch(event)
    if stopped:
        await until(lambda: fake_node.mailbox.stops == 1, "the development check to stop sync", seconds=5)
    else:
        await asyncio.sleep(0)
        assert not fake_node.mesh._checks
    assert fake_node.kernel.network is (not stopped) and fake_node.mailbox.running is (not stopped)
    binding = (await fake_node.mesh.status())["owner_binding"]
    assert binding == ({"state": "refused", "code": "node.development_mesh"} if stopped else {"state": "setup_required", "code": "mesh.not_enrolled"})
    assert node_identity.bound_owner() is None


async def test_the_development_check_reports_a_kernel_error_and_ignores_other_changes(fake_node, monkeypatch, caplog):
    set_env(monkeypatch, "production")
    fake_node.mesh._listener.dispatch(change("KernelChange.CHAT_CHANGED"))
    await asyncio.sleep(0)
    assert not fake_node.mesh._checks

    def unreadable():
        raise fake.KernelError.State("store.unreadable")

    fake_node.kernel.roster = unreadable
    fake_node.mesh._listener.dispatch(change("KernelChange.ROSTER_CHANGED"))
    await until(lambda: "node_mesh_development_check_failed: store.unreadable" in caplog.text, "the failed check to be reported", seconds=5)
    await until(lambda: not fake_node.mesh._checks, "the development check to finish", seconds=5)
    assert fake_node.kernel.network is True


async def test_stopping_the_node_cancels_a_pending_development_check(fake_node, monkeypatch):
    set_env(monkeypatch, "production")
    await fake_node.mesh._owner_lock.acquire()
    fake_node.mesh._listener.dispatch(change("KernelChange.ROSTER_CHANGED"))
    await until(lambda: bool(fake_node.mesh._checks), "the development check to be scheduled", seconds=5)
    pending = list(fake_node.mesh._checks)
    await fake_node.mesh.stop()
    fake_node.mesh._owner_lock.release()
    assert all(task.cancelled() for task in pending) and not fake_node.mesh._checks


def _base(kernel):
    return [NS(device_id=kernel.device_id, is_local=True), NS(device_id="sha256:" + "1" * 64, is_local=False)]


MALFORMED = {
    "no_removed": lambda k: NS(mesh_id=MESH, stopped=False, members=_base(k)),
    "removed_none": lambda k: NS(mesh_id=MESH, stopped=False, members=_base(k), removed=None),
    "removed_without_id": lambda k: NS(mesh_id=MESH, stopped=False, members=_base(k), removed=[NS(cutoffs=[])]),
    "removed_id_not_text": lambda k: NS(mesh_id=MESH, stopped=False, members=_base(k), removed=[NS(device_id=7)]),
    "no_members": lambda k: NS(mesh_id=MESH, stopped=False, removed=[]),
    "members_none": lambda k: NS(mesh_id=MESH, stopped=False, members=None, removed=[]),
    "member_without_id": lambda k: NS(mesh_id=MESH, stopped=False, members=[*_base(k), NS(is_local=False)], removed=[]),
    "member_id_not_text": lambda k: NS(mesh_id=MESH, stopped=False, members=[*_base(k), NS(device_id=None)], removed=[]),
    "no_mesh_id": lambda k: NS(stopped=False, members=_base(k), removed=[]),
    "mesh_id_not_text": lambda k: NS(mesh_id=None, stopped=False, members=_base(k), removed=[]),
}


@pytest.mark.parametrize("shape", sorted(MALFORMED))
@pytest.mark.parametrize("value", [None, "production"])
async def test_a_malformed_roster_outside_development_clears_a_bound_owner_and_refuses(fake_node, monkeypatch, value, shape):
    set_env(monkeypatch, value)
    assert await fake_node.mesh.refresh_personal_owner() == STORED == fake_node.mesh.personal_owner_id()
    stored = owner_file().read_bytes()
    fake_node.state.malformed = MALFORMED[shape]
    assert await fake_node.mesh.refresh_personal_owner() is None
    assert fake_node.mesh.personal_owner_id() is None
    assert (await fake_node.mesh.status())["owner_binding"] == {"state": "refused", "code": "node.development_mesh"}
    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await fake_node.mesh.personal_authority_context(fake_node.request, STORED, fake_node.kernel)
    assert refused.value.code == "node.development_mesh"
    assert owner_file().read_bytes() == stored
    assert fake_node.kernel.network is False and fake_node.mailbox.stops == 1


async def test_serving_a_dev_mesh_outside_development_stops_sync_once(fake_node, monkeypatch):
    set_env(monkeypatch, "production")
    fake_node.state.members.append(dev_mesh_keys.device_ids()["owner"])
    for _ in range(2):
        with pytest.raises(node_mesh.NodeMeshError) as refused:
            await fake_node.mesh.personal_authority_context(fake_node.request, STORED, fake_node.kernel)
        assert refused.value.code == "node.development_mesh"
    assert fake_node.kernel.network is False and fake_node.mailbox.stops == 1
    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await fake_node.mesh.sync_now()
    assert refused.value.code == "node.development_mesh"
    assert node_identity.bound_owner() is None


@pytest.mark.parametrize("value, sent", [(None, False), ("production", False), ("development", True)])
async def test_the_wake_trigger_never_pushes_for_a_dev_mesh_outside_development(fake_node, monkeypatch, value, sent):
    set_env(monkeypatch, value)
    fake_node.state.members.append(dev_mesh_keys.device_ids()["phone"])
    phone = dev_mesh_keys.device_ids()["phone"]
    fake_node.kernel.peers = [peer(phone, connected=False, pending_out=1)]
    transport = FakeTransport()
    gateway = push_wake.WakeGateway(
        targets=Targets(APNS), adapters={"apns": adapter(transport)}, limiter=push_wake.WakeLimiter(min_interval_seconds=0),
    )
    await fake_node.mesh.refresh_personal_owner()
    outcomes = await push_wake.WakeTrigger(fake_node.mesh, gateway, fake_node.mesh.personal_owner_id).evaluate()
    if sent:
        assert [outcome.status for outcome in outcomes] == [push_wake.SENT] and len(transport.calls) == 1
    else:
        assert outcomes == [] and transport.calls == []


DEVTOOLS = ("devtools", "backend.devtools")


def devtools_imports(root: Path) -> list:
    found = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if callee in {"import_module", "__import__"} and isinstance(node.args[0].value, str):
                    names = [node.args[0].value]
            if any(name in DEVTOOLS or name.startswith(tuple(f"{root}." for root in DEVTOOLS)) for name in names):
                found.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return found


def test_no_orchestrator_or_shared_module_imports_devtools():
    assert devtools_imports(BACKEND / "orchestrator") == []
    assert devtools_imports(BACKEND / "shared") == []


def test_the_import_scan_finds_every_form_of_a_devtools_import(tmp_path):
    (tmp_path / "pkg").mkdir()
    sources = {
        "a.py": "import devtools.dev_mesh\n",
        "b.py": "from devtools import dev_mesh\n",
        "c.py": "import importlib\nimportlib.import_module('devtools.dev_mesh')\n",
        "d.py": "__import__('devtools')\n",
        "f.py": "import backend.devtools\n",
        "g.py": "from backend.devtools import dev_mesh\n",
        "h.py": "from backend import devtools\n",
        "i.py": "import importlib\nimportlib.import_module('backend.devtools.dev_mesh')\n",
        "pkg/e.py": "def f():\n    import os, devtools\n",
        "quiet.py": (
            "BROWSER = '/devtools/browser/x'\nfrom .devtools import local\nimport devtoolsx\n"
            "import backend.devtoolsx\nfrom backend import shared\nfrom shared import devtools_names\n"
        ),
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source)
    assert devtools_imports(tmp_path) == [
        "a.py:1", "b.py:1", "c.py:2", "d.py:1", "f.py:1", "g.py:1", "h.py:1", "i.py:2", "pkg/e.py:2",
    ]
