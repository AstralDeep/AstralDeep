# The transcript format: OpenAI chat messages in a small versioned envelope, sent in as history and
# streamed back out. Applications own conversation history; the vessel checks what it is given and
# never accepts a system message, and whatever the vessel streams must parse back as history.

from __future__ import annotations

import base64
import json
import math
import re
from typing import Any

MEDIA_TYPE = "application/vnd.astralvessel.transcript+json"
VERSION = 1
MAX_MESSAGES = 2000
ROLE_KEYS = {
    "user": {"role", "content"},
    "assistant": {"role", "content", "tool_calls", "reasoning_content"},
    "tool": {"role", "tool_call_id", "content"},
}


class TranscriptError(ValueError):
    pass


def envelope(messages: list[dict[str, Any]], turn: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"version": VERSION, "messages": messages}
    if turn is not None:
        body["turn"] = turn
    return body


def parse_history(payload: Any) -> list[dict[str, Any]]:
    try:
        return _parse(payload)
    except TranscriptError:
        raise
    except Exception:  # noqa: BLE001  any shape problem is still a refusal, never a crash
        raise TranscriptError("the history is malformed") from None


def _parse(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        raise TranscriptError(f"history must be a transcript envelope with version {VERSION}")
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise TranscriptError(f"history must hold a list of at most {MAX_MESSAGES} messages")
    pending: set[str] = set()
    checked = []
    for index, message in enumerate(messages):
        where = f"message {index}"
        if not isinstance(message, dict) or message.get("role") not in ROLE_KEYS:
            raise TranscriptError(f"{where} needs a role of user, assistant or tool")
        role = message["role"]
        if set(message) - ROLE_KEYS[role]:
            raise TranscriptError(f"{where} has fields a {role} message may not carry")
        if role != "tool" and pending:
            raise TranscriptError(f"{where} comes before every tool call was answered")
        if role == "tool":
            call = message.get("tool_call_id")
            if not isinstance(call, str) or call not in pending or not isinstance(message.get("content"), str):
                raise TranscriptError(f"{where} answers no open tool call")
            pending.discard(call)
        elif role == "user":
            _check_user_content(message.get("content"), where)
        else:
            _check_assistant(message, where, pending)
        checked.append(dict(message))
    if pending:
        raise TranscriptError("the history ends with unanswered tool calls")
    images = sum(1 for m in checked if isinstance(m.get("content"), list)
                 for p in m["content"] if p.get("type") == "image_url")
    if images > MAX_HISTORY_IMAGES:
        raise TranscriptError(f"the history may carry at most {MAX_HISTORY_IMAGES} images")
    return checked


IMAGE_MAGIC = {"image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8\xff",),
               "image/gif": (b"GIF87a", b"GIF89a"), "image/webp": (b"RIFF",)}
MAX_IMAGE_BYTES = 5_000_000
MAX_HISTORY_IMAGES = 32
DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/]+={0,2})$")


def has_images(messages: list[dict[str, Any]]) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in messages)


def _check_image(url: Any) -> bool:
    match = DATA_URL.match(url) if isinstance(url, str) and len(url) < MAX_IMAGE_BYTES * 2 else None
    if not match:
        return False
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except ValueError:
        return False
    kind = match.group(1)
    return len(raw) <= MAX_IMAGE_BYTES and raw.startswith(IMAGE_MAGIC[kind]) and (
        kind != "image/webp" or raw[8:12] == b"WEBP")


def _check_user_content(content: Any, where: str) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list) or not content:
        raise TranscriptError(f"{where} needs text content or a list of content parts")
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text" and set(part) == {"type", "text"} and isinstance(part["text"], str):
            continue
        image = part.get("image_url") if kind == "image_url" and set(part) == {"type", "image_url"} else None
        ok = isinstance(image, dict) and set(image) <= {"url", "detail"} and \
            image.get("detail", "auto") in ("auto", "low", "high") and _check_image(image.get("url"))
        if not ok:
            raise TranscriptError(f"{where} has a content part that is not text or a valid inline image")


def _check_assistant(message: dict[str, Any], where: str, pending: set[str]) -> None:
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise TranscriptError(f"{where} content must be text or null")
    if "reasoning_content" in message and not isinstance(message["reasoning_content"], str):
        raise TranscriptError(f"{where} reasoning_content must be text")
    calls = message.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        raise TranscriptError(f"{where} tool_calls must be a list")
    for call in calls or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(fn, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str)
                or not isinstance(fn.get("name"), str) or not isinstance(fn.get("arguments"), str)
                or set(call) - {"id", "type", "function"} or set(fn) - {"name", "arguments"}):
            raise TranscriptError(f"{where} has a malformed tool call")
        call_id = call["id"]
        if not call_id.strip() or call_id in pending:
            raise TranscriptError(f"{where} needs nonempty and unique tool call IDs")
        pending.add(call_id)


def parse_arguments(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}", parse_constant=_reject_constant, parse_float=_finite)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def replayable(raw: str) -> str:
    # Model endpoints refuse a request whose earlier tool call holds arguments that are not a JSON
    # object; the call's error result stays, so the model can still see and retry it.
    return raw if raw.strip() and parse_arguments(raw) is not None else "{}"


def for_model(messages: list[dict[str, Any]], send_reasoning: bool) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if not send_reasoning:
            m = {k: v for k, v in m.items() if k != "reasoning_content"}
        if m.get("tool_calls"):
            m = {**m, "tool_calls": [{**c, "function": {**c["function"], "arguments": replayable(
                c["function"]["arguments"])}} for c in m["tool_calls"]]}
        out.append(m)
    return out


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def _finite(text: str) -> float:
    value = float(text)
    if math.isinf(value):
        raise ValueError("non-finite number")
    return value
