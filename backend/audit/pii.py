"""
PII handling helpers for the audit log (FR-015 / FR-016).

Two responsibilities:

1. **Filename stripping**: user-supplied filenames are treated as PHI and
   never persisted in audit rows. We keep only a normalized lowercase
   extension plus the artifact's existing identifier from its source store.
2. **Payload digests**: any cryptographic digest stored in the audit row
   uses ``HMAC-SHA256`` with a server-held key. Plain ``hashlib.sha256``
   of payload contents is forbidden — see :func:`hmac_digest`.

The HMAC key is loaded from ``AUDIT_HMAC_SECRET`` at process start. In
production this MUST be set to a high-entropy secret; in dev a
deterministic fallback is used so tests remain reproducible.
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Audit.PII")

# ---------------------------------------------------------------------------
# Key custody
# ---------------------------------------------------------------------------

_DEV_FALLBACK_SECRET = b"dev-only-audit-hmac-secret-not-for-production"


def _load_secret_for_key_id(key_id: str) -> bytes:
    """Resolve the HMAC secret for a given ``key_id``.

    For the active key (``AUDIT_HMAC_KEY_ID``, default ``"k1"``) we read
    ``AUDIT_HMAC_SECRET`` from the environment. To support rotation, older
    keys can be stored as ``AUDIT_HMAC_SECRET_<KEY_ID_UPPER>`` (e.g.
    ``AUDIT_HMAC_SECRET_K0``); this is checked before falling back to the
    active secret. If nothing is configured, a deterministic dev fallback
    is used and a warning is logged.
    """
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
    """Return the active HMAC ``key_id`` for new audit rows."""
    return os.getenv("AUDIT_HMAC_KEY_ID", "k1")


# ---------------------------------------------------------------------------
# Filename / extension helpers (FR-015)
# ---------------------------------------------------------------------------

_EXT_PATTERN = re.compile(r"^[a-z0-9]{1,16}$")


def normalize_extension(name: Optional[str]) -> Optional[str]:
    """Return a normalized lowercase extension (no dot) or ``None``.

    Accepts a raw filename or extension. Reads only the trailing
    ``.<ext>`` segment, lowercases it, and validates it against the
    JSON-schema pattern. Anything else (including empty / non-matching
    inputs) returns ``None`` so the audit row reflects "extension
    unknown" rather than leaking arbitrary text.
    """
    if not name:
        return None
    raw = name.rsplit(".", 1)[-1].strip().lower() if "." in name else name.strip().lower()
    if _EXT_PATTERN.match(raw):
        return raw
    return None


_FILENAME_KEYS = frozenset({
    "filename", "file_name", "original_name", "originalfilename",
    "name",  # only when in an artifact-pointer-shaped dict
    "file",  # only when value is string-shaped (path-like)
})

_PHI_RAW_KEYS = frozenset({
    # Common payload-bearing keys we never want to copy into audit rows.
    "content", "body", "raw", "data", "bytes", "blob", "buffer",
    "file_bytes", "file_content", "payload", "text",
})


def strip_filename(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``metadata`` with filename-shaped fields removed.

    Replaces any plaintext filename with a derived ``extension`` field
    (when one can be parsed). The original key is dropped entirely so
    audit consumers never see the filename. Other metadata is preserved
    as-is.
    """
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
            # Drop entirely — payload-shaped fields never enter the audit row.
            continue
        cleaned[key] = value
    if derived_ext and "extension" not in cleaned:
        cleaned["extension"] = derived_ext
    return cleaned


# ---------------------------------------------------------------------------
# Payload digests (FR-016)
# ---------------------------------------------------------------------------

def hmac_digest(value: bytes, key_id: Optional[str] = None) -> Tuple[str, str]:
    """Compute an HMAC-SHA256 digest of ``value`` and return ``(digest, key_id)``.

    The digest is base64-encoded (URL-safe, no padding) for compactness in
    the JSON DTO. ``key_id`` defaults to the active key. Use this helper
    for any digest stored in an audit row — never call ``hashlib.sha256``
    directly on payload bytes.
    """
    if not isinstance(value, (bytes, bytearray)):
        raise TypeError(f"hmac_digest requires bytes, got {type(value).__name__}")
    kid = key_id or get_active_key_id()
    secret = _load_secret_for_key_id(kid)
    mac = hmac.new(secret, bytes(value), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
    return encoded, kid


def chain_hmac(prev_hash: bytes, canonical_row_bytes: bytes, key_id: Optional[str] = None) -> Tuple[bytes, str]:
    """Compute the chain ``entry_hash`` and return ``(digest_bytes, key_id)``.

    Used by the repository's hash-chain insert (research.md §R3). The
    digest is returned as raw bytes (32 bytes for SHA-256) suitable for
    storage in a ``BYTEA`` column.
    """
    kid = key_id or get_active_key_id()
    secret = _load_secret_for_key_id(kid)
    mac = hmac.new(secret, prev_hash + canonical_row_bytes, hashlib.sha256).digest()
    return mac, kid


class AuditAnchorAuthenticator:
    """Authenticate retention anchors without exposing configured key bytes."""

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
