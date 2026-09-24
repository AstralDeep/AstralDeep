"""Owner-read projection of one completed, retained chat answer, reread and MAC-verified
inside the caller's existing Plane transaction. Shares its verification shape with
work_result.py's research projection.
"""

from __future__ import annotations

import re

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
)
from astralplane.repositories.assignment_models import (
    AssignmentActionRecord,
    AssignmentDefinition,
    AssignmentOperationRead,
    AssignmentRecord,
    AssignmentResourceAmount,
    AssignmentTransientInput,
)
from audit.pii import private_binding_key
from llm_config import research_profile as profile
from persistent_agents.chat_episode import (
    CHAT_KEY_PREFIX,
    CHAT_PROFILE,
    chat_action_key,
    chat_result_value,
)
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.research_input import chat_definition, receipt_payload, route
from persistent_agents.runtime_values import canonical, thaw
from orchestrator.work_result import _action, _envelope, _identity, _integer, _require, _same, _settled


def _chat_model(record, model, expected_key):
    _action(record, model)
    transient = model.intent.transient_input
    _require(type(transient) is AssignmentTransientInput)
    _require(type(transient.version) is int and transient.version == 1
             and transient.source_retention == "operation"
             and transient.reconstruction_kind == "model_messages"
             and transient.references == ())
    _require(type(transient.payload_binding) is str
             and re.fullmatch(r"[a-f0-9]{64}", transient.payload_binding) is not None)
    _require(type(transient.binding_key_id) is str
             and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", transient.binding_key_id) is not None)
    _require(thaw(model.intent.request) == route()
             and model.intent.action_key == expected_key
             and model.intent.request_digest == transient.payload_binding
             and model.intent.boundary == "unreplayable"
             and type(model.intent.maximum) is AssignmentResourceAmount
             and model.intent.maximum == AssignmentResourceAmount(
                 model_calls=1, tokens=profile.RESERVED_TOKENS,
                 elapsed_ms=profile.RESERVED_MILLISECONDS))
    return transient


def _answer(record, model, transient):
    result, attempt = _settled(model)
    _require(result["result_disposition"] == {
        "version": 1, "available": True, "reason": None, "references": [],
        "binding_key_id": transient.binding_key_id,
    })
    answer = result["result"]
    _require(type(answer) is dict and set(answer) == {"version", "profile", "text"}
             and answer["profile"] == CHAT_PROFILE
             and answer == chat_result_value(answer["text"]))
    key = private_binding_key(transient.binding_key_id)
    _require(key.verify("result", receipt_payload(payload_binding=transient.payload_binding,
        action_id=model.action_id, attempt_id=attempt["attempt_id"], outcome="succeeded",
        result=answer, actual=result["actual"], profile_name=CHAT_PROFILE),
        result["result_digest"]))
    expected = {"version": 1, "kind": "chat", "text": answer["text"],
                "model_action_id": model.action_id}
    _require(canonical(expected) == canonical(thaw(record.checkpoint).get("chat_result")))
    return {"version": 1, "kind": "chat", "scope": "model_only", "sources": [],
            "text": answer["text"]}


def project_chat_result(transaction, repository, *, owner_id, read):
    try:
        _require(type(read) is AssignmentOperationRead)
        record = read.assignment
        _require(type(record) is AssignmentRecord and type(record.definition) is AssignmentDefinition
                 and type(owner_id) is str and record.owner_id == owner_id)
        _identity(record.assignment_id)
        for value in (record.instruction_revision, record.control_epoch, record.state_version):
            _integer(value, 1)
        current = repository.get_operation(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id)
        _require(type(current) is AssignmentOperationRead and _same(current, read))
        operation = thaw(record.operation)
        if (record.execution_profile != "one_shot" or read.continuation_supported is not True
                or type(operation) is not dict or type(operation.get("version")) is not int
                or operation["version"] != 2 or operation.get("kind") != "chat"
                or operation.get("authority", {}).get("origin") != "interactive"
                or operation.get("authority", {}).get("reference_kind") != "session_incarnation"):
            return _envelope("unsupported")
        if (record.lifecycle != "completed" or read.disposition != "completed"
                or read.terminal_outcome != "completed"
                or operation.get("terminal_outcome") != "completed"):
            return _envelope("not_completed")
        chat_definition(record)
        _require(type(read.result_reference) is str
                 and read.result_reference == operation.get("result_reference"))
        _identity(read.result_reference)
        expected_key = CHAT_KEY_PREFIX + chat_action_key(record)
        peek = repository.get_action_by_key(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id, action_key=expected_key)
        _require(peek is not None and peek.action_id == read.result_reference)
        model = repository.get_action(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id, action_id=peek.action_id)
        _require(type(model) is AssignmentActionRecord and _same(model, peek))
        transient = _chat_model(record, model, expected_key)
        answer = _answer(record, model, transient)
        actual = repository.get_action(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id, action_id=model.action_id)
        _require(type(actual) is AssignmentActionRecord and _same(actual, model))
        final = repository.get_operation(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id)
        _require(type(final) is AssignmentOperationRead and _same(final, current))
        return _envelope(content=answer)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError,
            RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError, DispatchDenied):
        return _envelope("unavailable")
