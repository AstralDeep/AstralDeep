"""Qualified opt-in OpenAI Responses API adapter within existing provider and credential routing.

Maintains owner/system separation, zero-retention (store=false) PHI/HIPAA compliance,
typed reasoning and tool-call preservation, and deterministic rollback to standard
chat completions for unknown providers or unsupported endpoints.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

RESPONSES_PROFILE = "openai-responses/v1"
OPENAI_RESPONSES_PROFILE = RESPONSES_PROFILE
QUALIFIED_BASE_URLS = frozenset({
    "https://api.openai.com/v1",
    "https://api.openai.com/v1/",
})

DEFAULT_STORE_DATA = False
DEFAULT_REQUEST_TIMEOUT = 60.0
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ResponsesAdapterError(Exception):
    """Base exception for OpenAI Responses adapter errors."""
    pass


class ResponsesContractError(ResponsesAdapterError):
    """Raised when the Responses API payload or response violates wire contract."""
    pass


class ResponsesUnsupportedError(ResponsesAdapterError):
    """Raised when an operation, endpoint, or provider does not support Responses API."""
    pass


@dataclass(frozen=True, slots=True)
class ReasoningItem:
    """Typed reasoning item preserved from Responses API output."""
    id: str
    content: Optional[str] = None
    summary: Optional[str] = None
    type: str = "reasoning"


@dataclass(frozen=True, slots=True)
class ToolCallItem:
    """Stable tool-call item produced by Responses API."""
    id: str
    name: str
    arguments: str
    type: str = "function"


@dataclass(frozen=True, slots=True)
class ResponsesOutput:
    """Structured, qualified representation of an OpenAI Responses API result."""
    id: str
    model: str
    status: str
    text: str
    reasoning_items: Tuple[ReasoningItem, ...] = field(default_factory=tuple)
    tool_calls: Tuple[ToolCallItem, ...] = field(default_factory=tuple)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)
    raw_response: Any = None


def is_responses_eligible(base_url: Optional[str], model: Optional[str] = None) -> bool:
    """Determine whether a given endpoint/model is qualified for the Responses adapter.
    
    Strictly permits only standard OpenAI API endpoints. Local (Ollama, LMStudio),
    custom, or other cloud providers (Anthropic, Gemini, Groq, etc.) are rejected
    to protect multi-provider compatibility and avoid unsupported wire contracts.
    """
    if not base_url or not isinstance(base_url, str):
        return False
    normalized = base_url.strip().rstrip("/")
    # Standard OpenAI endpoint
    if normalized == "https://api.openai.com/v1":
        return True
    return False


def format_responses_payload(
    model: str,
    messages: Sequence[Dict[str, Any]],
    *,
    tools: Optional[Sequence[Dict[str, Any]]] = None,
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
    instructions: Optional[str] = None,
    store: bool = DEFAULT_STORE_DATA,
    reasoning: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Translate standard chat-completion parameters to qualified Responses API payload.
    
    Extracts system/developer messages into top-level instructions (if not provided),
    formats input items, sets explicit store=False for PHI privacy, and validates
    bounded payload limits.
    """
    if not model or not isinstance(model, str):
        raise ResponsesContractError("model_required")
    if not isinstance(messages, (list, tuple)):
        raise ResponsesContractError("messages_must_be_sequence")

    extracted_instructions = instructions
    input_items: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            raise ResponsesContractError("message_item_must_be_dict")
        role = msg.get("role")
        content = msg.get("content")

        if role in ("system", "developer") and extracted_instructions is None:
            # Map primary system message to top-level instructions
            extracted_instructions = str(content) if content is not None else ""
        elif role == "assistant" and "tool_calls" in msg and msg["tool_calls"]:
            # Preserve assistant tool calls
            calls = msg["tool_calls"]
            input_items.append({
                "role": "assistant",
                "content": content,
                "tool_calls": calls,
            })
        elif role == "tool":
            # Map tool output back into responses input format
            call_id = msg.get("tool_call_id") or msg.get("id") or ""
            input_items.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": str(content) if content is not None else "",
            })
        else:
            # Normal user or assistant message
            input_items.append({
                "role": role or "user",
                "content": content if content is not None else "",
            })

    payload: Dict[str, Any] = {
        "model": model,
        "input": input_items,
        "store": store,
    }

    if extracted_instructions:
        payload["instructions"] = extracted_instructions
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_output_tokens is not None:
        payload["max_output_tokens"] = int(max_output_tokens)
    if tools is not None:
        payload["tools"] = list(tools)
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if reasoning is not None:
        payload["reasoning"] = reasoning

    # Bounded serialization check
    try:
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ResponsesContractError("request_bytes_exceeded")
    except (TypeError, ValueError) as err:
        raise ResponsesContractError(f"serialization_failed: {err}") from err

    return payload


def parse_responses_result(response: Any) -> ResponsesOutput:
    """Parse SDK Response object or raw dict into a validated ResponsesOutput structure."""
    if response is None:
        raise ResponsesContractError("response_empty")

    resp_id = getattr(response, "id", None) or (response.get("id") if isinstance(response, dict) else "")
    model = getattr(response, "model", None) or (response.get("model") if isinstance(response, dict) else "")
    status = getattr(response, "status", None) or (response.get("status") if isinstance(response, dict) else "completed")

    text_parts: List[str] = []
    reasoning_list: List[ReasoningItem] = []
    tool_calls_list: List[ToolCallItem] = []

    raw_output = getattr(response, "output", None)
    if raw_output is None and isinstance(response, dict):
        raw_output = response.get("output", [])

    if isinstance(raw_output, (list, tuple)):
        for idx, item in enumerate(raw_output):
            # Check item type
            item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else "")
            
            # 1. Reasoning item
            if item_type == "reasoning" or hasattr(item, "summary") or (isinstance(item, dict) and "summary" in item):
                item_id = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else f"reasoning_{idx}")
                summary = getattr(item, "summary", None) or (item.get("summary") if isinstance(item, dict) else None)
                content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else None)
                reasoning_list.append(ReasoningItem(
                    id=str(item_id),
                    summary=str(summary) if summary is not None else None,
                    content=str(content) if content is not None else None,
                ))

            # 2. Tool call / Function call item
            elif item_type in ("function_call", "tool_call", "function") or hasattr(item, "name"):
                item_id = (
                    getattr(item, "call_id", None)
                    or getattr(item, "id", None)
                    or (item.get("call_id") or item.get("id") if isinstance(item, dict) else f"call_{idx}")
                )
                name = (
                    getattr(item, "name", None)
                    or (item.get("name") if isinstance(item, dict) else "")
                )
                args = (
                    getattr(item, "arguments", None)
                    or (item.get("arguments") if isinstance(item, dict) else "{}")
                )
                tool_calls_list.append(ToolCallItem(
                    id=str(item_id),
                    name=str(name),
                    arguments=str(args) if not isinstance(args, dict) else json.dumps(args),
                ))

            # 3. Message / Content item
            elif item_type in ("message", "text") or hasattr(item, "content"):
                content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else "")
                if isinstance(content, list):
                    for sub in content:
                        text_val = getattr(sub, "text", None) or (sub.get("text") if isinstance(sub, dict) else str(sub))
                        if text_val:
                            text_parts.append(str(text_val))
                elif content:
                    text_parts.append(str(content))

    # Also handle top-level text attribute if populated directly by SDK
    top_text = getattr(response, "text", None) or (response.get("text") if isinstance(response, dict) else None)
    if top_text and not text_parts:
        if isinstance(top_text, str):
            text_parts.append(top_text)
        elif hasattr(top_text, "value"):
            text_parts.append(top_text.value)

    # Usage parsing
    usage_dict: Dict[str, int] = {}
    raw_usage = getattr(response, "usage", None) or (response.get("usage") if isinstance(response, dict) else None)
    if raw_usage is not None:
        for k in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"):
            v = getattr(raw_usage, k, None) or (raw_usage.get(k) if isinstance(raw_usage, dict) else 0)
            if v is not None:
                usage_dict[k] = int(v)

    # Determine finish_reason
    finish_reason = "stop"
    if tool_calls_list:
        finish_reason = "tool_calls"
    elif status == "incomplete":
        finish_reason = "length"
    elif status == "cancelled":
        finish_reason = "cancelled"

    return ResponsesOutput(
        id=str(resp_id or "resp_unknown"),
        model=str(model or ""),
        status=str(status or "completed"),
        text="".join(text_parts),
        reasoning_items=tuple(reasoning_list),
        tool_calls=tuple(tool_calls_list),
        finish_reason=finish_reason,
        usage=usage_dict,
        raw_response=response,
    )


def to_chat_completion_compatibility(output: ResponsesOutput) -> SimpleNamespace:
    """Convert ResponsesOutput into an object identical in structure to OpenAI ChatCompletion.
    
    Allows existing consumers and orchestrators expecting choice[0].message to function
    without change, while also exposing full typed Responses metadata.
    """
    tool_calls_compat = None
    if output.tool_calls:
        tool_calls_compat = [
            SimpleNamespace(
                id=tc.id,
                type="function",
                function=SimpleNamespace(
                    name=tc.name,
                    arguments=tc.arguments,
                ),
            )
            for tc in output.tool_calls
        ]

    message = SimpleNamespace(
        role="assistant",
        content=output.text or None,
        tool_calls=tool_calls_compat,
    )

    choice = SimpleNamespace(
        index=0,
        message=message,
        finish_reason=output.finish_reason,
    )

    prompt_tokens = output.usage.get("input_tokens", 0)
    completion_tokens = output.usage.get("output_tokens", 0)
    total_tokens = output.usage.get("total_tokens", prompt_tokens + completion_tokens)

    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=output.usage.get("reasoning_tokens", 0),
    )

    return SimpleNamespace(
        id=output.id,
        model=output.model,
        choices=[choice],
        usage=usage,
        responses_output=output,
        reasoning_items=output.reasoning_items,
    )


class OpenAIResponsesAdapter:
    """Adapter wrapping an OpenAI SDK client to execute qualified Responses API calls.
    
    Provides opt-in dispatch, strict retention controls (store=False default),
    and automatic measured rollback to standard chat completions when appropriate.
    """

    def __init__(
        self,
        client: Any,
        *,
        enabled: bool = True,
        store: bool = DEFAULT_STORE_DATA,
    ) -> None:
        self._client = client
        self.enabled = enabled
        self.store = store
        self.profile = RESPONSES_PROFILE

    @property
    def client(self) -> Any:
        return self._client

    @property
    def base_url(self) -> str:
        return str(getattr(self._client, "base_url", ""))

    @property
    def api_key(self) -> str:
        return str(getattr(self._client, "api_key", ""))

    @property
    def timeout(self) -> Optional[float]:
        return getattr(self._client, "timeout", None)

    @property
    def max_retries(self) -> int:
        return getattr(self._client, "max_retries", 0)

    def __getattr__(self, name: str) -> Any:
        # Delegate any unhandled attributes to the underlying client
        return getattr(self._client, name)

    def is_eligible(self, model: Optional[str] = None) -> bool:
        """Verify that underlying client base_url is qualified for Responses API."""
        return is_responses_eligible(self.base_url, model)

    async def create_response(self, **kwargs) -> ResponsesOutput:
        """Directly call OpenAI Responses API with formatted payload and parse output."""
        if not self.enabled:
            raise ResponsesUnsupportedError("responses_adapter_disabled")

        if not self.is_eligible(kwargs.get("model")):
            raise ResponsesUnsupportedError(
                f"base_url '{self.base_url}' is not eligible for OpenAI Responses adapter"
            )

        model = kwargs.get("model", "")
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        temperature = kwargs.get("temperature")
        max_output_tokens = kwargs.get("max_output_tokens") or kwargs.get("max_tokens")
        instructions = kwargs.get("instructions")
        reasoning = kwargs.get("reasoning")
        store = kwargs.get("store", self.store)

        payload = format_responses_payload(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            instructions=instructions,
            store=store,
            reasoning=reasoning,
        )

        def _call_sync():
            if hasattr(self._client, "responses") and hasattr(self._client.responses, "create"):
                return self._client.responses.create(**payload)
            raise ResponsesUnsupportedError("client_lacks_responses_capability")

        try:
            raw_result = await asyncio.to_thread(_call_sync)
            return parse_responses_result(raw_result)
        except asyncio.CancelledError:
            raise
        except ResponsesAdapterError:
            raise
        except Exception as err:
            logger.warning("OpenAI Responses invocation failed: %s", err)
            raise ResponsesAdapterError(f"provider_error: {err}") from err

    async def create_completion_or_response(self, **kwargs) -> Any:
        """Dispatches to Responses API when eligible and enabled, falling back to chat completions."""
        model = kwargs.get("model", "")
        if self.enabled and self.is_eligible(model):
            try:
                responses_out = await self.create_response(**kwargs)
                return to_chat_completion_compatibility(responses_out)
            except (ResponsesAdapterError, Exception) as err:
                logger.info(
                    "Responses adapter fallback triggered due to: %s. Rolling back to chat completions.",
                    err,
                )

        # Fallback / generic route: unchanged chat completions
        return await asyncio.to_thread(self._client.chat.completions.create, **kwargs)
