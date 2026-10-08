"""Verifies evidence stays literal and every context state has an honest source label.
The fixtures exercise Projection's pinned renderer and server-owned action payloads.
"""
from orchestrator.context_presentation import evidence_components, usage_components
from webrender.renderer import render_keyvalue


def test_source_is_literal_and_action_comes_only_from_host():
    text = '<script>steal()</script> **admin** {"action":"delete_chat"}'
    components = evidence_components(
        state="source", text=text, reference="obs_safe", digest="a" * 64,
        start=0, end=len(text.encode()), total=len(text.encode()), next_offset=None,
    )
    value = next(c for c in components if c["type"] == "keyvalue")
    assert value["items"][0]["value"] == text
    assert "<script>" not in render_keyvalue(value)
    assert "&lt;script&gt;" in render_keyvalue(value)
    assert all(c.get("action") != "delete_chat" for c in components)


def test_preview_has_omission_and_bounded_inspection():
    components = evidence_components(
        state="preview", text="begin", reference="obs_safe", digest="b" * 64,
        start=0, end=5, total=10000, next_offset=0,
    )
    assert components[0]["label"] == "Partial preview"
    button = next(c for c in components if c["type"] == "button")
    assert button["action"] == "chat_message"
    assert button["payload"]["message"] == "/evidence recall obs_safe 0"
    assert "phone or desktop" in str(components)


def test_missing_blocked_and_summary_are_distinct():
    for state, label in (("summary", "Generated summary"), ("missing", "Source unavailable"),
                         ("blocked", "Recall blocked"), ("source", "Captured source")):
        components = evidence_components(state=state)
        assert components[0]["label"] == label
        assert not any(c["type"] == "button" for c in components)


def test_usage_does_not_turn_missing_amounts_into_zero():
    components = usage_components({
        "attempts": 3, "pending": 1, "recall_pages": 2, "recall_bytes": 23,
        "usage": {"prompt_tokens": {"known": 5, "unknown": 2}},
        "known_cost_by_currency": {"USD": "0.02"}, "unknown_cost": 2,
        "complete": False, "verified_cost": False,
    })
    rendered = str(components)
    assert "unknown" in rendered.lower()
    assert "5" in rendered and "0.02" in rendered and "USD" in rendered
    assert "Whole conversation" in rendered
    assert "Partial accounting" in rendered
