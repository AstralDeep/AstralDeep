"""Tests for the bounded PlaneError diagnostic-value contract in astralplane.errors.
They pin the entry-count, key, value, and code bounds plus the never-serialize rule
for nested objects, control characters, broken __str__ implementations, and values a
host passes without redaction.
"""

from __future__ import annotations

import pytest

from astralplane.errors import (
    DomainValidationError,
    PlaneError,
)

MAX_CODE_LENGTH = 64
MAX_METADATA_ENTRIES = 16
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_VALUE_LENGTH = 512


def test_scalar_metadata_renders_and_sorts_deterministically() -> None:
    error = PlaneError(
        "failed",
        metadata={"b": "two", "a": 3, "c": True, "d": None, "e": 1.5},
    )

    assert error.code == "plane_error"
    assert error.metadata == (
        ("a", "3"),
        ("b", "two"),
        ("c", "True"),
        ("d", "None"),
        ("e", "1.5"),
    )


def test_huge_value_is_truncated_to_the_documented_bound() -> None:
    error = PlaneError("failed", metadata={"blob": "x" * 100_000})

    assert error.metadata == (("blob", "x" * MAX_METADATA_VALUE_LENGTH),)


def test_entry_count_is_bounded_deterministically() -> None:
    metadata = {f"key{index:02d}": index for index in range(100)}

    error = PlaneError("failed", metadata=metadata)

    assert error.metadata == tuple(
        (f"key{index:02d}", str(index)) for index in range(MAX_METADATA_ENTRIES)
    )


def test_oversized_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="key exceeds its maximum length"):
        PlaneError("failed", metadata={"k" * (MAX_METADATA_KEY_LENGTH + 1): "value"})


def test_non_string_key_is_rejected() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        PlaneError("failed", metadata={1: "value"})  # type: ignore[dict-item]


def test_non_mapping_metadata_is_rejected() -> None:
    with pytest.raises(TypeError, match="must be a Mapping or None"):
        PlaneError("failed", metadata=[("key", "value")])  # type: ignore[arg-type]


def test_nested_objects_are_never_serialized() -> None:
    payload = {"nested": {"marker": "synthetic-payload-bytes"}}

    error = PlaneError("failed", metadata={"payload": payload, "items": ["a", "b"]})

    assert error.metadata == (("items", "<unsupported>"), ("payload", "<unsupported>"))
    assert "synthetic-payload-bytes" not in repr(error)


def test_broken_str_is_never_invoked() -> None:
    class Broken:
        def __str__(self) -> str:
            raise AssertionError("PlaneError must not serialize arbitrary objects")

    error = PlaneError("failed", metadata={"broken": Broken()})

    assert error.metadata == (("broken", "<unsupported>"),)


def test_broken_scalar_str_falls_back_to_placeholder() -> None:
    class BrokenInt(int):
        def __str__(self) -> str:
            raise AssertionError("unreachable")

    error = PlaneError("failed", metadata={"broken": BrokenInt(7), "huge": 10**6000})

    assert error.metadata == (
        ("broken", "<unsupported>"),
        ("huge", "<unsupported>"),
    )


def test_control_characters_are_escaped() -> None:
    error = PlaneError(
        "failed",
        metadata={"text": "line\nnext\x00esc\x1b[31mc1\x85"},
    )

    assert error.metadata == (
        ("text", "line\\x0anext\\x00esc\\x1b[31mc1\\x85"),
    )


def test_escaped_value_remains_within_the_bound() -> None:
    error = PlaneError("failed", metadata={"text": "\n" * 1000})

    ((_, value),) = error.metadata
    assert len(value) == MAX_METADATA_VALUE_LENGTH
    assert set(value) == {"\\", "x", "0", "a"}


def test_code_bounds_and_defaults() -> None:
    assert PlaneError("failed", code="custom_code").code == "custom_code"
    assert PlaneError("failed", code="").code == "plane_error"

    with pytest.raises(ValueError, match="code exceeds its maximum length"):
        PlaneError("failed", code="x" * (MAX_CODE_LENGTH + 1))
    with pytest.raises(ValueError, match="unsupported characters"):
        PlaneError("failed", code="bad code\n")
    with pytest.raises(TypeError, match="code must be a string"):
        PlaneError("failed", code=7)  # type: ignore[arg-type]


def test_unsafe_default_code_is_rejected() -> None:
    class UnsafeCodeError(PlaneError):
        default_code = "bad code"

    with pytest.raises(ValueError, match="unsupported characters"):
        UnsafeCodeError("failed")


def test_non_string_default_code_is_rejected() -> None:
    class NonStringCodeError(PlaneError):
        default_code = None  # type: ignore[assignment]

    with pytest.raises(TypeError, match="code must be a string"):
        NonStringCodeError("failed")


def test_synthetic_secret_values_are_bounded_but_not_redacted() -> None:
    synthetic = "api_key=synthetic-sensitive-diagnostic"

    error = PlaneError("failed", metadata={"synthetic_token": synthetic})

    assert error.metadata == (("synthetic_token", synthetic),)
    assert synthetic not in repr(error)


def test_empty_and_none_metadata() -> None:
    assert PlaneError("failed").metadata == ()
    assert PlaneError("failed", metadata={}).metadata == ()
    assert PlaneError("failed", metadata=None).metadata == ()


def test_mapping_iteration_order_does_not_matter() -> None:
    one = PlaneError("failed", metadata={"a": "1", "b": "2", "c": "3"})
    two = PlaneError("failed", metadata={"c": "3", "a": "1", "b": "2"})

    assert one.metadata == two.metadata == (("a", "1"), ("b", "2"), ("c", "3"))


def test_typed_subclasses_keep_their_codes() -> None:
    error = DomainValidationError("invalid")

    assert error.code == "domain_validation"
    assert isinstance(error, ValueError)


@pytest.mark.parametrize("key", ["", "line\nkey", "\x00", "\x85"])
def test_metadata_keys_reject_control_characters_and_empty_identifiers(key: str) -> None:
    with pytest.raises(ValueError, match="unsupported characters"):
        PlaneError("failed", metadata={key: "value"})


def test_scalar_subclasses_are_not_serialized() -> None:
    class SensitiveText(str):
        def __str__(self):
            raise AssertionError("must not invoke user code")

    class SensitiveFloat(float):
        def __str__(self):
            raise AssertionError("must not invoke user code")

    assert PlaneError("failed", metadata={"text": SensitiveText("hidden"),
        "float": SensitiveFloat(1.0)}).metadata == (
            ("float", "<unsupported>"), ("text", "<unsupported>"))


def test_oversized_key_is_rejected_even_when_outside_the_kept_entries() -> None:
    values = {f"a{index:02d}": index for index in range(MAX_METADATA_ENTRIES)}
    values["z" * (MAX_METADATA_KEY_LENGTH + 1)] = "hidden"
    with pytest.raises(ValueError, match="key exceeds its maximum length"):
        PlaneError("failed", metadata=values)
