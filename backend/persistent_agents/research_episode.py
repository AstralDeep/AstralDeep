"""Identifies the fixed research actions used by Work to verify retained results."""

from __future__ import annotations

from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.research_input import fixed_reader_source
from persistent_agents.runtime_values import digest

SOURCE_KEY = "research-source-v1"
MODEL_KEY = "research-selection-v1"


def research_action_keys(record):
    for value in (record.instruction_revision, record.control_epoch):
        if type(value) is not int or not 1 <= value <= 2**53 - 1:
            raise DispatchDenied("assignment_operation_profile_unavailable")
    return tuple(digest([domain, record.instruction_revision, record.control_epoch])
                 for domain in ("research-source-v2", "research-selection-v2"))


def source_request(record):
    source = fixed_reader_source(record)
    if (
        not isinstance(source, dict)
        or record.execution_profile != "one_shot"
        or record.operation.get("kind") != "research"
        or record.operation.get("source_retention") not in {"operation", "none"}
        or source.get("profile") != "public_page"
        or source.get("agent_id") != "web-research-1"
        or source.get("tool_name") != "fetch_page"
        or source.get("linked_document_urls")
    ):
        raise DispatchDenied("assignment_operation_profile_unavailable")
    return {
        "kind": "tool",
        **{key: source[key] for key in ("agent_id", "tool_name", "arguments")},
    }
