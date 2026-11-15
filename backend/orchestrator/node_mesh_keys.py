"""Key material of a node's mesh peer: the kernel's operations and wrap secrets, a restore marker and the software P-256 identity and presence keys, generated once into the node data directory at mode 0600.
node_mesh.py hands these to the kernel and signs through them, and a node has no secure enclave, so the signer reports software custody and never gives owner consent.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

MESH_DIR = "mesh"
OPERATIONS_FILE = "operations.secret"
WRAP_FILE = "wrap.secret"
MARKER_FILE = "restore.marker"
IDENTITY_FILE = "identity.pem"
PRESENCE_FILE = "presence.pem"
MARKER_BYTES = 32
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

OTHER_CONSENT_KIND = "OTHER"

_lock = threading.Lock()


class NodeMeshKeyError(Exception):
    pass


class PresenceRefused(NodeMeshKeyError):
    pass


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        os.chmod(path, 0o700)


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_private(path: Path) -> bytes:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise NodeMeshKeyError(f"{path.name} must be a regular file readable only by the node")
    data = path.read_bytes()
    if not data:
        raise NodeMeshKeyError(f"{path.name} is empty")
    return data


def _load_or_create(path: Path, make: Callable[[], bytes]) -> bytes:
    if path.exists():
        return _read_private(path)
    _write_new(path, make())
    return _read_private(path)


def _p256_pem() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _load_p256(pem: bytes, name: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except ValueError as exc:
        raise NodeMeshKeyError(f"{name} is not a valid private key") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise NodeMeshKeyError(f"{name} must be a P-256 key")
    return key


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _raw_signature(key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    if s > P256_ORDER // 2:
        s = P256_ORDER - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


@dataclass(frozen=True)
class NodeMeshKeys:
    operations_secret: bytes
    wrap_secret: bytes
    restore_marker: bytes
    identity: ec.EllipticCurvePrivateKey
    presence: ec.EllipticCurvePrivateKey

    @classmethod
    def load_or_create(cls, data_dir: Path, *, new_secret: Callable[[], bytes]) -> "NodeMeshKeys":
        directory = Path(data_dir) / MESH_DIR
        with _lock:
            _private_dir(Path(data_dir))
            _private_dir(directory)
            operations = _load_or_create(directory / OPERATIONS_FILE, new_secret)
            wrap = _load_or_create(directory / WRAP_FILE, new_secret)
            marker = _load_or_create(directory / MARKER_FILE, lambda: os.urandom(MARKER_BYTES))
            identity = _load_p256(_load_or_create(directory / IDENTITY_FILE, _p256_pem), IDENTITY_FILE)
            presence = _load_p256(_load_or_create(directory / PRESENCE_FILE, _p256_pem), PRESENCE_FILE)
        if len(marker) != MARKER_BYTES:
            raise NodeMeshKeyError("restore marker has the wrong length")
        return cls(operations, wrap, marker, identity, presence)

    def identity_public_key(self) -> bytes:
        return _point(self.identity)

    def identity_sign(self, message: bytes) -> bytes:
        return _raw_signature(self.identity, message)

    def presence_public_key(self) -> bytes:
        return _point(self.presence)

    def presence_sign(self, consent_kind: str, message: bytes) -> bytes:
        if consent_kind != OTHER_CONSENT_KIND:
            raise PresenceRefused("a node never signs an owner consent")
        return _raw_signature(self.presence, message)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.identity_public_key()).hexdigest()[:32]

    @property
    def hardware_backed(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"NodeMeshKeys(fingerprint={self.fingerprint()})"
