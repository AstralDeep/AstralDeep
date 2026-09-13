"""Closed declarative drafts retain instructions without granting execution."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from orchestrator.projection_surfaces import authoring
from persistent_agents.models import AssignmentLimits


def definition():
    return {
        "version": 1,
        "purpose": "Read public evidence",
        "instructions": "Keep quotations attributed to their source.",
        "capabilities": [],
        "memory": {"mode": "none"},
        "triggers": [{"kind": "manual"}],
        "limits": AssignmentLimits().model_dump(),
        "approvals": {"policy": "normal"},
    }


def test_tool_free_draft_is_explicit_detached_and_not_an_activation():
    original = definition()
    parsed = authoring.DeclarativeAgentDefinition.parse(original)
    original["instructions"] = "changed after parsing"
    result = parsed.to_dict()
    assert result == definition()
    result["limits"]["daily"]["tokens"] = 999
    assert parsed.to_dict() == definition()
    assert parsed.fixed_research_shape is False
    assert not any("declarative" in action for action in authoring.HANDLERS)


def test_supported_tool_shape_is_only_structural_not_authority():
    value = definition()
    value["capabilities"] = [{"agent_id": "web-research-1", "tool_name": "fetch_page"}]
    parsed = authoring.DeclarativeAgentDefinition.parse(value)
    assert parsed.fixed_research_shape is True
    # Existing default budgets are insufficient for the model profile. Parsing
    # a draft must not silently increase them or pretend to authorize execution.
    assert parsed.to_dict()["limits"]["daily"]["tokens"] == 100_000
    assert parsed.to_dict() == value


def test_other_capability_is_preserved_as_unvalidated_draft():
    value = definition()
    value["capabilities"] = [{"agent_id": "owner-agent", "tool_name": "unregistered"}]
    parsed = authoring.DeclarativeAgentDefinition.parse(value)
    assert parsed.to_dict() == value
    assert parsed.fixed_research_shape is False


def test_canonical_definition_digest_and_private_repr():
    value = definition()
    value["instructions"] = "Private owner-authored wording \u00e9"
    first = authoring.DeclarativeAgentDefinition.parse(value)
    second = authoring.DeclarativeAgentDefinition.parse(dict(reversed(list(value.items()))))
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False).encode("utf-8")
    assert first.digest == second.digest == hashlib.sha256(canonical).hexdigest()
    assert value["instructions"] not in repr(first)


@pytest.mark.parametrize("field", list(definition()))
def test_required_policy_cannot_disappear(field):
    value = definition()
    del value[field]
    with pytest.raises(authoring.DeclarativeAgentError) as caught:
        authoring.DeclarativeAgentDefinition.parse(value)
    assert caught.value.code == "declarative_definition_invalid"


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"version": "1"},
    {"purpose": " "}, {"purpose": "x" * 121}, {"purpose": "bad\x00name"},
    {"instructions": ""}, {"instructions": "x" * 4097}, {"instructions": "bad\x1btext"},
    {"instructions": "bad\ud800text"}, {"instructions": 99},
    {"code": "print('must never execute')"}, {"runtime": "python"},
    {"memory": {"mode": "automatic"}}, {"memory": {"mode": "none", "value": "private"}},
    {"triggers": []}, {"triggers": [{"kind": "interval", "seconds": 1}]},
    {"triggers": [{"kind": "manual"}, {"kind": "manual"}]},
    {"approvals": {"policy": "none"}}, {"approvals": {"policy": "normal", "safe": True}},
    {"capabilities": [{"agent_id": "agent", "tool_name": "tool", "scope": "system"}]},
    {"capabilities": [{"agent_id": "agent", "tool_name": "tool"}] * 2},
    {"capabilities": [{"agent_id": "agent", "tool_name": f"tool{i}"} for i in range(33)]},
    {"limits": {"daily": {"tokens": float("inf")}}},
    {"limits": {"daily": {"tokens": True}}},
    {"limits": {"daily": {"tokens": 9_000_000}, "lifetime": {"tokens": 3_000_000}}},
])
def test_closed_field_and_bound_denials_are_content_free(change):
    value = definition() | copy.deepcopy(change)
    with pytest.raises(authoring.DeclarativeAgentError) as caught:
        authoring.DeclarativeAgentDefinition.parse(value)
    assert str(caught.value) == "declarative_definition_invalid"
    assert caught.value.status_code == 422


@pytest.mark.parametrize("value", [None, [], "{}", 1, True])
def test_definition_requires_an_object(value):
    with pytest.raises(authoring.DeclarativeAgentError):
        authoring.DeclarativeAgentDefinition.parse(value)


def test_oversized_and_recursive_input_refuse_without_partial_definition():
    oversized = definition() | {"instructions": "\U0001f600" * 20_000}
    recursive = definition()
    recursive["memory"]["value"] = recursive
    for value in (oversized, recursive):
        with pytest.raises(authoring.DeclarativeAgentError) as caught:
            authoring.DeclarativeAgentDefinition.parse(value)
        assert str(caught.value) == "declarative_definition_invalid"


def test_maximum_unicode_content_and_capabilities_survive_without_truncation():
    value = definition()
    value["purpose"] = "\U0001f600" * 120
    value["instructions"] = "\U0001f600" * 4096
    value["capabilities"] = [
        {"agent_id": "a" * 128, "tool_name": f"tool{i}"} for i in range(32)
    ]
    assert authoring.DeclarativeAgentDefinition.parse(value).to_dict() == value


def test_instruction_prose_does_not_become_executable_capability():
    value = definition()
    value["instructions"] = "Please run __import__('os').system('example')\n\tThis remains prose."
    parsed = authoring.DeclarativeAgentDefinition.parse(value)
    assert parsed.to_dict() == value
    assert parsed.fixed_research_shape is False


def test_existing_limit_defaults_are_materialized_in_the_immutable_snapshot():
    value = definition()
    value["limits"] = {}
    parsed = authoring.DeclarativeAgentDefinition.parse(value)
    assert parsed.to_dict()["limits"] == AssignmentLimits().model_dump()
