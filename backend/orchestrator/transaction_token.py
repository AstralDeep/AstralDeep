"""HMAC-signed, single-use tokens binding (agent, user, tool, hash(args)) so the policy
engine's require_token effect can demand a specific confirmed call; fail-closed and
replay-proof via its own ConsumedStore, called from orchestrator.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("orchestrator.transaction_token")

_DEFAULT_TTL_S = 300


def _key() -> Optional[bytes]:
    raw = os.getenv("TXN_TOKEN_KEY") or os.getenv("MEMORY_HMAC_KEY")
    return raw.encode("utf-8") if raw else None


def _now_ms(now_ms: Optional[int]) -> int:
    return now_ms if now_ms is not None else int(time.time() * 1000)


# Strips _-keys — verify's args include the token, mint's don't
def args_hash(args: Optional[Dict[str, Any]]) -> str:
    clean = {k: v for k, v in (args or {}).items() if not str(k).startswith("_")}
    blob = json.dumps(clean, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _sign(body: str, key: bytes) -> str:
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def mint(agent: str, user: str, tool: str, args: Optional[Dict[str, Any]], *,
         ttl_s: int = _DEFAULT_TTL_S, now_ms: Optional[int] = None,
         nonce: Optional[str] = None) -> Optional[str]:
    key = _key()
    if not key:
        return None
    now = _now_ms(now_ms)
    payload = {
        "a": str(agent), "u": str(user), "t": str(tool),
        "h": args_hash(args), "e": now + max(1, int(ttl_s)) * 1000,
        "n": nonce or secrets.token_hex(8),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{body}.{_sign(body, key)}"


def _decode(token: Any, key: bytes) -> Optional[Dict[str, Any]]:
    if not isinstance(token, str) or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    if not body or not sig or not hmac.compare_digest(_sign(body, key), sig):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + pad))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def verify(token: Any, agent: str, user: str, tool: str,
           args: Optional[Dict[str, Any]], *, now_ms: Optional[int] = None
           ) -> Tuple[bool, Any]:
    key = _key()
    if not key:
        return False, "signing disabled"
    payload = _decode(token, key)
    if payload is None:
        return False, "invalid token"
    if int(payload.get("e", 0)) < _now_ms(now_ms):
        return False, "expired"
    if (payload.get("a") != str(agent) or payload.get("u") != str(user)
            or payload.get("t") != str(tool)):
        return False, "binding mismatch"
    if payload.get("h") != args_hash(args):
        return False, "args mismatch"
    return True, payload


class ConsumedStore:
    def __init__(self) -> None:
        self._seen: Dict[str, int] = {}

    def consume(self, nonce: str, exp_ms: int, *, now_ms: Optional[int] = None) -> bool:
        now = _now_ms(now_ms)
        if self._seen:
            self._seen = {n: e for n, e in self._seen.items() if e > now}
        if nonce in self._seen:
            return False
        self._seen[nonce] = int(exp_ms)
        return True


def verify_and_consume(store: ConsumedStore, token: Any, agent: str, user: str,
                       tool: str, args: Optional[Dict[str, Any]], *,
                       now_ms: Optional[int] = None) -> Tuple[bool, str]:
    ok, detail = verify(token, agent, user, tool, args, now_ms=now_ms)
    if not ok:
        return False, str(detail)
    if not store.consume(str(detail.get("n")), int(detail.get("e", 0)), now_ms=now_ms):
        return False, "already used"
    return True, "ok"


_PROCESS_STORE = ConsumedStore()


def default_store() -> ConsumedStore:
    return _PROCESS_STORE
