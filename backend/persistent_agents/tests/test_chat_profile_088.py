"""Tests for persistent_agents/chat_episode.py and research_input.py: chat response
parsing is closed, requests are fixed text-only and bounded, and unusable text or
non-pre-send transport codes are refused.
"""

import json

import pytest

from llm_config import research_profile as profile
from llm_config.research_profile import ResearchProfileUnavailable
from llm_config.tests.test_research_profile_088 import reply
from persistent_agents.chat_episode import (
    MAX_ANSWER_BYTES, build_chat_request, chat_result_value, parse_chat_response,
)

ANSWER = "A durable one-shot task keeps its identity and accounting until it settles."


def chat_reply(text=ANSWER, **changes):
    value = reply(**changes)
    value["choices"][0]["message"]["content"] = json.dumps({"version": 1, "text": text})
    return value


def test_chat_response_parser_is_closed():
    good = parse_chat_response(json.dumps(chat_reply()).encode(), status_code=200)
    assert good.accepted and good.text == ANSWER and good.usage.total_tokens == 120
    assert parse_chat_response(b"{}", status_code=200).disposition == "response_invalid"
    assert parse_chat_response(json.dumps(chat_reply()).encode(), status_code=503).usage is None
    extra = chat_reply()
    extra["choices"][0]["message"]["content"] = json.dumps({"version": 1, "text": ANSWER, "url": "x"})
    assert parse_chat_response(json.dumps(extra).encode(), status_code=200).disposition == "answer_invalid"
    long = chat_reply("y" * (MAX_ANSWER_BYTES + 1))
    parsed = parse_chat_response(json.dumps(long).encode(), status_code=200)
    assert parsed.text is None and parsed.usage is not None
    empty = chat_reply("   ")
    assert parse_chat_response(json.dumps(empty).encode(), status_code=200).text is None


def test_chat_request_is_fixed_text_only_and_bounded():
    request = build_chat_request("What is a one-shot task?")
    body = json.loads(request.body)
    assert request.passage_ids == ()
    assert body["model"] == profile.MODEL and body["stream"] is False and body["store"] is False
    assert body["max_completion_tokens"] == profile.OUTPUT_TOKENS
    assert body["messages"][1] == {"role": "user", "content": "What is a one-shot task?"}
    assert "passages" not in body["messages"][1]["content"]
    with pytest.raises(ResearchProfileUnavailable):
        build_chat_request("")
    with pytest.raises(ResearchProfileUnavailable):
        build_chat_request("x" * (profile.MAX_REQUEST_BYTES + 1))


@pytest.mark.parametrize("text", ["", "   ", "a" + chr(0) + "b", "y" * (MAX_ANSWER_BYTES + 1), None, 7])
def test_chat_result_value_refuses_unusable_text(text):
    with pytest.raises(ValueError):
        chat_result_value(text)


def test_unsent_attempt_requires_a_pre_send_transport_code():
    from persistent_agents.research_input import PRE_SEND_FAILURE_CODES, UnsentAttempt
    assert PRE_SEND_FAILURE_CODES == frozenset({"egress_blocked"})
    assert UnsentAttempt("egress_blocked").code == "egress_blocked"
    for code in ("unreachable", "deadline", "cleanup_uncertain", "child_failure", "authentication"):
        with pytest.raises(ValueError):
            UnsentAttempt(code)
