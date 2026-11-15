"""TTL-cached Keycloak JWKS fetch shared by orchestrator/auth.py, mcp_authz.py, and
shared/a2a_security.py so token validation doesn't hit the identity provider every
request; a kid-miss triggers an immediate refetch, and the last good keys keep serving
for a bounded grace when the issuer cannot be reached.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("shared.jwks_cache")

_TTL_SECONDS = 600
_DEFAULT_GRACE_SECONDS = 86400
_FETCH_TIMEOUT_SECONDS = 10
_RETRY_AFTER_FAILURE_SECONDS = 30
_cache: Dict[str, Dict[str, Any]] = {}


def _kids(jwks: Dict[str, Any]) -> set:
    return {k.get("kid") for k in (jwks or {}).get("keys", []) if isinstance(k, dict)}


def _token_kid(token: str) -> Optional[str]:
    try:
        import base64
        header = token.split(".")[0]
        header += "=" * (-len(header) % 4)
        return json.loads(base64.urlsafe_b64decode(header)).get("kid")
    except Exception:
        return None


def _grace_seconds() -> int:
    try:
        return max(0, int(os.getenv("JWKS_STALE_GRACE_SECONDS", str(_DEFAULT_GRACE_SECONDS))))
    except ValueError:
        return _DEFAULT_GRACE_SECONDS


async def _fetch(jwks_url: str) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(jwks_url) as resp:
            resp.raise_for_status()
            jwks = await resp.json()
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise ValueError("issuer returned a malformed key set")
    _cache[jwks_url] = {"jwks": jwks, "fetched_at": time.time()}
    return jwks


async def _refetch(jwks_url: str, entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    now = time.time()
    within_grace = entry is not None and (now - entry["fetched_at"]) < _TTL_SECONDS + _grace_seconds()
    if within_grace and entry.get("retry_at", 0) > now:
        return entry["jwks"]
    try:
        return await _fetch(jwks_url)
    except Exception as exc:
        if within_grace:
            entry["retry_at"] = now + _RETRY_AFTER_FAILURE_SECONDS
            logger.warning(
                "jwks_cache: issuer unreachable (%s), serving last good keys within grace",
                type(exc).__name__,
            )
            return entry["jwks"]
        raise


async def get_jwks(jwks_url: str, *, token: Optional[str] = None) -> Dict[str, Any]:
    entry = _cache.get(jwks_url)
    if entry and (time.time() - entry["fetched_at"]) < _TTL_SECONDS:
        jwks = entry["jwks"]
        if token is not None:
            kid = _token_kid(token)
            if kid and kid not in _kids(jwks):
                logger.info("jwks_cache: kid %s not in cached set — refetching (rotation?)", kid)
                return await _refetch(jwks_url, entry)
        return jwks
    return await _refetch(jwks_url, entry)


def clear() -> None:
    _cache.clear()
