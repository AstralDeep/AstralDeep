"""Strict owner input and bounded resource contracts."""
import copy
from uuid import uuid4

import pytest
from persistent_agents.models import CreateAssignmentRequest, SourceSelection
from pydantic import ValidationError


def create_payload():
    return {
        "submission_id": str(uuid4()), "name": "Release watch",
        "instructions": "Report meaningful changes to this public release page.",
        "source": {"profile": "public_page", "agent_id": "web-research-1",
                   "tool_name": "fetch_page", "arguments": {"url": "https://www.python.org/downloads/"}},
        "allowed_tools": [{"agent_id": "web-research-1", "tool_name": "fetch_page"}],
        "consent": True,
    }


def test_defaults_are_finite_and_money_is_unknown():
    model = CreateAssignmentRequest.model_validate(create_payload())
    limits = model.limits.to_plane()
    assert limits["daily_tool_calls"] <= limits["tool_calls"]
    assert limits["model_calls"] >= 0
    assert "spend_micro_units" not in limits
    assert model.source.identity == "web-research-1:fetch_page"


@pytest.mark.parametrize("change", [
    {"owner_id": "someone-else"}, {"consent": "true"}, {"name": " "},
    {"instructions": "x" * 4097}, {"submission_id": "123"},
    {"allowed_tools": []}, {"limits": {"cadence_seconds": 59}},
    {"limits": {"max_retries": True}},
    {"limits": {"lifetime": {"spend_micro_units": 0}}},
    {"limits": {"lifetime": {"currency": "USD"}}},
    {"limits": {"daily": {"tool_calls": 201}, "lifetime": {"tool_calls": 200}}},
])
def test_invalid_owner_inputs_are_rejected(change):
    payload = create_payload()
    payload.update(change)
    with pytest.raises(ValidationError):
        CreateAssignmentRequest.model_validate(payload)


@pytest.mark.parametrize("arguments", [
    {"url": "http://example.org"}, {"url": "https://user:secret@example.org"},
    {"url": "https://example.org/#secret"}, {"url": "https://example.org", "extra": 1},
    {"url": "https://example.org/?access_token=secret"}, {"url": "https://localhost"},
])
def test_public_source_has_exact_reviewed_public_url(arguments):
    source = copy.deepcopy(create_payload()["source"])
    source["arguments"] = arguments
    with pytest.raises(ValidationError):
        SourceSelection.model_validate(source)


def test_source_credentials_and_unbounded_json_are_rejected():
    source = {"profile": "registered_reader", "agent_id": "reader-1", "tool_name": "read",
              "arguments": {"nested": {"api_key": "private"}}}
    with pytest.raises(ValidationError):
        SourceSelection.model_validate(source)
    source["arguments"] = {"text": "x" * 8193}
    with pytest.raises(ValidationError):
        SourceSelection.model_validate(source)


def test_source_must_be_in_reviewed_tools_and_duplicates_rejected():
    payload = create_payload()
    payload["allowed_tools"] *= 2
    with pytest.raises(ValidationError):
        CreateAssignmentRequest.model_validate(payload)
    payload["allowed_tools"] = [{"agent_id": "reader-1", "tool_name": "read"}]
    with pytest.raises(ValidationError):
        CreateAssignmentRequest.model_validate(payload)
