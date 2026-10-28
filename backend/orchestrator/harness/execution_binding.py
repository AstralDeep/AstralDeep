"""Selected model attempts bind the original durable harness capture to the current execution fence.
The ordinary audited runner uses this private binding for exact configuration, endpoint, bounds and delivery checks.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from llm_config import research_profile
from llm_config.provider_egress import capture_private_policy
from orchestrator.harness.production_profiles import HarnessSelectionError, deployment_for_capture
from orchestrator.harness.selection_service import HarnessSelectionService, select_captured_config

SELECTED_ATTEMPT_SECONDS = 60


@dataclass(frozen=True, slots=True, repr=False)
class ModelHarnessBinding:
    service: HarnessSelectionService
    coordinator: object
    owner: object
    fence: object
    owner_id: str
    capture: object
    config: object
    key: object
    websocket: object
    claims: object
    egress_policy: object = None

    @property
    def adapter_identity(self):
        return deployment_for_capture(self.capture).adapter_identity

    async def assert_current(self):
        if self.egress_policy is not None:
            self.egress_policy.assert_current()
        orch = self.service.orch
        if orch is not None and (orch._llm_context_user_id(self.websocket) != self.owner_id
                or orch.ui_sessions.get(self.websocket) is not self.claims):
            raise HarnessSelectionError("harness_owner_invalid", 403)
        await asyncio.to_thread(self.coordinator.harness_capture, owner=self.owner,
            fence=self.fence, service=self.service, owner_id=self.owner_id,
            expected=self.capture, config=self.config, key=self.key)
        if orch is not None and (orch._llm_context_user_id(self.websocket) != self.owner_id
                or orch.ui_sessions.get(self.websocket) is not self.claims):
            raise HarnessSelectionError("harness_owner_invalid", 403)

    def client(self, *, evidence_bound=False):
        from llm_config.client_factory import build_llm_client
        from llm_config.evidence_transport import guard_model_client
        from llm_config.types import CredentialSource
        from llm_config.user_store import PersistedLLMConfig
        row = self.config._capture._record
        config = PersistedLLMConfig(row.provider, row.base_url, row.model,
            self.service.config_store.open_captured_user_key(self.config._capture))
        client, source, resolved = build_llm_client(config, CredentialSource.USER,
            timeout=self.config.profile.reserved_milliseconds / 1000)
        client = guard_model_client(client, base_url=row.base_url)
        if evidence_bound:
            client._evidence_provider_capture = self.config._capture
        return client, source, resolved

    def request(self, kwargs):
        from openai.types.chat import ChatCompletionMessage
        if type(kwargs) is not dict or type(kwargs.get("messages", [])) not in (list, tuple):
            raise HarnessSelectionError("harness_request_invalid", 422)
        kwargs["messages"] = [item.model_dump(mode="json") if isinstance(item, ChatCompletionMessage)
                              else item for item in kwargs.get("messages", [])]
        if kwargs.get("model") != self.config.profile.model:
            raise HarnessSelectionError("harness_request_changed")
        kwargs.update(stream=False, store=False, n=1,
                      max_completion_tokens=self.config.profile.output_tokens)
        self.validate_request(kwargs)

    def validate_request(self, kwargs):
        if type(kwargs) is not dict:
            raise HarnessSelectionError("harness_request_invalid", 422)
        outputs = [kwargs[name] for name in ("max_tokens", "max_completion_tokens") if name in kwargs]
        if (kwargs.get("model") != self.config.profile.model or kwargs.get("stream") is not False
                or kwargs.get("store") is not False or type(kwargs.get("n")) is not int or kwargs["n"] != 1
                or len(outputs) != 1 or type(outputs[0]) is not int
                or not 1 <= outputs[0] <= self.config.profile.output_tokens):
            raise HarnessSelectionError("harness_request_changed")
        try:
            body = json.dumps(kwargs, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
            if len(body) > research_profile.MAX_REQUEST_BYTES:
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise HarnessSelectionError("harness_request_invalid", 422) from None

    async def invoke(self, request):
        from openai.types.chat import ChatCompletion
        from shared import isolated_http
        if self.egress_policy is None:
            raise HarnessSelectionError("harness_egress_unavailable")
        self.egress_policy.assert_current()
        self.validate_request(request)
        private = ({"require_private_endpoint": True}
                   if urlsplit(self.config.profile.endpoint).scheme == "http" else {})
        response = await isolated_http.request("POST", self.config.profile.endpoint,
            api_key=self.config._api_key, json_body=request,
            allowed_private_hosts=self.egress_policy.hosts,
            max_response_bytes=research_profile.MAX_RESPONSE_BYTES, timeout_seconds=SELECTED_ATTEMPT_SECONDS,
            **private)
        if response.status_code != 200:
            raise HarnessSelectionError("harness_provider_failed")
        try:
            return ChatCompletion.model_validate_json(response.body)
        except (ValueError, TypeError):
            raise HarnessSelectionError("harness_output_invalid") from None

    def response(self, response, *, request=None):
        usage = getattr(response, "usage", None)
        counts = tuple(getattr(usage, name, None) for name in
                       ("prompt_tokens", "completion_tokens", "total_tokens"))
        if (any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in counts)
                or counts[2] != counts[0] + counts[1]
                or research_profile.ResearchUsage(*counts, self.config.profile.context_tokens,
                    self.config.profile.output_tokens).exceeds_profile):
            raise HarnessSelectionError("harness_usage_invalid")
        if request is not None:
            self.validate_request(request)
            maximum = request.get("max_completion_tokens", request.get("max_tokens"))
            if counts[1] > maximum:
                raise HarnessSelectionError("harness_usage_invalid")
        choices = getattr(response, "choices", None)
        if not choices or getattr(choices[0], "finish_reason", None) == "length":
            raise HarnessSelectionError("harness_output_invalid")
        try:
            body = response.model_dump_json().encode("utf-8")
            if len(body) > research_profile.MAX_RESPONSE_BYTES:
                raise ValueError
        except (AttributeError, ValueError, TypeError, UnicodeError):
            raise HarnessSelectionError("harness_output_invalid") from None


async def prepare_model_binding(orchestrator, websocket, operation_context, persistent=None):
    from orchestrator.work_admission import OperationOwner, OwnerScope
    if persistent is not None and persistent.operation_fence is not None:
        owner, fence = persistent.operation_owner, persistent.operation_fence
        owner_id = persistent.owner_id
    elif operation_context:
        owner, fence = operation_context.get("owner"), operation_context.get("execution_fence")
        owner_id = orchestrator._llm_context_user_id(websocket)
    else:
        return None
    service = getattr(orchestrator, "harness_selection", None)
    if (type(service) is not HarnessSelectionService or type(owner) is not OperationOwner
            or owner.owner_scope not in {OwnerScope.USER, OwnerScope.CONNECTION}
            or fence is None or not owner_id):
        raise HarnessSelectionError("harness_owner_invalid", 403)
    captured = await asyncio.to_thread(orchestrator.work_admission.harness_capture,
        owner=owner, fence=fence, service=service, owner_id=owner_id)
    if persistent is not None and persistent.operation_fence is not None:
        if captured != persistent.harness_capture:
            raise HarnessSelectionError("harness_capture_changed")
    if captured is None:
        return None
    config, key = await select_captured_config(captured, store=service.config_store, owner_id=owner_id)
    binding = ModelHarnessBinding(service, orchestrator.work_admission, owner, fence,
        owner_id, captured, config, key, websocket, orchestrator.ui_sessions.get(websocket),
        capture_private_policy())
    await binding.assert_current()
    return binding
