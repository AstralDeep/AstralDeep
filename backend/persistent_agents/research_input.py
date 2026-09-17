"""Private input for one fixed USER research model attempt.

Only route metadata, opaque source references and keyed bindings enter the action
ledger. The complete body, configuration ciphertext and opened provider key stay
in this immutable in-memory object. This object is not authority: callers must
revalidate Plane's current source/config/session fences before permit or replay.
Non-retained input additionally requires its original live acquisition proof;
an unavailable ledger row cannot reconstruct a prompt.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import asdict, dataclass, field

from astralplane.repositories.assignment_models import (
    AssignmentActionOutcome,
    AssignmentInputReference,
    AssignmentResultDisposition,
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


_SELECTED_INPUT_DOMAIN = b"astral.research.selected-input/v3\x00"
_SELECTED_RESULT_DOMAIN = b"astral.research.selected-result/v3\x00"

# Transport codes the isolated helper raises strictly before any request bytes
# exist: egress validation (URL policy and DNS resolution) precedes the socket.
# A refused connection, TLS failure or timeout shares the transport's
# ``unreachable``/``deadline`` codes with post-send losses, so those stay
# uncertain until the transport itself distinguishes them.
PRE_SEND_FAILURE_CODES = frozenset({"egress_blocked"})
PRE_SEND_FAILURE = "assignment_provider_unreachable"


@dataclass(frozen=True, slots=True)
class UnsentAttempt:
    """An issued permit whose request provably never left this process.

    Only the closed pre-send transport codes construct it. Settlement records a
    failed attempt with known-zero provider usage; it is never a response.
    """

    code: str

    def __post_init__(self):
        if self.code not in PRE_SEND_FAILURE_CODES:
            raise ValueError("unsent attempt requires a pre-send transport code")


def receipt_payload(*, payload_binding, action_id, attempt_id, outcome, result, actual,
                    selected_input=None, profile_name=profile.PROFILE):
    """Versioned factual receipt bytes; the legacy representation is unchanged."""
    values = dict(profile=profile_name, payload_binding=payload_binding,
                  action_id=action_id, attempt_id=attempt_id, outcome=outcome,
                  result=thaw(result), actual=thaw(actual))
    if selected_input is None:
        return canonical(values).encode("utf-8")
    values["selected_input"] = selected_input
    return _SELECTED_RESULT_DOMAIN + canonical(values).encode("utf-8")


@dataclass(frozen=True, slots=True, repr=False)
class ResearchGuidance:
    """Original persisted selection plus private, exact current-row expansion.

    Capture only under the existing operation lifecycle transaction. This is
    input identity, never authority; every use still requires its ordinary
    session, assignment, admission, source, configuration and policy checks.
    """

    _record_json: str
    snapshot: object
    captured: object
    _key: PrivateBindingKey | None
    _service: object
    _sessions: object

    @classmethod
    def capture(cls, tx, repository, record, *, orchestrator, runtime, now, service, sessions):
        from personalization.selected_guidance_boundary import SelectedGuidanceBoundary

        snapshot = repository.get_selected_input(tx, owner_id=record.owner_id,
                                                assignment_id=record.assignment_id)
        captured = key = None
        if snapshot is not None:
            envelope = snapshot.envelope
            if envelope is None:
                # Old metadata cannot silently become an authenticated expansion.
                _deny()
            else:
                key = private_binding_key(envelope.binding_key_id)
                boundary = SelectedGuidanceBoundary(orchestrator, plane_runtime=runtime,
                    include_notes=any(ref.kind == "note" for ref in envelope.references))
                captured = boundary.capture(tx, owner_id=record.owner_id,
                    instruction=record.definition.instructions,
                    agent=None if envelope.agent is None else
                        (envelope.agent.agent_id, envelope.agent.revision_id),
                    references=envelope.references, binding_key=key, now=now)
                if canonical(asdict(captured.prepared.envelope)) != canonical(asdict(envelope)):
                    _deny()
        return cls(_record_identity(record), snapshot, captured, key, service, sessions)

    def metadata(self):
        """Detach only immutable IDs, versions and opaque authentication."""
        return None if self.captured is None else thaw(asdict(self.snapshot.envelope))

    def assert_record(self, record):
        if _record_identity(record) != self._record_json:
            _deny()

    def assert_local(self):
        if self.captured is not None:
            boundary = self.captured._boundary
            boundary.assert_current()
            orch = boundary.orch
            if (self._service.orch is not orch
                    or self._service.store.plane_runtime is not boundary.runtime
                    or getattr(orch, "persistent_assignments", None) is not self._service
                    or getattr(orch, "web_sessions", None) is not self._sessions):
                _deny()
            if private_binding_key(self._key.key_id) != self._key:
                _deny()
            if self.captured.agent_revision is not None:
                from orchestrator.agent_authoring import byo_enabled
                from orchestrator.projection_surfaces.authoring import DeclarativeAgentService
                parsed = DeclarativeAgentService._stored_definition(self.captured.agent_revision)
                limits = parsed.to_dict()["limits"]
                requested = json.loads(self._record_json)["definition"]["limits"]
                if (not byo_enabled() or not parsed.fixed_research_shape
                        or any(requested[name] > limits[period][name]
                            for period in ("daily", "lifetime")
                            for name in ("model_calls", "tool_calls", "tokens", "elapsed_ms"))
                        or requested["max_retries"] > limits["max_retries"]
                        or limits["step_timeout_ms"] < max(profile.RESERVED_MILLISECONDS,
                            self._service.tool_bound("web-research-1:fetch_page")["elapsed_ms"])
                        or any(limits[period]["spend_micro_units"] is not None
                               for period in ("daily", "lifetime"))):
                    _deny()

    def assert_current(self, tx, repository, record, *, authority_valid_until):
        """Compare the original header and final DB-time current-head facts."""
        self.assert_local()
        repository.assert_selected_input_current(tx, owner_id=record.owner_id,
            assignment_id=record.assignment_id,
            expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch,
            expected_state_version=record.state_version, expected=self.snapshot,
            authority_valid_until=authority_valid_until)
        self.assert_local()


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


def chat_definition(record) -> None:
    """Require the source-less chat shape: no tools, no scopes, retained turn."""
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


def _record_identity(record) -> str:
    """Bind immutable operation content, excluding mutable usage and lease state."""
    try:
        kind = record.operation.get("kind")
    except (AttributeError, TypeError):
        raise DispatchDenied("assignment_operation_profile_unavailable") from None
    if kind == "chat":
        try:
            chat_definition(record)
        except DispatchDenied:
            # A kind that does not describe its own definition is a changed
            # binding, never a different supported profile.
            _deny()
    else:
        fixed_reader_source(record)
    operation = thaw(record.operation)
    if (
        record.execution_profile != "one_shot"
        or operation.get("version") != 2
        or operation.get("kind") not in {"research", "chat"}
        or operation.get("source_retention") not in {"operation", "none"}
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
    _ephemeral: object = field(default=None, repr=False)
    _guidance: ResearchGuidance | None = field(default=None, repr=False)
    _config_binding: tuple | None = field(default=None, repr=False)
    _kind: str = field(default="research", repr=False)

    @classmethod
    async def capture_chat(cls, record, *, config_store, key_id=None, guidance=None):
        """Freeze one source-less owner turn; the prompt is never stored.

        The same private key, exact uncached USER configuration and opaque
        payload binding as research apply. Selected guidance is not composed
        into a chat turn: admission refuses it and a bound selection here denies.
        """
        from persistent_agents.chat_episode import CHAT_PROFILE, build_chat_request

        try:
            context = config_store._repository
            config_binding = (config_store, context, context.plane_runtime,
                              context.repository, config_store._fernet)
            record_json = _record_identity(record)
            chat_definition(record)
            if guidance is not None:
                if type(guidance) is not ResearchGuidance:
                    _deny()
                guidance.assert_record(record)
                guidance.assert_local()
                if guidance.metadata() is not None:
                    _deny()
            key = private_binding_key(key_id)
            config = profile.select_config(
                await config_store.capture_user(record.owner_id),
                store=config_store,
                binding_key=key,
            )
            if config.owner_id != record.owner_id:
                _deny()
            request = build_chat_request(record.definition.instructions)
            values = {
                "profile": CHAT_PROFILE,
                "record": record_json,
                "config_revision": config.revision,
                "body": request.body.decode("utf-8"),
            }
            binding = key.sign("input", canonical(values).encode("utf-8"))
            return cls(record.owner_id, record.assignment_id, None, binding, record_json,
                       canonical({"profile": CHAT_PROFILE}), config, key, request,
                       None, guidance, config_binding, "chat")
        except (ValueError, TypeError, KeyError, AttributeError, PermissionError):
            _deny()

    @classmethod
    async def capture(cls, record, source, *, config_store, key_id=None, ephemeral=None,
                      guidance=None):
        """Freeze source and exact uncached USER selection without storing prompts."""
        try:
            context = config_store._repository
            config_binding = (config_store, context, context.plane_runtime,
                              context.repository, config_store._fernet)
            record_json = _record_identity(record)
            fixed_reader_source(record)
            if record.operation.get("source_retention") == "none":
                from persistent_agents.research_recovery import EphemeralResearchSource
                if type(ephemeral) is not EphemeralResearchSource:
                    _deny()
                source_json, observation = ephemeral.identity(record, source)
            else:
                if ephemeral is not None:
                    _deny()
                source_json, observation = _source_identity(record, source)
            selected_input = None
            if guidance is not None:
                if type(guidance) is not ResearchGuidance:
                    _deny()
                guidance.assert_record(record)
                guidance.assert_local()
                selected_input = guidance.metadata()
                if selected_input is not None:
                    if key_id is not None and key_id != selected_input["binding_key_id"]:
                        _deny()
                    key_id = selected_input["binding_key_id"]
            key = private_binding_key(key_id)
            config = profile.select_config(
                await config_store.capture_user(record.owner_id),
                store=config_store,
                binding_key=key,
            )
            if config.owner_id != record.owner_id:
                _deny()
            request = (profile.build_request(record.definition.instructions, observation)
                       if selected_input is None else guidance.captured.compose(
                           observation=observation,
                           approved_request_bytes=profile.MAX_REQUEST_BYTES).request)
            values = {
                "profile": profile.PROFILE,
                "record": record_json,
                "source": source_json,
                "config_revision": config.revision,
                "body": request.body.decode("utf-8"),
            }
            prefix = b""
            if selected_input is not None:
                values["selected_input"] = selected_input
                prefix = _SELECTED_INPUT_DOMAIN
            binding = key.sign(
                "input",
                prefix + canonical(values).encode("utf-8"),
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
                ephemeral,
                guidance,
                config_binding,
            )
        except (ValueError, TypeError, KeyError, AttributeError, PermissionError):
            _deny()

    @property
    def key_id(self) -> str:
        """Return only the nonsecret exact retained key identifier."""
        return self._key.key_id

    @property
    def kind(self) -> str:
        """Return the closed one-shot kind this private input was captured for."""
        return self._kind

    @property
    def passage_ids(self) -> tuple[str, ...]:
        """Return the closed source selection domain without its prose."""
        return self._request.passage_ids

    def body(self) -> dict:
        """Produce a detached complete request; mutations cannot alter this input."""
        return json.loads(self._request.body)

    def parse(self, body, *, status_code):
        """Interpret one provider reply under this input's own closed profile."""
        if self._kind == "chat":
            from persistent_agents.chat_episode import parse_chat_response
            return parse_chat_response(body, status_code=status_code)
        return profile.parse_response(body, status_code=status_code, passage_ids=self.passage_ids)

    def transient(self) -> AssignmentTransientInput:
        """Return only route reconstruction metadata for Plane's existing contract."""
        references = () if self._kind == "chat" else (
            AssignmentInputReference(kind="source", resource_id=self.source_action_id, revision=1),
        )
        return AssignmentTransientInput(
            binding_key_id=self.key_id,
            payload_binding=self.payload_binding,
            source_retention="none" if self._ephemeral is not None else "operation",
            references=references
            + (() if self._guidance is None or self._guidance.captured is None else tuple(
                AssignmentInputReference(ref.kind, ref.resource_id, ref.revision)
                for ref in self._guidance.snapshot.references)),
        )

    def assert_record(self, record) -> None:
        """Refuse a different owner/instruction/control/original session selection."""
        if _record_identity(record) != self._record_json:
            _deny()
        if self._guidance is not None:
            self._guidance.assert_record(record)

    def assert_current(self, record, source, config_row) -> None:
        """Compare already guarded/locked current rows and re-resolve the exact key."""
        self.assert_record(record)
        if self._kind == "chat":
            chat_definition(record)
            current_source = canonical({"profile": json.loads(self._source_json)["profile"]})
            if source is not None or self.source_action_id is not None:
                _deny()
        else:
            current_source = (self._ephemeral.identity(record, source)[0]
                              if self._ephemeral is not None
                              else _source_identity(record, source)[0])
        if (
            current_source != self._source_json
            or not self._config.matches(config_row)
            or private_binding_key(self.key_id) != self._key
        ):
            _deny()
        if self._guidance is not None:
            self._guidance.assert_local()

    def assert_body(self, owner_id, body) -> None:
        """Bind exact final kwargs before and after every awaited dispatch gate."""
        if (
            owner_id != self.owner_id
            or canonical(body).encode("utf-8") != self._request.body
        ):
            _deny()

    def assert_local(self, *, orchestrator, runtime):
        """Final pure key/config/selection check after the last database wait."""
        if self._config_binding is None:
            _deny()
        store, context, original_runtime, repository, cipher = self._config_binding
        if (orchestrator._llm_store is not store or store._repository is not context
                or context.plane_runtime is not original_runtime or original_runtime is not runtime
                or context.repository is not repository
                or runtime.repositories.encrypted_llm_config is not repository
                or store._fernet is not cipher or private_binding_key(self.key_id) != self._key):
            _deny()
        if self._guidance is not None:
            self._guidance.assert_local()

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
        if self._kind != "research":
            _deny()
        source = json.loads(self._source_json)
        # Existing deterministic builder enforces exact distinct selection.
        build_page_result(
            source["observation"],
            list(selected),
            source_action_id=self.source_action_id,
            source_result_digest=source["result_digest"],
        )
        result = {
            "version": 1,
            "profile": profile.PROFILE,
            "passage_ids": list(selected),
            "source_action_id": self.source_action_id,
            "source_result_digest": source["result_digest"],
        }
        if self._guidance is not None and self._guidance.captured is not None:
            result.update(version=3, selected_input=self._guidance.metadata())
        return result

    def receipt_digest(self, *, action_id, attempt_id, outcome, result, actual) -> str:
        """Sign a factual attempt receipt even if current key authority is retired."""
        if self._kind == "chat":
            from persistent_agents.chat_episode import CHAT_PROFILE
            profile_name = CHAT_PROFILE
        else:
            profile_name = profile.PROFILE
        return self._key.sign(
            "result",
            receipt_payload(payload_binding=self.payload_binding, action_id=action_id,
                attempt_id=attempt_id, outcome=outcome, result=result, actual=actual,
                selected_input=None if self._guidance is None else self._guidance.metadata(),
                profile_name=profile_name),
        )

    def chat_result(self, action) -> dict:
        """Authenticate one retained chat answer against its keyed receipt."""
        from persistent_agents.chat_episode import chat_result_value

        if self._kind != "chat":
            _deny()
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
        try:
            rebuilt = chat_result_value(result["text"])
        except (TypeError, KeyError, ValueError):
            _deny()
        if (
            attempt.get("state") != "succeeded"
            or disposition.get("binding_key_id") != self.key_id
            or attempt.get("outcome") != expected
            or result != rebuilt
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

    def retained_result(self, action) -> dict:
        """Authenticate cached output only after current source/config guards."""
        if self._kind != "research":
            _deny()
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

    def ephemeral_result(self, action, selection) -> dict:
        """Authenticate live selection against an unavailable model receipt."""
        self.assert_action(action)
        if self._ephemeral is None:
            _deny()
        result = self.selection_result(selection["passage_ids"])
        expected = thaw(AssignmentActionOutcome(outcome="succeeded",
            result_digest=action.result["result_digest"], result={}, actual=thaw(action.result["actual"]),
            result_disposition=AssignmentResultDisposition(available=False,
                reason="retention_discarded", binding_key_id=self.key_id)))
        attempt = thaw(action.attempts[-1])
        if (action.state != "succeeded" or action.ever_started is not True
                or canonical(selection) != canonical(result)
                or attempt.get("state") != "succeeded"
                or canonical(attempt.get("outcome")) != canonical(expected)
                or canonical(thaw(action.result)) != canonical({**expected,
                    "result_available": False, "reacquisition_reason": "retention_discarded"})
                or not hmac.compare_digest(expected["result_digest"], self.receipt_digest(
                    action_id=action.action_id, attempt_id=attempt["attempt_id"],
                    outcome="succeeded", result=result, actual=expected["actual"]))):
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
    unsent = None

    async def physical():
        nonlocal response, unsent
        selected.assert_body(context.owner_id, body)
        try:
            response = await isolated_http.request(
                "POST",
                profile.ENDPOINT,
                api_key=selected._config._api_key,
                json_body=body,
                allowed_private_hosts=(),
                max_response_bytes=profile.MAX_RESPONSE_BYTES,
                timeout_seconds=60,
            )
        except isolated_http.IsolatedHttpError as error:
            # Only the closed pre-send codes become a known-zero failed
            # attempt. Every other transport outcome may have reached the
            # provider and keeps the existing uncertain settlement.
            if error.code not in PRE_SEND_FAILURE_CODES:
                raise
            unsent = UnsentAttempt(error.code)
            return unsent
        return response

    try:
        await context.invoke_model(physical, body)
        if unsent is not None:
            raise DispatchDenied(PRE_SEND_FAILURE)
        parsed = selected.parse(response.body, status_code=response.status_code)
        return parsed, parsed.usage
    finally:
        parsed = (
            selected.parse(response.body, status_code=response.status_code)
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
            outcome="success" if _accepted(parsed) else "failure",
        )


def _accepted(parsed) -> bool:
    """A usable reply under either closed profile; usage alone is not success."""
    if parsed is None:
        return False
    if type(parsed) is profile.ResearchResponse:
        return parsed.passage_ids is not None
    return getattr(parsed, "accepted", False) is True
