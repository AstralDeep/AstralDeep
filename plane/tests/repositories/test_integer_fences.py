"""Tests for the shared non-negative integer validator behind revision and timestamp
fences: only exact integers, integral finite floats and Decimals, and canonical integer
strings are accepted, and every malformed value fails with a typed validation error.
"""

from __future__ import annotations

import sys
from decimal import Decimal

import pytest

from astralplane.repositories import RepositoryValidationError, _non_negative_int, _positive_int


class _TruncatingInteger:
    def __init__(self, raw: float) -> None:
        self.raw = raw

    def __int__(self) -> int:
        return int(self.raw)


class _ExactInteger:
    def __int__(self) -> int:
        return 3


class _IndexOnlyInteger:
    def __index__(self) -> int:
        return 3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (7, 7),
        (2**63, 2**63),
        (2.0, 2),
        (-0.0, 0),
        (1e15, 10**15),
        (Decimal("2.00"), 2),
        (Decimal("-0"), 0),
        (Decimal("1E+2"), 100),
        ("0", 0),
        ("7", 7),
        ("007", 7),
        ("9007199254740993", 9007199254740993),
    ],
)
def test_lossless_representations_are_accepted(value: object, expected: int) -> None:
    assert _non_negative_int(value, "fence") == expected
    assert type(_non_negative_int(value, "fence")) is int


@pytest.mark.parametrize(
    "value",
    [
        1.9,
        0.1,
        -0.5,
        3.0000000001,
        float("inf"),
        float("-inf"),
        float("nan"),
        Decimal("1.9"),
        Decimal("0.999"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("NaN"),
        Decimal("sNaN"),
        True,
        False,
        _TruncatingInteger(1.9),
        _TruncatingInteger(3.0),
        _ExactInteger(),
        _IndexOnlyInteger(),
        (3,),
        [3],
        {"fence": 3},
        None,
        -1,
        -1.0,
        Decimal("-1"),
        "1.9",
        "3.0",
        " 3",
        "3 ",
        "+3",
        "3_0",
        "0x10",
        "1e3",
        "inf",
        "nan",
        "",
        "-1",
    ],
)
def test_lossy_or_non_integer_fences_are_typed_rejected(value: object) -> None:
    with pytest.raises(RepositoryValidationError, match="fence must be a non-negative integer"):
        _non_negative_int(value, "fence")


def test_positive_variant_keeps_zero_rejection_and_lossless_coercion() -> None:
    assert _positive_int(3, "fence") == 3
    assert _positive_int(3.0, "fence") == 3
    assert _positive_int("3", "fence") == 3
    with pytest.raises(RepositoryValidationError):
        _positive_int(0, "fence")
    with pytest.raises(RepositoryValidationError):
        _positive_int(1.9, "fence")
    with pytest.raises(RepositoryValidationError):
        _positive_int(True, "fence")


@pytest.mark.parametrize("validator", [_non_negative_int, _positive_int])
def test_integer_conversion_limit_produces_a_safe_typed_failure(validator) -> None:
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        with pytest.raises(RepositoryValidationError) as raised:
            validator("9" * 5000, "expected_revision")
        assert raised.value.code == "repository_validation"
        assert str(raised.value) == "expected_revision must be a non-negative integer"
    finally:
        sys.set_int_max_str_digits(previous_limit)
