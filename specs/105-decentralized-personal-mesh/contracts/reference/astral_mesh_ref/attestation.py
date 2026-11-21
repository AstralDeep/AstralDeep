"""Outside attestations stored in the roster: the identity statement made from a Keycloak ID token and the PHI approval signed by an institution.
The roster module calls these checks while folding entries, and nothing here touches the network.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from . import crypto
from .cj import b64url_decode, strict_loads
from .errors import MeshError

STALE_AFTER_S = 30 * 24 * 3600
TOKEN_SKEW_S = 300
IDENTITY_FIELDS = {"device", "id_token", "issuer", "kind", "mesh", "sig", "subject"}
PHI_FIELDS = {
    "institution",
    "issued_s",
    "kind",
    "not_after_s",
    "sig",
    "subject",
    "subject_kind",
}
SUBJECT_KINDS = ("device", "node", "endpoint")


def nonce_for(identity_public: bytes, mesh_id: str) -> str:
    return hashlib.sha256(identity_public + crypto.id_bytes(mesh_id)).hexdigest()


def _jwt_parts(token: Any) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    if not isinstance(token, str) or token.count(".") != 2:
        raise MeshError("attestation.malformed", "token is not a compact JWS")
    head, payload, signature = token.split(".")
    try:
        header = strict_loads(b64url_decode(head))
        claims = strict_loads(b64url_decode(payload))
        raw_signature = b64url_decode(signature)
    except MeshError as exc:
        raise MeshError("attestation.malformed", "token segments") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise MeshError("attestation.malformed", "token segments")
    return header, claims, (head + "." + payload).encode("ascii"), raw_signature


def _int(text: str) -> int:
    return int.from_bytes(b64url_decode(text), "big")


def _verify_jws(
    alg: str, jwk: dict[str, Any], signing_input: bytes, signature: bytes
) -> bool:
    try:
        if alg == "RS256" and jwk.get("kty") == "RSA":
            key = rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return True
        if alg == "ES256" and jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
            if len(signature) != 64:
                return False
            point = ec.EllipticCurvePublicNumbers(
                _int(jwk["x"]), _int(jwk["y"]), ec.SECP256R1()
            ).public_key()
            r = int.from_bytes(signature[:32], "big")
            s = int.from_bytes(signature[32:], "big")
            point.verify(
                encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
            )
            return True
    except (InvalidSignature, ValueError, KeyError, MeshError):
        return False
    return False


def verify_identity(
    att: Any,
    identity_public: bytes,
    mesh_id: str,
    issuers: dict[str, dict[str, Any]],
    *,
    entry_time_s: int,
    now_s: int | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(att, dict)
        or set(att) != IDENTITY_FIELDS
        or att["kind"] != "identity"
    ):
        raise MeshError("attestation.malformed", "identity fields")
    if att["device"] != crypto.device_id(identity_public) or att["mesh"] != mesh_id:
        raise MeshError("attestation.wrong_device", "device or mesh differs")
    if not crypto.verify_object("p256", identity_public, "astral.attestation/v1", att):
        raise MeshError("attestation.bad_signature", "device signature")
    issuer = issuers.get(att["issuer"])
    if issuer is None:
        raise MeshError("attestation.unknown_issuer", str(att["issuer"]))
    header, claims, signing_input, raw = _jwt_parts(att["id_token"])
    if header.get("alg") not in ("RS256", "ES256"):
        raise MeshError("attestation.bad_alg", str(header.get("alg")))
    jwk = next((k for k in issuer["keys"] if k.get("kid") == header.get("kid")), None)
    if jwk is None:
        raise MeshError("attestation.unknown_key", str(header.get("kid")))
    if not _verify_jws(header["alg"], jwk, signing_input, raw):
        raise MeshError("attestation.bad_token_signature", "token signature")
    if claims.get("iss") != att["issuer"]:
        raise MeshError("attestation.wrong_issuer", str(claims.get("iss")))
    client = claims["azp"] if "azp" in claims else claims.get("aud")
    if not isinstance(client, str) or client not in issuer["clients"]:
        raise MeshError("attestation.wrong_audience", str(client))
    if (
        claims.get("sub") != att["subject"]
        or not isinstance(att["subject"], str)
        or not att["subject"]
    ):
        raise MeshError("attestation.wrong_subject", "sub claim differs")
    if claims.get("nonce") != nonce_for(identity_public, mesh_id):
        raise MeshError(
            "attestation.wrong_nonce", "nonce does not bind this key and mesh"
        )
    iat = claims.get("iat")
    if type(iat) is not int or iat < 0:
        raise MeshError("attestation.malformed", "iat")
    exp = claims.get("exp")
    if type(exp) is not int or exp < 0:
        raise MeshError("attestation.malformed", "exp")
    if entry_time_s + TOKEN_SKEW_S < iat:
        raise MeshError("attestation.token_not_yet_valid", "issued after the entry")
    if entry_time_s - TOKEN_SKEW_S > exp:
        raise MeshError("attestation.token_expired", "expired before the entry")
    stale = now_s is not None and now_s - iat > STALE_AFTER_S
    return {
        "issuer": att["issuer"],
        "subject": att["subject"],
        "iat": iat,
        "stale": stale,
    }


def build_identity(
    identity_scalar: int,
    identity_public: bytes,
    mesh_id: str,
    issuer: str,
    subject: str,
    id_token: str,
) -> dict[str, Any]:
    att: dict[str, Any] = {
        "device": crypto.device_id(identity_public),
        "id_token": id_token,
        "issuer": issuer,
        "kind": "identity",
        "mesh": mesh_id,
        "subject": subject,
    }
    att["sig"] = crypto.sign_object(
        "p256", identity_scalar, "astral.attestation/v1", att
    )
    return att


def build_phi(
    institution_scalar: int,
    institution_public: bytes,
    subject_kind: str,
    subject: str,
    issued_s: int,
    not_after_s: int,
) -> dict[str, Any]:
    att: dict[str, Any] = {
        "institution": crypto.b64url_encode(institution_public),
        "issued_s": issued_s,
        "kind": "phi",
        "not_after_s": not_after_s,
        "subject": subject,
        "subject_kind": subject_kind,
    }
    att["sig"] = crypto.sign_object(
        "p256", institution_scalar, "astral.attestation/v1", att
    )
    return att


def verify_phi(
    att: Any, authorities: set[str], *, now_s: int | None = None
) -> dict[str, Any]:
    if not isinstance(att, dict) or set(att) != PHI_FIELDS or att["kind"] != "phi":
        raise MeshError("attestation.malformed", "phi fields")
    if (
        att["subject_kind"] not in SUBJECT_KINDS
        or not isinstance(att["subject"], str)
        or not att["subject"]
    ):
        raise MeshError("attestation.malformed", "phi subject")
    if (
        type(att["issued_s"]) is not int
        or type(att["not_after_s"]) is not int
        or att["not_after_s"] <= att["issued_s"]
    ):
        raise MeshError("attestation.malformed", "phi validity")
    if att["institution"] not in authorities:
        raise MeshError(
            "attestation.unknown_authority",
            "institution is not a PHI authority of this mesh",
        )
    if not crypto.verify_object(
        "p256", b64url_decode(att["institution"]), "astral.attestation/v1", att
    ):
        raise MeshError("attestation.bad_signature", "institution signature")
    if now_s is not None and not att["issued_s"] <= now_s <= att["not_after_s"]:
        raise MeshError("attestation.expired", "outside the validity window")
    return {
        "institution": att["institution"],
        "subject": att["subject"],
        "not_after_s": att["not_after_s"],
    }
