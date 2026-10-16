"""Validates and preserves bounded Gemini tool-continuation metadata for the
OpenAI-compatible endpoint across stream accumulation, message reconstruction,
and the next request; the orchestrator binds it to the resolved Gemini profile.
"""

from __future__ import annotations

from typing import Any, Optional

GEMINI_OPENAI_COMPAT_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
)

MAX_THOUGHT_SIGNATURE_BYTES = 65536


class ContinuationStateError(RuntimeError):
    pass


def is_gemini_continuation_profile(base_url: Any) -> bool:
    if type(base_url) is not str:
        return False
    return base_url.strip().rstrip("/") == GEMINI_OPENAI_COMPAT_BASE_URL


def _validated_signature(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContinuationStateError("malformed_thought_signature")
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > MAX_THOUGHT_SIGNATURE_BYTES:
        raise ContinuationStateError("unbounded_thought_signature")
    return value


def continuation_signature(extra_content: Any) -> Optional[str]:
    if extra_content is None:
        return None
    if not isinstance(extra_content, dict):
        raise ContinuationStateError("malformed_extra_content")
    google = extra_content.get("google")
    if google is None:
        return None
    if not isinstance(google, dict):
        raise ContinuationStateError("malformed_extra_content")
    return _validated_signature(google.get("thought_signature"))


def merge_continuation_signature(
    existing: Optional[str], extra_content: Any
) -> Optional[str]:
    incoming = continuation_signature(extra_content)
    if incoming is None:
        return existing
    if existing is not None and existing != incoming:
        raise ContinuationStateError("contradictory_thought_signature")
    return incoming


def _tool_call_extra_content(tool_call: Any) -> Any:
    if isinstance(tool_call, dict):
        return tool_call.get("extra_content")
    return getattr(tool_call, "extra_content", None)


def _message_tool_calls(message: Any) -> Any:
    if isinstance(message, dict):
        if message.get("role") != "assistant":
            return None
        return message.get("tool_calls")
    if getattr(message, "role", None) != "assistant":
        return None
    return getattr(message, "tool_calls", None)


def validate_continuation_wire(messages: Any) -> None:
    if not isinstance(messages, (list, tuple)):
        return
    for message in messages:
        tool_calls = _message_tool_calls(message)
        if not tool_calls:
            continue
        for tool_call in tool_calls:
            continuation_signature(_tool_call_extra_content(tool_call))
