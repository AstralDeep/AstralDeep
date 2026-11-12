"""Tests for orchestrator/tool_arguments.py: repair, coercion, validation and error text."""

import pytest
from orchestrator.tool_arguments import (
    ArgumentError, ArgumentResult, example_for, model_error_text, prepare_arguments)

SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"}, "days": {"type": "integer"}, "ratio": {"type": "number"},
        "metric": {"type": "boolean"}, "unit": {"type": "string", "enum": ["c", "f"]},
        "zip": {"type": "string"}, "filters": {"type": "object"},
    },
    "required": ["city"],
}


def test_valid_json_passes_untouched():
    assert prepare_arguments('{"city": "Rome"}', SCHEMA) == ArgumentResult({"city": "Rome"}, False, None)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_becomes_an_empty_object(raw):
    assert prepare_arguments(raw, None) == ArgumentResult({}, False, None)


def test_a_dict_is_used_as_given():
    assert prepare_arguments({"city": "Rome"}, SCHEMA).args == {"city": "Rome"}


def test_raw_control_characters_are_accepted_without_repair():
    assert prepare_arguments('{"city": "a\nb"}', None) == ArgumentResult({"city": "a\nb"}, False, None)


@pytest.mark.parametrize("raw", [
    '{"city": "Rome",}',
    '```json\n{"city": "Rome"}\n```',
    'Here you go: {"city": "Rome"} thanks',
    '"{\\"city\\": \\"Rome\\"}"',
])
def test_mechanical_repairs(raw):
    assert prepare_arguments(raw, SCHEMA) == ArgumentResult({"city": "Rome"}, True, None)


def test_python_literals_are_repaired():
    result = prepare_arguments('{"city": "Rome", "metric": True, "filters": None}', None)
    assert result == ArgumentResult({"city": "Rome", "metric": True, "filters": None}, True, None)


def test_truncated_json_is_not_completed():
    result = prepare_arguments('{"city": "Rome", "zip": "405', SCHEMA)
    assert result.args is None and result.error.code == "invalid_json"


def test_a_non_object_is_refused():
    assert prepare_arguments("[1, 2]", SCHEMA).error.code == "not_an_object"


def test_coercion_follows_the_schema():
    raw = '{"city": "Rome", "days": "3", "ratio": "0.5", "metric": "true", "zip": 40506, "filters": "{\\"a\\": 1}"}'
    assert prepare_arguments(raw, SCHEMA) == ArgumentResult(
        {"city": "Rome", "days": 3, "ratio": 0.5, "metric": True, "zip": "40506", "filters": {"a": 1}}, True, None)


def test_repair_never_guesses():
    assert prepare_arguments('{"city": "007"}', SCHEMA) == ArgumentResult({"city": "007"}, False, None)
    assert prepare_arguments('{"city": "Rome", "days": "3.5"}', SCHEMA).error.field == "days"
    assert prepare_arguments('{"city": "Rome", "days": "3"}', None).args == {"city": "Rome", "days": "3"}


def test_extras_are_dropped_only_when_the_schema_forbids_them():
    closed = {**SCHEMA, "additionalProperties": False}
    assert prepare_arguments('{"city": "Rome", "x": 1}', closed) == ArgumentResult({"city": "Rome"}, True, None)
    assert prepare_arguments('{"city": "Rome", "x": 1}', SCHEMA).args == {"city": "Rome", "x": 1}


def test_missing_required_field():
    error = prepare_arguments("{}", SCHEMA).error
    assert error == ArgumentError("schema_mismatch", "city", "missing required field 'city'.", {"city": "<string>"})


def test_enum_and_type_messages():
    assert prepare_arguments('{"city": "Rome", "unit": "k"}', SCHEMA).error.message == \
        "field 'unit' must be one of [\"c\", \"f\"]."
    assert prepare_arguments('{"city": ["Rome"]}', SCHEMA).error.message == "field 'city' must be string."


def test_a_broken_schema_never_fails_a_call():
    assert prepare_arguments('{"a": 1}', {"type": "nonsense"}).args == {"a": 1}
    assert prepare_arguments('{"a": 1}', "not a schema").args == {"a": 1}


def test_example_for_uses_enum_then_type():
    schema = {"type": "object", "required": ["unit", "days", "tags"],
              "properties": {"unit": {"enum": ["c", "f"]}, "days": {"type": "integer"}, "tags": {"type": "array"}}}
    assert example_for(schema) == {"unit": "c", "days": 0, "tags": []}


def test_model_error_text():
    error = ArgumentError("schema_mismatch", "city", "missing required field 'city'.", {"city": "<string>"})
    assert model_error_text("get_weather", error) == (
        "The arguments for 'get_weather' were not run: missing required field 'city'. "
        "Send the call again with arguments like {\"city\": \"<string>\"}.")
    bare = ArgumentError("invalid_json", "", "Expecting value: line 1 column 1 (char 0)", None)
    assert model_error_text("get_weather", bare) == (
        "The arguments for 'get_weather' were not run: Expecting value: line 1 column 1 (char 0)")


def test_literals_and_commas_inside_strings_are_left_alone():
    result = prepare_arguments('{"city": "True, None,}", "zip": "a,]"}', None)
    assert result == ArgumentResult({"city": "True, None,}", "zip": "a,]"}, False, None)


def test_a_json_string_that_is_not_an_object_is_refused():
    assert prepare_arguments('"plain text"', None).error.code == "not_an_object"
    assert prepare_arguments(42, None).error.code == "not_an_object"


def test_nested_coercion_reaches_array_items_and_nested_objects():
    schema = {"type": "object", "properties": {
        "ids": {"type": "array", "items": {"type": "integer"}},
        "box": {"type": "object", "properties": {"w": {"type": "number"}}, "additionalProperties": False},
        "tags": {"type": "array"}}}
    raw = '{"ids": ["1", "2"], "box": {"w": "2", "extra": 1}, "tags": "[\\"a\\"]"}'
    assert prepare_arguments(raw, schema) == ArgumentResult(
        {"ids": [1, 2], "box": {"w": 2}, "tags": ["a"]}, True, None)


def test_non_finite_and_malformed_numbers_are_not_coerced():
    schema = {"type": "object", "properties": {"x": {"type": "number"}}}
    assert prepare_arguments('{"x": "nan"}', schema).error.field == "x"
    assert prepare_arguments('{"x": "1_0"}', schema).error.field == "x"
    assert prepare_arguments('{"x": "1e400"}', schema).error.field == "x"


def test_other_validator_messages_are_cut_and_carry_the_path():
    schema = {"type": "object", "properties": {"city": {"type": "string", "minLength": 3}}}
    error = prepare_arguments('{"city": "Ro"}', schema).error
    assert error.field == "city" and error.code == "schema_mismatch" and len(error.message) <= 200


def test_union_types_are_described_and_the_first_error_is_by_path():
    schema = {"type": "object", "properties": {"a": {"type": ["string", "null"]}, "b": {"type": "integer"}}}
    assert prepare_arguments('{"a": 1.5}', schema).error.message == "field 'a' must be string or null."
    assert prepare_arguments('{"b": [], "a": 1.5}', schema).error.field == "a"


def test_example_for_prefers_examples_then_default_then_unknown():
    schema = {"required": ["a", "b", "c", "d"], "properties": {
        "a": {"examples": ["x"]}, "b": {"default": 7}, "c": {"type": ["null", "boolean"]}, "d": {}}}
    assert example_for(schema) == {"a": "x", "b": 7, "c": False, "d": "<value>"}


def test_raw_arguments_given_as_a_dict_are_not_mutated():
    original = {"city": "Rome", "days": "3"}
    result = prepare_arguments(original, SCHEMA)
    assert result.args == {"city": "Rome", "days": 3} and original == {"city": "Rome", "days": "3"}
