"""Tests for the node's mesh peer: key custody, the software signer, kernel composition, status, pairing offers and chat delegation against a fake kernel binding."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from orchestrator import node_mesh, node_mesh_keys
from orchestrator.node_mailbox import MailboxError
from shared import node_identity as ni

from tests import fake_astral_kernel as fake


@pytest.fixture(autouse=True)
def reset_fake():
    fake.Kernel.instances.clear()
    yield
    node_mesh.install(None)


async def no_issuers():
    return [{"issuer": "https://idp.example/realms/a", "client_id": "c", "jwks_json": "{}"}]


class StubMailbox:
    def __init__(self, fail=False):
        self.fail = fail
        self.started = self.stopped = False

    async def start(self):
        if self.fail:
            raise MailboxError("mailbox_binary_missing")
        self.started = True

    async def stop(self):
        self.stopped = True

    def snapshot(self):
        return {"running": self.started and not self.stopped, "address": '{"id":"mb"}', "id": "mb", "restarts": 0, "last_error": None}


def make(tmp_path, **kwargs):
    kwargs.setdefault("mailbox", StubMailbox())
    return node_mesh.NodeMesh(data_dir=tmp_path, binding=fake, issuers=no_issuers, **kwargs)


def test_keys_are_generated_once_at_mode_0600(tmp_path):
    first = node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)
    second = node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=lambda: b"x" * 32)
    assert first.operations_secret == second.operations_secret
    assert first.identity_public_key() == second.identity_public_key()
    assert first.restore_marker == second.restore_marker and len(first.restore_marker) == 32
    directory = tmp_path / "mesh"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for entry in directory.iterdir():
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600


def test_keys_refuse_loose_empty_or_wrong_files(tmp_path):
    node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)
    wrap = tmp_path / "mesh" / node_mesh_keys.WRAP_FILE
    os.chmod(wrap, 0o644)
    with pytest.raises(node_mesh_keys.NodeMeshKeyError):
        node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)
    os.chmod(wrap, 0o600)
    wrap.write_bytes(b"")
    with pytest.raises(node_mesh_keys.NodeMeshKeyError, match="empty"):
        node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)
    wrap.write_bytes(b"k" * 32)
    (tmp_path / "mesh" / node_mesh_keys.IDENTITY_FILE).write_bytes(b"not a pem")
    with pytest.raises(node_mesh_keys.NodeMeshKeyError, match="valid private key"):
        node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)


def test_wrong_curve_key_is_refused(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)
    pem = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    (tmp_path / "mesh" / node_mesh_keys.PRESENCE_FILE).write_bytes(pem)
    with pytest.raises(node_mesh_keys.NodeMeshKeyError, match="P-256"):
        node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)


def test_a_failed_write_leaves_no_partial_secret(tmp_path):
    def broken():
        raise RuntimeError("entropy")

    with pytest.raises(RuntimeError):
        node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=broken)
    assert not (tmp_path / "mesh" / node_mesh_keys.OPERATIONS_FILE).exists()


def test_signatures_verify_and_are_low_s(tmp_path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    keys = node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keys.identity_public_key())
    for index in range(20):
        message = b"m%d" % index
        raw = keys.identity_sign(message)
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        assert s <= node_mesh_keys.P256_ORDER // 2
        public.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    assert keys.presence_sign("OTHER", b"statement")
    assert not keys.hardware_backed
    assert keys.fingerprint() in repr(keys) and "secret" not in repr(keys)


@pytest.mark.parametrize("kind", ["ROSTER_ADD", "ROSTER_REMOVE", "STOP", "TOOL_EFFECT"])
def test_presence_never_signs_owner_consent(tmp_path, kind):
    keys = node_mesh_keys.NodeMeshKeys.load_or_create(tmp_path, new_secret=fake.new_operations_secret)
    with pytest.raises(node_mesh_keys.PresenceRefused):
        keys.presence_sign(kind, b"x")


async def test_start_opens_the_kernel_as_a_node_with_software_custody(tmp_path):
    mesh = make(tmp_path)
    await mesh.start()
    kernel = fake.Kernel.instances[0]
    assert kernel.config.device_kind == fake.DeviceKind.NODE
    assert kernel.config.network.mailbox == '{"id":"mb"}'
    assert kernel.config.issuers[0].client_id == "c"
    assert kernel.network and mesh.started
    signer = kernel.signer
    assert signer.hardware_backed() is False
    assert signer.identity_public_key() == mesh._keys.identity_public_key()
    assert len(signer.identity_sign(b"m")) == 64
    assert signer.presence_public_key() == mesh._keys.presence_public_key()
    from types import SimpleNamespace

    assert signer.presence_sign(SimpleNamespace(kind=fake.ConsentKind.OTHER), b"m")
    with pytest.raises(fake.HostSignerError.Denied):
        signer.presence_sign(SimpleNamespace(kind=fake.ConsentKind.ROSTER_ADD), b"m")
    await mesh.start()
    assert len(fake.Kernel.instances) == 1
    await mesh.stop()
    assert not kernel.network and mesh._mailbox.stopped
    await mesh.stop()


async def test_status_before_and_after_joining(tmp_path):
    mesh = make(tmp_path)
    assert (await mesh.status()) == {"available": False, "state": "stopped"}
    await mesh.start()
    status = await mesh.status()
    assert status["state"] == "not_enrolled" and status["mesh_id"] is None and status["member_id"] is None
    assert status["hardware_backed"] is False and status["requests"] is True
    assert status["mailbox"]["address"] == '{"id":"mb"}'
    fake.Kernel.instances[0].join("mesh-9")
    status = await mesh.status()
    assert (status["state"], status["mesh_id"], status["member_id"], status["members"]) == ("enrolled", "mesh-9", "dev-node", 2)


async def test_status_survives_an_unavailable_endpoint(tmp_path):
    mesh = make(tmp_path)
    await mesh.start()
    fake.Kernel.instances[0].address_error = RuntimeError("offline")
    assert (await mesh.status())["endpoint_address"] is None


async def test_pairing_offer_and_refusal_once_joined(tmp_path):
    mesh = make(tmp_path)
    await mesh.start()
    offer = await mesh.pairing_offer()
    assert offer["code"] == "123-456-789" and offer["payload"] == "payload-1"
    assert offer["endpoint_address"] and offer["mailbox"]["address"]
    await mesh.cancel_pairing_offer()
    assert fake.Kernel.instances[0].cancelled == 1
    fake.Kernel.instances[0].join()
    with pytest.raises(node_mesh.NodeMeshError) as refused:
        await mesh.pairing_offer()
    assert refused.value.code == "mesh_already_joined"


async def test_kernel_errors_become_mesh_errors(tmp_path):
    mesh = make(tmp_path)
    await mesh.start()
    kernel = fake.Kernel.instances[0]

    async def broken():
        raise fake.KernelError.State("pairing.busy")

    kernel.create_pairing_offer = broken
    with pytest.raises(node_mesh.NodeMeshError) as caught:
        await mesh.pairing_offer()
    assert caught.value.code == "pairing.busy"

    async def crash():
        raise RuntimeError("boom")

    kernel.create_pairing_offer = crash
    with pytest.raises(RuntimeError):
        await mesh.pairing_offer()


async def test_a_missing_mailbox_does_not_stop_the_mesh(tmp_path):
    mesh = make(tmp_path, mailbox=StubMailbox(fail=True))
    await mesh.start()
    status = await mesh.status()
    assert status["available"] and status["mailbox"] == {"running": False, "address": None, "error": "mailbox_binary_missing"}


async def test_a_mailbox_is_created_from_settings_when_enabled(tmp_path, monkeypatch):
    created = {}

    class Recording(StubMailbox):
        def __init__(self, settings, *, data_dir):
            super().__init__()
            created["dir"] = data_dir

    monkeypatch.setattr(node_mesh, "MailboxProcess", Recording)
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=fake, issuers=no_issuers)
    await mesh.start()
    assert created["dir"] == tmp_path / "mailbox" and mesh.mailbox.started


async def test_kernel_start_failure_stops_the_network(tmp_path):
    mesh = make(tmp_path)

    async def failing():
        raise RuntimeError("no network")

    original = fake.Kernel.open

    def open_failing(config, signer, model=None, tools=None, listener=None):
        kernel = original(config, signer, model, tools, listener)
        kernel.start_network = failing
        return kernel

    class Binding:
        def __getattr__(self, name):
            if name == "Kernel":
                return type("K", (), {"open": staticmethod(open_failing)})
            return getattr(fake, name)

    mesh._binding = Binding()
    with pytest.raises(RuntimeError):
        await mesh.start()
    assert not mesh.started
    with pytest.raises(node_mesh.MeshUnavailable):
        mesh.kernel


async def test_chat_operations_delegate_to_the_kernel(tmp_path):
    mesh = make(tmp_path)
    with pytest.raises(node_mesh.MeshUnavailable):
        mesh.subscribe(lambda change: None)
    await mesh.start()
    chat = await mesh.create_chat("hello")
    assert (await mesh.send_message(chat, "hi")).startswith("msg-")
    assert [c.chat_id for c in await mesh.list_chats()] == [chat]
    assert (await mesh.chat(chat)).view.chat_id == chat
    await mesh.sync_now()


async def test_the_kernel_opens_with_no_mailbox_address_when_the_mailbox_fails(tmp_path):
    mesh = make(tmp_path, mailbox=StubMailbox(fail=True))
    await mesh.start()
    assert fake.Kernel.instances[0].config.network.mailbox is None
    await mesh.stop()


async def test_the_mailbox_runs_before_the_kernel_opens_and_stops_after_it(tmp_path):
    order = []

    class Ordered(StubMailbox):
        async def start(self):
            order.append("mailbox up")
            await super().start()

        async def stop(self):
            order.append("mailbox down")
            await super().stop()

    mesh = make(tmp_path, mailbox=Ordered())
    original = fake.Kernel.open.__func__

    def opening(cls, *args, **kwargs):
        order.append("kernel open")
        return original(cls, *args, **kwargs)

    fake.Kernel.open = classmethod(opening)
    try:
        await mesh.start()
        kernel = fake.Kernel.instances[0]
        original_stop = kernel.stop_network

        async def stopping():
            order.append("kernel down")
            await original_stop()

        kernel.stop_network = stopping
        await mesh.stop()
    finally:
        fake.Kernel.open = classmethod(original)
    assert order == ["mailbox up", "kernel open", "kernel down", "mailbox down"]


async def test_a_failed_network_start_stops_the_kernel_and_the_mailbox(tmp_path):
    mesh = make(tmp_path)
    original = fake.Kernel.start_network

    async def failing(self):
        raise RuntimeError("no network")

    fake.Kernel.start_network = failing
    try:
        with pytest.raises(RuntimeError):
            await mesh.start()
    finally:
        fake.Kernel.start_network = original
    assert mesh._mailbox.stopped and not fake.Kernel.instances[0].network and not mesh.started


async def test_request_operations_delegate_to_the_kernel_and_name_refusals(tmp_path):
    mesh = make(tmp_path)
    await mesh.start()
    kernel = fake.Kernel.instances[0]
    kernel.join()
    kernel.add_request("req-1", "chat-1", "summarize")
    assert [r.id for r in await mesh.pending_requests()] == ["req-1"]
    assert [r.id for r in await mesh.chat_requests("chat-1")] == ["req-1"]
    claim = await mesh.claim_request("req-1")
    assert claim == await mesh.claim_request("req-1")
    assert await mesh.pending_requests() == []
    step = node_mesh.StepRecord(
        call_id="c1", tool="lookup", arguments_json="{}", tool_def="sha256:" + "a" * 64, status="ok",
        content="found", labels=("private", "tool_trusted"),
    )
    turn_id = await mesh.record_turn(
        "chat-1", continues=None, message=None, answers="req-1", provider="node", model="m",
        labels=("phi", "untrusted"), steps=[step], text="done", reason="completed",
    )
    assert turn_id == kernel.request("req-1").turn
    _, turn = kernel.recorded[0]
    assert turn.labels == fake.peer_labels("PHI", "UNTRUSTED") and turn.steps[0].labels == fake.peer_labels("PRIVATE", "TOOL_TRUSTED")
    assert (turn.answers, turn.text, turn.steps[0].tool, turn.steps[0].consent) == ("req-1", "done", "lookup", None)
    with pytest.raises(node_mesh.NodeMeshError) as closed:
        await mesh.refuse_request("req-1", "node.late")
    assert closed.value.code == "request.answered"
    kernel.add_request("req-2", "chat-1")
    with pytest.raises(node_mesh.NodeMeshError) as unclaimed:
        await mesh.record_turn(
            "chat-1", continues=None, message=None, answers="req-2", provider="node", model="m",
            labels=("private", "owner"), steps=[], text="x", reason="completed",
        )
    assert unclaimed.value.code == "request.not_claimed"
    assert await mesh.refuse_request("req-2", "node.busy")
    kernel.ticket_error = "ticket.bad_root"
    with pytest.raises(node_mesh.NodeMeshError) as forged:
        await mesh.verify_ticket("{}", "{}", "mesh_read")
    assert forged.value.code == "ticket.bad_root"


async def test_requests_require_the_kernel_export(tmp_path, monkeypatch):
    mesh = make(tmp_path)
    await mesh.start()
    monkeypatch.delattr(fake.Kernel, "record_peer_turn")
    assert mesh.requests_supported is False


async def test_listener_dispatches_and_survives_a_failing_subscriber(tmp_path):
    mesh = make(tmp_path)
    await mesh.start()
    seen = []

    def failing(change):
        raise RuntimeError("bad")

    mesh.subscribe(failing)
    mesh.subscribe(seen.append)
    fake.Kernel.instances[0].notify()
    assert len(seen) == 1


def test_settings_from_environment():
    settings = node_mesh.MeshSettings.from_env(
        {
            "ASTRAL_NODE_MESH_LABEL": " Home node ",
            "ASTRAL_NODE_MESH_RELAY": "none",
            "ASTRAL_NODE_MESH_LOOPBACK": "1",
            "ASTRAL_NODE_MESH_SYNC_MS": "10",
            "ASTRAL_NODE_MAILBOX": "0",
        }
    )
    assert settings.label == "Home node" and settings.relay == ("none",)
    assert settings.loopback_only and not settings.local_discovery and not settings.internet_lookup
    assert settings.sync_interval_ms == node_mesh.MIN_SYNC_MS
    assert settings.mailbox.enabled is False and settings.mailbox.relay == ("none",)
    fallback = node_mesh.MeshSettings.from_env({"ASTRAL_NODE_MESH_SYNC_MS": "x", "ASTRAL_NODE_MESH_RELAY": ","})
    assert fallback.sync_interval_ms == node_mesh.DEFAULT_SYNC_MS and fallback.relay == ("default",)
    assert node_mesh._relay(fake, ("https://r.example/",)).urls == ["https://r.example/"]
    assert isinstance(node_mesh._relay(fake, ("none",)), fake.RelayChoice.DISABLED)
    assert isinstance(node_mesh._relay(fake, ("default",)), fake.RelayChoice.DEFAULT)


def test_enabled_needs_the_personal_profile(monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    assert node_mesh.enabled() is False
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    assert node_mesh.enabled({}) is True
    assert node_mesh.enabled({"ASTRAL_NODE_MESH": "off"}) is False


def test_missing_binding_is_reported(monkeypatch):
    monkeypatch.setattr(node_mesh, "BINDING_MODULE", "astral_kernel_not_installed")
    with pytest.raises(node_mesh.MeshUnavailable) as caught:
        node_mesh.load_binding()
    assert caught.value.code == "kernel_binding_missing"


async def test_start_configured_installs_and_shutdown_clears(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv("ASTRAL_NODE_MAILBOX", "0")
    monkeypatch.setattr(node_mesh, "load_binding", lambda: fake)
    monkeypatch.setattr(node_mesh, "default_issuers", no_issuers)
    mesh = await node_mesh.start_configured(tmp_path)
    assert mesh is not None and node_mesh.current() is mesh
    await node_mesh.shutdown()
    assert node_mesh.current() is None and not mesh.started


async def test_mint_passes_the_exact_call_and_running_labels_to_the_binding(tmp_path, monkeypatch):
    mesh = make(tmp_path)
    await mesh.start()
    captured = []
    monkeypatch.setattr(fake, "MintCall", lambda **kwargs: SimpleNamespace(**kwargs), raising=False)

    def mint(request_id, call):
        captured.append((request_id, call))
        return SimpleNamespace(ticket_json="ticket", action_json="action", receipt_event="receipt")

    monkeypatch.setattr(mesh.kernel, "mint_ticket_for_request", mint, raising=False)
    result = await mesh.mint_ticket(
        "request", call_id="call-1", tool="lookup", arguments_json='{"q":"x"}',
        tool_def="sha256:" + "a" * 64, effect_kind="outward_read", labels=("private", "untrusted"), untrusted_arguments=True,
    )
    request_id, call = captured[0]
    assert result.receipt_event == "receipt" and request_id == "request"
    assert (call.call_id, call.tool, call.arguments_json) == ("call-1", "lookup", '{"q":"x"}')
    assert call.tool_def_hash == "sha256:" + "a" * 64 and call.effect_kind == "outward_read"
    assert call.labels == fake.peer_labels("PRIVATE", "UNTRUSTED")
    assert call.untrusted_arguments and call.influence == [] and call.consent_json is None
    await mesh.stop()


async def test_mint_refuses_old_bindings_and_keeps_kernel_denial_codes(tmp_path, monkeypatch):
    mesh = make(tmp_path)
    await mesh.start()
    args = dict(call_id="call", tool="lookup", arguments_json="{}", tool_def="hash", effect_kind="outward_read", labels=("private", "owner"), untrusted_arguments=False)
    with pytest.raises(node_mesh.MeshUnavailable) as unavailable:
        await mesh.mint_ticket("request", **args)
    assert unavailable.value.code == "mint_unsupported"
    monkeypatch.setattr(fake, "MintCall", lambda **kwargs: SimpleNamespace(**kwargs), raising=False)

    def refuse(*args):
        raise fake.KernelError.InvalidInput("gate.permission_denied", "refused")

    monkeypatch.setattr(mesh.kernel, "mint_ticket_for_request", refuse, raising=False)
    with pytest.raises(node_mesh.NodeMeshError) as denied:
        await mesh.mint_ticket("request", **args)
    assert denied.value.code == "gate.permission_denied"
    await mesh.stop()


async def test_start_configured_is_off_without_the_profile_or_binding(tmp_path, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    assert await node_mesh.start_configured(tmp_path) is None
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")

    def missing():
        raise node_mesh.MeshUnavailable("kernel_binding_missing")

    monkeypatch.setattr(node_mesh, "load_binding", missing)
    assert await node_mesh.start_configured(tmp_path) is None
    await node_mesh.shutdown()


async def test_default_issuers_fetch_keys_and_skip_failures(monkeypatch):
    from shared import jwks_cache

    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "client")

    async def get(url, token=None):
        return {"keys": [{"kid": "k"}]}

    monkeypatch.setattr(jwks_cache, "get_jwks", get)
    found = await node_mesh.default_issuers()
    assert found == [{"issuer": "https://idp.example/realms/a", "client_id": "client", "jwks_json": '{"keys": [{"kid": "k"}]}'}]

    async def fail(url, token=None):
        raise RuntimeError("down")

    monkeypatch.setattr(jwks_cache, "get_jwks", fail)
    assert await node_mesh.default_issuers() == []
    monkeypatch.delenv("KEYCLOAK_CLIENT_ID")
    assert await node_mesh.default_issuers() == []
