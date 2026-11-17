# The executor's own key material: the kernel's operations and wrap secrets, a restore marker and software P-256 keys.
# mesh.py hands these to the kernel; they are created once in the executor's data directory, readable only by it.

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

KEY_DIR = "keys"
MARKER_BYTES = 32
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
OTHER_CONSENT = "OTHER"
_lock = threading.Lock()


class KeysError(Exception):
    pass


class PresenceRefused(KeysError):
    pass


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        os.chmod(path, 0o700)


def _read_private(path: Path) -> bytes:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise KeysError(f"{path.name} must be a regular file readable only by the executor")
    data = path.read_bytes()
    if not data:
        raise KeysError(f"{path.name} is empty")
    return data


def _load_or_create(path: Path, make: Callable[[], bytes]) -> bytes:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(make())
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return _read_private(path)


def _p256_pem() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _load_p256(pem: bytes, name: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except ValueError as exc:
        raise KeysError(f"{name} is not a valid private key") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise KeysError(f"{name} must be a P-256 key")
    return key


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _raw_signature(key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    if s > P256_ORDER // 2:
        s = P256_ORDER - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


@dataclass(frozen=True)
class ExecutorKeys:
    operations_secret: bytes
    wrap_secret: bytes
    restore_marker: bytes
    identity: ec.EllipticCurvePrivateKey
    presence: ec.EllipticCurvePrivateKey

    @classmethod
    def load_or_create(cls, data_dir: Path, *, new_secret: Callable[[], bytes]) -> ExecutorKeys:
        directory = Path(data_dir) / KEY_DIR
        with _lock:
            _private_dir(Path(data_dir))
            _private_dir(directory)
            operations = _load_or_create(directory / "operations.secret", new_secret)
            wrap = _load_or_create(directory / "wrap.secret", new_secret)
            marker = _load_or_create(directory / "restore.marker", lambda: os.urandom(MARKER_BYTES))
            identity = _load_p256(_load_or_create(directory / "identity.pem", _p256_pem), "identity.pem")
            presence = _load_p256(_load_or_create(directory / "presence.pem", _p256_pem), "presence.pem")
        if len(marker) != MARKER_BYTES:
            raise KeysError("restore marker has the wrong length")
        return cls(operations, wrap, marker, identity, presence)

    def identity_public_key(self) -> bytes:
        return _point(self.identity)

    def identity_sign(self, message: bytes) -> bytes:
        return _raw_signature(self.identity, message)

    def presence_public_key(self) -> bytes:
        return _point(self.presence)

    def presence_sign(self, consent_kind: str, message: bytes) -> bytes:
        if consent_kind != OTHER_CONSENT:
            raise PresenceRefused("an executor never signs an owner consent")
        return _raw_signature(self.presence, message)
