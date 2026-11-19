"""OpenAI Responses API adapter with typed stream/tool item support.

This module provides a default-off, manually scoped adapter for the OpenAI
Responses API, preserving per-owner encrypted credential custody and normal
turn gates. It supports typed reasoning/tool items, stable tool-call IDs,
reasoning-item replay, cancellation, and malformed/incomplete response handling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

from .types import (
    CredentialSource,
    LLMUnavailable,
    ResolvedConfig,
    ResponsesCapability,
    ResponsesStreamEvent,
    ReasoningItem,
    ToolCallItem,
    ToolResultItem,
    MessageItem,
)

logger = logging.getLogger(__name__)

# Bounded retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_BACKOFF_MAX_SECONDS = 30.0

# Timeout configuration
DEFAULT_RESPONSES_TIMEOUT_SECONDS = 120.0


class ResponsesError(Exception):
    """Base exception for Responses API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ResponsesMalformedError(ResponsesError):
    """Raised when the Responses API returns malformed or incomplete data."""

    def __init__(self, message: str, raw_data: Optional[str] = None):
        super().__init__(message, retryable=False)
        self.raw_data = raw_data


class ResponsesCancelledError(ResponsesError):
    """Raised when a Responses API request is cancelled."""

    def __init__(self, message: str = "Request cancelled"):
        super().__init__(message, retryable=False)


@dataclass(frozen=True, slots=True)
class ResponsesRequest:
    """Encapsulates a Responses API request."""

    model: str
    input: Union[str, List[Dict[str, Any]]]
    instructions: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = None
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    store: bool = False  # Explicitly set to false for privacy
    metadata: Dict[str, Any] = field(default_factory=dict)
    user: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ResponsesUsage:
    """Complete auxiliary usage tracking for Responses API."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    tool_calls: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "tool_calls": self.tool_calls,
        }


class OpenAIResponsesAdapter:
    """Adapter for the OpenAI Responses API.

    This adapter is default-off and must be explicitly opted-in via feature flag.
    It preserves per-owner encrypted credential custody and normal turn gates.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = DEFAULT_RESPONSES_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = None

    def _get_client(self):
        """Lazily initialize the OpenAI client."""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def create_response(
        self,
        request: ResponsesRequest,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Create a response using the Responses API.

        Args:
            request: The Responses API request.
            cancel_event: Optional cancellation event.

        Returns:
            The response data as a dictionary.

        Raises:
            ResponsesError: If the request fails.
            ResponsesCancelledError: If the request is cancelled.
        """
        client = self._get_client()

        # Build the request parameters
        params: Dict[str, Any] = {
            "model": request.model,
            "input": request.input,
            "store": request.store,  # Explicitly false for privacy
        }

        if request.instructions is not None:
            params["instructions"] = request.instructions
        if request.tools is not None:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            params["max_output_tokens"] = request.max_output_tokens
        if request.metadata:
            params["metadata"] = request.metadata
        if request.user is not None:
            params["user"] = request.user

        # Retry logic with bounded backoff
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise ResponsesCancelledError()

                response = await client.responses.create(**params)

                # Validate response structure
                if not hasattr(response, "id") or not hasattr(response, "output"):
                    raise ResponsesMalformedError(
                        "Response missing required fields",
                        raw_data=str(response),
                    )

                return self._parse_response(response)

            except ResponsesCancelledError:
                raise
            except ResponsesMalformedError:
                raise
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    import asyncio
                    backoff = min(
                        RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt),
                        RETRY_BACKOFF_MAX_SECONDS,
                    )
                    logger.warning(
                        "Responses API request failed (attempt %d/%d): %s. Retrying in %.1fs",
                        attempt + 1,
                        self._max_retries,
                        str(e),
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "Responses API request failed after %d attempts: %s",
                        self._max_retries,
                        str(e),
                    )

        raise ResponsesError(
            f"Responses API request failed after {self._max_retries} attempts: {last_error}",
            status_code=getattr(last_error, "status_code", None),
            retryable=False,
        )

    async def create_response_stream(
        self,
        request: ResponsesRequest,
        cancel_event: Optional[Any] = None,
    ) -> AsyncIterator[ResponsesStreamEvent]:
        """Create a streaming response using the Responses API.

        Args:
            request: The Responses API request.
            cancel_event: Optional cancellation event.

        Yields:
            ResponsesStreamEvent objects representing typed stream items.

        Raises:
            ResponsesError: If the request fails.
            ResponsesCancelledError: If the request is cancelled.
        """
        client = self._get_client()

        # Build the request parameters
        params: Dict[str, Any] = {
            "model": request.model,
            "input": request.input,
            "stream": True,
            "store": request.store,  # Explicitly false for privacy
        }

        if request.instructions is not None:
            params["instructions"] = request.instructions
        if request.tools is not None:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            params["max_output_tokens"] = request.max_output_tokens
        if request.metadata:
            params["metadata"] = request.metadata
        if request.user is not None:
            params["user"] = request.user

        # Retry logic with bounded backoff
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise ResponsesCancelledError()

                stream = await client.responses.create(**params)

                # Process the stream
                async for event in stream:
                    if cancel_event is not None and cancel_event.is_set():
                        raise ResponsesCancelledError()

                    parsed_event = self._parse_stream_event(event)
                    if parsed_event is not None:
                        yield parsed_event

                return  # Stream completed successfully

            except ResponsesCancelledError:
                raise
            except ResponsesMalformedError:
                raise
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    import asyncio
                    backoff = min(
                        RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt),
                        RETRY_BACKOFF_MAX_SECONDS,
                    )
                    logger.warning(
                        "Responses API stream failed (attempt %d/%d): %s. Retrying in %.1fs",
                        attempt + 1,
                        self._max_retries,
                        str(e),
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "Responses API stream failed after %d attempts: %s",
                        self._max_retries,
                        str(e),
                    )

        raise ResponsesError(
            f"Responses API stream failed after {self._max_retries} attempts: {last_error}",
            status_code=getattr(last_error, "status_code", None),
            retryable=False,
        )

    def _parse_response(self, response: Any) -> Dict[str, Any]:
        """Parse a Responses API response into a standardized format."""
        result: Dict[str, Any] = {
            "id": getattr(response, "id", None),
            "model": getattr(response, "model", None),
            "status": getattr(response, "status", None),
            "output": [],
            "usage": ResponsesUsage(),
        }

        # Parse output items
        output = getattr(response, "output", [])
        for item in output:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                result["output"].append(self._parse_message_item(item))
            elif item_type == "reasoning":
                result["output"].append(self._parse_reasoning_item(item))
            elif item_type == "tool_call":
                result["output"].append(self._parse_tool_call_item(item))
            elif item_type == "tool_result":
                result["output"].append(self._parse_tool_result_item(item))

        # Parse usage
        usage = getattr(response, "usage", None)
        if usage is not None:
            result["usage"] = ResponsesUsage(
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
                total_tokens=getattr(usage, "total_tokens", 0),
                reasoning_tokens=getattr(usage, "reasoning_tokens", 0),
                tool_calls=getattr(usage, "tool_calls", 0),
            )

        return result

    def _parse_message_item(self, item: Any) -> MessageItem:
        """Parse a message item from the Responses API."""
        content = getattr(item, "content", [])
        text_parts = []
        for part in content:
            if getattr(part, "type", None) == "output_text":
                text_parts.append(getattr(part, "text", ""))

        return MessageItem(
            id=getattr(item, "id", None),
            role=getattr(item, "role", "assistant"),
            content="".join(text_parts),
        )

    def _parse_reasoning_item(self, item: Any) -> ReasoningItem:
        """Parse a reasoning item from the Responses API."""
        content = getattr(item, "content", [])
        text_parts = []
        for part in content:
            if getattr(part, "type", None) == "reasoning_text":
                text_parts.append(getattr(part, "text", ""))

        return ReasoningItem(
            id=getattr(item, "id", None),
            content="".join(text_parts),
            summary=getattr(item, "summary", None),
        )

    def _parse_tool_call_item(self, item: Any) -> ToolCallItem:
        """Parse a tool call item from the Responses API."""
        return ToolCallItem(
            id=getattr(item, "id", None),
            name=getattr(item, "name", None),
            arguments=getattr(item, "arguments", {}),
            call_id=getattr(item, "call_id", None),
        )

    def _parse_tool_result_item(self, item: Any) -> ToolResultItem:
        """Parse a tool result item from the Responses API."""
        return ToolResultItem(
            id=getattr(item, "id", None),
            call_id=getattr(item, "call_id", None),
            output=getattr(item, "output", None),
        )

    def _parse_stream_event(self, event: Any) -> Optional[ResponsesStreamEvent]:
        """Parse a stream event from the Responses API."""
        event_type = getattr(event, "type", None)

        if event_type == "response.created":
            response = getattr(event, "response", None)
            return ResponsesStreamEvent(
                type="response.created",
                data={"id": getattr(response, "id", None)},
            )

        elif event_type == "response.output_item.added":
            item = getattr(event, "item", None)
            item_type = getattr(item, "type", None)
            if item_type == "message":
                return ResponsesStreamEvent(
                    type="message.added",
                    data=self._parse_message_item(item),
                )
            elif item_type == "reasoning":
                return ResponsesStreamEvent(
                    type="reasoning.added",
                    data=self._parse_reasoning_item(item),
                )
            elif item_type == "tool_call":
                return ResponsesStreamEvent(
                    type="tool_call.added",
                    data=self._parse_tool_call_item(item),
                )
            elif item_type == "tool_result":
                return ResponsesStreamEvent(
                    type="tool_result.added",
                    data=self._parse_tool_result_item(item),
                )

        elif event_type == "response.output_text.delta":
            delta = getattr(event, "delta", "")
            return ResponsesStreamEvent(
                type="message.delta",
                data={"delta": delta},
            )

        elif event_type == "response.reasoning_text.delta":
            delta = getattr(event, "delta", "")
            return ResponsesStreamEvent(
                type="reasoning.delta",
                data={"delta": delta},
            )

        elif event_type == "response.completed":
            response = getattr(event, "response", None)
            usage = getattr(response, "usage", None)
            usage_data = None
            if usage is not None:
                usage_data = ResponsesUsage(
                    input_tokens=getattr(usage, "input_tokens", 0),
                    output_tokens=getattr(usage, "output_tokens", 0),
                    total_tokens=getattr(usage, "total_tokens", 0),
                    reasoning_tokens=getattr(usage, "reasoning_tokens", 0),
                    tool_calls=getattr(usage, "tool_calls", 0),
                ).to_dict()
            return ResponsesStreamEvent(
                type="response.completed",
                data={"usage": usage_data},
            )

        elif event_type == "response.failed":
            error = getattr(event, "error", None)
            error_message = getattr(error, "message", "Unknown error")
            raise ResponsesError(
                f"Response failed: {error_message}",
                status_code=getattr(error, "code", None),
                retryable=False,
            )

        # Ignore unknown event types
        return None

    def close(self) -> None:
        """Close the underlying client."""
        if self._client is not None:
            self._client.close()
            self._client = None
