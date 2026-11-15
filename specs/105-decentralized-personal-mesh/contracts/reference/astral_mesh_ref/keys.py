"""Key groups and epochs, wrapping of epoch keys to devices and to the recovery key, the 24-word recovery encoding and the sealed backup file.
Roster entries carry the wraps built here, and event.py uses the resulting keys to seal headers and bodies.
"""

from __future__ import annotations

import hashlib
from importlib import resources
from typing import Any

from . import crypto
from .cj import b64url_decode, b64url_encode, canonical, strict_loads
from .errors import MeshError

WORDS_PER_KEY = 24
BACKUP_FORMAT = "astral.backup/v1"


def wrap_aad(group: str, recipient: str) -> dict[str, str]:
    return {"group": group, "purpose": "epoch", "recipient": recipient}


def make_epoch_body(
    group: str,
    epoch_key: bytes,
    members: dict[str, bytes],
    recovery_wrap_public: bytes | None,
    rng: Any,
) -> dict[str, Any]:
    wraps = []
    for device in sorted(members):
        ephemeral, nonce = rng.bytes(32), rng.bytes(crypto.NONCE_LEN)
        wraps.append(
            {
                "device": device,
                "wrap": crypto.wrap_key(
                    members[device],
                    epoch_key,
                    wrap_aad(group, device),
                    ephemeral,
                    nonce,
                ),
            }
        )
    recovery = None
    if recovery_wrap_public is not None:
        ephemeral, nonce = rng.bytes(32), rng.bytes(crypto.NONCE_LEN)
        recovery = crypto.wrap_key(
            recovery_wrap_public,
            epoch_key,
            wrap_aad(group, "recovery"),
            ephemeral,
            nonce,
        )
    return {"group": group, "recovery_wrap": recovery, "wraps": wraps}


def make_wrap_grants(
    group: str,
    device: str,
    wrap_public: bytes,
    keys_by_epoch: dict[str, bytes],
    rng: Any,
) -> list[dict[str, Any]]:
    grants = []
    for epoch in sorted(keys_by_epoch):
        ephemeral, nonce = rng.bytes(32), rng.bytes(crypto.NONCE_LEN)
        grants.append(
            {
                "epoch": epoch,
                "wrap": crypto.wrap_key(
                    wrap_public,
                    keys_by_epoch[epoch],
                    wrap_aad(group, device),
                    ephemeral,
                    nonce,
                ),
            }
        )
    return grants


def open_epoch_key(body: dict[str, Any], device: str, wrap_private: bytes) -> bytes:
    for item in body["wraps"]:
        if item["device"] == device:
            return crypto.unwrap_key(
                wrap_private, item["wrap"], wrap_aad(body["group"], device)
            )
    raise MeshError("wrap.not_a_recipient", device)


def open_recovery_key(body: dict[str, Any], recovery_wrap_private: bytes) -> bytes:
    if body["recovery_wrap"] is None:
        raise MeshError("wrap.not_a_recipient", "recovery")
    return crypto.unwrap_key(
        recovery_wrap_private,
        body["recovery_wrap"],
        wrap_aad(body["group"], "recovery"),
    )


def _wordlist() -> list[str]:
    text = (
        resources.files(__package__)
        .joinpath("wordlist_en.txt")
        .read_text(encoding="ascii")
    )
    words = text.split()
    if len(words) != 2048:
        raise MeshError("recovery.wordlist", "word list must hold 2048 words")
    return words


def encode_words(secret: bytes) -> str:
    if len(secret) != 32:
        raise MeshError("recovery.bad_length", "secret must be 32 bytes")
    words = _wordlist()
    bits = int.from_bytes(secret, "big") << 8 | hashlib.sha256(secret).digest()[0]
    return " ".join(
        words[(bits >> (11 * (WORDS_PER_KEY - 1 - i))) & 0x7FF]
        for i in range(WORDS_PER_KEY)
    )


def decode_words(text: str) -> bytes:
    words = _wordlist()
    index = {w: i for i, w in enumerate(words)}
    parts = text.strip().lower().split()
    if len(parts) != WORDS_PER_KEY:
        raise MeshError("recovery.bad_length", f"{len(parts)} words")
    bits = 0
    for part in parts:
        if part not in index:
            raise MeshError("recovery.unknown_word", part)
        bits = bits << 11 | index[part]
    secret = (bits >> 8).to_bytes(32, "big")
    if hashlib.sha256(secret).digest()[0] != bits & 0xFF:
        raise MeshError("recovery.bad_checksum", "checksum byte differs")
    return secret


def derive_recovery(secret: bytes) -> dict[str, bytes]:
    if len(secret) != 32:
        raise MeshError("recovery.bad_length", "secret must be 32 bytes")
    sign_seed = crypto.hkdf(secret, b"astral.recovery.sign/v1")
    wrap_private = crypto.hkdf(secret, b"astral.recovery.wrap/v1")
    return {
        "sign_seed": sign_seed,
        "sign_public": crypto.ed25519_public(sign_seed),
        "wrap_private": wrap_private,
        "wrap_public": crypto.x25519_public(wrap_private),
    }


def seal_backup(
    recovery_wrap_public: bytes,
    mesh: str,
    payload: dict[str, Any],
    ephemeral: bytes,
    nonce: bytes,
) -> dict[str, str]:
    epk = crypto.x25519_public(ephemeral)
    key = crypto.hkdf(
        crypto.x25519_shared(ephemeral, recovery_wrap_public),
        b"astral.backup/v1",
        salt=epk + recovery_wrap_public,
    )
    aad = canonical({"format": BACKUP_FORMAT, "mesh": mesh})
    return {
        "ct": b64url_encode(crypto.aead_seal(key, nonce, canonical(payload), aad)),
        "epk": b64url_encode(epk),
        "format": BACKUP_FORMAT,
        "mesh": mesh,
        "nonce": b64url_encode(nonce),
    }


def open_backup(recovery_wrap_private: bytes, backup: Any) -> dict[str, Any]:
    if (
        not isinstance(backup, dict)
        or set(backup) != {"ct", "epk", "format", "mesh", "nonce"}
        or backup["format"] != BACKUP_FORMAT
    ):
        raise MeshError("backup.malformed", "fields or format")
    if not crypto.is_id(backup["mesh"]):
        raise MeshError("backup.malformed", "mesh")
    epk = b64url_decode(backup["epk"])
    recipient = crypto.x25519_public(recovery_wrap_private)
    key = crypto.hkdf(
        crypto.x25519_shared(recovery_wrap_private, epk),
        b"astral.backup/v1",
        salt=epk + recipient,
    )
    aad = canonical({"format": BACKUP_FORMAT, "mesh": backup["mesh"]})
    try:
        plain = crypto.aead_open(
            key, b64url_decode(backup["nonce"]), b64url_decode(backup["ct"]), aad
        )
    except MeshError as exc:
        raise MeshError(
            "backup.decrypt_failed", "wrong recovery key or edited file"
        ) from exc
    payload = strict_loads(plain)
    if not isinstance(payload, dict) or set(payload) != {"events", "roster"}:
        raise MeshError("backup.malformed", "payload fields")
    return payload
