"""Hashes, ids, domain-separated signatures, X25519 wrapping and AEAD sealing shared by all formats.
Signature contexts and key types are fixed here, and every other module calls these functions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

import nacl.bindings as nb
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, x25519
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .cj import b64url_decode, b64url_encode, canonical
from .errors import MeshError

CONTEXTS = (
    "astral.event/v1",
    "astral.roster/v1",
    "astral.checkpoint/v1",
    "astral.consent/v1",
    "astral.policy/v1",
    "astral.attestation/v1",
    "astral.pairing/v1",
)
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
HALF_ORDER = P256_ORDER // 2
NONCE_LEN = 24
KEY_LEN = 32
TAG_LEN = 16


def sha256_id(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def id_bytes(identifier: str) -> bytes:
    if not isinstance(identifier, str) or not identifier.startswith("sha256:"):
        raise MeshError("id.malformed", str(identifier))
    digits = identifier[7:]
    if len(digits) != 64 or digits != digits.lower():
        raise MeshError("id.malformed", identifier)
    try:
        return bytes.fromhex(digits)
    except ValueError as exc:
        raise MeshError("id.malformed", identifier) from exc


def is_id(value: Any) -> bool:
    try:
        id_bytes(value)
    except MeshError:
        return False
    return True


def object_id(obj: Any) -> str:
    return sha256_id(canonical(obj))


def signing_input(
    context: str, obj: dict[str, Any], exclude: Iterable[str] = ("sig",)
) -> bytes:
    if context not in CONTEXTS:
        raise MeshError("sig.unknown_context", context)
    skip = set(exclude)
    return (
        context.encode("ascii")
        + b"\n"
        + canonical({k: v for k, v in obj.items() if k not in skip})
    )


def ed25519_public(seed: bytes) -> bytes:
    return (
        ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def p256_private(scalar: int) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(scalar, ec.SECP256R1())


def p256_public(scalar: int) -> bytes:
    return (
        p256_private(scalar)
        .public_key()
        .public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )


def p256_sign(scalar: int, message: bytes) -> bytes:
    der = p256_private(scalar).sign(
        message, ec.ECDSA(hashes.SHA256(), deterministic_signing=True)
    )
    r, s = decode_dss_signature(der)
    if s > HALF_ORDER:
        s = P256_ORDER - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def p256_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 65 or public[0] != 4 or len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (0 < r < P256_ORDER and 0 < s <= HALF_ORDER):
        return False
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def sign_object(
    alg: str,
    secret: Any,
    context: str,
    obj: dict[str, Any],
    exclude: Iterable[str] = ("sig",),
) -> str:
    message = signing_input(context, obj, exclude)
    raw = (
        ed25519_sign(secret, message)
        if alg == "ed25519"
        else p256_sign(secret, message)
    )
    return b64url_encode(raw)


def verify_object(
    alg: str,
    public: bytes,
    context: str,
    obj: dict[str, Any],
    field: str = "sig",
    exclude: Iterable[str] = ("sig",),
) -> bool:
    signature = obj.get(field)
    if not isinstance(signature, str):
        return False
    try:
        raw = b64url_decode(signature)
    except MeshError:
        return False
    message = signing_input(context, obj, exclude)
    return (
        ed25519_verify(public, message, raw)
        if alg == "ed25519"
        else p256_verify(public, message, raw)
    )


def sign_detached(alg: str, secret: Any, context: str, payload: dict[str, Any]) -> str:
    return sign_object(alg, secret, context, payload, exclude=())


def verify_detached(
    alg: str, public: bytes, context: str, payload: dict[str, Any], signature: Any
) -> bool:
    return verify_object(
        alg, public, context, {**payload, "sig": signature}, "sig", exclude=("sig",)
    )


def x25519_public(private: bytes) -> bytes:
    return (
        x25519.X25519PrivateKey.from_private_bytes(private)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def x25519_shared(private: bytes, public: bytes) -> bytes:
    try:
        return x25519.X25519PrivateKey.from_private_bytes(private).exchange(
            x25519.X25519PublicKey.from_public_bytes(public)
        )
    except ValueError as exc:
        raise MeshError("wrap.bad_key", "unusable X25519 key") from exc


def hkdf(
    secret: bytes, info: bytes, salt: bytes | None = None, length: int = 32
) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(
        secret
    )


def aead_seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_LEN or len(nonce) != NONCE_LEN:
        raise MeshError("aead.bad_input", "key or nonce length")
    return nb.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)


def aead_open(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_LEN or len(nonce) != NONCE_LEN or len(ciphertext) < TAG_LEN:
        raise MeshError("aead.bad_input", "key, nonce or ciphertext length")
    try:
        return nb.crypto_aead_xchacha20poly1305_ietf_decrypt(
            ciphertext, aad, nonce, key
        )
    except Exception as exc:
        raise MeshError("aead.decrypt_failed", "authentication failed") from exc


def device_id(identity_public: bytes) -> str:
    return sha256_id(identity_public)


def wrap_key(
    recipient_public: bytes,
    secret: bytes,
    aad_obj: dict[str, Any],
    ephemeral: bytes,
    nonce: bytes,
) -> dict[str, str]:
    epk = x25519_public(ephemeral)
    key = hkdf(
        x25519_shared(ephemeral, recipient_public),
        b"astral.wrap/v1",
        salt=epk + recipient_public,
    )
    sealed = aead_seal(key, nonce, secret, canonical(aad_obj))
    return {
        "epk": b64url_encode(epk),
        "nonce": b64url_encode(nonce),
        "ct": b64url_encode(sealed),
    }


def unwrap_key(
    recipient_private: bytes, wrapped: dict[str, str], aad_obj: dict[str, Any]
) -> bytes:
    try:
        epk = b64url_decode(wrapped["epk"])
        nonce = b64url_decode(wrapped["nonce"])
        sealed = b64url_decode(wrapped["ct"])
    except (KeyError, TypeError) as exc:
        raise MeshError("wrap.malformed", "missing field") from exc
    recipient_public = x25519_public(recipient_private)
    key = hkdf(
        x25519_shared(recipient_private, epk),
        b"astral.wrap/v1",
        salt=epk + recipient_public,
    )
    try:
        return aead_open(key, nonce, sealed, canonical(aad_obj))
    except MeshError as exc:
        raise MeshError("wrap.decrypt_failed", "wrong key or associated data") from exc
