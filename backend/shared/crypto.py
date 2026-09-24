"""ECIES (ECDH P-256 + HKDF-SHA256 + AES-256-GCM) credential encryption so the
orchestrator can store a credential only the target agent's private key can decrypt;
used by orchestrator/credential_manager.py and shared/base_agent.py.
"""

import os
import base64
import logging
from typing import Tuple

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger("Crypto")

E2E_PREFIX = "e2e:"
HKDF_INFO = b"astral-credential-v1"
EC_CURVE = ec.SECP256R1()


def generate_ec_keypair() -> Tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    private_key = ec.generate_private_key(EC_CURVE, default_backend())
    return private_key, private_key.public_key()


def build_jwk(public_key: ec.EllipticCurvePublicKey) -> dict:
    numbers = public_key.public_numbers()
    x_bytes = numbers.x.to_bytes(32, byteorder="big")
    y_bytes = numbers.y.to_bytes(32, byteorder="big")
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": base64.urlsafe_b64encode(x_bytes).rstrip(b"=").decode(),
        "y": base64.urlsafe_b64encode(y_bytes).rstrip(b"=").decode(),
    }


def ec_public_key_from_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    def _pad_b64(s: str) -> str:
        return s + "=" * (-len(s) % 4)

    x_bytes = base64.urlsafe_b64decode(_pad_b64(jwk["x"]))
    y_bytes = base64.urlsafe_b64decode(_pad_b64(jwk["y"]))
    x_int = int.from_bytes(x_bytes, byteorder="big")
    y_int = int.from_bytes(y_bytes, byteorder="big")
    public_numbers = ec.EllipticCurvePublicNumbers(x_int, y_int, EC_CURVE)
    return public_numbers.public_key(default_backend())


def save_private_key(key: ec.EllipticCurvePrivateKey, path: str) -> None:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(pem)
    logger.info(f"Saved agent private key to {path}")


def load_private_key(path: str) -> ec.EllipticCurvePrivateKey:
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def encrypt_for_agent(plaintext: str, agent_public_key: ec.EllipticCurvePublicKey) -> str:
    eph_private = ec.generate_private_key(EC_CURVE, default_backend())
    eph_public = eph_private.public_key()

    shared_secret = eph_private.exchange(ec.ECDH(), agent_public_key)

    salt = os.urandom(16)
    derived_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
        backend=default_backend(),
    ).derive(shared_secret)

    nonce = os.urandom(12)
    aesgcm = AESGCM(derived_key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

    eph_pub_bytes = eph_public.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    payload = eph_pub_bytes + salt + nonce + ciphertext_and_tag
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()

    return E2E_PREFIX + encoded


def decrypt_from_orchestrator(ciphertext: str, agent_private_key: ec.EllipticCurvePrivateKey) -> str:
    if not ciphertext.startswith(E2E_PREFIX):
        raise ValueError("Ciphertext does not have e2e: prefix — not ECIES-encrypted")

    encoded = ciphertext[len(E2E_PREFIX):]
    padded = encoded + "=" * (-len(encoded) % 4)
    raw = base64.urlsafe_b64decode(padded)

    if len(raw) < 65 + 16 + 12 + 16:
        raise ValueError("Ciphertext too short")

    eph_pub_bytes = raw[:65]
    salt = raw[65:81]
    nonce = raw[81:93]
    ciphertext_and_tag = raw[93:]

    eph_public = ec.EllipticCurvePublicKey.from_encoded_point(EC_CURVE, eph_pub_bytes)

    shared_secret = agent_private_key.exchange(ec.ECDH(), eph_public)

    derived_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
        backend=default_backend(),
    ).derive(shared_secret)

    aesgcm = AESGCM(derived_key)
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext_and_tag, None)
    return plaintext_bytes.decode("utf-8")


def is_e2e_encrypted(value: str) -> bool:
    return value.startswith(E2E_PREFIX)
