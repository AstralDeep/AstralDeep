# The caller client's credentials and proofs: the holder key kept in a 0600 caller-key file beside deployment.toml, the
# owner-minted ticket read from a file, and the signed proof each request carries. node.py sends the headers it builds,
# and cli.py exposes `astralvessel caller key`.

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

KEY_FILE = "caller-key"
MAX_HEADER = 8192
MAX_KEY_BYTES = 1024
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
TICKET_FILE_VARIABLE = "ASTRAL_CALLER_TICKET_FILE"
TICKET_TEXT = "astral-caller-ticket:v1:"
KEY_TEXT = "astral-caller-key:v1:"
HOLDER_TEXT = "astral-caller-holder:v1:"
SCHEME = "AstralCaller "
PROOF_HEADER = "Astral-Caller-Proof"
PROOF_CONTEXT = b"astral.caller-proof/v1\n"
# Settings that carried credentials in earlier releases; using one is refused so nobody believes it still works.
REMOVED_ENVIRONMENT = {
    "ASTRAL_OWNER_TOKEN": "owner tokens are gone; the node serves mesh devices and caller tickets",
    "ASTRAL_CALLER_KEY": "the holder key is read only from the caller-key file, created by astralvessel caller key",
    "ASTRAL_CALLER_TICKET": f"the ticket is read from a file: pass --ticket-file or set {TICKET_FILE_VARIABLE}",
}
REMOVED_OPTIONS = {
    "--token-file": "owner tokens are gone; ask the owner for a caller ticket and pass --ticket-file",
    "--key-file": "the holder key is read only from the caller-key file, created by astralvessel caller key",
}


class CallerError(Exception):
    pass


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def holder_text(key: ed25519.Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return HOLDER_TEXT + b64(raw)


class Caller:
    # The ticket is public; the holder key is the secret and is only ever used to sign.
    def __init__(self, ticket_text: str, key_text: str):
        if len(ticket_text) > MAX_HEADER or len(SCHEME) + len(ticket_text) - len(TICKET_TEXT) > MAX_HEADER:
            raise CallerError(f"the caller ticket is longer than the {MAX_HEADER} characters the contract allows")
        try:
            if not ticket_text.startswith(TICKET_TEXT) or not key_text.startswith(KEY_TEXT):
                raise ValueError
            self.ticket_text = ticket_text
            self.ticket_id = json.loads(unb64(ticket_text[len(TICKET_TEXT):]))["id"]
            self._key = ed25519.Ed25519PrivateKey.from_private_bytes(unb64(key_text[len(KEY_TEXT):]))
        except (ValueError, KeyError, TypeError):
            raise CallerError("the caller ticket must be astral-caller-ticket:v1:... and the key "
                              "astral-caller-key:v1:...") from None

    def headers(self, method: str, target: str, body: bytes = b"", *, ms: int | None = None,
                nonce: bytes | None = None) -> dict[str, str]:
        fields: dict[str, Any] = {"v": 1, "ticket": self.ticket_id, "method": method, "target": target,
                                  "body": "sha256:" + hashlib.sha256(body).hexdigest(),
                                  "ms": time.time_ns() // 1_000_000 if ms is None else ms,
                                  "nonce": b64(os.urandom(16) if nonce is None else nonce)}
        fields["sig"] = b64(self._key.sign(PROOF_CONTEXT + canonical(fields)))
        return {"Authorization": SCHEME + self.ticket_text[len(TICKET_TEXT):], PROOF_HEADER: b64(canonical(fields))}


def key_path(directory: Path) -> Path:
    return Path(directory) / KEY_FILE


def create_key(directory: Path) -> str:
    # Created exclusively at 0600; only the public holder form is ever printed.
    path = key_path(directory)
    if path.exists() or path.is_symlink():
        raise CallerError(f"{path} already exists; it is the key your caller tickets were minted for, so it is kept")
    key = ed25519.Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    except FileExistsError:
        raise CallerError(f"{path} already exists; it is the key your caller tickets were minted for, so it is "
                          "kept") from None
    except OSError as exc:
        raise CallerError(f"cannot create the holder key at {path}: {exc.strerror}") from None
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(KEY_TEXT + b64(seed) + "\n")
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise CallerError(f"cannot write the holder key at {path}: {exc.strerror}; nothing was kept") from None
    return holder_text(key)


def read_key(directory: Path) -> str:
    path = key_path(directory)
    try:
        descriptor = os.open(path, os.O_RDONLY | NOFOLLOW)
    except FileNotFoundError:
        raise CallerError(f"there is no holder key at {path}; run astralvessel caller key to create it, then ask the "
                          "owner to mint a caller ticket for the holder key it prints") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise CallerError(f"{path} must be a regular file, not a link or a directory") from None
        raise CallerError(f"cannot read the holder key at {path}: {exc.strerror}") from None
    with os.fdopen(descriptor, "r") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise CallerError(f"{path} must be a regular file, not a link or a directory")
        if status.st_mode & 0o077:
            raise CallerError(f"{path} is readable by group or others (mode {stat.S_IMODE(status.st_mode):04o}); it "
                              f"holds a private key, so run chmod 600 {path}")
        return handle.read(MAX_KEY_BYTES).strip()


def read_ticket(ticket_file: str | None) -> str:
    named = ticket_file or os.environ.get(TICKET_FILE_VARIABLE)
    if not named:
        raise CallerError(f"pass --ticket-file or set {TICKET_FILE_VARIABLE} to a file holding the caller ticket the "
                          "owner minted for this node")
    try:
        with open(Path(named).expanduser(), "rb") as handle:
            raw = handle.read(MAX_HEADER + 1024)
    except FileNotFoundError:
        raise CallerError(f"the ticket file {named} does not exist") from None
    except OSError as exc:
        raise CallerError(f"cannot read the ticket file {named}: {exc.strerror}") from None
    if len(raw) > MAX_HEADER + 512:
        raise CallerError(f"the ticket file {named} is too large to be a caller ticket")
    text = raw.decode("utf-8", errors="replace").strip()
    if "\n" in text or KEY_TEXT in text:
        raise CallerError(f"the ticket file {named} must hold only the ticket on one line; it holds more, "
                          "possibly a private key, so remove everything but the ticket")
    return text


def load_caller(directory: Path, ticket_file: str | None) -> Caller:
    for name, reason in REMOVED_ENVIRONMENT.items():
        if os.environ.get(name):
            raise CallerError(f"{name} is no longer used: {reason}")
    key = read_key(directory)
    return Caller(read_ticket(ticket_file), key)
