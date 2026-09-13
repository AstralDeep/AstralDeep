"""Closed owner-read projection of a completed, retained research selection.

The caller supplies the current owner and its existing Plane transaction, then
revalidates read authority before delivery. These reads neither grant execution
nor require the original execution session, provider configuration or instruction
expansion. The named result MAC authenticates the stored selection and opaque
input binding; it does not reconstruct the private input MAC or provider prompt.

Equality rereads detect ledger changes under READ COMMITTED. They are bounded
read validation, not an atomic snapshot or a held owner/retirement lock. Only
already settled, immutable action bindings are interpreted. A missing historical
verification key makes the result unavailable, without adopting the active key.
Plane's individual action reads lock their rows; the caller must bound SQL waits
on the supplied transaction, with no external work while these locks are held.
"""

from __future__ import annotations

import re

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
)
from astralplane.repositories.assignment_models import (
    AssignmentActionIntent,
    AssignmentActionRecord,
    AssignmentDefinition,
    AssignmentInputReference,
    AssignmentOperationRead,
    AssignmentRecord,
    AssignmentResourceAmount,
    AssignmentTransientInput,
)
from audit.pii import private_binding_key
from llm_config import research_profile as profile
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import validate_id
from persistent_agents.research_input import _source_identity, route
from persistent_agents.research_episode import MODEL_KEY, source_request
from persistent_agents.research_result import build_page_result
from persistent_agents.runtime_values import canonical, digest, thaw


def _envelope(reason=None, content=None):
    return {"version": 1, "available": reason is None, "reason": reason, "content": content}


def _require(condition):
    if not condition:
        raise ValueError("work_result_unavailable")


def _integer(value, minimum=0):
    _require(type(value) is int and minimum <= value <= 2**53 - 1)


def _identity(value):
    _require(type(value) is str)
    return validate_id(value)


def _digest(value):
    _require(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None)


def _amount(value):
    value = thaw(value)
    counters = {"model_calls", "tool_calls", "tokens", "elapsed_ms"}
    _require(type(value) is dict and set(value) == counters | {"spend_micro_units", "currency"})
    for name in counters:
        _integer(value[name])
    if value["spend_micro_units"] is None:
        _require(value["currency"] is None)
    else:
        _integer(value["spend_micro_units"])
        _require(type(value["currency"]) is str and 1 <= len(value["currency"]) <= 8
                 and value["currency"].strip() != "")
    return value


def _same(left, right):
    return type(left) is type(right) and canonical(thaw(left)) == canonical(thaw(right))


def _action(record, action):
    _require(type(action) is AssignmentActionRecord)
    _require(type(action.intent) is AssignmentActionIntent)
    _identity(action.action_id)
    _identity(action.assignment_id)
    _integer(action.instruction_revision, 1)
    _integer(action.control_epoch, 1)
    _require((action.owner_id, action.assignment_id, action.instruction_revision,
              action.control_epoch) == (record.owner_id, record.assignment_id,
              record.instruction_revision, record.control_epoch))
    _require(type(action.state) is str and action.state == "succeeded" and action.ever_started is True)
    _require(action.intent.sensitivity == "ordinary" and action.intent.interactive_only is False
             and action.intent.task_id is None and action.intent.event_id is None
             and action.interactive_proposal_id is None)
    _require(type(action.intent.maximum) is AssignmentResourceAmount)
    _amount(action.intent.maximum)
    for value in (action.intent.request_digest, action.intent.permission_digest,
                  action.intent.precondition_digest):
        _digest(value)


def _settled(action):
    result = thaw(action.result)
    _require(type(result) is dict and type(result.get("outcome")) is str
             and result["outcome"] == "succeeded")
    # No reconciliation, unknown fields, unavailable marker or copied private
    # payload can be interpreted as this profile's original settled receipt.
    keys = {"outcome", "result_digest", "result", "evidence_reference", "actual"}
    disposition = result.get("result_disposition")
    if disposition is not None:
        keys.update(("result_disposition", "result_available"))
        _require(result.get("result_available") is True)
    _require(set(result) == keys and result["evidence_reference"] is None)
    _require(bool(action.attempts))
    attempt = thaw(action.attempts[-1])
    _identity(attempt["attempt_id"])
    _amount(result["actual"])
    _require(_amount(attempt["maximum"]) == _amount(action.intent.maximum))
    _require(set(attempt) <= {"attempt_id", "state", "maximum", "quote_digest",
                             "quote_expires_at", "outcome", "uncertain_observation"})
    original = {key: value for key, value in result.items() if key != "result_available"}
    _require(type(attempt["state"]) is str and attempt["state"] == "succeeded"
             and _same(attempt["outcome"], original))
    return result, attempt


def _model_source(record, model):
    _action(record, model)
    transient = model.intent.transient_input
    _require(type(transient) is AssignmentTransientInput)
    _require(type(transient.version) is int and transient.version == 1
             and transient.source_retention == "operation"
             and transient.reconstruction_kind == "model_messages"
             and len(transient.references) == 1)
    reference = transient.references[0]
    _require(type(reference) is AssignmentInputReference and reference.kind == "source"
             and type(reference.revision) is int and reference.revision == 1)
    _identity(reference.resource_id)
    _digest(transient.payload_binding)
    _require(type(transient.binding_key_id) is str
             and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", transient.binding_key_id) is not None)
    _require(thaw(model.intent.request) == route()
             and type(model.intent.request["max_output_tokens"]) is int
             and model.intent.action_key == "research-v1-" + MODEL_KEY
             and model.intent.request_digest == transient.payload_binding
             and model.intent.boundary == "unreplayable"
             and type(model.intent.maximum) is AssignmentResourceAmount
             and model.intent.maximum == AssignmentResourceAmount(
                 model_calls=1, tokens=profile.RESERVED_TOKENS,
                 elapsed_ms=profile.RESERVED_MILLISECONDS))
    return transient, reference.resource_id


def _page(record, model, source, transient):
    _action(record, source)
    _require(thaw(source.intent.request) == source_request(record)
             and source.intent.request_digest == digest(thaw(source.intent.request)))
    _source_identity(record, source)
    retained, _ = _settled(source)
    _require(retained.get("result_disposition") is None)
    observation = retained["result"]
    _require(observation["requested_url"] == source.intent.request["arguments"]["url"])
    result, attempt = _settled(model)
    _require(type(result["result_disposition"].get("version")) is int
             and result["result_disposition"].get("available") is True)
    _require(result["result_disposition"] == {
        "version": 1, "available": True, "reason": None, "references": [],
        "binding_key_id": transient.binding_key_id,
    })
    selection = result["result"]
    _require(type(selection) is dict and type(selection.get("version")) is int)
    _require(selection == {
        "version": 1, "profile": profile.PROFILE,
        "passage_ids": selection.get("passage_ids"),
        "source_action_id": source.action_id,
        "source_result_digest": retained["result_digest"],
    })
    key = private_binding_key(transient.binding_key_id)
    _require(key.verify("result", canonical({
        "profile": profile.PROFILE, "payload_binding": transient.payload_binding,
        "action_id": model.action_id, "attempt_id": attempt["attempt_id"],
        "outcome": "succeeded", "result": selection, "actual": result["actual"],
    }).encode("utf-8"), result["result_digest"]))
    page = build_page_result(observation, selection["passage_ids"],
        source_action_id=source.action_id, source_result_digest=retained["result_digest"])
    _require(canonical(page) == canonical(thaw(record.checkpoint).get("research_result")))
    return page


def project_research_result(transaction, repository, *, owner_id, read):
    """Return bounded content only after same-transaction, exact ledger proof.

    No network, provider store, execution authority, publication or direct SQL is used.
    Missing/foreign records and invalid proofs share a data-free unavailable
    result. The caller's normal owner-read policy remains mandatory.
    """
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
                or operation["version"] != 2 or operation.get("kind") != "research"
                or operation.get("authority", {}).get("origin") != "interactive"
                or operation.get("authority", {}).get("reference_kind") != "session_incarnation"):
            return _envelope("unsupported")
        if (record.lifecycle != "completed" or read.disposition != "completed"
                or read.terminal_outcome != "completed"
                or operation.get("terminal_outcome") != "completed"):
            return _envelope("not_completed")
        if operation.get("source_retention") != "operation":
            return _envelope("not_retained")
        _require(type(read.result_reference) is str
                 and read.result_reference == operation.get("result_reference"))
        _identity(read.result_reference)
        source_request(record)
        # This unlocked inspection only discovers IDs. It cannot prove a result.
        # Match execution's sorted lock order, then require exact locked equality.
        peek = repository.get_action_by_key(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id, action_key="research-v1-" + MODEL_KEY)
        _, source_id = _model_source(record, peek)
        _require(peek.action_id == read.result_reference and source_id != peek.action_id)
        locked = {identity: repository.get_action(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id, action_id=identity)
            for identity in sorted((source_id, peek.action_id))}
        model, source = locked[peek.action_id], locked[source_id]
        _require(type(model) is AssignmentActionRecord and _same(model, peek))
        transient, locked_source_id = _model_source(record, model)
        _require(locked_source_id == source_id)
        page = _page(record, model, source, transient)
        for action in (source, model):
            actual = repository.get_action(transaction, owner_id=owner_id,
                assignment_id=record.assignment_id, action_id=action.action_id)
            _require(type(actual) is AssignmentActionRecord and _same(actual, action))
        final = repository.get_operation(transaction, owner_id=owner_id,
            assignment_id=record.assignment_id)
        _require(type(final) is AssignmentOperationRead and _same(final, current))
        return _envelope(content=page)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError,
            RepositoryConflictError, RepositoryDataError, RepositoryNotFoundError, DispatchDenied):
        return _envelope("unavailable")
