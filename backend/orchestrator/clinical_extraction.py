"""Runs one bounded clinical proposal only inside the canonical governed FHIR host.
Captured USER configuration, existing context accounting and original human authority fence every provider effect and delivery.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
from urllib.parse import urlsplit
from uuid import uuid4

from agents.fhir import extraction
from agents.fhir.extraction_prompts import build_messages
from agents.fhir.extraction_schema import MAX_RESPONSE_CHARS
from audit.pii import private_binding_key
from audit.recorder import Recorder
from llm_config.audit_events import _assert_no_api_key, record_llm_call
from llm_config.data_sharing import DataSharingStore
from llm_config.client_factory import KEYLESS_API_KEY_SENTINEL
from llm_config.local_endpoint import classify_endpoint
from llm_config.evidence_transport import _endpoint, _private_hosts
from llm_config.types import CredentialSource, ResolvedConfig
from llm_config.user_store import CapturedUserLLMConfig, UserLLMConfigStore
from orchestrator import evidence_context
from orchestrator.context_authority import current_context_authority
from orchestrator.context_usage import ContextUsage
from orchestrator.human_request_authority import _Composition
from shared import isolated_http
from shared.feature_flags import flags
from shared.protocol import MCPResponse

AGENT, TOOL = "fhir-1", "extract_note_fields"
OUTPUT_TOKENS = 4096
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 384 * 1024
MAX_SECONDS = 60
_EXPECTED_TOOL = extraction.extract_note_fields


class ClinicalUnavailable(ValueError):
    def __init__(self, code="EXTRACT_HOST_UNAVAILABLE", *, usage=None):
        self.code, self.usage = code, usage
        super().__init__(code)


def _json(value):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
        default=encode,
    )


def _valid_ownership(value):
    if value is None:
        return True
    return (
        type(value) is dict
        and set(value)
        == {"agent_id", "owner_email", "is_public", "created_at", "updated_at"}
        and value["agent_id"] == AGENT
        and type(value["owner_email"]) is str
        and type(value["is_public"]) is bool
        and all(
            item is None or type(item) is int and item >= 0
            for item in (value["created_at"], value["updated_at"])
        )
    )


def _identity(orch):
    from agents.fhir import mcp_tools
    from agents.fhir.fhir_agent import FhirAgent
    from agents.fhir.mcp_server import MCPServer
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.terminal_host_review import _canonical_card, _in_process

    agent = _in_process(orch, AGENT, "fhir")
    if (
        type(orch) is not Orchestrator
        or type(agent) is not FhirAgent
        or type(agent.mcp_server) is not MCPServer
        or agent.mcp_server.tools is not mcp_tools.TOOL_REGISTRY
        or mcp_tools.TOOL_REGISTRY.get(TOOL, {}).get("function") is not _EXPECTED_TOOL
        or extraction.extract_note_fields is not _EXPECTED_TOOL
    ):
        raise ClinicalUnavailable()
    card = _canonical_card(orch, agent, AGENT)
    if card is None:
        raise ClinicalUnavailable()
    return agent, card


def _resources(orch):
    from orchestrator.orchestrator import Orchestrator
    from agents.fhir.fhir_agent import FhirAgent
    from agents.fhir.mcp_server import MCPServer
    from orchestrator.tool_permissions import ToolPermissionManager
    from orchestrator.user_agents import UserAgentRegistry

    composition = orch.runtime_composition
    plane = composition.plane
    boundary = orch.human_request_boundary
    store, consent = orch._llm_store, orch._data_sharing_store
    permissions, registry = orch.tool_permissions, orch.user_agent_registry
    if (
        type(store) is not UserLLMConfigStore
        or type(consent) is not DataSharingStore
        or store._repository.plane_runtime is not plane.runtime
        or store._repository.repository is not plane.repositories.encrypted_llm_config
        or consent._repository.plane_runtime is not plane.runtime
        or consent._repository.repository
        is not plane.repositories.preferences.data_sharing
        or type(permissions) is not ToolPermissionManager
        or type(registry) is not UserAgentRegistry
        or permissions._policy.plane_runtime is not plane.runtime
        or permissions._policy.repository is not plane.repositories.tool_policy_state
        or permissions._agents.plane_runtime is not plane.runtime
        or permissions._agents.repository is not plane.repositories.agents
        or registry._agents.plane_runtime is not plane.runtime
        or registry._agents.repository is not plane.repositories.agents
        or type(orch.audit_recorder) is not Recorder
        or orch.audit_recorder._repo is not orch.audit_repo
        or getattr(orch._context_model_call, "__func__", None)
        is not Orchestrator._context_model_call
    ):
        raise ClinicalUnavailable()
    return (
        composition,
        plane,
        plane.runtime,
        plane.repositories,
        boundary,
        orch.web_sessions,
        orch.audit_repo,
        orch.audit_recorder,
        store,
        store._fernet,
        consent,
        orch.work_admission,
        orch.tool_permissions,
        orch.lets_runtime,
        FhirAgent.handle_mcp_request,
        MCPServer.process_request,
        MCPServer._respond,
        extraction.prepare_note,
        extraction.evaluate_proposal,
        build_messages,
        registry,
        registry._agents,
        permissions._policy,
        permissions._agents,
    )


@dataclass(slots=True, repr=False)
class ClinicalExtractionService:
    host: object
    agent: object
    card: str
    resources: tuple = field(repr=False)
    graph: object = field(repr=False)
    accounting: tuple | None = field(default=None, repr=False)

    def current(self):
        agent, card = _identity(self.host)
        if (
            getattr(self.host, "_clinical_extraction", None) is not self
            or agent is not self.agent
            or card != self.card
            or any(
                a is not b
                for a, b in zip(_resources(self.host), self.resources, strict=True)
            )
        ):
            raise ClinicalUnavailable()
        self.graph.assert_host_current(self.host)

    def bind_accounting(self, context):
        if (
            type(context) is not evidence_context.EvidenceContext
            or context.orchestrator is not self.host
            or type(context.usage) is not ContextUsage
            or getattr(context.usage._record, "__self__", None) is not context
            or getattr(context.usage._record, "__func__", None)
            is not evidence_context.EvidenceContext._persist
            or getattr(context.usage._recover, "__self__", None) is not context
            or getattr(context.usage._recover, "__func__", None)
            is not evidence_context.EvidenceContext._recover
        ):
            raise ClinicalUnavailable("EXTRACT_CONTEXT_UNAVAILABLE")
        selected = (context, context.usage)
        if self.accounting is None:
            self.accounting = selected
        elif any(a is not b for a, b in zip(self.accounting, selected, strict=True)):
            raise ClinicalUnavailable("EXTRACT_CONTEXT_UNAVAILABLE")
        return self.accounting

    async def invoke(self, websocket, args, owner, chat, timeout):
        from orchestrator.agent_identity import verified_identity_for
        from persistent_agents.dispatch_context import current_dispatch

        self.current()
        if not flags.is_enabled("clinical_note_extract"):
            raise ClinicalUnavailable("EXTRACT_DISABLED")
        context = getattr(self.host, "_evidence_context", None)
        if not evidence_context.enabled():
            raise ClinicalUnavailable("EXTRACT_CONTEXT_UNAVAILABLE")
        accounting = self.bind_accounting(context)
        authority = current_context_authority(
            orchestrator=self.host, websocket=websocket, chat_id=chat
        )
        turn = authority._turn
        if (
            current_dispatch() is not None
            or authority.owner_id != owner
            or turn.foreground is None
            or any(
                getattr(turn, name) is not None
                for name in ("background", "machine", "parent")
            )
            or type(args) is not dict
            or set(args) - {"note_text", "note_id"}
            or verified_identity_for(
                self.agent.card, json.loads(turn.origin.claims_json)
            )
            is None
        ):
            raise ClinicalUnavailable("EXTRACT_CONTEXT_UNAVAILABLE")
        note, label, digest = extraction.prepare_note(
            args.get("note_text", ""), args.get("note_id", "")
        )
        await authority.verify(
            orchestrator=self.host, websocket=websocket, chat_id=chat
        )
        self.current()
        capture = await self.host._llm_store.capture_user(owner)
        if type(capture) is not CapturedUserLLMConfig or capture.owner_id != owner:
            raise ClinicalUnavailable("EXTRACT_CONFIG_UNAVAILABLE")
        record = capture._record
        _assert_no_api_key(
            {
                "provider": record.provider,
                "base_url": record.base_url,
                "model": record.model,
            }
        )
        endpoint = _endpoint(record.base_url)
        if endpoint.scheme != "https":
            raise ClinicalUnavailable("EXTRACT_CONFIG_UNAVAILABLE")
        route = record.base_url.rstrip("/") + "/chat/completions"
        if endpoint.path != urlsplit(route).path:
            raise ClinicalUnavailable("EXTRACT_CONFIG_UNAVAILABLE")
        hosts = tuple(sorted(_private_hosts()))
        key = private_binding_key()
        sealed = key.sign("config", _json(asdict(record)).encode())
        budget = await context.budget(owner, capture)
        request = {
            "model": record.model,
            "messages": build_messages(note, label, digest),
            "stream": False,
            "store": False,
            "n": 1,
            "response_format": {"type": "json_object"},
            budget.output_parameter: min(OUTPUT_TOKENS, budget.max_output_tokens),
        }
        if len(_json(request).encode()) > MAX_REQUEST_BYTES:
            raise ClinicalUnavailable("EXTRACT_REQUEST_LIMIT")
        request_identity = _json(request)
        origin = asyncio.current_task()
        principal = (
            json.loads(turn.origin.claims_json).get("preferred_username") or owner
        )
        recorder = self.resources[7]
        ledger = context.usage
        attempt = str(uuid4())
        physical_task = None
        reported = None
        sent = False

        def permitted():
            from orchestrator.tool_permissions import turn_permission_memo

            permissions, registry = (
                self.host.tool_permissions,
                self.host.user_agent_registry,
            )
            with turn_permission_memo():
                safe = registry.get_agent_is_safe(AGENT)
                ownership = registry.get_agent_ownership(AGENT)
                if type(safe) is not bool or not _valid_ownership(ownership):
                    raise ClinicalUnavailable("EXTRACT_AUTHORITY_UNAVAILABLE")
                baseline = (
                    flags.is_enabled("safe_agents") is True
                    and safe
                    and (ownership is None or ownership["is_public"])
                )
                scope = permissions.get_tool_scope(AGENT, TOOL)
                effective = permissions.get_effective_tool_permissions(
                    owner, AGENT, safe_default=baseline
                )
                choice = permissions.get_tool_selection(owner, AGENT)
                if choice is not None and (
                    type(choice) is not list
                    or any(type(item) is not str for item in choice)
                ):
                    raise ClinicalUnavailable("EXTRACT_AUTHORITY_UNAVAILABLE")
                from agents.fhir.mcp_tools import TOOL_REGISTRY

                return (
                    scope == TOOL_REGISTRY[TOOL]["scope"]
                    and effective.get(TOOL, {}).get(scope) is True
                    and permissions.is_tool_allowed(owner, AGENT, TOOL) is True
                    and permissions.is_agent_disabled(owner, AGENT) is False
                    and (choice is None or TOOL in choice)
                )

        async def guard():
            self.current()
            if (
                origin.done()
                or origin.cancelling()
                or not flags.is_enabled("clinical_note_extract")
                or not evidence_context.enabled()
                or getattr(self.host, "_evidence_context", None) is not context
                or self.bind_accounting(context) is not accounting
                or context.usage is not ledger
                or _json(request) != request_identity
                or getattr(ledger._record, "__self__", None) is not context
                or getattr(ledger._record, "__func__", None)
                is not evidence_context.EvidenceContext._persist
                or getattr(ledger._recover, "__self__", None) is not context
                or getattr(ledger._recover, "__func__", None)
                is not evidence_context.EvidenceContext._recover
                or current_context_authority(
                    orchestrator=self.host, websocket=websocket, chat_id=chat
                )
                is not authority
                or authority._turn is not turn
                or tuple(sorted(_private_hosts())) != hosts
                or not key.verify(
                    "config",
                    b"clinical-config-key",
                    private_binding_key(key.key_id).sign(
                        "config", b"clinical-config-key"
                    ),
                )
            ):
                raise ClinicalUnavailable("EXTRACT_AUTHORITY_UNAVAILABLE")
            await authority.verify(
                orchestrator=self.host, websocket=websocket, chat_id=chat
            )
            current = await self.host._llm_store.capture_user(owner)
            acknowledgment = await self.host._data_sharing_store.state(owner)
            if (
                current is None
                or not capture.matches(current._record)
                or not key.verify(
                    "config", _json(asdict(current._record)).encode(), sealed
                )
                or acknowledgment.acknowledged is not True
                or await context.budget(owner, current) != budget
                or not await asyncio.to_thread(permitted)
                or self.host.security_flags.get(AGENT, {}).get(TOOL, {}).get("blocked")
            ):
                raise ClinicalUnavailable("EXTRACT_AUTHORITY_UNAVAILABLE")
            self.current()
            authority.assert_current(
                orchestrator=self.host, websocket=websocket, chat_id=chat
            )
            if origin.done() or origin.cancelling():
                raise ClinicalUnavailable("EXTRACT_AUTHORITY_UNAVAILABLE")

        async def physical():
            nonlocal physical_task, reported, sent
            physical_task = asyncio.current_task()
            await guard()
            api_key = self.host._llm_store.open_captured_user_key(capture)
            if api_key in ("", KEYLESS_API_KEY_SENTINEL):
                if (
                    record.provider not in {"ollama", "lmstudio", "custom"}
                    or endpoint.host not in hosts
                    or not classify_endpoint(record.base_url).local
                ):
                    raise ClinicalUnavailable("EXTRACT_CONFIG_UNAVAILABLE")
                api_key = ""
            sent = True
            try:
                response = await isolated_http.request(
                    "POST",
                    route,
                    api_key=api_key,
                    json_body=request,
                    allowed_private_hosts=hosts,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                    timeout_seconds=min(MAX_SECONDS, timeout),
                )
                final = urlsplit(response.final_url)
                if (
                    (
                        final.scheme,
                        final.hostname,
                        final.port or (443 if final.scheme == "https" else 80),
                        final.path,
                    )
                    != (endpoint.scheme, endpoint.host, endpoint.port, endpoint.path)
                    or final.username is not None
                    or final.password is not None
                    or final.query
                    or final.fragment
                ):
                    raise ClinicalUnavailable("EXTRACT_RESPONSE_INVALID")
                parsed = parse_response(
                    response,
                    record.model,
                    request[budget.output_parameter],
                    budget.context_tokens,
                )
                reported = parsed.usage
                await guard()
                return parsed
            except BaseException as exc:
                if reported is not None:
                    exc.usage = reported
                raise

        try:
            await guard()
            result = await self.host._context_model_call(
                websocket,
                chat,
                "clinical_note_extract",
                record.model,
                physical,
                attempt_id=attempt,
                request=request,
                base_url=record.base_url,
                provider_capture=capture,
            )
            await guard()
            outcome = extraction.evaluate_proposal(note, label, result.content)
            await guard()
            await record_llm_call(
                recorder,
                actor_user_id=owner,
                auth_principal=principal,
                feature="clinical_note_extract",
                credential_source=CredentialSource.USER,
                resolved=ResolvedConfig(record.base_url, record.model),
                total_tokens=reported["total_tokens"],
                outcome="success",
                correlation_id=attempt,
            )
            await guard()
            from agents.fhir.mcp_server import MCPServer

            return MCPServer._respond(f"clinical-{attempt}", outcome)
        except asyncio.CancelledError as cancelled:
            if physical_task is not None and not physical_task.done():
                physical_task.cancel()
                while not physical_task.done():
                    try:
                        await asyncio.shield(physical_task)
                    except (asyncio.CancelledError, Exception):
                        pass
            try:
                if sent:
                    await record_llm_call(
                        recorder,
                        actor_user_id=owner,
                        auth_principal=principal,
                        feature="clinical_note_extract",
                        credential_source=CredentialSource.USER,
                        resolved=ResolvedConfig(record.base_url, record.model),
                        total_tokens=None,
                        outcome="failure",
                        upstream_error_class="cancelled",
                        correlation_id=attempt,
                    )
            finally:
                raise cancelled
        except Exception as exc:
            usage = getattr(exc, "usage", None) or reported
            if sent:
                await record_llm_call(
                    recorder,
                    actor_user_id=owner,
                    auth_principal=principal,
                    feature="clinical_note_extract",
                    credential_source=CredentialSource.USER,
                    resolved=ResolvedConfig(record.base_url, record.model),
                    total_tokens=usage.get("total_tokens") if usage else None,
                    outcome="failure",
                    upstream_error_class=type(exc).__name__,
                    correlation_id=attempt,
                )
            code = (
                exc.code
                if isinstance(exc, ClinicalUnavailable)
                else "EXTRACT_PROVIDER_UNAVAILABLE"
            )
            raise ClinicalUnavailable(code) from None


@dataclass(frozen=True, slots=True)
class _Response:
    content: str = field(repr=False)
    usage: dict = field(repr=False)


def _wire_json(body):
    def pairs(items):
        result = {}
        for name, item in items:
            if name in result:
                raise ValueError
            result[name] = item
        return result

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError
        return value

    def invalid(_):
        raise ValueError

    value = json.loads(
        body.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_constant=invalid,
        parse_float=number,
    )
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 16 or count > 10000:
            raise ValueError
        if type(item) is dict:
            pending.extend(
                (child, depth + 1) for child in (*item.keys(), *item.values())
            )
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            item.encode("utf-8", errors="strict")
    return value


def parse_response(response, model, output_tokens, context_tokens):
    usage = None
    try:
        if (
            type(response) is not isolated_http.IsolatedHttpResponse
            or response.status_code != 200
            or len(response.body) > MAX_RESPONSE_BYTES
            or response.content_type.split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise ValueError
        if type(response.body) is not bytes:
            raise ValueError
        value = _wire_json(response.body)
        if type(value) is not dict or set(value) - {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
            "system_fingerprint",
            "service_tier",
        }:
            raise ValueError
        reported = value.get("usage")
        if (
            type(reported) is not dict
            or not {"prompt_tokens", "completion_tokens", "total_tokens"}
            <= reported.keys()
            or set(reported)
            - {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_tokens_details",
                "completion_tokens_details",
            }
            or any(
                type(reported.get(name)) is not int
                or not 0 <= reported[name] <= 2**53 - 1
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            or reported["total_tokens"]
            != reported["prompt_tokens"] + reported["completion_tokens"]
        ):
            raise ValueError
        usage = {
            name: reported[name]
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        for name, allowed, maximum in (
            (
                "prompt_tokens_details",
                {"cached_tokens", "audio_tokens"},
                usage["prompt_tokens"],
            ),
            (
                "completion_tokens_details",
                {
                    "reasoning_tokens",
                    "audio_tokens",
                    "accepted_prediction_tokens",
                    "rejected_prediction_tokens",
                },
                usage["completion_tokens"],
            ),
        ):
            details = reported.get(name)
            if details is not None and (
                type(details) is not dict
                or set(details) - allowed
                or any(
                    type(count) is not int or not 0 <= count <= maximum
                    for count in details.values()
                )
            ):
                raise ValueError
        usage["cached_tokens"] = (reported.get("prompt_tokens_details") or {}).get(
            "cached_tokens"
        )
        if (
            usage["completion_tokens"] > output_tokens
            or usage["total_tokens"] > context_tokens
            or type(value.get("id")) is not str
            or not 1 <= len(value["id"]) <= 256
            or value.get("object") != "chat.completion"
            or type(value.get("created")) is not int
            or value["created"] < 0
            or value.get("model") != model
            or type(value.get("choices")) is not list
            or len(value["choices"]) != 1
        ):
            raise ValueError
        choice = value["choices"][0]
        if (
            type(choice) is not dict
            or set(choice) - {"index", "message", "finish_reason", "logprobs"}
            or type(choice.get("index")) is not int
            or choice["index"] != 0
            or choice.get("finish_reason") != "stop"
            or choice.get("logprobs") is not None
        ):
            raise ValueError
        message = choice.get("message")
        if (
            type(message) is not dict
            or set(message) - {"role", "content", "refusal", "annotations"}
            or message.get("role") != "assistant"
            or message.get("refusal") is not None
            or message.get("annotations", []) != []
            or type(message.get("content")) is not str
            or not message["content"].strip()
            or len(message["content"]) > MAX_RESPONSE_CHARS
        ):
            raise ValueError
        if (
            type(_wire_json(message["content"].encode("utf-8", errors="strict")))
            is not dict
        ):
            raise ValueError
        return _Response(message["content"], usage)
    except (
        ValueError,
        TypeError,
        UnicodeError,
        KeyError,
        OverflowError,
        RecursionError,
    ):
        raise ClinicalUnavailable("EXTRACT_RESPONSE_INVALID", usage=usage) from None


def register(orch):
    try:
        agent, card = _identity(orch)
        service = ClinicalExtractionService(
            orch,
            agent,
            card,
            _resources(orch),
            _Composition.capture_host(orch.human_request_boundary),
        )
        orch._clinical_extraction = service
        service.current()
        context = getattr(orch, "_evidence_context", None)
        if context is not None:
            service.bind_accounting(context)
        return True
    except Exception:
        return False


def current(orch):
    try:
        service = getattr(orch, "_clinical_extraction", None)
        if type(service) is not ClinicalExtractionService:
            return False
        service.current()
        return True
    except Exception:
        return False


async def invoke(orch, websocket, args, *, owner, chat, timeout):
    try:
        service = getattr(orch, "_clinical_extraction", None)
        if type(service) is not ClinicalExtractionService:
            raise ClinicalUnavailable()
        return await service.invoke(websocket, args, owner, chat, timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, (ClinicalUnavailable, extraction.FhirError))
            else "EXTRACT_AUTHORITY_UNAVAILABLE"
        )
        return MCPResponse(
            error={
                "code": code,
                "message": "Clinical extraction is unavailable under the current host, context or provider binding",
                "retryable": False,
            }
        )
