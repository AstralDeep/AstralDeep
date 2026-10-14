"""Tests for llm_config/tool_continuation.py: Gemini profile binding, allowlisted
bounded thought-signature validation, fragment merging, and outgoing-wire checks.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm_config.tool_continuation import (  # noqa: E402
    GEMINI_OPENAI_COMPAT_BASE_URL,
    MAX_THOUGHT_SIGNATURE_BYTES,
    ContinuationStateError,
    continuation_signature,
    is_gemini_continuation_profile,
    merge_continuation_signature,
    validate_continuation_wire,
)
from llm_config.providers import get_preset, resolve_base_url  # noqa: E402


class TestProfileBinding:
    def test_preset_base_url_binds_to_gemini_profile(self):
        preset = get_preset("gemini")
        assert preset is not None
        assert resolve_base_url("gemini", "https://attacker.example/v1") == (
            GEMINI_OPENAI_COMPAT_BASE_URL)
        assert is_gemini_continuation_profile(preset.base_url)

    def test_trailing_slash_and_whitespace_tolerated(self):
        assert is_gemini_continuation_profile(GEMINI_OPENAI_COMPAT_BASE_URL + "/")
        assert is_gemini_continuation_profile(
            "  " + GEMINI_OPENAI_COMPAT_BASE_URL + "  ")

    def test_other_profiles_do_not_bind(self):
        for url in (
            "https://api.openai.com/v1",
            "https://api.anthropic.com/v1",
            "http://localhost:11434/v1",
            "https://generativelanguage.googleapis.com/v1beta",
            "https://evil.example/generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com/v1beta/openai.evil.example",
        ):
            assert not is_gemini_continuation_profile(url), url

    def test_custom_endpoint_is_not_silently_rewritten_or_bound(self):
        assert resolve_base_url("custom", "https://my-proxy.example/v1") == (
            "https://my-proxy.example/v1")
        assert not is_gemini_continuation_profile("https://my-proxy.example/v1")

    def test_non_string_base_url_does_not_bind(self):
        for value in (None, 123, b"https://x", ["x"]):
            assert not is_gemini_continuation_profile(value)


class TestSignatureValidation:
    def test_absent_metadata_is_not_signature_state(self):
        for value in (None, {}, {"vendor": {"x": 1}}, {"google": {}}):
            assert continuation_signature(value) is None

    def test_valid_signature_returns_original_bytes(self):
        sig = "R/thought+A/B9==" + "é€"
        out = continuation_signature({"google": {"thought_signature": sig}})
        assert out == sig
        assert out.encode("utf-8") == sig.encode("utf-8")

    def test_unknown_provider_fields_ignored(self):
        extra = {"google": {"thought_signature": "s", "other": "x"},
                 "vendor": {"deep": [1, 2]}}
        assert continuation_signature(extra) == "s"

    def test_non_dict_extra_content_is_malformed(self):
        for value in ("sig", 5, [], ("g",), True):
            with pytest.raises(ContinuationStateError):
                continuation_signature(value)

    def test_non_dict_google_section_is_malformed(self):
        with pytest.raises(ContinuationStateError):
            continuation_signature({"google": "sig"})

    def test_non_string_signature_is_malformed(self):
        for value in (5, {"a": 1}, ["s"], True, b"sig"):
            with pytest.raises(ContinuationStateError):
                continuation_signature({"google": {"thought_signature": value}})

    def test_empty_signature_is_malformed(self):
        with pytest.raises(ContinuationStateError):
            continuation_signature({"google": {"thought_signature": ""}})

    def test_oversized_signature_is_malformed(self):
        oversized = "s" * (MAX_THOUGHT_SIGNATURE_BYTES + 1)
        with pytest.raises(ContinuationStateError):
            continuation_signature({"google": {"thought_signature": oversized}})

    def test_signature_at_bound_is_accepted(self):
        bounded = "s" * MAX_THOUGHT_SIGNATURE_BYTES
        assert continuation_signature(
            {"google": {"thought_signature": bounded}}) == bounded


class TestFragmentMerging:
    def test_first_valid_fragment_is_kept(self):
        assert merge_continuation_signature(
            None, {"google": {"thought_signature": "a"}}) == "a"

    def test_identical_duplicate_fragment_is_stable(self):
        assert merge_continuation_signature(
            "a", {"google": {"thought_signature": "a"}}) == "a"

    def test_contradictory_duplicate_fragments_raise(self):
        with pytest.raises(ContinuationStateError):
            merge_continuation_signature(
                "a", {"google": {"thought_signature": "b"}})

    def test_fragment_without_signature_keeps_existing_state(self):
        assert merge_continuation_signature("a", None) == "a"
        assert merge_continuation_signature("a", {}) == "a"
        assert merge_continuation_signature("a", {"vendor": {}}) == "a"

    def test_malformed_fragment_raises_without_state_change(self):
        with pytest.raises(ContinuationStateError):
            merge_continuation_signature(
                "a", {"google": {"thought_signature": 9}})


class TestWireValidation:
    def _assistant_msg(self, tool_calls):
        return types.SimpleNamespace(
            role="assistant", content=None, tool_calls=tool_calls)

    def test_object_and_dict_forms_validate(self):
        tc = types.SimpleNamespace(
            id="c", type="function",
            function=types.SimpleNamespace(name="f", arguments="{}"),
            extra_content={"google": {"thought_signature": "s"}})
        validate_continuation_wire([
            {"role": "user", "content": "hi"},
            self._assistant_msg([tc]),
            {"role": "tool", "tool_call_id": "c", "content": "r"},
        ])
        validate_continuation_wire([
            {"role": "assistant", "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "f", "arguments": "{}"},
                 "extra_content": {"google": {"thought_signature": "s"}}}]},
        ])

    def test_non_assistant_and_toolless_messages_ignored(self):
        weird = types.SimpleNamespace(role="tool", tool_calls=["junk"])
        validate_continuation_wire([
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "no tools"},
            weird,
        ])

    def test_malformed_state_on_wire_raises(self):
        for extra in ("sig", {"google": 1}, {"google": {"thought_signature": 5}},
                      {"google": {"thought_signature": ""}},
                      {"google": {"thought_signature": "s" * (MAX_THOUGHT_SIGNATURE_BYTES + 1)}}):
            tc = {"id": "c", "type": "function",
                  "function": {"name": "f", "arguments": "{}"},
                  "extra_content": extra}
            with pytest.raises(ContinuationStateError):
                validate_continuation_wire([
                    {"role": "assistant", "tool_calls": [tc]}])

    def test_missing_signature_is_not_a_validation_failure(self):
        validate_continuation_wire([
            {"role": "assistant", "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}]},
        ])

    def test_non_list_input_is_noop(self):
        validate_continuation_wire(None)
        validate_continuation_wire("not-a-list")
