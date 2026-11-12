"""Signs a node's published-capability list and its A2A agent card with the node's Ed25519 key.
a2a_orchestrator_executor.py calls it when it builds the public card, so peers can verify exactly which capabilities the owner opted in to.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

from a2a.types import AgentCard, AgentCardSignature, AgentExtension
from a2a.utils.signing import _canonicalize_agent_card
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from google.protobuf.struct_pb2 import Struct

logger = logging.getLogger("Orchestrator.A2APublicationSigning")

LIST_FORMAT = "astral.publication/v1"
EXTENSION_URI = "https://astraldeep.org/a2a/extensions/publication/v1"
PROFILE_ENV = "ASTRAL_NODE_PROFILE"
DATA_DIR_ENV = "ASTRAL_NODE_DATA_DIR"
KEY_FILE = "node-ed25519.pem"
_PERSONAL = "personal"


class PublicationSigningError(Exception):
    pass


class PublicationSigner(Protocol):
    key_id: str

    def public_jwk(self) -> dict[str, str]: ...

    def sign(self, data: bytes) -> bytes: ...


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class NodeKeySigner:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._private = private_key
        raw = _raw_public(private_key.public_key())
        self._x = _b64(raw)
        self.key_id = hashlib.sha256(raw).hexdigest()[:32]

    def public_jwk(self) -> dict[str, str]:
        return {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig", "kid": self.key_id, "x": self._x}

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)


def is_personal_profile() -> bool:
    return os.getenv(PROFILE_ENV, "").strip().lower() == _PERSONAL


def key_path() -> Path:
    configured = os.getenv(DATA_DIR_ENV, "").strip()
    directory = Path(configured).expanduser() if configured else Path(__file__).resolve().parent.parent / "data" / "node"
    return directory / KEY_FILE


def load_node_signer() -> NodeKeySigner:
    path = key_path()
    try:
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PublicationSigningError("The node signing key file must be readable by its owner only.")
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except FileNotFoundError:
        raise PublicationSigningError("The node signing key is missing.") from None
    except (OSError, ValueError, TypeError):
        raise PublicationSigningError("The node signing key file is unreadable.") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise PublicationSigningError("The node signing key must be Ed25519.")
    return NodeKeySigner(key)


def publication_list(records: Iterable[Any]) -> list[dict[str, Any]]:
    entries = [{"capability": record.agent_id, "audiences": sorted(record.audiences),
                "expires_at": record.expires_at, "publication_id": record.publication_id}
               for record in records]
    return sorted(entries, key=lambda entry: (entry["capability"], entry["publication_id"]))


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_publication_list(entries: list[dict[str, Any]], signer: PublicationSigner) -> str:
    header = _b64(_canonical({"alg": "EdDSA", "kid": signer.key_id, "typ": LIST_FORMAT}))
    payload = _b64(_canonical({"format": LIST_FORMAT, "node": signer.key_id, "publications": entries}))
    return f"{header}.{payload}.{_b64(signer.sign(f'{header}.{payload}'.encode()))}"


def verify_publication_list(token: Any, public_jwk: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        header_part, payload_part, signature_part = str(token).split(".")
        header = json.loads(_unb64(header_part))
        if (header.get("alg") != "EdDSA" or header.get("typ") != LIST_FORMAT
                or public_jwk.get("kty") != "OKP" or public_jwk.get("crv") != "Ed25519"):
            raise ValueError
        key = Ed25519PublicKey.from_public_bytes(_unb64(str(public_jwk["x"])))
        key.verify(_unb64(signature_part), f"{header_part}.{payload_part}".encode())
        document = json.loads(_unb64(payload_part))
        if document.get("format") != LIST_FORMAT or type(document.get("publications")) is not list:
            raise ValueError
        return document["publications"]
    except Exception:  # noqa: BLE001
        raise PublicationSigningError("The publication list is not signed by this node key.") from None


def sign_card(card: AgentCard, entries: list[dict[str, Any]], signer: PublicationSigner) -> AgentCard:
    params = Struct()
    params.update({"list": sign_publication_list(entries, signer)})
    card.capabilities.extensions.append(AgentExtension(
        uri=EXTENSION_URI, description="Capabilities this node's owner chose to publish, signed by the node key.",
        required=False, params=params))
    header = {"alg": "EdDSA", "kid": signer.key_id, "typ": "JOSE"}
    protected = _b64(_canonical(header))
    payload = _b64(_canonicalize_agent_card(card).encode("utf-8"))
    signature = signer.sign(f"{protected}.{payload}".encode())
    card.signatures.append(AgentCardSignature(protected=protected, signature=_b64(signature)))
    return card


def signer_for(orchestrator: Any) -> Optional[PublicationSigner]:
    existing = getattr(orchestrator, "publication_signer", None)
    if existing is not None:
        return existing
    if not is_personal_profile():
        return None
    signer = load_node_signer()
    orchestrator.publication_signer = signer
    return signer


def can_publish(orchestrator: Any) -> bool:
    try:
        return signer_for(orchestrator) is not None or not is_personal_profile()
    except PublicationSigningError:
        return False
