"""Tests for llm_config/research_profile.py's build_request/parse_response: the fixed
request body never substitutes or truncates, and usage parsing never invents billing
from an error body or treats missing usage as zero.
"""

import ast
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from llm_config import research_profile as profile
from persistent_agents.research_result import (
    build_page_result,
    page_passages,
    retain_page_observation,
)
from persistent_agents.runtime_values import digest

ACTION = "d2f39b41-b686-4b8f-92e0-70d0d743d6e0"


def retained(text="Version 088 adds bounded background research. " * 30):
    return retain_page_observation(
        {
            "version": 1,
            "requested_url": "https://example.org/releases",
            "final_url": "https://example.org/releases",
            "retrieved_at": "2026-09-12T12:00:00+00:00",
            "media_type": "text/plain",
            "extraction_profile": "plain_text_v1",
            "title": "Releases",
            "text": text,
            "body_complete": True,
            "extraction_complete": True,
            "excerpt_complete": True,
            "redacted": False,
        },
        source_action_id=ACTION,
        redacted=False,
    )


def reply(selection=None, **changes):
    return {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion",
        "created": 1789214400,
        "model": profile.MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "logprobs": None,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "version": 1,
                            "passage_ids": ["p001"] if selection is None else selection,
                        }
                    ),
                    "refusal": None,
                    "annotations": [],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 80, "audio_tokens": 0},
            "completion_tokens_details": {
                "reasoning_tokens": 3,
                "audio_tokens": 0,
                "accepted_prediction_tokens": 4,
                "rejected_prediction_tokens": 5,
            },
        },
        "system_fingerprint": None,
        "service_tier": "default",
        **changes,
    }


def parse(value, **kwargs):
    return profile.parse_response(
        json.dumps(value).encode(),
        status_code=200,
        passage_ids=("p001", "p002"),
        **kwargs,
    )


def test_fixed_whole_request_is_immutable_exact_and_never_substitutes():
    source = retained()
    request = profile.build_request("Find the version changes.", source)
    body = json.loads(request.body)
    assert set(body) == {
        "model",
        "stream",
        "store",
        "n",
        "max_completion_tokens",
        "response_format",
        "messages",
    }
    assert {k: body[k] for k in body if k != "messages"} == {
        "model": "gpt-4o-mini-2024-07-18",
        "stream": False,
        "store": False,
        "n": 1,
        "max_completion_tokens": 1024,
        "response_format": {"type": "json_object"},
    }
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    content = json.loads(body["messages"][1]["content"])
    assert content == {
        "task": "Find the version changes.",
        "passages": page_passages(source),
    }
    assert request.passage_ids == tuple(p["id"] for p in content["passages"])
    assert "Find the version" not in repr(request)
    assert profile.ENDPOINT == "https://api.openai.com/v1/chat/completions"
    assert profile.RESERVED_TOKENS == 129024 and profile.RESERVED_MILLISECONDS == 65000
    assert len(request.body) <= profile.MAX_REQUEST_BYTES
    with pytest.raises(FrozenInstanceError):
        request.body = b"changed"
    body["messages"][1]["content"] = "mutated"
    assert request == profile.build_request("Find the version changes.", source)


@pytest.mark.parametrize(
    "instruction", [None, True, "", " \n", "\ud800", "x" * 32769, "界" * 10923]
)
def test_instruction_refuses_instead_of_truncating(instruction):
    with pytest.raises(
        profile.ResearchProfileUnavailable, match="^research_profile_unavailable$"
    ):
        profile.build_request(instruction, retained())


def test_entire_encoded_body_limit_counts_escaped_bytes_not_characters():
    source = retained()
    with pytest.raises(profile.ResearchProfileUnavailable):
        profile.build_request("\x01" * 20000, source)
    result = profile.build_request("界" * 10000, source)
    assert (
        "界" * 10000
        in json.loads(json.loads(result.body)["messages"][1]["content"])["task"]
    )
    assert len(result.body) <= profile.MAX_REQUEST_BYTES


@pytest.mark.parametrize("source", [{}, None, {"text": "not a retained observation"}])
def test_unknown_or_unretained_source_refuses(source):
    with pytest.raises(profile.ResearchProfileUnavailable):
        profile.build_request("Find evidence", source)


@pytest.mark.parametrize(
    "selection,disposition",
    [(["p002", "p001"], "evidence"), ([], "insufficient_evidence")],
)
def test_exact_selection_drives_batch_a_builder_without_new_claims(
    selection, disposition
):
    source = retained()
    result = parse(reply(selection))
    assert result.passage_ids == tuple(selection)
    assert result.disposition == disposition
    assert result.usage == profile.ResearchUsage(100, 20, 120)
    output = build_page_result(
        source,
        list(result.passage_ids),
        source_action_id=ACTION,
        source_result_digest=digest(source),
    )
    assert output["disposition"] == disposition
    assert [p["id"] for p in output["passages"]] == selection
    assert all(p in page_passages(source) for p in output["passages"])


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        "[]",
        "null",
        "{}",
        '{"version":true,"passage_ids":[]}',
        '{"version":2,"passage_ids":[]}',
        '{"version":1,"passage_ids":[],"answer":"invented"}',
        '{"version":1,"version":1,"passage_ids":[]}',
        '{"version":1,"passage_ids":["p001","p001"]}',
        '{"version":1,"passage_ids":["P001"]}',
        '{"version":1,"passage_ids":["p099"]}',
        '{"version":1,"passage_ids":[true]}',
        '{"version":1,"passage_ids":null}',
        '{"version":1,"passage_ids":["https://example.org"]}',
        '{"version":1,"passage_ids":[]}' + " " * 8192,
        '{"version":1,"passage_ids":[],"extra":NaN}',
        '{"version":1,"passage_ids":["p001","p002","p003","p004","p005","p006","p007","p008","p009"]}',
    ],
)
def test_invalid_selection_preserves_known_usage(content):
    value = reply()
    value["choices"][0]["message"]["content"] = content
    result = parse(value)
    assert result.passage_ids is None and result.disposition == "answer_invalid"
    assert result.usage == profile.ResearchUsage(100, 20, 120)
    assert content not in repr(result)


@pytest.mark.parametrize(
    "domain",
    [
        None,
        [],
        (),
        ("p001", "p001"),
        (True,),
        ("P001",),
        tuple(f"p{i:03}" for i in range(65)),
    ],
)
def test_selection_domain_itself_is_closed(domain):
    with pytest.raises(profile.ResearchProfileUnavailable):
        profile.parse_selection('{"version":1,"passage_ids":[]}', passage_ids=domain)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "gpt-4o-mini"},
        {"object": "chat.completion.chunk"},
        {"created": True},
        {"created": -1},
        {"id": ""},
        {"id": "x" * 257},
        {"new_field": "not silently accepted"},
        {"choices": []},
        {"choices": [None]},
        {"choices": {}},
        {"choices": [reply()["choices"][0]] * 2},
        {"service_tier": {}},
        {"system_fingerprint": 1},
    ],
)
def test_envelope_denial_differs_from_unusable_completion_answer(change):
    result = parse(reply(**change))
    if set(change) <= {"model", "choices"}:
        assert result.usage == profile.ResearchUsage(100, 20, 120)
        assert result.passage_ids is None and result.disposition == "answer_invalid"
    else:
        assert result == profile.ResearchResponse(None, None, "response_invalid")


@pytest.mark.parametrize(
    "field,value",
    [
        ("index", True),
        ("index", 1),
        ("finish_reason", "length"),
        ("finish_reason", "tool_calls"),
        ("logprobs", {}),
        ("unexpected", 1),
        ("message", None),
    ],
)
def test_choice_denials_keep_usage(field, value):
    document = reply()
    document["choices"][0][field] = value
    assert parse(document) == profile.ResearchResponse(
        profile.ResearchUsage(100, 20, 120), None, "answer_invalid"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "tool"),
        ("content", None),
        ("content", []),
        ("refusal", "sensitive refusal"),
        ("tool_calls", []),
        ("function_call", {}),
        ("audio", {}),
        ("annotations", [{"url": "hidden"}]),
    ],
)
def test_nontext_or_refused_message_keeps_usage(field, value):
    document = reply()
    document["choices"][0]["message"][field] = value
    result = parse(document)
    assert result.usage.total_tokens == 120 and result.disposition == "answer_invalid"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\xff",
        b"null",
        b"[]",
        b"{} {}",
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":1,"x":2}',
        b'{"x":"\\ud800"}',
        b'{"x":9223372036854775808}',
        b"[" * 4096 + b"]" * 4096,
        b'{"x":' + b"[" * 17 + b"0" + b"]" * 17 + b"}",
        json.dumps({"x": list(range(10001))}).encode(),
        b" " * (profile.MAX_RESPONSE_BYTES + 1),
    ],
)
def test_invalid_whole_document_has_unknown_usage_no_synthetic_zero(body):
    assert profile.parse_response(
        body, status_code=200, passage_ids=("p001",)
    ) == profile.ResearchResponse(None, None, "response_invalid")


@pytest.mark.parametrize("status", [None, True, 201, 400, 429, 500])
def test_non_success_cannot_invent_billing_from_an_error_body(status):
    result = profile.parse_response(
        json.dumps(reply()).encode(), status_code=status, passage_ids=("p001",)
    )
    assert result == profile.ResearchResponse(None, None, "response_invalid")


@pytest.mark.parametrize(
    "change",
    [
        None,
        {},
        {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": 1.0, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 4},
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "unknown": 4},
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "prompt_tokens_details": [],
        },
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "completion_tokens_details": {"rejected_prediction_tokens": 3},
        },
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "completion_tokens_details": {"reasoning_tokens": True},
        },
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "completion_tokens_details": {"unknown": 0},
        },
    ],
)
def test_invalid_usage_cannot_become_evidence(change):
    result = parse(reply(usage=change))
    assert result == profile.ResearchResponse(None, None, "usage_unknown")


def test_missing_optional_details_and_required_usage():
    value = reply(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    assert parse(value).usage == profile.ResearchUsage(1, 2, 3)
    del value["usage"]
    assert parse(value) == profile.ResearchResponse(None, None, "response_invalid")


@pytest.mark.parametrize(
    "prompt,completion", [(128001, 1), (1, 1025), (128000, 1), (2**40, 2**30)]
)
def test_valid_overrun_is_observed_in_full_never_clamped(prompt, completion):
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    result = parse(reply(usage=usage))
    assert result == profile.ResearchResponse(
        profile.ResearchUsage(prompt, completion, prompt + completion),
        None,
        "profile_exceeded",
    )
    assert result.usage.exceeds_profile


def test_no_sdk_http_netrc_or_fallback_route_is_present():
    tree = ast.parse(Path(profile.__file__).read_text())
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    imports |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(
        name
        and any(
            part in name
            for part in (
                "openai",
                "requests",
                "httpx",
                "netrc",
                "client_factory",
                "isolated_http",
            )
        )
        for name in imports
    )
    before = reply()
    copy = deepcopy(before)
    assert parse(before).disposition == "evidence" and before == copy


@pytest.mark.parametrize(
    "document",
    [
        {
            "error": {"message": "upstream failed"},
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
    ],
)
def test_error_or_bare_usage_cannot_claim_known_completion_consumption(document):
    assert parse(document) == profile.ResearchResponse(None, None, "response_invalid")


def test_exact_plane_integer_limit_is_observed_and_one_beyond_is_unknown():
    maximum = 2**53 - 1
    usage = {
        "prompt_tokens": maximum - 1,
        "completion_tokens": 1,
        "total_tokens": maximum,
    }
    result = parse(reply(usage=usage))
    assert result.usage == profile.ResearchUsage(maximum - 1, 1, maximum)
    assert result.disposition == "profile_exceeded"
    usage["prompt_tokens"] += 1
    usage["total_tokens"] += 1
    assert parse(reply(usage=usage)) == profile.ResearchResponse(
        None, None, "response_invalid"
    )
