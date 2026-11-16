"""Personal-node identity: the profile switch, the node signing key, issuer owner ids for server profiles, and single-owner custody of the verified mesh id, stored as mesh.<hex> because storage paths refuse the kernel's sha256: form.
shared/token_verify.py, orchestrator/delegation.py, node_mesh.py and node_api.py build on it; it never imports orchestrator code.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import tempfile
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger("shared.node_identity")

PROFILE_ENV = "ASTRAL_NODE_PROFILE"
DATA_DIR_ENV = "ASTRAL_NODE_DATA_DIR"
HOST_ENV = "ORCHESTRATOR_HOST"
SERVER_HOST = "0.0.0.0"
PERSONAL_HOST = "127.0.0.1"
OWNER_ENV = "ASTRAL_NODE_OWNER"
ISSUERS_ENV = "ASTRAL_ACCEPTED_ISSUERS"
PERSONAL = "personal"
INSTITUTION = "institution"

KEY_FILE = "node-ed25519.pem"
OWNER_FILE = "owner"
NODE_ID_PREFIX = "astral-node:"
MAX_TOKEN_SECONDS = 300
CLOCK_SKEW_SECONDS = 30
_MAX_TOKEN_CHARS = 16384
MESH_ID_PREFIX = "sha256:"
MESH_OWNER_PREFIX = "mesh."
MESH_ID = re.compile(r"sha256:[0-9a-f]{64}")
MESH_OWNER_ID = re.compile(r"mesh\.[0-9a-f]{64}")
MESH_OWNER = re.compile(r"(?:sha256:|mesh\.)[0-9a-f]{64}")
_SAFE_OWNER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-"
)


class NodeIdentityError(Exception):
    pass


class NodeKeyError(NodeIdentityError):
    pass


class NodeTokenError(NodeIdentityError):
    pass


class OwnerMismatch(NodeIdentityError):
    pass


class LegacyOwner(OwnerMismatch):
    pass


def is_personal() -> bool:
    return os.getenv(PROFILE_ENV, "").strip().lower() == PERSONAL


def is_institution() -> bool:
    return os.getenv(PROFILE_ENV, "").strip().lower() == INSTITUTION


def bind_host() -> str:
    configured = os.getenv(HOST_ENV, "").strip()
    if not configured:
        return PERSONAL_HOST if is_personal() else SERVER_HOST
    if configured != "localhost":
        try:
            ipaddress.ip_address(configured)
        except ValueError:
            raise NodeIdentityError(f"{HOST_ENV} must be an IP address or localhost") from None
    return configured


def data_dir() -> Path:
    configured = os.getenv(DATA_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "node"


def normalize_issuer(value: Any) -> str:
    return value.strip().rstrip("/") if isinstance(value, str) else ""


def accepted_issuers(extra: Iterable[str] = ()) -> List[str]:
    found: List[str] = []
    candidates = [os.getenv("KEYCLOAK_AUTHORITY", ""), *extra]
    candidates += os.getenv(ISSUERS_ENV, "").split(",")
    for raw in candidates:
        issuer = normalize_issuer(raw)
        if issuer and issuer not in found:
            found.append(issuer)
    return found


def issuer_digest(issuer: str) -> str:
    return hashlib.sha256(normalize_issuer(issuer).encode()).hexdigest()[:16]


def canonical_owner_id(issuer: Any, subject: Any) -> str:
    normalized = normalize_issuer(issuer)
    if not normalized or not isinstance(subject, str) or not subject or subject != subject.strip():
        raise NodeIdentityError("issuer and subject are required")
    owner = f"oidc.{issuer_digest(normalized)}.{subject}"
    if len(owner) > 255 or not set(owner) <= _SAFE_OWNER_CHARS:
        raise NodeIdentityError("subject cannot form a storable owner id")
    return owner


def is_mesh_owner(value: Any) -> bool:
    return type(value) is str and MESH_OWNER.fullmatch(value) is not None


def owner_id_for_mesh(mesh_id: Any) -> str:
    if type(mesh_id) is not str or MESH_ID.fullmatch(mesh_id) is None:
        raise NodeIdentityError("a kernel mesh id is required")
    return MESH_OWNER_PREFIX + mesh_id[len(MESH_ID_PREFIX):]


def mesh_id_for_owner(owner_id: Any) -> str:
    if type(owner_id) is not str or MESH_OWNER_ID.fullmatch(owner_id) is None:
        raise NodeIdentityError("a mesh owner id is required")
    return MESH_ID_PREFIX + owner_id[len(MESH_OWNER_PREFIX):]


def _stored_owner(value: str) -> str:
    return owner_id_for_mesh(value) if MESH_ID.fullmatch(value) else value


def owner_principal_hash(owner_id: str) -> str:
    return "sha256:" + hashlib.sha256(owner_id.encode()).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64url(value: str) -> bytes:
    if not isinstance(value, str) or not value.isascii():
        raise NodeTokenError("malformed segment")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass(frozen=True)
class NodeIdentity:
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    node_id: str
    kid: str

    @classmethod
    def from_private(cls, private_key: Ed25519PrivateKey) -> "NodeIdentity":
        public = private_key.public_key()
        digest = hashlib.sha256(_raw_public(public)).hexdigest()
        return cls(private_key, public, NODE_ID_PREFIX + digest[:32], digest[:32])

    def public_jwk(self) -> Dict[str, str]:
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "alg": "EdDSA",
            "use": "sig",
            "kid": self.kid,
            "x": _b64url(_raw_public(self.public_key)),
        }

    def jwks(self) -> Dict[str, Any]:
        return {"keys": [self.public_jwk()]}

    def derive_secret(self, info: str) -> bytes:
        seed = self.private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=info.encode()
        ).derive(seed)

    def sign_token(self, payload: Dict[str, Any]) -> str:
        header = {"alg": "EdDSA", "typ": "JWT", "kid": self.kid}
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        )
        return signing_input + "." + _b64url(self.private_key.sign(signing_input.encode()))

    def mint_token(
        self,
        claims: Dict[str, Any],
        *,
        audience: Iterable[str] = (),
        lifetime: int = MAX_TOKEN_SECONDS,
        now: Optional[int] = None,
    ) -> str:
        issued = int(now if now is not None else time.time())
        ttl = max(1, min(int(lifetime), MAX_TOKEN_SECONDS))
        aud: List[str] = []
        for value in [*audience, self.node_id]:
            if isinstance(value, str) and value and value not in aud:
                aud.append(value)
        payload = {k: v for k, v in claims.items() if k not in {"iss", "aud", "iat", "exp"}}
        payload.update({"iss": self.node_id, "aud": aud, "iat": issued, "exp": issued + ttl})
        return self.sign_token(payload)

    def verify_token(
        self,
        token: str,
        *,
        audience: Optional[str] = None,
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        header, payload, signing_input, signature = _split(token)
        if header.get("alg") != "EdDSA" or header.get("kid") != self.kid:
            raise NodeTokenError("unsupported algorithm or key")
        try:
            self.public_key.verify(signature, signing_input)
        except InvalidSignature:
            raise NodeTokenError("signature invalid") from None
        if payload.get("iss") != self.node_id:
            raise NodeTokenError("issuer is not this node")
        aud = payload.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self.node_id not in audiences:
            raise NodeTokenError("token was not issued for this node")
        if audience is not None and audience not in audiences:
            raise NodeTokenError("audience mismatch")
        iat, exp = payload.get("iat"), payload.get("exp")
        if type(iat) is not int or type(exp) is not int:
            raise NodeTokenError("iat and exp are required")
        current = int(now if now is not None else time.time())
        if exp <= iat or exp - iat > MAX_TOKEN_SECONDS:
            raise NodeTokenError("lifetime out of bounds")
        if iat > current + CLOCK_SKEW_SECONDS or current >= exp + CLOCK_SKEW_SECONDS:
            raise NodeTokenError("token is not current")
        return payload


def _split(token: str):
    if not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN_CHARS or token.count(".") != 2:
        raise NodeTokenError("malformed token")
    try:
        head, body, sig = token.split(".")
        header = json.loads(_unb64url(head))
        payload = json.loads(_unb64url(body))
        signature = _unb64url(sig)
    except (ValueError, UnicodeDecodeError) as exc:
        raise NodeTokenError("malformed token") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise NodeTokenError("malformed token")
    return header, payload, (head + "." + body).encode(), signature


def peek_algorithm(token: Any) -> Optional[str]:
    try:
        alg = _split(token)[0].get("alg")
    except NodeTokenError:
        return None
    return alg if isinstance(alg, str) else None


_lock = threading.Lock()
_cached: Dict[str, NodeIdentity] = {}


def _ensure_private_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        directory.chmod(0o700)


def _read_key(path: Path) -> Ed25519PrivateKey:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise NodeKeyError("node signing key file must be readable by its owner only")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise NodeKeyError("node signing key file is unreadable") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise NodeKeyError("node signing key must be Ed25519")
    return key


def load_node_identity(*, create: bool = True) -> NodeIdentity:
    directory = data_dir()
    path = directory / KEY_FILE
    cache_key = str(path)
    with _lock:
        existing = _cached.get(cache_key)
        if existing is not None:
            return existing
        if path.exists():
            key = _read_key(path)
        elif not create:
            raise NodeKeyError("node signing key is missing")
        else:
            try:
                _ensure_private_dir(directory)
                key = Ed25519PrivateKey.generate()
                pem = key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = _read_key(path)
            except OSError as exc:
                raise NodeKeyError("node data directory is not writable") from exc
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(pem)
                logger.info("generated a new node signing key")
        identity = NodeIdentity.from_private(key)
        _cached[cache_key] = identity
        return identity


def reset_cache() -> None:
    with _lock:
        _cached.clear()


def _owner_file() -> Optional[str]:
    try:
        value = (data_dir() / OWNER_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NodeIdentityError("owner binding is unreadable") from exc
    if not value:
        raise NodeIdentityError("owner binding is empty")
    return value


def bound_owner() -> Optional[str]:
    value = os.getenv(OWNER_ENV, "").strip() or _owner_file()
    return None if value is None else _stored_owner(value)


def bound_mesh_owner() -> Optional[str]:
    owner = bound_owner()
    if owner is not None and MESH_OWNER_ID.fullmatch(owner) is None:
        raise LegacyOwner("this node holds a legacy owner binding")
    return owner


def _bind_owner_file(owner_id: str) -> str:
    directory = data_dir()
    current = _owner_file()
    if current is not None:
        return current
    temporary = None
    try:
        _ensure_private_dir(directory)
        descriptor, temporary = tempfile.mkstemp(prefix=".owner-", dir=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(owner_id)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, directory / OWNER_FILE)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except FileExistsError:
            current = (directory / OWNER_FILE).read_text(encoding="utf-8").strip()
            if not current:
                raise NodeIdentityError("owner binding is empty")
            return current
    except OSError as exc:
        raise NodeIdentityError("owner binding cannot be stored") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    logger.info("bound this node to its owner")
    return owner_id


def _require_owner(owner_id: str, current: str) -> None:
    current = _stored_owner(current)
    if current == owner_id:
        return
    if MESH_OWNER_ID.fullmatch(current) is None:
        raise LegacyOwner("this node holds a legacy owner binding")
    raise OwnerMismatch("existing owner binding belongs to another principal")


def bind_mesh_owner(mesh_id: str) -> str:
    if not is_personal():
        raise NodeIdentityError("verified personal mesh principal required")
    owner_id = owner_id_for_mesh(mesh_id)
    configured = os.getenv(OWNER_ENV, "").strip()
    if configured:
        _require_owner(owner_id, configured)
    _require_owner(owner_id, _bind_owner_file(owner_id))
    return owner_id


def enforce_owner(owner_id: str) -> None:
    if not is_personal():
        return
    bound = bound_mesh_owner()
    if bound is None or bound != owner_id:
        raise OwnerMismatch("this node serves a different owner")
