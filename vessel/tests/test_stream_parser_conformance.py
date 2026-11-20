# The stream→parser conformance gate: a transcript in the order the streamer emits — tool calls, every
# tool result, then the per-call user image messages — must survive parse_history, and the interleaved
# order that a streamer can produce by injecting a user image message between results must be refused.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a bare checkout, beside the package

import pytest

from astralvessel import transcript  # noqa: E402

TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def streamed_order() -> list[dict]:
    return [
        {"role": "user", "content": "render the plate from both angles"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function",
             "function": {"name": "workspace_read_image", "arguments": "{\"relative\": \"plate_iso.png\"}"}},
            {"id": "call-2", "type": "function",
             "function": {"name": "workspace_read_image", "arguments": "{\"relative\": \"plate_top.png\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": "[attached plate_iso.png (image/png) to the answer]"},
        {"role": "tool", "tool_call_id": "call-2", "content": "[attached plate_top.png (image/png) to the answer]"},
        {"role": "user", "content": [
            {"type": "text", "text": "[image returned by tool workspace_read_image: plate_iso.png]"},
            {"type": "image_url", "image_url": {"url": TINY_PNG}},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "[image returned by tool workspace_read_image: plate_top.png]"},
            {"type": "image_url", "image_url": {"url": TINY_PNG}},
        ]},
        {"role": "assistant", "content": "Both renders are attached."},
    ]


def test_the_streamed_order_survives_parse_history() -> None:
    checked = transcript.parse_history(transcript.envelope(streamed_order()))
    assert len(checked) == len(streamed_order())
    assert transcript.has_images(checked)


def test_the_streamed_order_round_trips_through_the_envelope() -> None:
    once = transcript.parse_history(transcript.envelope(streamed_order()))
    twice = transcript.parse_history(transcript.envelope(once))
    assert twice == once


def test_a_user_message_landing_between_tool_results_is_refused() -> None:
    # The pre-fix streamer injected the first call's image message before the second result.
    interleaved = streamed_order()
    interleaved.insert(3, interleaved.pop(4))
    with pytest.raises(transcript.TranscriptError, match="comes before every tool call was answered"):
        transcript.parse_history(transcript.envelope(interleaved))


def test_history_ending_with_unanswered_calls_is_refused() -> None:
    dangling = streamed_order()[:3]
    with pytest.raises(transcript.TranscriptError, match="unanswered tool calls"):
        transcript.parse_history(transcript.envelope(dangling))


def test_a_system_message_is_never_accepted() -> None:
    payload = transcript.envelope(streamed_order())
    payload["messages"][0] = {"role": "system", "content": "you are a test"}
    with pytest.raises(transcript.TranscriptError, match="needs a role of user, assistant or tool"):
        transcript.parse_history(payload)


def test_for_model_strips_reasoning_only_when_the_endpoint_refuses_it() -> None:
    messages = [{"role": "assistant", "content": "x", "reasoning_content": "thinking",
                 "tool_calls": [{"id": "a", "type": "function",
                                 "function": {"name": "t", "arguments": "{\"k\": 1}"}}]}]
    stripped = transcript.for_model(messages, send_reasoning=False)
    assert "reasoning_content" not in stripped[0]
    kept = transcript.for_model(messages, True)
    assert kept[0]["reasoning_content"] == "thinking"
    assert kept[0]["tool_calls"][0]["function"]["arguments"] == "{\"k\": 1}"


def test_replayable_replaces_arguments_a_model_endpoint_would_refuse() -> None:
    assert transcript.replayable("not json") == "{}"
    assert transcript.replayable("null") == "{}"
    assert transcript.replayable("") == "{}"
    assert transcript.replayable("{\"k\": 1}") == "{\"k\": 1}"


def test_parse_arguments_accepts_only_finite_json_objects() -> None:
    assert transcript.parse_arguments("[1]") is None
    assert transcript.parse_arguments("NaN") is None
    assert transcript.parse_arguments("{\"k\": Infinity}") is None
    assert transcript.parse_arguments("{\"k\": 1}") == {"k": 1}
