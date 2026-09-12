"""Closed source facts and exact extractive result construction, without a model."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from persistent_agents.research_result import (
    build_page_result,
    legacy_page_response,
    page_passages,
    read_page_observation,
    retain_page_observation,
)
from persistent_agents.runtime_values import canonical, digest, extract_result


URL = "https://example.org/source"
ACTION = "1c6081aa-80e8-4b9d-9ef6-1c9bba104dfd"


def page(**changes):
    return {
        "version": 1,
        "requested_url": URL,
        "final_url": URL,
        "retrieved_at": "2026-09-12T12:00:00+00:00",
        "media_type": "text/html",
        "extraction_profile": "html_readable_v1",
        "title": "Release notes",
        "text": "The release is available.\n\nThe build is stable.",
        "body_complete": True,
        "extraction_complete": True,
        "excerpt_complete": True,
        "redacted": False,
        **changes,
    }


def response(value):
    data = {
        "url": URL,
        "title": value["title"],
        "characters": len(value["text"]),
        "truncated": not value["excerpt_complete"],
    }
    return SimpleNamespace(
        error=None,
        ui_components=[{"type": "text", "content": value["text"]}],
        result={"_data": {**data, "page_observation": value}},
    )


def retained(**changes):
    return retain_page_observation(
        page(**changes), source_action_id=ACTION, redacted=False
    )


def test_legacy_metadata_removal_preserves_exact_output_and_input():
    raw = response(page(text="Long complete content. " * 900))
    original = deepcopy(raw)
    old = deepcopy(raw)
    old.result["_data"].pop("page_observation")
    assert extract_result(legacy_page_response(raw)) == extract_result(old)
    assert raw == original


def test_source_facts_and_action_binding_survive_retention_and_result():
    value = read_page_observation(response(page()), requested_url=URL)
    observation = retain_page_observation(
        value, source_action_id=ACTION, redacted=False
    )
    passages = page_passages(observation)
    result = build_page_result(
        observation,
        [passages[0]["id"]],
        source_action_id=ACTION,
        source_result_digest=digest(observation),
    )
    assert result["disposition"] == "evidence"
    assert result["passages"][0]["text"] == passages[0]["text"]
    assert result["source"]["action_id"] == ACTION
    assert result["source"]["result_digest"] == digest(observation)
    assert result["scope"] == "one_page_excerpts"


def test_empty_selection_is_explicit_insufficient_evidence():
    value = retained()
    result = build_page_result(
        value, [], source_action_id=ACTION, source_result_digest=digest(value)
    )
    assert result["disposition"] == "insufficient_evidence"
    assert result["passages"] == []


@pytest.mark.parametrize("text", ["é" * 15000, 'a\\"\n' * 4000, "paragraph\n\n" * 1500])
def test_canonical_byte_bound_and_exact_passages(text):
    value = retained(text=text)
    assert len(canonical(value).encode("utf-8")) <= 8192
    assert value["text"] and text.startswith(value["text"])
    assert value["excerpt_complete"] is False
    passages = page_passages(value)
    assert "".join(item["text"] for item in passages) == value["text"]
    assert passages == page_passages(deepcopy(value))
    assert len({item["id"] for item in passages}) == len(passages)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"extra": "private"},
        {"text": ""},
        {"text": "   "},
        {"text": "a" * 20001},
        {"text": "\ud800"},
        {"title": "t" * 513},
        {"requested_url": "https://other.org/source"},
        {"final_url": "http://example.org"},
        {"final_url": "https://127.0.0.1"},
        {"final_url": "https://example.org/a?token=private"},
        {"retrieved_at": "2026-09-12T12:00:00"},
        {"retrieved_at": "2026-09-12T12:00:00+01:00"},
        {"retrieved_at": "not-a-date"},
        {"media_type": "application/pdf"},
        {"extraction_profile": "visual_page"},
        {"extraction_profile": "plain_text_v1"},
        {"body_complete": 1},
        {"extraction_complete": "yes"},
        {"excerpt_complete": None},
        {"redacted": True},
    ],
)
def test_malformed_source_facts_refuse_with_data_free_code(changes):
    with pytest.raises(ValueError, match="^assignment_source_observation_invalid$"):
        read_page_observation(response(page(**changes)), requested_url=URL)


@pytest.mark.parametrize(
    "selection", [["p999"], ["p001", "p001"], [True], "p001", {}, ["p001"] * 9]
)
def test_selection_cannot_create_citations_or_claims(selection):
    value = retained()
    with pytest.raises(ValueError, match="^assignment_research_result_invalid$"):
        build_page_result(
            value,
            selection,
            source_action_id=ACTION,
            source_result_digest=digest(value),
        )


@pytest.mark.parametrize(
    "action,digest_value",
    [
        (ACTION, "0" * 64),
        ("061ddf41-b89e-4a13-bf4d-cf48716c4d84", None),
        ("not-a-uuid", None),
    ],
)
def test_source_result_binding_is_exact(action, digest_value):
    value = retained()
    with pytest.raises(ValueError, match="^assignment_research_result_invalid$"):
        build_page_result(
            value,
            ["p001"],
            source_action_id=action,
            source_result_digest=digest_value or digest(value),
        )


@pytest.mark.parametrize(
    "result", [None, {}, {"_data": None}, {"_data": {"text": "existing"}}]
)
def test_legacy_projection_is_identity_without_the_additive_member(result):
    value = SimpleNamespace(result=result)
    assert legacy_page_response(value) is value


def test_response_error_cannot_advertise_successful_page_facts():
    value = response(page())
    value.error = "private upstream error"
    with pytest.raises(ValueError, match="^assignment_source_observation_invalid$"):
        read_page_observation(value, requested_url=URL)


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_action_id", "wrong"),
        ("revision_digest", "x" * 64),
        ("version", 2),
        ("text", "a" * 9000),
    ],
)
def test_invalid_retained_domain_cannot_form_passages(key, value):
    observation = retained()
    observation[key] = value
    with pytest.raises(ValueError, match="^assignment_source_observation_invalid$"):
        page_passages(observation)


@pytest.mark.parametrize("redacted,action", [(1, ACTION), (False, "invalid")])
def test_retention_requires_typed_redaction_and_real_action_identity(redacted, action):
    with pytest.raises(ValueError, match="^assignment_source_observation_invalid$"):
        retain_page_observation(page(), source_action_id=action, redacted=redacted)


def test_metadata_cannot_consume_the_entire_retained_text_budget():
    url = "https://example.org/" + "é" * 2000
    with pytest.raises(ValueError, match="^assignment_source_observation_invalid$"):
        retain_page_observation(
            page(requested_url=url, final_url=url, title="t" * 512),
            source_action_id=ACTION,
            redacted=False,
        )


def test_raw_serialized_domain_includes_escaping_overhead():
    with pytest.raises(ValueError, match="^assignment_source_observation_invalid$"):
        retain_page_observation(
            page(text="\x01" * 12000), source_action_id=ACTION, redacted=False
        )


def test_passages_preserve_whitespace_and_selected_document_has_its_own_limit():
    value = retained(text="complete words " * 1000)
    passages = page_passages(value)
    assert passages[0]["text"].endswith(" ")
    assert "".join(item["text"] for item in passages) == value["text"]
    url = "https://example.org/" + "a" * 2000
    value = retained(requested_url=url, final_url=url, title="t" * 512, text="a" * 8000)
    with pytest.raises(ValueError, match="^assignment_research_result_invalid$"):
        build_page_result(
            value,
            [item["id"] for item in page_passages(value)],
            source_action_id=ACTION,
            source_result_digest=digest(value),
        )
