"""Tests for persistent_agents/chat_episode.py and research_input.py: chat response
parsing is closed, requests are fixed text-only and bounded, and unusable text or
non-pre-send transport codes are refused.
"""

import hashlib
import json
from dataclasses import replace

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


LOCAL = profile.LOCAL_PROFILE.bind(
    model="qualified-local-test", base_url="http://127.0.0.1:1234/v1")


def test_default_chat_request_and_result_preserve_exact_contract():
    request = build_chat_request("What is a one-shot task?")
    assert hashlib.sha256(request.body).hexdigest() == (
        "5de0d0f47aca9580818c53806a6e4696b5211f3efd8ae0f719adb84c1e89f72a")
    assert build_chat_request("What is a one-shot task?",
        profile_spec=profile.OPENAI_PROFILE).body == request.body
    assert chat_result_value(ANSWER) == {
        "version": 1, "profile": "openai-chat-turn/v1", "text": ANSWER}


def test_selected_local_chat_request_uses_only_the_declared_bound_profile():
    request = build_chat_request("What is a one-shot task?", profile_spec=LOCAL)
    body = json.loads(request.body)
    assert body["model"] == LOCAL.model
    assert body["max_completion_tokens"] == 1024
    assert "tools" not in body and request.passage_ids == ()
    assert body["messages"][1]["content"] == "What is a one-shot task?"
    assert chat_result_value(ANSWER, profile_spec=LOCAL) == {
        "version": 1, "profile": "local-chat-turn/v1", "text": ANSWER}


@pytest.mark.parametrize("tokens,completion,expected", [
    (31744, 1024, "answer"), (32768, 1, "profile_exceeded"),
    (32769, 1, "profile_exceeded"),
    (10, 1025, "profile_exceeded"), (32769, 1025, "profile_exceeded"),
])
def test_selected_local_chat_parser_applies_local_model_and_accounting(
        tokens, completion, expected):
    value = chat_reply(model=LOCAL.model)
    value["usage"] = {"prompt_tokens": tokens, "completion_tokens": completion,
                      "total_tokens": tokens + completion}
    parsed = parse_chat_response(json.dumps(value).encode(), status_code=200,
                                 profile_spec=LOCAL)
    assert parsed.disposition == expected
    assert parsed.usage.prompt_tokens == tokens
    assert parsed.usage.completion_tokens == completion
    assert parsed.accepted is (expected == "answer")


def test_selected_local_chat_denies_default_or_changed_model_with_truthful_usage():
    value = chat_reply()
    parsed = parse_chat_response(json.dumps(value).encode(), status_code=200,
                                 profile_spec=LOCAL)
    assert parsed.disposition == "answer_invalid" and parsed.usage.total_tokens == 120
    value["model"] = LOCAL.model
    value["usage"]["prompt_tokens"] = True
    parsed = parse_chat_response(json.dumps(value).encode(), status_code=200,
                                 profile_spec=LOCAL)
    assert parsed.disposition == "usage_unknown" and parsed.usage is None
    assert not parsed.accepted


@pytest.mark.parametrize("selected", [
    None, "standard_local", profile.LOCAL_PROFILE,
    replace(LOCAL, context_tokens=8192), replace(LOCAL, output_tokens=2048),
    replace(LOCAL, reserved_milliseconds=65000),
])
def test_chat_contract_refuses_unbound_unknown_and_forged_profiles(selected):
    with pytest.raises(ResearchProfileUnavailable):
        build_chat_request("hello", profile_spec=selected)
    with pytest.raises(ResearchProfileUnavailable):
        parse_chat_response(json.dumps(chat_reply()).encode(), status_code=200,
                            profile_spec=selected)
    with pytest.raises(ResearchProfileUnavailable):
        chat_result_value(ANSWER, profile_spec=selected)
