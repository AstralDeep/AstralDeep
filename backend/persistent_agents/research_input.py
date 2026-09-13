"""Private, reconstructable input for one fixed USER research model attempt.

Only route metadata, opaque source references and keyed bindings enter the action
ledger. The complete body, configuration ciphertext and opened provider key stay
in this immutable in-memory object. This object is not authority: callers must
revalidate Plane's current source/config/session fences before permit or replay.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field

from astralplane.repositories.assignment_models import (
    AssignmentInputReference,
    AssignmentTransientInput,
)
from audit.pii import PrivateBindingKey, private_binding_key
from llm_config import research_profile as profile
from llm_config.research_profile import ResearchConfigSelection, ResearchRequest
from persistent_agents.dispatch_context import DispatchDenied, canonical
from persistent_agents.research_result import build_page_result
from persistent_agents.runtime_values import thaw


def route() -> dict:
    """Return the only durable routing fields for this supported model profile."""
    return {
        "kind": "model",
        "provider": "openai",
        "model": profile.MODEL,
        "max_output_tokens": profile.OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }


def _deny():
    raise DispatchDenied("assignment_research_binding_changed")


def fixed_reader_source(record) -> dict:
    """Return the sole source whose complete tool policy has a qualified fence."""
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


def _record_identity(record) -> str:
    """Bind immutable operation content, excluding mutable usage and lease state."""
    fixed_reader_source(record)
    operation = thaw(record.operation)
    if (
        record.execution_profile != "one_shot"
        or operation.get("version") != 2
        or operation.get("kind") != "research"
        or operation.get("source_retention") != "operation"
        or operation.get("authority", {}).get("origin") != "interactive"
        or operation.get("authority", {}).get("reference_kind") != "session_incarnation"
    ):
        _deny()
    return canonical(
        {
            "owner_id": record.owner_id,
            "assignment_id": record.assignment_id,
            "instruction_revision": record.instruction_revision,
            "control_epoch": record.control_epoch,
            "definition": thaw(record.definition),
            "operation": operation,
        }
    )


def _source_identity(record, action) -> tuple[str, dict]:
    """Require an authentic succeeded fixed reader result and exact settled attempt."""
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
    # Batch A checks all source facts and its own action/result binding. Empty
    # selection validates provenance without creating any generated claim.
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
        # Plane's public result projection can add disposition-derived fields.
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


@dataclass(frozen=True, slots=True)
class ResearchInput:
    """Exact private input with reusable named-key and source/config comparisons."""

    owner_id: str = field(repr=False)
    assignment_id: str
    source_action_id: str
    payload_binding: str = field(repr=False)
    _record_json: str = field(repr=False)
    _source_json: str = field(repr=False)
    _config: ResearchConfigSelection = field(repr=False)
    _key: PrivateBindingKey = field(repr=False)
    _request: ResearchRequest = field(repr=False)

    @classmethod
    async def capture(cls, record, source, *, config_store, key_id=None):
        """Freeze source and exact uncached USER selection without storing prompts."""
        try:
            record_json = _record_identity(record)
            source_json, observation = _source_identity(record, source)
            key = private_binding_key(key_id)
            config = profile.select_config(
                await config_store.capture_user(record.owner_id),
                store=config_store,
                binding_key=key,
            )
            if config.owner_id != record.owner_id:
                _deny()
            request = profile.build_request(record.definition.instructions, observation)
            binding = key.sign(
                "input",
                canonical(
                    {
                        "profile": profile.PROFILE,
                        "record": record_json,
                        "source": source_json,
                        "config_revision": config.revision,
                        "body": request.body.decode("utf-8"),
                    }
                ).encode("utf-8"),
            )
            return cls(
                record.owner_id,
                record.assignment_id,
                source.action_id,
                binding,
                record_json,
                source_json,
                config,
                key,
                request,
            )
        except (ValueError, TypeError, KeyError, AttributeError, PermissionError):
            _deny()

    @property
    def key_id(self) -> str:
        """Return only the nonsecret exact retained key identifier."""
        return self._key.key_id

    @property
    def passage_ids(self) -> tuple[str, ...]:
        """Return the closed source selection domain without its prose."""
        return self._request.passage_ids

    def body(self) -> dict:
        """Produce a detached complete request; mutations cannot alter this input."""
        return json.loads(self._request.body)

    def transient(self) -> AssignmentTransientInput:
        """Return only route reconstruction metadata for Plane's existing contract."""
        return AssignmentTransientInput(
            binding_key_id=self.key_id,
            payload_binding=self.payload_binding,
            source_retention="operation",
            references=(
                AssignmentInputReference(
                    kind="source", resource_id=self.source_action_id, revision=1
                ),
            ),
        )

    def assert_record(self, record) -> None:
        """Refuse a different owner/instruction/control/original session selection."""
        if _record_identity(record) != self._record_json:
            _deny()

    def assert_current(self, record, source, config_row) -> None:
        """Compare already guarded/locked current rows and re-resolve the exact key."""
        self.assert_record(record)
        if (
            _source_identity(record, source)[0] != self._source_json
            or not self._config.matches(config_row)
            or private_binding_key(self.key_id) != self._key
        ):
            _deny()

    def assert_body(self, owner_id, body) -> None:
        """Bind exact final kwargs before and after every awaited dispatch gate."""
        if (
            owner_id != self.owner_id
            or canonical(body).encode("utf-8") != self._request.body
        ):
            _deny()

    def assert_action(self, action) -> None:
        """Bind a stored route-only intent; generic/cached model actions never pass."""
        if (
            action.owner_id != self.owner_id
            or action.assignment_id != self.assignment_id
            or thaw(action.intent.request) != route()
            or action.intent.request_digest != self.payload_binding
            or thaw(action.intent.transient_input) != thaw(self.transient())
            or action.intent.boundary != "unreplayable"
            or action.intent.sensitivity != "ordinary"
            or action.intent.interactive_only
            or action.intent.task_id is not None
            or action.intent.event_id is not None
        ):
            _deny()

    def selection_result(self, selected) -> dict:
        """Retain only exact selected IDs and existing source bindings, never prose."""
        source = json.loads(self._source_json)
        # Existing deterministic builder enforces exact distinct selection.
        build_page_result(
            source["observation"],
            list(selected),
            source_action_id=self.source_action_id,
            source_result_digest=source["result_digest"],
        )
        return {
            "version": 1,
            "profile": profile.PROFILE,
            "passage_ids": list(selected),
            "source_action_id": self.source_action_id,
            "source_result_digest": source["result_digest"],
        }

    def receipt_digest(self, *, action_id, attempt_id, outcome, result, actual) -> str:
        """Sign a factual attempt receipt even if current key authority is retired."""
        return self._key.sign(
            "result",
            canonical(
                {
                    "profile": profile.PROFILE,
                    "payload_binding": self.payload_binding,
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                    "outcome": outcome,
                    "result": thaw(result),
                    "actual": thaw(actual),
                }
            ).encode("utf-8"),
        )

    def retained_result(self, action) -> dict:
        """Authenticate cached output only after current source/config guards."""
        self.assert_action(action)
        retained = thaw(action.result)
        if (
            action.state != "succeeded"
            or not action.ever_started
            or not retained
            or retained.get("result_available") is not True
            or retained.get("outcome") != "succeeded"
            or not action.attempts
        ):
            _deny()
        attempt = thaw(action.attempts[-1])
        disposition = retained.get("result_disposition", {})
        result = retained.get("result")
        expected = {
            key: value
            for key, value in retained.items()
            if key not in {"result_available", "reacquisition_reason"}
        }
        if (
            attempt.get("state") != "succeeded"
            or disposition.get("binding_key_id") != self.key_id
            or attempt.get("outcome") != expected
            or not isinstance(result, dict)
            or result != self.selection_result(result.get("passage_ids", []))
            or type(retained.get("result_digest")) is not str
            or not hmac.compare_digest(
                retained["result_digest"],
                self.receipt_digest(
                    action_id=action.action_id,
                    attempt_id=attempt["attempt_id"],
                    outcome="succeeded",
                    result=result,
                    actual=retained.get("actual"),
                ),
            )
        ):
            _deny()
        return result


async def invoke_fixed_user_model(
    orch,
    websocket,
    messages,
    context,
    *,
    tools_desc,
    temperature,
    feature,
    response_format,
    reasoning_effort,
    allow_stream,
    stream_chat_id,
):
    """Enter only from ordinary _call_llm with the private fixed model capability.

    No SDK/default/config cache, fallback, streaming or client content delivery is
    reachable. The isolated transport remains the sole physical HTTP boundary.
    """
    from llm_config.types import CredentialSource, ResolvedConfig
    from shared import isolated_http

    selected = context.research_input
    if (
        type(selected) is not ResearchInput
        or tools_desc is not None
        or temperature is not None
        or feature != "persistent_assignment"
        or response_format != {"type": "json_object"}
        or reasoning_effort is not None
        or allow_stream
        or stream_chat_id is not None
        or orch._llm_context_user_id(websocket) != selected.owner_id
    ):
        _deny()
    body = selected.body()
    body["messages"] = messages
    selected.assert_body(context.owner_id, body)
    actor, principal = orch._llm_audit_principals(websocket)
    response = None

    async def physical():
        nonlocal response
        selected.assert_body(context.owner_id, body)
        response = await isolated_http.request(
            "POST",
            profile.ENDPOINT,
            api_key=selected._config._api_key,
            json_body=body,
            allowed_private_hosts=(),
            max_response_bytes=profile.MAX_RESPONSE_BYTES,
            timeout_seconds=60,
        )
        return response

    try:
        await context.invoke_model(physical, body)
        parsed = profile.parse_response(
            response.body,
            status_code=response.status_code,
            passage_ids=selected.passage_ids,
        )
        return parsed, parsed.usage
    finally:
        parsed = (
            profile.parse_response(
                response.body,
                status_code=response.status_code,
                passage_ids=selected.passage_ids,
            )
            if response is not None
            else None
        )
        await orch._record_llm_call(
            orch.audit_recorder,
            actor_user_id=actor,
            auth_principal=principal,
            feature=feature,
            credential_source=CredentialSource.USER,
            resolved=ResolvedConfig(profile.BASE_URL, profile.MODEL),
            routed_model=profile.MODEL,
            total_tokens=parsed.usage.total_tokens if parsed and parsed.usage else None,
            outcome="success"
            if parsed and parsed.passage_ids is not None
            else "failure",
        )
