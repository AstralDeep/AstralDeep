"""One fixed, source-less USER chat turn per owner instruction: no tool, source or
guidance. Answer is retained under research's keyed receipt binding; invoked by
persistent_agents/runner.py, consumed via orchestrator/work_chat_result.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from astralplane.repositories.assignment_models import AssignmentEpisodeCompletion
from llm_config import research_profile as profile
from llm_config.research_profile import ResearchRequest, ResearchProfileUnavailable, ResearchUsage
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import _OperationAuthorityWindow, safe_text
from persistent_agents.research_input import ResearchInput, chat_definition, route
from persistent_agents.runtime_values import canonical, digest, thaw

CHAT_PROFILE = "openai-chat-turn/v1"
CHAT_KEY_PREFIX = "chat-v1-"
# 8 KiB Plane result bound minus envelope overhead
MAX_ANSWER_BYTES = 6144
_SYSTEM = (
    "Answer the owner's request directly from your own knowledge. No source "
    "text, tool result or document is supplied and none may be claimed. "
    'Return only a JSON object with exact fields {"version":1,"text":"..."}. '
    "The text is the complete answer; do not add citations, URLs or fields."
)


def chat_action_key(record) -> str:
    chat_definition(record)
    for value in (record.instruction_revision, record.control_epoch):
        if type(value) is not int or not 1 <= value <= 2**53 - 1:
            raise DispatchDenied("assignment_operation_profile_unavailable")
    return digest(["chat-turn-v1", record.instruction_revision, record.control_epoch])


def build_chat_request(instruction: str) -> ResearchRequest:
    profile._text(instruction, 32768)
    try:
        body = profile._canonical({
            "model": profile.MODEL, "stream": False, "store": False, "n": 1,
            "max_completion_tokens": profile.OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": instruction}],
        })
        if len(body) > profile.MAX_REQUEST_BYTES:
            profile._refuse()
        return ResearchRequest(body, ())
    except (ValueError, TypeError, UnicodeError, OverflowError):
        profile._refuse()


def chat_result_value(text) -> dict:
    if type(text) is not str or not text.strip() or "\x00" in text:
        raise ValueError("chat answer unavailable")
    if len(text.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise ValueError("chat answer unavailable")
    return {"version": 1, "profile": CHAT_PROFILE, "text": text}


@dataclass(frozen=True, slots=True)
class ChatResponse:
    usage: ResearchUsage | None
    text: str | None
    disposition: str

    @property
    def accepted(self) -> bool:
        return self.text is not None


def _answer(content) -> str:
    profile._text(content, 8192)
    value = profile._strict_json(content.encode(), 8192)
    if (type(value) is not dict or set(value) != {"version", "text"}
            or type(value["version"]) is not int or value["version"] != 1):
        profile._refuse()
    try:
        return chat_result_value(value["text"])["text"]
    except ValueError:
        profile._refuse()


def parse_chat_response(body: bytes, *, status_code: int) -> ChatResponse:
    try:
        if type(status_code) is not int or status_code != 200:
            profile._refuse()
        value = profile._strict_json(body, profile.MAX_RESPONSE_BYTES)
        required = {"id", "object", "created", "model", "choices", "usage"}
        if not required <= value.keys() or value.keys() - required - {"system_fingerprint", "service_tier"}:
            profile._refuse()
        profile._text(value["id"], 256)
        profile._text(value["model"], 256)
        if (value["object"] != "chat.completion" or type(value["created"]) is not int
                or value["created"] < 0):
            profile._refuse()
        for key in ("system_fingerprint", "service_tier"):
            if value.get(key) is not None:
                profile._text(value[key], 256)
    except ResearchProfileUnavailable:
        return ChatResponse(None, None, "response_invalid")
    usage = profile._usage(value["usage"])
    try:
        if value["model"] != profile.MODEL:
            profile._refuse()
        choices = value["choices"]
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            profile._refuse()
        choice = choices[0]
        if (set(choice) - {"index", "message", "finish_reason", "logprobs"}
                or type(choice.get("index")) is not int or choice["index"] != 0
                or choice.get("finish_reason") != "stop" or choice.get("logprobs") is not None):
            profile._refuse()
        message = choice.get("message")
        if (type(message) is not dict
                or set(message) - {"role", "content", "refusal", "annotations"}
                or message.get("role") != "assistant" or message.get("refusal") is not None
                or message.get("annotations", []) != []):
            profile._refuse()
        text = _answer(message.get("content"))
        if usage is None:
            return ChatResponse(None, None, "usage_unknown")
        if usage.exceeds_profile:
            return ChatResponse(usage, None, "profile_exceeded")
        return ChatResponse(usage, text, "answer")
    except ResearchProfileUnavailable:
        return ChatResponse(usage, None, "answer_invalid")


def chat_execution_checks(service, owner_id, claims, record, *, authority) -> dict:
    from orchestrator.session_authority import OperationExecutionAuthority
    from persistent_agents.models import AssignmentError

    service._owner(owner_id, claims, human=False)
    if record.owner_id != owner_id:
        raise AssignmentError("assignment_not_found", 404)
    chat_definition(record)
    if (not isinstance(authority, OperationExecutionAuthority)
            or authority.plane_runtime is not service.store.plane_runtime
            or authority.claims != claims
            or authority.record.owner_id != owner_id
            or authority.record.assignment_id != record.assignment_id
            or authority.record.definition != record.definition
            or authority.record.instruction_revision != record.instruction_revision
            or authority.record.control_epoch != record.control_epoch
            or authority.record.operation != record.operation
            or record.operation.get("version") != 2):
        raise AssignmentError("assignment_authorization_required", 403)
    selected = record.operation.get("authority", {})
    credential = authority.observation.credential
    if (selected.get("origin") != "interactive"
            or selected.get("reference_kind") != "session_incarnation"
            or selected.get("owner_id") != owner_id
            or selected.get("reference_id") != credential.incarnation_id
            or credential.owner_id != owner_id or record.definition.offline_grant_id is not None):
        raise AssignmentError("assignment_authorization_required", 403)
    permission = digest({"tools": {}, "instruction_revision": record.instruction_revision,
                         "control_epoch": record.control_epoch,
                         "operation_authority": thaw(record.operation["authority"])})
    return {"permission_digest": permission, "precondition_digest": digest({"source": {}})}


def _matching_checks(action, checks):
    if (action.intent.permission_digest != checks["permission_digest"]
            or action.intent.precondition_digest != checks["precondition_digest"]):
        raise DispatchDenied("assignment_precondition_changed")


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    private: ResearchInput = field(repr=False)
    model_action_id: str

    def rebuild(self, executor, tx, repository, current, checks):
        if self.private.kind != "chat":
            raise DispatchDenied("assignment_research_binding_changed")
        repository.assert_current_assignment_execution(tx, fence=executor.claim.fence,
            binding=executor.binding, authority=checks["model"]["authority"].observation,
            action_id=self.model_action_id)
        model = repository.get_action(tx, owner_id=current.owner_id,
            assignment_id=current.assignment_id, action_id=self.model_action_id)
        _matching_checks(model, checks["model"])
        answer = self.private.chat_result(model)
        chat_definition(current)
        result = {"version": 1, "kind": "chat", "text": answer["text"],
                  "model_action_id": model.action_id}
        checkpoint = {**thaw(current.checkpoint), "chat_result": result}
        return AssignmentEpisodeCompletion(expected_state_version=current.state_version,
            checkpoint=checkpoint, completion_digest=digest(["chat-result-v1", result]),
            phase="waiting", wake_reason="chat_completed", completed=True,
            terminal_outcome="completed", result_reference=model.action_id)

    async def refresh(self, executor):
        self.private.assert_record(executor.record)
        model = await executor.refresh(route(), _research=self.private)
        return {"model": model}

    def assert_completion(self, executor, tx, repository, current, checks, completion):
        rebuilt = self.rebuild(executor, tx, repository, current, checks)
        expected = thaw(rebuilt)
        expected["expected_state_version"] = completion.expected_state_version
        if canonical(expected) != canonical(thaw(completion)):
            raise DispatchDenied("assignment_research_result_invalid")


async def run_chat_episode(executor):
    from persistent_agents.runner import OneShotEpisodeResult

    chat_definition(executor.record)
    key = chat_action_key(executor.record)
    await executor.chat_turn(key)
    async with _OperationAuthorityWindow(executor.operation_authority_lock):
        record = executor.record
        model = await executor.store.transaction(
            lambda tx, repository: repository.get_action_by_key(
                tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
                action_key=CHAT_KEY_PREFIX + key),
            bound_session_waits=True)
        if model is None or model.intent.transient_input is None:
            raise DispatchDenied("assignment_research_binding_changed")
        private = await ResearchInput.capture_chat(record, config_store=executor.orch._llm_store,
            key_id=model.intent.transient_input.binding_key_id,
            guidance=executor._research_guidance)
        proof = ChatCompletion(private, model.action_id)
        refreshed = await proof.refresh(executor)

        def prepare(tx, repository, guarded):
            return guarded, proof.rebuild(executor, tx, repository, guarded, refreshed)

        current, completion = await executor._research_transaction(
            private, refreshed["model"]["authority"], prepare, action_id=model.action_id)
        await safe_text(completion.checkpoint["chat_result"]["text"])
        return OneShotEpisodeResult(current, completion, research=proof)


__all__ = [
    "CHAT_KEY_PREFIX", "CHAT_PROFILE", "ChatCompletion", "ChatResponse", "build_chat_request",
    "chat_action_key", "chat_execution_checks", "chat_result_value", "parse_chat_response",
    "run_chat_episode",
]
