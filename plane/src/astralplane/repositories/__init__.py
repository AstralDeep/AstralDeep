"""Shared base errors and validation helpers for every astralplane.repositories store.
Methods take an explicit Transaction and never borrow connections or commit, so
callers can compose several repository operations into one atomic unit.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, NoReturn

from astralplane.errors import RepositoryConflictError, RepositoryError

CANONICAL_JSON_MAX_DEPTH: Final = 128
CANONICAL_JSON_MAX_NODES: Final = 4096
CANONICAL_JSON_MAX_BYTES: Final = 1_048_576


class RepositoryDataError(RepositoryError):
    default_code = "repository_data_invalid"


class RepositoryNotFoundError(RepositoryError):
    default_code = "repository_not_found"


class RepositoryValidationError(RepositoryError, ValueError):
    default_code = "repository_validation"


def _required_id(value: object, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepositoryValidationError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise RepositoryValidationError(
            f"{field} exceeds its maximum length",
            metadata={"field": field, "maximum": maximum},
        )
    return value


def _bounded_text(
    value: object,
    field: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise RepositoryValidationError(f"{field} must be a string")
    if not allow_empty and not value.strip():
        raise RepositoryValidationError(f"{field} must not be empty")
    if len(value) > maximum:
        raise RepositoryValidationError(
            f"{field} exceeds its maximum length",
            metadata={"field": field, "maximum": maximum},
        )
    return value


def _bounded_limit(value: object, *, maximum: int = 200) -> int:
    if isinstance(value, bool):
        raise RepositoryValidationError("limit must be an integer")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError("limit must be an integer") from exc
    if limit < 1 or limit > maximum:
        raise RepositoryValidationError(
            "limit is outside the supported range",
            metadata={"maximum": maximum},
        )
    return limit


_CANONICAL_INTEGER_TEXT = re.compile(r"-?[0-9]+")


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise RepositoryValidationError(f"{field} must be a non-negative integer")
    if isinstance(value, int):
        integer: int = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise RepositoryValidationError(f"{field} must be a non-negative integer")
        integer = int(value)
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise RepositoryValidationError(f"{field} must be a non-negative integer")
        integer = int(value)
    elif isinstance(value, str) and _CANONICAL_INTEGER_TEXT.fullmatch(value):
        try:
            integer = int(value)
        except ValueError as exc:
            raise RepositoryValidationError(f"{field} must be a non-negative integer") from exc
    else:
        raise RepositoryValidationError(f"{field} must be a non-negative integer")
    if integer < 0:
        raise RepositoryValidationError(f"{field} must be a non-negative integer")
    return integer


def _positive_int(value: object, field: str) -> int:
    integer = _non_negative_int(value, field)
    if integer == 0:
        raise RepositoryValidationError(f"{field} must be a positive integer")
    return integer


def _canonical_json(value: object, field: str) -> str:
    active: set[int] = set()
    nodes = 0

    def json_ready(item: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if depth > CANONICAL_JSON_MAX_DEPTH:
            raise RepositoryValidationError(
                f"{field} exceeds canonical JSON structural bounds",
                metadata={"field": field, "depth": depth},
            )
        if nodes > CANONICAL_JSON_MAX_NODES:
            raise RepositoryValidationError(
                f"{field} exceeds canonical JSON structural bounds",
                metadata={"field": field, "nodes": nodes},
            )
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise RepositoryValidationError(
                    f"{field} contains a cyclic reference",
                    metadata={"field": field},
                )
            active.add(identity)
            try:
                result = {}
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise TypeError(
                            f"Dictionary keys must be strings, got {type(key).__name__}"
                        )
                    if not key.isascii():
                        try:
                            key.encode("utf-8")
                        except UnicodeEncodeError as exc:
                            raise RepositoryValidationError(
                                f"{field} contains unpaired surrogate code points",
                                metadata={"field": field},
                            ) from exc
                    result[key] = json_ready(nested, depth + 1)
            finally:
                active.discard(identity)
            return result
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in active:
                raise RepositoryValidationError(
                    f"{field} contains a cyclic reference",
                    metadata={"field": field},
                )
            active.add(identity)
            try:
                return [json_ready(nested, depth + 1) for nested in item]
            finally:
                active.discard(identity)
        if isinstance(item, str) and not item.isascii():
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise RepositoryValidationError(
                    f"{field} contains unpaired surrogate code points",
                    metadata={"field": field},
                ) from exc
        return item

    try:
        normalized = json_ready(value, 0)
    except RepositoryValidationError:
        raise
    # The walk raises ValueError subclasses; only foreign dumps errors take the generic message
    except (TypeError, ValueError, RecursionError) as exc:
        raise RepositoryValidationError(f"{field} must be canonical JSON-compatible data") from exc
    try:
        encoded_text = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload = encoded_text.encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RepositoryValidationError(f"{field} must be canonical JSON-compatible data") from exc
    if len(payload) > CANONICAL_JSON_MAX_BYTES:
        raise RepositoryValidationError(
            f"{field} exceeds the canonical JSON byte bound",
            metadata={"field": field, "maximum": CANONICAL_JSON_MAX_BYTES},
        )
    return encoded_text


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


def _structured_json(value: object, field: str, *, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    decoded: object = value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RepositoryDataError(
                f"persisted {field} is not valid JSON", metadata={"field": field}
            ) from exc
    if not isinstance(decoded, (Mapping, Sequence)) or isinstance(
        decoded, (str, bytes, bytearray, memoryview)
    ):
        raise RepositoryDataError(
            f"persisted {field} has an unsupported JSON shape",
            metadata={"field": field},
        )
    return _freeze(decoded)


def _content_value(value: object) -> Any:
    if not isinstance(value, str):
        return _freeze(value)
    try:
        return _freeze(json.loads(value))
    except json.JSONDecodeError:
        return value


def _row_value(row: Mapping[str, Any], field: str) -> Any:
    try:
        return row[field]
    except KeyError as exc:
        raise RepositoryDataError(
            "persisted row is missing a required field", metadata={"field": field}
        ) from exc


def _single_returned(result: object, operation: str) -> Mapping[str, Any]:
    rows = getattr(result, "returned_records", ())
    if len(rows) != 1:
        raise RepositoryDataError(
            "write did not return exactly one detached record",
            metadata={"operation": operation, "returned": len(rows)},
        )
    row = rows[0]
    if not isinstance(row, Mapping):
        raise RepositoryDataError(
            "write returned a non-mapping record", metadata={"operation": operation}
        )
    return row


def _raise_write_miss(
    existing: object,
    *,
    operation: str,
    conflict_message: str,
) -> NoReturn:
    if existing is None:
        raise RepositoryNotFoundError(
            "owner-scoped row was not found", metadata={"operation": operation}
        )
    raise RepositoryConflictError(conflict_message, metadata={"operation": operation})


__all__ = (
    "RepositoryConflictError",
    "RepositoryDataError",
    "RepositoryError",
    "RepositoryNotFoundError",
    "RepositoryValidationError",
)
