"""Tests for voice_coordinator.py's approved announcement phrases: bounded,
concise terminal and rejection text.
"""

from __future__ import annotations


import pytest

from orchestrator.voice_coordinator import APPROVED_PHRASE_TEXT


@pytest.mark.parametrize(
    ("phrase_key", "expected_text"),
    (
        ("sensitive_result_ready", "Private result ready."),
        ("request_busy", "Please try again later."),
        ("conversation_unavailable", "Choose another chat."),
        ("request_retry_needed", "Please try that again."),
        ("request_not_understood", "Please say that again."),
    ),
)
def test_bounded_terminal_and_rejection_phrases_remain_concise(
    phrase_key: str,
    expected_text: str,
) -> None:
    assert APPROVED_PHRASE_TEXT[phrase_key] == expected_text

