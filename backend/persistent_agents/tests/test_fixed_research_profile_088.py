"""Tests for persistent_agents/execution.py and research_episode.py: unsupported
identities are refused before reader-config capture, requested tool/scope must match
the fixed reader profile, and locked checks run before any policy callback.
"""

from types import SimpleNamespace

import pytest

from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.research_episode import source_request
from persistent_agents.research_input import fixed_reader_source

AGENT, TOOL = "web-research-1", "fetch_page"
REQUEST = {"kind": "tool", "agent_id": AGENT, "tool_name": TOOL,
           "arguments": {"url": "https://example.test/page"}}


def record():
    return SimpleNamespace(
        owner_id="owner", assignment_id="assignment", instruction_revision=1, control_epoch=1,
        execution_profile="one_shot",
        operation={"version": 2, "kind": "research", "source_retention": "operation"},
        definition=SimpleNamespace(allowed_tools=(AGENT + ":" + TOOL,), source={
            "profile": "public_page", "agent_id": AGENT, "tool_name": TOOL,
            "arguments": dict(REQUEST["arguments"]), "linked_document_urls": [],
        }),
    )


def change_record(value, change):
    if change == "additional_tool":
        value.definition.allowed_tools += ("other-1:read",)
    elif change == "only_other_tool":
        value.definition.allowed_tools = ("other-1:read",)
    elif change == "duplicate_tool":
        value.definition.allowed_tools *= 2
    elif change == "empty_tools":
        value.definition.allowed_tools = ()
    elif change == "invalid_tools":
        value.definition.allowed_tools = None
    elif change == "wrong_kind":
        value.operation["kind"] = "chat"
    elif change == "persistent":
        value.execution_profile = "persistent"
    elif change == "registered_source":
        value.definition.source["profile"] = "registered_reader"
    elif change == "wrong_source_agent":
        value.definition.source["agent_id"] = "other-1"
    elif change == "wrong_source_tool":
        value.definition.source["tool_name"] = "search"
    elif change == "missing_source":
        value.definition.source = None
    else:
        value.definition = None


@pytest.mark.parametrize("change", [
    "additional_tool", "only_other_tool", "duplicate_tool", "empty_tools", "invalid_tools",
    "persistent", "registered_source", "wrong_source_agent", "wrong_source_tool",
    "missing_source", "missing_definition",
])
def test_retained_research_profile_refuses_unbound_sources(change):
    value = record()
    change_record(value, change)

    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        source_request(value)


def test_supported_source_is_detached_and_reconstructs_its_original_request():
    value = record()
    detached = fixed_reader_source(value)
    detached["arguments"]["url"] = "https://example.test/changed"
    assert source_request(value) == REQUEST
