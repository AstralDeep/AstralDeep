from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterable

from backend.common.outbound import post_json, stream_json_events
from backend.llm.providers.base import BaseLLMClient, LLMResponse, LLMUsage


ALLOWED_HEADERS = {"x-api-key", "anthropic-version", "content-type"}


class AnthropicNativeClient(BaseLLMClient):
    def __init__(self, owner_id: str, endpoint: str, api_key: str, timeout_seconds: int = 30):
        self.owner_id = owner_id
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.anthropic_version = "2023-06-01"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
        }

    def _normalize_usage(self, usage: dict[str, Any]) -> LLMUsage:
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cache_creation = int(usage.get("cache_creation_input_tokens", 0))
        cache_read = int(usage.get("cache_read_input_tokens", 0))

        uncached_input = max(input_tokens - cache_read, 0)
        total_input = uncached_input + cache_creation + cache_read

        return LLMUsage(
            input_tokens=uncached_input,
            output_tokens=output_tokens,
            cache_write_tokens=cache_creation,
            cache_read_tokens=cache_read,
            total_tokens=total_input + output_tokens,
        )

    def _build_request(self, model: str, messages: list[dict], tools: list[dict] | None, max_tokens: int) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
        return body

    def complete(self, model: str, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 1024) -> LLMResponse:
        body = self._build_request(model, messages, tools, max_tokens)
        resp = post_json(
            url=self.endpoint,
            json_body=body,
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Anthropic native error {resp.status_code}: {resp.text}")

        data = resp.json()
        content = data.get("content", [])
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        text = "".join(text_parts)

        stop_reason = data.get("stop_reason")
        usage = self._normalize_usage(data.get("usage", {}))

        return LLMResponse(
            text=text,
            finish_reason=stop_reason,
            usage=usage,
            raw=data,
        )

    def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 1024) -> AsyncIterator[LLMResponse]:
        body = self._build_request(model, messages, tools, max_tokens)
        body["stream"] = True

        headers = self._headers()
        headers["accept"] = "text/event-stream"

        accumulated_text = ""
        usage: LLMUsage | None = None
        finish_reason: str | None = None

        def event_handler(event: dict[str, Any]) -> None:
            nonlocal accumulated_text, usage, finish_reason
            t = event.get("type")
            if t == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    accumulated_text += delta.get("text", "")
            elif t == "message_delta":
                delta = event.get("delta", {})
                finish_reason = delta.get("stop_reason")
                if "usage" in event:
                    usage = self._normalize_usage(event["usage"])
            elif t == "message_stop":
                pass

        stream_json_events(
            url=self.endpoint,
            json_body=body,
            headers=headers,
            timeout=self.timeout_seconds,
            on_event=event_handler,
        )

        if usage is None:
            usage = LLMUsage(input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0, total_tokens=0)

        yield LLMResponse(
            text=accumulated_text,
            finish_reason=finish_reason,
            usage=usage,
            raw={"stream_complete": True},
        )
