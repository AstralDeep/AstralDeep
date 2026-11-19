import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.llm_config.responses_adapter import (
    OpenAIResponsesAdapter,
    ResponsesRequest,
    ResponsesUsage,
    ResponsesError,
    ResponsesMalformedError,
    ResponsesCancelledError,
)
from backend.llm_config.types import ResolvedConfig, LLMUnavailable

@pytest.fixture
def mock_config():
    return ResolvedConfig(provider="openai", model="gpt-4o")

@pytest.fixture
def adapter(mock_config):
    return OpenAIResponsesAdapter(mock_config)

@pytest.mark.asyncio
async def test_create_response_success(adapter):
    request = ResponsesRequest(model="gpt-4o", input="Hello")
    response = await adapter.create_response(request)
    assert response["id"] == "resp_123"
    assert response["status"] == "completed"

@pytest.mark.asyncio
async def test_create_response_unavailable():
    config = ResolvedConfig(provider="anthropic", model="claude-3-opus")
    adapter = OpenAIResponsesAdapter(config)
    request = ResponsesRequest(model="claude-3-opus", input="Hello")
    with pytest.raises(LLMUnavailable):
        await adapter.create_response(request)

@pytest.mark.asyncio
async def test_stream_success(adapter):
    request = ResponsesRequest(model="gpt-4o", input="Hello")
    events = []
    async for event in adapter.stream(request):
        events.append(event)
    assert len(events) == 1
    assert events[0].type == "response.completed"

@pytest.mark.asyncio
async def test_stream_unavailable():
    config = ResolvedConfig(provider="anthropic", model="claude-3-opus")
    adapter = OpenAIResponsesAdapter(config)
    request = ResponsesRequest(model="claude-3-opus", input="Hello")
    with pytest.raises(LLMUnavailable):
        async for _ in adapter.stream(request):
            pass

def test_responses_usage_to_dict():
    usage = ResponsesUsage(input_tokens=10, output_tokens=20, total_tokens=30, reasoning_tokens=5, tool_calls=1)
    d = usage.to_dict()
    assert d["input_tokens"] == 10
    assert d["output_tokens"] == 20
    assert d["total_tokens"] == 30
    assert d["reasoning_tokens"] == 5
    assert d["tool_calls"] == 1

def test_responses_error():
    err = ResponsesError("Test error", status_code=500, retryable=True)
    assert err.status_code == 500
    assert err.retryable is True

def test_responses_malformed_error():
    err = ResponsesMalformedError("Malformed data", raw_data="{invalid")
    assert err.retryable is False
    assert err.raw_data == "{invalid"

def test_responses_cancelled_error():
    err = ResponsesCancelledError()
    assert err.retryable is False
    assert str(err) == "Request cancelled"
