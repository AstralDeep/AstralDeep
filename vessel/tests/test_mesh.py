# The executor's mesh membership and key material, against a scripted kernel binding.

from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from astralvessel import keys, mesh


class KernelError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Kernel:
    opened: list = []

    def __init__(self, config, signer):
        self.config, self.signer = config, signer
        self.state = "NOT_ENROLLED"
        self.network = False
        self.tickets: list = []
        self.error = None
        Kernel.opened.append(self)

    @classmethod
    def open(cls, config, signer, model, tools, listener):
        assert listener.on_change("x") is None
        return cls(config, signer)

    async def start_network(self):
        self.network = True

    async def stop_network(self):
        self.network = False

    async def mesh_status(self):
        return SimpleNamespace(state=SimpleNamespace(name=self.state), mesh_id="m")

    async def create_pairing_offer(self):
        if self.error:
            raise KernelError(self.error)
        return SimpleNamespace(code="1234 5678", payload="PAYLOAD", expires_ms=77)

    def verify_ticket(self, ticket_json, action_json, effect):
        self.tickets.append((ticket_json, action_json, effect))
        if self.error:
            raise KernelError(self.error)
        return SimpleNamespace(mesh="m", device="d", turn="t", action_digest="sha256:" + "9" * 64, agents=["a"])


class Denied(Exception):
    pass


def binding():
    return SimpleNamespace(
        Kernel=Kernel, KernelError=KernelError, KernelListener=object,
        HostSigner=object, HostSignerError=SimpleNamespace(Denied=Denied),
        KernelConfig=lambda **kw: SimpleNamespace(**kw), NetworkConfig=lambda **kw: SimpleNamespace(**kw),
        RelayChoice=SimpleNamespace(DISABLED=lambda: "disabled"), DeviceKind=SimpleNamespace(NODE="node"),
        ConsentKind=SimpleNamespace(OTHER="other"), new_operations_secret=lambda: os.urandom(32))


@pytest.fixture(autouse=True)
def clean():
    Kernel.opened = []


class Mailbox:
    def __init__(self, *answers):
        self.answers = list(answers)

    async def __call__(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


async def opened(tmp_path, *answers, **kw):
    verifier = mesh.KernelVerifier(tmp_path, binding=binding(), mailbox=Mailbox(*answers), interval=0.01, **kw)
    await verifier.start()
    return verifier


async def until(condition):
    for _ in range(300):
        if await condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def test_codes_are_dotted_and_bounded():
    assert mesh.dotted("ticket.expired") == "ticket.expired"
    assert mesh.dotted("Kernel Error!", "ticket") == "ticket.kernel_error"
    assert mesh.dotted("", "ticket") == "ticket.failed" and mesh.dotted(None) == "executor.failed"
    assert len(mesh.dotted("x" * 200)) == 80 and len(mesh.dotted("a." + "b" * 200)) == 80


async def test_without_a_binding_every_ticket_is_refused(tmp_path):
    verifier = mesh.KernelVerifier(tmp_path, binding=None, mailbox=Mailbox(None))
    verifier._binding = None
    await verifier.start()
    assert await verifier.state() == "no_kernel"
    for call in (verifier.verify("{}", "{}", "mesh_read"), verifier.pairing_offer()):
        with pytest.raises(mesh.Refused) as refused:
            await call
        assert refused.value.code == "executor.no_kernel"
    await verifier.stop()


async def test_the_executor_waits_for_the_nodes_mailbox_and_then_offers_pairing(tmp_path):
    verifier = await opened(tmp_path, None, None, "MAILBOX")
    assert await verifier.state() == "waiting_for_node"
    with pytest.raises(mesh.Refused, match="executor.waiting_for_node"):
        await verifier.pairing_offer()
    with pytest.raises(mesh.Refused, match="executor.waiting_for_node"):
        await verifier.verify("{}", "{}", "mesh_read")

    async def ready():
        return await verifier.state() == "not_enrolled"

    await until(ready)
    [kernel] = Kernel.opened
    assert kernel.network and kernel.config.network.mailbox == "MAILBOX"
    assert kernel.config.network.relay == "disabled" and kernel.config.network.local_discovery is False
    assert kernel.config.network.internet_lookup is False and kernel.config.device_kind == "node"
    assert kernel.config.issuers == [] and kernel.config.data_dir == str(tmp_path / "kernel")
    assert await verifier.pairing_offer() == {"code": "1234 5678", "payload": "PAYLOAD", "expires_ms": 77}
    with pytest.raises(mesh.Refused, match="executor.not_enrolled"):
        await verifier.verify("{}", "{}", "mesh_read")
    assert await verifier.mesh_id() is None
    kernel.error = "mesh.pairing_closed"
    with pytest.raises(mesh.Refused, match="mesh.pairing_closed"):
        await verifier.pairing_offer()
    await verifier.stop()
    assert not kernel.network


async def test_a_loopback_executor_opens_at_once_without_a_mailbox_and_stays_on_loopback(tmp_path):
    verifier = await opened(tmp_path, None, loopback=True)
    await until(lambda: _is(verifier, "not_enrolled"))
    [kernel] = Kernel.opened
    assert kernel.config.network.loopback_only is True and kernel.config.network.mailbox is None
    await verifier.stop()


async def _is(verifier, state):
    return await verifier.state() == state


async def test_an_enrolled_executor_verifies_and_maps_kernel_refusals(tmp_path):
    verifier = await opened(tmp_path, "MAILBOX")

    async def ready():
        return await verifier.state() != "waiting_for_node"

    await until(ready)
    [kernel] = Kernel.opened
    kernel.state = "ENROLLED"
    assert await verifier.state() == "enrolled"
    claims = await verifier.verify("T", "A", "outside_effect")
    assert claims == mesh.Verified("m", "d", "t", "sha256:" + "9" * 64, ("a",))
    assert kernel.tickets == [("T", "A", "outside_effect")]
    with pytest.raises(mesh.Refused, match="executor.enrolled"):
        await verifier.pairing_offer()
    assert await verifier.mesh_id() == "m"
    for raised, shown in (("ticket.expired", "ticket.expired"), ("Weird Failure", "ticket.weird_failure")):
        kernel.error = raised
        with pytest.raises(mesh.Refused) as refused:
            await verifier.verify("T", "A", "outside_effect")
        assert refused.value.code == shown
    kernel.state = "RESTORED"
    kernel.error = None
    assert await verifier.state() == "enrolled"
    await verifier.stop()


async def test_the_keys_made_for_the_kernel_are_private_and_kept(tmp_path):
    verifier = await opened(tmp_path, "MAILBOX")

    async def ready():
        return bool(Kernel.opened)

    await until(ready)
    first = Kernel.opened[0].config
    folder = tmp_path / "keys"
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700 and stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
    assert sorted(p.name for p in folder.iterdir()) == [
        "identity.pem", "operations.secret", "presence.pem", "restore.marker", "wrap.secret"]
    assert all(stat.S_IMODE(os.stat(p).st_mode) == 0o600 for p in folder.iterdir())
    await verifier.stop()
    again = await opened(tmp_path, "MAILBOX")
    await until(ready)
    second = Kernel.opened[-1].config
    assert (first.operations_secret, first.wrap_secret, first.restore_marker) == (
        second.operations_secret, second.wrap_secret, second.restore_marker)
    await again.stop()


async def test_the_signer_signs_for_the_kernel_but_never_for_an_owners_consent(tmp_path):
    verifier = await opened(tmp_path, "MAILBOX")

    async def ready():
        return bool(Kernel.opened)

    await until(ready)
    signer = Kernel.opened[0].signer
    point = signer.identity_public_key()
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    message = b"hello"
    raw = signer.identity_sign(message)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, message, ec.ECDSA(hashes.SHA256()))
    assert int.from_bytes(raw[32:], "big") <= keys.P256_ORDER // 2
    assert signer.hardware_backed() is False
    assert signer.presence_public_key() != point and len(signer.presence_public_key()) == 65
    assert len(signer.presence_sign(SimpleNamespace(kind="other"), b"x")) == 64
    with pytest.raises(Denied, match="never signs an owner consent"):
        signer.presence_sign(SimpleNamespace(kind="OWNER_PRESENCE"), b"x")
    await verifier.stop()


def test_a_key_file_with_loose_modes_or_bad_content_is_refused(tmp_path):
    def load():
        return keys.ExecutorKeys.load_or_create(tmp_path, new_secret=lambda: os.urandom(32))

    load()
    folder = tmp_path / "keys"
    (folder / "wrap.secret").chmod(0o640)
    with pytest.raises(keys.KeysError, match="readable only by the executor"):
        load()
    (folder / "wrap.secret").chmod(0o600)
    (folder / "wrap.secret").write_bytes(b"")
    with pytest.raises(keys.KeysError, match="is empty"):
        load()
    (folder / "wrap.secret").write_bytes(b"x" * 32)
    (folder / "identity.pem").write_bytes(b"not a key")
    with pytest.raises(keys.KeysError, match="not a valid private key"):
        load()
    other = ec.generate_private_key(ec.SECP384R1())
    from cryptography.hazmat.primitives import serialization
    (folder / "identity.pem").write_bytes(other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(keys.KeysError, match="must be a P-256 key"):
        load()


def test_a_loose_directory_is_tightened_and_a_failed_write_leaves_no_half_file(tmp_path):
    tmp_path.chmod(0o755)
    keys.ExecutorKeys.load_or_create(tmp_path, new_secret=lambda: os.urandom(32))
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
    fresh = tmp_path / "second"

    def broken():
        raise RuntimeError("entropy failed")

    with pytest.raises(RuntimeError):
        keys.ExecutorKeys.load_or_create(fresh, new_secret=broken)
    assert not (fresh / "keys" / "operations.secret").exists()
    wrong_length = tmp_path / "third"
    (wrong_length / "keys").mkdir(parents=True)
    (wrong_length / "keys" / "restore.marker").write_bytes(b"short")
    (wrong_length / "keys" / "restore.marker").chmod(0o600)
    with pytest.raises(keys.KeysError, match="wrong length"):
        keys.ExecutorKeys.load_or_create(wrong_length, new_secret=lambda: os.urandom(32))
