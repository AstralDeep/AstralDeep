"""Checks the strict value and SQL helpers used by SQLite repository statements and schema guards.
Known values, malformed encodings and boundary cases cover PostgreSQL-compatible scalar behavior.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from astralplane.database import sqlite_functions as functions


@pytest.mark.parametrize(
    "value,expected",
    [
        ("{}", []),
        ("{ }", []),
        ("{a,b}", ["a", "b"]),
        ('{NULL,"NULL"}', [None, "NULL"]),
        ('{"a,b",c}', ["a,b", "c"]),
        ('{"a\\"b", c}', ['a"b', "c"]),
        ("{a\\,b,c}", ["a,b", "c"]),
        ('{"", a }', ["", "a"]),
    ],
)
def test_array_literal_parser_preserves_quotes_escapes_and_nulls(value, expected):
    assert functions._array_items(value) == expected


@pytest.mark.parametrize(
    "value", ["a", "{a,}", "{,a}", '{"unterminated}', "{a\\}", "{{a}}", '{"a"x}', "{a,,b}"]
)
def test_malformed_array_literals_are_rejected(value):
    with pytest.raises(ValueError):
        functions._array_items(value)


@pytest.mark.parametrize(
    "function,arguments,expected",
    [
        ("_text", (None,), None),
        ("_text", (b"abc",), "abc"),
        ("_text", (123,), "123"),
        ("_bytes", (memoryview(b"abc"),), b"abc"),
        ("_bytes", ("abc",), b"abc"),
        ("_epoch", (None,), None),
        ("_epoch", (12,), 12.0),
        ("_epoch", ("1970-01-01T00:00:01Z",), 1.0),
        ("_right", (None, 2), None),
        ("_right", ("abc", None), None),
        ("_right", ("abcdef", 2), "ef"),
        ("_right", ("abcde", -2), "cde"),
        ("_right", ("ab", -5), ""),
        ("_greatest", (None, 3, 1), 3),
        ("_greatest", (None, None), None),
        ("_least", (None, 3, 1), 1),
        ("_least", (None, None), None),
        ("_split_part", (None, "/", 1), None),
        ("_split_part", ("a/b", "/", 2), "b"),
        ("_split_part", ("a/b", "/", -1), "b"),
        ("_split_part", ("a/b", "/", 3), ""),
        ("_split_part", ("abc", "", 1), "abc"),
        ("_split_part", ("abc", "", 2), ""),
        ("_digest", (None, "sha256"), None),
        (
            "_digest",
            ("abc", "sha256"),
            bytes.fromhex("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
        ),
        ("_encode", (None, "hex"), None),
        ("_encode", (b"abc", "hex"), "616263"),
        ("_encode", (b"abc", "base64"), "YWJj"),
        ("_decode", (None, "hex"), None),
        ("_decode", ("616263", "hex"), b"abc"),
        ("_decode", ("YWJj", "base64"), b"abc"),
        ("_hashtext", (None,), None),
        ("_jsonb_typeof", (None,), None),
        ("_jsonb_typeof", ("null",), "null"),
        ("_jsonb_typeof", ("true",), "boolean"),
        ("_jsonb_typeof", ("1",), "number"),
        ("_jsonb_typeof", ('"text"',), "string"),
        ("_jsonb_typeof", ("[]",), "array"),
        ("_jsonb_typeof", ("{}",), "object"),
        ("_jsonb_array_length", (None,), None),
        ("_jsonb_array_length", ("[1,null]",), 2),
        ("_jsonb_build_object", ("x", 1, "y", None), '{"x":1,"y":null}'),
        ("_to_timestamp", (None,), None),
        ("_to_timestamp", (1,), "1970-01-01T00:00:01.000000Z"),
        ("_regexp", (None, "x"), None),
        ("_regexp", ("a$", "a\n"), 0),
        ("_regexp", ("[[:digit:]]+", "123"), 1),
        ("_regexp", ("(?i)abc", "ABC"), 1),
        ("_cast_json", (None,), None),
        ("_cast_json", ('{"é":1}',), '{"é":1}'),
        ("_cast_array", (None,), None),
        ("_cast_array", ("{}",), "[]"),
        ("_cast_boolean", (None,), None),
        ("_cast_boolean", ("true",), 1),
        ("_cast_boolean", (3,), 1),
        ("_json_has", (None, "x"), None),
        ("_json_has", ('"x"', "x"), 1),
        ("_json_has", ('{"x":1}', "x"), 1),
        ("_pg_input_is_valid", (None, "jsonb"), None),
        ("_pg_input_is_valid", ("{}", "jsonb"), 1),
        ("_pg_input_is_valid", ("{bad", "jsonb"), 0),
        ("_to_jsonb", (None,), None),
        ("_timestamp_is_valid", (None,), 0),
        ("_timestamp_is_valid", ("invalid",), 0),
        ("_timestamp_is_valid", ("2026-11-12T12:00:00Z",), 1),
    ],
)
def test_scalar_helpers_preserve_known_values(function, arguments, expected):
    assert getattr(functions, function)(*arguments) == expected


@pytest.mark.parametrize(
    "function,arguments",
    [
        ("_split_part", ("a", "/", 0)),
        ("_encode", (b"x", "unknown")),
        ("_decode", ("invalid", "hex")),
        ("_decode", ("not base64!", "base64")),
        ("_decode", ("x", "unknown")),
        ("_jsonb_array_length", ("{}",)),
        ("_jsonb_build_object", ("x",)),
        ("_jsonb_build_object", (None, 1)),
        ("_cast_array", ("{}x",)),
        ("_pg_input_is_valid", ("1", "unsupported")),
        ("timeout_seconds", ("-1ms",)),
        ("_cast_json", ("NaN",)),
        ("_cast_integer", ("x", 64)),
        ("_convert_boolean", (b"true",)),
        ("_path_items", ("[1]",)),
    ],
)
def test_malformed_values_never_become_coerced_success(function, arguments):
    with pytest.raises(ValueError):
        getattr(functions, function)(*arguments)


@pytest.mark.parametrize(
    "raw,path,replacement,create,expected",
    [
        ('{"a":1}', "{a}", "2", True, '{"a":2}'),
        ('{"a":1}', '["b"]', "2", False, '{"a":1}'),
        ('{"a":1}', "{missing,x}", "2", True, '{"a":1}'),
        ('{"a":1}', "{a,x}", "2", True, '{"a":1}'),
        ("[1,2]", "{-1}", "3", True, "[1,3]"),
        ("[1,2]", "{8}", "3", True, "[1,2,3]"),
        ("[1,2]", "{-8}", "3", True, "[3,1,2]"),
        ("[1,2]", "{8}", "3", False, "[1,2]"),
        ('{"a":[1]}', "{a,0}", "2", True, '{"a":[2]}'),
        ('{"a":[1]}', "{a,8,x}", "2", True, '{"a":[1]}'),
        (None, "{a}", "2", True, None),
    ],
)
def test_json_updates_preserve_paths_and_creation_rules(raw, path, replacement, create, expected):
    assert functions._jsonb_set(raw, path, replacement, create) == expected


@pytest.mark.parametrize(
    "raw,path,expected",
    [
        (None, "{a}", None),
        ('{"a":{"b":true}}', "{a,b}", "true"),
        ('{"a":[1,2]}', "{a,-1}", "2"),
        ('{"a":[1]}', "{a,8}", None),
        ('{"a":[1]}', "{a,invalid}", None),
        ('{"a":1}', "{a,x}", None),
        ("{}", "{missing}", None),
        ('{"a":null}', "{a}", None),
        ('{"a":"text"}', "{a}", "text"),
    ],
)
def test_json_paths_preserve_missing_null_and_scalar_values(raw, path, expected):
    assert functions._json_path_text(raw, path) == expected


def test_adapters_and_aggregates_preserve_standard_driver_values():
    identifier = UUID("00000000-0000-4000-8000-000000000001")
    assert functions._adapt_uuid(identifier) == str(identifier)
    assert functions._adapt_decimal(Decimal("12.50")) == "12.50"
    assert functions._adapt_date(date(2026, 11, 12)) == "2026-11-12"
    assert functions._adapt_datetime(datetime(2026, 11, 12, tzinfo=UTC)).endswith("Z")
    assert (
        functions.format_timestamp(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00.000000Z"
    )
    assert functions.parse_timestamp("2026-11-12T00:00:00").tzinfo == UTC
    assert functions._convert_timestamp(b"invalid") == "invalid"
    assert functions._convert_boolean(b"1") is True
    assert functions._convert_json(b"[1]") == [1]
    assert functions.bind_value({"b", "a"}) == '["a","b"]'
    text = functions._StringAggregate()
    assert text.finalize() is None
    text.step(None, ",")
    text.step("a", None)
    text.step("b", ",")
    assert text.finalize() == "a,b"
    for aggregate in (functions._BoolAnd, functions._BoolOr):
        instance = aggregate()
        assert instance.finalize() is None
        instance.step(None)
        instance.step(True)
        instance.step(False)
        assert instance.finalize() == int(aggregate is functions._BoolOr)


@pytest.mark.parametrize(
    "raw,kind,expected",
    [
        ("[1,true,null]", "text", '["1","true",null]'),
        ('["32767",null]', "smallint", "[32767,null]"),
        ('["9223372036854775807"]', "bigint", "[9223372036854775807]"),
        ('["yes",0]', "boolean", "[true,false]"),
        (
            '["{00000000000040008000000000000001}"]',
            "uuid",
            '["00000000-0000-4000-8000-000000000001"]',
        ),
    ],
)
def test_array_casts_preserve_each_declared_element_type(raw, kind, expected):
    assert functions._cast_array(raw, kind) == expected


@pytest.mark.parametrize(
    "raw,kind",
    [
        ('["32768"]', "smallint"),
        ('["bad"]', "uuid"),
        ("[[]]", "text"),
        ("[1]", "unsupported"),
        ('["maybe"]', "boolean"),
    ],
)
def test_invalid_array_elements_are_refused(raw, kind):
    with pytest.raises((ValueError, ArithmeticError)):
        functions._cast_array(raw, kind)


def test_bindings_and_unowned_lock_functions_cannot_fabricate_success():
    import sqlite3

    with pytest.raises(TypeError):
        functions.bind_value({"value": object()})
    with sqlite3.connect(":memory:") as connection:
        functions.install_functions(
            connection,
            clock=lambda: datetime.now(UTC),
            transaction_clock=lambda: datetime.now(UTC),
            settings={},
        )
        for name in ("pg_advisory_xact_lock", "pg_try_advisory_xact_lock"):
            with pytest.raises(sqlite3.OperationalError):
                connection.execute(f"SELECT {name}(1)").fetchall()
