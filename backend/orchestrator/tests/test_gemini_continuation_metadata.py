"""Gemini tool-continuation metadata survives streamed tool-call assembly (#347)."""
from types import SimpleNamespace

from backend.orchestrator.orchestrator import (
    Orchestrator,
    _bounded_extra_content,
)

SIG = "opaque-signature-bytes=="


def _delta(extra=None):
    return SimpleNamespace(extra_content=extra)


def test_allowlisted_signature_is_kept_verbatim():
    got = _bounded_extra_content(_delta({"google": {"thought_signature": SIG, "x": 1}, "other": 2}))
    assert got == {"google": {"thought_signature": SIG}}


def test_malformed_or_oversized_metadata_is_dropped():
    assert _bounded_extra_content(_delta(None)) is None
    assert _bounded_extra_content(_delta("nope")) is None
    assert _bounded_extra_content(_delta({"google": "x"})) is None
    assert _bounded_extra_content(_delta({"google": {"thought_signature": 5}})) is None
    assert _bounded_extra_content(_delta({"google": {"thought_signature": ""}})) is None
    assert _bounded_extra_content(_delta({"google": {"thought_signature": "a" * 9000}})) is None


def test_first_call_only_signature_in_parallel_calls_is_not_invented():
    acc = {
        0: {"id": "c0", "name": "a", "arguments": "{}", "extra_content": {"google": {"thought_signature": SIG}}},
        1: {"id": "c1", "name": "b", "arguments": "{}"},
    }
    msg = Orchestrator._assemble_streamed_message("", acc)
    dumped = [tc.model_dump() if hasattr(tc, "model_dump") else vars(tc) for tc in msg.tool_calls]
    assert dumped[0]["extra_content"] == {"google": {"thought_signature": SIG}}
    assert "extra_content" not in dumped[1] or dumped[1]["extra_content"] is None
    assert dumped[0]["function"]["arguments"] == "{}"
