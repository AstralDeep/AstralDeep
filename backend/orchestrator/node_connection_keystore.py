"""The node's host keystore for the kernel's connection credentials: each item is a Fernet-encrypted file in the node data directory, keyed by a secret derived from the node's mesh wrap secret, written at mode 0600 and bound to its account name.
node_mesh.py installs it on the kernel it opens, and a host error or an empty value refuses the call instead of ever being read as a credential.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEYSTORE_DIR = "connection-keystore"
KEY_INFO = b"astral.node.connection-keystore/v1"
MAX_ACCOUNT = 256
MAX_SECRET = 65536


class KeystoreFailure(Exception):
    pass


def _fernet(wrap_secret: bytes) -> Fernet:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=KEY_INFO).derive(bytes(wrap_secret))
    return Fernet(base64.urlsafe_b64encode(derived))


class NodeKeystore:
    def __init__(self, directory: Path, wrap_secret: bytes):
        self._directory = Path(directory) / KEYSTORE_DIR
        self._fernet = _fernet(wrap_secret)

    def _path(self, account: str) -> Path:
        if type(account) is not str or not account or len(account) > MAX_ACCOUNT:
            raise KeystoreFailure("account")
        return self._directory / (hashlib.sha256(account.encode()).hexdigest() + ".item")

    def _ready(self) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(self._directory.stat().st_mode) & 0o077:
            os.chmod(self._directory, 0o700)

    def read(self, account: str) -> Optional[bytes]:
        path = self._path(account)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise KeystoreFailure("item")
        try:
            document = json.loads(self._fernet.decrypt(path.read_bytes()))
            value = base64.b64decode(document["value"], validate=True)
            bound = document["account"]
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            raise KeystoreFailure("item") from exc
        if bound != account or not value:
            raise KeystoreFailure("item")
        return value

    def write(self, account: str, secret: bytes) -> None:
        value = bytes(secret)
        if not value or len(value) > MAX_SECRET:
            raise KeystoreFailure("value")
        path = self._path(account)
        self._ready()
        sealed = self._fernet.encrypt(
            json.dumps({"account": account, "value": base64.b64encode(value).decode()}).encode()
        )
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(sealed)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def delete(self, account: str) -> None:
        self._path(account).unlink(missing_ok=True)

    def accounts_present(self) -> int:
        try:
            return sum(1 for entry in self._directory.iterdir() if entry.suffix == ".item")
        except FileNotFoundError:
            return 0


def host_keystore(binding: Any, store: NodeKeystore) -> Any:
    failure = binding.HostKeystoreError

    class Host(binding.HostKeystore):  # type: ignore[name-defined, misc]
        def get(self, account):
            try:
                return store.read(account)
            except (KeystoreFailure, OSError) as exc:
                raise failure.Failed(type(exc).__name__) from exc

        def put(self, account, secret):
            try:
                store.write(account, secret)
            except (KeystoreFailure, OSError) as exc:
                raise failure.Failed(type(exc).__name__) from exc

        def remove(self, account):
            try:
                store.delete(account)
            except (KeystoreFailure, OSError) as exc:
                raise failure.Failed(type(exc).__name__) from exc

    return Host()
