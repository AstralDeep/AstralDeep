"""Tests for shared/llm_text.py's strip_reasoning_markup: removes leaked Harmony channel
tokens and <think> blocks from provider output before it reaches chat, summaries, or
titles.
"""

from shared.llm_text import strip_reasoning_markup


def test_mangled_channel_tokens_from_field_report():
    # Exact malformed shape seen in production output
    raw = "<|channel>thought\n<channel|>Here is your weather dashboard for Lexington, KY:"
    assert strip_reasoning_markup(raw) == "Here is your weather dashboard for Lexington, KY:"


def test_canonical_harmony_framing():
    raw = ("<|channel|>analysis<|message|>The user wants weather. I should "
           "call the tool.<|end|><|start|>assistant<|channel|>final<|message|>"
           "Here's the current weather in **Lexington**.")
    out = strip_reasoning_markup(raw)
    assert out == "Here's the current weather in **Lexington**."


def test_think_block_removed():
    raw = "<think>step 1… step 2…</think>The answer is 42."
    assert strip_reasoning_markup(raw) == "The answer is 42."
    raw2 = "<thinking>hmm</thinking>Done."
    assert strip_reasoning_markup(raw2) == "Done."


def test_clean_text_passthrough():
    for text in (
        "Plain reply with no markup.",
        "Math: 3 < 5 and 7 > 2.",
        "| Metric | Value |\n|---|---|\n| Temp | 83.7 |",
        "Code: `a < b ? x : y` and <div> in prose.",
        "",
    ):
        assert strip_reasoning_markup(text) == text


def test_non_string_passthrough():
    assert strip_reasoning_markup(None) is None
    assert strip_reasoning_markup(42) == 42


def test_thought_only_content_falls_back_to_token_stripped_text():
    raw = "<|channel|>thought<|message|>only reasoning, no final channel"
    out = strip_reasoning_markup(raw)
    assert out
    assert "<|" not in out and "|>" not in out


def test_stray_tokens_stripped():
    raw = "Result ready.<|end|><|return|>"
    assert strip_reasoning_markup(raw) == "Result ready."


def test_plain_angle_words_untouched():
    assert strip_reasoning_markup("<channel> tuning is fun") == "<channel> tuning is fun"


def test_idempotent():
    raw = "<|channel>thought\n<channel|>Final text."
    once = strip_reasoning_markup(raw)
    assert strip_reasoning_markup(once) == once
