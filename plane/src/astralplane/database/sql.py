"""Native psycopg statement/parameter handling: AstralPlane performs no lexical SQL
translation, so callers use psycopg's own placeholder contract directly against
database/transaction.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

from astralplane.contracts import Parameters, Statement
from astralplane.errors import SQLContractError

NativeParameters: TypeAlias = tuple[object, ...] | dict[str, object] | None


def validate_statement(statement: Statement) -> str:
    if not isinstance(statement, str):
        raise SQLContractError("statement must be a string")
    if not statement.strip():
        raise SQLContractError("statement must not be empty")
    if "\x00" in statement:
        raise SQLContractError("statement must not contain a NUL byte")
    return statement


def normalize_parameters(parameters: Parameters) -> NativeParameters:
    if isinstance(parameters, Mapping):
        normalized: dict[str, object] = {}
        for key, value in parameters.items():
            if not isinstance(key, str) or not key:
                raise SQLContractError("named parameter keys must be non-empty strings")
            normalized[key] = value
        return normalized or None
    if isinstance(parameters, Sequence) and not isinstance(
        parameters, (str, bytes, bytearray, memoryview)
    ):
        normalized_sequence = tuple(parameters)
        return normalized_sequence or None
    raise SQLContractError("parameters must be a positional sequence or named mapping")


def execute_native(cursor: Any, statement: Statement, parameters: Parameters = ()) -> Any:
    exact_statement = validate_statement(statement)
    native_parameters = normalize_parameters(parameters)
    if native_parameters is None:
        return cursor.execute(exact_statement)
    return cursor.execute(exact_statement, native_parameters)


__all__ = (
    "NativeParameters",
    "execute_native",
    "normalize_parameters",
    "validate_statement",
)
