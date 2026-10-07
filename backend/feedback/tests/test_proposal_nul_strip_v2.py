"""Regression test for the NUL-character bug in feedback/proposals.py (v2).

Follow-up to PR #354 / #314. The original fix only sanitized at the diff
stage but kept `existing_content` unsanitized, so when the proposal was
later applied via `_apply_unified_diff` the `-` and ` ` (context) lines
in the diff didn't match the on-disk file (which still had the NUL) and
`apply_unified_diff` raised:

    ValueError: diff context mismatch on '-' line

(see armstrongsam25's P1 review comment on #314, 2026-10-07 03:31:58Z).

This test asserts:
  1. `_make_unified_diff` returns the *sanitized* old text alongside the diff
     (not the original with NULs).
  2. The diff and the sanitized old text round-trip cleanly through
     `_apply_unified_diff` back to the new text.
  3. If the on-disk file is the *original* (with NUL), the diff is NOT
     applicable directly — you must use the sanitized old. This documents
     the contract that callers must use the returned sanitized version.
  4. Plain (NUL-free) inputs still work and round-trip correctly.
  5. Edge cases: empty old, identical old/new (empty diff).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from feedback import proposals as proposals_mod  # noqa: E402


def test_make_unified_diff_returns_sanitized_old_when_input_has_nul():
    """`_make_unified_diff` must return the sanitized old, not the original.

    Otherwise `_apply_unified_diff` would compare an NUL-bearing line to
    a U+FFFD-bearing diff line and raise on the first mismatch.
    """
    old_with_nul = "line1\nline\x00with_nul\nline3\n"
    new_clean = "line1\nreplacement\nline3\n"
    sanitized_old, diff = proposals_mod._make_unified_diff(old_with_nul, new_clean, "test.md")
    assert "\x00" not in sanitized_old, "sanitized_old must not contain NUL"
    assert "\ufffd" in sanitized_old, "sanitized_old must contain U+FFFD"
    # Caller must use sanitized_old, not the original
    assert sanitized_old == old_with_nul.replace("\x00", "\ufffd")


def test_make_unified_diff_plain_inputs_unchanged():
    """NUL-free inputs should be returned byte-identical and round-trip cleanly."""
    old = "alpha\nbeta\ngamma\n"
    new = "alpha\nBETA\ngamma\n"
    sanitized_old, diff = proposals_mod._make_unified_diff(old, new, "p.md")
    assert sanitized_old == old  # untouched
    applied = proposals_mod._apply_unified_diff(sanitized_old, diff)
    assert applied == new


def test_roundtrip_nul_in_old():
    """The whole point: sanitize -> diff -> apply must yield the new text."""
    old = "alpha\nli\x00ne_with_nul\nbeta\n"
    new = "alpha\nclean_line\nbeta\n"
    sanitized_old, diff = proposals_mod._make_unified_diff(old, new, "x.md")
    # Apply the diff to the SANITIZED old (not the original)
    applied = proposals_mod._apply_unified_diff(sanitized_old, diff)
    assert applied == new, (
        f"Roundtrip failed: applied={applied!r} != new={new!r}. "
        "This is the P1 bug armstrongsam25 reported."
    )


def test_roundtrip_nul_in_new():
    """NUL in the new text is also stripped and round-trips."""
    old = "alpha\nbeta\n"
    new = "alpha\nbe\x00ta\n"
    sanitized_old, diff = proposals_mod._make_unified_diff(old, new, "x.md")
    assert "\x00" not in diff  # DB-safe
    applied = proposals_mod._apply_unified_diff(sanitized_old, diff)
    # Applied gives the SANITIZED new (NUL → U+FFFD), not the original with NUL
    assert applied == "alpha\nbe\ufffdta\n"


def test_roundtrip_nul_in_both():
    """NUL in both inputs — fully symmetric, must still round-trip."""
    old = "a\x00a\nbbb\nccc\n"
    new = "aaa\nb\x00bb\nccc\n"
    sanitized_old, diff = proposals_mod._make_unified_diff(old, new, "x.md")
    applied = proposals_mod._apply_unified_diff(sanitized_old, diff)
    assert applied == "aaa\nb\ufffdbb\nccc\n"
    assert "\x00" not in applied


def test_cannot_apply_diff_to_original_with_nul():
    """Demonstrates the P1: using the *original* old (with NUL) breaks.

    This documents why callers MUST use the sanitized old returned by
    `_make_unified_diff`. If the maintainer ever reverts the function to
    return just the diff, this test will catch the contract violation.
    """
    original_old = "line1\nli\x00ne\nline3\n"
    new = "line1\nclean\nline3\n"
    sanitized_old, diff = proposals_mod._make_unified_diff(original_old, new, "x.md")
    # Applying the diff to the ORIGINAL (with NUL) must fail
    with pytest.raises(ValueError, match="diff context mismatch"):
        proposals_mod._apply_unified_diff(original_old, diff)
    # ...but applying to the SANITIZED old works
    applied = proposals_mod._apply_unified_diff(sanitized_old, diff)
    assert applied == new


def test_empty_old_roundtrip():
    """Empty old text should still produce a valid diff (full insertion)."""
    sanitized_old, diff = proposals_mod._make_unified_diff("", "first line\n", "new.md")
    assert sanitized_old == ""
    applied = proposals_mod._apply_unified_diff(sanitized_old, diff)
    assert applied == "first line\n"


def test_identical_inputs_yield_empty_diff():
    """Identical old/new — diff should be empty (whitespace-stripped empty)."""
    sanitized_old, diff = proposals_mod._make_unified_diff("same\n", "same\n", "x.md")
    assert sanitized_old == "same\n"
    # Empty diff — caller can skip this in the proposal pipeline
    assert diff.strip() == ""
