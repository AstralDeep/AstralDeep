"""Attempt-local proof for a non-retained source read; PostgreSQL keeps no body or
digest, so recovery after process/lease loss requires a fresh charged read. Backs
execution.py's non-retained profile via research_episode.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from astralplane.repositories.assignment_models import (
    AssignmentActionOutcome, AssignmentActionRecord, AssignmentRecord, AssignmentResultDisposition,
)
from audit.pii import PrivateBindingKey, private_binding_key
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.research_result import build_page_result
from persistent_agents.runtime_values import canonical, digest, thaw


def _deny():
    raise DispatchDenied("assignment_research_binding_changed")


def model_key(record, source):
    from persistent_agents.research_episode import research_action_keys

    return digest([research_action_keys(record)[1], source.source_action_id, source.receipt])


def assert_ready(record):
    outstanding = record.usage.get("outstanding", {})
    if (record.lifecycle != "active" or record.phase != "checking"
            or any(type(outstanding.get(key, 0)) is not int or outstanding.get(key, 0) != 0
                for key in ("model_calls", "tool_calls", "tokens", "elapsed_ms"))
            or (outstanding.get("spend_micro_units") is not None and
                (type(outstanding["spend_micro_units"]) is not int or outstanding["spend_micro_units"] != 0))):
        raise DispatchDenied("assignment_action_uncertain")


@dataclass(frozen=True, slots=True)
class EphemeralAcquisition:
    generation: str
    action_key: str
    _execution_json: str = field(repr=False)
    _key: PrivateBindingKey = field(repr=False)

    @classmethod
    def create(cls, executor):
        from persistent_agents.research_episode import research_action_keys

        record = executor.record
        if record.operation.get("source_retention") != "none":
            _deny()
        generation = str(uuid4())
        return cls(generation, digest([research_action_keys(record)[0], generation]),
                   canonical([thaw(executor.claim.fence), thaw(executor.binding)]),
                   private_binding_key())


@dataclass(frozen=True, slots=True)
class EphemeralResearchSource:
    source_action_id: str
    attempt_id: str
    generation: str
    _record_json: str = field(repr=False)
    _intent_json: str = field(repr=False)
    _observation_json: str = field(repr=False)
    _actual_json: str = field(repr=False)
    _execution_json: str = field(repr=False)
    _key: PrivateBindingKey = field(repr=False)

    @classmethod
    def from_effect(cls, record, action, attempt_id, observation, actual, acquisition):
        from persistent_agents.research_input import _record_identity

        if type(acquisition) is not EphemeralAcquisition:
            _deny()
        result = cls(action.action_id, attempt_id, acquisition.generation,
            _record_identity(record), canonical(thaw(action.intent)),
            canonical(observation), canonical(thaw(actual)),
            acquisition._execution_json, acquisition._key)
        result._validate(record, action)
        return result

    def _validate(self, record, action):
        from persistent_agents.research_episode import research_action_keys, source_request
        from persistent_agents.research_input import _record_identity

        if type(record) is not AssignmentRecord or type(action) is not AssignmentActionRecord:
            _deny()
        generation = UUID(self.generation)
        if (str(generation) != self.generation or generation.version != 4
                or record.operation.get("source_retention") != "none"
                or _record_identity(record) != self._record_json
                or action.owner_id != record.owner_id
                or action.assignment_id != record.assignment_id
                or type(action.instruction_revision) is not int
                or type(action.control_epoch) is not int
                or action.instruction_revision != record.instruction_revision
                or action.control_epoch != record.control_epoch
                or action.action_id != self.source_action_id
                or canonical(thaw(action.intent)) != self._intent_json
                or action.intent.action_key != digest([research_action_keys(record)[0], self.generation])
                or thaw(action.intent.request) != source_request(record)
                or action.intent.boundary != "read_only"
                or action.intent.sensitivity != "ordinary"
                or action.intent.interactive_only
                or action.intent.transient_input is not None
                or private_binding_key(self._key.key_id) != self._key):
            _deny()
        observation = json.loads(self._observation_json)
        build_page_result(observation, [], source_action_id=self.source_action_id,
                          source_result_digest=digest(observation))

    @property
    def receipt(self):
        return self._key.sign("result", canonical({
            "profile": "ephemeral-page-v1", "record": self._record_json,
            "intent": self._intent_json, "generation": self.generation,
            "execution": self._execution_json,
            "action_id": self.source_action_id, "attempt_id": self.attempt_id,
            "observation": json.loads(self._observation_json),
            "actual": json.loads(self._actual_json), "outcome": "succeeded",
        }).encode("utf-8"))

    @property
    def key_id(self):
        return self._key.key_id

    def assert_executor(self, executor):
        if (executor._research_generation != self.generation
                or canonical([thaw(executor.claim.fence), thaw(executor.binding)]) != self._execution_json):
            _deny()

    def identity(self, record, action):
        try:
            self._validate(record, action)
            retained = thaw(action.result)
            expected = thaw(AssignmentActionOutcome(outcome="succeeded", result_digest=self.receipt,
                result={}, actual=json.loads(self._actual_json),
                result_disposition=AssignmentResultDisposition(available=False,
                    reason="retention_discarded", binding_key_id=self.key_id)))
            attempt = thaw(action.attempts[-1])
            if (action.state != "succeeded" or action.ever_started is not True
                    or attempt.get("attempt_id") != self.attempt_id
                    or attempt.get("state") != "succeeded"
                    or canonical(attempt.get("outcome")) != canonical(expected)
                    or canonical(retained) != canonical({**expected,
                        "result_available": False, "reacquisition_reason": "retention_discarded"})):
                _deny()
            observation = json.loads(self._observation_json)
            return canonical({"action_id": self.source_action_id,
                "intent": thaw(action.intent), "attempt_id": self.attempt_id,
                "result_digest": digest(observation), "receipt": self.receipt,
                "generation": self.generation, "observation": observation}), observation
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            _deny()

    def metadata(self):
        value = json.loads(self._observation_json)
        return {"version": 1, "action_id": self.source_action_id,
            "attempt_id": self.attempt_id, "observation_generation": self.generation,
            "receipt": self.receipt, "binding_key_id": self.key_id,
            "text_available": False,
            "source": {key: value[key] for key in (
                "requested_url", "final_url", "retrieved_at", "media_type",
                "extraction_profile", "title", "body_complete", "extraction_complete",
                "excerpt_complete", "redacted")},
            "reacquisition": {"version": 1, "required": False}}
