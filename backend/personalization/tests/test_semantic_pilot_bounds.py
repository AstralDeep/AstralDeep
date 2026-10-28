"""Tests for the pilot bounds: construction coherence, every refusal branch, and the
token estimate contract.
"""

from __future__ import annotations

import pytest

from personalization.semantic_pilot.bounds import (
    Bounds,
    BoundsExceeded,
    estimate_tokens,
)


def test_estimate_tokens_is_monotonic_and_positive():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("a" * 400) == 100


@pytest.mark.parametrize("kwargs,fragment", [
    ({"max_snapshot_rows": 0}, "max_snapshot_rows"),
    ({"max_row_chars": 0}, "max_row_chars"),
    ({"max_query_chars": 0}, "max_query_chars"),
    ({"max_snapshot_bytes": 0}, "max_snapshot_bytes"),
    ({"max_batch_rows": 0}, "max_batch_rows"),
    ({"max_provider_calls": 0}, "max_provider_calls"),
    ({"max_total_tokens": 0}, "max_total_tokens"),
    ({"max_candidates": 0}, "max_candidates"),
    ({"max_report_bytes": 0}, "max_report_bytes"),
    ({"max_retries": -1}, "max_retries"),
    ({"max_batch_rows": 64, "max_snapshot_rows": 32}, "max_batch_rows must not exceed"),
    ({"max_candidates": 64, "max_snapshot_rows": 32}, "max_candidates must not exceed"),
    ({"runtime_deadline_seconds": 0}, "runtime_deadline_seconds"),
    ({"runtime_deadline_seconds": -5}, "runtime_deadline_seconds"),
])
def test_bounds_construction_refuses_incoherent_values(kwargs, fragment):
    with pytest.raises(ValueError, match=fragment):
        Bounds(**kwargs)


def test_bounds_deadline_accepted_when_positive():
    bounds = Bounds(runtime_deadline_seconds=1.5)
    assert bounds.runtime_deadline_seconds == 1.5


def test_validate_query_refuses_empty_and_non_string():
    bounds = Bounds()
    with pytest.raises(BoundsExceeded, match="query is empty"):
        bounds.validate_query("   ")
    with pytest.raises(BoundsExceeded, match="query is empty"):
        bounds.validate_query(None)


def test_validate_snapshot_refuses_non_string_value():
    with pytest.raises(BoundsExceeded, match="not a string"):
        Bounds().validate_snapshot([{"id": "x", "value": 42}])


def test_validate_snapshot_byte_bound():
    bounds = Bounds(max_snapshot_bytes=20)
    with pytest.raises(BoundsExceeded, match="max_snapshot_bytes"):
        bounds.validate_snapshot([
            {"id": f"r{i}", "value": "v" * 10} for i in range(3)
        ])


def test_validate_batch_and_candidates_bounds():
    bounds = Bounds(max_batch_rows=2, max_candidates=1)
    with pytest.raises(BoundsExceeded, match="max_batch_rows"):
        bounds.validate_batch(["a", "b", "c"])
    with pytest.raises(BoundsExceeded, match="max_candidates"):
        bounds.validate_candidates(2)
    bounds.validate_batch(["a", "b"])
    bounds.validate_candidates(1)


def test_validate_call_count_and_report_bytes():
    bounds = Bounds(max_provider_calls=2, max_report_bytes=8)
    with pytest.raises(BoundsExceeded, match="max_provider_calls"):
        bounds.validate_call_count(3)
    with pytest.raises(BoundsExceeded, match="max_report_bytes"):
        bounds.validate_report_bytes(100)
    bounds.validate_call_count(2)
