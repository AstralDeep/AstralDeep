"""Tests for src/astralplane/database/sql.py: native psycopg parameter handling
preserves statement text exactly and rejects malformed inputs before driver
execution.
"""

from __future__ import annotations

from typing import Any

import pytest

from astralplane.database.sql import execute_native, normalize_parameters, validate_statement
from astralplane.errors import SQLContractError


class RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def execute(self, *arguments: Any) -> str:
        self.calls.append(arguments)
        return "driver-result"


@pytest.mark.parametrize(
    ("statement", "parameters", "expected_call"),
    [
        ("SELECT '?' AS literal", (), ("SELECT '?' AS literal",)),
        ("SELECT 1 -- ? and %s are comments", (), ("SELECT 1 -- ? and %s are comments",)),
        ("SELECT name LIKE '100%'", (), ("SELECT name LIKE '100%'",)),
        (
            "SELECT payload ? 'enabled', payload ?| array['a', 'b']",
            (),
            ("SELECT payload ? 'enabled', payload ?| array['a', 'b']",),
        ),
        ("SELECT 10 % 3", (), ("SELECT 10 % 3",)),
        ("SELECT * FROM item WHERE id = %s", (7,), ("SELECT * FROM item WHERE id = %s", (7,))),
        (
            "SELECT * FROM item WHERE id = %(item_id)s",
            {"item_id": 7},
            ("SELECT * FROM item WHERE id = %(item_id)s", {"item_id": 7}),
        ),
        ("SELECT '%%' AS escaped_percent", (), ("SELECT '%%' AS escaped_percent",)),
        ("/* %s ? */ SELECT %s", (9,), ("/* %s ? */ SELECT %s", (9,))),
    ],
)
def test_execute_native_preserves_statement_text_exactly(
    statement: str,
    parameters: object,
    expected_call: tuple[object, ...],
) -> None:
    cursor = RecordingCursor()

    result = execute_native(cursor, statement, parameters)  # type: ignore[arg-type]

    assert result == "driver-result"
    assert cursor.calls == [expected_call]


def test_named_parameters_are_snapshotted_before_driver_use() -> None:
    cursor = RecordingCursor()
    parameters = {"owner_id": "owner-1"}

    execute_native(cursor, "SELECT %(owner_id)s", parameters)
    parameters["owner_id"] = "changed"

    assert cursor.calls == [("SELECT %(owner_id)s", {"owner_id": "owner-1"})]
    assert cursor.calls[0][1] is not parameters


@pytest.mark.parametrize("parameters", [(), [], {}, ()])
def test_empty_native_parameters_use_the_driver_one_argument_form(parameters: object) -> None:
    cursor = RecordingCursor()

    execute_native(cursor, "SELECT 100%", parameters)  # type: ignore[arg-type]

    assert cursor.calls == [("SELECT 100%",)]


@pytest.mark.parametrize("statement", ["", "   \n", "SELECT\x00 1", 7, None])
def test_invalid_statements_fail_before_driver_execution(statement: object) -> None:
    cursor = RecordingCursor()

    with pytest.raises(SQLContractError):
        execute_native(cursor, statement)  # type: ignore[arg-type]

    assert cursor.calls == []


@pytest.mark.parametrize(
    "parameters",
    ["value", b"value", bytearray(b"value"), memoryview(b"value"), 3, {"": 1}, {1: 2}],
)
def test_invalid_parameter_shapes_fail_closed(parameters: object) -> None:
    with pytest.raises(SQLContractError):
        normalize_parameters(parameters)  # type: ignore[arg-type]


def test_validation_does_not_rewrite_or_normalize_whitespace() -> None:
    statement = "\n  SELECT E'\\\\?'  -- trailing space  \n"
    assert validate_statement(statement) is statement
