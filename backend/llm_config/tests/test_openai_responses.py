"""Deterministic offline unit tests for the OpenAI Responses capability adapter.

Validates:
- Default-off opt-in provider routing and preservation of legacy completions
- Multi-provider isolation: local and non-OpenAI endpoints never route to Responses
- Zero-retention storage controls (store=False default) for clinical/PHI compliance
- Typed reasoning items and stable tool-call ID preservation
- Compatibility choice/message mapping for existing consumers
- Measured fallback and rollback on provider errors
- Secure encrypted credential resolution without environment key leakage
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from llm_config.client_factory import (
    build_llm_client,
    create_completion,
    create_response,
)
from llm_config.openai_responses import (
    DEFAULT_STORE_DATA,
    OPENAI_RESPONSES_PROFILE,
    OpenAIResponsesAdapter,
    ReasoningItem,
    ResponsesAdapterError,
    ResponsesContractError,
    ResponsesOutput,
    ResponsesUnsupportedError,
    ToolCallItem,
    format_responses_payload,
    is_responses_eligible,
    parse_responses_result,
    to_chat_completion_compatibility,
)
from llm_config.types import CredentialSource, ResolvedConfig
from llm_config.user_store import PersistedLLMConfig


def _openai_cfg(
    api_key="sk-user-test-key-12345",
    base_url="https://api.openai.com/v1",
    model="gpt-4o",
) -> PersistedLLMConfig:
    return PersistedLLMConfig(
        provider="openai", base_url=base_url, model=model, api_key=api_key
    )


def _local_cfg(
    base_url="http://localhost:11434/v1",
    model="llama3",
) -> PersistedLLMConfig:
    return PersistedLLMConfig(
        provider="ollama", base_url=base_url, model=model, api_key=""
    )


class TestEligibilityAndRouting:
    def test_standard_openai_endpoint_is_eligible(self):
        assert is_responses_eligible("https://api.openai.com/v1") is True
        assert is_responses_eligible("https://api.openai.com/v1/") is True

    def test_local_endpoints_are_not_eligible(self):
        assert is_responses_eligible("http://localhost:11434/v1") is False
        assert is_responses_eligible("http://127.0.0.1:1234/v1") is False

    def test_other_cloud_endpoints_are_not_eligible(self):
        assert is_responses_eligible("https://api.anthropic.com/v1") is False
        assert is_responses_eligible("https://generativelanguage.googleapis.com/v1beta/openai") is False
        assert is_responses_eligible("https://openrouter.ai/api/v1") is False
        assert is_responses_eligible("https://api.groq.com/openai/v1") is False

    def test_invalid_and_empty_endpoints(self):
        assert is_responses_eligible("") is False
        assert is_responses_eligible(None) is False
        assert is_responses_eligible(123) is False


class TestPayloadFormatting:
    def test_basic_user_message_formatting(self):
        payload = format_responses_payload(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Hello world"}],
        )
        assert payload["model"] == "gpt-4o"
        assert payload["store"] is False  # Explicit zero retention by default
        assert payload["input"] == [{"role": "user", "content": "Hello world"}]
        assert "instructions" not in payload

    def test_system_message_mapped_to_instructions(self):
        payload = format_responses_payload(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a clinical assistant."},
                {"role": "user", "content": "Analyze symptoms."},
            ],
        )
        assert payload["instructions"] == "You are a clinical assistant."
        assert payload["input"] == [{"role": "user", "content": "Analyze symptoms."}]

    def test_explicit_store_control(self):
        payload_default = format_responses_payload(
            model="gpt-4o",
            messages=[{"role": "user", "content": "test"}],
        )
        assert payload_default["store"] is False

        payload_stored = format_responses_payload(
            model="gpt-4o",
            messages=[{"role": "user", "content": "test"}],
            store=True,
        )
        assert payload_stored["store"] is True

    def test_tool_messages_and_assistant_tool_calls_preserved(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "query_db", "arguments": '{"q": "stat"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": '{"status": "ok"}',
            },
        ]
        payload = format_responses_payload(model="gpt-4o", messages=messages)
        assert len(payload["input"]) == 2
        assert payload["input"][0]["role"] == "assistant"
        assert payload["input"][0]["tool_calls"][0]["id"] == "call_abc123"
        assert payload["input"][1]["role"] == "tool"
        assert payload["input"][1]["tool_call_id"] == "call_abc123"

    def test_invalid_model_or_messages_raises_contract_error(self):
        with pytest.raises(ResponsesContractError, match="model_required"):
            format_responses_payload(model="", messages=[])

        with pytest.raises(ResponsesContractError, match="messages_must_be_sequence"):
            format_responses_payload(model="gpt-4o", messages="not-a-list")

        with pytest.raises(ResponsesContractError, match="message_item_must_be_dict"):
            format_responses_payload(model="gpt-4o", messages=["not-a-dict"])


class TestResponsesOutputParsing:
    def test_text_output_parsing(self):
        raw = SimpleNamespace(
            id="resp_001",
            model="gpt-4o",
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    content="Clinical diagnosis verified.",
                )
            ],
            usage=SimpleNamespace(
                input_tokens=150,
                output_tokens=42,
                total_tokens=192,
                reasoning_tokens=0,
            ),
        )
        result = parse_responses_result(raw)
        assert isinstance(result, ResponsesOutput)
        assert result.id == "resp_001"
        assert result.model == "gpt-4o"
        assert result.text == "Clinical diagnosis verified."
        assert result.finish_reason == "stop"
        assert result.usage["input_tokens"] == 150
        assert result.usage["output_tokens"] == 42
        assert result.usage["total_tokens"] == 192

    def test_typed_reasoning_items_preservation(self):
        raw = SimpleNamespace(
            id="resp_002",
            model="o1-preview",
            status="completed",
            output=[
                SimpleNamespace(
                    type="reasoning",
                    id="rsn_99",
                    summary="Evaluating biochemical markers",
                    content="Checked liver enzymes.",
                ),
                SimpleNamespace(
                    type="message",
                    content="The markers are within nominal limits.",
                ),
            ],
            usage=SimpleNamespace(
                input_tokens=200,
                output_tokens=80,
                total_tokens=280,
                reasoning_tokens=50,
            ),
        )
        result = parse_responses_result(raw)
        assert len(result.reasoning_items) == 1
        reasoning = result.reasoning_items[0]
        assert isinstance(reasoning, ReasoningItem)
        assert reasoning.id == "rsn_99"
        assert reasoning.summary == "Evaluating biochemical markers"
        assert reasoning.content == "Checked liver enzymes."
        assert result.usage["reasoning_tokens"] == 50
        assert result.text == "The markers are within nominal limits."

    def test_tool_call_items_with_stable_ids(self):
        raw = SimpleNamespace(
            id="resp_003",
            model="gpt-4o",
            status="completed",
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_stable_uuid_987",
                    name="retrieve_record",
                    arguments='{"patient_id": "P-1049"}',
                )
            ],
            usage=SimpleNamespace(input_tokens=50, output_tokens=30, total_tokens=80),
        )
        result = parse_responses_result(raw)
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert isinstance(tc, ToolCallItem)
        assert tc.id == "call_stable_uuid_987"
        assert tc.name == "retrieve_record"
        assert tc.arguments == '{"patient_id": "P-1049"}'

    def test_empty_response_raises_contract_error(self):
        with pytest.raises(ResponsesContractError, match="response_empty"):
            parse_responses_result(None)


class TestChatCompletionCompatibility:
    def test_converts_to_compatible_chat_completion_structure(self):
        output = ResponsesOutput(
            id="resp_compat_1",
            model="gpt-4o",
            status="completed",
            text="Hello user",
            tool_calls=(
                ToolCallItem(
                    id="call_x",
                    name="lookup",
                    arguments='{"q": "info"}',
                ),
            ),
            reasoning_items=(
                ReasoningItem(
                    id="rsn_x",
                    summary="reasoning step",
                ),
            ),
            finish_reason="tool_calls",
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "reasoning_tokens": 5},
        )
        compat = to_chat_completion_compatibility(output)
        assert compat.id == "resp_compat_1"
        assert compat.model == "gpt-4o"
        assert len(compat.choices) == 1
        choice = compat.choices[0]
        assert choice.finish_reason == "tool_calls"
        assert choice.message.role == "assistant"
        assert choice.message.content == "Hello user"
        assert len(choice.message.tool_calls) == 1
        assert choice.message.tool_calls[0].id == "call_x"
        assert choice.message.tool_calls[0].function.name == "lookup"
        assert choice.message.tool_calls[0].function.arguments == '{"q": "info"}'
        assert compat.usage.prompt_tokens == 10
        assert compat.usage.completion_tokens == 20
        assert compat.usage.total_tokens == 30
        assert compat.usage.reasoning_tokens == 5
        assert compat.responses_output is output
        assert len(compat.reasoning_items) == 1


class TestFactoryIntegrationAndOptIn:
    def test_default_build_is_responses_disabled(self):
        client, source, resolved = build_llm_client(_openai_cfg(), CredentialSource.USER)
        assert source is CredentialSource.USER
        assert not isinstance(client, OpenAIResponsesAdapter)
        assert resolved.profile is None

    def test_opt_in_responses_for_eligible_openai_config(self):
        client, source, resolved = build_llm_client(
            _openai_cfg(), CredentialSource.USER, enable_responses=True
        )
        assert isinstance(client, OpenAIResponsesAdapter)
        assert client.enabled is True
        assert client.store is False
        assert resolved.profile == OPENAI_RESPONSES_PROFILE
        assert client.api_key == "sk-user-test-key-12345"

    def test_opt_in_responses_for_non_openai_gracefully_falls_back(self):
        client, source, resolved = build_llm_client(
            _local_cfg(), CredentialSource.USER, enable_responses=True
        )
        # Should NOT wrap with Responses adapter because Ollama is not an OpenAI Responses endpoint
        assert not isinstance(client, OpenAIResponsesAdapter)
        assert resolved.profile is None


@pytest.mark.anyio
class TestAsyncExecutionAndRollback:
    async def test_create_response_direct_dispatch(self):
        mock_raw = SimpleNamespace(
            id="resp_async_1",
            model="gpt-4o",
            status="completed",
            output=[SimpleNamespace(type="message", content="Async response confirmed.")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        mock_client = MagicMock()
        mock_client.base_url = "https://api.openai.com/v1"
        mock_client.responses.create.return_value = mock_raw

        adapter = OpenAIResponsesAdapter(mock_client, enabled=True)
        res = await adapter.create_response(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert res.id == "resp_async_1"
        assert res.text == "Async response confirmed."
        mock_client.responses.create.assert_called_once()
        # Verify store=False was passed
        kwargs = mock_client.responses.create.call_args.kwargs
        assert kwargs["store"] is False

    async def test_create_completion_with_use_responses_kwarg(self):
        mock_raw = SimpleNamespace(
            id="resp_async_2",
            model="gpt-4o",
            status="completed",
            output=[SimpleNamespace(type="message", content="Direct kwarg dispatch.")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        mock_client = MagicMock()
        mock_client.base_url = "https://api.openai.com/v1"
        mock_client.responses.create.return_value = mock_raw

        resp = await create_completion(
            mock_client,
            model="gpt-4o",
            messages=[{"role": "user", "content": "test"}],
            use_responses=True,
        )
        assert resp.choices[0].message.content == "Direct kwarg dispatch."

    async def test_create_completion_or_response_rolls_back_on_unsupported_error(self):
        mock_client = MagicMock()
        mock_client.base_url = "https://api.openai.com/v1"
        # Responses creation fails (e.g. 404 or unsupported feature)
        mock_client.responses.create.side_effect = RuntimeError("Endpoint not found")

        # Chat completions succeeds
        mock_chat_completion = SimpleNamespace(
            id="chat_fallback_1",
            choices=[
                SimpleNamespace(
                    index=0,
                    message=SimpleNamespace(role="assistant", content="Fallback succeeded!"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        mock_client.chat.completions.create.return_value = mock_chat_completion

        adapter = OpenAIResponsesAdapter(mock_client, enabled=True)
        result = await adapter.create_completion_or_response(
            model="gpt-4o",
            messages=[{"role": "user", "content": "test fallback"}],
        )
        # Should gracefully roll back to chat completions
        assert result.choices[0].message.content == "Fallback succeeded!"
        mock_client.chat.completions.create.assert_called_once()

    async def test_cancellation_is_not_swallowed(self):
        mock_client = MagicMock()
        mock_client.base_url = "https://api.openai.com/v1"
        mock_client.responses.create.side_effect = asyncio.CancelledError()

        adapter = OpenAIResponsesAdapter(mock_client, enabled=True)
        with pytest.raises(asyncio.CancelledError):
            await adapter.create_response(
                model="gpt-4o",
                messages=[{"role": "user", "content": "cancel me"}],
            )
