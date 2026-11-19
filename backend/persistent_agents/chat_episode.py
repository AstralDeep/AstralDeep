"""Validates the existing Work chat profile, action identities and retained answers."""

from __future__ import annotations

from dataclasses import dataclass

from llm_config import research_profile as profile
from llm_config.research_profile import ResearchRequest, ResearchProfileUnavailable, ResearchUsage
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.research_input import chat_definition
from persistent_agents.runtime_values import digest

CHAT_PROFILE = "openai-chat-turn/v1"
LOCAL_CHAT_PROFILE = "local-chat-turn/v1"
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


def chat_profile_name(profile_spec=profile.OPENAI_PROFILE) -> str:
    profile._declared_profile(profile_spec)
    if not profile_spec.bound:
        profile._refuse()
    return LOCAL_CHAT_PROFILE if profile_spec.local else CHAT_PROFILE


def build_chat_request(
        instruction: str, *, profile_spec=profile.OPENAI_PROFILE) -> ResearchRequest:
    chat_profile_name(profile_spec)
    profile._text(instruction, 32768)
    try:
        body = profile._canonical({
            "model": profile_spec.model, "stream": False, "store": False, "n": 1,
            "max_completion_tokens": profile_spec.output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": instruction}],
        })
        if len(body) > profile.MAX_REQUEST_BYTES:
            profile._refuse()
        return ResearchRequest(body, ())
    except (ValueError, TypeError, UnicodeError, OverflowError):
        profile._refuse()


def chat_result_value(text, *, profile_spec=profile.OPENAI_PROFILE) -> dict:
    profile_name = chat_profile_name(profile_spec)
    if type(text) is not str or not text.strip() or "\x00" in text:
        raise ValueError("chat answer unavailable")
    if len(text.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise ValueError("chat answer unavailable")
    return {"version": 1, "profile": profile_name, "text": text}


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


def parse_chat_response(
        body: bytes, *, status_code: int,
        profile_spec=profile.OPENAI_PROFILE) -> ChatResponse:
    chat_profile_name(profile_spec)
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
    usage = profile._usage(value["usage"], profile_spec)
    try:
        if value["model"] != profile_spec.model:
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
