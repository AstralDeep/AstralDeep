"""Validates the bounded text and client-tool native Messages wire profile. The factory adapter uses its request, response, and incremental event normalization without executing tools."""

from __future__ import annotations

import json
import math
import re
from types import SimpleNamespace

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from openai.types.chat import ChatCompletionMessage
from orchestrator.context_token_accounting import account_request_tokens

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 65536
MAX_EVENTS = 4096
MAX_BLOCKS = 64
MAX_ARGUMENT_BYTES = 65536
MAX_OUTPUT_TOKENS = 8192
MAX_CONTEXT_TOKENS = 32768
_NAME = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_STOPS = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length", "refusal": "content_filter"}
_USAGE = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class NativeContractError(ValueError):
    def __init__(self, code="native_contract_invalid"):
        super().__init__(code)
        self.code = code


def _fail(code="native_contract_invalid"):
    raise NativeContractError(code)


def _valid_json(value, depth=0):
    if depth > 32:
        _fail()
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or key in {"cache_control", "$ref", "$dynamicRef"}:
                _fail("native_capability_unsupported")
            _valid_json(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _valid_json(item, depth + 1)
    elif value is not None and type(value) not in (str, int, bool, float):
        _fail()
    elif type(value) is float and not math.isfinite(value):
        _fail()


def encode(value, maximum=MAX_REQUEST_BYTES):
    _valid_json(value)
    try:
        result = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _fail()
    if len(result) > maximum:
        _fail("native_size_limit")
    return result


def parse(raw, maximum=MAX_RESPONSE_BYTES):
    if type(raw) not in (str, bytes) or len(raw) > maximum:
        _fail("native_size_limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        _valid_json(value)
        return value
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _fail()


def _mapping(value):
    if type(value) is dict:
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    _fail()


def _text(value):
    if type(value) is str:
        return [{"type": "text", "text": value}] if value else []
    if type(value) is list and len(value) <= MAX_BLOCKS:
        if all(type(part) is dict and set(part) == {"type", "text"}
               and part["type"] == "text" and type(part["text"]) is str for part in value):
            return value
    if value is None:
        return []
    _fail("native_content_unsupported")


def _arguments(value):
    if type(value) is str:
        value = parse(value, MAX_ARGUMENT_BYTES)
    if type(value) is not dict:
        _fail()
    encode(value, MAX_ARGUMENT_BYTES)
    return value


def _valid_name(value):
    return type(value) is str and _NAME.fullmatch(value) is not None


def _safe_schema(schema):
    if type(schema) is not dict:
        return
    if set(schema) & {"pattern", "patternProperties", "format"}:
        _fail("native_schema_unsupported")
    for key, value in schema.items():
        if key in {"properties", "dependentSchemas", "$defs"} and type(value) is dict:
            for child in value.values():
                _safe_schema(child)
        elif key in {"items", "additionalProperties", "propertyNames", "not", "if", "then", "else", "contains"}:
            _safe_schema(value)
        elif key in {"anyOf", "oneOf", "allOf", "prefixItems"} and type(value) is list:
            for child in value:
                _safe_schema(child)


def _tools(values):
    if values is None:
        return []
    if type(values) is not list or len(values) > MAX_BLOCKS:
        _fail()
    tools, names = [], set()
    for raw in values:
        if type(raw) is not dict or set(raw) != {"type", "function"} or raw["type"] != "function":
            _fail("native_tool_unsupported")
        tool = raw["function"]
        if (type(tool) is not dict or set(tool) - {"name", "description", "parameters", "strict"}
                or not _valid_name(tool.get("name")) or tool["name"] in names
                or type(tool.get("parameters")) is not dict
                or tool["parameters"].get("type") != "object"):
            _fail()
        if "strict" in tool and type(tool["strict"]) is not bool:
            _fail()
        if type(tool.get("description", "")) is not str:
            _fail()
        encode(tool["parameters"], MAX_ARGUMENT_BYTES)
        _safe_schema(tool["parameters"])
        try:
            Draft202012Validator.check_schema(tool["parameters"])
        except SchemaError:
            _fail()
        names.add(tool["name"])
        item = {"name": tool["name"], "description": tool.get("description", ""), "input_schema": tool["parameters"]}
        if "strict" in tool:
            item["strict"] = tool["strict"]
        tools.append(item)
    return tools


def build_request(*, bound_model, **kwargs):
    if set(kwargs) - {"model", "messages", "tools", "tool_choice", "temperature", "max_tokens", "max_completion_tokens", "stream"}:
        _fail("native_capability_unsupported")
    if kwargs.get("model") != bound_model or type(bound_model) is not str or not 1 <= len(bound_model) <= 256:
        _fail("native_model_binding_changed")
    output = kwargs.get("max_completion_tokens", kwargs.get("max_tokens", 4096))
    if (type(output) is not int or not 1 <= output <= MAX_OUTPUT_TOKENS
            or ("max_tokens" in kwargs and "max_completion_tokens" in kwargs)):
        _fail("native_output_limit")
    stream = kwargs.get("stream", False)
    if type(stream) is not bool:
        _fail()
    messages = kwargs.get("messages")
    if type(messages) is not list or not 1 <= len(messages) <= 256:
        _fail()
    tools = _tools(kwargs.get("tools"))
    system, native, pending = [], [], set()
    for raw in messages:
        message = _mapping(raw)
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name"}:
            _fail("native_message_unsupported")
        role = message.get("role")
        if role in ("system", "developer"):
            if native:
                _fail("native_late_system_unsupported")
            system.extend(_text(message.get("content")))
            continue
        if pending and role != "tool":
            _fail("native_tool_results_incomplete")
        if role in ("user", "assistant"):
            blocks = list(_text(message.get("content")))
            calls = message.get("tool_calls")
            if calls:
                if role != "assistant" or type(calls) is not list or len(calls) > MAX_BLOCKS:
                    _fail()
                for call in calls:
                    call = _mapping(call)
                    if set(call) - {"id", "type", "function"} or call.get("type", "function") != "function":
                        _fail("native_continuation_unsupported")
                    function = call.get("function")
                    call_id = call.get("id")
                    if (type(function) is not dict or not _valid_name(function.get("name"))
                            or type(call_id) is not str or not 1 <= len(call_id) <= 256 or call_id in pending):
                        _fail()
                    blocks.append({"type": "tool_use", "id": call_id, "name": function["name"], "input": _arguments(function.get("arguments"))})
                    pending.add(call_id)
            if not blocks:
                _fail()
            native.append({"role": role, "content": blocks})
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if type(call_id) is not str or call_id not in pending:
                _fail("native_tool_result_unbound")
            pending.remove(call_id)
            block = {"type": "tool_result", "tool_use_id": call_id, "content": _text(message.get("content"))}
            if native[-1]["role"] == "user":
                native[-1]["content"].append(block)
            else:
                native.append({"role": "user", "content": [block]})
        else:
            _fail("native_role_unsupported")
    if pending or not native or native[0]["role"] != "user":
        _fail("native_tool_results_incomplete")
    payload = {"model": bound_model, "max_tokens": output, "stream": stream, "messages": native}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    choice = kwargs.get("tool_choice")
    if choice in (None, "auto"):
        pass
    elif choice in ("none", "required"):
        payload["tool_choice"] = {"type": "none" if choice == "none" else "any"}
    elif type(choice) is dict and choice.get("type") == "function":
        function = choice.get("function")
        if type(function) is not dict:
            _fail()
        name = function.get("name")
        if name not in {tool["name"] for tool in tools}:
            _fail()
        payload["tool_choice"] = {"type": "tool", "name": name}
    else:
        _fail("native_tool_choice_unsupported")
    if "temperature" in kwargs:
        temperature = kwargs["temperature"]
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 1:
            _fail()
        payload["temperature"] = temperature
    encode(payload)
    estimate = account_request_tokens(messages, kwargs.get("tools"), provider="anthropic", model=bound_model,
                                      context_window=MAX_CONTEXT_TOKENS, reserved_output_tokens=output)
    if estimate.total_tokens > MAX_CONTEXT_TOKENS:
        _fail("native_context_limit")
    return payload


def _usage(raw, maximum):
    if type(raw) is not dict:
        _fail("native_usage_unavailable")
    values = {key: raw.get(key) for key in _USAGE}
    for key, value in values.items():
        if value is not None and (type(value) is not int or not 0 <= value <= 2**53 - 1):
            _fail("native_usage_invalid")
        if key in _USAGE[:2] and value is None:
            _fail("native_usage_unavailable")
    if values["output_tokens"] > maximum:
        _fail("native_output_limit")
    prompt = None if any(values[key] is None for key in _USAGE[2:]) else sum(values[key] for key in (_USAGE[0], *_USAGE[2:]))
    if prompt is not None and prompt + values["output_tokens"] > 2**53 - 1:
        _fail("native_usage_invalid")
    uncached = values["input_tokens"]
    values["input_tokens"] = prompt
    return SimpleNamespace(**values, uncached_input_tokens=uncached, prompt_tokens=prompt, completion_tokens=values["output_tokens"],
        total_tokens=None if prompt is None else prompt + values["output_tokens"],
        prompt_tokens_details=SimpleNamespace(cached_tokens=values["cache_read_input_tokens"]))


def decode_response(value, request):
    if type(value) in (str, bytes):
        value = parse(value)
    if (type(value) is not dict or value.get("type") != "message" or value.get("role") != "assistant"
            or type(value.get("id")) is not str or not value["id"] or value.get("model") != request["model"]
            or value.get("stop_reason") not in _STOPS or type(value.get("content")) is not list
            or not 1 <= len(value["content"]) <= MAX_BLOCKS):
        _fail("native_response_invalid")
    usage = _usage(value.get("usage"), request["max_tokens"])
    try:
        return _decode_response_body(value, request, usage)
    except NativeContractError as error:
        error.usage = usage
        raise


def _decode_response_body(value, request, usage):
    texts, calls, seen = [], [], set()
    schemas = {tool["name"]: tool["input_schema"] for tool in request.get("tools", [])}
    for block in value["content"]:
        if type(block) is not dict:
            _fail()
        if block.get("type") == "text" and type(block.get("text")) is str:
            if set(block) - {"type", "text", "citations"} or block.get("citations"):
                _fail("native_content_unsupported")
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            call_id, name = block.get("id"), block.get("name")
            if (set(block) != {"type", "id", "name", "input"} or type(call_id) is not str
                    or not 1 <= len(call_id) <= 256 or call_id in seen or type(name) is not str or name not in schemas):
                _fail("native_tool_invalid")
            arguments = _arguments(block.get("input"))
            try:
                Draft202012Validator(schemas[name]).validate(arguments)
            except ValidationError:
                _fail("native_tool_invalid")
            calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": encode(arguments, MAX_ARGUMENT_BYTES).decode()}})
            seen.add(call_id)
        else:
            _fail("native_content_unsupported")
    if bool(calls) != (value["stop_reason"] == "tool_use"):
        _fail("native_tool_stop_invalid")
    if value["stop_reason"] == "max_tokens":
        error = NativeContractError("native_response_truncated")
        error.usage = usage
        raise error
    encode(value, MAX_RESPONSE_BYTES)
    message = ChatCompletionMessage(role="assistant", content="".join(texts) or None, tool_calls=calls or None,
        refusal=("".join(texts) or "The provider declined the request.") if value["stop_reason"] == "refusal" else None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=_STOPS[value["stop_reason"]])], usage=usage, model=value["model"])


def _chunk(*, text=None, calls=None, finish=None, usage=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=calls), finish_reason=finish)], usage=usage)


async def decode_events(source, request):
    buffer, count, total = b"", 0, 0
    message, active, fragments, stopped, final = None, None, "", False, None
    async for raw in source:
        if type(raw) is not bytes or stopped:
            _fail("native_stream_invalid")
        total += len(raw)
        if total > MAX_RESPONSE_BYTES:
            _fail("native_size_limit")
        buffer = (buffer + raw).replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            event, buffer = buffer.split(b"\n\n", 1)
            count += 1
            if stopped:
                _fail("native_stream_invalid")
            if count > MAX_EVENTS or len(event) > MAX_EVENT_BYTES:
                _fail("native_event_limit")
            try:
                lines = event.decode("utf-8").splitlines()
            except UnicodeError:
                _fail()
            data = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
            if not data and all(line.startswith(":") for line in lines):
                continue
            item = parse(data, MAX_EVENT_BYTES)
            if type(item) is not dict:
                _fail()
            kind = item.get("type")
            if kind == "ping":
                continue
            if kind == "message_start":
                if message is not None:
                    _fail()
                message = item.get("message")
                if (type(message) is not dict or message.get("content") != []
                        or type(message.get("usage")) is not dict):
                    _fail()
                if (message.get("type") != "message" or message.get("role") != "assistant"
                        or message.get("model") != request["model"] or type(message.get("id")) is not str
                        or not message["id"] or message.get("stop_reason") is not None):
                    _fail("native_response_invalid")
                _usage(message["usage"], request["max_tokens"])
                message = {**message, "content": [], "usage": dict(message["usage"])}
            elif kind == "content_block_start":
                if message is None or active is not None or item.get("index") != len(message["content"]) or len(message["content"]) >= MAX_BLOCKS:
                    _fail()
                active = item.get("content_block")
                if type(active) is not dict or active.get("type") not in {"text", "tool_use"}:
                    _fail("native_content_unsupported")
                active = dict(active)
                fragments = ""
            elif kind == "content_block_delta":
                delta = item.get("delta")
                if active is None or item.get("index") != len(message["content"]) or type(delta) is not dict:
                    _fail()
                if active["type"] == "text" and delta.get("type") == "text_delta" and type(delta.get("text")) is str:
                    active["text"] = active.get("text", "") + delta["text"]
                elif active["type"] == "tool_use" and delta.get("type") == "input_json_delta" and type(delta.get("partial_json")) is str:
                    fragments += delta["partial_json"]
                    if len(fragments.encode()) > MAX_ARGUMENT_BYTES:
                        _fail("native_size_limit")
                else:
                    _fail()
            elif kind == "content_block_stop":
                if active is None or item.get("index") != len(message["content"]):
                    _fail()
                if active["type"] == "tool_use" and fragments:
                    if active.get("input") != {}:
                        _fail()
                    active["input"] = _arguments(fragments)
                message["content"].append(active)
                if active["type"] == "text":
                    if type(active.get("text")) is not str:
                        _fail()
                    yield _chunk(text=active["text"])
                active = None
            elif kind == "message_delta":
                if message is None or active is not None or type(item.get("delta")) is not dict or type(item.get("usage")) is not dict:
                    _fail()
                if set(item["delta"]) - {"stop_reason", "stop_sequence"}:
                    _fail()
                message.update(item["delta"])
                for key, value in item["usage"].items():
                    if key in _USAGE:
                        previous = message["usage"].get(key)
                        if type(value) is not int or (previous is not None and value < previous):
                            _fail("native_usage_invalid")
                        message["usage"][key] = value
            elif kind == "message_stop":
                if message is None or active is not None or stopped:
                    _fail()
                result = decode_response(message, request)
                calls = [SimpleNamespace(index=index, id=call.id, type="function", function=call.function)
                         for index, call in enumerate(result.choices[0].message.tool_calls or [])]
                stopped = True
                final = _chunk(calls=calls or None, finish=result.choices[0].finish_reason, usage=result.usage)
            else:
                _fail("native_event_unsupported")
        if len(buffer) > MAX_EVENT_BYTES:
            _fail("native_event_limit")
    if not stopped or buffer.strip():
        _fail("native_stream_incomplete")
    yield final
