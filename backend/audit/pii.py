"""PII-safety helpers for the audit log: strips filenames to a normalized extension and
computes HMAC-SHA256 payload/chain digests with a server-held key, used by
audit/repository.py's row construction.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Audit.PII")

_DEV_FALLBACK_SECRET = b"dev-only-audit-hmac-secret-not-for-production"


def _load_secret_for_key_id(key_id: str) -> bytes:
    specific = os.getenv(f"AUDIT_HMAC_SECRET_{key_id.upper()}")
    if specific:
        return specific.encode("utf-8")
    active = os.getenv("AUDIT_HMAC_SECRET")
    if active:
        return active.encode("utf-8")
    logger.warning(
        "AUDIT_HMAC_SECRET is not set — using dev fallback. Set the env var "
        "before deploying to production."
    )
    return _DEV_FALLBACK_SECRET


def get_active_key_id() -> str:
    return os.getenv("AUDIT_HMAC_KEY_ID", "k1")


class PrivateBindingUnavailable(ValueError):
    pass


_PRIVATE_KEY_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PRIVATE_DOMAINS = frozenset({"config", "input", "result"})


@dataclass(frozen=True, slots=True)
class PrivateBindingKey:
    key_id: str
    _key: bytes = field(repr=False)

    def sign(self, domain: str, payload: bytes) -> str:
        if (
            type(domain) is not str or domain not in _PRIVATE_DOMAINS
            or type(payload) is not bytes or len(payload) > 2 * 1024 * 1024
        ):
            raise PrivateBindingUnavailable("private_binding_unavailable")
        return hmac.new(
            self._key, b"astral.work.private/v1/" + domain.encode() + b"\x00" + payload,
            hashlib.sha256,
        ).hexdigest()

    def verify(self, domain: str, payload: bytes, authentication: str) -> bool:
        expected = self.sign(domain, payload)
        return (
            type(authentication) is str
            and re.fullmatch(r"[a-f0-9]{64}", authentication) is not None
            and hmac.compare_digest(expected, authentication)
        )


def private_binding_key(key_id: Optional[str] = None) -> PrivateBindingKey:
    try:
        active = get_active_key_id()
        selected = active if key_id is None else key_id
        if any(type(value) is not str or not _PRIVATE_KEY_ID.fullmatch(value)
               for value in (active, selected)):
            raise ValueError
        specific = os.getenv(f"AUDIT_HMAC_SECRET_{selected.upper()}")
        current = os.getenv("AUDIT_HMAC_SECRET") if selected == active else None
        if specific is not None and current is not None and specific != current:
            raise ValueError
        secret = specific if specific is not None else current
        if (
            secret is None or secret != secret.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in secret)
            or secret in {"dev-audit-hmac-secret-change-me-in-prod", "change-me",
                          _DEV_FALLBACK_SECRET.decode()}
        ):
            raise ValueError
        raw = secret.encode("utf-8")
        if not 32 <= len(raw) <= 4096:
            raise ValueError
        derived = hmac.new(
            raw, b"astral.work.private/key/v1\x00" + selected.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return PrivateBindingKey(selected, derived)
    except (ValueError, UnicodeError, TypeError):
        raise PrivateBindingUnavailable("private_binding_unavailable") from None


_EXT_PATTERN = re.compile(r"^[a-z0-9]{1,16}$")


def normalize_extension(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    raw = name.rsplit(".", 1)[-1].strip().lower() if "." in name else name.strip().lower()
    if _EXT_PATTERN.match(raw):
        return raw
    return None


_FILENAME_KEYS = frozenset({
    "filename", "file_name", "original_name", "originalfilename",
    "name",
    "file",
})

_PHI_RAW_KEYS = frozenset({
    "content", "body", "raw", "data", "bytes", "blob", "buffer",
    "file_bytes", "file_content", "payload", "text",
})


def strip_filename(metadata: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    derived_ext: Optional[str] = None
    for key, value in metadata.items():
        kl = key.lower()
        if kl in _FILENAME_KEYS and isinstance(value, str):
            ext = normalize_extension(value)
            if ext and not derived_ext:
                derived_ext = ext
            continue
        if kl in _PHI_RAW_KEYS:
            continue
        cleaned[key] = value
    if derived_ext and "extension" not in cleaned:
        cleaned["extension"] = derived_ext
    return cleaned


# Never hashlib.sha256 directly — digests need the HMAC key
def hmac_digest(value: bytes, key_id: Optional[str] = None) -> Tuple[str, str]:
    if not isinstance(value, (bytes, bytearray)):
        raise TypeError(f"hmac_digest requires bytes, got {type(value).__name__}")
    kid = key_id or get_active_key_id()
    secret = _load_secret_for_key_id(kid)
    mac = hmac.new(secret, bytes(value), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
    return encoded, kid


def chain_hmac(prev_hash: bytes, canonical_row_bytes: bytes, key_id: Optional[str] = None) -> Tuple[bytes, str]:
    kid = key_id or get_active_key_id()
    secret = _load_secret_for_key_id(kid)
    mac = hmac.new(secret, prev_hash + canonical_row_bytes, hashlib.sha256).digest()
    return mac, kid


class AuditAnchorAuthenticator:
    def sign(self, key_id: str, payload: bytes) -> bytes:
        return hmac.new(
            _load_secret_for_key_id(key_id),
            payload,
            hashlib.sha256,
        ).digest()

    def verify(
        self,
        key_id: str,
        payload: bytes,
        authentication: bytes,
    ) -> bool:
        return hmac.compare_digest(
            self.sign(key_id, payload),
            bytes(authentication),
        )
