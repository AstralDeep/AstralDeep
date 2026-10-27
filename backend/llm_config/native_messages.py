"""Builds, sends, and translates native Anthropic Messages API requests for the
opt-in prefix-cache path: OpenAI-shaped chat input is translated strictly, any
unsupported shape fails closed to the existing OpenAI-compatible dispatch, and
responses are translated back with native cache usage preserved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

import httpx

from .prompt_prefix_cache import (
    ANTHROPIC_VERSION_HEADER,
    NativeUsage,
    apply_cache_markers,
    parse_native_usage,
)

DEFAULT_NATIVE_MAX_TOKENS = 4096

_STOP_REASON_BY_NATIVE = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}

_FINISH_REASON_BY_NATIVE = dict(_STOP_REASON_BY_NATIVE)


class NativeMessagesError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NativeShapeError(NativeMessagesError):
    pass


def _disabled_decision():
    from .prompt_prefix_cache import PrefixCacheDecision

    return PrefixCacheDecision(False, "policy_disabled")


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _content_blocks(content: Any, *, where: str) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [_text_part(content)] if content else []
    if isinstance(content, (list, tuple)):
        blocks: list[dict[str, Any]] = []
        for part in content:
            value = part if isinstance(part, dict) else None
            kind = value.get("type") if value is not None else None
            if kind == "text" and type(value.get("text")) is str:
                blocks.append(_text_part(value["text"]))
            else:
                raise NativeShapeError(f"unsupported_content_part_{where}")
        return blocks
    if content is None:
        return []
    raise NativeShapeError(f"unsupported_content_{where}")


def _tool_use_input(arguments: Any) -> Any:
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if type(arguments) is str:
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            raise NativeShapeError("malformed_tool_arguments") from None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise NativeShapeError("malformed_tool_arguments")


def _tool_name_of(tool: Any) -> str:
    function = tool.get("function") if isinstance(tool, dict) else None
    if isinstance(function, dict) and type(function.get("name")) is str:
        return function["name"]
    if isinstance(tool, dict) and type(tool.get("name")) is str:
        return tool["name"]
    raise NativeShapeError("malformed_tool_definition")


def _tool_schema_of(tool: Any) -> dict[str, Any]:
    function = tool.get("function") if isinstance(tool, dict) else None
    schema = function.get("parameters") if isinstance(function, dict) else None
    if schema is None and isinstance(tool, dict):
        schema = tool.get("input_schema")
    if not isinstance(schema, dict):
        raise NativeShapeError("malformed_tool_schema")
    return schema


def _tool_description_of(tool: Any) -> str:
    function = tool.get("function") if isinstance(tool, dict) else None
    description = function.get("description") if isinstance(function, dict) else None
    if description is None and isinstance(tool, dict):
        description = tool.get("description")
    return description if type(description) is str else ""


def _native_tools(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, (list, tuple)):
        raise NativeShapeError("unsupported_tools")
    native: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise NativeShapeError("malformed_tool_definition")
        native.append({
            "name": _tool_name_of(tool),
            "description": _tool_description_of(tool),
            "input_schema": _tool_schema_of(tool),
        })
    return native


def build_native_payload(
    *,
    model: Any,
    messages: Any,
    tools: Any = None,
    temperature: Any = None,
    max_tokens: Any = None,
    tool_choice: Any = None,
    static_core: Optional[str] = None,
    instructions: bool = False,
) -> dict[str, Any]:
    if type(model) is not str or not model.strip():
        raise NativeShapeError("unsupported_model")
    if tool_choice not in (None, "auto"):
        raise NativeShapeError("unsupported_tool_choice")
    if not isinstance(messages, (list, tuple)) or not messages:
        raise NativeShapeError("unsupported_messages")

    system_parts: list[str] = []
    trailing_system: list[str] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        value = message if isinstance(message, dict) else None
        if value is None or value.get("role") != "system":
            break
        content = value.get("content")
        if type(content) is not str:
            raise NativeShapeError("unsupported_system_content")
        system_parts.append(content)
        index += 1
    if not system_parts:
        raise NativeShapeError("missing_system_prefix")

    native_messages: list[dict[str, Any]] = []
    for message in messages[index:]:
        if not isinstance(message, dict):
            raise NativeShapeError("malformed_message")
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if type(content) is not str:
                raise NativeShapeError("unsupported_system_content")
            trailing_system.append(content)
            continue
        if role == "user":
            user_blocks = _content_blocks(content, where="user")
            if not user_blocks:
                raise NativeShapeError("empty_user_message")
            native_messages.append({"role": "user", "content": user_blocks})
        elif role == "assistant":
            blocks = _content_blocks(content, where="assistant")
            tool_calls = message.get("tool_calls")
            if tool_calls:
                for call in tool_calls:
                    if isinstance(call, dict):
                        function = call.get("function")
                        call_id = call.get("id")
                        if isinstance(function, dict):
                            name = function.get("name")
                            arguments = function.get("arguments")
                        else:
                            name, arguments = None, None
                    else:
                        function = getattr(call, "function", None)
                        call_id = getattr(call, "id", None)
                        name = getattr(function, "name", None) if function is not None else None
                        arguments = getattr(function, "arguments", None) if function is not None else None
                    if type(name) is not str or not name:
                        raise NativeShapeError("malformed_tool_call")
                    blocks.append({
                        "type": "tool_use",
                        "id": call_id if type(call_id) is str and call_id else f"call_{len(blocks)}",
                        "name": name,
                        "input": _tool_use_input(arguments),
                    })
            if not blocks:
                raise NativeShapeError("empty_assistant_message")
            native_messages.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if type(call_id) is not str or not call_id:
                raise NativeShapeError("malformed_tool_result")
            native_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _content_blocks(content, where="tool"),
                }],
            })
        else:
            raise NativeShapeError("unsupported_message_role")

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens if type(max_tokens) is int and max_tokens > 0 else DEFAULT_NATIVE_MAX_TOKENS,
        "messages": native_messages,
    }
    if system_parts:
        joined = "\n\n".join(system_parts)
        if (
            instructions
            and type(static_core) is str
            and static_core
            and joined.startswith(static_core)
        ):
            remainder = joined[len(static_core):]
            system_blocks: list[dict[str, Any]] = [_text_part(static_core)]
            if remainder.strip():
                system_blocks.append(_text_part(remainder))
        else:
            system_blocks = [_text_part(joined)]
        system_blocks.extend(_text_part(part) for part in trailing_system)
        payload["system"] = system_blocks
    native_tools = _native_tools(tools)
    if native_tools:
        payload["tools"] = native_tools
    if temperature is not None and type(temperature) in (int, float):
        payload["temperature"] = float(temperature)
    return apply_cache_markers(payload, _disabled_decision())


def _tool_call_objects(tool_use_blocks: list[dict[str, Any]]) -> list[Any]:
    calls: list[Any] = []
    try:
        from openai.types.chat.chat_completion_message_tool_call import (
            ChatCompletionMessageToolCall,
            Function,
        )
        for block in tool_use_blocks:
            calls.append(ChatCompletionMessageToolCall(
                id=block["id"], type="function",
                function=Function(name=block["name"], arguments=json.dumps(block.get("input") or {})),
            ))
        return calls
    except Exception:
        for block in tool_use_blocks:
            calls.append(SimpleNamespace(
                id=block["id"], type="function",
                function=SimpleNamespace(name=block["name"], arguments=json.dumps(block.get("input") or {})),
            ))
        return calls


def translate_native_response(payload: Mapping[str, Any]) -> SimpleNamespace:
    if payload.get("type") == "error":
        error = payload.get("error")
        status = payload.get("status_code") if type(payload.get("status_code")) is int else None
        message = error.get("message") if isinstance(error, dict) and type(error.get("message")) is str else "native_error"
        raise NativeMessagesError(message, status_code=status)
    content_blocks = payload.get("content")
    if not isinstance(content_blocks, list):
        raise NativeMessagesError("native_response_malformed")
    texts: list[str] = []
    tool_blocks: list[dict[str, Any]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and type(block.get("text")) is str:
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            block_id = block.get("id")
            name = block.get("name")
            if type(block_id) is not str or type(name) is not str:
                raise NativeMessagesError("native_response_malformed")
            tool_blocks.append({
                "id": block_id,
                "name": name,
                "input": block.get("input") if isinstance(block.get("input"), Mapping) else {},
            })
    message = SimpleNamespace(
        role="assistant",
        content="".join(texts),
        tool_calls=_tool_call_objects(tool_blocks) if tool_blocks else None,
    )
    usage = parse_native_usage(payload.get("usage"))
    total = usage.total_tokens
    prompt = None
    completion = usage.output_tokens
    if usage.input_tokens is not None and usage.output_tokens is not None:
        prompt = usage.input_tokens + (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0)
    chat_usage = SimpleNamespace(
        total_tokens=total,
        prompt_tokens=prompt,
        completion_tokens=completion,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=message,
            finish_reason=_FINISH_REASON_BY_NATIVE.get(payload.get("stop_reason")),
        )],
        usage=chat_usage,
        model=payload.get("model"),
    )


def default_transport(url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: Any) -> Mapping[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=dict(headers), json=dict(payload))
    if response.status_code >= 400:
        raise NativeMessagesError(
            f"native_messages_status_{response.status_code}", status_code=response.status_code,
        )
    try:
        return response.json()
    except ValueError:
        raise NativeMessagesError("native_response_unparseable") from None


def _default_prefix_estimator(static_prefix: str, tools: Any) -> int:
    from orchestrator.context_token_accounting import account_request_tokens

    estimate = account_request_tokens(
        messages=[{"role": "system", "content": static_prefix}],
        tools=tools or None,
    )
    return estimate.message_tokens + estimate.tool_tokens


def _measurement(
    decision: Any,
    *,
    owner_id: str,
    base_url: Any,
    model: Any,
    outcome: str,
    usage: NativeUsage,
) -> Any:
    from .prompt_prefix_cache import (
        NATIVE_MESSAGES_PROFILE,
        CacheMeasurement,
        config_revision,
    )

    return CacheMeasurement(
        owner_id=owner_id,
        provider="anthropic",
        model=model if type(model) is str else "",
        profile=NATIVE_MESSAGES_PROFILE,
        policy_revision=decision.policy_revision,
        config_revision=config_revision(base_url, model),
        reason=decision.reason,
        outcome=outcome,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_write_tokens=usage.cache_creation_input_tokens,
        uncached_input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )


@dataclass(frozen=True, slots=True)
class NativeCacheCall:
    request: dict[str, Any]
    decision: Any
    _send: Callable[[], Any] = field(repr=False)

    def send(self) -> Any:
        return self._send()


def plan_native_cached_call(
    *,
    enabled: bool,
    policy: Any,
    base_url: Any,
    model: Any,
    api_key: Any,
    messages: Any,
    tools: Any,
    temperature: Any,
    static_prefix: Any,
    static_tool_names: Any = frozenset(),
    timeout: Any = None,
    owner_id: str = "",
    prefix_token_estimator: Optional[Callable[[str, Any], int]] = None,
) -> Optional[NativeCacheCall]:
    from .prompt_prefix_cache import (
        apply_cache_markers,
        parse_native_usage,
        record_measurement,
    )

    leading_system = _leading_system_content(messages)
    decision = policy.evaluate(
        enabled=enabled,
        base_url=base_url,
        model=model,
        static_prefix=static_prefix,
        system_content=leading_system,
        tool_defs=tools,
        static_tool_names=static_tool_names,
        prefix_token_estimator=(
            prefix_token_estimator
            if prefix_token_estimator is not None
            else _default_prefix_estimator
        ),
    )
    if not decision.enabled:
        return None
    if type(api_key) is not str or not api_key:
        return None
    try:
        payload = build_native_payload(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            static_core=decision.static_core,
            instructions=decision.instructions,
        )
    except NativeShapeError:
        return None
    payload = apply_cache_markers(payload, decision)
    key = api_key

    def _send() -> Any:
        try:
            raw = send_native_message(
                base_url=base_url, api_key=key, payload=payload, timeout=timeout,
            )
            response = translate_native_response(raw)
        except Exception:
            record_measurement(_measurement(
                decision, owner_id=owner_id, base_url=base_url, model=model,
                outcome="failed", usage=NativeUsage(),
            ))
            raise
        usage = parse_native_usage(getattr(response, "usage", None))
        if (usage.cache_read_input_tokens or 0) > 0:
            outcome = "hit"
        elif (usage.cache_creation_input_tokens or 0) > 0:
            outcome = "write"
        else:
            outcome = "miss"
        record_measurement(_measurement(
            decision, owner_id=owner_id, base_url=base_url, model=model,
            outcome=outcome, usage=usage,
        ))
        return response

    return NativeCacheCall(request=payload, decision=decision, _send=_send)


def _leading_system_content(messages: Any) -> Optional[str]:
    if not isinstance(messages, (list, tuple)) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict) or first.get("role") != "system":
        return None
    content = first.get("content")
    return content if type(content) is str else None


def send_native_message(
    *,
    base_url: str,
    api_key: str,
    payload: Mapping[str, Any],
    timeout: Any = None,
    transport: Any = None,
) -> Mapping[str, Any]:
    if not base_url or not api_key:
        raise NativeMessagesError("native_messages_unconfigured")
    url = base_url.rstrip("/") + "/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION_HEADER,
        "content-type": "application/json",
    }
    sender = transport if transport is not None else default_transport
    return sender(url, headers, payload, timeout)


__all__ = [
    "DEFAULT_NATIVE_MAX_TOKENS",
    "NativeCacheCall",
    "NativeMessagesError",
    "NativeShapeError",
    "build_native_payload",
    "default_transport",
    "plan_native_cached_call",
    "send_native_message",
    "translate_native_response",
]
