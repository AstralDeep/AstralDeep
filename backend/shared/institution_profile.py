"""Institution node configuration: the institution's P-256 key, the accepted issuers, and the signed statement that approves this node for the institution's PHI, checked offline against that key.
session_store.py refuses to start without it, node_api.py puts the approval in the descriptor, and orchestrator/institution_meshes.py and institution_policy.py build on it.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key

from shared import node_identity

logger = logging.getLogger("shared.institution_profile")

KEY_ENV = "ASTRAL_INSTITUTION_KEY"
APPROVAL_ENV = "ASTRAL_INSTITUTION_NODE_APPROVAL"
CONTEXT = "astral.attestation/v1"
KIND = "phi"
SUBJECT_KINDS = ("device", "node", "endpoint")
FIELDS = frozenset({"institution", "issued_s", "kind", "not_after_s", "sig", "subject", "subject_kind"})
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
EXIT_CONFIG = 78
_MAX_APPROVAL_BYTES = 8192


class InstitutionConfigError(Exception):
    pass


class ApprovalError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64url(value: str) -> bytes:
    if not isinstance(value, str) or not value.isascii():
        raise ApprovalError("attestation.malformed")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise ApprovalError("attestation.malformed") from None


def canonical(value: Any) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def signing_input(statement: Mapping[str, Any]) -> bytes:
    body = {key: item for key, item in statement.items() if key != "sig"}
    return CONTEXT.encode("ascii") + b"\n" + canonical(body)


def public_point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def key_text(key: ec.EllipticCurvePrivateKey) -> str:
    return _b64url(public_point(key))


def sign_phi_approval(
    key: ec.EllipticCurvePrivateKey,
    *,
    subject_kind: str,
    subject: str,
    issued_s: int,
    not_after_s: int,
) -> Dict[str, Any]:
    statement: Dict[str, Any] = {
        "institution": key_text(key),
        "issued_s": issued_s,
        "kind": KIND,
        "not_after_s": not_after_s,
        "subject": subject,
        "subject_kind": subject_kind,
    }
    r, s = decode_dss_signature(key.sign(signing_input(statement), ec.ECDSA(hashes.SHA256())))
    if s > P256_ORDER // 2:
        s = P256_ORDER - s
    statement["sig"] = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return statement


def _valid_point(text: str) -> ec.EllipticCurvePublicKey:
    try:
        raw = _unb64url(text)
    except ApprovalError:
        raise InstitutionConfigError(f"{KEY_ENV} is not base64url") from None
    if len(raw) != 65 or raw[0] != 4:
        raise InstitutionConfigError(f"{KEY_ENV} must be an uncompressed P-256 point in base64url")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise InstitutionConfigError(f"{KEY_ENV} is not a point on P-256") from None


def verify_phi_approval(
    statement: Any,
    institution_key: str,
    *,
    subject_kind: str,
    subject: str,
    now_s: Optional[int] = None,
) -> Dict[str, Any]:
    if not isinstance(statement, dict) or set(statement) != FIELDS or statement["kind"] != KIND:
        raise ApprovalError("attestation.malformed")
    issued, until = statement["issued_s"], statement["not_after_s"]
    if (
        statement["subject_kind"] not in SUBJECT_KINDS
        or not isinstance(statement["subject"], str)
        or not statement["subject"]
        or type(issued) is not int
        or type(until) is not int
        or until <= issued
    ):
        raise ApprovalError("attestation.malformed")
    if statement["institution"] != institution_key:
        raise ApprovalError("attestation.unknown_authority")
    signature = _unb64url(statement["sig"])
    if len(signature) != 64:
        raise ApprovalError("attestation.bad_signature")
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    if not (0 < r < P256_ORDER and 0 < s <= P256_ORDER // 2):
        raise ApprovalError("attestation.bad_signature")
    try:
        _valid_point(institution_key).verify(
            encode_dss_signature(r, s), signing_input(statement), ec.ECDSA(hashes.SHA256())
        )
    except (InvalidSignature, InstitutionConfigError):
        raise ApprovalError("attestation.bad_signature") from None
    if statement["subject_kind"] != subject_kind or statement["subject"] != subject:
        raise ApprovalError("attestation.wrong_subject")
    if now_s is not None and not issued <= now_s <= until:
        raise ApprovalError("attestation.expired")
    return statement


@dataclass(frozen=True)
class InstitutionConfig:
    key: str
    issuers: Tuple[str, ...]
    approval: Optional[Dict[str, Any]] = None

    def current_approval(self, now_s: Optional[int] = None) -> Optional[Dict[str, Any]]:
        statement = self.approval
        if statement is None:
            return None
        moment = int(time.time()) if now_s is None else now_s
        return statement if statement["issued_s"] <= moment <= statement["not_after_s"] else None


def problems(env: Optional[Mapping[str, str]] = None) -> List[str]:
    values = os.environ if env is None else env
    found: List[str] = []
    key = values.get(KEY_ENV, "").strip()
    if not key:
        found.append(f"{KEY_ENV} is unset, so this institution node cannot tell whose PHI approvals to trust.")
    else:
        try:
            _valid_point(key)
        except InstitutionConfigError as exc:
            found.append(str(exc))
    if not node_identity.accepted_issuers():
        found.append(
            "No accepted issuer is configured. Set KEYCLOAK_AUTHORITY or ASTRAL_ACCEPTED_ISSUERS "
            "so the institution node knows whom to trust."
        )
    return found


def load(env: Optional[Mapping[str, str]] = None, *, node_id: Optional[str] = None) -> InstitutionConfig:
    values = os.environ if env is None else env
    found = problems(values)
    if found:
        raise InstitutionConfigError(" ".join(found))
    key = values[KEY_ENV].strip()
    approval: Optional[Dict[str, Any]] = None
    path = values.get(APPROVAL_ENV, "").strip()
    if path:
        try:
            raw = Path(path).expanduser().read_bytes()
            if len(raw) > _MAX_APPROVAL_BYTES:
                raise InstitutionConfigError(f"{APPROVAL_ENV} file is too large")
            statement = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise InstitutionConfigError(f"{APPROVAL_ENV} cannot be read as a signed statement") from exc
        subject = node_id or node_identity.load_node_identity().node_id
        try:
            approval = verify_phi_approval(statement, key, subject_kind="node", subject=subject)
        except ApprovalError as exc:
            raise InstitutionConfigError(f"{APPROVAL_ENV} was refused: {exc.code}") from exc
    return InstitutionConfig(key=key, issuers=tuple(node_identity.accepted_issuers()), approval=approval)


def enforce(env: Optional[Mapping[str, str]] = None) -> InstitutionConfig:
    try:
        return load(env)
    except (InstitutionConfigError, node_identity.NodeIdentityError) as exc:
        logger.critical("REFUSING TO START the institution profile: %s", exc)
        raise SystemExit(EXIT_CONFIG) from None


def _load_signing_key(path: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = load_pem_private_key(Path(path).expanduser().read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise InstitutionConfigError("the institution signing key file cannot be read") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise InstitutionConfigError("the institution signing key must be a P-256 key")
    return key


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shared.institution_profile",
        description="Offline tools for the institution that signs PHI approvals. The signing key never leaves the institution.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    public = commands.add_parser("public-key", help="print the base64url public key to configure on nodes")
    public.add_argument("--key", required=True, help="path to the institution P-256 private key (PEM)")
    sign = commands.add_parser("sign", help="print a signed PHI approval statement as JSON")
    sign.add_argument("--key", required=True)
    sign.add_argument("--kind", choices=SUBJECT_KINDS, required=True)
    sign.add_argument("--subject", required=True, help="a node id, a device id or an endpoint URL")
    sign.add_argument("--days", type=int, default=90, help="how long the approval lasts")
    arguments = parser.parse_args(argv)
    try:
        key = _load_signing_key(arguments.key)
    except InstitutionConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if arguments.command == "public-key":
        print(key_text(key))
        return 0
    if not 1 <= arguments.days <= 3650:
        print("days must be between 1 and 3650", file=sys.stderr)
        return 1
    issued = int(time.time())
    statement = sign_phi_approval(
        key,
        subject_kind=arguments.kind,
        subject=arguments.subject,
        issued_s=issued,
        not_after_s=issued + arguments.days * 86400,
    )
    print(json.dumps(statement, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
