"""LETS-CJ/1 canonical JSON, strict parsing and unpadded base64url used by every hashed or signed object.
It matches the LETS reference byte for byte and raises MeshError codes instead of ValueError.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import MeshError

INT_MIN = -(1 << 63)
INT_MAX = (1 << 63) - 1


def _normalize(value: Any) -> Any:
    if isinstance(value, bytes):
        return b64url_encode(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise MeshError("canonical.type", "object keys must be strings")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        raise MeshError("canonical.type", "unordered sets are refused")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise MeshError("canonical.range", "integer outside signed 64 bits")
        return value
    if isinstance(value, float):
        raise MeshError("canonical.float", "floats are refused")
    raise MeshError("canonical.type", type(value).__name__)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            _normalize(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MeshError("canonical.surrogate", "lone surrogate in string") from exc


def strict_loads(text: str | bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in pairs:
            if key in out:
                raise MeshError("canonical.duplicate_key", key)
            out[key] = item
        return out

    def reject_constant(name: str) -> Any:
        raise MeshError("canonical.non_finite", name)

    def reject_float(text_: str) -> Any:
        raise MeshError("canonical.float", text_)

    def checked_int(text_: str) -> int:
        number = int(text_)
        if not INT_MIN <= number <= INT_MAX:
            raise MeshError("canonical.range", text_)
        return number

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=unique,
            parse_constant=reject_constant,
            parse_float=reject_float,
            parse_int=checked_int,
        )
    except json.JSONDecodeError as exc:
        raise MeshError("canonical.parse", str(exc)) from exc
    canonical(parsed)
    return parsed


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or "=" in text:
        raise MeshError("canonical.base64", "must be unpadded text")
    try:
        encoded = text.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise MeshError("canonical.base64", "malformed") from exc
    if b64url_encode(decoded) != text:
        raise MeshError("canonical.base64", "not canonical")
    return decoded
