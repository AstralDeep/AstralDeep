"""Opens the seeded development mesh devices on the real kernel binding with the published keys of shared/dev_mesh_keys.py, so a developer or a test can create a mesh, pair a node with full history, send it requests and call it with caller tickets and holder proofs.
It refuses to build a device, and a device refuses to create, start, offer, approve, send, mint or sign, unless ASTRAL_ENV is exactly development. tests/helpers/dev_mesh.py builds on it and no orchestrator or shared module imports it."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import astral_kernel as ak
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from shared import dev_mesh_keys

ENV = "ASTRAL_ENV"
DEVELOPMENT = "development"
OWNERS = ("owner", "second-owner")
KINDS = {
    "owner": ak.DeviceKind.DESKTOP,
    "phone": ak.DeviceKind.POCKET,
    "second-owner": ak.DeviceKind.DESKTOP,
    "second-phone": ak.DeviceKind.POCKET,
}
SYNC_INTERVAL_MS = 500
DEFAULT_LIFETIME_S = 604_800
REQUEST_CHAT_TITLE = "Development requests"
HOLDER_TEXT = "astral-caller-holder:v1:"
KEY_TEXT = "astral-caller-key:v1:"
TICKET_TEXT = "astral-caller-ticket:v1:"
SCHEME = "AstralCaller "
PROOF_CONTEXT = b"astral.caller-proof/v1\n"
PROOF_HEADER = "Astral-Caller-Proof"
NONCE_BYTES = 16


class DevMeshRefused(RuntimeError):
    pass


def require_development() -> None:
    if os.environ.get(ENV) != DEVELOPMENT:
        raise DevMeshRefused(f"the development mesh runs only with {ENV}={DEVELOPMENT}")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    if s > dev_mesh_keys.P256_ORDER // 2:
        s = dev_mesh_keys.P256_ORDER - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


class _Signer(ak.HostSigner):
    def __init__(self, keys: dev_mesh_keys.DevDeviceKeys):
        self._keys = keys
        self._identity = ec.derive_private_key(keys.identity_scalar, ec.SECP256R1())
        self._presence = ec.derive_private_key(keys.presence_scalar, ec.SECP256R1())

    def identity_public_key(self):
        return self._keys.identity_public_key

    def identity_sign(self, message):
        return _sign(self._identity, bytes(message))

    def presence_public_key(self):
        return self._keys.presence_public_key

    def presence_sign(self, prompt, message):
        return _sign(self._presence, bytes(message))

    def hardware_backed(self):
        return False


@dataclass(frozen=True)
class CallerHolder:
    key: ed25519.Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "CallerHolder":
        require_development()
        return cls(ed25519.Ed25519PrivateKey.generate())

    @property
    def key_text(self) -> str:
        seed = self.key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        return KEY_TEXT + _b64(seed)

    @classmethod
    def from_key_text(cls, text: str) -> "CallerHolder":
        require_development()
        if not text.startswith(KEY_TEXT):
            raise ValueError("not a caller key text form")
        seed = base64.urlsafe_b64decode(text[len(KEY_TEXT):] + "=" * (-len(text[len(KEY_TEXT):]) % 4))
        return cls(ed25519.Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def holder_text(self) -> str:
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return HOLDER_TEXT + _b64(public)


class DevDevice:
    def __init__(self, name: str, data_dir: Path):
        require_development()
        if name not in KINDS:
            raise ValueError(f"{name!r} is not a development mesh device")
        self.name = name
        self.keys = dev_mesh_keys.device_keys(name)
        directory = Path(data_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = ak.KernelConfig(
            data_dir=str(directory),
            operations_secret=self.keys.operations_secret,
            wrap_secret=self.keys.wrap_secret,
            restore_marker=self.keys.restore_marker,
            device_label=name,
            device_kind=KINDS[name],
            network=ak.NetworkConfig(
                relay=ak.RelayChoice.DISABLED(),
                local_discovery=False,
                internet_lookup=False,
                loopback_only=True,
                sync_interval_ms=SYNC_INTERVAL_MS,
                mailbox=None,
            ),
        )
        self.kernel = ak.Kernel.open(config, _Signer(self.keys))

    @property
    def device_id(self) -> str:
        return self.keys.device_id

    async def start(self) -> str:
        require_development()
        return await self.kernel.start_network()

    async def stop(self) -> None:
        await self.kernel.stop_network()

    async def pairing_offer(self) -> str:
        require_development()
        return (await self.kernel.create_pairing_offer()).payload

    async def enrolled(self) -> bool:
        return (await self.kernel.mesh_status()).state == ak.EnrollmentState.ENROLLED


class DevOwner(DevDevice):
    def __init__(self, name: str, data_dir: Path):
        if name not in OWNERS:
            raise ValueError(f"{name!r} is not a development mesh owner")
        super().__init__(name, data_dir)

    @property
    def mesh_id(self) -> str:
        return self.kernel.roster().mesh_id

    def create_mesh(self) -> ak.RecoveryKit:
        require_development()
        return self.kernel.create_mesh()

    def confirm_recovery(self) -> None:
        require_development()
        self.kernel.confirm_mesh_recovery()

    async def approve_offer(self, payload: str) -> ak.PairingApproved:
        require_development()
        return await self.kernel.approve_pairing(payload, ak.HistoryGrant.ALL)

    def send_request(
        self, to_device: str, text: str, *, chat_id: Optional[str] = None, continues: Optional[str] = None,
    ) -> Tuple[str, str]:
        require_development()
        chat = chat_id or self.kernel.create_chat(REQUEST_CHAT_TITLE)
        return chat, self.kernel.send_request(chat, text, to_device, continues, None)

    def mint_caller_ticket(
        self, node_device: str, holder: CallerHolder, tools: Sequence[str], *,
        label: str = "Development caller", a2a: bool = False, lifetime_s: int = DEFAULT_LIFETIME_S,
    ) -> ak.CallerTicket:
        require_development()
        return self.kernel.mint_caller_ticket(node_device, holder.holder_text, label, list(tools), a2a, lifetime_s)

    def sign_holder_proof(
        self, holder: CallerHolder, ticket_id: str, method: str, target: str, body: bytes = b"", *,
        ms: Optional[int] = None, nonce: Optional[bytes] = None,
    ) -> str:
        require_development()
        fields = {
            "v": 1,
            "ticket": ticket_id,
            "method": method,
            "target": target,
            "body": "sha256:" + hashlib.sha256(body).hexdigest(),
            "ms": time.time_ns() // 1_000_000 if ms is None else ms,
            "nonce": _b64(os.urandom(NONCE_BYTES) if nonce is None else nonce),
        }
        unsigned = ak.canonical_json(json.dumps(fields).encode())
        fields["sig"] = _b64(holder.key.sign(PROOF_CONTEXT + unsigned))
        return _b64(ak.canonical_json(json.dumps(fields).encode()))

    def caller_headers(
        self, holder: CallerHolder, ticket: ak.CallerTicket, method: str, target: str, body: bytes = b"",
    ) -> Dict[str, str]:
        if not ticket.text.startswith(TICKET_TEXT):
            raise ValueError("not a caller ticket text form")
        return {
            "Authorization": SCHEME + ticket.text[len(TICKET_TEXT):],
            PROOF_HEADER: self.sign_holder_proof(holder, ticket.id, method, target, body),
        }
