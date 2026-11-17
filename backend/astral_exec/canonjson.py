"""LETS-CJ/1 canonical JSON and strict parsing for policy documents, reimplemented on the standard library so the package has no dependency.
policy.py uses it for the digest and for the strict text rules: no duplicate keys, no floats, 64 bit integers and valid UTF-8.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

INT_MIN = -(1 << 63)
INT_MAX = (1 << 63) - 1


class CanonicalError(ValueError):
    pass


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise CanonicalError("object keys must be strings")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise CanonicalError("integer outside signed 64 bits")
        return value
    raise CanonicalError(f"unsupported type {type(value).__name__}")


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            _normalize(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError as error:
        raise CanonicalError("lone surrogate in string") from error
    except RecursionError as error:
        raise CanonicalError("nesting too deep") from error


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in pairs:
        if key in out:
            raise CanonicalError(f"duplicate key {key}")
        out[key] = item
    return out


def _reject_constant(name: str) -> Any:
    raise CanonicalError(f"non finite number {name}")


def _reject_float(text: str) -> Any:
    raise CanonicalError(f"float {text}")


def _checked_int(text: str) -> int:
    number = int(text)
    if not INT_MIN <= number <= INT_MAX:
        raise CanonicalError(f"integer {text} outside signed 64 bits")
    return number


def strict_loads(data: str | bytes) -> Any:
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        parsed = json.loads(
            text,
            object_pairs_hook=_unique,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            parse_int=_checked_int,
        )
    except UnicodeDecodeError as error:
        raise CanonicalError("not UTF-8") from error
    except RecursionError as error:
        raise CanonicalError("nesting too deep") from error
    except json.JSONDecodeError as error:
        raise CanonicalError(str(error)) from error
    except ValueError as error:
        if isinstance(error, CanonicalError):
            raise
        raise CanonicalError(str(error)) from error
    canonical(parsed)
    return parsed
