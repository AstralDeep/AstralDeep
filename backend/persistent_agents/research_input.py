"""Reconstructs bindings and source identities for previously completed Work operations.
Work result readers use the original receipt encodings when verifying retained content.
"""

from __future__ import annotations

from llm_config import research_profile as profile
from persistent_agents.dispatch_context import DispatchDenied, canonical
from persistent_agents.research_result import build_page_result
from persistent_agents.runtime_values import thaw


def route(record=None) -> dict:
    from orchestrator.harness.production_profiles import model_route
    return model_route(record)


def _deny():
    raise DispatchDenied("assignment_research_binding_changed")


_SELECTED_RESULT_DOMAIN = b"astral.research.selected-result/v3\x00"

def receipt_payload(*, payload_binding, action_id, attempt_id, outcome, result, actual,
                    selected_input=None, profile_name=profile.PROFILE):
    values = dict(profile=profile_name, payload_binding=payload_binding,
                  action_id=action_id, attempt_id=attempt_id, outcome=outcome,
                  result=thaw(result), actual=thaw(actual))
    if selected_input is None:
        return canonical(values).encode("utf-8")
    values["selected_input"] = selected_input
    return _SELECTED_RESULT_DOMAIN + canonical(values).encode("utf-8")


def fixed_reader_source(record) -> dict:
    try:
        source = thaw(record.definition.source)
        supported = (
            record.execution_profile == "one_shot"
            and tuple(record.definition.allowed_tools) == ("web-research-1:fetch_page",)
            and isinstance(source, dict)
            and source.get("profile") == "public_page"
            and source.get("agent_id") == "web-research-1"
            and source.get("tool_name") == "fetch_page"
        )
    except (AttributeError, TypeError, ValueError):
        supported = False
    if not supported:
        raise DispatchDenied("assignment_operation_profile_unavailable")
    return source


def chat_definition(record) -> None:
    try:
        operation = thaw(record.operation)
        supported = (
            record.execution_profile == "one_shot"
            and operation.get("kind") == "chat"
            and operation.get("source_retention") == "operation"
            and thaw(record.definition.source) == {}
            and tuple(record.definition.allowed_tools) == ()
            and tuple(record.definition.consented_scopes) == ()
            and record.definition.offline_grant_id is None
            and record.definition.completion_condition is None
        )
    except (AttributeError, TypeError, ValueError):
        supported = False
    if not supported:
        raise DispatchDenied("assignment_operation_profile_unavailable")


def _source_identity(record, action) -> tuple[str, dict]:
    request = thaw(action.intent.request)
    if (
        action.owner_id != record.owner_id
        or action.assignment_id != record.assignment_id
        or action.instruction_revision != record.instruction_revision
        or action.control_epoch != record.control_epoch
        or action.state != "succeeded"
        or action.intent.boundary != "read_only"
        or action.intent.sensitivity != "ordinary"
        or action.intent.interactive_only
        or action.intent.transient_input is not None
        or set(request) != {"kind", "agent_id", "tool_name", "arguments"}
        or request.get("kind") != "tool"
        or request.get("agent_id") != "web-research-1"
        or request.get("tool_name") != "fetch_page"
    ):
        _deny()
    result = thaw(action.result)
    if (
        not isinstance(result, dict)
        or result.get("outcome") != "succeeded"
        or result.get("result_available") is False
    ):
        _deny()
    observation = result.get("result")
    build_page_result(
        observation,
        [],
        source_action_id=action.action_id,
        source_result_digest=result.get("result_digest"),
    )
    attempts = thaw(action.attempts)
    if not attempts or not action.ever_started:
        _deny()
    attempt = attempts[-1]
    if attempt.get("state") != "succeeded" or attempt.get("outcome") != result:
        expected = dict(result)
        for key in ("result_available", "reacquisition_reason"):
            expected.pop(key, None)
        if attempt.get("state") != "succeeded" or attempt.get("outcome") != expected:
            _deny()
    return canonical(
        {
            "action_id": action.action_id,
            "intent": thaw(action.intent),
            "attempt_id": attempt["attempt_id"],
            "result_digest": result["result_digest"],
            "observation": observation,
        }
    ), observation
