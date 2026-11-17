"""Validation and decoding helpers shared by the mesh event, roster, derived chat and
run repositories: content-hash references, bounded integers, byte strings and row decoders.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Final

from astralplane.repositories import (
    RepositoryDataError,
    RepositoryValidationError,
    _freeze,
    _row_value,
)

MAX_INTEGER: Final = 9007199254740991
_HASH_REFERENCE: Final = re.compile(r"sha256:[0-9a-f]{64}")


def hash_reference(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def hash_value(value: object, field: str) -> str:
    if type(value) is not str or _HASH_REFERENCE.fullmatch(value) is None:
        raise RepositoryValidationError(
            f"{field} must be a sha256 reference", metadata={"field": field}
        )
    return value


def optional_hash_value(value: object, field: str) -> str | None:
    return None if value is None else hash_value(value, field)


def bounded_integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise RepositoryValidationError(
            f"{field} is outside its supported bounds", metadata={"field": field}
        )
    return value


def byte_string(value: object, field: str, *, maximum: int) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise RepositoryValidationError(f"{field} must be bytes", metadata={"field": field})
    data = bytes(value)
    if not 1 <= len(data) <= maximum:
        raise RepositoryValidationError(
            f"{field} is outside its supported size", metadata={"field": field, "maximum": maximum}
        )
    return data


def stored_bytes(row: Mapping[str, Any], field: str) -> bytes:
    value = _row_value(row, field)
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise RepositoryDataError("persisted bytes are malformed", metadata={"field": field})
    return bytes(value)


def stored_json(row: Mapping[str, Any], field: str) -> Any:
    return _freeze(_row_value(row, field))


def stored_integer(row: Mapping[str, Any], field: str) -> int:
    value = _row_value(row, field)
    if type(value) is not int:
        raise RepositoryDataError("persisted integer is malformed", metadata={"field": field})
    return value


def returned_row(result: object, operation: str) -> Mapping[str, Any] | None:
    rows = getattr(result, "returned_records", ())
    if len(rows) > 1:
        raise RepositoryDataError(
            "write returned multiple detached records",
            metadata={"operation": operation, "returned": len(rows)},
        )
    return rows[0] if rows else None
